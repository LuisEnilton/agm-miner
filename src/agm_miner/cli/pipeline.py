"""Pipeline stages: discover, select, add-repo, mine, export, analyze and run."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agm_miner.cli.common import (
    PANEL_ANALYSIS,
    PANEL_COLLECT,
    StudyOption,
    _context,
    _database,
    _github_client,
    _handle_errors,
    _output,
    _print,
    _showing_progress,
    app,
)


@app.command(rich_help_panel=PANEL_COLLECT)
@_handle_errors
def discover(
    query: Annotated[
        list[str] | None, typer.Option("--query", "-q", help="Override the study's queries.")
    ] = None,
    restart: Annotated[
        bool, typer.Option(help="Forget saved progress and search every size slice again.")
    ] = False,
    study_path: StudyOption = None,
) -> None:
    """Stage 1: find repositories containing agent artifacts via GitHub code search."""
    from agm_miner.stages.discovery import discover_repositories

    settings, study = _context(study_path)
    with _showing_progress():
        result = discover_repositories(
            study, _database(settings), _github_client(settings), query, restart=restart
        )
    _print(result)


@app.command("select", rich_help_panel=PANEL_COLLECT)
@_handle_errors
def select_command(
    reevaluate: Annotated[
        bool, typer.Option(help="Re-check every repository against the current criteria.")
    ] = False,
    limit: Annotated[int | None, typer.Option(help="Process at most N repositories.")] = None,
    study_path: StudyOption = None,
) -> None:
    """Stage 2: fetch repository metadata and apply the selection criteria."""
    from agm_miner.stages.selection import select_repositories

    settings, study = _context(study_path)
    with _showing_progress():
        result = select_repositories(
            study,
            _database(settings),
            _github_client(settings),
            reevaluate=reevaluate,
            limit=limit,
        )
    _print(result)


@app.command("add-repo", rich_help_panel=PANEL_COLLECT)
@_handle_errors
def add_repo(
    names: Annotated[list[str], typer.Argument(help="Repositories as owner/name.")],
    study_path: StudyOption = None,
) -> None:
    """Register repositories by name (tagged as manual additions) and evaluate the criteria."""
    from agm_miner.stages.selection import add_repositories

    settings, study = _context(study_path)
    _print(add_repositories(study, _database(settings), _github_client(settings), names))


@app.command(rich_help_panel=PANEL_COLLECT)
@_handle_errors
def mine(
    repo: Annotated[
        list[str] | None,
        typer.Option("--repo", "-r", help="Mine only these repositories (any status)."),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Mine at most N repositories.")] = None,
    workers: Annotated[int, typer.Option(min=1, help="Parallel workers.")] = 1,
    retry_failed: Annotated[bool, typer.Option(help="Also retry FAILED repositories.")] = False,
    refresh: Annotated[
        bool, typer.Option(help="Also re-mine MINED repositories whose HEAD changed.")
    ] = False,
    force: Annotated[bool, typer.Option(help="Re-mine even if HEAD is unchanged.")] = False,
    keep_clones: Annotated[bool, typer.Option(help="Keep clones in CLONE_DIR.")] = False,
    study_path: StudyOption = None,
) -> None:
    """Stage 3: mine artifact histories from blobless git clones."""
    from agm_miner.stages.mining import MiningOptions, mine_repositories

    settings, study = _context(study_path)
    options = MiningOptions(
        repo_names=tuple(repo or ()),
        limit=limit,
        workers=workers,
        include_failed=retry_failed,
        refresh=refresh,
        force=force,
        keep_clones=keep_clones,
    )
    with _showing_progress():
        result = mine_repositories(settings, study, _database(settings), options)
    _print(result)


@app.command(rich_help_panel=PANEL_ANALYSIS)
@_handle_errors
def export(
    output: Annotated[
        Path | None, typer.Option(help="Parent directory (default: DATA_DIR/bronze).")
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Stage 4: export every table as a bronze Parquet dataset with a manifest."""
    from agm_miner.stages.export import export_bronze

    settings, study = _context(study_path)
    with _showing_progress():
        destination = export_bronze(study, _database(settings), output or settings.bronze_dir)
    typer.echo(str(destination))


@app.command(rich_help_panel=PANEL_ANALYSIS)
@_handle_errors
def analyze(
    bronze: Annotated[
        Path | None, typer.Option(help="Bronze export directory (default: the latest one).")
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Output directory (default: DATA_DIR/gold/<export>).")
    ] = None,
    study_path: StudyOption = None,
) -> None:
    """Stage 5: build silver/gold datasets for the research questions (no database needed)."""
    from agm_miner.analysis.datasets import build_datasets
    from agm_miner.progress import Progress, publishing
    from agm_miner.stages.export import latest_export

    settings, study = _context(study_path)
    bronze_dir = bronze or latest_export(settings.bronze_dir)
    output_dir = output or settings.gold_dir / bronze_dir.name
    with publishing(Progress("ANALYZE")) as progress, _showing_progress():
        counts = build_datasets(
            bronze_dir,
            output_dir,
            study.analysis,
            progress,
            history_cutoff=study.history_cutoff(),
        )
    _print({"output": str(output_dir), **counts})


@app.command(rich_help_panel=PANEL_COLLECT)
@_handle_errors
def run(
    workers: Annotated[int, typer.Option(min=1, help="Parallel workers of each mine run.")] = 4,
    discover: Annotated[
        bool, typer.Option(help="Run discovery (--no-discover only drains the backlog).")
    ] = True,
    query: Annotated[
        list[str] | None, typer.Option("--query", "-q", help="Override the study's queries.")
    ] = None,
    export: Annotated[bool, typer.Option(help="Export when collection finishes.")] = True,
    analyze: Annotated[
        bool, typer.Option(help="Analyze the new export (skipped with --no-export).")
    ] = True,
    select_interval: Annotated[
        float, typer.Option(min=0, help="Minimum seconds between two select runs.")
    ] = 60.0,
    mine_interval: Annotated[
        float, typer.Option(min=0, help="Minimum seconds between two mine runs.")
    ] = 60.0,
    status_interval: Annotated[
        float, typer.Option(min=1, help="Seconds between progress lines.")
    ] = 60.0,
    study_path: StudyOption = None,
) -> None:
    """Run discover, select and mine in parallel until done, then export and analyze."""
    from agm_miner.orchestrator import (
        PipelineOptions,
        PipelineSupervisor,
        child_runs,
        status_counts,
    )
    from agm_miner.progress_view import ConsoleWriter, PipelinePanel, live_panel

    settings, _ = _context(study_path)
    options = PipelineOptions(
        workers=workers,
        discover=discover,
        export=export,
        analyze=analyze,
        select_interval=select_interval,
        mine_interval=mine_interval,
        status_interval=status_interval,
        study_path=study_path,
        queries=tuple(query or ()),
    )
    factory = _database(settings)
    console = _output["console"]
    if console is None:  # no live panel: status lines carry the children's progress
        result = PipelineSupervisor(factory, options).run()
    else:
        supervisor = PipelineSupervisor(
            factory, options, output=ConsoleWriter(console), progress_lines=False
        )

        panel = PipelinePanel(
            supervisor.running_children,
            lambda children: child_runs(factory, children),
            lambda: status_counts(factory),
        )
        with live_panel(panel, console):
            result = supervisor.run()
    _print(
        {
            "exit_code": result.exit_code,
            "failed_stage": result.failed_stage,
            "interrupted": result.interrupted,
            "stages": [f"{stage}:{code}" for stage, code in result.stages],
            "repositories": result.final_counts,
        }
    )
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)
