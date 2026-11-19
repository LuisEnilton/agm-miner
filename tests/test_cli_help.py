from __future__ import annotations

import subprocess
import sys

import typer
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


def _sections(output: str) -> dict[str, list[str]]:
    """Commands listed under each panel header of `agm help`."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.endswith(":") and not line.startswith(" "):
            current = line[:-1]
            sections[current] = []
        elif line.startswith("  agm ") and current is not None:
            sections[current].append(line.split("  ")[1].removeprefix("agm ").strip())
    return sections


def test_every_command_has_a_panel_in_the_documented_order() -> None:
    from agm_miner.cli import PANELS
    from agm_miner.cli.ops import _command_lines

    root = typer.main.get_command(app)
    assert all(panel in PANELS for _, _, panel in _command_lines(root, ""))
    sections = _sections(runner.invoke(app, ["help"]).output)
    assert list(sections) == list(PANELS)


def test_final_results_and_pilot_commands_are_in_separate_panels() -> None:
    from agm_miner.cli import PANELS

    collect, analysis, results, ops, experimental = PANELS
    sections = _sections(runner.invoke(app, ["help"]).output)
    assert {"discover", "select", "mine", "run"} <= set(sections[collect])
    assert sections[analysis] == ["export", "analyze"]
    assert {"rq1 classify", "rq2 summary", "audit", "figures"} <= set(sections[results])
    assert {"status", "runs funnel", "help"} <= set(sections[ops])
    pilot = {"rq1 propose", "rq1 consolidate", "rq1 smoke", "rq1 compare", "rq1 validate llm"}
    assert pilot <= set(sections[experimental])
    assert not pilot & set(sections[results])


def test_narrowed_help_keeps_the_panels() -> None:
    sections = _sections(runner.invoke(app, ["help", "rq1"]).output)
    assert "rq1 classify" in sections["Study results (no database)"]
    assert "rq1 propose" in sections["Experimental / pilot (not used for the final results)"]


def test_the_package_runs_as_a_module() -> None:
    # `agm run` starts every stage as `python -m agm_miner.cli <stage>` (orchestrator).
    result = subprocess.run(
        [sys.executable, "-m", "agm_miner.cli", "help", "mine"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "agm mine" in result.stdout
