"""External validation with the dataset of *Agent READMEs* (Chatlatanagulchai et al., 2025).

The paper released 2,303 context files (``hao-li/AgentREADMEs`` on Hugging Face: ``CLAUDE.md``,
``AGENTS.md`` and ``copilot-instructions.md``) and, in its replication package
(``woraamy/Agent-Context-File-Analysis``), the labels its authors gave 332 of them by hand with
their 16-label taxonomy, plus the labels GPT-5 gave the same files.

``import_agentreadmes`` turns the dataset into an analysis-like output (``rq1_sections`` and an
``analysis_manifest.json``), so every ``agm rq1`` command runs on it with ``--gold``. The
reference labels are mapped onto ``codebooks/seed_agent_readmes.yaml``. ``score_agentreadmes``
then compares, file by file, our classification (with that codebook) with the human labels, next
to GPT-5 as the paper's baseline.

Nothing is filtered at import: ``copilot-instructions.md`` (tool ``COPILOT``, which this study
does not mine) stays in, and the score can be restricted to some tools.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST, SECTIONS_SCHEMA, document_section_rows
from agm_miner.analysis.markdown import parse_document
from agm_miner.config import Rq1Config
from agm_miner.rq1.agreement import agreement
from agm_miner.rq1.codebook import OTHER, load_codebook
from agm_miner.rq1.stages import _codebook_slug, _write_manifest, model_slug
from agm_miner.rq1.store import run_lock
from agm_miner.rq1.units import RQ1_SECTIONS, build_units, load_units
from agm_miner.runs import code_version

FILES = "files.parquet"
REFERENCE_LABELS = "reference_labels.parquet"
HUMAN, GPT5 = "human", "gpt-5"

# The dataset's `agent` column -> our tool names (COPILOT is not mined by this study).
TOOLS = {"claude": "CLAUDE", "agents": "AGENTS", "copilot-instructions": "COPILOT"}

# Labels as written in final_am_classification_3.csv (typo included) -> seed codebook ids.
HUMAN_LABELS = {
    "System Overview": "system_overview",
    "Claude/AI Integration": "ai_integration",
    "Documentation": "documentation",
    "Architecture": "architecture",
    "Implementation Details": "implementation_details",
    "Build & Run": "build_and_run",
    "Test": "testing",
    "Configuration & Environment": "configuration_environment",
    "Deployment and Operations": "devops",
    "Development Process": "development_process",
    "Project Management": "project_management",
    "Maintanability": "maintenance",
    "Maintainability": "maintenance",
    "Debugging": "debugging",
    "Performance": "performance",
    "Security": "security",
    "UI/UX": "ui_ux",
}

# Label keys of gpt_5_file_classifications.csv -> seed codebook ids.
GPT5_LABELS = {
    "system_overview": "system_overview",
    "ai_integration": "ai_integration",
    "documentation_references": "documentation",
    "architecture": "architecture",
    "implementation_details": "implementation_details",
    "build_run": "build_and_run",
    "test": "testing",
    "config_environment": "configuration_environment",
    "deployment_operations": "devops",
    "development_process": "development_process",
    "project_management": "project_management",
    "maintainability": "maintenance",
    "debugging": "debugging",
    "performance": "performance",
    "security": "security",
    "ui_ux": "ui_ux",
}

_FILES_SCHEMA = {
    "artifact_id": pl.Int64,
    "repo_id": pl.Int64,
    "full_name": pl.Utf8,
    "path": pl.Utf8,
    "tool": pl.Utf8,
    "file_url": pl.Utf8,
    "stars": pl.Float64,
    "created_at": pl.Utf8,
    "content_commit_sha": pl.Utf8,
    "bytes": pl.Int64,
    "sections": pl.Int64,
}

_REFERENCE_SCHEMA = {
    "rater": pl.Utf8,
    "label_id": pl.Utf8,  # the row id shared by both CSVs ("#" / "ID")
    "file_url": pl.Utf8,
    "artifact_id": pl.Int64,  # null when the labelled file is not in the dataset
    "categories": pl.List(pl.Utf8),
}


class AgentReadmesError(ValueError):
    """The input files do not look like the published dataset or labels."""


class OutputExistsError(RuntimeError):
    """The import would replace an existing output."""


# --------------------------------------------------------------------------- import (pure)


def build_files(dataset: pl.DataFrame) -> pl.DataFrame:
    """One row per context file, with ids derived from the content only (sorted URLs and
    repository names), so the same dataset always gets the same ids."""
    missing = {"content", "agent", "repository_owner", "repository_name", "file_path", "file_url"}
    missing -= set(dataset.columns)
    if missing:
        raise AgentReadmesError(f"the dataset has no column {sorted(missing)}")
    unknown = sorted(set(dataset["agent"].unique().to_list()) - set(TOOLS))
    if unknown:
        raise AgentReadmesError(f"unknown agent values {unknown}")
    duplicated = dataset.filter(pl.col("file_url").is_duplicated())["file_url"].unique()
    if duplicated.len():
        raise AgentReadmesError(f"duplicated file_url: {sorted(duplicated.to_list())[:5]}")

    files = (
        dataset.with_columns(
            (pl.col("repository_owner") + "/" + pl.col("repository_name")).alias("full_name")
        )
        .sort("file_url")
        .with_row_index("artifact_id", offset=1)
    )
    return files.with_columns(
        pl.col("artifact_id").cast(pl.Int64),
        pl.col("full_name").rank("dense").cast(pl.Int64).alias("repo_id"),
        pl.col("file_path").alias("path"),
        pl.col("agent").replace_strict(TOOLS).alias("tool"),
    )


def build_sections(files: pl.DataFrame) -> pl.DataFrame:
    """``rq1_sections`` of every file, parsed exactly as ``agm analyze`` parses artifacts."""
    rows: list[dict[str, Any]] = []
    for file in files.iter_rows(named=True):
        document = parse_document(file["content"] or "")
        rows.extend(
            document_section_rows(
                document, file["artifact_id"], file["artifact_id"], file["repo_id"]
            )
        )
    sections = pl.DataFrame(rows, schema=SECTIONS_SCHEMA)
    attributes = files.select(
        pl.col("artifact_id"),
        "full_name",
        "path",
        "tool",
        pl.lit("INSTRUCTION").alias("kind"),
        pl.when(pl.col("path").str.contains("/") & ~pl.col("path").str.starts_with(".github/"))
        .then(pl.lit("NESTED"))
        .otherwise(pl.lit("ROOT"))
        .alias("scope"),
        pl.lit(None, dtype=pl.Utf8).alias("skill_name"),
    )
    return sections.join(attributes, on="artifact_id", how="inner").sort(
        "repo_id", "artifact_id", "position"
    )


def _map_labels(values: Sequence[str], mapping: Mapping[str, str], source: str) -> list[str]:
    unknown = sorted({value for value in values if value not in mapping})
    if unknown:
        raise AgentReadmesError(f"unknown {source} labels {unknown}")
    return sorted({mapping[value] for value in values})


def parse_human_labels(frame: pl.DataFrame) -> dict[str, tuple[str, list[str]]]:
    """``final_am_classification_3.csv`` -> row id -> (file URL, seed category ids). Rows
    without any label stay in with no category, as in the paper's evaluation."""
    columns = [column for column in frame.columns if column.lower().startswith("label")]
    if "#" not in frame.columns or "file_url" not in frame.columns or not columns:
        raise AgentReadmesError("the human labels need the columns '#', 'file_url' and Label*")
    labels: dict[str, tuple[str, list[str]]] = {}
    for row in frame.iter_rows(named=True):
        values = [str(row[c]).strip() for c in columns if row[c] and str(row[c]).strip()]
        labels[str(row["#"])] = (row["file_url"], _map_labels(values, HUMAN_LABELS, "human"))
    return labels


