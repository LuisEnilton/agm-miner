"""Study sample rules applied to the mined data (minimum artifact size, human contributors)."""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl

from agm_miner.analysis.datasets import (
    build_artifact_sample,
    build_repository_sample,
    sample_artifact_ids,
)
from agm_miner.config import AnalysisConfig

CONFIG = AnalysisConfig(min_artifact_bytes=2048, min_human_contributors=2)
BIG, SMALL = 4096, 500


def tables(artifacts: list[dict], versions: list[dict], changes: list[dict]) -> dict:
    commits = [
        {"repo_id": change["repo_id"], "sha": change["sha"], "committed_at": change["at"]}
        for change in changes
    ]
    return {
        "artifacts": pl.DataFrame(
            artifacts,
            schema={
                "id": pl.Int64,
                "repo_id": pl.Int64,
                "path": pl.Utf8,
                "kind": pl.Utf8,
                "skill_name": pl.Utf8,
                "exists_at_head": pl.Boolean,
                "head_blob_sha": pl.Utf8,
            },
        ),
        "artifact_versions": pl.DataFrame(
            versions,
            schema={
                "id": pl.Int64,
                "artifact_id": pl.Int64,
                "blob_sha": pl.Utf8,
                "is_symlink": pl.Boolean,
                "size_bytes": pl.Int64,
            },
        ),
        "artifact_changes": pl.DataFrame(
            [{k: v for k, v in change.items() if k != "at"} for change in changes],
            schema={
                "artifact_id": pl.Int64,
                "repo_id": pl.Int64,
                "sha": pl.Utf8,
                "version_id": pl.Int64,
            },
        ),
        "commits": pl.DataFrame(
            commits,
            schema={
                "repo_id": pl.Int64,
                "sha": pl.Utf8,
                "committed_at": pl.Datetime("us", "UTC"),
            },
        ),
        "repositories": pl.DataFrame(
            [
                {"id": 1, "full_name": "org/a", "status": "MINED"},
                {"id": 2, "full_name": "org/b", "status": "MINED"},
                {"id": 3, "full_name": "org/c", "status": "REJECTED"},
            ]
        ),
    }


def artifact(artifact_id, path, kind="INSTRUCTION", skill=None, head=True, repo=1):
    return {
        "id": artifact_id,
        "repo_id": repo,
        "path": path,
        "kind": kind,
        "skill_name": skill,
        "exists_at_head": head,
        "head_blob_sha": f"b{artifact_id}" if head else None,
    }


def version(version_id, artifact_id, size, blob=None, symlink=False):
    blob = blob or f"b{artifact_id}"
    return {
        "id": version_id,
        "artifact_id": artifact_id,
        "blob_sha": blob,
        "is_symlink": symlink,
        "size_bytes": size,
    }


def at(day: int) -> datetime:
    return datetime(2025, 1, day, tzinfo=UTC)


def in_sample(frame: pl.DataFrame) -> dict[str, bool]:
    return dict(frame.select("path", "in_sample").iter_rows())


def test_artifact_size_rules() -> None:
    data = tables(
        [
            artifact(1, "AGENTS.md"),
            artifact(2, "CLAUDE.md"),  # symlink to AGENTS.md
            artifact(3, "docs/CLAUDE.md"),
            artifact(4, "old/CLAUDE.md", head=False),  # deleted: its last version counts
            artifact(5, ".claude/skills/db/SKILL.md", "SKILL_MANIFEST", "db"),
            artifact(6, ".claude/skills/db/notes.txt", "SKILL_RESOURCE", "db"),
            artifact(7, ".claude/skills/ui/SKILL.md", "SKILL_MANIFEST", "ui"),
            artifact(8, ".claude/skills/ui/big.md", "SKILL_RESOURCE", "ui"),
        ],
        [
            version(1, 1, BIG),
            version(2, 2, 9, symlink=True),
            version(3, 3, SMALL),
            version(4, 4, BIG, blob="old1"),
            version(5, 4, SMALL, blob="old2"),
            version(6, 5, BIG),
            version(7, 6, 10),
            version(8, 7, SMALL),
            version(9, 8, BIG),
        ],
        [
            {"artifact_id": 4, "repo_id": 1, "sha": "c1", "version_id": 4, "at": at(1)},
            {"artifact_id": 4, "repo_id": 1, "sha": "c2", "version_id": 5, "at": at(2)},
            {"artifact_id": 4, "repo_id": 1, "sha": "c3", "version_id": None, "at": at(3)},
        ],
    )
    assert in_sample(build_artifact_sample(data, CONFIG)) == {
        "AGENTS.md": True,
        "CLAUDE.md": False,
        "docs/CLAUDE.md": False,
        "old/CLAUDE.md": False,  # shrank below the limit before being deleted
        ".claude/skills/db/SKILL.md": True,
        ".claude/skills/db/notes.txt": True,  # resources follow their skill manifest
        ".claude/skills/ui/SKILL.md": False,
        ".claude/skills/ui/big.md": False,
    }


def roles(*rows: tuple) -> pl.DataFrame:
    """Rows of (repo_id, is_bot, commits) or (repo_id, is_bot, commits, agent)."""
    padded = [row if len(row) == 4 else (*row, None) for row in rows]
    return pl.DataFrame(
        padded,
        schema={"repo_id": pl.Int64, "is_bot": pl.Boolean, "commits": pl.Int64, "agent": pl.Utf8},
        orient="row",
    )


def test_repository_sample_reasons() -> None:
    data = tables(
        [artifact(1, "AGENTS.md", repo=1), artifact(2, "AGENTS.md", repo=2)],
        [version(1, 1, BIG), version(2, 2, SMALL)],
        [],
    )
    artifacts = build_artifact_sample(data, CONFIG)
    people = roles((1, False, 5), (1, True, 40), (1, False, 0), (2, False, 3), (2, False, 3))
    repositories = build_repository_sample(data, artifacts, people, CONFIG)

    # Repository 1: one human author with commits (a bot and a commit-less identity don't count).
    # Repository 2: its only artifact is too small. Repository 3 was never mined.
    rows = {row["repo_id"]: row for row in repositories.iter_rows(named=True)}
    assert set(rows) == {1, 2}
    assert rows[1]["exclusion_reason"] == "min_human_contributors"
    assert rows[1]["human_contributors"] == 1
    assert rows[2]["exclusion_reason"] == "min_artifact_bytes"
    assert not repositories["in_sample"].any()
    assert sample_artifact_ids(artifacts, repositories).height == 0

    people = roles((1, False, 5), (1, False, 2))
    repositories = build_repository_sample(data, artifacts, people, CONFIG)
    assert repositories.filter(pl.col("in_sample"))["repo_id"].to_list() == [1]
    assert sample_artifact_ids(artifacts, repositories)["artifact_id"].to_list() == [1]


def test_coding_agents_are_not_human_contributors() -> None:
    data = tables([artifact(1, "AGENTS.md")], [version(1, 1, BIG)], [])
    artifacts = build_artifact_sample(data, CONFIG)

    # A solo developer whose commits are partly authored by Claude is still a solo developer.
    solo = roles((1, False, 5), (1, False, 12, "claude"))
    row = build_repository_sample(data, artifacts, solo, CONFIG).row(0, named=True)
    assert row["human_contributors"] == 1
    assert row["exclusion_reason"] == "min_human_contributors"

    team = roles((1, False, 5), (1, False, 2), (1, False, 12, "claude"))
    row = build_repository_sample(data, artifacts, team, CONFIG).row(0, named=True)
    assert row["human_contributors"] == 2 and row["in_sample"]
