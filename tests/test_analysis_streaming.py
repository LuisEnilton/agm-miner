"""The analysis never holds the version contents (or the rows built from them) in memory at
once: contents are read in batches, sections are written as they are produced, and the RQ3
step keeps only outlines of the documents and aggregates ``commit_files`` in polars."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from agm_miner.analysis import datasets
from agm_miner.analysis.datasets import (
    SECTIONS_SCHEMA,
    build_rq1_sections,
    build_version_tables,
    co_changed_counts,
    contents_of,
    load_bronze,
    read_version_contents,
)
from agm_miner.analysis.structure_diff import DocumentOutline
from agm_miner.config import AnalysisConfig

DOC = "# Title\n\n## Setup\nRun the tests before committing.\n\n## Style\nUse ruff.\n"
SKILL = "---\nname: x\n---\n# Skill\n\nDo things.\n"


def _tables() -> dict[str, pl.DataFrame]:
    artifacts = pl.DataFrame(
        {
            "id": [1, 2, 3, 4],
            "repo_id": [10, 10, 20, 20],
            "path": [
                "CLAUDE.md",
                ".claude/skills/x/SKILL.md",
                "AGENTS.md",
                ".claude/skills/x/a.py",
            ],
            "kind": ["INSTRUCTION", "SKILL_MANIFEST", "INSTRUCTION", "SKILL_RESOURCE"],
        }
    )
    versions = pl.DataFrame(
        {
            "id": [100, 101, 102, 103, 104, 105],
            "artifact_id": [1, 1, 2, 3, 3, 4],
            "repo_id": [10, 10, 10, 20, 20, 20],
            "blob_sha": ["b1", "b2", "b3", "b1", "b4", "b5"],
            "is_symlink": [False, False, False, False, True, False],
            "content": [DOC, None, SKILL, DOC, "CLAUDE.md", "print()"],
        }
    )
    return {"artifacts": artifacts, "artifact_versions": versions}


def test_versions_are_parsed_from_content_batches(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(datasets, "_CHUNK_ROWS", 2)
    tables = _tables()
    contents = contents_of(tables["artifact_versions"], batch_rows=2)
    tables["artifact_versions"] = tables["artifact_versions"].drop("content")
    path = tmp_path / "sections.parquet"

    metrics, section_rows, outlines = build_version_tables(tables, contents, AnalysisConfig(), path)

    # No content (101), a symlink (104) and a non-Markdown resource (105) are skipped.
    assert sorted(metrics["version_id"]) == [100, 102, 103]
    sections = pl.read_parquet(path)
    assert sections.height == section_rows
    assert sections.schema == pl.Schema(SECTIONS_SCHEMA)
    assert sorted(set(sections["version_id"])) == [100, 102, 103]
    assert set(outlines) == {(10, "b1"), (10, "b3"), (20, "b1")}
    # One outline per blob, shared by the repositories that hold it.
    assert outlines[(10, "b1")] is outlines[(20, "b1")]
    assert isinstance(outlines[(10, "b3")], DocumentOutline)
    assert outlines[(10, "b3")].frontmatter == {"name": "x"}


def test_sections_file_exists_without_sections(tmp_path: Path) -> None:
    tables = _tables()
    tables["artifact_versions"] = tables["artifact_versions"].drop("content")
    path = tmp_path / "sections.parquet"
    metrics, section_rows, outlines = build_version_tables(tables, [], AnalysisConfig(), path)
    assert metrics.height == section_rows == 0 and outlines == {}
    assert pl.read_parquet(path).schema == pl.Schema(SECTIONS_SCHEMA)


def test_bronze_versions_are_loaded_without_content(tmp_path: Path) -> None:
    versions = _tables()["artifact_versions"]
    for name in datasets.BRONZE_TABLES:
        frame = versions if name == "artifact_versions" else pl.DataFrame({"id": [1]})
        frame.write_parquet(tmp_path / f"{name}.parquet", row_group_size=2)

    tables = load_bronze(tmp_path)
    assert "content" not in tables["artifact_versions"].columns
    assert tables["artifact_versions"].height == versions.height
    # commit_files (millions of rows) is only ever scanned.
    assert "commit_files" not in tables


@pytest.mark.parametrize(("batch_rows", "slice_rows"), [(4, 4), (2, 5), (10, 3), (1, 100)])
def test_version_contents_are_read_a_slice_at_a_time(
    tmp_path: Path, batch_rows: int, slice_rows: int
) -> None:
    versions = _tables()["artifact_versions"]
    versions.write_parquet(tmp_path / "artifact_versions.parquet", row_group_size=2)
    batches = list(read_version_contents(tmp_path, batch_rows, slice_rows))
    assert all(batch.columns == ["id", "content"] for batch in batches)
    assert all(batch.height <= min(batch_rows, slice_rows) for batch in batches)
    # Every version once, in file order.
    assert pl.concat(batches).equals(versions.select("id", "content"))


def _naive_co_changed(files: pl.DataFrame, repo: int, sha: str, path: str) -> dict[str, int]:
    others = files.filter(
        (pl.col("repo_id") == repo) & (pl.col("sha") == sha) & (pl.col("path") != path)
    )
    counts = {column: 0 for column in datasets._CO_CHANGED}
    counts["co_changed_files"] = others.height
    for category in others["file_category"]:
        counts[f"co_changed_{category}"] += 1
    return counts


def test_co_changed_counts_exclude_the_artifact_itself() -> None:
    files = pl.DataFrame(
        {
            "repo_id": [1, 1, 1, 1, 2],
            "sha": ["a", "a", "a", "b", "a"],
            "path": ["CLAUDE.md", "src/x.py", "tests/t.py", "CLAUDE.md", "CLAUDE.md"],
            "file_category": ["agent_artifact", "code", "test", "agent_artifact", "agent_artifact"],
        }
    )
    keys = pl.DataFrame(
        {
            "repo_id": [1, 1, 1, 2, 3],
            "sha": ["a", "a", "b", "a", "z"],
            # A deleted path is not in commit_files; an unknown commit has no files.
            "path": ["CLAUDE.md", "AGENTS.md", "CLAUDE.md", "CLAUDE.md", "CLAUDE.md"],
        }
    )
    result = co_changed_counts(files, keys)
    assert result.height == keys.height
    scanned = co_changed_counts(files.lazy(), keys)
    assert scanned.sort(scanned.columns).equals(result.sort(result.columns))
    for row in result.iter_rows(named=True):
        expected = _naive_co_changed(files, row["repo_id"], row["sha"], row["path"])
        assert {column: row[column] for column in expected} == expected
    first = result.filter((pl.col("sha") == "a") & (pl.col("repo_id") == 1))
    assert sorted(first["co_changed_files"]) == [2, 3]


@pytest.mark.parametrize("chunk", [1, 3, 1000])
def test_frame_chunks_keep_every_row_in_order(chunk: int, monkeypatch) -> None:
    monkeypatch.setattr(datasets, "_CHUNK_ROWS", chunk)
    sunk: list[pl.DataFrame] = []
    collected = datasets._FrameChunks({"n": pl.Int64})
    streamed = datasets._FrameChunks({"n": pl.Int64}, sunk.append)
    for n in range(7):
        collected.add({"n": n})
        streamed.add({"n": n})
    streamed.flush()
    assert collected.frame()["n"].to_list() == list(range(7))
    assert pl.concat(sunk)["n"].to_list() == list(range(7)) and streamed.height == 7
    assert datasets._FrameChunks({"n": pl.Int64}).frame().schema == pl.Schema({"n": pl.Int64})


def _head_tables() -> dict[str, pl.DataFrame]:
    """``_tables()`` with what ``head_versions`` needs, and versions without their content."""
    tables = _tables()
    tables["artifacts"] = tables["artifacts"].with_columns(
        exists_at_head=pl.Series([True, True, True, False]),
        head_blob_sha=pl.Series(["b1", "b3", "b4", None]),
        tool=pl.lit("CLAUDE"),
        scope=pl.lit("PROJECT"),
        skill_name=pl.lit(None, pl.Utf8),
    )
    tables["repositories"] = pl.DataFrame({"id": [10, 20], "full_name": ["o/a", "o/b"]})
    tables["artifact_versions"] = (
        tables["artifact_versions"]
        .drop("content")
        .with_columns(size_bytes=pl.lit(1), content_omitted=pl.lit(False))
    )
    return tables


@pytest.mark.parametrize("slice_rows", [1, 2, 1000])
def test_rq1_sections_read_from_the_sections_file_a_slice_at_a_time(
    tmp_path: Path, slice_rows: int, monkeypatch
) -> None:
    tables = _head_tables()
    contents = contents_of(_tables()["artifact_versions"])
    path = tmp_path / "sections.parquet"
    build_version_tables(tables, contents, AnalysisConfig(), path)
    in_memory = build_rq1_sections(tables, pl.read_parquet(path))

    monkeypatch.setattr(datasets, "_SECTIONS_SLICE_ROWS", slice_rows)
    scanned = build_rq1_sections(tables, pl.scan_parquet(path))

    assert scanned.equals(in_memory)
    heads = datasets.head_versions(tables).select(
        "version_id", "full_name", "path", "tool", "kind", "scope", "skill_name"
    )
    reference = (
        pl.read_parquet(path)
        .join(heads, on="version_id", how="inner")
        .sort("repo_id", "artifact_id", "position")
    )
    assert scanned.equals(reference)
    # HEAD versions only: 100 (b1 of CLAUDE.md) and 102 (the skill); 103 is not at HEAD and
    # 104 (b4, the HEAD of AGENTS.md) is a symlink without sections.
    assert sorted(set(scanned["version_id"])) == [100, 102]


def test_scan_slices_of_an_empty_scan_keep_the_schema() -> None:
    empty = pl.DataFrame(schema={"version_id": pl.Int64}).lazy()
    frames = list(datasets._scan_slices(empty, 10))
    assert len(frames) == 1 and frames[0].schema == pl.Schema({"version_id": pl.Int64})


# Words outside the CMU dictionary, so textstat falls back to pyphen to count syllables.
ENGLISH = "# Guide\n\n" + "Always run kubectlify and frobnicatorize before pushing changes. " * 20


def test_syllable_cache_is_cleared_without_changing_results() -> None:
    import pyphen

    from agm_miner.analysis.metrics import clear_syllable_cache, compute_metrics

    first, _ = compute_metrics(ENGLISH)
    assert first.flesch_reading_ease is not None
    assert any(dictionary.cache for dictionary in pyphen.hdcache.values())
    clear_syllable_cache()
    assert not any(dictionary.cache for dictionary in pyphen.hdcache.values())
    again, _ = compute_metrics(ENGLISH)
    assert again == first


def test_version_parsing_clears_the_syllable_cache_after_every_batch(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(datasets, "clear_syllable_cache", lambda: calls.append(1))
    tables = _tables()
    contents = contents_of(tables["artifact_versions"], batch_rows=2)
    tables["artifact_versions"] = tables["artifact_versions"].drop("content")
    build_version_tables(tables, contents, AnalysisConfig(), tmp_path / "s.parquet")
    assert len(calls) == len(contents) == 3


@pytest.mark.parametrize("chunk", [1, 2, 1000])
def test_rq1_sections_are_written_a_chunk_at_a_time(
    tmp_path: Path, chunk: int, monkeypatch
) -> None:
    tables = _head_tables()
    contents = contents_of(_tables()["artifact_versions"])
    path = tmp_path / "sections.parquet"
    build_version_tables(tables, contents, AnalysisConfig(), path)
    monkeypatch.setattr(datasets, "_CHUNK_ROWS", chunk)

    whole = build_rq1_sections(tables, pl.scan_parquet(path))
    output = tmp_path / "rq1.parquet"
    assert datasets.write_rq1_sections(tables, pl.scan_parquet(path), output) == whole.height
    assert pl.read_parquet(output).equals(whole)

    # Only the artifacts to keep (the study sample), like the semi join it replaces.
    keep = pl.DataFrame({"artifact_id": [2]})
    kept = build_rq1_sections(tables, pl.scan_parquet(path), keep)
    assert kept.equals(whole.join(keep, on="artifact_id", how="semi"))
    nothing = tmp_path / "none.parquet"
    none = pl.DataFrame({"artifact_id": [99]})
    assert datasets.write_rq1_sections(tables, pl.scan_parquet(path), nothing, none) == 0
    assert pl.read_parquet(nothing).schema == whole.schema