def _parse_list(cell: str | None) -> list[str]:
    if cell is None or not cell.strip():
        return []
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(cell)
        except (ValueError, SyntaxError):
            continue
        if isinstance(parsed, list | tuple):
            return [str(value) for value in parsed]
    raise AgentReadmesError(f"cannot read the label list {cell!r}")


def parse_gpt5_labels(frame: pl.DataFrame) -> dict[str, list[str]]:
    """``gpt_5_file_classifications.csv`` -> row id -> seed category ids."""
    if "ID" not in frame.columns or "type" not in frame.columns:
        raise AgentReadmesError("the GPT-5 labels need the columns 'ID' and 'type'")
    return {
        str(row["ID"]): _map_labels(_parse_list(row["type"]), GPT5_LABELS, "GPT-5")
        for row in frame.iter_rows(named=True)
    }


def build_reference_labels(
    files: pl.DataFrame,
    human: Mapping[str, tuple[str, list[str]]],
    gpt5: Mapping[str, list[str]] | None = None,
) -> pl.DataFrame:
    """Both raters per labelled file; GPT-5 rows are matched to files through the row id."""
    artifact_of = dict(files.select("file_url", "artifact_id").iter_rows())
    unknown_ids = sorted(set(gpt5 or {}) - set(human))
    if unknown_ids:
        raise AgentReadmesError(f"GPT-5 rows without a human row: {unknown_ids[:5]}")
    rows = []
    for label_id, (url, categories) in human.items():
        rows.append(
            {
                "rater": HUMAN,
                "label_id": label_id,
                "file_url": url,
                "artifact_id": artifact_of.get(url),
                "categories": categories,
            }
        )
        if gpt5 is not None and label_id in gpt5:
            rows.append(rows[-1] | {"rater": GPT5, "categories": gpt5[label_id]})
    return pl.DataFrame(rows, schema=_REFERENCE_SCHEMA).sort("rater", "label_id")


