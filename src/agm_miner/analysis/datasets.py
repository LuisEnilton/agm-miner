"""Build analysis datasets (silver + gold) from a bronze export.

Silver (one row per parsed artifact version / section):

* ``version_metrics``  — structural and readability metrics of every Markdown version;
* ``version_sections`` — sections with heading paths of every Markdown version.

Gold (one table per research question):

* ``rq1_sections``      — sections of the artifacts at HEAD (input for content coding);
* ``rq2_structure``     — metrics of the artifacts at HEAD;
* ``rq3_changes``       — every artifact change with timing, bursts, section/frontmatter diff,
  co-changed file categories, agent involvement and author role;
* ``rq3_author_roles``  — work profile of every contributor;
* ``repo_features``     — repository metrics + aggregated artifact attributes (correlations).
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.authors import author_role, category_shares, detect_agent, is_bot
from agm_miner.analysis.markdown import ParsedDocument, count_words
from agm_miner.analysis.metrics import compute_metrics
from agm_miner.analysis.structure_diff import diff_documents
from agm_miner.artifacts import ArtifactKind, ArtifactTool
from agm_miner.config import AnalysisConfig
from agm_miner.file_categories import FileCategory
from agm_miner.runs import code_version
from agm_miner.stages.export import read_manifest

logger = logging.getLogger(__name__)

BRONZE_TABLES = (
    "repositories",
    "discovery_hits",
    "artifacts",
    "artifact_versions",
    "artifact_changes",
    "commits",
    "commit_files",
    "commit_coauthors",
    "identities",
    "contributor_activity",
    "repository_activity",
)
_MARKDOWN_SUFFIXES = (".md", ".markdown", ".mdx")
ANALYSIS_MANIFEST = "analysis_manifest.json"
_MANUAL_QUERY_PREFIX = "manual:"


def load_bronze(bronze_dir: Path) -> dict[str, pl.DataFrame]:
    return {name: pl.read_parquet(bronze_dir / f"{name}.parquet") for name in BRONZE_TABLES}


def is_markdown_artifact(path: str, kind: str) -> bool:
    return kind != ArtifactKind.SKILL_RESOURCE.value or path.lower().endswith(_MARKDOWN_SUFFIXES)


# --------------------------------------------------------------------------- silver


def build_version_tables(
    tables: dict[str, pl.DataFrame], config: AnalysisConfig
) -> tuple[pl.DataFrame, pl.DataFrame, dict[tuple[int, str], ParsedDocument]]:
    """Parse every Markdown version once (cached by blob, since identical blobs are identical
    content). Returns metrics, sections and the parsed documents keyed by (repo_id, blob_sha)."""
    versions = tables["artifact_versions"].join(
        tables["artifacts"].select(pl.col("id").alias("artifact_id"), "path", "kind"),
        on="artifact_id",
    )
    parsed_by_blob: dict[str, tuple[dict[str, Any], ParsedDocument]] = {}
    documents: dict[tuple[int, str], ParsedDocument] = {}
    metric_rows: list[dict[str, Any]] = []
    section_rows: list[dict[str, Any]] = []

    for row in versions.iter_rows(named=True):
        if (
            row["content"] is None
            or row["is_symlink"]
            or not is_markdown_artifact(row["path"], row["kind"])
        ):
            continue
        cached = parsed_by_blob.get(row["blob_sha"])
        if cached is None:
            metrics, document = compute_metrics(row["content"], config.min_chars_for_language)
            cached = (metrics.as_row(), document)
            parsed_by_blob[row["blob_sha"]] = cached
        metrics_row, document = cached
        documents[(row["repo_id"], row["blob_sha"])] = document
        metric_rows.append(
            {"version_id": row["id"], "artifact_id": row["artifact_id"], "repo_id": row["repo_id"]}
            | metrics_row
        )
        for section in document.sections:
            section_rows.append(
                {
                    "version_id": row["id"],
                    "artifact_id": row["artifact_id"],
                    "repo_id": row["repo_id"],
                    "position": section.position,
                    "level": section.level,
                    "heading": section.heading,
                    "heading_path": list(section.path),
                    "h1": section.ancestor(1),
                    "h2": section.ancestor(2),
                    "h3": section.ancestor(3),
                    "text": section.text,
                    "words": count_words(section.text),
                }
            )

    metrics_frame = pl.DataFrame(metric_rows, schema=_METRICS_SCHEMA)
    sections_frame = pl.DataFrame(section_rows, schema=_SECTIONS_SCHEMA)
    return metrics_frame, sections_frame, documents


_METRICS_SCHEMA = {
    "version_id": pl.Int64,
    "artifact_id": pl.Int64,
    "repo_id": pl.Int64,
    "lines": pl.Int64,
    "chars": pl.Int64,
    "words": pl.Int64,
    "headings": pl.Int64,
    "h1": pl.Int64,
    "h2": pl.Int64,
    "h3": pl.Int64,
    "h4_plus": pl.Int64,
    "max_heading_depth": pl.Int64,
    "sections": pl.Int64,
    "paragraphs": pl.Int64,
    "code_blocks": pl.Int64,
    "code_languages": pl.List(pl.Utf8),
    "inline_code": pl.Int64,
    "bullet_lists": pl.Int64,
    "ordered_lists": pl.Int64,
    "list_items": pl.Int64,
    "task_items": pl.Int64,
    "tables": pl.Int64,
    "links": pl.Int64,
    "images": pl.Int64,
    "blockquotes": pl.Int64,
    "html_blocks": pl.Int64,
    "emphasis": pl.Int64,
    "has_frontmatter": pl.Boolean,
    "frontmatter_valid": pl.Boolean,
    "frontmatter_keys": pl.List(pl.Utf8),
    "frontmatter_json": pl.Utf8,
    "heading_tree_json": pl.Utf8,
    "language": pl.Utf8,
    "flesch_reading_ease": pl.Float64,
    "flesch_kincaid_grade": pl.Float64,
}

_SECTIONS_SCHEMA = {
    "version_id": pl.Int64,
    "artifact_id": pl.Int64,
    "repo_id": pl.Int64,
    "position": pl.Int64,
    "level": pl.Int64,
    "heading": pl.Utf8,
    "heading_path": pl.List(pl.Utf8),
    "h1": pl.Utf8,
    "h2": pl.Utf8,
    "h3": pl.Utf8,
    "text": pl.Utf8,
    "words": pl.Int64,
}


# --------------------------------------------------------------------------- gold


def head_versions(tables: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Artifacts present at the mined HEAD with the id of their HEAD version."""
    artifacts = tables["artifacts"].filter(pl.col("exists_at_head"))
    versions = tables["artifact_versions"].select(
        pl.col("id").alias("version_id"),
        "artifact_id",
        "blob_sha",
        "is_symlink",
        "size_bytes",
        "content_omitted",
    )
    repositories = tables["repositories"].select(pl.col("id").alias("repo_id"), "full_name")
    return (
        artifacts.rename({"id": "artifact_id"})
        .join(
            versions,
            left_on=["artifact_id", "head_blob_sha"],
            right_on=["artifact_id", "blob_sha"],
            how="left",
        )
        .join(repositories, on="repo_id", how="left")
    )


