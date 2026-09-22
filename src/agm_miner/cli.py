"""Command-line interface: one command per pipeline stage.

agm discover            search GitHub for artifact files (stage 1)
agm select              fetch metadata, apply selection criteria (stage 2)
agm add-repo NAME...    register repositories by name (validation runs)
agm mine                mine selected repositories from git clones (stage 3)
agm export              dump the database as a bronze Parquet dataset (stage 4)
agm analyze             build silver/gold analysis datasets from a bronze export (stage 5)
agm run                 run stages 1-3 in parallel until done, then 4 and 5
agm runs list|funnel    inspect pipeline runs and the selection funnel
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar

import typer
from sqlalchemy import select

from agm_miner.config import Settings, StudyConfig, get_settings, load_study_config
from agm_miner.database.models import PipelineRun
from agm_miner.database.session import get_session_factory

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Never print local variables in tracebacks: they may include configuration values.
    pretty_exceptions_show_locals=False,
    help="agm-miner: mining pipeline for AI agent configuration artifacts (run stages in order).",
)
runs_app = typer.Typer(no_args_is_help=True, help="Inspect pipeline runs.")
app.add_typer(runs_app, name="runs")

P = ParamSpec("P")
R = TypeVar("R")

StudyOption = Annotated[
    Path | None, typer.Option("--study", help="Study configuration (default: STUDY_CONFIG_PATH).")
]


def _context(study_path: Path | None) -> tuple[Settings, StudyConfig]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    study = load_study_config(study_path or settings.study_config_path)
    return settings, study


def _handle_errors(command: Callable[P, R]) -> Callable[P, R]:
    """Report expected failures (bad token, unknown repository, ...) as one line, exit code 1;
    Ctrl+C / SIGTERM exit with code 130 after the stage has saved its progress."""

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from agm_miner.git.runner import GitError
        from agm_miner.github.client import GitHubAuthError, GitHubRequestError
        from agm_miner.runs import StageBusyError, interrupt_signals
        from agm_miner.stages.mining import RepositoryLookupError
        from agm_miner.stages.selection import SelectionError

        try:
            with interrupt_signals():
                return command(*args, **kwargs)
        except KeyboardInterrupt as exc:
            typer.secho(
                "interrupted: completed work is saved; run the same command again to continue",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=130) from exc
        except (
            StageBusyError,
            GitError,
            GitHubAuthError,
            GitHubRequestError,
            SelectionError,
            RepositoryLookupError,
            FileNotFoundError,
        ) as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    return wrapper


def _print(result: object) -> None:
    typer.echo(json.dumps(result, indent=2, default=str, sort_keys=True))


def _github_client(settings: Settings):
    from agm_miner.github.client import GitHubClient

    return GitHubClient(settings)


@app.command()
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
    _print(
        discover_repositories(
            study,
            get_session_factory(settings.database_url),
            _github_client(settings),
            query,
            restart=restart,
        )
    )


@app.command("select")
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
    _print(
        select_repositories(
            study,
            get_session_factory(settings.database_url),
            _github_client(settings),
            reevaluate=reevaluate,
            limit=limit,
        )
    )


@app.command("add-repo")
@_handle_errors
def add_repo(
    names: Annotated[list[str], typer.Argument(help="Repositories as owner/name.")],
    study_path: StudyOption = None,
) -> None:
    """Register repositories by name (tagged as manual additions) and evaluate the criteria."""
    from agm_miner.stages.selection import add_repositories

    settings, study = _context(study_path)
    _print(
        add_repositories(
            study, get_session_factory(settings.database_url), _github_client(settings), names
        )
    )


@app.command()
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
    _print(mine_repositories(settings, study, get_session_factory(settings.database_url), options))


@app.command()
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
    destination = export_bronze(
        study, get_session_factory(settings.database_url), output or settings.bronze_dir
    )
    typer.echo(str(destination))


@app.command()
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
    from agm_miner.stages.export import latest_export

    settings, study = _context(study_path)
    bronze_dir = bronze or latest_export(settings.bronze_dir)
    output_dir = output or settings.gold_dir / bronze_dir.name
    _print({"output": str(output_dir), **build_datasets(bronze_dir, output_dir, study.analysis)})


@app.command()
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
    from agm_miner.orchestrator import PipelineOptions, run_pipeline

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
    result = run_pipeline(get_session_factory(settings.database_url), options)
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


@runs_app.command("list")
def runs_list(limit: Annotated[int, typer.Option(help="Show the N latest runs.")] = 20) -> None:
    """Latest pipeline runs with their statistics."""
    settings, _ = _context(None)
    with get_session_factory(settings.database_url)() as session:
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
                }
                for run in runs
            ]
        )


@runs_app.command()
def funnel() -> None:
    """Repository counts per status and rejection reason (selection funnel)."""
    from agm_miner.stages.selection import funnel as compute_funnel

    settings, _ = _context(None)
    with get_session_factory(settings.database_url)() as session:
        rows = compute_funnel(session)
    _print(
        [
            {"status": status, "reason": reason, "repositories": count}
            for status, reason, count in rows
        ]
    )


if __name__ == "__main__":
    app()
