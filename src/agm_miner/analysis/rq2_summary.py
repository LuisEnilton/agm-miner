"""RQ2 summary: the structure of the artifacts, per file type, from ``rq2_structure``.

Reads only an analysis output (``rq2_structure.parquet``), so the same code summarises the study
sample and the files of the *Agent READMEs* paper (imported by ``agm rq1 agentreadmes import``).
Both go through the same filters (``min_bytes``, file types), and every number is given for four
cuts, because the two datasets differ in ways that change the numbers:

* ``all`` / ``root``: the paper has only root files; ~70% of our instruction files are nested;
* ``files`` / ``repositories``: a repository can have dozens of nested files; ``repositories``
  keeps one file per repository (the root one when it exists, else the shallowest path).

``size_bands`` gives the share of files with each element per size band, to tell a difference
of population (e.g. smaller files) from a difference of writing style.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST

RQ2_STRUCTURE = "rq2_structure.parquet"
SUMMARY_JSON, SUMMARY_MD = "rq2_summary.json", "rq2_summary.md"

FILE_TYPES = ("CLAUDE.md", "AGENTS.md", "SKILL.md")
SIZE_METRICS = ("lines", "words", "chars", "headings", "sections", "max_heading_depth")
ELEMENTS = (
    "code_blocks",
    "inline_code",
    "tables",
    "bullet_lists",
    "ordered_lists",
    "task_items",
    "links",
    "images",
    "blockquotes",
    "html_blocks",
    "emphasis",
)
WORD_BINS = (250, 500, 1000)  # < 250, 250-499, 500-999, >= 1000
SIZE_BANDS = (4096, 8192, 16384)  # bytes: < 4 KB, 4-8 KB, 8-16 KB, >= 16 KB
CUTS = (("all", "files"), ("all", "repositories"), ("root", "files"), ("root", "repositories"))


def file_types(structure: pl.DataFrame) -> pl.DataFrame:
    """``file_type`` of every row (CLAUDE.md, AGENTS.md or SKILL.md); other rows (skill resources,
    files of tools this study does not mine) are dropped."""
    typed = structure.with_columns(
        pl.when(pl.col("kind") == "SKILL_MANIFEST")
        .then(pl.lit("SKILL.md"))
        .when((pl.col("kind") == "INSTRUCTION") & (pl.col("tool") == "CLAUDE"))
        .then(pl.lit("CLAUDE.md"))
        .when((pl.col("kind") == "INSTRUCTION") & (pl.col("tool") == "AGENTS"))
        .then(pl.lit("AGENTS.md"))
        .otherwise(None)
        .alias("file_type")
    )
    return typed.filter(pl.col("file_type").is_not_null())


def prepare(structure: pl.DataFrame, min_bytes: int) -> pl.DataFrame:
    """Rows of the summary: typed files with at least ``min_bytes``, never symlinks."""
    typed = file_types(structure)
    return typed.filter(
        (pl.col("size_bytes") >= min_bytes) & ~pl.col("is_symlink").fill_null(False)
    )


def one_per_repository(files: pl.DataFrame) -> pl.DataFrame:
    """One file per repository and file type: the root one, else the shallowest path."""
    return (
        files.with_columns(
            (pl.col("scope") != "ROOT").alias("_nested"),
            pl.col("path").str.count_matches("/").alias("_depth"),
        )
        .sort("repo_id", "file_type", "_nested", "_depth", "path")
        .unique(["repo_id", "file_type"], keep="first", maintain_order=True)
        .drop("_nested", "_depth")
    )


def _cut(files: pl.DataFrame, scope: str, unit: str) -> pl.DataFrame:
    selected = files if scope == "all" else files.filter(pl.col("scope") == "ROOT")
    return selected if unit == "files" else one_per_repository(selected)


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _distribution(column: pl.Series) -> dict[str, float | None]:
    values = column.drop_nulls()
    if values.is_empty():
        return {"mean": None, "q1": None, "median": None, "q3": None}
    return {
        "mean": _round(values.mean()),
        "q1": _round(values.quantile(0.25, "nearest")),
        "median": _round(values.median()),
        "q3": _round(values.quantile(0.75, "nearest")),
    }


def _bins(values: pl.Series, edges: Sequence[int]) -> dict[str, float]:
    labels = [f"<{edges[0]}"]
    labels += [f"{low}-{high - 1}" for low, high in zip(edges, edges[1:], strict=False)]
    labels.append(f">={edges[-1]}")
    counts = Counter()
    for value in values.to_list():
        index = sum(1 for edge in edges if value >= edge)
        counts[labels[index]] += 1
    total = len(values) or 1
    return {label: round(counts[label] / total, 3) for label in labels}


def _frontmatter(files: pl.DataFrame) -> dict[str, Any]:
    with_frontmatter = files.filter(pl.col("has_frontmatter").fill_null(False))
    valid = with_frontmatter.filter(pl.col("frontmatter_valid").fill_null(False))
    keys = Counter(key for keys in valid["frontmatter_keys"].to_list() for key in keys or [])
    return {
        "with_frontmatter": _round(with_frontmatter.height / files.height)
        if files.height
        else None,
        "valid_among_with_frontmatter": _round(valid.height / with_frontmatter.height)
        if with_frontmatter.height
        else None,
        "keys": {key: round(n / valid.height, 3) for key, n in keys.most_common()}
        if valid.height
        else {},
    }


def describe(files: pl.DataFrame) -> dict[str, Any]:
    """Every RQ2 number of one group of files."""
    n = files.height
    english = files.filter(pl.col("language") == "en")
    depth = files["max_heading_depth"].fill_null(0)
    return {
        "files": n,
        "repositories": files["repo_id"].n_unique(),
        "size": {metric: _distribution(files[metric]) for metric in SIZE_METRICS},
        "words_bins": _bins(files["words"].fill_null(0), WORD_BINS) if n else {},
        "max_heading_depth": {
            str(level) if level < 4 else "4+": _round(
                (depth.clip(upper_bound=4) == level).sum() / n
            )
            for level in range(5)
        }
        if n
        else {},
        "with_element": {
            element: _round((files[element].fill_null(0) > 0).sum() / n) if n else None
            for element in ELEMENTS
        },
        "per_file": {element: _distribution(files[element]) for element in ELEMENTS},
        "readability": {
            "english_files": english.height,
            "flesch_reading_ease": _distribution(english["flesch_reading_ease"]),
            "flesch_kincaid_grade": _distribution(english["flesch_kincaid_grade"]),
        },
        "frontmatter": _frontmatter(files),
    }


def size_bands(files: pl.DataFrame, elements: Sequence[str] = ELEMENTS) -> dict[str, Any]:
    """Per size band: number of files and share of them with each element."""
    labels = [f"<{SIZE_BANDS[0]}"]
    labels += [f"{lo}-{hi - 1}" for lo, hi in zip(SIZE_BANDS, SIZE_BANDS[1:], strict=False)]
    labels.append(f">={SIZE_BANDS[-1]}")
    band = pl.sum_horizontal([(pl.col("size_bytes") >= edge).cast(pl.Int32) for edge in SIZE_BANDS])
    banded = files.with_columns(band.alias("_band"))
    result: dict[str, Any] = {}
    for index, label in enumerate(labels):
        group = banded.filter(pl.col("_band") == index)
        result[label] = {
            "files": group.height,
            **{
                element: _round((group[element].fill_null(0) > 0).sum() / group.height)
                if group.height
                else None
                for element in elements
            },
        }
    return result


def summarize(structure: pl.DataFrame, *, min_bytes: int) -> dict[str, Any]:
    """The summary of one dataset: per cut, per file type."""
    files = prepare(structure, min_bytes)
    present = [t for t in FILE_TYPES if t in set(files["file_type"].to_list())]
    return {
        "min_bytes": min_bytes,
        "files": files.height,
        "repositories": files["repo_id"].n_unique(),
        "scope_shares": {
            file_type: _round(
                files.filter(
                    (pl.col("file_type") == file_type) & (pl.col("scope") == "ROOT")
                ).height
                / files.filter(pl.col("file_type") == file_type).height
            )
            for file_type in present
        },
        "cuts": {
            f"{scope}/{unit}": {
                file_type: describe(group)
                for file_type in present
                if (
                    group := _cut(files.filter(pl.col("file_type") == file_type), scope, unit)
                ).height
            }
            for scope, unit in CUTS
        },
        "size_bands": {
            file_type: size_bands(files.filter(pl.col("file_type") == file_type))
            for file_type in present
        },
    }


# --------------------------------------------------------------------------- report


def _fmt(value: Any, percent: bool = False) -> str:
    if value is None:
        return "—"
    return f"{100 * value:.0f}%" if percent else f"{value:g}"


def _comparison_rows(
    ours: Mapping[str, Any], theirs: Mapping[str, Any] | None, cut: str
) -> list[str]:
    lines = []
    for file_type, group in ours["cuts"][cut].items():
        other = (theirs or {}).get("cuts", {}).get(cut, {}).get(file_type)
        header = f"| metric | {file_type} (ours, n={group['files']}) |"
        rule = "|---|---|"
        if other:
            header += f" {file_type} (compared, n={other['files']}) |"
            rule += "---|"
        lines += [f"### {file_type}", "", header, rule]
        for metric in SIZE_METRICS:
            row = f"| median {metric} | {_fmt(group['size'][metric]['median'])} |"
            if other:
                row += f" {_fmt(other['size'][metric]['median'])} |"
            lines.append(row)
        for element in ELEMENTS:
            row = f"| with {element} | {_fmt(group['with_element'][element], True)} |"
            if other:
                row += f" {_fmt(other['with_element'][element], True)} |"
            lines.append(row)
        row = (
            "| median Flesch reading ease (en) | "
            f"{_fmt(group['readability']['flesch_reading_ease']['median'])} |"
        )
        if other:
            row += f" {_fmt(other['readability']['flesch_reading_ease']['median'])} |"
        lines += [row, ""]
    return lines


def summary_markdown(summary: Mapping[str, Any]) -> str:
    ours, theirs = summary["dataset"], summary.get("compared")
    lines = [
        "# RQ2: structure of the artifacts",
        "",
        f"Dataset: `{summary['gold_dir']}` ({ours['files']} files, {ours['repositories']} "
        f"repositories, files >= {ours['min_bytes']} bytes).",
    ]
    if theirs:
        lines.append(
            f"Compared with: `{summary['compare_dir']}` ({theirs['files']} files, same filters)."
        )
    lines += [
        "",
        "Share of root files: "
        + ", ".join(f"{t} {_fmt(v, True)}" for t, v in ours["scope_shares"].items()),
        "",
    ]
    for cut in ours["cuts"]:
        lines += [f"## Cut: {cut}", ""]
        lines += _comparison_rows(ours, theirs, cut)
    lines += ["## Code blocks by size band (all/files)", ""]
    lines += ["| file type | dataset | band (bytes) | files | with code blocks | with tables |"]
    lines.append("|---|---|---|---|---|---|")
    for name, data in (("ours", ours), ("compared", theirs)):
        if not data:
            continue
        for file_type, bands in data["size_bands"].items():
            for band, values in bands.items():
                lines.append(
                    f"| {file_type} | {name} | {band} | {values['files']} | "
                    f"{_fmt(values['code_blocks'], True)} | {_fmt(values['tables'], True)} |"
                )
    skill = ours["cuts"]["all/files"].get("SKILL.md")
    if skill:
        frontmatter = skill["frontmatter"]
        lines += [
            "",
            "## SKILL.md frontmatter (all/files)",
            "",
            f"With frontmatter: {_fmt(frontmatter['with_frontmatter'], True)}; valid YAML among "
            f"them: {_fmt(frontmatter['valid_among_with_frontmatter'], True)}.",
            "",
            "| key | share of valid frontmatters |",
            "|---|---|",
        ]
        lines += [f"| {key} | {_fmt(share, True)} |" for key, share in frontmatter["keys"].items()]
    lines.append("")
    return "\n".join(lines)


def _manifest(directory: Path) -> dict[str, Any]:
    return json.loads((directory / ANALYSIS_MANIFEST).read_text(encoding="utf-8"))


def write_summary(
    gold_dir: Path,
    output_dir: Path,
    *,
    min_bytes: int,
    compare_dir: Path | None = None,
    code_version: str | None = None,
) -> dict[str, Any]:
    """Write ``rq2_summary.json`` and ``rq2_summary.md`` into ``output_dir`` (through a
    ``.partial`` folder renamed at the end). The JSON embeds the ``analysis_manifest.json`` of
    each dataset, as the RQ1 manifests do."""
    for directory in (gold_dir, compare_dir):
        if directory is None:
            continue
        for name in (RQ2_STRUCTURE, ANALYSIS_MANIFEST):
            if not (directory / name).exists():
                raise FileNotFoundError(f"No {name} in {directory}")
    ours = summarize(pl.read_parquet(gold_dir / RQ2_STRUCTURE), min_bytes=min_bytes)
    theirs = (
        summarize(pl.read_parquet(compare_dir / RQ2_STRUCTURE), min_bytes=min_bytes)
        if compare_dir is not None
        else None
    )
    summary: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "gold_dir": str(gold_dir),
        "compare_dir": None if compare_dir is None else str(compare_dir),
        # Where each dataset came from (export, configuration and code of the analysis), so a
        # table of the paper can be traced back to the export it was computed from.
        "analysis_manifest": _manifest(gold_dir),
        "compared_analysis_manifest": None if compare_dir is None else _manifest(compare_dir),
        "dataset": ours,
        "compared": theirs,
    }
    partial = output_dir.with_name(f"{output_dir.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    (partial / SUMMARY_JSON).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (partial / SUMMARY_MD).write_text(summary_markdown(summary), encoding="utf-8")
    shutil.rmtree(output_dir, ignore_errors=True)
    partial.rename(output_dir)
    return {
        "output": str(output_dir),
        "files": ours["files"],
        "compared_files": None if theirs is None else theirs["files"],
        "by_type": {t: g["files"] for t, g in ours["cuts"]["all/files"].items()},
    }