def build_rq1_sections(tables: dict[str, pl.DataFrame], sections: pl.DataFrame) -> pl.DataFrame:
    heads = head_versions(tables).select(
        "version_id", "full_name", "path", "tool", "kind", "scope", "skill_name"
    )
    return sections.join(heads, on="version_id", how="inner").sort(
        "repo_id", "artifact_id", "position"
    )


def build_rq2_structure(tables: dict[str, pl.DataFrame], metrics: pl.DataFrame) -> pl.DataFrame:
    heads = head_versions(tables)
    return heads.join(metrics.drop("artifact_id", "repo_id"), on="version_id", how="left").sort(
        "repo_id", "path"
    )


def build_author_roles(tables: dict[str, pl.DataFrame], config: AnalysisConfig) -> pl.DataFrame:
    activity = tables["contributor_activity"]
    changes_by_identity: dict[int, dict[str, int]] = defaultdict(dict)
    for row in activity.iter_rows(named=True):
        changes_by_identity[row["identity_id"]][row["file_category"]] = row["file_changes"]
    artifact_commits = (
        tables["commits"].group_by("author_identity_id").agg(pl.len().alias("artifact_commits"))
    )
    artifact_commit_count = dict(artifact_commits.iter_rows())

    rows = []
    for identity in tables["identities"].iter_rows(named=True):
        file_changes = changes_by_identity.get(identity["id"], {})
        shares = category_shares(file_changes)
        bot = is_bot(identity["name"], identity["email"], config)
        rows.append(
            {
                "identity_id": identity["id"],
                "repo_id": identity["repo_id"],
                "name": identity["name"],
                "github_login": identity["github_login"],
                "commits": identity["commits"],
                "artifact_commits": artifact_commit_count.get(identity["id"], 0),
                "is_bot": bot,
                "agent": detect_agent(identity["name"], identity["email"], config),
                "role": author_role(shares, bot, config),
                "file_changes": sum(file_changes.values()),
                **{
                    f"share_{category.value}": shares.get(category.value, 0.0)
                    for category in FileCategory
                },
            }
        )
    schema = {
        "identity_id": pl.Int64,
        "repo_id": pl.Int64,
        "name": pl.Utf8,
        "github_login": pl.Utf8,
        "commits": pl.Int64,
        "artifact_commits": pl.Int64,
        "is_bot": pl.Boolean,
        "agent": pl.Utf8,
        "role": pl.Utf8,
        "file_changes": pl.Int64,
        **{f"share_{category.value}": pl.Float64 for category in FileCategory},
    }
    return pl.DataFrame(rows, schema=schema)


