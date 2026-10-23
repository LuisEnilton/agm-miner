"""Command-line interface: one command per pipeline stage.

agm discover            search GitHub for artifact files (stage 1)
agm select              fetch metadata, apply selection criteria (stage 2)
agm add-repo NAME...    register repositories by name (validation runs)
agm mine                mine selected repositories from git clones (stage 3)
agm export              dump the database as a bronze Parquet dataset (stage 4)
agm analyze             build silver/gold analysis datasets from a bronze export (stage 5)
agm run                 run stages 1-3 in parallel until done, then 4 and 5
agm status              what is running now and how far it got (also from another terminal)
agm runs list|funnel    inspect pipeline runs and the selection funnel
agm rq1 ...             RQ1 content coding with an LLM (sample, propose, classify, validate)
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

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
rq1_app = typer.Typer(
    no_args_is_help=True,
    help="RQ1 content coding: create labels with an LLM on a sample, then classify every section.",
)
app.add_typer(rq1_app, name="rq1")

P = ParamSpec("P")
R = TypeVar("R")

StudyOption = Annotated[
    Path | None, typer.Option("--study", help="Study configuration (default: STUDY_CONFIG_PATH).")
]


# How the running command shows its progress (set up by _context from AGM_PROGRESS).
_output: dict[str, Any] = {"mode": "off", "console": None}


def _context(study_path: Path | None) -> tuple[Settings, StudyConfig]:
    from agm_miner.progress_view import configure_logging, resolve_mode

    settings = get_settings()
    mode = resolve_mode(settings.progress_mode)
    _output["mode"] = mode
    _output["console"] = configure_logging(settings.log_level.upper(), mode)
    study = load_study_config(study_path or settings.study_config_path)
    return settings, study


@contextmanager
def _showing_progress() -> Iterator[None]:
    """Live panel (interactive terminal) or periodic summary lines while a stage runs."""
    from agm_miner.progress_view import showing_progress

    with showing_progress(_output["mode"], _output["console"]):
        yield


def _handle_errors(command: Callable[P, R]) -> Callable[P, R]:
    """Report expected failures (bad token, unknown repository, ...) as one line, exit code 1;
    Ctrl+C / SIGTERM exit with code 130 after the stage has saved its progress."""

    @functools.wraps(command)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        from agm_miner.database.schema import SchemaMismatchError
        from agm_miner.git.runner import GitError
        from agm_miner.github.client import GitHubAuthError, GitHubRequestError
        from agm_miner.rq1.llm import LLMAuthError, LLMRequestError
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
            SchemaMismatchError,
            StageBusyError,
            GitError,
            GitHubAuthError,
            GitHubRequestError,
            LLMAuthError,
            LLMRequestError,
            SelectionError,
            RepositoryLookupError,
            FileNotFoundError,
        ) as exc:
            typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc

    return wrapper


def _database(settings: Settings) -> sessionmaker[Session]:
    """Session factory for DATABASE_URL, refusing a database at another schema revision."""
    from agm_miner.database.schema import ensure_current_schema

    factory = get_session_factory(settings.database_url)
    ensure_current_schema(factory.kw["bind"])
    return factory


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
    with _showing_progress():
        result = discover_repositories(
            study, _database(settings), _github_client(settings), query, restart=restart
        )
    _print(result)


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
    with _showing_progress():
        result = select_repositories(
            study,
            _database(settings),
            _github_client(settings),
            reevaluate=reevaluate,
            limit=limit,
        )
    _print(result)


@app.command("add-repo")
@_handle_errors
def add_repo(
    names: Annotated[list[str], typer.Argument(help="Repositories as owner/name.")],
    study_path: StudyOption = None,
) -> None:
    """Register repositories by name (tagged as manual additions) and evaluate the criteria."""
    from agm_miner.stages.selection import add_repositories

    settings, study = _context(study_path)
    _print(add_repositories(study, _database(settings), _github_client(settings), names))


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
    with _showing_progress():
        result = mine_repositories(settings, study, _database(settings), options)
    _print(result)


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
    with _showing_progress():
        destination = export_bronze(study, _database(settings), output or settings.bronze_dir)
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
    from agm_miner.progress import Progress, publishing
    from agm_miner.stages.export import latest_export

    settings, study = _context(study_path)
    bronze_dir = bronze or latest_export(settings.bronze_dir)
    output_dir = output or settings.gold_dir / bronze_dir.name
    with publishing(Progress("ANALYZE")) as progress, _showing_progress():
        counts = build_datasets(bronze_dir, output_dir, study.analysis, progress)
    _print({"output": str(output_dir), **counts})


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


@app.command()
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


@runs_app.command("list")
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


@runs_app.command()
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


# --------------------------------------------------------------------------- rq1

GoldOption = Annotated[
    Path | None,
    typer.Option("--gold", help="Analysis output to code (default: the latest in DATA_DIR/gold)."),
]


def _rq1_dirs(settings: Settings, gold: Path | None) -> tuple[Path, Path]:
    """The analysis output to read and the RQ1 directory that belongs to it."""
    from agm_miner.analysis.datasets import latest_analysis

    gold_dir = gold or latest_analysis(settings.gold_dir)
    return gold_dir, settings.rq1_dir / gold_dir.name


@rq1_app.command("sample")
@_handle_errors
def rq1_sample(
    gold: GoldOption = None,
    size: Annotated[int | None, typer.Option(min=1, help="Override rq1.sample_size.")] = None,
    study_path: StudyOption = None,
) -> None:
    """Stratified sample of sections for label creation (rq1.* in study.yaml)."""
    from agm_miner.rq1.stages import sample_sections

    settings, study = _context(study_path)
    config = study.rq1 if size is None else study.rq1.model_copy(update={"sample_size": size})
    gold_dir, output_dir = _rq1_dirs(settings, gold)
    _print(sample_sections(gold_dir, output_dir, config))


@rq1_app.command("models")
@_handle_errors
def rq1_models() -> None:
    """Free OpenRouter models that accept structured outputs (the list changes often)."""
    from agm_miner.rq1.backends import list_free_structured_models

    _print(list_free_structured_models())


SMOKE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}},
    "required": ["ok", "echo"],
    "additionalProperties": False,
}


@rq1_app.command("smoke")
@_handle_errors
def rq1_smoke(
    phase: Annotated[
        str, typer.Option(help="Which phase's model to try: propose, consolidate or classify.")
    ] = "classify",
    study_path: StudyOption = None,
) -> None:
    """One tiny request to the configured provider: checks the key, the model and that it
    returns the requested JSON schema, before spending a day's quota."""
    from agm_miner.rq1.backends import PromptRequest, make_backend

    if phase not in ("propose", "consolidate", "classify"):
        raise typer.BadParameter("phase must be propose, consolidate or classify")
    settings, study = _context(study_path)
    backend = make_backend(settings, study.rq1, phase)
    result = backend.complete(
        PromptRequest(
            custom_id="smoke",
            system="You answer with the JSON object requested and nothing else.",
            user='Set "ok" to true and "echo" to the word agm.',
            schema=SMOKE_SCHEMA,
            max_tokens=2_000,
            schema_name="smoke",
        )
    )
    try:
        answer = json.loads(result.text or "")
    except json.JSONDecodeError:
        answer = None
    _print(
        {
            "provider": backend.provider,
            "model": backend.model,
            "served_by": result.served_by,
            "status": result.status,
            "error": result.error,
            "usage": result.usage,
            "answer": answer if answer is not None else result.text,
            "schema_ok": isinstance(answer, dict) and answer.get("echo") == "agm",
        }
    )


if __name__ == "__main__":
    app()
