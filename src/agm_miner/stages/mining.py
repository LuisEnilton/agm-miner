"""Stage 3 — mine selected repositories from local blobless clones.

Guarantees:

* **Idempotent**: a repository's mined data is replaced atomically (delete + insert in one
  transaction), so re-mining never duplicates rows and a failure leaves the previous data intact.
* **Incremental**: repositories whose remote HEAD equals the mined HEAD are skipped (``--force``
  re-mines anyway).
* **Concurrency-safe**: each repository is guarded by a PostgreSQL advisory lock, so several
  workers or processes never mine the same repository at once.
* **Failures are recorded** (``FAILED`` + ``last_error`` + ``attempts``) and can be retried.
* **Interruptible**: the first Ctrl+C/SIGTERM stops scheduling new repositories and lets the
  in-progress ones finish; a second one aborts them. Aborted or never-started repositories keep
  their previous status, so the next run simply continues.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import Settings, StudyConfig
from agm_miner.database.models import (
    Artifact,
    ArtifactChange,
    ArtifactVersion,
    Commit,
    CommitCoauthor,
    CommitFile,
    ContributorActivity,
    Identity,
    Repository,
    RepositoryActivity,
    RepositoryStatus,
    RunStage,
)
from agm_miner.extraction import Extraction, extract_repository
from agm_miner.git.repository import GitRepository, ensure_git_version, remote_head
from agm_miner.git.runner import terminate_active_processes
from agm_miner.runs import RunContext, track_run

logger = logging.getLogger(__name__)

_ERROR_MAX_CHARS = 4000


class RepositoryLookupError(LookupError):
    """Repositories requested by name are unknown or ambiguous."""


class MiningOutcome(StrEnum):
    MINED = "mined"
    UNCHANGED = "unchanged"
    FAILED = "failed"
    LOCKED = "locked"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"  # aborted while in progress; status left untouched
    NOT_STARTED = "not_started"  # cancelled before it started


@dataclass(slots=True)
class Cancellation:
    """Shared stop flags: ``stopping`` = start nothing new, ``aborting`` = stop in-progress work."""

    stopping: threading.Event = field(default_factory=threading.Event)
    aborting: threading.Event = field(default_factory=threading.Event)

    def request(self) -> None:
        """Escalate one step (called on each Ctrl+C / SIGTERM)."""
        if not self.stopping.is_set():
            self.stopping.set()
            logger.warning("Stopping: no new repositories will start (Ctrl+C again to abort)")
            return
        if not self.aborting.is_set():
            self.aborting.set()
            killed = terminate_active_processes()
            logger.warning("Aborting in-progress repositories (%s git processes killed)", killed)


@dataclass(frozen=True, slots=True)
class MiningOptions:
    repo_names: Sequence[str] = ()
    limit: int | None = None
    workers: int = 1
    include_failed: bool = False
    refresh: bool = False
    force: bool = False
    keep_clones: bool = False


def github_clone_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def candidate_statuses(options: MiningOptions) -> set[RepositoryStatus]:
    statuses = {RepositoryStatus.SELECTED}
    if options.include_failed:
        statuses.add(RepositoryStatus.FAILED)
    if options.refresh or options.force:
        statuses.add(RepositoryStatus.MINED)
    return statuses


def mine_repositories(
    settings: Settings,
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    options: MiningOptions,
    clone_url: Callable[[str], str] = github_clone_url,
    cancellation: Cancellation | None = None,
) -> dict[str, int]:
    """Mine the candidate repositories; raises KeyboardInterrupt after a requested stop."""
    cancellation = cancellation or Cancellation()
    params = {
        "repo_names": list(options.repo_names),
        "limit": options.limit,
        "workers": options.workers,
        "include_failed": options.include_failed,
        "refresh": options.refresh,
        "force": options.force,
    }
    ensure_git_version()
    with track_run(session_factory, RunStage.MINE, study, params) as run:
        candidates = _candidates(session_factory, options)
        run.stats["candidates"] = len(candidates)
        logger.info("Mining %s repositories with %s worker(s)", len(candidates), options.workers)

        def work(repo_id: int) -> MiningOutcome:
            return _mine_one(
                settings, study, session_factory, options, clone_url, repo_id, run.id, cancellation
            )

        pool = ThreadPoolExecutor(max_workers=max(1, options.workers))
        try:
            with _signals_escalate(cancellation):
                futures = [pool.submit(work, repo_id) for repo_id in candidates]
                _drain(futures, run, cancellation)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        if cancellation.stopping.is_set():
            raise KeyboardInterrupt
        return dict(run.stats)


@contextmanager
def _signals_escalate(cancellation: Cancellation) -> Iterator[None]:
    """While mining, Ctrl+C/SIGTERM only escalate ``cancellation``.

    A KeyboardInterrupt can be raised at any bytecode, even inside the code handling the
    previous one, so the signals are turned into flags that the main loop checks instead.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum, frame) -> None:
        cancellation.request()

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def _drain(
    futures: list[Future[MiningOutcome]],
    run: RunContext,
    cancellation: Cancellation,
) -> None:
    """Collect outcomes; once a stop is requested, repositories that did not start are cancelled."""
    pending: set[Future[MiningOutcome]] = set(futures)
    while pending:
        done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
        if cancellation.stopping.is_set():
            for future in pending:
                future.cancel()
        for future in done:
            if future.cancelled():
                run.increment(MiningOutcome.NOT_STARTED.value)
            else:
                run.increment(future.result().value)