def build_rq3_changes(
    tables: dict[str, pl.DataFrame],
    documents: dict[tuple[int, str], ParsedDocument],
    roles: pl.DataFrame,
    config: AnalysisConfig,
) -> pl.DataFrame:
    commits = tables["commits"].select(
        "repo_id",
        "sha",
        "authored_at",
        "committed_at",
        "author_identity_id",
        "author_name",
        "author_email",
        "committer_name",
        "committer_email",
        "message",
    )
    artifacts = tables["artifacts"].select(
        pl.col("id").alias("artifact_id"), "path", "tool", "kind", "scope", "skill_name"
    )
    changes = (
        tables["artifact_changes"]
        .join(commits, on=["repo_id", "sha"], how="inner")
        .join(artifacts, on="artifact_id", how="inner")
        .sort("artifact_id", "authored_at", "committed_at", "sha")
    )

    coauthor_agents: dict[tuple[int, str], set[str]] = defaultdict(set)
    for row in tables["commit_coauthors"].iter_rows(named=True):
        agent = detect_agent(row["name"], row["email"], config)
        if agent:
            coauthor_agents[(row["repo_id"], row["sha"])].add(agent)

    co_changed: dict[tuple[int, str], list[tuple[str, str]]] = defaultdict(list)
    for row in tables["commit_files"].iter_rows(named=True):
        co_changed[(row["repo_id"], row["sha"])].append((row["path"], row["file_category"]))

    role_by_identity = {
        row[0]: row[1:] for row in roles.select("identity_id", "role", "is_bot").iter_rows()
    }

    rows: list[dict[str, Any]] = []
    previous_time: dict[int, Any] = {}
    change_index: dict[int, int] = defaultdict(int)
    for row in changes.iter_rows(named=True):
        artifact_id = row["artifact_id"]
        change_index[artifact_id] += 1
        last = previous_time.get(artifact_id)
        hours = None if last is None else (row["authored_at"] - last).total_seconds() / 3600
        previous_time[artifact_id] = row["authored_at"]

        before = (
            documents.get((row["repo_id"], row["previous_blob_sha"]))
            if row["previous_blob_sha"]
            else None
        )
        after = documents.get((row["repo_id"], row["blob_sha"])) if row["blob_sha"] else None
        comparable = (row["previous_blob_sha"] is None or before is not None) and (
            row["blob_sha"] is None or after is not None
        )
        diff = diff_documents(before, after) if comparable else None

        others = [
            (path, category)
            for path, category in co_changed[(row["repo_id"], row["sha"])]
            if path != row["path"]
        ]
        category_counts = {category.value: 0 for category in FileCategory}
        for _, category in others:
            category_counts[category] += 1

        author_agent = detect_agent(row["author_name"], row["author_email"], config)
        committer_agent = detect_agent(row["committer_name"], row["committer_email"], config)
        agents = sorted(
            coauthor_agents[(row["repo_id"], row["sha"])]
            | {agent for agent in (author_agent, committer_agent) if agent}
        )
        role, bot = role_by_identity.get(row["author_identity_id"], (None, None))
        message = row["message"] or ""
        rows.append(
            {
                "repo_id": row["repo_id"],
                "artifact_id": artifact_id,
                "path": row["path"],
                "tool": row["tool"],
                "kind": row["kind"],
                "scope": row["scope"],
                "skill_name": row["skill_name"],
                "sha": row["sha"],
                "change_type": row["change_type"],
                "source_path": row["source_path"],
                "similarity": row["similarity"],
                "additions": row["additions"],
                "deletions": row["deletions"],
                "authored_at": row["authored_at"],
                "committed_at": row["committed_at"],
                "change_index": change_index[artifact_id],
                "hours_since_previous_change": hours,
                "in_burst": None if hours is None else hours <= config.burst_window_hours,
                "diff_available": diff is not None,
                "sections_added": diff.sections_added if diff else None,
                "sections_removed": diff.sections_removed if diff else None,
                "sections_modified": diff.sections_modified if diff else None,
                "frontmatter_added": diff.frontmatter_added if diff else None,
                "frontmatter_removed": diff.frontmatter_removed if diff else None,
                "frontmatter_modified": diff.frontmatter_modified if diff else None,
                "structural_change": diff.is_structural if diff else None,
                "co_changed_files": len(others),
                **{f"co_changed_{name}": count for name, count in category_counts.items()},
                "author_identity_id": row["author_identity_id"],
                "author_role": role,
                "author_is_bot": bot,
                "agents_involved": agents,
                "agent_involved": bool(agents),
                "message_subject": message.split("\n", 1)[0],
                "message": message,
            }
        )
    return pl.DataFrame(rows, schema=_RQ3_SCHEMA)


