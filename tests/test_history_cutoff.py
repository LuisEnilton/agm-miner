"""Cutting the mined history at the reference date (pure, on small bronze-like tables)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from agm_miner.analysis.history_cutoff import cut_history, cutoff_instant
from agm_miner.config import AnalysisConfig, SelectionConfig, StudyConfig

REFERENCE = date(2026, 9, 23)


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# sha -> (committed at, author identity)
COMMITS = {
    "s1": ("2026-09-01T10:00", 1),
    "s5": ("2026-09-10T10:00", 1),
    "s6": ("2026-09-15T10:00", 1),
    "s8": ("2026-09-23T23:59", 1),  # last minute of the reference date: kept
    "s2": ("2026-09-24T00:00", 2),  # first instant after it: dropped
    "s3": ("2026-10-02T10:00", 2),
    "s4": ("2026-10-05T10:00", 2),
    "s7": ("2026-10-10T10:00", 2),
}
# (artifact, sha, change type, blob, previous blob)
CHANGES = [
    (1, "s1", "A", "x1", None),
    (1, "s2", "M", "x2", "x1"),  # changed after: back to x1
    (2, "s3", "A", "x8", None),  # created after: did not exist yet
    (3, "s1", "A", "x3", None),
    (3, "s4", "D", None, "x3"),  # deleted after: still there at the cutoff
    (4, "s1", "A", "x4", None),
    (4, "s5", "D", None, "x4"),  # deleted before: untouched
    (6, "s1", "A", "x6", None),
    (6, "s6", "D", None, "x6"),
    (6, "s7", "A", "x7", None),  # re-created after: deleted at the cutoff
    (7, "s8", "A", "x9", None),
]
# (artifact, path, exists at the mined HEAD, HEAD blob)
ARTIFACTS = [
    (1, "CLAUDE.md", True, "x2"),
    (2, "AGENTS.md", True, "x8"),
    (3, "old/CLAUDE.md", False, None),
    (4, "pkg/CLAUDE.md", False, None),
    (5, "docs/CLAUDE.md", True, "x5"),  # no recorded change (merge only): untouched
    (6, "x/CLAUDE.md", True, "x7"),
    (7, "y/CLAUDE.md", True, "x9"),
]
VERSIONS = [(1, "x1"), (1, "x2"), (2, "x8"), (3, "x3"), (4, "x4"), (5, "x5"), (6, "x6"), (6, "x7")]
VERSIONS += [(7, "x9")]


def tables() -> dict[str, pl.DataFrame]:
    return {
        "commits": pl.DataFrame(
            [
                {
                    "repo_id": 1,
                    "sha": sha,
                    "author_identity_id": who,
                    "authored_at": ts(at),
                    "committed_at": ts(at),
                }
                for sha, (at, who) in COMMITS.items()
            ]
        ),
        "artifact_changes": pl.DataFrame(
            [
                {
                    "artifact_id": a,
                    "sha": sha,
                    "repo_id": 1,
                    "change_type": kind,
                    "blob_sha": blob,
                    "previous_blob_sha": previous,
                }
                for a, sha, kind, blob, previous in CHANGES
            ],
            schema_overrides={"blob_sha": pl.Utf8, "previous_blob_sha": pl.Utf8},
        ),
        "artifacts": pl.DataFrame(
            [
                {
                    "id": a,
                    "repo_id": 1,
                    "path": path,
                    "tool": "CLAUDE",
                    "kind": "INSTRUCTION",
                    "scope": "ROOT",
                    "skill_name": None,
                    "exists_at_head": head,
                    "head_blob_sha": blob,
                }
                for a, path, head, blob in ARTIFACTS
            ],
            schema_overrides={"skill_name": pl.Utf8, "head_blob_sha": pl.Utf8},
        ),
        "artifact_versions": pl.DataFrame(
            [
                {
                    "id": i,
                    "artifact_id": a,
                    "repo_id": 1,
                    "blob_sha": blob,
                    "is_symlink": False,
                    "size_bytes": 3000,
                }
                for i, (a, blob) in enumerate(VERSIONS, start=1)
            ]
        ),
        "commit_files": pl.DataFrame(
            [{"repo_id": 1, "sha": sha, "path": "CLAUDE.md"} for sha in ("s1", "s2")]
        ),
        "commit_coauthors": pl.DataFrame([{"repo_id": 1, "sha": "s2", "name": "Claude"}]),
        "identities": pl.DataFrame(
            [
                {"id": 1, "repo_id": 1, "first_commit_at": ts("2020-01-01T00:00")},
                {"id": 2, "repo_id": 1, "first_commit_at": ts("2026-09-30T00:00")},
                # Started before the cutoff without touching artifacts: kept.
                {"id": 3, "repo_id": 1, "first_commit_at": ts("2026-09-01T00:00")},
            ]
        ),
        "contributor_activity": pl.DataFrame(
            [{"identity_id": i, "repo_id": 1, "file_changes": 1} for i in (1, 2, 3)]
        ),
        "repository_activity": pl.DataFrame(
            [{"repo_id": 1, "month": date(2026, m, 1), "commits": 1} for m in (8, 9, 10)]
        ),
    }


def test_cutoff_instant_is_the_start_of_the_next_day() -> None:
    assert cutoff_instant(REFERENCE) == ts("2026-09-24T00:00")


def test_cut_history_rebuilds_the_state_at_the_cutoff() -> None:
    cut, stats = cut_history(tables(), REFERENCE)
    state = {
        row["id"]: (row["exists_at_head"], row["head_blob_sha"])
        for row in cut["artifacts"].iter_rows(named=True)
    }
    assert state == {
        1: (True, "x1"),
        3: (True, "x3"),
        4: (False, None),
        5: (True, "x5"),
        6: (False, None),
        7: (True, "x9"),
    }
    assert sorted(cut["commits"]["sha"].to_list()) == ["s1", "s5", "s6", "s8"]
    assert set(cut["artifact_changes"]["sha"].to_list()) == {"s1", "s5", "s6", "s8"}
    assert cut["artifact_changes"].columns == tables()["artifact_changes"].columns
    assert sorted(cut["artifact_versions"]["blob_sha"].to_list()) == [
        "x1",
        "x3",
        "x4",
        "x5",
        "x6",
        "x9",
    ]
    assert cut["commit_files"]["sha"].to_list() == ["s1"]
    assert cut["commit_coauthors"].is_empty()
    assert sorted(cut["identities"]["id"].to_list()) == [1, 3]
    assert sorted(cut["contributor_activity"]["identity_id"].to_list()) == [1, 3]
    assert cut["repository_activity"]["month"].max() == date(2026, 9, 1)
    assert stats == {
        "reference_date": "2026-09-23",
        "cutoff": "2026-09-24T00:00:00+00:00",
        "commits_after_cutoff": 4,
        "repositories_with_commits_after_cutoff": 1,
        "artifact_changes_after_cutoff": 4,
        "artifacts_changed_after_cutoff": 4,
        "artifacts_not_yet_created": 1,
        "artifacts_with_another_state": 3,
        "artifacts_deleted_at_cutoff": 1,
        "versions_dropped": 3,
        "identities_dropped": 1,
    }


def test_nothing_changes_when_the_history_ends_before_the_cutoff() -> None:
    original = tables()
    cut, stats = cut_history(original, date(2026, 12, 31))
    assert cut["artifacts"].sort("id").equals(original["artifacts"].sort("id"))
    assert cut["artifact_versions"].height == original["artifact_versions"].height
    assert stats["commits_after_cutoff"] == 0 and stats["artifacts_with_another_state"] == 0


def test_optional_tables_pass_through() -> None:
    minimal = {name: tables()[name] for name in ("commits", "artifact_changes", "artifacts")}
    cut, stats = cut_history(minimal, REFERENCE)
    assert set(cut) == set(minimal) and stats["versions_dropped"] == 0


def test_the_cut_needs_a_fixed_reference_date() -> None:
    with pytest.raises(ValueError, match="reference_date"):
        StudyConfig(analysis=AnalysisConfig(truncate_at_reference_date=True))
    study = StudyConfig(
        selection=SelectionConfig(reference_date=REFERENCE),
        analysis=AnalysisConfig(truncate_at_reference_date=True),
    )
    assert study.history_cutoff() == REFERENCE
    assert StudyConfig(selection=SelectionConfig(reference_date=REFERENCE)).history_cutoff() is None
