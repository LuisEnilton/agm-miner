from __future__ import annotations

import re

import pytest
from sqlalchemy import delete, func, select

from agm_miner.config import StudyConfig
from agm_miner.database.models import (
    DiscoveryHit,
    DiscoverySlice,
    PipelineRun,
    Repository,
    RepositoryStatus,
    RunStage,
    RunStatus,
)
from agm_miner.github.client import CodeSearchHit, CodeSearchPage
from agm_miner.runs import StageBusyError
from agm_miner.stages.discovery import discover_repositories

pytestmark = pytest.mark.db

_SIZE_RE = re.compile(r"size:(\d+)\.\.(\d+)")


class FakeSearch:
    """Simulates code search over files of given sizes, honouring the 1000-result cap."""

    def __init__(self, files: list[tuple[int, int, str]], cap: int = 1000) -> None:
        self.files = files  # (repo_id, size, path)
        self.cap = cap
        self.queries: list[tuple[str, int]] = []

    def search_code(self, query: str, page: int, per_page: int = 100) -> CodeSearchPage:
        self.queries.append((query, page))
        match = _SIZE_RE.search(query)
        low, high = (int(match.group(1)), int(match.group(2))) if match else (0, 10**9)
        matching = [f for f in self.files if low <= f[1] <= high]
        reachable = matching[: self.cap]
        chunk = reachable[(page - 1) * per_page : page * per_page]
        hits = [
            CodeSearchHit(repo_id=repo, node_id=f"R_{repo}", full_name=f"org/repo{repo}", path=path)
            for repo, _, path in chunk
        ]
        return CodeSearchPage(total_count=len(matching), incomplete_results=False, hits=hits)


def study(**discovery) -> StudyConfig:
    return StudyConfig.model_validate(
        {"discovery": {"queries": ["filename:CLAUDE.md"], **discovery}}
    )


def test_bisects_size_range_until_every_slice_fits(session_factory) -> None:
    # 2500 files in 2500 distinct repos, sizes 0..2499: needs splitting to reach all of them.
    fake = FakeSearch([(i + 1, i, "CLAUDE.md") for i in range(2500)])
    stats = discover_repositories(study(size_min=0, size_max=4999), session_factory, fake)

    assert stats["new_repositories"] == 2500
    assert stats["hits"] == 2500
    assert stats["splits"] >= 2
    assert "truncated_queries" not in stats
    with session_factory() as session:
        repositories = session.scalars(select(Repository)).all()
        assert len(repositories) == 2500
        assert {r.status for r in repositories} == {RepositoryStatus.DISCOVERED}
        hit = session.scalars(select(DiscoveryHit).where(DiscoveryHit.repo_id == 1)).one()
        # Provenance keeps the base query, not the internal size slice.
        assert hit.query == "filename:CLAUDE.md" and hit.path == "CLAUDE.md"
        assert session.scalars(select(PipelineRun)).one().stats == stats


def test_rediscovery_is_idempotent_and_keeps_status(session_factory) -> None:
    fake = FakeSearch([(1, 10, "CLAUDE.md"), (1, 20, "sub/CLAUDE.md"), (2, 30, "CLAUDE.md")])
    first = discover_repositories(study(), session_factory, fake)
    assert first["new_repositories"] == 2 and first["hits"] == 3
    with session_factory() as session, session.begin():
        session.get(Repository, 1).status = RepositoryStatus.MINED

    # A finished discovery is not repeated...
    calls = len(fake.queries)
    second = discover_repositories(study(), session_factory, fake)
    assert second == {"queries": 1, "resumed_slices": 1}
    assert len(fake.queries) == calls

    # ...unless explicitly restarted, which searches again without duplicating anything.
    third = discover_repositories(study(), session_factory, fake, restart=True)
    assert third["new_repositories"] == 0 and third["hits"] == 3
    with session_factory() as session:
        assert session.get(Repository, 1).status is RepositoryStatus.MINED
        assert len(session.scalars(select(DiscoveryHit)).all()) == 3


class InterruptingSearch(FakeSearch):
    """Raises KeyboardInterrupt (as Ctrl+C would) on the N-th request."""

    def __init__(self, *args, interrupt_at: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.interrupt_at = interrupt_at

    def search_code(self, query: str, page: int, per_page: int = 100) -> CodeSearchPage:
        if len(self.queries) + 1 == self.interrupt_at:
            self.queries.append((query, page))
            raise KeyboardInterrupt
        return super().search_code(query, page, per_page)


def test_interrupted_discovery_resumes_where_it_stopped(session_factory) -> None:
    files = [(i + 1, i, "CLAUDE.md") for i in range(2500)]
    config = study(size_min=0, size_max=4999)
    uninterrupted = FakeSearch(files)
    discover_repositories(config, session_factory, uninterrupted)
    total_requests = len(uninterrupted.queries)

    # Start over on an empty database and stop halfway through.
    with session_factory() as session, session.begin():
        for model in (DiscoveryHit, DiscoverySlice, Repository, PipelineRun):
            session.execute(delete(model))
    interrupted = InterruptingSearch(files, interrupt_at=total_requests // 2)
    with pytest.raises(KeyboardInterrupt):
        discover_repositories(config, session_factory, interrupted)
    with session_factory() as session:
        run = session.scalars(select(PipelineRun)).one()
        assert run.status is RunStatus.INTERRUPTED
        partial = session.scalar(select(func.count()).select_from(Repository))
    assert 0 < partial < 2500

    resumed = FakeSearch(files)
    stats = discover_repositories(config, session_factory, resumed)
    assert stats["resumed_slices"] >= 1
    # Finished slices are not searched again: resuming costs less than starting over.
    assert len(resumed.queries) < total_requests
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Repository)) == 2500


def test_only_one_discovery_at_a_time(session_factory, pg_engine) -> None:
    with pg_engine.connect() as other_process:
        key = list(RunStage).index(RunStage.DISCOVER)
        assert other_process.execute(select(func.pg_try_advisory_lock(0x41474D, key))).scalar()
        with pytest.raises(StageBusyError, match="agm discover"):
            discover_repositories(study(), session_factory, FakeSearch([]))
        other_process.execute(select(func.pg_advisory_unlock(0x41474D, key)))
    assert discover_repositories(study(), session_factory, FakeSearch([]))["queries"] == 1


def test_unsplittable_slices_are_reported_as_truncated(session_factory) -> None:
    fake = FakeSearch([(i + 1, 7, "CLAUDE.md") for i in range(1200)])
    stats = discover_repositories(study(size_min=7, size_max=7), session_factory, fake)
    assert stats["truncated_queries"] == 1
    assert stats["unreachable_matches"] == 200
    assert stats["new_repositories"] == 1000


def test_explicit_size_qualifier_disables_bisection(session_factory) -> None:
    fake = FakeSearch([(1, 10, "CLAUDE.md")])
    discover_repositories(study(), session_factory, fake, queries=["filename:CLAUDE.md size:>5"])
    assert fake.queries == [("filename:CLAUDE.md size:>5", 1)]
