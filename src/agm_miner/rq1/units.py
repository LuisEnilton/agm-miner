"""Classification units: the sections of ``rq1_sections`` that have text.

A unit is identified by ``section_key``, a hash of its heading path and text. Database ids change
when a repository is mined again; the key does not, so LLM answers cached under it stay valid
across exports, and identical sections (e.g. a template copied into many repositories) are coded
once.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import polars as pl

RQ1_SECTIONS = "rq1_sections.parquet"
# Whole files (``artifact_id, repo_id, full_name, path, tool, kind, content``): written by the
# import of an external dataset, for classifying whole files (``agm rq1 classify --unit file``).
FILE_CONTENTS = "file_contents.parquet"


def load_file_contents(gold_dir: Path) -> pl.DataFrame:
    path = gold_dir / FILE_CONTENTS
    if not path.exists():
        raise FileNotFoundError(
            f"No {FILE_CONTENTS} in {gold_dir}: --unit file needs the whole files, written by "
            "`agm rq1 agentreadmes import` (import the dataset again with --force)"
        )
    return pl.read_parquet(path).sort("repo_id", "path", "artifact_id")


def section_key(heading_path: Sequence[str], text: str) -> str:
    canonical = json.dumps([list(heading_path), text], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_units(gold_dir: Path) -> pl.DataFrame:
    """Sections with text from ``<gold_dir>/rq1_sections.parquet``, with their ``section_key``."""
    return build_units(pl.read_parquet(gold_dir / RQ1_SECTIONS))


def build_units(sections: pl.DataFrame) -> pl.DataFrame:
    with_text = sections.filter(pl.col("text").str.strip_chars().str.len_chars() > 0)
    keys = [
        section_key(path or [], text)
        for path, text in zip(
            with_text["heading_path"].to_list(), with_text["text"].to_list(), strict=True
        )
    ]
    return with_text.with_columns(pl.Series("section_key", keys, dtype=pl.Utf8)).sort(
        "repo_id", "artifact_id", "position"
    )


def artifact_chunks(units: pl.DataFrame, size: int) -> Iterator[list[dict[str, Any]]]:
    """Sections grouped by file, in document order, in chunks of at most ``size``."""
    for _, group in units.sort("artifact_id", "position").group_by(
        "artifact_id", maintain_order=True
    ):
        rows = group.to_dicts()
        for start in range(0, len(rows), size):
            yield rows[start : start + size]
