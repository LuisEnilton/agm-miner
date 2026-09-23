"""Stage 1 — discover candidate repositories with the GitHub code-search API.

The Search API returns at most 1000 results per query. Queries without an explicit ``size:``
qualifier are therefore split by bisecting the file-size range until every slice fits under the
cap. Every hit is recorded in ``discovery_hits`` (query + path) as sampling provenance.

Progress is stored per size slice in ``discovery_slices`` (SPLIT or COLLECTED), so an interrupted
discovery resumes without repeating finished slices; ``--restart`` forgets that progress.

Known limitations (report them as threats to validity): code search only indexes default
branches and files smaller than 384 KB, and its results are not guaranteed to be deterministic.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.artifacts import classify_included
from agm_miner.config import DiscoveryConfig, StudyConfig
from agm_miner.database.models import (
    DiscoveryHit,
    DiscoverySlice,
    Repository,
    RepositoryStatus,
    RunStage,
    SliceOutcome,
)
from agm_miner.github.client import CodeSearchHit, CodeSearchPage, GitHubClient
from agm_miner.runs import RunContext, exclusive_stage, track_run

logger = logging.getLogger(__name__)

_SIZE_QUALIFIER_RE = re.compile(r"(^|\s)size:\S+")


@dataclass(frozen=True, slots=True)
class _Search:
    config: DiscoveryConfig
    exclude_globs: tuple[str, ...]
    session_factory: sessionmaker[Session]
    client: GitHubClient
    run: RunContext


def discover_repositories(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    queries: Sequence[str] | None = None,
    *,
    restart: bool = False,
) -> dict[str, int]:
    """Search every query, resuming from ``discovery_slices`` unless ``restart`` is set.

    Only one discovery may run per database at a time (they would share one token's quota).
    """
    selected_queries = list(queries or study.discovery.queries)
    params = {"queries": selected_queries, "restart": restart}
    with (
        exclusive_stage(session_factory, RunStage.DISCOVER),
        track_run(session_factory, RunStage.DISCOVER, study, params) as run,
    ):
        if restart:
            _forget_progress(session_factory, selected_queries)
        search = _Search(
            config=study.discovery,
            exclude_globs=tuple(study.mining.exclude_path_globs),
            session_factory=session_factory,
            client=client,
            run=run,
        )
        for query in selected_queries:
            _discover_query(search, query)
        return dict(run.stats)


def _discover_query(search: _Search, query: str) -> None:
    logger.info("Discovering with query %r", query)
    search.run.increment("queries")
    if _SIZE_QUALIFIER_RE.search(query):
        logger.warning("Query %r already has a size qualifier; size bisection disabled", query)
        if _progress(search.session_factory, query) is SliceOutcome.COLLECTED:
            search.run.increment("resumed_slices")
            return
        _collect(search, query, query)
        return
    _bisect(search, query, search.config.size_min, search.config.size_max)


def _bisect(search: _Search, base_query: str, low: int, high: int) -> None:
    sliced = f"{base_query} size:{low}..{high}"
    done = _progress(search.session_factory, sliced)
    if done is SliceOutcome.COLLECTED:
        search.run.increment("resumed_slices")
        return
    if done is SliceOutcome.SPLIT:
        middle = (low + high) // 2
        _bisect(search, base_query, low, middle)
        _bisect(search, base_query, middle + 1, high)
        return

    first = search.client.search_code(sliced, page=1, per_page=search.config.page_size)
    search.run.increment("api_requests")
    if first.total_count >= search.config.max_results_per_query and low < high:
        middle = (low + high) // 2
        logger.info("Splitting %s (%s matches)", sliced, first.total_count)
        search.run.increment("splits")
        _record(search, sliced, base_query, SliceOutcome.SPLIT, first.total_count)
        _bisect(search, base_query, low, middle)
        _bisect(search, base_query, middle + 1, high)
        return
    _collect(search, base_query, sliced, first_page=first)


def _collect(
    search: _Search, base_query: str, query: str, first_page: CodeSearchPage | None = None
) -> None:
    config, run = search.config, search.run
    page = first_page
    if page is None:
        page = search.client.search_code(query, page=1, per_page=config.page_size)
        run.increment("api_requests")
    if page.total_count > config.max_results_per_query:
        # Only possible for a single-byte size slice or a user-provided size qualifier.
        logger.warning(
            "%s has %s matches; only the first %s are reachable",
            query,
            page.total_count,
            config.max_results_per_query,
        )
        run.increment("truncated_queries")
        run.increment("unreachable_matches", page.total_count - config.max_results_per_query)

    total = page.total_count
    reachable = min(total, config.max_results_per_query)
    pages = max(1, math.ceil(reachable / config.page_size))
    number = 1
    while True:
        if page.incomplete_results:
            run.increment("incomplete_pages")
        _store_hits(search, base_query, page.hits)
        number += 1
        if number > pages or not page.hits:
            break
        page = search.client.search_code(query, page=number, per_page=config.page_size)
        run.increment("api_requests")
    # Recorded only after every page is stored: an interrupted slice is searched again.
    _record(search, query, base_query, SliceOutcome.COLLECTED, total)


def _progress(session_factory: sessionmaker[Session], slice_query: str) -> SliceOutcome | None:
    with session_factory() as session:
        return session.scalar(
            select(DiscoverySlice.outcome).where(DiscoverySlice.slice_query == slice_query)
        )


def _record(
    search: _Search, slice_query: str, base_query: str, outcome: SliceOutcome, total_count: int
) -> None:
    values = {
        "slice_query": slice_query,
        "base_query": base_query,
        "outcome": outcome,
        "total_count": total_count,
        "run_id": search.run.id,
    }
    statement = insert(DiscoverySlice).values(**values)
    with search.session_factory() as session, session.begin():
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[DiscoverySlice.slice_query],
                set_={
                    "outcome": statement.excluded.outcome,
                    "total_count": statement.excluded.total_count,
                    "run_id": statement.excluded.run_id,
                    "completed_at": func.now(),
                },
            )
        )


def _forget_progress(session_factory: sessionmaker[Session], queries: Sequence[str]) -> None:
    with session_factory() as session, session.begin():
        session.execute(delete(DiscoverySlice).where(DiscoverySlice.base_query.in_(list(queries))))


def _store_hits(search: _Search, base_query: str, hits: list[CodeSearchHit]) -> None:
    run = search.run
    run.increment("hits", len(hits))
    # Code search matches file names loosely ("filename:AGENTS.md" also returns
    # "LLM_Agents.md"); keep only paths that are artifacts under the study's rules.
    hits = [hit for hit in hits if classify_included(hit.path, search.exclude_globs) is not None]
    run.increment("artifact_hits", len(hits))
    if not hits:
        return
    repositories = {
        hit.repo_id: {
            "id": hit.repo_id,
            "node_id": hit.node_id,
            "full_name": hit.full_name,
            "status": RepositoryStatus.DISCOVERED,
        }
        for hit in hits
    }
    provenance = {
        (hit.repo_id, hit.path): {
            "repo_id": hit.repo_id,
            "query": base_query,
            "path": hit.path,
            "run_id": run.id,
        }
        for hit in hits
    }
    with search.session_factory() as session, session.begin():
        inserted = session.execute(
            insert(Repository)
            .values(list(repositories.values()))
            # Any unique conflict (id, node_id or name) means the repository is already known.
            .on_conflict_do_nothing()
            .returning(Repository.id)
        ).all()
        # A hit whose repository row could not be inserted (a unique conflict on name or node id
        # with a different repository id) has nothing to point to.
        known = set(
            session.scalars(select(Repository.id).where(Repository.id.in_(list(repositories))))
        )
        rows = [row for row in provenance.values() if row["repo_id"] in known]
        if rows:
            session.execute(insert(DiscoveryHit).values(rows).on_conflict_do_nothing())
    run.increment("new_repositories", len(inserted))