def _candidates(session_factory: sessionmaker[Session], options: MiningOptions) -> list[int]:
    with session_factory() as session:
        query = select(Repository.id, Repository.full_name).order_by(Repository.id)
        if options.repo_names:
            query = query.where(Repository.full_name.in_(list(options.repo_names)))
        else:
            query = query.where(Repository.status.in_(candidate_statuses(options)))
        if options.limit is not None:
            query = query.limit(options.limit)
        rows = session.execute(query).all()
    if options.repo_names:
        missing = set(options.repo_names) - {name for _, name in rows}
        if missing:
            raise RepositoryLookupError(
                f"Unknown repositories (add them first with `agm add-repo`): {sorted(missing)}"
            )
        names = [name for _, name in rows]
        ambiguous = sorted({name for name in names if names.count(name) > 1})
        if ambiguous:
            raise RepositoryLookupError(
                f"Names shared by several repositories (run `agm select --reevaluate` to refresh "
                f"their current names): {ambiguous}"
            )
    return [repo_id for repo_id, _ in rows]


def _mine_one(
    settings: Settings,
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    options: MiningOptions,
    clone_url: Callable[[str], str],
    repo_id: int,
    run_id: int,
    cancellation: Cancellation,
) -> MiningOutcome:
    if cancellation.stopping.is_set():
        return MiningOutcome.NOT_STARTED
    engine = session_factory.kw["bind"]
    with engine.connect() as lock_connection:
        acquired = lock_connection.execute(select(func.pg_try_advisory_lock(repo_id))).scalar()
        lock_connection.commit()
        if not acquired:
            logger.info("Repository %s is being mined by another worker", repo_id)
            return MiningOutcome.LOCKED
        try:
            return _mine_locked(
                settings, study, session_factory, options, clone_url, repo_id, run_id, cancellation
            )
        finally:
            lock_connection.execute(select(func.pg_advisory_unlock(repo_id)))
            lock_connection.commit()


def _mine_locked(
    settings: Settings,
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    options: MiningOptions,
    clone_url: Callable[[str], str],
    repo_id: int,
    run_id: int,
    cancellation: Cancellation,
) -> MiningOutcome:
    with session_factory() as session:
        repository = session.get(Repository, repo_id)
        full_name, status, mined_head = (
            repository.full_name,
            repository.status,
            repository.mined_head_sha,
        )
    # Re-check under the lock: another worker may have finished it since candidate selection.
    if not options.repo_names and status not in candidate_statuses(options):
        return MiningOutcome.SKIPPED

    url = clone_url(full_name)
    clone_path = Path(settings.clone_dir) / str(repo_id)
    clone: GitRepository | None = None
    try:
        if status is RepositoryStatus.MINED and mined_head and not options.force:
            current_head = remote_head(url, settings.git_timeout_seconds)
            if current_head == mined_head:
                logger.info("%s unchanged since last mining (%s)", full_name, mined_head[:12])
                return MiningOutcome.UNCHANGED

        if cancellation.aborting.is_set():
            return MiningOutcome.INTERRUPTED
        logger.info("Cloning %s", full_name)
        clone = GitRepository.clone(url, clone_path, settings.git_timeout_seconds)
        extraction = extract_repository(clone, study.mining)
        if cancellation.aborting.is_set():
            return MiningOutcome.INTERRUPTED
        with session_factory() as session, session.begin():
            persist_extraction(session, repo_id, extraction)
            session.execute(
                update(Repository)
                .where(Repository.id == repo_id)
                .values(
                    status=RepositoryStatus.MINED,
                    mined_head_sha=extraction.head_sha,
                    mined_at=datetime.now(UTC),
                    mined_run_id=run_id,
                    last_error=None,
                    attempts=Repository.attempts + 1,
                )
            )
        logger.info(
            "Mined %s: %s artifacts, %s versions, %s changes, %s commits",
            full_name,
            len(extraction.artifacts),
            len(extraction.versions),
            len(extraction.changes),
            len(extraction.commits),
        )
        return MiningOutcome.MINED
    except Exception as exc:
        if cancellation.aborting.is_set():
            # The error is the abort itself (killed git processes), not a property of the repo.
            logger.info("Aborted %s; its status is unchanged", full_name)
            return MiningOutcome.INTERRUPTED
        logger.exception("Failed to mine %s", full_name)
        with session_factory() as session, session.begin():
            session.execute(
                update(Repository)
                .where(Repository.id == repo_id)
                .values(
                    status=RepositoryStatus.FAILED,
                    last_error=f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS],
                    attempts=Repository.attempts + 1,
                )
            )
        return MiningOutcome.FAILED
    finally:
        if clone is not None and not options.keep_clones:
            clone.remove()


