"""Pipeline-run bookkeeping: every stage execution is recorded with its parameters, the study
configuration snapshot and the exact code version, which makes the dataset reproducible."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

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
    """Record a pipeline run; the row is finalised as SUCCEEDED or FAILED (with the error)."""
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
