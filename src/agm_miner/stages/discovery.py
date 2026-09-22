"""Stage 1 — discover candidate repositories with the GitHub code-search API.

The Search API returns at most 1000 results per query. Queries without an explicit ``size:``
qualifier are therefore split by bisecting the file-size range until every slice fits under the
cap. Every hit is recorded in ``discovery_hits`` (query + path) as sampling provenance.

Known limitations (report them as threats to validity): code search only indexes default
branches and files smaller than 384 KB, and its results are not guaranteed to be deterministic.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import DiscoveryConfig, StudyConfig
from agm_miner.database.models import DiscoveryHit, Repository, RepositoryStatus, RunStage
from agm_miner.github.client import CodeSearchHit, GitHubClient
from agm_miner.runs import RunContext, track_run

logger = logging.getLogger(__name__)

_SIZE_QUALIFIER_RE = re.compile(r"(^|\s)size:\S+")


def discover_repositories(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    queries: Sequence[str] | None = None,
) -> dict[str, int]:
    selected_queries = list(queries or study.discovery.queries)
    with track_run(session_factory, RunStage.DISCOVER, study, {"queries": selected_queries}) as run:
        for query in selected_queries:
            _discover_query(query, study.discovery, session_factory, client, run)
        return dict(run.stats)


def _discover_query(
    query: str,
    config: DiscoveryConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    run: RunContext,
) -> None:
    logger.info("Discovering with query %r", query)
    run.increment("queries")
    if _SIZE_QUALIFIER_RE.search(query):
        logger.warning("Query %r already has a size qualifier; size bisection disabled", query)
        _collect(query, query, config, session_factory, client, run)
        return
    _bisect(query, config.size_min, config.size_max, config, session_factory, client, run)


def _bisect(
    base_query: str,
    low: int,
    high: int,
    config: DiscoveryConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    run: RunContext,
) -> None:
    sliced = f"{base_query} size:{low}..{high}"
    first = client.search_code(sliced, page=1, per_page=config.page_size)
    run.increment("api_requests")
    if first.total_count >= config.max_results_per_query and low < high:
        middle = (low + high) // 2
        logger.info("Splitting %s (%s matches)", sliced, first.total_count)
        run.increment("splits")
        _bisect(base_query, low, middle, config, session_factory, client, run)
        _bisect(base_query, middle + 1, high, config, session_factory, client, run)
        return
    _collect(base_query, sliced, config, session_factory, client, run, first_page=first)


def _collect(
    base_query: str,
    query: str,
    config: DiscoveryConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    run: RunContext,
    first_page=None,
) -> None:
    page = first_page
    if page is None:
        page = client.search_code(query, page=1, per_page=config.page_size)
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

    reachable = min(page.total_count, config.max_results_per_query)
    pages = max(1, math.ceil(reachable / config.page_size))
    number = 1
    while True:
        if page.incomplete_results:
            run.increment("incomplete_pages")
        _store_hits(session_factory, base_query, page.hits, run)
        number += 1
        if number > pages or not page.hits:
            break
        page = client.search_code(query, page=number, per_page=config.page_size)
        run.increment("api_requests")


def _store_hits(
    session_factory: sessionmaker[Session],
    base_query: str,
    hits: list[CodeSearchHit],
    run: RunContext,
) -> None:
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
    with session_factory() as session, session.begin():
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
    run.increment("hits", len(hits))
    run.increment("new_repositories", len(inserted))
