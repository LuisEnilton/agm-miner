"""RQ1 stages. Each reads an analysis output (gold) and writes into
``DATA_DIR/rq1/<gold name>/`` with a JSON manifest next to every result."""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST
from agm_miner.config import Rq1Config
from agm_miner.rq1.sampling import stratified_sample
from agm_miner.rq1.store import run_lock
from agm_miner.rq1.units import load_units
from agm_miner.runs import code_version

SAMPLE = "sample.parquet"


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)


def _write_manifest(path: Path, gold_dir: Path, config: Rq1Config, **details: Any) -> None:
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "gold_dir": str(gold_dir),
        "analysis_manifest": json.loads((gold_dir / ANALYSIS_MANIFEST).read_text("utf-8")),
        "rq1_config": config.model_dump(mode="json"),
        **details,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def sample_sections(gold_dir: Path, output_dir: Path, config: Rq1Config) -> dict[str, Any]:
    """Stratified label-creation sample of the sections with text (``sample.parquet``)."""
    with run_lock(output_dir):
        return _sample_sections(gold_dir, output_dir, config)


def _sample_sections(gold_dir: Path, output_dir: Path, config: Rq1Config) -> dict[str, Any]:
    units = load_units(gold_dir)
    sample = stratified_sample(
        units,
        config.sample_size,
        per_repo_cap=config.per_repo_cap,
        seed=config.seed,
        length_bins=config.length_bins,
    )
    _write_parquet(sample, output_dir / SAMPLE)
    summary = {
        "units": units.height,
        "distinct_units": units["section_key"].n_unique(),
        "sampled": sample.height,
        "repositories": sample["repo_id"].n_unique(),
        "strata": dict(sorted(Counter(sample["stratum"].to_list()).items())),
    }
    _write_manifest(output_dir / "sample.json", gold_dir, config, **summary)
    return {"output": str(output_dir / SAMPLE), **summary}
