"""Sampling audit (agm audit) on a small, hand-checkable bronze export."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from agm_miner.analysis.sampling_audit import (
    _warnings,
    artifact_coverage,
    audit_tables,
    discovery_coverage,
    history_summary,
    load_tables,
    merge_ranges,
    mining_summary,
    path_file_type,
    query_file_type,
    selection_effects,
    write_audit,
)
from agm_miner.cli import app
from agm_miner.config import AnalysisConfig, DiscoveryConfig, SelectionConfig, StudyConfig

CLAUDE_Q, AGENTS_Q = "filename:CLAUDE.md", "filename:AGENTS.md"
STUDY = StudyConfig(
    discovery=DiscoveryConfig(queries=[CLAUDE_Q, AGENTS_Q], size_min=100, size_max=1000),
    selection=SelectionConfig(created_before=date(2026, 8, 1), reference_date=date(2026, 9, 23)),
    analysis=AnalysisConfig(min_artifact_bytes=100, truncate_at_reference_date=True),
)
UTC_TIME = pl.Datetime("us", "UTC")


def ts(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


REPOSITORY_SCHEMA = {
    "id": pl.Int64,
    "full_name": pl.Utf8,
    "node_id": pl.Utf8,
    "status": pl.Utf8,
    "rejection_reason": pl.Utf8,
    "last_error": pl.Utf8,
    "metadata_fetched_at": UTC_TIME,
    "default_branch": pl.Utf8,
    "gh_created_at": UTC_TIME,
    "pushed_at": UTC_TIME,
    "stars": pl.Int64,
    "forks": pl.Int64,
    "watchers": pl.Int64,
    "disk_usage_kb": pl.Int64,
    "primary_language": pl.Utf8,
    "topics": pl.List(pl.Utf8),
    "license_spdx": pl.Utf8,
    "description": pl.Utf8,
    "is_fork": pl.Boolean,
    "is_archived": pl.Boolean,
    "is_template": pl.Boolean,
    "is_mirror": pl.Boolean,
    "commits_count": pl.Int64,
    "last_commit_at": UTC_TIME,
    "mined_at": UTC_TIME,
}


def repo(repo_id: int, status: str, **values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": repo_id,
        "full_name": f"org/r{repo_id}",
        "node_id": f"R_{repo_id}",
        "status": status,
        "rejection_reason": None,
        "last_error": None,
        "metadata_fetched_at": ts("2026-09-23"),
        "default_branch": "main",
        "gh_created_at": ts("2020-01-01"),
        "pushed_at": ts("2026-09-01"),
        "stars": 500,
        "forks": 1,
        "watchers": 1,
        "disk_usage_kb": 100,
        "primary_language": "Python",
        "topics": [],
        "license_spdx": "MIT",
        "description": "a library",
        "is_fork": False,
        "is_archived": False,
        "is_template": False,
        "is_mirror": False,
        "commits_count": 100,
        "last_commit_at": ts("2026-09-01"),
        "mined_at": None,
    }
    return base | values


NO_METADATA = {key: None for key in REPOSITORY_SCHEMA if key not in ("id", "full_name", "status")}
NO_METADATA |= {"node_id": "R_x", "topics": None}

REPOSITORIES = [
    repo(1, "MINED", mined_at=ts("2026-09-22")),
    repo(2, "MINED", mined_at=ts("2026-10-01")),
    repo(3, "REJECTED", rejection_reason="license", license_spdx=None),
    repo(4, "REJECTED", rejection_reason="min_stars", stars=3),
    repo(5, "DISCOVERED") | NO_METADATA,
    repo(6, "FAILED", last_error="GitTimeoutError: git clone ... timed out"),
    repo(7, "REJECTED") | NO_METADATA | {"rejection_reason": "not_found"},
]

SLICES = [
    (f"{CLAUDE_Q} size:100..1000", "SPLIT", 3000, 2),
    (f"{CLAUDE_Q} size:100..550", "SPLIT", 2500, 2),
    (f"{CLAUDE_Q} size:100..325", "COLLECTED", 900, 2),
    (f"{CLAUDE_Q} size:326..550", "COLLECTED", 1200, 3),  # over the cap: 200 unreachable
    (f"{CLAUDE_Q} size:0..5", "COLLECTED", 1500, 1),  # below size_min: outside the protocol
]

HITS = [
    (1, CLAUDE_Q, "CLAUDE.md", 1),
    (2, CLAUDE_Q, "CLAUDE.md", 2),
    (3, "manual:add-repo", "", 9),
    (4, CLAUDE_Q, "CLAUDE.md", 2),
    (5, CLAUDE_Q, "x/CLAUDE.md", 3),
    (6, CLAUDE_Q, "CLAUDE.md", 2),
    (6, CLAUDE_Q, "a/CLAUDE.md", 1),
    (7, CLAUDE_Q, "CLAUDE.md", 2),
]

# (id, repo, path, tool, kind, scope, at_head, blob, symlink, size, omitted)
ARTIFACTS = [
    (1, 1, "CLAUDE.md", "CLAUDE", "INSTRUCTION", "ROOT", True, "b1", False, 11, None),
    (2, 1, "AGENTS.md", "AGENTS", "INSTRUCTION", "ROOT", True, "b2", False, 5000, None),
    (3, 2, "CLAUDE.md", "CLAUDE", "INSTRUCTION", "ROOT", True, "b3", False, 300, None),
    (4, 2, "pkg/CLAUDE.md", "CLAUDE", "INSTRUCTION", "NESTED", True, "b4", False, 2000, None),
    (5, 2, "x/CLAUDE.md", "CLAUDE", "INSTRUCTION", "NESTED", True, "b5", True, 9, None),
    (
        6,
        2,
        ".claude/skills/s/SKILL.md",
        "CLAUDE",
        "SKILL_MANIFEST",
        "ROOT",
        True,
        "b6",
        False,
        300,
        None,
    ),
    (7, 1, "docs/CLAUDE.md", "CLAUDE", "INSTRUCTION", "NESTED", True, "b3", False, 300, None),
    (8, 6, "CLAUDE.md", "CLAUDE", "INSTRUCTION", "ROOT", True, "b8", False, 900, None),
    (9, 2, "old/CLAUDE.md", "CLAUDE", "INSTRUCTION", "NESTED", False, None, False, 0, None),
    (
        10,
        2,
        ".claude/skills/s/a.png",
        "CLAUDE",
        "SKILL_RESOURCE",
        "ROOT",
        True,
        "b10",
        False,
        700,
        "BINARY",
    ),
]


# Repository 2 added pkg/CLAUDE.md after the reference date; docs/CLAUDE.md of repository 1 has
# no recorded change (merge only).
COMMITS = [(1, "c1", "2026-09-01"), (2, "c1", "2026-09-01"), (2, "c2", "2026-10-01")]
CHANGES = [
    (1, 1, "c1", "b1"),
    (2, 1, "c1", "b2"),
    (3, 2, "c1", "b3"),
    (4, 2, "c2", "b4"),
    (5, 2, "c1", "b5"),
    (6, 2, "c1", "b6"),
    (10, 2, "c1", "b10"),
]


def bronze_tables() -> dict[str, pl.DataFrame]:
    artifacts = pl.DataFrame(
        [
            {
                "id": a,
                "repo_id": r,
                "path": p,
                "tool": t,
                "kind": k,
                "scope": s,
                "skill_name": None,
                "exists_at_head": h,
                "head_blob_sha": b,
            }
            for a, r, p, t, k, s, h, b, *_ in ARTIFACTS
        ],
        schema_overrides={"skill_name": pl.Utf8, "head_blob_sha": pl.Utf8},
    )
    versions = pl.DataFrame(
        [
            {
                "id": a,
                "artifact_id": a,
                "repo_id": r,
                "blob_sha": b,
                "is_symlink": sl,
                "size_bytes": size,
                "content_omitted": om,
                "content": "x",
            }
            for a, r, _, _, _, _, _, b, sl, size, om in ARTIFACTS
            if b is not None
        ],
        schema_overrides={"content_omitted": pl.Utf8},
    )
    return {
        "repositories": pl.DataFrame(REPOSITORIES, schema=REPOSITORY_SCHEMA),
        "discovery_slices": pl.DataFrame(
            [
                {
                    "slice_query": q,
                    "base_query": CLAUDE_Q,
                    "outcome": o,
                    "total_count": n,
                    "run_id": run,
                }
                for q, o, n, run in SLICES
            ]
        ),
        "discovery_hits": pl.DataFrame(
            [{"repo_id": r, "query": q, "path": p, "run_id": run} for r, q, p, run in HITS]
        ),
        "artifacts": artifacts,
        "artifact_versions": versions,
        "commits": pl.DataFrame(
            [
                {
                    "repo_id": r,
                    "sha": sha,
                    "author_identity_id": 1,
                    "authored_at": ts(at),
                    "committed_at": ts(at),
                }
                for r, sha, at in COMMITS
            ]
        ),
        "artifact_changes": pl.DataFrame(
            [
                {
                    "artifact_id": a,
                    "sha": sha,
                    "repo_id": r,
                    "change_type": "A",
                    "blob_sha": blob,
                    "previous_blob_sha": None,
                }
                for a, r, sha, blob in CHANGES
            ],
            schema_overrides={"previous_blob_sha": pl.Utf8},
        ),
    }


def test_helpers() -> None:
    assert query_file_type(CLAUDE_Q) == "CLAUDE.md"
    assert query_file_type("path:.claude/skills filename:SKILL.md") == "SKILL.md"
    assert query_file_type("filename:agents.md") == "AGENTS.md"
    assert query_file_type("language:python") is None
    assert path_file_type("a/.claude/skills/x/SKILL.md") == "SKILL.md"
    assert path_file_type(".claude/skills/x/ref.md") is None
    assert path_file_type("pkg/AGENTS.md") == "AGENTS.md"
    assert path_file_type("") is None and path_file_type("README.md") is None
    assert merge_ranges([(5, 9), (0, 3), (4, 4), (20, 30), (25, 26)]) == [(0, 9), (20, 30)]


def test_discovery_coverage_and_provenance() -> None:
    result = discovery_coverage(bronze_tables(), STUDY.discovery)
    claude, agents = result["queries"][CLAUDE_Q], result["queries"][AGENTS_Q]
    assert claude["estimated_matches"] == 3000 and claude["collected_matches"] == 2100
    assert claude["estimated_coverage"] == 0.7
    assert claude["covered_ranges"] == [[100, 550]] and claude["contiguous_up_to"] == 550
    assert not claude["complete"]
    assert (claude["truncated_slices"], claude["unreachable_matches"]) == (1, 200)
    assert claude["repositories"] == 6  # every hit of the query, inside the protocol or not
    assert not agents["searched"] and agents["estimated_matches"] is None

    outside = result["outside_protocol"]
    assert outside["slices"] == 1 and outside["size_range"] == [0, 5]
    assert (outside["truncated_slices"], outside["unreachable_matches"]) == (1, 500)
    assert outside["runs"] == [1]
    # Repository 1 was found only by run 1; repository 6 also by a run of the protocol.
    assert outside["repositories_only_from_outside"] == 1
    assert outside["kept_only_from_outside"] == 1
    assert outside["share_of_kept"] == pytest.approx(1 / 3, abs=1e-4)
    assert result["manual"] == {"repositories": 1, "kept": 0}
    assert result["queries_in_data_not_configured"] == []


def test_a_query_collected_in_full_is_complete() -> None:
    tables = bronze_tables()
    tables["discovery_slices"] = pl.DataFrame(
        [
            {
                "slice_query": f"{CLAUDE_Q} size:100..1000",
                "base_query": CLAUDE_Q,
                "outcome": "COLLECTED",
                "total_count": 800,
                "run_id": 2,
            }
        ]
    )
    claude = discovery_coverage(tables, STUDY.discovery)["queries"][CLAUDE_Q]
    assert claude["complete"] and claude["estimated_coverage"] == 1.0
    assert claude["contiguous_up_to"] == 1000


def test_selection_effects() -> None:
    result = selection_effects(bronze_tables()["repositories"], STUDY.selection)
    assert (result["evaluated"], result["kept"], result["pending"], result["not_found"]) == (
        5,
        3,
        1,
        1,
    )
    assert result["criteria"]["license"] == {"fail": 1, "fail_share": 0.2, "only_failure": 1}
    assert result["criteria"]["min_stars"]["only_failure"] == 1
    assert result["only_license"]["by_license"] == {"(none)": 1}
    assert result["only_license"]["top_by_stars"][0]["full_name"] == "org/r3"
    assert result["status_disagreements"] == {
        "kept_but_failing_now": 0,
        "rejected_but_passing_now": 0,
    }
    assert result["kept_by_creation_year"] == {"2020": 3}
    assert result["stars"]["rejected"]["p50"] in (3.0, 500.0)
    assert {"status": "REJECTED", "reason": "license", "repositories": 1} in result["funnel"]


def test_selection_flags_statuses_the_configuration_disagrees_with() -> None:
    repositories = bronze_tables()["repositories"].with_columns(
        pl.when(pl.col("id") == 2).then(10).otherwise(pl.col("stars")).alias("stars")
    )
    stricter = selection_effects(repositories, STUDY.selection)
    assert stricter["status_disagreements"]["kept_but_failing_now"] == 1
    relaxed = selection_effects(
        repositories, STUDY.selection.model_copy(update={"min_stars": 0, "allowed_licenses": None})
    )
    # Repositories 3 and 4 pass now, while 2 (10 stars) is kept again.
    assert relaxed["status_disagreements"] == {
        "kept_but_failing_now": 0,
        "rejected_but_passing_now": 2,
    }


def test_mining_summary() -> None:
    result = mining_summary(bronze_tables(), date(2026, 9, 23))
    assert (result["mined"], result["failed"], result["selected_not_mined"]) == (2, 1, 0)
    assert result["failures_by_type"] == {"GitTimeoutError": 1}
    assert result["failures"][0]["full_name"] == "org/r6"
    assert result["content_omitted"] == {"BINARY": 1}
    assert result["mined_after_reference_date"] == 1


def test_artifact_coverage() -> None:
    tables = bronze_tables()
    discovery = discovery_coverage(tables, STUDY.discovery)
    result = artifact_coverage(tables, STUDY.analysis, discovery, STUDY.discovery.size_max)
    claude = result["by_file_type"]["CLAUDE.md"]
    # Repository 6 is not mined; the deleted file and the skill resource are left out.
    assert (claude["at_head"], claude["symlinks"], claude["below_min_bytes"]) == (5, 1, 1)
    assert claude["eligible"] == 3 and claude["collected_up_to"] == 550
    assert claude["eligible_above_collected"] == 1
    assert claude["eligible_above_index_limit"] == 1
    assert claude["repositories_reached_only_through_other_types"] == 0
    # The 11-byte root CLAUDE.md of repository 1 sits next to an eligible AGENTS.md.
    assert claude["repositories_with_small_or_symlink_root"] == 1
    assert claude["of_which_with_other_eligible_type"] == 1

    agents, skill = result["by_file_type"]["AGENTS.md"], result["by_file_type"]["SKILL.md"]
    assert not agents["searched"] and agents["eligible_above_collected"] is None
    assert agents["repositories_reached_only_through_other_types"] == 1
    assert skill["eligible"] == 1 and skill["repositories_reached_only_through_other_types"] == 1

    assert result["concentration"] == {
        "eligible_files": 5,
        "repositories": 2,
        "files_per_repository": result["concentration"]["files_per_repository"],
        "max_files_per_repository": 3,
        "top_1pct_repositories": 1,
        "top_1pct_share": 0.6,
    }
    assert result["duplicates"] == {
        "duplicate_copies": 1,
        "duplicate_share": 0.2,
        "blobs_in_several_repositories": 1,
        "repository_occurrences": 2,
    }


def test_audit_tables_puts_the_sections_together() -> None:
    audit = audit_tables(bronze_tables(), STUDY)
    assert set(audit) == {"discovery", "selection", "mining", "history", "artifacts"}


def test_history_summary_measures_the_cut() -> None:
    history = history_summary(bronze_tables(), STUDY)
    assert history["enabled"] and history["cutoff"] == "2026-09-24T00:00:00+00:00"
    assert history["commits_after_cutoff"] == 1 and history["artifacts_not_yet_created"] == 1
    assert history["mined_before_cutoff"] == 1 and history["max_days_missing"] == 2.0
    assert history["artifacts_without_recorded_changes"] == 1
    # pkg/CLAUDE.md (2000 bytes) did not exist at the cutoff.
    assert history["eligible_at_head"]["files"]["CLAUDE.md"] == 3
    assert history["eligible_at_cutoff"]["files"]["CLAUDE.md"] == 2
    assert history["eligible_at_cutoff"]["repositories"] == 2


def test_history_summary_without_the_cut_warns_about_late_mining() -> None:
    study = STUDY.model_copy(
        update={"analysis": STUDY.analysis.model_copy(update={"truncate_at_reference_date": False})}
    )
    audit = audit_tables(bronze_tables(), study)
    # The numbers show what the cut would remove even when it is disabled.
    assert not audit["history"]["enabled"] and audit["history"]["commits_after_cutoff"] == 1
    audit |= {"config_differs_from_export": False}
    warnings = "\n".join(_warnings(audit))
    assert "the analysis does not cut the history" in warnings
    assert "miss up to" not in warnings
    no_date = study.model_copy(
        update={"selection": study.selection.model_copy(update={"reference_date": None})}
    )
    assert history_summary(bronze_tables(), no_date) == {"enabled": False, "reference_date": None}


def _write_bronze(directory: Path, tables: dict[str, pl.DataFrame], config_hash: str) -> None:
    directory.mkdir(parents=True)
    for name, frame in tables.items():
        frame.write_parquet(directory / f"{name}.parquet")
    (directory / "manifest.json").write_text(
        json.dumps({"exported_at": "2026-11-16T00:00:00+00:00", "study_config_hash": config_hash})
    )


def test_write_audit_reports_warnings(tmp_path: Path) -> None:
    bronze = tmp_path / "bronze" / "20261116T000000Z"
    _write_bronze(bronze, bronze_tables(), config_hash="older")
    # Content is never loaded.
    assert "content" not in load_tables(bronze)["artifact_versions"].columns

    result = write_audit(bronze, tmp_path / "out", STUDY, code_version="abc")
    warnings = "\n".join(result["warnings"])
    for expected in (
        "query 'filename:AGENTS.md' was never searched",
        "query 'filename:CLAUDE.md' is incomplete",
        "1 slices over the result cap, 200 matches unreachable",
        "1 kept repositories (33% of the kept) were found only by slices or runs outside",
        "1 discovered repositories were not evaluated yet",
        "criterion 'license' is the only failure of 1 repositories",
        "1 repositories failed to mine",
        "1 repositories were mined before the end of the reference date and miss up to 2.0 days",
        "1 artifacts have no recorded change",
        "CLAUDE.md: 1 eligible files (33%) are larger than the collected size range",
        "AGENTS.md: never searched",
        "SKILL.md: never searched",
        "the top 1% repositories hold 60%",
        "differs from the one stored in the export",
    ):
        assert expected in warnings, expected

    audit = json.loads((tmp_path / "out" / "audit.json").read_text())
    assert audit["code_version"] == "abc" and audit["config_differs_from_export"]
    assert audit["study_config"]["discovery"]["size_min"] == 100
    report = (tmp_path / "out" / "audit.md").read_text()
    assert "## Discovery" in report and "| `filename:CLAUDE.md` | yes | 3000 | 2100 | 70%" in report
    assert "| CLAUDE.md | 5 | 1 | 1 | 3 |" in report
    assert not (tmp_path / "out.partial").exists()
    assert "\\newcommand{\\auditKept}{3}" in (tmp_path / "out" / "audit.tex").read_text()
    assert "\\paragraph{Seleção.}" in (tmp_path / "out" / "audit_rascunho.tex").read_text()

    _write_bronze(tmp_path / "bronze2", bronze_tables(), config_hash=STUDY.fingerprint())
    (tmp_path / "bronze2" / "discovery_slices.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="discovery_slices"):
        write_audit(tmp_path / "bronze2", tmp_path / "out2", STUDY)


def test_audit_command(tmp_path: Path, monkeypatch) -> None:
    bronze = tmp_path / "bronze" / "20261116T000000Z"
    _write_bronze(bronze, bronze_tables(), config_hash="older")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    done = CliRunner().invoke(app, ["audit"])
    assert done.exit_code == 0, done.output
    output = json.loads(done.output[done.output.index("{") :])
    assert output["output"] == str(tmp_path / "audit" / "20261116T000000Z")
    assert (tmp_path / "audit" / "20261116T000000Z" / "audit.md").exists()

    missing = CliRunner().invoke(app, ["audit", "--bronze", str(tmp_path / "nope")])
    assert missing.exit_code == 1 and "error:" in missing.output