# --------------------------------------------------------------------------- import (I/O)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def import_agentreadmes(
    dataset_path: Path,
    output_dir: Path,
    *,
    human_path: Path | None = None,
    gpt5_path: Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write ``output_dir`` (``rq1_sections``, ``files``, ``reference_labels`` and the
    manifest) from the dataset Parquet and, optionally, the paper's label CSVs. Written into a
    ``.partial`` folder and renamed at the end, like ``agm analyze``."""
    if gpt5_path is not None and human_path is None:
        raise AgentReadmesError("the GPT-5 labels are matched through the human labels' row ids")
    if output_dir.exists() and not force:
        raise OutputExistsError(f"{output_dir} already exists; use --force to replace it")

    files = build_files(pl.read_parquet(dataset_path))
    sections = build_sections(files)
    with_text = build_units(sections)
    files = files.join(
        with_text.group_by("artifact_id").agg(pl.len().cast(pl.Int64).alias("sections")),
        on="artifact_id",
        how="left",
    ).with_columns(
        pl.col("sections").fill_null(0),
        pl.col("content").str.len_bytes().cast(pl.Int64).alias("bytes"),
    )
    for column, dtype in _FILES_SCHEMA.items():
        if column not in files.columns:
            files = files.with_columns(pl.lit(None, dtype=dtype).alias(column))
    files = files.select([pl.col(c).cast(t) for c, t in _FILES_SCHEMA.items()])

    outputs: dict[str, pl.DataFrame] = {RQ1_SECTIONS: sections, FILES: files}
    inputs = {"dataset": dataset_path}
    reference_summary: dict[str, Any] = {}
    if human_path is not None:
        human = parse_human_labels(pl.read_csv(human_path, infer_schema=False))
        gpt5 = None
        if gpt5_path is not None:
            gpt5 = parse_gpt5_labels(pl.read_csv(gpt5_path, infer_schema=False))
            inputs["gpt5_labels"] = gpt5_path
        inputs["human_labels"] = human_path
        reference = build_reference_labels(files, human, gpt5)
        outputs[REFERENCE_LABELS] = reference
        humans = reference.filter(pl.col("rater") == HUMAN)
        reference_summary = {
            "human_files": humans.height,
            "human_files_in_dataset": humans["artifact_id"].is_not_null().sum(),
            "human_files_without_label": humans.filter(pl.col("categories").list.len() == 0).height,
            "gpt5_files": reference.filter(pl.col("rater") == GPT5).height,
            "missing_from_dataset": sorted(
                humans.filter(pl.col("artifact_id").is_null())["file_url"].to_list()
            ),
        }

    partial = output_dir.with_name(f"{output_dir.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    for name, frame in outputs.items():
        frame.write_parquet(partial / name)
    summary = {
        "files": files.height,
        "files_by_tool": dict(sorted(files.group_by("tool").len().iter_rows())),
        "repositories": files["repo_id"].n_unique(),
        "sections": sections.height,
        "sections_with_text": with_text.height,
        **reference_summary,
    }
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "source": "Agent READMEs (Chatlatanagulchai et al. 2025, arXiv:2511.12884)",
        "inputs": {
            name: {"path": str(path), "sha256": _sha256(path)} for name, path in inputs.items()
        },
        "outputs": {name.removesuffix(".parquet"): frame.height for name, frame in outputs.items()},
        **summary,
    }
    (partial / ANALYSIS_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    shutil.rmtree(output_dir, ignore_errors=True)
    partial.rename(output_dir)
    return {"output": str(output_dir), **summary}


# --------------------------------------------------------------------------- score


def labelled_artifact_ids(gold_dir: Path) -> list[int]:
    """Files of an imported dataset that have human reference labels (``agm rq1 classify
    --only-labelled`` classifies just these, which is all the score needs)."""
    path = gold_dir / REFERENCE_LABELS
    if not path.exists():
        raise FileNotFoundError(
            f"No {REFERENCE_LABELS} in {gold_dir}: --only-labelled needs a dataset imported "
            "with its reference labels (agm rq1 agentreadmes import --human-labels)"
        )
    reference = pl.read_parquet(path).filter(
        (pl.col("rater") == HUMAN) & pl.col("artifact_id").is_not_null()
    )
    return sorted(reference["artifact_id"].unique().to_list())


def predicted_file_labels(section_labels: pl.DataFrame) -> dict[int, frozenset[str]]:
    """A file has every category one of its sections has (``other`` is not a category)."""
    return {
        artifact_id: frozenset(categories) - {OTHER}
        for artifact_id, categories in section_labels.group_by("artifact_id")
        .agg(pl.col("category").unique())
        .iter_rows()
    }


def _rater(reference: pl.DataFrame, rater: str) -> dict[int, frozenset[str]]:
    return {
        artifact_id: frozenset(categories)
        for artifact_id, categories in reference.filter(
            (pl.col("rater") == rater) & pl.col("artifact_id").is_not_null()
        )
        .select("artifact_id", "categories")
        .iter_rows()
    }


def _comparisons(
    human: Mapping[int, frozenset[str]],
    llm: Mapping[int, frozenset[str]],
    gpt5: Mapping[int, frozenset[str]],
    files: Sequence[int],
    categories: Sequence[str],
) -> dict[str, Any]:
    def restrict(labels: Mapping[int, frozenset[str]]) -> dict[int, frozenset[str]]:
        return {key: labels[key] for key in files if key in labels}

    result = {"human_vs_llm": agreement(restrict(human), restrict(llm), categories)}
    if gpt5:
        result["human_vs_gpt5"] = agreement(restrict(human), restrict(gpt5), categories)
        result["gpt5_vs_llm"] = agreement(restrict(gpt5), restrict(llm), categories)
    return result


def score_agentreadmes(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    *,
    codebook_path: Path,
    provider: str,
    model: str,
    tools: Sequence[str] | None = None,
) -> dict[str, Any]:
    """File-level agreement of our classification (``agm rq1 classify`` with the seed
    codebook) with the human labels, next to GPT-5's, overall and per tool.

    The files are the human-labelled files of the dataset (``tools`` only, when given) whose
    sections were all classified. A file without any section with text gets no category: that
    is our method's answer for it. Files whose classification is incomplete are left out and
    counted in ``unclassified_files``.
    """
    with run_lock(output_dir):
        codebook = load_codebook(codebook_path)
        reference_path = gold_dir / REFERENCE_LABELS
        if not reference_path.exists():
            raise FileNotFoundError(
                f"No {REFERENCE_LABELS} in {gold_dir}: import it with --human-labels"
            )
        reference = pl.read_parquet(reference_path)
        used = {c for cats in reference["categories"].to_list() for c in cats}
        foreign = sorted(used - set(codebook.ids))
        if foreign:
            raise AgentReadmesError(
                f"the codebook {codebook.label} has no category {foreign}: score with "
                "codebooks/seed_agent_readmes.yaml (the codebook of the reference labels)"
            )
        classified_dir = output_dir / "classify" / _codebook_slug(codebook) / provider
        classified_dir = classified_dir / model_slug(model)
        if not (classified_dir / "section_labels.parquet").exists():
            raise FileNotFoundError(
                f"No classification in {classified_dir}: run `agm rq1 classify --gold "
                f"{gold_dir} --codebook {codebook_path}` first"
            )
        section_labels = pl.read_parquet(classified_dir / "section_labels.parquet")
        files = pl.read_parquet(gold_dir / FILES).select("artifact_id", "tool")
        if tools:
            unknown = sorted(set(tools) - set(TOOLS.values()))
            if unknown:
                raise AgentReadmesError(f"unknown tools {unknown}; use {sorted(TOOLS.values())}")
            files = files.filter(pl.col("tool").is_in(list(tools)))
        tool_of = dict(files.iter_rows())

        human, gpt5 = _rater(reference, HUMAN), _rater(reference, GPT5)
        units = load_units(gold_dir)
        sections_of = units.group_by("artifact_id").agg(pl.col("section_key").unique())
        with_text = {artifact for artifact, _ in sections_of.iter_rows()}
        predicted = predicted_file_labels(section_labels)
        labelled_keys = set(section_labels["section_key"].unique().to_list())
        incomplete = {
            artifact for artifact, keys in sections_of.iter_rows() if set(keys) - labelled_keys
        }
        llm: dict[int, frozenset[str]] = {}
        unclassified: list[int] = []
        for artifact in sorted(set(human) & set(tool_of)):
            if artifact in incomplete:
                unclassified.append(artifact)
            else:  # a file without text has no section, hence no category
                llm[artifact] = predicted.get(artifact, frozenset())
        scored = sorted(llm)
        if not scored:
            raise AgentReadmesError("no labelled file is classified yet")

        categories = codebook.ids
        scores: dict[str, Any] = {
            "codebook": codebook.label,
            "provider": provider,
            "model": model,
            "tools": sorted(tools) if tools else sorted(set(tool_of.values())),
            "files": len(scored),
            "files_without_text": sum(1 for a in scored if a not in with_text),
            "unclassified_files": len(unclassified),
            **_comparisons(human, llm, gpt5, scored, categories),
            "by_tool": {
                tool: _comparisons(
                    human, llm, gpt5, [a for a in scored if tool_of[a] == tool], categories
                )
                for tool in sorted({tool_of[a] for a in scored})
            },
        }
        destination = output_dir / "agentreadmes" / _codebook_slug(codebook) / provider
        destination = destination / model_slug(model)
        destination.mkdir(parents=True, exist_ok=True)
        stem = "scores" if not tools else "scores-" + "-".join(sorted(tools))
        (destination / f"{stem}.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
        (destination / f"{stem}.md").write_text(scores_markdown(scores), encoding="utf-8")
        summary = _summary(scores)
        _write_manifest(destination / f"{stem}.manifest.json", gold_dir, config, **summary)
        return {"output": str(destination / f"{stem}.json"), **summary}


_TITLES = {
    "human_vs_llm": "Human × our classifier",
    "human_vs_gpt5": "Human × GPT-5 (paper's baseline)",
    "gpt5_vs_llm": "GPT-5 × our classifier",
}
_HEADLINE = ("sections", "macro_kappa", "macro_kappa_ci95", "macro_f1", "micro_f1")


def _summary(scores: Mapping[str, Any]) -> dict[str, Any]:
    def headline(result: Mapping[str, Any]) -> dict[str, Any]:
        return {("files" if k == "sections" else k): result[k] for k in _HEADLINE}

    summary = {k: scores[k] for k in ("tools", "files", "files_without_text", "unclassified_files")}
    for comparison in _TITLES:
        if comparison in scores:
            summary[comparison] = headline(scores[comparison])
    summary["by_tool"] = {
        tool: {c: headline(r) for c, r in comparisons.items()}
        for tool, comparisons in scores["by_tool"].items()
    }
    return summary


def scores_markdown(scores: Mapping[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.2f}"

    lines = [
        f"# Agent READMEs: file-level agreement ({scores['codebook']}, "
        f"{scores['provider']} {scores['model']})",
        "",
        f"Tools: {', '.join(scores['tools'])}. {scores['files']} human-labelled files "
        f"({scores['files_without_text']} without text); {scores['unclassified_files']} left "
        "out (classification incomplete).",
        "",
    ]
    for comparison, title in _TITLES.items():
        if comparison not in scores:
            continue
        result = scores[comparison]
        lines += [
            f"## {title}",
            "",
            f"{result['sections']} files; micro F1 {fmt(result['micro_f1'])}; macro F1 "
            f"{fmt(result['macro_f1'])}; macro kappa {fmt(result['macro_kappa'])} "
            f"{result['macro_kappa_ci95'] or ''}",
            "",
            "| category | kappa | precision | recall | F1 | prevalence A | prevalence B |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in result["categories"]:
            lines.append(
                f"| {row['category']} | {fmt(row['kappa'])} | {fmt(row['precision'])} | "
                f"{fmt(row['recall'])} | {fmt(row['f1'])} | {row['prevalence_a']:.2f} | "
                f"{row['prevalence_b']:.2f} |"
            )
        lines.append("")
    lines += ["## By tool", "", "| tool | comparison | files | micro F1 | macro F1 | macro kappa |"]
    lines.append("|---|---|---|---|---|---|")
    for tool, comparisons in scores["by_tool"].items():
        for comparison, result in comparisons.items():
            lines.append(
                f"| {tool} | {_TITLES[comparison]} | {result['sections']} | "
                f"{fmt(result['micro_f1'])} | {fmt(result['macro_f1'])} | "
                f"{fmt(result['macro_kappa'])} |"
            )
    lines.append("")
    return "\n".join(lines)
