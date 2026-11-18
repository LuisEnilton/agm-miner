from __future__ import annotations

from typer.testing import CliRunner

from agm_miner.cli import app

runner = CliRunner()


def test_help_lists_every_command_with_its_summary() -> None:
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0, result.output
    names = (
        "discover",
        "mine",
        "analyze",
        "audit",
        "figures",
        "runs funnel",
        "rq1 classify",
        "rq1 validate score",
    )
    for name in names:
        assert f"agm {name} " in result.output
    assert "What is running now" in result.output


def test_help_narrows_to_a_group() -> None:
    result = runner.invoke(app, ["help", "rq1", "validate"])
    assert result.exit_code == 0, result.output
    assert "agm rq1 validate score" in result.output
    assert "agm discover" not in result.output


def test_help_single_command_and_unknown() -> None:
    assert "agm mine" in runner.invoke(app, ["help", "mine"]).output
    assert runner.invoke(app, ["help", "nope"]).exit_code != 0
