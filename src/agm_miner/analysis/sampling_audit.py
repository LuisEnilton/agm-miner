"""Sampling audit: where discovery, selection and mining can bias the sample (``agm audit``).

Reads only a bronze export, like ``agm analyze``, and checks it against a study configuration:

* **discovery**: which configured queries were searched, the size range collected for each,
  the estimated share of the query's population that range holds (the code search counts are
  estimates), truncated slices (unreachable results), and slices, runs and repositories that
  come from outside the current protocol (another size range or query);
* **selection**: the funnel (first failing criterion) and, per criterion, how many repositories
  fail it at all and how many fail *only* it (they would be kept without it); popularity and
  creation year of the kept repositories; stored statuses the configuration no longer agrees
  with;
* **mining**: failures per error type, omitted contents, when the mining ran and how many
  repositories were mined after the reference date;
* **history**: what cutting the history at the reference date removes (``history_cutoff``),
  measured even when the cut is disabled; repositories mined before the cutoff (days of
  history they may miss), artifacts without recorded changes, eligible files at HEAD versus at
  the cutoff;
* **artifacts** (mined repositories, at HEAD, before the human-contributor filter of
  ``repo_features``): per file type, symlinks and files below ``min_artifact_bytes``, eligible
  files larger than the size range collected for their query, repositories whose files of one
  type were reached only through another type's query; how concentrated the eligible files are
  in few repositories and how many are duplicates.

``warnings`` lists, in plain words, every finding that limits what the sample can claim.
"""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.audit_latex import AUDIT_TEX, DRAFT_TEX, audit_tex, draft_tex
from agm_miner.analysis.history_cutoff import cut_history, cutoff_instant
from agm_miner.analysis.rq2_summary import FILE_TYPES, file_types
from agm_miner.artifacts import ArtifactKind, ArtifactTool, classify_path
from agm_miner.config import AnalysisConfig, DiscoveryConfig, SelectionConfig, StudyConfig
from agm_miner.stages.export import read_manifest
from agm_miner.stages.selection import (
    CRITERIA,
    NOT_FOUND,
    RepositoryMetadata,
    failed_criteria,
    reference_date,
)

AUDIT_JSON, AUDIT_MD = "audit.json", "audit.md"
AUDIT_FILES = frozenset({AUDIT_JSON, AUDIT_MD, AUDIT_TEX, DRAFT_TEX})
KEPT_STATUSES = ("SELECTED", "MINED", "FAILED")
MANUAL_PREFIX = "manual:"
_SIZED_KINDS = (ArtifactKind.INSTRUCTION.value, ArtifactKind.SKILL_MANIFEST.value)
_VERSION_COLUMNS = ["artifact_id", "repo_id", "blob_sha", "is_symlink", "size_bytes"]
_VERSION_COLUMNS += ["content_omitted"]
_TABLES = ("repositories", "discovery_hits", "discovery_slices", "artifacts", "artifact_changes")
# Large tables, read without the columns the audit does not need (contents, messages).
_COLUMNS = {
    "artifact_versions": _VERSION_COLUMNS,
    "commits": ["repo_id", "sha", "author_identity_id", "authored_at", "committed_at"],
}
_SLICE_SIZE = r" size:(\d+)\.\.(\d+)$"
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9, 0.99)


def load_tables(bronze_dir: Path) -> dict[str, pl.DataFrame]:
    """The bronze tables the audit needs (artifact versions without their content)."""
    tables: dict[str, pl.DataFrame] = {}
    for name in (*_TABLES, *_COLUMNS):
        if not (bronze_dir / f"{name}.parquet").exists():
            raise FileNotFoundError(f"No {name}.parquet in {bronze_dir}")
    for name in _TABLES:
        tables[name] = pl.read_parquet(bronze_dir / f"{name}.parquet")
    for name, columns in _COLUMNS.items():
        tables[name] = pl.read_parquet(bronze_dir / f"{name}.parquet", columns=columns)
    return tables


# --------------------------------------------------------------------------- helpers


def query_file_type(query: str) -> str | None:
    """The file type a search query looks for, read from the file name it contains."""
    text = query.lower()
    if "skill.md" in text:
        return "SKILL.md"
    if "agents.md" in text:
        return "AGENTS.md"
    if "claude.md" in text or "claude.local.md" in text:
        return "CLAUDE.md"
    return None


