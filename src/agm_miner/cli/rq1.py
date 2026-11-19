"""RQ1 commands: content coding with an LLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import click
import typer

from agm_miner.cli.common import (
    PANEL_EXPERIMENTAL,
    PANEL_RESULTS,
    CodebookOption,
    GoldOption,
    ModelOption,
    StudyOption,
    UnitOption,
    _context,
    _handle_errors,
    _print,
    _rq1_dirs,
    _showing_progress,
    _with_model,
    rq1_agentreadmes_app,
    rq1_app,
    rq1_validate_app,
)


@rq1_app.command("sample", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_sample(
    gold: GoldOption = None,
    size: Annotated[int | None, typer.Option(min=1, help="Override rq1.sample_size.")] = None,
    force: Annotated[
        bool, typer.Option(help="Replace a sample that already has proposals.")
    ] = False,
    study_path: StudyOption = None,
) -> None:
    """Stratified sample of sections for label creation (rq1.* in study.yaml)."""
    from agm_miner.rq1.stages import sample_sections

    settings, study = _context(study_path)
    config = study.rq1 if size is None else study.rq1.model_copy(update={"sample_size": size})
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    _print(sample_sections(gold_dir, output_dir, config, force=force))


@rq1_app.command("propose", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_propose(gold: GoldOption = None, study_path: StudyOption = None) -> None:
    """Free labels for every sampled section (rq1.propose_model). Answers are cached: running
    it again after an interruption or a daily limit continues where it stopped."""
    from agm_miner.progress import Progress, publishing
    from agm_miner.rq1.backends import make_backend
    from agm_miner.rq1.stages import propose_labels

    settings, study = _context(study_path)
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    backend = make_backend(settings, study.rq1, "propose")
    with publishing(Progress("RQ1 PROPOSE")) as progress, _showing_progress():
        result = propose_labels(gold_dir, output_dir, study.rq1, backend, progress)
    _print(result)


@rq1_app.command("consolidate", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_consolidate(
    gold: GoldOption = None,
    partial: Annotated[
        bool, typer.Option(help="Consolidate even if part of the sample has no proposals.")
    ] = False,
    study_path: StudyOption = None,
) -> None:
    """Draft codebook from the proposed labels (rq1.consolidate_model), to be curated by hand
    into codebooks/rq1.yaml."""
    from agm_miner.progress import Progress, publishing
    from agm_miner.rq1.backends import make_backend
    from agm_miner.rq1.stages import consolidate_labels

    settings, study = _context(study_path)
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    backend = make_backend(settings, study.rq1, "consolidate")
    with publishing(Progress("RQ1 CONSOLIDATE")) as progress, _showing_progress():
        result = consolidate_labels(
            gold_dir,
            output_dir,
            study.rq1,
            backend,
            seed_codebook=study.rq1.seed_codebook,
            allow_partial=partial,
            progress=progress,
        )
    _print(result)


@rq1_app.command("models", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_models(
    provider: Annotated[
        str | None,
        typer.Option(help="gemini or openrouter (default: rq1.provider in study.yaml)."),
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Models available for testing: Gemini models of your key (which are free: AI Studio) or
    free OpenRouter models that accept structured outputs. Both lists change often."""
    from agm_miner.rq1.backends import list_free_structured_models, list_gemini_models
    from agm_miner.rq1.llm import LLMAuthError

    settings, study = _context(study_path)
    chosen = provider or study.rq1.provider
    if chosen == "openrouter":
        _print(list_free_structured_models())
    elif chosen == "gemini":
        from google import genai

        if settings.gemini_api_key is None:
            raise LLMAuthError("GEMINI_API_KEY is not set in .env")
        _print(list_gemini_models(genai.Client(api_key=settings.gemini_api_key.get_secret_value())))
    else:
        raise typer.BadParameter("--provider must be gemini or openrouter")


SMOKE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}},
    "required": ["ok", "echo"],
    "additionalProperties": False,
}


