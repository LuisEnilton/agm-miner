"""Commands that compute the study's results from analysis outputs (no database)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agm_miner.cli.common import (
    PANEL_RESULTS,
    CodebookOption,
    ModelOption,
    StudyOption,
    _context,
    _handle_errors,
    _print,
    _showing_progress,
    _with_model,
    app,
    rq2_app,
)


@app.command(rich_help_panel=PANEL_RESULTS)
@_handle_errors
def audit(
    bronze: Annotated[
        Path | None, typer.Option(help="Bronze export directory (default: the latest one).")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Output directory (default: DATA_DIR/audit/<export>).")
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Sampling audit of a bronze export: discovery coverage, selection effects, mining failures
    and artifact coverage, with warnings (audit.json and audit.md; no database needed)."""
    from agm_miner.analysis.sampling_audit import write_audit
    from agm_miner.runs import code_version
    from agm_miner.stages.export import latest_export

    settings, study = _context(study_path)
    bronze_dir = bronze or latest_export(settings.bronze_dir)
    output_dir = output or settings.data_dir / "audit" / bronze_dir.name
    _print(write_audit(bronze_dir, output_dir, study, code_version=code_version()))


@rq2_app.command("summary", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def rq2_summary(
    gold: Annotated[
        Path | None,
        typer.Option(help="Analysis output to summarise (default: the latest in DATA_DIR/gold)."),
    ] = None,
    compare: Annotated[
        Path | None,
        typer.Option(
            help="Another analysis output summarised with the same filters, side by side "
            "(e.g. DATA_DIR/external/agentreadmes, the Agent READMEs paper's files)."
        ),
    ] = None,
    min_bytes: Annotated[
        int | None,
        typer.Option(help="Smallest file kept (default: analysis.min_artifact_bytes)."),
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Size, headings, elements, readability and SKILL.md frontmatter per file type, for all
    files and root files only, per file and one file per repository. Written to
    DATA_DIR/rq2/<gold name>/ (rq2_summary.json and .md)."""
    from agm_miner.analysis.datasets import latest_analysis
    from agm_miner.analysis.rq2_summary import write_summary
    from agm_miner.runs import code_version

    settings, study = _context(study_path)
    gold_dir = gold or latest_analysis(settings.gold_dir)
    output_dir = settings.data_dir / "rq2" / gold_dir.name
    if compare is not None:
        output_dir = output_dir.with_name(f"{gold_dir.name}__vs__{compare.name}")
    _print(
        write_summary(
            gold_dir,
            output_dir,
            min_bytes=study.analysis.min_artifact_bytes if min_bytes is None else min_bytes,
            compare_dir=compare,
            code_version=code_version(),
        )
    )


@app.command("figures", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def figures(
    gold: Annotated[
        Path | None,
        typer.Option(help="Analysis output (default: the latest in DATA_DIR/gold)."),
    ] = None,
    audit_dir: Annotated[
        Path | None,
        typer.Option("--audit", help="Audit of the same export (default: DATA_DIR/audit/<gold>)."),
    ] = None,
    compare: Annotated[
        Path | None,
        typer.Option(
            help="Imported Agent READMEs files (default: DATA_DIR/external/agentreadmes)."
        ),
    ] = None,
    model: ModelOption = None,
    codebook: CodebookOption = None,
    labels: Annotated[
        Path | None,
        typer.Option(
            help="RQ1 classify folder of the analysis (default: DATA_DIR/rq1/<gold>/classify/"
            "<codebook>/<provider>/<model>)."
        ),
    ] = None,
    compare_labels: Annotated[
        Path | None,
        typer.Option(
            help="RQ1 classify folder of the Agent READMEs files (default: the same folder under "
            "DATA_DIR/rq1/<compare name>)."
        ),
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Output folder (default: DATA_DIR/figures/<gold>).")
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """The paper's figures (pipeline, QP2 elements, QP1 categories) as LaTeX macros in
    figuras.tex, computed from the analysis, the audit and the RQ1 classification (no database).
    """
    from agm_miner.analysis.datasets import latest_analysis
    from agm_miner.analysis.paper_figures import write_figures
    from agm_miner.rq1.codebook import load_codebook
    from agm_miner.rq1.stages import model_slug
    from agm_miner.runs import code_version

    settings, study = _context(study_path)
    study = _with_model(study, model)
    gold_dir = gold or latest_analysis(settings.gold_dir)
    compare_dir = compare or settings.external_dir / "agentreadmes"
    loaded = load_codebook(codebook or study.rq1.codebook)

    def classify_dir(dataset: Path) -> Path:
        folder = settings.rq1_dir / dataset.name / "classify" / model_slug(loaded.label)
        return folder / study.rq1.provider / model_slug(study.rq1.classify_model)

    _print(
        write_figures(
            gold_dir=gold_dir,
            audit_dir=audit_dir or settings.data_dir / "audit" / gold_dir.name,
            compare_dir=compare_dir,
            labels_dir=labels or classify_dir(gold_dir),
            compare_labels_dir=compare_labels or classify_dir(compare_dir),
            output_dir=output or settings.data_dir / "figures" / gold_dir.name,
            categories=loaded.ids,
            names={category.id: category.name for category in loaded.categories},
            code_version=code_version(),
        )
    )


@app.command("package", rich_help_panel=PANEL_RESULTS)
@_handle_errors
def package(
    bronze: Annotated[
        Path | None, typer.Option(help="Bronze export directory (default: the latest one).")
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(help="Output folder (default: DATA_DIR/package/<export>)."),
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Replication package of an export for Zenodo: the bronze export and its analysis with the
    personal data pseudonymised (PACKAGE_SALT), the RQ1 cache and outputs, the results, the
    configuration, a data dictionary and reproduzir.sh (no database needed)."""
    from agm_miner.progress import Progress, publishing
    from agm_miner.replication.anonymize import Pseudonymizer
    from agm_miner.replication.package import PackageInputError, PackageInputs, build_package
    from agm_miner.runs import code_version
    from agm_miner.stages.export import latest_export

    settings, study = _context(study_path)
    if settings.package_salt is None or len(settings.package_salt.get_secret_value()) < 16:
        raise PackageInputError(
            "PACKAGE_SALT is not set (.env): a secret of at least 16 characters that "
            "pseudonymises the personal data; keep it to rebuild the same pseudonyms, and never "
            "publish it"
        )
    bronze_dir = bronze or latest_export(settings.bronze_dir)
    inputs = PackageInputs(
        bronze_dir=bronze_dir,
        gold_dir=settings.gold_dir / bronze_dir.name,
        data_dir=settings.data_dir,
        study_path=study_path or settings.study_config_path,
        codebooks_dir=Path("codebooks"),
        output_dir=output or settings.data_dir / "package" / bronze_dir.name,
    )
    pseudonymizer = Pseudonymizer(
        settings.package_salt.get_secret_value().encode("utf-8"), study.analysis
    )
    with publishing(Progress("PACKAGE")) as progress, _showing_progress():
        result = build_package(
            inputs, pseudonymizer, code_version=code_version(), progress=progress
        )
    _print(result)
