from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agm_miner.cli import app

pytestmark = pytest.mark.db

runner = CliRunner()


def test_help_lists_every_stage() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("discover", "select", "add-repo", "mine", "export", "analyze", "runs"):
        assert command in result.output


def test_runs_and_funnel_on_empty_database(session_factory) -> None:
    assert json.loads(runner.invoke(app, ["runs", "funnel"]).output) == []
    assert json.loads(runner.invoke(app, ["runs", "list"]).output) == []


def test_mine_unknown_repository_fails(session_factory) -> None:
    result = runner.invoke(app, ["mine", "--repo", "ghost/repo"])
    assert result.exit_code == 1
    assert "add-repo" in result.output
    assert "Traceback" not in result.output


def test_export_then_analyze(session_factory, tmp_path: Path) -> None:
    exported = runner.invoke(app, ["export", "--output", str(tmp_path / "bronze")])
    assert exported.exit_code == 0, exported.output
    bronze = Path(exported.output.strip().splitlines()[-1])
    assert (bronze / "manifest.json").exists()

    analyzed = runner.invoke(
        app, ["analyze", "--bronze", str(bronze), "--output", str(tmp_path / "gold")]
    )
    assert analyzed.exit_code == 0, analyzed.output
    result = json.loads(analyzed.output[analyzed.output.index("{") :])
    assert result["rq3_changes"] == 0
    assert (tmp_path / "gold" / "repo_features.parquet").exists()

    runs = json.loads(runner.invoke(app, ["runs", "list"]).output)
    assert [run["stage"] for run in runs] == ["EXPORT"]
    assert runs[0]["status"] == "SUCCEEDED"
