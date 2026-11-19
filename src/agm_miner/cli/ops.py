"""Operations: what is running (status), past runs (runs) and the list of commands (help)."""

from __future__ import annotations

import re
from typing import Annotated, Any

import typer
from sqlalchemy import select

from agm_miner.cli.common import (
    PANEL_OPS,
    PANELS,
    _context,
    _database,
    _handle_errors,
    _print,
    app,
    runs_app,
)
from agm_miner.database.models import PipelineRun


@app.command(rich_help_panel=PANEL_OPS)
@_handle_errors
def status(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Keep refreshing until Ctrl+C.")
    ] = False,
    interval: Annotated[
        float, typer.Option(min=0.5, help="Seconds between refreshes with --watch.")
    ] = 2.0,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON instead.")] = False,
) -> None:
    """What is running now, how far it got and what each worker is doing.

    Reads the progress that running stages save in the database every few seconds, so it works
    from any terminal (or machine) pointing to the same database.
    """
    import time

    from rich.console import Console
    from rich.live import Live

    from agm_miner.orchestrator import status_counts
    from agm_miner.progress_view import render_status, run_view
    from agm_miner.runs import running_runs

    settings, _ = _context(None)
    factory = _database(settings)

    def current():
        views = [run_view(run) for run in running_runs(factory)]
        return views, status_counts(factory)

    if json_output:
        views, counts = current()
        _print(
            {
                "running": [
                    {
                        "id": view.run_id,
                        "stage": view.stage,
                        "stale": view.stale,
                        "heartbeat_at": view.heartbeat_at,
                        "progress": view.progress,
                        "stats": view.stats,
                    }
                    for view in views
                ],
                "repositories": counts,
            }
        )
        return
    console = Console()
    if not watch:
        console.print(render_status(*current()))
        return
    try:
        with Live(render_status(*current()), console=console, auto_refresh=False) as live:
            while True:
                time.sleep(interval)
                live.update(render_status(*current()), refresh=True)
    except KeyboardInterrupt:
        return  # leaving --watch is not an interruption of any work


def _summary(command: Any) -> str:
    """First sentence of a command's help text."""
    text = (command.help or command.short_help or "").strip()
    paragraph = text.split("\n\n", 1)[0].replace("\n", " ").strip()
    return re.split(r"(?<=[.])\s", paragraph, maxsplit=1)[0]


def _panel(command: Any, inherited: str | None) -> str | None:
    """Help panel of a command: its own, else the one of the group it is in."""
    return getattr(command, "rich_help_panel", None) or inherited


def _command_lines(
    group: Any, prefix: str, panel: str | None = None
) -> list[tuple[str, str, str | None]]:
    """(full name, summary, panel) of every runnable command below ``group``, depth first."""
    lines: list[tuple[str, str, str | None]] = []
    for name in group.list_commands(None):
        command = group.get_command(None, name)
        if command is None or command.hidden:
            continue
        full = f"{prefix} {name}".strip()
        if hasattr(command, "list_commands"):
            lines.extend(_command_lines(command, full, _panel(command, panel)))
        else:
            lines.append((full, _summary(command), _panel(command, panel)))
    return lines


def _by_panel(
    lines: list[tuple[str, str, str | None]],
) -> list[tuple[str | None, list[tuple[str, str]]]]:
    """``lines`` grouped by panel, in the order of ``PANELS`` (commands without one last)."""
    order = {panel: index for index, panel in enumerate(PANELS)}
    groups: dict[str | None, list[tuple[str, str]]] = {}
    for name, summary, panel in lines:
        groups.setdefault(panel, []).append((name, summary))
    return sorted(groups.items(), key=lambda item: order.get(item[0], len(order)))


@app.command("help", rich_help_panel=PANEL_OPS)
def help_command(
    ctx: typer.Context,
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Show only the commands under this name (e.g. `rq1 validate`)."),
    ] = None,
) -> None:
    """List every agm command and what it does (`agm help rq1` narrows it down)."""
    root = ctx.find_root().command
    group, prefix = root, ""
    for word in command or []:
        found = group.get_command(None, word) if hasattr(group, "list_commands") else None
        if found is None:
            raise typer.BadParameter(f"unknown command: {' '.join(command or [])}")
        group, prefix = found, f"{prefix} {word}".strip()
    if hasattr(group, "list_commands"):
        lines = _command_lines(group, prefix)
    else:
        typer.echo(f"agm {prefix}  {_summary(group)}")
        return
    width = max((len(name) for name, _, _ in lines), default=0)
    for index, (panel, commands) in enumerate(_by_panel(lines)):
        if index:
            typer.echo("")
        typer.echo(f"{panel or 'Other'}:")
        for name, summary in commands:
            typer.echo(f"  agm {name.ljust(width)}  {summary}")
    typer.echo("\nDetails of a command: agm <command> --help")


@runs_app.command("list", rich_help_panel=PANEL_OPS)
@_handle_errors
def runs_list(limit: Annotated[int, typer.Option(help="Show the N latest runs.")] = 20) -> None:
    """Latest pipeline runs with their statistics."""
    from agm_miner.runs import is_stale

    settings, _ = _context(None)
    with _database(settings)() as session:
        runs = session.scalars(
            select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
        ).all()
        _print(
            [
                {
                    "id": run.id,
                    "stage": run.stage.value,
                    "status": run.status.value,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "code_version": run.code_version,
                    "study_config_hash": run.study_config_hash[:12],
                    "stats": run.stats,
                    "error": run.error,
                    "heartbeat_at": run.heartbeat_at,
                    # RUNNING without a recent heartbeat: the process was most likely killed.
                    "stale": is_stale(run),
                }
                for run in runs
            ]
        )


@runs_app.command("cleanup", rich_help_panel=PANEL_OPS)
@_handle_errors
def runs_cleanup(
    minutes: Annotated[
        int,
        typer.Option(
            min=1, help="Silence (minutes without a heartbeat) after which a run counts as dead."
        ),
    ] = 60,
) -> None:
    """Record RUNNING runs with no sign of life (killed processes) as INTERRUPTED."""
    from datetime import timedelta

    from agm_miner.runs import mark_stale_runs

    settings, _ = _context(None)
    marked = mark_stale_runs(_database(settings), silent_for=timedelta(minutes=minutes))
    _print({"interrupted": marked})


@runs_app.command(rich_help_panel=PANEL_OPS)
@_handle_errors
def funnel() -> None:
    """Repository counts per status and rejection reason (selection funnel)."""
    from agm_miner.stages.selection import funnel as compute_funnel

    settings, _ = _context(None)
    with _database(settings)() as session:
        rows = compute_funnel(session)
    _print(
        [
            {"status": status, "reason": reason, "repositories": count}
            for status, reason, count in rows
        ]
    )
