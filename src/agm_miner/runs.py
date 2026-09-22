"""Pipeline-run bookkeeping: every stage execution is recorded with its parameters, the study
configuration snapshot and the exact code version, which makes the dataset reproducible."""

from __future__ import annotations

import logging
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import StudyConfig
from agm_miner.database.models import PipelineRun, RunStage, RunStatus

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@lru_cache
def code_version() -> str:
    """Git commit of this checkout (``-dirty`` when there are uncommitted changes)."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except (OSError, subprocess.CalledProcessError):
        try:
            return f"pkg-{version('agm-miner')}"
        except PackageNotFoundError:
            return "unknown"


@dataclass(slots=True)
class RunContext:
    id: int
    stage: RunStage
    stats: dict[str, Any] = field(default_factory=dict)

    def increment(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount


@contextmanager
def track_run(
    session_factory: sessionmaker[Session],
    stage: RunStage,
    study: StudyConfig,
    params: dict[str, Any] | None = None,
) -> Iterator[RunContext]:
    """Record a pipeline run; the row ends as SUCCEEDED, INTERRUPTED (Ctrl+C / SIGTERM) or
    FAILED (with the error). A process killed with SIGKILL leaves the row RUNNING."""
    with session_factory() as session, session.begin():
        run = PipelineRun(
            stage=stage,
            status=RunStatus.RUNNING,
            code_version=code_version(),
            study_config_hash=study.fingerprint(),
            study_config=study.snapshot(),
            params=params or {},
            stats={},
        )
        session.add(run)
        session.flush()
        context = RunContext(id=run.id, stage=stage)
    logger.info("Started %s run %s", stage.value, context.id)

    try:
        yield context
    except KeyboardInterrupt:
        _finish(session_factory, context, RunStatus.INTERRUPTED)
        raise
    except BaseException as exc:
        _finish(session_factory, context, RunStatus.FAILED, error=repr(exc))
        raise
    _finish(session_factory, context, RunStatus.SUCCEEDED)


def _finish(
    session_factory: sessionmaker[Session],
    context: RunContext,
    status: RunStatus,
    error: str | None = None,
) -> None:
    with session_factory() as session, session.begin():
        run = session.get(PipelineRun, context.id)
        run.status = status
        run.finished_at = datetime.now(UTC)
        run.stats = dict(context.stats)
        run.error = error
    logger.info("Finished %s run %s: %s %s", context.stage.value, context.id, status, context.stats)


class StageBusyError(RuntimeError):
    """Another process is already running a stage that must run alone."""


# Advisory-lock namespace for stage locks. The two-integer lock space never overlaps with the
# single-bigint space used for per-repository mining locks.
_STAGE_LOCK_NAMESPACE = 0x41474D  # "AGM"


@contextmanager
def exclusive_stage(session_factory: sessionmaker[Session], stage: RunStage) -> Iterator[None]:
    """Hold a database-wide lock for ``stage`` (discover/select share one GitHub token quota, so
    two concurrent instances would only duplicate work and trip rate limits)."""
    key = list(RunStage).index(stage)
    engine = session_factory.kw["bind"]
    with engine.connect() as connection:
        acquired = connection.execute(
            select(func.pg_try_advisory_lock(_STAGE_LOCK_NAMESPACE, key))
        ).scalar()
        connection.commit()
        if not acquired:
            raise StageBusyError(
                f"Another `agm {stage.value.lower()}` is already running against this database."
            )
        try:
            yield
        finally:
            connection.execute(select(func.pg_advisory_unlock(_STAGE_LOCK_NAMESPACE, key)))
            connection.commit()


@contextmanager
def interrupt_signals() -> Iterator[None]:
    """Make Ctrl+C (SIGINT) and SIGTERM (e.g. ``kill <pid>``) raise KeyboardInterrupt, so every
    stage stops cleanly and records its run as INTERRUPTED.

    SIGINT is installed explicitly because processes started in the background by a
    non-interactive shell inherit it as *ignored*, and Python then never raises
    KeyboardInterrupt.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame) -> None:
        raise KeyboardInterrupt

    previous = {
        signal.SIGINT: signal.signal(signal.SIGINT, signal.default_int_handler),
        signal.SIGTERM: signal.signal(signal.SIGTERM, handler),
    }
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