_RQ3_SCHEMA = {
    "repo_id": pl.Int64,
    "artifact_id": pl.Int64,
    "path": pl.Utf8,
    "tool": pl.Utf8,
    "kind": pl.Utf8,
    "scope": pl.Utf8,
    "skill_name": pl.Utf8,
    "sha": pl.Utf8,
    "change_type": pl.Utf8,
    "source_path": pl.Utf8,
    "similarity": pl.Int64,
    "additions": pl.Int64,
    "deletions": pl.Int64,
    "authored_at": pl.Datetime("us", "UTC"),
    "committed_at": pl.Datetime("us", "UTC"),
    "change_index": pl.Int64,
    "hours_since_previous_change": pl.Float64,
    "in_burst": pl.Boolean,
    "diff_available": pl.Boolean,
    "sections_added": pl.List(pl.Utf8),
    "sections_removed": pl.List(pl.Utf8),
    "sections_modified": pl.List(pl.Utf8),
    "frontmatter_added": pl.List(pl.Utf8),
    "frontmatter_removed": pl.List(pl.Utf8),
    "frontmatter_modified": pl.List(pl.Utf8),
    "structural_change": pl.Boolean,
    "co_changed_files": pl.Int64,
    **{f"co_changed_{category.value}": pl.Int64 for category in FileCategory},
    "author_identity_id": pl.Int64,
    "author_role": pl.Utf8,
    "author_is_bot": pl.Boolean,
    "agents_involved": pl.List(pl.Utf8),
    "agent_involved": pl.Boolean,
    "message_subject": pl.Utf8,
    "message": pl.Utf8,
}