@rq1_app.command("smoke", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_smoke(
    phase: Annotated[
        str, typer.Option(help="Which phase's model to try: propose, consolidate or classify.")
    ] = "classify",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Try this model for the phase instead, as provider:model."),
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """One tiny request to the configured provider: checks the key, the model and that it
    returns the requested JSON schema, before spending a day's quota."""
    from agm_miner.rq1.backends import PromptRequest, make_backend

    if phase not in ("propose", "consolidate", "classify"):
        raise typer.BadParameter("phase must be propose, consolidate or classify")
    settings, study = _context(study_path)
    study = _with_model(study, model, phase)
    backend = make_backend(settings, study.rq1, phase)
    result = backend.complete(
        PromptRequest(
            custom_id="smoke",
            system="You answer with the JSON object requested and nothing else.",
            user='Set "ok" to true and "echo" to the word agm.',
            schema=SMOKE_SCHEMA,
            max_tokens=2_000,
            schema_name="smoke",
        )
    )
    try:
        answer = json.loads(result.text or "")
    except json.JSONDecodeError:
        answer = None
    _print(
        {
            "provider": backend.provider,
            "model": backend.model,
            "served_by": result.served_by,
            "status": result.status,
            "error": result.error,
            "usage": result.usage,
            "answer": answer if answer is not None else result.text,
            "schema_ok": isinstance(answer, dict) and answer.get("echo") == "agm",
        }
    )


def _rq1_backend_run(
    stage: str,
    phase: str,
    study_path: Path | None,
    gold: Path | None,
    run,
    model: str | None = None,
):
    """Shared plumbing of the LLM commands: backend of ``phase``, live progress, JSON result."""
    from agm_miner.progress import Progress, publishing
    from agm_miner.rq1.backends import make_backend

    settings, study = _context(study_path)
    study = _with_model(study, model)
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    backend = make_backend(settings, study.rq1, phase)
    with publishing(Progress(stage)) as progress, _showing_progress():
        result = run(gold_dir, output_dir, study, backend, progress)
    _print(result)


@rq1_app.command("classify", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def rq1_classify(
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    limit: Annotated[
        int | None, typer.Option(min=1, help="Send at most N requests (a pilot run).")
    ] = None,
    only_labelled: Annotated[
        bool,
        typer.Option(
            help="Only the files with reference labels in --gold (an imported dataset such "
            "as Agent READMEs): what `agentreadmes score` needs, for a fraction of the cost."
        ),
    ] = False,
    kind: Annotated[
        list[str] | None,
        typer.Option(
            "--kind",
            click_type=click.Choice(["INSTRUCTION", "SKILL_MANIFEST", "SKILL_RESOURCE"]),
            help="Only artifacts of this kind (repeatable), e.g. --kind INSTRUCTION --kind "
            "SKILL_MANIFEST to leave out the skills' resource files.",
        ),
    ] = None,
    unit: UnitOption = "section",
    model: ModelOption = None,
    study_path: StudyOption = None,
) -> None:
    """Categories of every section with text (rq1.classify_model), file by file; with --unit
    file, of every whole file, one request per file. Cached and resumable: run it again after an
    interruption or a daily limit."""
    from agm_miner.rq1.agentreadmes import labelled_artifact_ids
    from agm_miner.rq1.stages import classify_files, classify_sections

    classify = classify_sections if unit == "section" else classify_files
    _rq1_backend_run(
        "RQ1 CLASSIFY",
        "classify",
        study_path,
        gold,
        lambda gold_dir, output_dir, study, backend, progress: classify(
            gold_dir,
            output_dir,
            study.rq1,
            backend,
            codebook_path=codebook or study.rq1.codebook,
            limit=limit,
            artifact_ids=labelled_artifact_ids(gold_dir) if only_labelled else None,
            kinds=kind,
            progress=progress,
        ),
        model,
    )


@rq1_app.command("saturation", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_saturation(
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    model: ModelOption = None,
    study_path: StudyOption = None,
) -> None:
    """Classify a fresh sample to measure how many sections fit no category."""
    from agm_miner.rq1.stages import saturation

    _rq1_backend_run(
        "RQ1 SATURATION",
        "classify",
        study_path,
        gold,
        lambda gold_dir, output_dir, study, backend, progress: saturation(
            gold_dir,
            output_dir,
            study.rq1,
            backend,
            codebook_path=codebook or study.rq1.codebook,
            progress=progress,
        ),
        model,
    )


@rq1_validate_app.command("export", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_validate_export(
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    size: Annotated[int | None, typer.Option(min=1, help="Override rq1.validation_size.")] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing sheet.")] = False,
    study_path: StudyOption = None,
) -> None:
    """Blind sheet (CSV) to label by hand: one 0/1 column per category, no LLM answers."""
    from agm_miner.rq1.stages import export_validation

    settings, study = _context(study_path)
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    _print(
        export_validation(
            gold_dir,
            output_dir,
            study.rq1,
            codebook_path=codebook or study.rq1.codebook,
            size=size,
            force=force,
        )
    )


@rq1_validate_app.command("llm", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_validate_llm(
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    replicate: Annotated[
        int, typer.Option(min=1, help="1 for the LLM labels, 2+ for stability repetitions.")
    ] = 1,
    model: ModelOption = None,
    study_path: StudyOption = None,
) -> None:
    """The LLM's labels for the validation sample (rq1.classify_model)."""
    from agm_miner.rq1.stages import validation_llm

    _rq1_backend_run(
        "RQ1 VALIDATE",
        "classify",
        study_path,
        gold,
        lambda gold_dir, output_dir, study, backend, progress: validation_llm(
            gold_dir,
            output_dir,
            study.rq1,
            backend,
            codebook_path=codebook or study.rq1.codebook,
            replicate=replicate,
            progress=progress,
        ),
        model,
    )


@rq1_validate_app.command("score", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_validate_score(
    human: Annotated[
        Path | None,
        typer.Option(help="The filled validation sheet (CSV); without it, only the stability."),
    ] = None,
    retest: Annotated[
        Path | None, typer.Option(help="A later re-labelling of part of the sheet.")
    ] = None,
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    model: ModelOption = None,
    study_path: StudyOption = None,
) -> None:
    """Agreement: human x LLM, LLM x LLM (stability) and human x human (re-test)."""
    from agm_miner.rq1.stages import score_validation

    settings, study = _context(study_path)
    study = _with_model(study, model)
    _, output_dir = _rq1_dirs(settings, gold)
    _print(
        score_validation(
            output_dir,
            study.rq1,
            codebook_path=codebook or study.rq1.codebook,
            human=human,
            retest=retest,
            provider=study.rq1.provider,
            model=study.rq1.classify_model,
        )
    )


@rq1_app.command("compare", rich_help_panel=PANEL_EXPERIMENTAL)
@_handle_errors
def rq1_compare(
    model: Annotated[
        list[str],
        typer.Option(
            "--model", help="A classifier as provider:model; give exactly two (A, then B)."
        ),
    ],
    human: Annotated[
        Path | None,
        typer.Option(help="The filled validation sheet, for human x A and human x B."),
    ] = None,
    gold: GoldOption = None,
    codebook: CodebookOption = None,
    study_path: StudyOption = None,
) -> None:
    """Compare two classifiers: on the validation sample (human x A, human x B, A x B) and on
    every section (A x B, label prevalence), with what `validate llm --model` and `classify
    --model` produced for each."""
    from agm_miner.rq1.stages import compare_models

    if len(model) != 2:
        raise typer.BadParameter("give exactly two models", param_hint="--model")
    settings, study = _context(study_path)
    models = []
    for spec in model:
        rq1 = _with_model(study, spec).rq1
        models.append((rq1.provider, rq1.classify_model))
    if models[0] == models[1]:
        raise typer.BadParameter("the two models must differ", param_hint="--model")
    _, output_dir = _rq1_dirs(settings, gold)
    _print(
        compare_models(
            output_dir,
            codebook_path=codebook or study.rq1.codebook,
            models=models,
            human=human,
        )
    )


@rq1_agentreadmes_app.command("import", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def rq1_agentreadmes_import(
    dataset: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Dataset Parquet (Hugging Face).")
    ],
    human_labels: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="final_am_classification_3.csv"),
    ] = None,
    gpt5_labels: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="gpt_5_file_classifications.csv"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(help="Output folder (default: DATA_DIR/external/agentreadmes)."),
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing output.")] = False,
    study_path: StudyOption = None,
) -> None:
    """Import the Agent READMEs files (and the paper's labels) as an analysis output, with the
    RQ2 metrics of every file (rq2_structure); then run
    `agm rq1 classify --gold <output> --codebook codebooks/seed_agent_readmes.yaml`."""
    from agm_miner.rq1.agentreadmes import import_agentreadmes

    settings, study = _context(study_path)
    _print(
        import_agentreadmes(
            dataset,
            output or settings.external_dir / "agentreadmes",
            human_path=human_labels,
            gpt5_path=gpt5_labels,
            force=force,
            analysis=study.analysis,
        )
    )


@rq1_agentreadmes_app.command("score", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def rq1_agentreadmes_score(
    gold: Annotated[
        Path | None,
        typer.Option(help="The imported dataset (default: DATA_DIR/external/agentreadmes)."),
    ] = None,
    codebook: Annotated[
        Path, typer.Option(help="Codebook of the classification and of the reference labels.")
    ] = Path("codebooks/seed_agent_readmes.yaml"),
    tool: Annotated[
        list[str] | None,
        typer.Option("--tool", help="Score only this tool (CLAUDE, AGENTS, COPILOT); repeatable."),
    ] = None,
    unit: UnitOption = "section",
    model: ModelOption = None,
    study_path: StudyOption = None,
) -> None:
    """File-level agreement of the classification with the paper's human labels, next to GPT-5,
    overall and per tool (model: --model, or rq1.provider and rq1.classify_model). With --unit
    file, also compares the whole-file and the per-section classification of the model."""
    from agm_miner.rq1.agentreadmes import score_agentreadmes

    settings, study = _context(study_path)
    study = _with_model(study, model)
    gold_dir, output_dir = _rq1_dirs(settings, gold or settings.external_dir / "agentreadmes")
    _print(
        score_agentreadmes(
            gold_dir,
            output_dir,
            study.rq1,
            codebook_path=codebook,
            provider=study.rq1.provider,
            model=study.rq1.classify_model,
            tools=tool,
            unit=unit,
        )
    )
