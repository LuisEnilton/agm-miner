from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import Settings, StudyConfig
from agm_miner.database.models import (
    Artifact,
    ArtifactChange,
    ArtifactVersion,
    ChangeType,
    Commit,
    CommitCoauthor,
    CommitFile,
    ContributorActivity,
    Identity,
    PipelineRun,
    Repository,
    RepositoryActivity,
    RepositoryStatus,
    RunStatus,
)
from agm_miner.git.runner import GitError
from agm_miner.stages.mining import (
    Cancellation,
    MiningOptions,
    RepositoryLookupError,
    mine_repositories,
)
from tests.gitrepo import RepoBuilder

pytestmark = [pytest.mark.db, pytest.mark.git]

ALL_TABLES = (
    Identity,
    ContributorActivity,
    RepositoryActivity,
    Artifact,
    ArtifactVersion,
    Commit,
    CommitCoauthor,
    CommitFile,
    ArtifactChange,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(CLONE_DIR=str(tmp_path / "clones"))


@pytest.fixture
def origin(repo_builder: RepoBuilder) -> RepoBuilder:
    repo_builder.commit(
        "Add instructions\n\nCo-authored-by: Claude <noreply@anthropic.com>",
        {"CLAUDE.md": "# Agent\n\n## Build\nmake\n", "src/main.py": "x = 1\n"},
        date="2024-02-01T10:00:00+00:00",
    )
    repo_builder.commit(
        "Add skill",
        {".claude/skills/db/SKILL.md": "---\nname: db\n---\nUse migrations.\n"},
        author=("Bob", "bob@example.org"),
        date="2024-02-02T10:00:00+00:00",
    )
    return repo_builder


def add_repository(
    session_factory: sessionmaker[Session], status=RepositoryStatus.SELECTED
) -> None:
    with session_factory() as session, session.begin():
        session.add(Repository(id=42, node_id="R_42", full_name="acme/widget", status=status))


def counts(session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as session:
        return {
            model.__tablename__: session.execute(select(func.count()).select_from(model)).scalar()
            for model in ALL_TABLES
        }


def mine(settings, session_factory, origin, **options):
    return mine_repositories(
        settings,
        StudyConfig(),
        session_factory,
        MiningOptions(**options),
        clone_url=lambda _: origin.url,
    )


def test_mines_selected_repository(settings, session_factory, origin) -> None:
    add_repository(session_factory)
    stats = mine(settings, session_factory, origin)
    assert stats == {"candidates": 1, "mined": 1}

    with session_factory() as session:
        repository = session.get(Repository, 42)
        assert repository.status is RepositoryStatus.MINED
        assert repository.mined_head_sha == origin.head()
        assert repository.attempts == 1
        artifacts = {a.path: a for a in session.scalars(select(Artifact))}
        assert set(artifacts) == {"CLAUDE.md", ".claude/skills/db/SKILL.md"}
        change = session.scalars(
            select(ArtifactChange).where(ArtifactChange.artifact_id == artifacts["CLAUDE.md"].id)
        ).one()
        assert change.change_type is ChangeType.ADDED
        version = session.get(ArtifactVersion, change.version_id)
        assert version.content == "# Agent\n\n## Build\nmake\n"
        coauthor = session.scalars(select(CommitCoauthor)).one()
        assert coauthor.email == "noreply@anthropic.com"
        run = session.scalars(select(PipelineRun)).one()
        assert run.status is RunStatus.SUCCEEDED
        assert run.stats == {"candidates": 1, "mined": 1}
        assert run.study_config["selection"]["min_stars"] == 50
    assert not (Path(settings.clone_dir) / "42").exists()


def test_remining_is_idempotent_and_incremental(settings, session_factory, origin) -> None:
    add_repository(session_factory)
    mine(settings, session_factory, origin)
    first = counts(session_factory)

    assert mine(settings, session_factory, origin, refresh=True) == {
        "candidates": 1,
        "unchanged": 1,
    }
    assert mine(settings, session_factory, origin, force=True) == {"candidates": 1, "mined": 1}
    assert counts(session_factory) == first

    origin.commit(
        "Update", {"CLAUDE.md": "# Agent\n\n## Build\nmake all\n"}, date="2024-02-03T10:00:00+00:00"
    )
    assert mine(settings, session_factory, origin, refresh=True) == {"candidates": 1, "mined": 1}
    after = counts(session_factory)
    assert after["artifact_changes"] == first["artifact_changes"] + 1
    assert after["commits"] == first["commits"] + 1


def test_only_selected_repositories_are_candidates(settings, session_factory, origin) -> None:
    add_repository(session_factory, status=RepositoryStatus.REJECTED)
    assert mine(settings, session_factory, origin) == {"candidates": 0}
    # Explicit names bypass the status filter (manual validation runs).
    assert mine(settings, session_factory, origin, repo_names=["acme/widget"]) == {
        "candidates": 1,
        "mined": 1,
    }


def test_unknown_explicit_repository_fails_the_run(settings, session_factory, origin) -> None:
    with pytest.raises(RepositoryLookupError, match="agm add-repo"):
        mine(settings, session_factory, origin, repo_names=["ghost/repo"])
    with session_factory() as session:
        assert session.scalars(select(PipelineRun)).one().status is RunStatus.FAILED


def test_failure_is_recorded_and_previous_data_kept(
    settings, session_factory, origin, tmp_path
) -> None:
    add_repository(session_factory)
    mine(settings, session_factory, origin)
    before = counts(session_factory)

    stats = mine_repositories(
        settings,
        StudyConfig(),
        session_factory,
        MiningOptions(force=True),
        clone_url=lambda _: (tmp_path / "does-not-exist").as_uri(),
    )
    assert stats == {"candidates": 1, "failed": 1}
    with session_factory() as session:
        repository = session.get(Repository, 42)
        assert repository.status is RepositoryStatus.FAILED
        assert "GitError" in repository.last_error
        assert repository.attempts == 2
    assert counts(session_factory) == before

    assert mine(settings, session_factory, origin) == {"candidates": 0}
    assert mine(settings, session_factory, origin, include_failed=True) == {
        "candidates": 1,
        "mined": 1,
    }


def test_locked_repository_is_skipped(settings, session_factory, origin, pg_engine) -> None:
    add_repository(session_factory)
    with pg_engine.connect() as other_worker:
        assert other_worker.execute(select(func.pg_try_advisory_lock(42))).scalar()
        assert mine(settings, session_factory, origin) == {"candidates": 1, "locked": 1}
        other_worker.execute(select(func.pg_advisory_unlock(42)))
    assert mine(settings, session_factory, origin) == {"candidates": 1, "mined": 1}


def test_ambiguous_explicit_name_fails_the_run(settings, session_factory, origin) -> None:
    add_repository(session_factory)
    with session_factory() as session, session.begin():
        session.add(Repository(id=43, node_id="R_43", full_name="acme/widget"))
    with pytest.raises(RepositoryLookupError, match="select --reevaluate"):
        mine(settings, session_factory, origin, repo_names=["acme/widget"])


def add_repositories(session_factory: sessionmaker[Session], *repo_ids: int) -> None:
    with session_factory() as session, session.begin():
        for repo_id in repo_ids:
            session.add(
                Repository(
                    id=repo_id,
                    node_id=f"R_{repo_id}",
                    full_name=f"acme/repo{repo_id}",
                    status=RepositoryStatus.SELECTED,
                )
            )


def ctrl_c() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def test_first_interrupt_finishes_current_repository(settings, session_factory, origin) -> None:
    add_repositories(session_factory, 1, 2, 3)
    cancellation = Cancellation()

    def clone_url(full_name: str) -> str:
        if full_name == "acme/repo1":
            ctrl_c()
            assert cancellation.stopping.wait(5)
        return origin.url

    with pytest.raises(KeyboardInterrupt):
        mine_repositories(
            settings,
            StudyConfig(),
            session_factory,
            MiningOptions(workers=1),
            clone_url=clone_url,
            cancellation=cancellation,
        )
    with session_factory() as session:
        # The repository in progress was completed and saved; the others did not start.
        assert session.get(Repository, 1).status is RepositoryStatus.MINED
        for repo_id in (2, 3):
            repository = session.get(Repository, repo_id)
            assert repository.status is RepositoryStatus.SELECTED and repository.attempts == 0
        run = session.scalars(select(PipelineRun)).one()
        assert run.status is RunStatus.INTERRUPTED
        assert run.stats == {"candidates": 3, "mined": 1, "not_started": 2}

    # Running the same command again continues with what is left.
    assert mine(settings, session_factory, origin) == {"candidates": 2, "mined": 2}


def test_second_interrupt_aborts_without_marking_failures(
    settings, session_factory, origin
) -> None:
    add_repositories(session_factory, 1, 2)
    cancellation = Cancellation()

    def clone_url(full_name: str) -> str:
        ctrl_c()
        assert cancellation.stopping.wait(5)
        ctrl_c()
        assert cancellation.aborting.wait(5)
        return origin.url

    with pytest.raises(KeyboardInterrupt):
        mine_repositories(
            settings,
            StudyConfig(),
            session_factory,
            MiningOptions(workers=1),
            clone_url=clone_url,
            cancellation=cancellation,
        )
    with session_factory() as session:
        for repo_id in (1, 2):
            repository = session.get(Repository, repo_id)
            assert repository.status is RepositoryStatus.SELECTED
            assert repository.attempts == 0 and repository.last_error is None
        stats = session.scalars(select(PipelineRun)).one().stats
        assert stats == {"candidates": 2, "interrupted": 1, "not_started": 1}


def test_errors_caused_by_an_abort_are_not_failures(
    settings, session_factory, origin, monkeypatch
) -> None:
    """Git processes killed by the abort make extraction fail; that is not a repository failure."""
    from agm_miner.stages import mining as mining_module

    add_repositories(session_factory, 1)
    cancellation = Cancellation()

    def killed_mid_extraction(clone, config, on_phase):
        cancellation.stopping.set()
        cancellation.aborting.set()
        raise GitError(["log"], -9, "killed")

    monkeypatch.setattr(mining_module, "extract_repository", killed_mid_extraction)
    with pytest.raises(KeyboardInterrupt):
        mine_repositories(
            settings,
            StudyConfig(),
            session_factory,
            MiningOptions(workers=1),
            clone_url=lambda _: origin.url,
            cancellation=cancellation,
        )
    with session_factory() as session:
        repository = session.get(Repository, 1)
        assert repository.status is RepositoryStatus.SELECTED
        assert repository.last_error is None and repository.attempts == 0
