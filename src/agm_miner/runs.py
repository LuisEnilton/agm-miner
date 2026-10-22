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
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import StudyConfig
from agm_miner.database.models import PipelineRun, RunStage, RunStatus
from agm_miner.progress import Progress, publishing

logger = logging.getLogger(__name__)

# How often a running stage saves its progress and stats. A RUNNING run whose heartbeat is older
# than STALE_AFTER gave no sign of life: its process was most likely killed (kill -9, crash).
HEARTBEAT_SECONDS = 5.0
STALE_AFTER = timedelta(seconds=3 * HEARTBEAT_SECONDS)

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
    progress: Progress = field(default_factory=Progress)

    def increment(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount


@contextmanager
def track_run(
    session_factory: sessionmaker[Session],
    stage: RunStage,
    study: StudyConfig,
    params: dict[str, Any] | None = None,
    *,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> Iterator[RunContext]:
    """Record a pipeline run; the row ends as SUCCEEDED, INTERRUPTED (Ctrl+C / SIGTERM) or
    FAILED (with the error).

    While the run is alive, its progress, stats and a heartbeat are saved every
    ``heartbeat_seconds`` (``agm status`` reads them). A process killed with SIGKILL leaves the
    row RUNNING with an old heartbeat, which ``agm status`` reports as stale.
    """
    progress = Progress(stage.value)
    with session_factory() as session, session.begin():
        run = PipelineRun(
            stage=stage,
            status=RunStatus.RUNNING,
            code_version=code_version(),
            study_config_hash=study.fingerprint(),
            study_config=study.snapshot(),
            params=params or {},
            stats={},
            progress=progress.snapshot(),
            heartbeat_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        context = RunContext(id=run.id, stage=stage, progress=progress)
    logger.info("Started %s run %s", stage.value, context.id)

    heartbeat = _Heartbeat(session_factory, context, heartbeat_seconds)
    heartbeat.start()
    try:
        with publishing(progress):
            yield context
    except KeyboardInterrupt:
        heartbeat.stop()
        _finish(session_factory, context, RunStatus.INTERRUPTED)
        raise
    except BaseException as exc:
        heartbeat.stop()
        _finish(session_factory, context, RunStatus.FAILED, error=repr(exc))
        raise
    heartbeat.stop()
    _finish(session_factory, context, RunStatus.SUCCEEDED)


class _Heartbeat(threading.Thread):
    """Save the run's progress, stats and heartbeat periodically (its own session)."""

    def __init__(
        self, session_factory: sessionmaker[Session], context: RunContext, interval: float
    ) -> None:
        super().__init__(name=f"heartbeat-{context.id}", daemon=True)
        self._session_factory = session_factory
        self._context = context
        self._interval = interval
        self._stopped = threading.Event()

    def run(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                _save_progress(self._session_factory, self._context)
            except Exception:  # a lost heartbeat must never stop the stage
                logger.warning("Could not save the progress of run %s", self._context.id)

    def stop(self) -> None:
        self._stopped.set()
        self.join(timeout=self._interval + 5)


def _save_progress(session_factory: sessionmaker[Session], context: RunContext) -> None:
    with session_factory() as session, session.begin():
        run = session.get(PipelineRun, context.id)
        run.progress = context.progress.snapshot()
        run.stats = dict(context.stats)
        run.heartbeat_at = datetime.now(UTC)


def running_runs(session_factory: sessionmaker[Session]) -> list[PipelineRun]:
    """Runs still marked RUNNING (alive or stale), oldest first."""
    with session_factory() as session:
        return list(
            session.scalars(
                select(PipelineRun)
                .where(PipelineRun.status == RunStatus.RUNNING)
                .order_by(PipelineRun.id)
            )
        )


def is_stale(run: PipelineRun, now: datetime | None = None) -> bool:
    """A RUNNING run that stopped giving signs of life (its process is most likely gone)."""
    if run.status is not RunStatus.RUNNING:
        return False
    if run.heartbeat_at is None:  # recorded before heartbeats existed
        return True
    return (now or datetime.now(UTC)) - run.heartbeat_at > STALE_AFTER


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
        run.progress = context.progress.snapshot()
        run.heartbeat_at = run.finished_at
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
