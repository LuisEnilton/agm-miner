from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
import re
import time

from github import GithubException

from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import Settings
from agm_miner.core.github_client import GitHubClient
from agm_miner.database.models import Repository, RepositoryStatus
from agm_miner.database.session import get_session_factory


logger = logging.getLogger(__name__)

SIZE_RANGE_START = 20
SIZE_RANGE_END = 384_000
MAX_CODE_SEARCH_RESULTS = 1000
PAGE_SIZE = 100


@dataclass(slots=True)
class DiscoveryResult:
    repositories_discovered: int = 0


def _compose_query(base_query: str, start_date: date, end_date: date) -> str:
    _ = (start_date, end_date)
    return base_query.strip()


def _has_size_qualifier(query: str) -> bool:
    return re.search(r"(^|\s)size:\S+", query) is not None


def _append_size_qualifier(base_query: str, min_size: int, max_size: int) -> str:
    return f"{base_query} size:{min_size}..{max_size}".strip()


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    if isinstance(exc, GithubException):
        if getattr(exc, "status", None) == 403:
            return True
    return (
        "rate limit" in message
        or "secondary rate limit" in message
        or "403" in message
        or "abuse" in message
        or "retryerror" in message
    )


def _upsert_pending_repository(session: Session, repo_id: int, full_name: str) -> None:
    repository = session.get(Repository, repo_id)
    if repository is None:
        session.add(
            Repository(
                id=repo_id,
                full_name=full_name,
                status=RepositoryStatus.PENDING,
            )
        )
        return
    repository.full_name = full_name


def _discover_window(
    session: Session,
    client: GitHubClient,
    base_query: str,
    start_date: date,
    end_date: date,
    seen_repo_ids: set[int],
) -> int:
    query = _compose_query(base_query, start_date, end_date)

    if _has_size_qualifier(query):
        logger.info("Base query already contains size qualifier; chunking by size disabled.")
        return _discover_single_query(session, client, query, seen_repo_ids)

    return _discover_with_size_chunks(
        session=session,
        client=client,
        base_query=query,
        min_size=SIZE_RANGE_START,
        max_size=SIZE_RANGE_END,
        seen_repo_ids=seen_repo_ids,
    )


def _discover_single_query(
    session: Session,
    client: GitHubClient,
    query: str,
    seen_repo_ids: set[int],
) -> int:
    search_results = client.search_code(query)
    total_count = getattr(search_results, "totalCount", 0) or 0
    if total_count > MAX_CODE_SEARCH_RESULTS:
        logger.warning(
            "Query '%s' returned %s code matches. Processing only first %s due to API limit.",
            query,
            total_count,
            MAX_CODE_SEARCH_RESULTS,
        )

    max_pages = min(MAX_CODE_SEARCH_RESULTS // PAGE_SIZE, max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE))
    discovered = 0
    page_index = 0
    while page_index < max_pages:
        # Mandatory pacing to reduce GitHub abuse/secondary rate-limit triggers.
        time.sleep(2)
        try:
            page_items = list(search_results.get_page(page_index))
        except Exception as exc:
            exc_text = str(exc).lower()
            if (
                "403" in exc_text
                or "rate limit" in exc_text
                or "forbidden" in exc_text
                or _is_rate_limit_error(exc)
            ):
                logger.warning(
                    "GitHub Rate Limit/Abuse atingido na página %s. Aguardando 60s...",
                    page_index,
                )
                time.sleep(60)
                continue
            logger.error("Erro fatal na página %s: %s", page_index, exc)
            raise

        if not page_items:
            break

        for item in page_items:
            repository = getattr(item, "repository", None)
            if repository is None:
                continue

            repo_id = int(repository.id)
            if repo_id in seen_repo_ids:
                continue

            seen_repo_ids.add(repo_id)
            _upsert_pending_repository(session, repo_id, repository.full_name)
            discovered += 1

        session.commit()
        page_index += 1

    return discovered


def _discover_with_size_chunks(
    session: Session,
    client: GitHubClient,
    base_query: str,
    min_size: int,
    max_size: int,
    seen_repo_ids: set[int],
) -> int:
    if min_size > max_size:
        return 0

    query = _append_size_qualifier(base_query, min_size, max_size)

    # === BLOCO DEFENSIVO INÍCIO ===
    total_count = 0
    while True:
        try:
            # Pausa profilática para evitar estourar as 30 req/min da Search API
            time.sleep(2.5)

            search_results = client.search_code(query)
            # A requisição real à API acontece AQUI:
            total_count = getattr(search_results, "totalCount", 0) or 0
            break  # Sucesso, sai do loop defensivo

        except Exception as e:
            if "403" in str(e):
                logger.warning(
                    "Limite secundário atingido avaliando chunk %s..%s. Aguardando 60s...",
                    min_size,
                    max_size,
                )
                time.sleep(60)
                continue  # Tenta fazer a leitura do totalCount novamente
            else:
                # Outros erros (ex: falha de rede) devem ser repassados
                logger.error("Erro inesperado ao buscar chunk %s..%s: %s", min_size, max_size, e)
                raise
    # === BLOCO DEFENSIVO FIM ===
    logger.info(
        "Chunk %s..%s retornou %s matches de código.",
        min_size,
        max_size,
        total_count,
    )
    if total_count < MAX_CODE_SEARCH_RESULTS or min_size >= max_size:
        if total_count > MAX_CODE_SEARCH_RESULTS:
            logger.warning(
                "Single-size range %s..%s still has %s matches; processing only first %s.",
                min_size,
                max_size,
                total_count,
                MAX_CODE_SEARCH_RESULTS,
            )
        return _discover_single_query(session, client, query, seen_repo_ids)

    midpoint = (min_size + max_size) // 2
    if midpoint <= min_size:
        return _discover_single_query(session, client, query, seen_repo_ids)

    logger.info(
        "Splitting size range %s..%s (%s matches) into %s..%s and %s..%s",
        min_size,
        max_size,
        total_count,
        min_size,
        midpoint,
        midpoint + 1,
        max_size,
    )
    discovered = 0
    discovered += _discover_with_size_chunks(
        session=session,
        client=client,
        base_query=base_query,
        min_size=min_size,
        max_size=midpoint,
        seen_repo_ids=seen_repo_ids,
    )
    discovered += _discover_with_size_chunks(
        session=session,
        client=client,
        base_query=base_query,
        min_size=midpoint + 1,
        max_size=max_size,
        seen_repo_ids=seen_repo_ids,
    )
    return discovered


def discover_repositories(
    settings: Settings,
    start_date: date,
    end_date: date,
    base_query: str,
    session_factory: sessionmaker[Session] | None = None,
    client: GitHubClient | None = None,
) -> DiscoveryResult:
    session_factory = session_factory or get_session_factory(settings)
    github_client = client or GitHubClient(settings)
    seen_repo_ids: set[int] = set()

    with session_factory() as session:
        if start_date != end_date:
            logger.info(
                "Date window %s..%s received, but code search currently ignores date filters.",
                start_date,
                end_date,
            )
        discovered_count = _discover_window(
            session=session,
            client=github_client,
            base_query=base_query,
            start_date=start_date,
            end_date=end_date,
            seen_repo_ids=seen_repo_ids,
        )
        session.commit()

    logger.info("Discovered %s repositories via GitHub Search API", discovered_count)
    return DiscoveryResult(repositories_discovered=discovered_count)