def build_repo_features(
    tables: dict[str, pl.DataFrame],
    structure: pl.DataFrame,
    changes: pl.DataFrame,
    roles: pl.DataFrame,
) -> pl.DataFrame:
    instruction = pl.col("kind") == ArtifactKind.INSTRUCTION.value
    artifact_features = structure.group_by("repo_id").agg(
        (instruction & (pl.col("tool") == ArtifactTool.CLAUDE.value)).any().alias("has_claude_md"),
        (instruction & (pl.col("tool") == ArtifactTool.AGENTS.value)).any().alias("has_agents_md"),
        instruction.sum().alias("instruction_files"),
        (instruction & (pl.col("scope") == "NESTED")).sum().alias("nested_instruction_files"),
        pl.col("skill_name").filter(pl.col("skill_name").is_not_null()).n_unique().alias("skills"),
        (pl.col("kind") != ArtifactKind.INSTRUCTION.value).sum().alias("skill_files"),
        pl.col("words").filter(instruction).sum().alias("instruction_words"),
        pl.col("flesch_reading_ease").filter(instruction).mean().alias("instruction_reading_ease"),
    )
    evolution = changes.group_by("repo_id").agg(
        pl.len().alias("artifact_changes"),
        pl.col("sha").n_unique().alias("artifact_commits"),
        pl.col("authored_at").min().alias("first_artifact_change_at"),
        pl.col("authored_at").max().alias("last_artifact_change_at"),
        pl.col("in_burst").mean().alias("artifact_burst_share"),
        pl.col("agent_involved").mean().alias("artifact_agent_share"),
    )
    people = roles.group_by("repo_id").agg(
        pl.col("commits").sum().alias("total_commits"),
        (~pl.col("is_bot")).sum().alias("contributors"),
        pl.col("artifact_commits").gt(0).sum().alias("artifact_authors"),
    )
    active_months = (
        tables["repository_activity"].group_by("repo_id").agg(pl.len().alias("active_months"))
    )
    manual = (
        tables["discovery_hits"]
        .group_by("repo_id")
        .agg(pl.col("query").str.starts_with(_MANUAL_QUERY_PREFIX).all().alias("manual_only"))
    )
    repositories = (
        tables["repositories"].rename({"id": "repo_id"}).filter(pl.col("status") == "MINED")
    )
    features = (
        repositories.join(artifact_features, on="repo_id", how="left")
        .join(evolution, on="repo_id", how="left")
        .join(people, on="repo_id", how="left")
        .join(active_months, on="repo_id", how="left")
        .join(manual, on="repo_id", how="left")
    )
    return features.with_columns(
        (pl.col("total_commits") / pl.col("active_months")).alias("commits_per_active_month"),
        pl.col("manual_only").fill_null(False),
    ).sort("repo_id")


# --------------------------------------------------------------------------- entry point


def build_datasets(bronze_dir: Path, output_dir: Path, config: AnalysisConfig) -> dict[str, int]:
    """Write every dataset to ``output_dir``, replacing it only once all files are complete."""
    staging = output_dir.with_name(output_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        counts = _build_into(bronze_dir, staging, config)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(output_dir, ignore_errors=True)
    staging.rename(output_dir)
    return counts


def _build_into(bronze_dir: Path, output_dir: Path, config: AnalysisConfig) -> dict[str, int]:
    tables = load_bronze(bronze_dir)
    metrics, sections, documents = build_version_tables(tables, config)
    roles = build_author_roles(tables, config)
    structure = build_rq2_structure(tables, metrics)
    changes = build_rq3_changes(tables, documents, roles, config)
    outputs = {
        "silver_version_metrics": metrics,
        "silver_version_sections": sections,
        "rq1_sections": build_rq1_sections(tables, sections),
        "rq2_structure": structure,
        "rq3_changes": changes,
        "rq3_author_roles": roles,
        "repo_features": build_repo_features(tables, structure, changes, roles),
    }
    counts = {}
    for name, frame in outputs.items():
        frame.write_parquet(output_dir / f"{name}.parquet")
        counts[name] = frame.height
        logger.info("Wrote %s rows to %s", frame.height, name)
    # The analysis never touches the database, so its provenance lives next to its outputs.
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "bronze_dir": str(bronze_dir),
        "bronze_manifest": read_manifest(bronze_dir),
        "analysis_config": config.model_dump(mode="json"),
        "outputs": counts,
    }
    (output_dir / ANALYSIS_MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return counts
