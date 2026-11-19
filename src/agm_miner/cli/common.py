"""Shared pieces of the CLI: the Typer apps, the help panels, options and helpers."""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, ParamSpec, TypeVar

import click
import typer
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import Settings, StudyConfig, get_settings, load_study_config
from agm_miner.database.session import get_session_factory

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Never print local variables in tracebacks: they may include configuration values.
    pretty_exceptions_show_locals=False,
    help="agm-miner: mining pipeline for AI agent configuration artifacts (run stages in order).",
)

# Panels of `agm --help` and `agm help`, in this order. The experimental panel holds what was
# used in the pilot or to compare alternatives, not to produce the study's final results.
PANEL_COLLECT = "Collection (GitHub + Postgres)"
PANEL_ANALYSIS = "Export and analysis"
PANEL_RESULTS = "Study results (no database)"
PANEL_OPS = "Operations"
PANEL_EXPERIMENTAL = "Experimental / pilot (not used for the final results)"
PANELS = (PANEL_COLLECT, PANEL_ANALYSIS, PANEL_RESULTS, PANEL_OPS, PANEL_EXPERIMENTAL)

runs_app = typer.Typer(no_args_is_help=True, help="Inspect pipeline runs.")
app.add_typer(runs_app, name="runs", rich_help_panel=PANEL_OPS)
rq1_app = typer.Typer(
    no_args_is_help=True,
    help="RQ1 content coding with an LLM: classify every section with a codebook (the pilot "
    "commands create a codebook from a sample).",
)
app.add_typer(rq1_app, name="rq1", rich_help_panel=PANEL_RESULTS)
rq1_validate_app = typer.Typer(
    no_args_is_help=True,
    help="Validate the classification against hand labels (export, llm, score).",
)
rq1_app.add_typer(rq1_validate_app, name="validate", rich_help_panel=PANEL_EXPERIMENTAL)
rq1_agentreadmes_app = typer.Typer(
    no_args_is_help=True,
    help="External validation on the Agent READMEs dataset (import, score).",
)
rq1_app.add_typer(rq1_agentreadmes_app, name="agentreadmes", rich_help_panel=PANEL_RESULTS)
rq2_app = typer.Typer(no_args_is_help=True, help="RQ2 structure: summary tables of rq2_structure.")
app.add_typer(rq2_app, name="rq2", rich_help_panel=PANEL_RESULTS)

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
        from agm_miner.analysis.paper_figures import FiguresInputError
        from agm_miner.analysis.sampling_audit import AuditOutputError
        from agm_miner.database.schema import SchemaMismatchError
        from agm_miner.git.runner import GitError
        from agm_miner.github.client import GitHubAuthError, GitHubRequestError
        from agm_miner.rq1.agentreadmes import AgentReadmesError, OutputExistsError
        from agm_miner.rq1.llm import LLMAuthError, LLMRequestError
        from agm_miner.rq1.prompts import InvalidAnswer
        from agm_miner.rq1.stages import (
            IncompleteProposalsError,
            InvalidSheetError,
            NothingToCompareError,
            SampleMismatchError,
            ValidationSheetExistsError,
        )
        from agm_miner.rq1.store import Rq1BusyError
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
            AuditOutputError,
            FiguresInputError,
            SchemaMismatchError,
            StageBusyError,
            GitError,
            GitHubAuthError,
            GitHubRequestError,
            LLMAuthError,
            LLMRequestError,
            Rq1BusyError,
            InvalidAnswer,
            SampleMismatchError,
            IncompleteProposalsError,
            InvalidSheetError,
            NothingToCompareError,
            ValidationSheetExistsError,
            SelectionError,
            RepositoryLookupError,
            AgentReadmesError,
            OutputExistsError,
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


GoldOption = Annotated[
    Path | None,
    typer.Option("--gold", help="Analysis output to code (default: the latest in DATA_DIR/gold)."),
]


def _rq1_dirs(settings: Settings, gold: Path | None) -> tuple[Path, Path]:
    """The analysis output to read and the RQ1 directory that belongs to it."""
    from agm_miner.analysis.datasets import latest_analysis

    gold_dir = gold or latest_analysis(settings.gold_dir)
    return gold_dir, settings.rq1_dir / gold_dir.name


CodebookOption = Annotated[
    Path | None, typer.Option("--codebook", help="Curated codebook (default: rq1.codebook).")
]


ModelOption = Annotated[
    str | None,
    typer.Option(
        "--model",
        help="Classifier as provider:model, e.g. gemini:gemini-3.5-flash-lite (default: "
        "rq1.provider and rq1.classify_model).",
    ),
]


UnitOption = Annotated[
    str,
    typer.Option(
        click_type=click.Choice(["section", "file"]),
        help="Classify each section (section) or each whole file in one request (file, the "
        "Agent READMEs paper's strategy; needs a dataset imported with its file contents).",
    ),
]


def _with_model(study: StudyConfig, model: str | None, phase: str = "classify") -> StudyConfig:
    """``study`` with the ``--model`` model for ``phase`` (recorded in every manifest's
    rq1_config)."""
    if model is None:
        return study
    try:
        return study.model_copy(update={"rq1": study.rq1.with_model(model, phase)})
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--model") from exc