def persist_extraction(session: Session, repo_id: int, extraction: Extraction) -> None:
    """Replace everything mined for ``repo_id`` with ``extraction``.

    The caller owns the transaction, so the replacement is atomic.
    """
    # Deleting commits cascades to coauthors, commit files and artifact changes; deleting
    # artifacts cascades to versions; deleting identities cascades to contributor activity.
    session.execute(delete(Commit).where(Commit.repo_id == repo_id))
    session.execute(delete(Artifact).where(Artifact.repo_id == repo_id))
    session.execute(delete(Identity).where(Identity.repo_id == repo_id))
    session.execute(delete(RepositoryActivity).where(RepositoryActivity.repo_id == repo_id))

    identity_ids = _insert_returning(
        session,
        Identity,
        [
            {
                "repo_id": repo_id,
                "identity_key": record.key,
                "name": record.name,
                "email": record.email,
                "github_login": record.github_login,
                "commits": record.commits,
                "first_commit_at": record.first_commit_at,
                "last_commit_at": record.last_commit_at,
            }
            for record in extraction.identities.values()
        ],
        key_columns=("identity_key",),
    )
    _insert(
        session,
        ContributorActivity,
        [
            {
                "identity_id": identity_ids[(key,)],
                "file_category": category,
                "repo_id": repo_id,
                "commits": counter.commits,
                "file_changes": counter.file_changes,
            }
            for (key, category), counter in extraction.contributor_activity.items()
        ],
    )
    _insert(
        session,
        RepositoryActivity,
        [
            {"repo_id": repo_id, "month": month, "commits": commits, "authors": authors}
            for month, (commits, authors) in extraction.monthly_activity.items()
        ],
    )

    artifact_ids = _insert_returning(
        session,
        Artifact,
        [
            {
                "repo_id": repo_id,
                "path": record.path,
                "tool": record.match.tool,
                "kind": record.match.kind,
                "scope": record.match.scope,
                "skill_name": record.match.skill_name,
                "exists_at_head": record.exists_at_head,
                "head_blob_sha": record.head_blob_sha,
            }
            for record in extraction.artifacts.values()
        ],
        key_columns=("path",),
    )
    version_ids = _insert_returning(
        session,
        ArtifactVersion,
        [
            {
                "artifact_id": artifact_ids[(version.path,)],
                "repo_id": repo_id,
                "blob_sha": version.blob_sha,
                "file_mode": version.file_mode,
                "is_symlink": version.is_symlink,
                "size_bytes": version.size_bytes,
                "content": version.content,
                "content_omitted": version.content_omitted,
            }
            for version in extraction.versions
        ],
        key_columns=("artifact_id", "blob_sha"),
    )

    _insert(
        session,
        Commit,
        [
            {
                "repo_id": repo_id,
                "sha": commit.sha,
                "author_identity_id": identity_ids[(commit.identity_key,)],
                "author_name": commit.author_name,
                "author_email": commit.author_email,
                "committer_name": commit.committer_name,
                "committer_email": commit.committer_email,
                "authored_at": commit.authored_at,
                "committed_at": commit.committed_at,
                "message": commit.message,
                "parents": list(commit.parents),
            }
            for commit in extraction.commits
        ],
    )
    _insert(
        session,
        CommitCoauthor,
        [
            {
                "repo_id": repo_id,
                "sha": commit.sha,
                "position": position,
                "name": name,
                "email": email,
            }
            for commit in extraction.commits
            for position, (name, email) in enumerate(commit.coauthors)
        ],
    )
    _insert(
        session,
        CommitFile,
        [
            {
                "repo_id": repo_id,
                "sha": record.sha,
                "path": record.path,
                "change_type": record.change_type,
                "file_category": record.file_category,
            }
            for record in extraction.commit_files
        ],
    )
    _insert(
        session,
        ArtifactChange,
        [
            {
                "artifact_id": artifact_ids[(change.path,)],
                "sha": change.sha,
                "repo_id": repo_id,
                "change_type": change.change_type,
                "source_path": change.source_path,
                "similarity": change.similarity,
                "additions": change.additions,
                "deletions": change.deletions,
                "blob_sha": change.blob_sha,
                "previous_blob_sha": change.previous_blob_sha,
                "version_id": (
                    version_ids[(artifact_ids[(change.path,)], change.blob_sha)]
                    if change.blob_sha is not None
                    else None
                ),
            }
            for change in extraction.changes
        ],
    )


def _insert(session: Session, model: type, rows: list[dict]) -> None:
    if rows:
        session.execute(insert(model), rows)


def _insert_returning(
    session: Session, model: type, rows: list[dict], key_columns: tuple[str, ...]
) -> dict[tuple, int]:
    """Bulk insert and map each row's natural key to its generated id."""
    if not rows:
        return {}
    columns = [getattr(model, column) for column in key_columns]
    result = session.execute(insert(model).returning(model.id, *columns), rows)
    return {tuple(row[1:]): row[0] for row in result.all()}