def path_file_type(path: str) -> str | None:
    """CLAUDE.md, AGENTS.md or SKILL.md for an artifact path, else ``None``."""
    match = classify_path(path) if path else None
    if match is None or match.kind is ArtifactKind.SKILL_RESOURCE:
        return None
    if match.kind is ArtifactKind.SKILL_MANIFEST:
        return "SKILL.md"
    return "AGENTS.md" if match.tool is ArtifactTool.AGENTS else "CLAUDE.md"


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of inclusive integer ranges (adjacent ranges are joined)."""
    merged: list[tuple[int, int]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def _quantiles(values: pl.Series) -> dict[str, float] | None:
    values = values.drop_nulls()
    if values.is_empty():
        return None
    return {f"p{round(q * 100)}": float(values.quantile(q, "nearest")) for q in QUANTILES}


def _share(part: int, whole: int | None) -> float | None:
    # Six decimals keep tiny shares (1 in 50,000) apart from zero in the paper's tables.
    return round(part / whole, 6) if whole else None


def _slices(slices: pl.DataFrame, config: DiscoveryConfig, queries: list[str]) -> pl.DataFrame:
    """Slices with their size range and whether they belong to the current protocol."""
    parsed = slices.with_columns(
        pl.col("slice_query").str.extract(_SLICE_SIZE, 1).cast(pl.Int64).alias("low"),
        pl.col("slice_query").str.extract(_SLICE_SIZE, 2).cast(pl.Int64).alias("high"),
        (pl.col("slice_query") == pl.col("base_query")).alias("whole_query"),
    )
    in_range = (pl.col("low") >= config.size_min) & (pl.col("high") <= config.size_max)
    return parsed.with_columns(
        (pl.col("base_query").is_in(queries) & (pl.col("whole_query") | in_range))
        .fill_null(False)
        .alias("in_protocol")
    )


# --------------------------------------------------------------------------- discovery


def _query_coverage(query: str, slices: pl.DataFrame, config: DiscoveryConfig) -> dict[str, Any]:
    own = slices.filter(pl.col("base_query") == query)
    root_queries = (query, f"{query} size:{config.size_min}..{config.size_max}")
    root = own.filter(pl.col("slice_query").is_in(root_queries))
    population = int(root["total_count"].max()) if root.height else None
    collected = own.filter((pl.col("outcome") == "COLLECTED") & pl.col("in_protocol"))
    whole = bool(collected["whole_query"].any()) if collected.height else False
    ranges = merge_ranges(
        collected.filter(~pl.col("whole_query")).select("low", "high").iter_rows()
    )
    if whole:
        ranges = [(config.size_min, config.size_max)]
    frontier = ranges[0][1] if ranges and ranges[0][0] == config.size_min else None
    totals = collected["total_count"]
    cap = config.max_results_per_query
    matches = int(totals.sum()) if collected.height else 0
    return {
        "file_type": query_file_type(query),
        "searched": own.height > 0,
        "estimated_matches": population,
        "collected_matches": matches,
        "estimated_coverage": _share(matches, population),
        "covered_ranges": [list(r) for r in ranges],
        "complete": ranges == [(config.size_min, config.size_max)],
        # Largest size collected without a gap from size_min (None: size_min not collected).
        "contiguous_up_to": frontier,
        "truncated_slices": int((totals > cap).sum()) if collected.height else 0,
        "unreachable_matches": int((totals - cap).clip(lower_bound=0).sum())
        if collected.height
        else 0,
    }


def discovery_coverage(
    tables: Mapping[str, pl.DataFrame], config: DiscoveryConfig
) -> dict[str, Any]:
    """Coverage of every configured query, and what was collected outside the protocol."""
    queries = list(config.queries)
    slices = _slices(tables["discovery_slices"], config, queries)
    hits, repositories = tables["discovery_hits"], tables["repositories"]
    per_query = {query: _query_coverage(query, slices, config) for query in queries}
    for query, coverage in per_query.items():
        own_hits = hits.filter(pl.col("query") == query)
        coverage["hits"] = own_hits.height
        coverage["repositories"] = own_hits["repo_id"].n_unique()

    collected = slices.filter(pl.col("outcome") == "COLLECTED")
    outside = collected.filter(~pl.col("in_protocol"))
    # A run is outside the protocol when every slice it collected is. Slices keep the id of
    # the last run that collected them, so a run without slices is not judged.
    by_run = collected.group_by("run_id").agg(pl.col("in_protocol").any().alias("any_inside"))
    outside_runs = sorted(by_run.filter(~pl.col("any_inside"))["run_id"].to_list())
    origin = hits.with_columns(
        pl.when(pl.col("query").str.starts_with(MANUAL_PREFIX))
        .then(pl.lit("manual"))
        .when(~pl.col("query").is_in(queries) | pl.col("run_id").is_in(outside_runs))
        .then(pl.lit("outside"))
        .otherwise(pl.lit("protocol"))
        .alias("origin")
    )
    per_repo = origin.group_by("repo_id").agg(pl.col("origin").unique().alias("origins"))
    status = repositories.select(pl.col("id").alias("repo_id"), "status")
    per_repo = per_repo.join(status, on="repo_id", how="left").with_columns(
        pl.col("status").is_in(KEPT_STATUSES).fill_null(False).alias("kept"),
        pl.col("origins").list.contains("protocol").alias("from_protocol"),
        pl.col("origins").list.contains("manual").alias("manual"),
    )
    not_protocol = per_repo.filter(~pl.col("from_protocol"))
    only_outside = not_protocol.filter(~pl.col("manual"))
    manual = not_protocol.filter(pl.col("manual"))
    kept = int(per_repo["kept"].sum())
    return {
        "queries": per_query,
        "queries_in_data_not_configured": sorted(
            set(hits["query"].unique().to_list())
            - set(queries)
            - {q for q in hits["query"].unique().to_list() if q.startswith(MANUAL_PREFIX)}
        ),
        "outside_protocol": {
            "slices": outside.height,
            "size_range": [outside["low"].min(), outside["high"].max()] if outside.height else None,
            "estimated_matches": int(outside["total_count"].sum()) if outside.height else 0,
            "truncated_slices": int((outside["total_count"] > config.max_results_per_query).sum())
            if outside.height
            else 0,
            "unreachable_matches": int(
                (outside["total_count"] - config.max_results_per_query).clip(lower_bound=0).sum()
            )
            if outside.height
            else 0,
            "runs": outside_runs,
            "repositories_only_from_outside": only_outside.height,
            "kept_only_from_outside": int(only_outside["kept"].sum()),
            "share_of_kept": _share(int(only_outside["kept"].sum()), kept),
        },
        "manual": {
            "repositories": manual.height,
            "kept": int(manual["kept"].sum()),
        },
    }


# --------------------------------------------------------------------------- selection


def _criterion(reason: str) -> str:
    return reason.split(":", 1)[0]


def selection_effects(repositories: pl.DataFrame, config: SelectionConfig) -> dict[str, Any]:
    """Funnel, per-criterion effects, and the profile of the kept repositories."""
    reference = reference_date(config)
    funnel = (
        repositories.group_by("status", "rejection_reason")
        .len()
        .sort(["status", "len"], descending=[False, True])
    )
    evaluated = repositories.filter(
        pl.col("metadata_fetched_at").is_not_null()
        & pl.col("gh_created_at").is_not_null()
        & (pl.col("rejection_reason").fill_null("") != NOT_FOUND)
    )
    fails: Counter[str] = Counter()
    only: Counter[str] = Counter()
    only_license: list[dict[str, Any]] = []
    disagreements = {"kept_but_failing_now": 0, "rejected_but_passing_now": 0}
    for row in evaluated.iter_rows(named=True):
        failed = [
            _criterion(r)
            for r in failed_criteria(RepositoryMetadata.from_columns(row), config, reference)
        ]
        fails.update(failed)
        if len(failed) == 1:
            only[failed[0]] += 1
            if failed[0] == "license":
                only_license.append(row)
        kept = row["status"] in KEPT_STATUSES
        if kept and failed:
            disagreements["kept_but_failing_now"] += 1
        elif not kept and row["status"] != "DISCOVERED" and not failed:
            disagreements["rejected_but_passing_now"] += 1

    is_kept = pl.col("status").is_in(KEPT_STATUSES)
    kept_rows = repositories.filter(is_kept)
    rejected_rows = evaluated.filter(~is_kept & (pl.col("status") != "DISCOVERED"))
    licenses = Counter(row["license_spdx"] or "(none)" for row in only_license)
    top_license = sorted(only_license, key=lambda row: -(row["stars"] or 0))[:10]
    years = (
        kept_rows.filter(pl.col("gh_created_at").is_not_null())
        .group_by(pl.col("gh_created_at").dt.year().alias("year"))
        .len()
    )
    return {
        "reference_date": reference.isoformat(),
        "repositories": repositories.height,
        "pending": repositories.filter(pl.col("status") == "DISCOVERED").height,
        "not_found": repositories.filter(pl.col("rejection_reason") == NOT_FOUND).height,
        "evaluated": evaluated.height,
        "kept": kept_rows.height,
        "funnel": [
            {"status": status, "reason": reason, "repositories": n}
            for status, reason, n in funnel.iter_rows()
        ],
        "criteria": {
            criterion: {
                "fail": fails[criterion],
                "fail_share": _share(fails[criterion], evaluated.height),
                "only_failure": only[criterion],
            }
            for criterion in CRITERIA
        },
        "only_license": {
            "by_license": dict(licenses.most_common()),
            "top_by_stars": [
                {"full_name": r["full_name"], "stars": r["stars"], "license": r["license_spdx"]}
                for r in top_license
            ],
        },
        "status_disagreements": disagreements,
        "stars": {
            "kept": _quantiles(kept_rows["stars"]),
            "rejected": _quantiles(rejected_rows["stars"]),
        },
        "kept_by_creation_year": {str(year): n for year, n in sorted(years.iter_rows())},
    }


# --------------------------------------------------------------------------- mining


def mining_summary(tables: Mapping[str, pl.DataFrame], reference: date) -> dict[str, Any]:
    repositories = tables["repositories"]
    mined = repositories.filter(pl.col("status") == "MINED")
    failed = repositories.filter(pl.col("status") == "FAILED")
    error_type = pl.col("last_error").fill_null("").str.extract(r"^([A-Za-z_.]+)", 1)
    failures = failed.with_columns(error_type.fill_null("unknown").alias("error_type"))
    omitted = (
        tables["artifact_versions"]
        .join(mined.select(pl.col("id").alias("repo_id")), on="repo_id", how="semi")
        .filter(pl.col("content_omitted").is_not_null())
        .group_by("content_omitted")
        .len()
    )
    mined_at = mined["mined_at"].drop_nulls()
    return {
        "mined": mined.height,
        "failed": failed.height,
        "selected_not_mined": repositories.filter(pl.col("status") == "SELECTED").height,
        "failures_by_type": dict(Counter(failures["error_type"].to_list()).most_common()),
        "failures": [
            {"full_name": name, "error_type": kind, "disk_usage_kb": size}
            for name, kind, size in failures.select("full_name", "error_type", "disk_usage_kb")
            .sort("full_name")
            .iter_rows()
        ],
        "content_omitted": {kind: n for kind, n in sorted(omitted.iter_rows())},
        "mined_from": mined_at.min().isoformat() if mined_at.len() else None,
        "mined_to": mined_at.max().isoformat() if mined_at.len() else None,
        "mined_after_reference_date": int((mined_at.dt.date() > reference).sum())
        if mined_at.len()
        else 0,
    }


# --------------------------------------------------------------------------- history cutoff


def _eligible_counts(heads: pl.DataFrame) -> dict[str, Any]:
    eligible = heads.filter(pl.col("eligible"))
    return {
        "files": {t: eligible.filter(pl.col("file_type") == t).height for t in FILE_TYPES},
        "repositories": eligible["repo_id"].n_unique(),
    }


def history_summary(tables: Mapping[str, pl.DataFrame], study: StudyConfig) -> dict[str, Any]:
    """What cutting the history at the reference date changes (``cut_history``), measured even
    when the cut is disabled, so the audit shows what the analysis would otherwise include."""
    enabled = study.analysis.truncate_at_reference_date
    reference = study.selection.reference_date
    if reference is None:
        return {"enabled": False, "reference_date": None}
    end = cutoff_instant(reference)
    mined = tables["repositories"].filter(
        (pl.col("status") == "MINED") & pl.col("mined_at").is_not_null()
    )
    early = mined.filter(pl.col("mined_at") < end)
    # Days between the earliest mining and the cutoff, rounded up to a tenth.
    missing = (
        math.ceil(max((end - at).total_seconds() for at in early["mined_at"]) / 8640) / 10
        if early.height
        else 0.0
    )
    cut, stats = cut_history(tables, reference)
    mined_ids = mined.select(pl.col("id").alias("repo_id"))
    at_head = (
        tables["artifacts"]
        .filter(pl.col("exists_at_head"))
        .join(mined_ids, on="repo_id", how="semi")
    )
    undatable = at_head.join(
        tables["artifact_changes"].select(pl.col("artifact_id").alias("id")).unique(),
        on="id",
        how="anti",
    )
    return {
        "enabled": enabled,
        **stats,
        "mined_before_cutoff": early.height,
        "max_days_missing": missing,
        "artifacts_without_recorded_changes": undatable.height,
        "eligible_at_head": _eligible_counts(head_artifacts(tables, study.analysis)),
        "eligible_at_cutoff": _eligible_counts(head_artifacts(cut, study.analysis)),
    }


def _history_markdown(history: Mapping[str, Any]) -> list[str]:
    if history["reference_date"] is None:
        return ["## History cutoff", "", "No fixed reference date: the history is not cut.", ""]
    state = "enabled" if history["enabled"] else "DISABLED (numbers show what it would remove)"
    head, cut = history["eligible_at_head"], history["eligible_at_cutoff"]
    rows = [
        f"| {file_type} | {head['files'][file_type]} | {cut['files'][file_type]} |"
        for file_type in FILE_TYPES
    ]
    return [
        "## History cutoff",
        "",
        f"Cut at {history['cutoff']} (end of {history['reference_date']}): {state}.",
        "",
        f"After the cutoff: {history['commits_after_cutoff']} commits that touched artifacts in "
        f"{history['repositories_with_commits_after_cutoff']} repositories, "
        f"{history['artifact_changes_after_cutoff']} artifact changes. "
        f"{history['artifacts_not_yet_created']} artifacts did not exist yet, "
        f"{history['artifacts_with_another_state']} had another state at the cutoff "
        f"({history['artifacts_deleted_at_cutoff']} of them deleted there), "
        f"{history['identities_dropped']} authors had not committed yet.",
        "",
        f"{history['mined_before_cutoff']} repositories were mined before the cutoff (up to "
        f"{history['max_days_missing']} days missing); "
        f"{history['artifacts_without_recorded_changes']} artifacts at HEAD have no recorded "
        "change and keep their HEAD state.",
        "",
        "| file type | eligible at HEAD | eligible at the cutoff |",
        "|---|---|---|",
        *rows,
        f"| repositories | {head['repositories']} | {cut['repositories']} |",
        "",
    ]


# --------------------------------------------------------------------------- artifacts


def head_artifacts(tables: Mapping[str, pl.DataFrame], config: AnalysisConfig) -> pl.DataFrame:
    """Typed artifacts of the mined repositories at HEAD, with ``eligible`` (non-symlink file of
    at least ``min_artifact_bytes``, the size rule of the study sample)."""
    mined = (
        tables["repositories"]
        .filter(pl.col("status") == "MINED")
        .select(pl.col("id").alias("repo_id"))
    )
    versions = tables["artifact_versions"].select(
        "artifact_id", "blob_sha", "is_symlink", "size_bytes"
    )
    heads = (
        tables["artifacts"]
        .filter(pl.col("exists_at_head") & pl.col("kind").is_in(_SIZED_KINDS))
        .join(mined, on="repo_id", how="semi")
        .rename({"id": "artifact_id"})
        .join(
            versions,
            left_on=["artifact_id", "head_blob_sha"],
            right_on=["artifact_id", "blob_sha"],
            how="left",
        )
    )
    return file_types(heads).with_columns(
        (
            ~pl.col("is_symlink").fill_null(True)
            & (pl.col("size_bytes").fill_null(0) >= config.min_artifact_bytes)
        ).alias("eligible")
    )


def artifact_coverage(
    tables: Mapping[str, pl.DataFrame],
    config: AnalysisConfig,
    discovery: Mapping[str, Any],
    size_max: int,
) -> dict[str, Any]:
    heads = head_artifacts(tables, config)
    eligible = heads.filter(pl.col("eligible"))
    hits = tables["discovery_hits"].select("repo_id", "path").unique()
    paths = hits.select("path").unique()
    typed_paths = paths.with_columns(
        pl.col("path").map_elements(path_file_type, return_dtype=pl.Utf8).alias("file_type")
    )
    hit_types = hits.join(typed_paths, on="path").filter(pl.col("file_type").is_not_null())

    searched: dict[str, dict[str, Any]] = {}
    for coverage in discovery["queries"].values():
        file_type = coverage["file_type"]
        if file_type is None or not coverage["searched"]:
            continue
        best = searched.get(file_type)
        bound = size_max if coverage["complete"] else coverage["contiguous_up_to"]
        if best is None or (bound or 0) > (best["bound"] or 0):
            searched[file_type] = {"bound": bound}

    eligible_types = {
        file_type: set(eligible.filter(pl.col("file_type") == file_type)["repo_id"].to_list())
        for file_type in FILE_TYPES
    }
    per_type: dict[str, Any] = {}
    for file_type in FILE_TYPES:
        rows = heads.filter(pl.col("file_type") == file_type)
        good = eligible.filter(pl.col("file_type") == file_type)
        bound = searched.get(file_type, {}).get("bound")
        found_by_type = set(hit_types.filter(pl.col("file_type") == file_type)["repo_id"].to_list())
        repos = eligible_types[file_type]
        others = set().union(*(r for t, r in eligible_types.items() if t != file_type))
        small_root = set(
            rows.filter((pl.col("scope") == "ROOT") & ~pl.col("eligible"))["repo_id"].to_list()
        )
        above = int((good["size_bytes"] > bound).sum()) if bound is not None else None
        per_type[file_type] = {
            "at_head": rows.height,
            "symlinks": int(rows["is_symlink"].fill_null(False).sum()),
            "below_min_bytes": rows.filter(
                ~pl.col("is_symlink").fill_null(False) & ~pl.col("eligible")
            ).height,
            "eligible": good.height,
            "eligible_size": _quantiles(good["size_bytes"]),
            "searched": file_type in searched,
            "collected_up_to": bound,
            "eligible_above_collected": above,
            "eligible_above_collected_share": _share(above, good.height)
            if above is not None
            else None,
            "eligible_above_index_limit": int((good["size_bytes"] > size_max).sum()),
            "repositories_with_eligible": len(repos),
            "repositories_reached_only_through_other_types": len(repos - found_by_type),
            "repositories_with_small_or_symlink_root": len(small_root),
            "of_which_with_other_eligible_type": len(small_root & others),
        }

    per_repo = eligible.group_by("repo_id").len()
    counts = per_repo["len"].sort(descending=True)
    top = max(1, math.ceil(per_repo.height / 100)) if per_repo.height else 0
    blobs = eligible.group_by("head_blob_sha").agg(pl.col("repo_id").n_unique().alias("repos"))
    shared = blobs.filter(pl.col("repos") > 1)
    return {
        "by_file_type": per_type,
        "concentration": {
            "eligible_files": eligible.height,
            "repositories": per_repo.height,
            "files_per_repository": _quantiles(per_repo["len"]),
            "max_files_per_repository": int(counts.max()) if per_repo.height else 0,
            "top_1pct_repositories": top,
            "top_1pct_share": _share(int(counts.head(top).sum()), eligible.height),
        },
        "duplicates": {
            "duplicate_copies": eligible.height - blobs.height,
            "duplicate_share": _share(eligible.height - blobs.height, eligible.height),
            "blobs_in_several_repositories": shared.height,
            "repository_occurrences": int(shared["repos"].sum()) if shared.height else 0,
        },
    }


# --------------------------------------------------------------------------- audit


def _warnings(audit: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    discovery = audit["discovery"]
    for query, cov in discovery["queries"].items():
        if not cov["searched"]:
            found.append(f"query {query!r} was never searched")
        elif not cov["complete"]:
            share = cov["estimated_coverage"]
            found.append(
                f"query {query!r} is incomplete: collected sizes {cov['covered_ranges']} "
                f"(contiguous up to {cov['contiguous_up_to']} bytes), "
                f"~{_pct(share)} of the estimated matches"
            )
        if cov["truncated_slices"]:
            found.append(
                f"query {query!r}: {cov['truncated_slices']} slices over the result cap, "
                f"{cov['unreachable_matches']} matches unreachable"
            )
    outside = discovery["outside_protocol"]
    if outside["kept_only_from_outside"]:
        found.append(
            f"{outside['kept_only_from_outside']} kept repositories "
            f"({_pct(outside['share_of_kept'])} of the kept) were found only by slices or runs "
            "outside the current protocol "
            f"(sizes {outside['size_range']}, runs {outside['runs']})"
        )
    if discovery["manual"]["kept"]:
        found.append(f"{discovery['manual']['kept']} kept repositories were added by hand")
    selection = audit["selection"]
    if selection["pending"]:
        found.append(f"{selection['pending']} discovered repositories were not evaluated yet")
    disagreements = selection["status_disagreements"]
    if any(disagreements.values()):
        found.append(
            "stored statuses disagree with the configuration "
            f"({disagreements}); run `agm select --reevaluate`"
        )
    # A criterion is worth a sensitivity analysis when dropping it would grow the kept set by
    # at least a tenth (a reporting threshold, not a sampling decision).
    for criterion, effect in selection["criteria"].items():
        if effect["only_failure"] and effect["only_failure"] >= 0.1 * max(selection["kept"], 1):
            found.append(
                f"criterion {criterion!r} is the only failure of {effect['only_failure']} "
                f"repositories: dropping it would add them to the {selection['kept']} kept"
            )
    mining = audit["mining"]
    if mining["failed"]:
        found.append(
            f"{mining['failed']} repositories failed to mine: {mining['failures_by_type']}"
        )
    if mining["selected_not_mined"]:
        found.append(f"{mining['selected_not_mined']} selected repositories are not mined yet")
    history = audit["history"]
    if not history["enabled"] and mining["mined_after_reference_date"]:
        found.append(
            f"{mining['mined_after_reference_date']} repositories were mined after the reference "
            f"date {selection['reference_date']} and the analysis does not cut the history "
            "(analysis.truncate_at_reference_date): their history goes past it"
        )
    if history["enabled"] and history["mined_before_cutoff"]:
        found.append(
            f"{history['mined_before_cutoff']} repositories were mined before the end of the "
            f"reference date and miss up to {history['max_days_missing']} days of history"
        )
    if history["enabled"] and history["artifacts_without_recorded_changes"]:
        found.append(
            f"{history['artifacts_without_recorded_changes']} artifacts have no recorded change "
            "(changed only in merge commits) and keep their mined HEAD state at the cutoff"
        )
    for file_type, info in audit["artifacts"]["by_file_type"].items():
        if info["eligible"] and not info["searched"]:
            found.append(
                f"{file_type}: never searched; its {info['eligible']} eligible files come from "
                "repositories found through other file types"
            )
        elif info["eligible_above_collected"]:
            found.append(
                f"{file_type}: {info['eligible_above_collected']} eligible files "
                f"({_pct(info['eligible_above_collected_share'])}) are larger than the collected "
                f"size range (up to {info['collected_up_to']} bytes)"
            )
    concentration = audit["artifacts"]["concentration"]
    if (concentration["top_1pct_share"] or 0) >= 0.2:
        found.append(
            f"the top 1% repositories hold {_pct(concentration['top_1pct_share'])} of the "
            "eligible files; report per-repository statistics too"
        )
    if audit["config_differs_from_export"]:
        found.append("the study configuration differs from the one stored in the export")
    return found


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.0f}%"


def audit_tables(tables: Mapping[str, pl.DataFrame], study: StudyConfig) -> dict[str, Any]:
    """The audit of one set of bronze tables (no warnings, no provenance)."""
    discovery = discovery_coverage(tables, study.discovery)
    selection = selection_effects(tables["repositories"], study.selection)
    return {
        "discovery": discovery,
        "selection": selection,
        "mining": mining_summary(tables, reference_date(study.selection)),
        "history": history_summary(tables, study),
        "artifacts": artifact_coverage(tables, study.analysis, discovery, study.discovery.size_max),
    }


def audit_markdown(audit: Mapping[str, Any]) -> str:
    discovery, selection = audit["discovery"], audit["selection"]
    mining, artifacts = audit["mining"], audit["artifacts"]
    lines = [
        "# Sampling audit",
        "",
        f"Bronze export: `{audit['bronze_dir']}` (exported {audit['exported_at']}).",
        "",
        "## Warnings",
        "",
    ]
    lines += [f"- {warning}" for warning in audit["warnings"]] or ["- none"]
    lines += [
        "",
        "## Discovery",
        "",
        "| query | searched | estimated matches | collected | coverage | collected sizes "
        "| truncated slices | unreachable | repositories |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for query, cov in discovery["queries"].items():
        lines.append(
            f"| `{query}` | {'yes' if cov['searched'] else 'no'} | "
            f"{cov['estimated_matches'] if cov['estimated_matches'] is not None else '—'} | "
            f"{cov['collected_matches']} | {_pct(cov['estimated_coverage'])} | "
            f"{cov['covered_ranges'] or '—'} | {cov['truncated_slices']} | "
            f"{cov['unreachable_matches']} | {cov['repositories']} |"
        )
    outside = discovery["outside_protocol"]
    lines += [
        "",
        f"Outside the protocol: {outside['slices']} slices (sizes {outside['size_range']}, "
        f"{outside['unreachable_matches']} unreachable matches), runs {outside['runs']}; "
        f"{outside['repositories_only_from_outside']} repositories found only there, "
        f"{outside['kept_only_from_outside']} of them kept. Manual additions: "
        f"{discovery['manual']['repositories']} ({discovery['manual']['kept']} kept).",
        "",
        "## Selection",
        "",
        f"{selection['repositories']} repositories, {selection['evaluated']} evaluated, "
        f"{selection['kept']} kept, {selection['pending']} pending, "
        f"{selection['not_found']} not found (reference date {selection['reference_date']}).",
        "",
        "| criterion | fail | share of evaluated | only failure (kept without it) |",
        "|---|---|---|---|",
    ]
    for criterion, effect in selection["criteria"].items():
        lines.append(
            f"| {criterion} | {effect['fail']} | {_pct(effect['fail_share'])} | "
            f"{effect['only_failure']} |"
        )
    stars = selection["stars"]
    lines += [
        "",
        f"Stars (p10…p99), kept: {stars['kept']}; rejected: {stars['rejected']}.",
        "",
        f"Rejected only by the license: {selection['only_license']['by_license']}. Most starred: "
        + ", ".join(
            f"{r['full_name']} ({r['stars']}, {r['license'] or 'none'})"
            for r in selection["only_license"]["top_by_stars"]
        ),
        "",
        "Kept repositories by creation year: "
        + ", ".join(f"{y}: {n}" for y, n in selection["kept_by_creation_year"].items()),
        "",
        "## Mining",
        "",
        f"{mining['mined']} mined, {mining['failed']} failed, {mining['selected_not_mined']} "
        f"selected but not mined; mined from {mining['mined_from']} to {mining['mined_to']} "
        f"({mining['mined_after_reference_date']} after the reference date).",
        "",
        f"Failures by type: {mining['failures_by_type']}. Omitted contents: "
        f"{mining['content_omitted']}.",
        "",
        *_history_markdown(audit["history"]),
        "## Artifacts at HEAD (mined repositories)",
        "",
        "| file type | at HEAD | symlinks | below min bytes | eligible | median size | searched "
        "| collected up to | eligible above it | repos with eligible | reached only via "
        "other types | small/symlink root | …with other eligible type |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for file_type, info in artifacts["by_file_type"].items():
        size = info["eligible_size"] or {}
        above = info["eligible_above_collected"]
        above_text = (
            "—" if above is None else f"{above} ({_pct(info['eligible_above_collected_share'])})"
        )
        lines.append(
            f"| {file_type} | {info['at_head']} | {info['symlinks']} | "
            f"{info['below_min_bytes']} | {info['eligible']} | {size.get('p50', '—')} | "
            f"{'yes' if info['searched'] else 'no'} | {info['collected_up_to'] or '—'} | "
            f"{above_text} | {info['repositories_with_eligible']} | "
            f"{info['repositories_reached_only_through_other_types']} | "
            f"{info['repositories_with_small_or_symlink_root']} | "
            f"{info['of_which_with_other_eligible_type']} |"
        )
    concentration, duplicates = artifacts["concentration"], artifacts["duplicates"]
    lines += [
        "",
        f"Concentration: {concentration['eligible_files']} eligible files in "
        f"{concentration['repositories']} repositories (per repository "
        f"{concentration['files_per_repository']}, "
        f"max {concentration['max_files_per_repository']}); "
        f"the top {concentration['top_1pct_repositories']} repositories (1%) hold "
        f"{_pct(concentration['top_1pct_share'])}.",
        "",
        f"Duplicates: {duplicates['duplicate_copies']} copies of an identical blob "
        f"({_pct(duplicates['duplicate_share'])}); {duplicates['blobs_in_several_repositories']} "
        f"blobs appear in several repositories ({duplicates['repository_occurrences']} "
        "occurrences).",
        "",
    ]
    return "\n".join(lines)


class AuditOutputError(RuntimeError):
    """The output directory holds something other than a previous audit."""


def check_output_dir(output_dir: Path) -> None:
    """Refuse an output that is not a previous audit: ``write_audit`` deletes and rewrites it,
    so ``--output data`` would otherwise wipe the whole data directory."""
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise AuditOutputError(f"{output_dir} exists and is not a directory")
    others = sorted(path.name for path in output_dir.iterdir() if path.name not in AUDIT_FILES)
    if others:
        raise AuditOutputError(
            f"{output_dir} holds files that are not an audit ({', '.join(others[:5])}); "
            "choose another --output"
        )


def write_audit(
    bronze_dir: Path,
    output_dir: Path,
    study: StudyConfig,
    *,
    code_version: str | None = None,
) -> dict[str, Any]:
    """Audit a bronze export and write ``audit.json``, ``audit.md`` and the paper's LaTeX
    (``audit.tex``, ``audit_rascunho.tex``; see ``audit_latex``) into ``output_dir``, through a
    ``.partial`` folder renamed at the end. An existing ``output_dir`` is replaced only when it
    holds nothing but a previous audit (``check_output_dir``)."""
    check_output_dir(output_dir)
    manifest = read_manifest(bronze_dir)
    audit: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version,
        "bronze_dir": str(bronze_dir),
        "exported_at": manifest.get("exported_at"),
        "study_config_hash": study.fingerprint(),
        "export_study_config_hash": manifest.get("study_config_hash"),
        "config_differs_from_export": manifest.get("study_config_hash") != study.fingerprint(),
        "study_config": study.snapshot(),
        **audit_tables(load_tables(bronze_dir), study),
    }
    audit["warnings"] = _warnings(audit)
    partial = output_dir.with_name(f"{output_dir.name}.partial")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    (partial / AUDIT_JSON).write_text(json.dumps(audit, indent=2, default=str), encoding="utf-8")
    (partial / AUDIT_MD).write_text(audit_markdown(audit), encoding="utf-8")
    # The paper's version: macros and tables (audit.tex) and a draft of the text.
    (partial / AUDIT_TEX).write_text(audit_tex(audit), encoding="utf-8")
    (partial / DRAFT_TEX).write_text(draft_tex(audit), encoding="utf-8")
    shutil.rmtree(output_dir, ignore_errors=True)
    partial.rename(output_dir)
    return {"output": str(output_dir), "warnings": audit["warnings"]}
