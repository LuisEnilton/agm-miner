"""RQ2 summary tables (pure, from small rq2_structure frames)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from agm_miner.analysis.rq2_summary import (
    ELEMENTS,
    SIZE_METRICS,
    describe,
    one_per_repository,
    prepare,
    size_bands,
    summarize,
    write_summary,
)
from agm_miner.cli import app


def row(**values: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "artifact_id": 1,
        "repo_id": 1,
        "path": "CLAUDE.md",
        "tool": "CLAUDE",
        "kind": "INSTRUCTION",
        "scope": "ROOT",
        "size_bytes": 3000,
        "is_symlink": False,
        "language": "en",
        "flesch_reading_ease": 40.0,
        "flesch_kincaid_grade": 12.0,
        "has_frontmatter": False,
        "frontmatter_valid": None,
        "frontmatter_keys": [],
    }
    base |= {metric: 10 for metric in SIZE_METRICS} | {element: 0 for element in ELEMENTS}
    base["max_heading_depth"] = 2
    return base | values


def frame(*rows: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(
        [r | {"artifact_id": i} for i, r in enumerate(rows, start=1)],
        schema_overrides={"frontmatter_keys": pl.List(pl.Utf8), "frontmatter_valid": pl.Boolean},
    )


STRUCTURE = frame(
    row(repo_id=1, code_blocks=2, tables=1, words=100),
    row(repo_id=1, path="pkg/CLAUDE.md", scope="NESTED", words=300),
    row(repo_id=1, path="a/b/CLAUDE.md", scope="NESTED", words=700),
    row(repo_id=2, path="AGENTS.md", tool="AGENTS", code_blocks=1, size_bytes=20000, words=2000),
    row(repo_id=2, path="small/AGENTS.md", tool="AGENTS", scope="NESTED", size_bytes=100),
    row(repo_id=3, path="x/AGENTS.md", tool="AGENTS", scope="NESTED", is_symlink=True),
    row(repo_id=4, path=".github/copilot-instructions.md", tool="COPILOT"),
    row(
        repo_id=3,
        path=".claude/skills/s/SKILL.md",
        kind="SKILL_MANIFEST",
        scope="NESTED",
        has_frontmatter=True,
        frontmatter_valid=True,
        frontmatter_keys=["description", "name"],
        language="pt",
    ),
    row(repo_id=3, path=".claude/skills/s/ref.md", kind="SKILL_RESOURCE", scope="NESTED"),
)


def test_prepare_keeps_the_three_file_types_above_the_size_limit() -> None:
    files = prepare(STRUCTURE, min_bytes=2048)
    # COPILOT, the skill resource, the small file and the symlink are left out.
    assert sorted(files["file_type"].to_list()) == [
        "AGENTS.md",
        "CLAUDE.md",
        "CLAUDE.md",
        "CLAUDE.md",
        "SKILL.md",
    ]


def test_one_per_repository_prefers_the_root_then_the_shallowest_file() -> None:
    files = prepare(STRUCTURE, min_bytes=0)
    chosen = one_per_repository(files)
    assert (
        dict(chosen.filter(pl.col("file_type") == "CLAUDE.md").select("repo_id", "path").rows())[1]
        == "CLAUDE.md"
    )
    nested_only = prepare(STRUCTURE, min_bytes=0).filter(pl.col("scope") == "NESTED")
    assert one_per_repository(nested_only.filter(pl.col("file_type") == "CLAUDE.md"))[
        "path"
    ].to_list() == ["pkg/CLAUDE.md"]


def test_describe_gives_shares_bins_and_readability() -> None:
    claude = prepare(STRUCTURE, min_bytes=2048).filter(pl.col("file_type") == "CLAUDE.md")
    result = describe(claude)
    assert result["files"] == 3 and result["repositories"] == 1
    assert result["with_element"]["code_blocks"] == pytest.approx(0.333)
    assert result["words_bins"] == {
        "<250": 0.333,
        "250-499": 0.333,
        "500-999": 0.333,
        ">=1000": 0.0,
    }
    assert result["size"]["words"]["median"] == 300
    assert result["max_heading_depth"]["4+"] == 0.0 and result["readability"]["english_files"] == 3


def test_summarize_cuts_and_frontmatter() -> None:
    summary = summarize(STRUCTURE, min_bytes=2048)
    assert set(summary["cuts"]) == {
        "all/files",
        "all/repositories",
        "root/files",
        "root/repositories",
    }
    assert summary["cuts"]["all/files"]["CLAUDE.md"]["files"] == 3
    assert summary["cuts"]["all/repositories"]["CLAUDE.md"]["files"] == 1
    assert summary["cuts"]["root/files"]["CLAUDE.md"]["files"] == 1
    assert "SKILL.md" not in summary["cuts"]["root/files"]  # the skill is nested
    assert summary["scope_shares"]["CLAUDE.md"] == pytest.approx(0.333)
    skill = summary["cuts"]["all/files"]["SKILL.md"]
    assert skill["frontmatter"] == {
        "with_frontmatter": 1.0,
        "valid_among_with_frontmatter": 1.0,
        "keys": {"description": 1.0, "name": 1.0},
    }
    assert skill["readability"]["english_files"] == 0
    assert skill["readability"]["flesch_reading_ease"]["median"] is None


def test_size_bands_separate_small_and_large_files() -> None:
    files = prepare(STRUCTURE, min_bytes=0).filter(pl.col("file_type") == "AGENTS.md")
    bands = size_bands(files, ["code_blocks"])
    assert bands["<4096"] == {"files": 1, "code_blocks": 0.0}
    assert bands[">=16384"] == {"files": 1, "code_blocks": 1.0}
    assert bands["4096-8191"] == {"files": 0, "code_blocks": None}


def test_write_summary_and_command(tmp_path: Path, monkeypatch) -> None:
    ours, theirs = tmp_path / "gold" / "20261116T000000Z", tmp_path / "external" / "paper"
    for directory in (ours, theirs):
        directory.mkdir(parents=True)
    STRUCTURE.write_parquet(ours / "rq2_structure.parquet")
    (ours / "analysis_manifest.json").write_text("{}")  # what `latest_analysis` looks for
    STRUCTURE.filter(pl.col("scope") == "ROOT").write_parquet(theirs / "rq2_structure.parquet")

    result = write_summary(ours, tmp_path / "out", min_bytes=2048, compare_dir=theirs)
    assert result["files"] == 5 and result["compared_files"] == 2
    summary = json.loads((tmp_path / "out" / "rq2_summary.json").read_text())
    assert summary["compared"]["cuts"]["all/files"]["AGENTS.md"]["files"] == 1
    report = (tmp_path / "out" / "rq2_summary.md").read_text()
    assert "## Cut: root/repositories" in report and "(compared, n=" in report
    assert "## SKILL.md frontmatter" in report and "| name | 100% |" in report
    assert not (tmp_path / "out.partial").exists()
    with pytest.raises(FileNotFoundError, match="rq2_structure"):
        write_summary(tmp_path, tmp_path / "x", min_bytes=0)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = CliRunner()
    done = runner.invoke(app, ["rq2", "summary", "--compare", str(theirs), "--min-bytes", "0"])
    assert done.exit_code == 0, done.output
    output = json.loads(done.output[done.output.index("{") :])["output"]
    assert output == str(tmp_path / "rq2" / "20261116T000000Z__vs__paper")
