"""Stage 2 — fetch repository metadata (GitHub GraphQL) and apply the study's selection criteria.

Criteria come from ``StudyConfig.selection`` and are evaluated in a fixed order; a rejected
repository records the *first* failing criterion, which yields the filtering funnel reported in
the paper (``agm runs funnel``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import SelectionConfig, StudyConfig
from agm_miner.database.models import DiscoveryHit, Repository, RepositoryStatus, RunStage
from agm_miner.github.client import GitHubClient
from agm_miner.runs import RunContext, track_run

logger = logging.getLogger(__name__)

MANUAL_QUERY = "manual:add-repo"
NOT_FOUND = "not_found"

REPOSITORY_FIELDS = """
  id
  databaseId
  nameWithOwner
  createdAt
  pushedAt
  stargazerCount
  forkCount
  diskUsage
  description
  isFork
  isArchived
  isTemplate
  isMirror
  isEmpty
  watchers { totalCount }
  primaryLanguage { name }
  licenseInfo { spdxId }
  repositoryTopics(first: 20) { nodes { topic { name } } }
  defaultBranchRef { name target { ... on Commit { history { totalCount } } } }
"""

NODES_QUERY = (
    "query($ids: [ID!]!) { nodes(ids: $ids) { ... on Repository { " + REPOSITORY_FIELDS + " } } }"
)


class SelectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepositoryMetadata:
    database_id: int
    node_id: str
    full_name: str
    created_at: datetime
    pushed_at: datetime | None
    stars: int
    forks: int
    watchers: int
    disk_usage_kb: int | None
    description: str | None
    primary_language: str | None
    license_spdx: str | None
    topics: tuple[str, ...]
    is_fork: bool
    is_archived: bool
    is_template: bool
    is_mirror: bool
    is_empty: bool
    default_branch: str | None
    commits_count: int | None

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> RepositoryMetadata:
        branch = node.get("defaultBranchRef") or {}
        history = ((branch.get("target") or {}).get("history")) or {}
        return cls(
            database_id=int(node["databaseId"]),
            node_id=node["id"],
            full_name=node["nameWithOwner"],
            created_at=_timestamp(node["createdAt"]),
            pushed_at=_timestamp(node.get("pushedAt")),
            stars=int(node["stargazerCount"]),
            forks=int(node["forkCount"]),
            watchers=int((node.get("watchers") or {}).get("totalCount", 0)),
            disk_usage_kb=node.get("diskUsage"),
            description=node.get("description"),
            primary_language=(node.get("primaryLanguage") or {}).get("name"),
            license_spdx=(node.get("licenseInfo") or {}).get("spdxId"),
            topics=tuple(
                item["topic"]["name"]
                for item in (node.get("repositoryTopics") or {}).get("nodes", [])
            ),
            is_fork=bool(node["isFork"]),
            is_archived=bool(node["isArchived"]),
            is_template=bool(node["isTemplate"]),
            is_mirror=bool(node["isMirror"]),
            is_empty=bool(node["isEmpty"]),
            default_branch=branch.get("name"),
            commits_count=history.get("totalCount"),
        )

    def as_columns(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "default_branch": self.default_branch,
            "gh_created_at": self.created_at,
            "pushed_at": self.pushed_at,
            "stars": self.stars,
            "forks": self.forks,
            "watchers": self.watchers,
            "disk_usage_kb": self.disk_usage_kb,
            "primary_language": self.primary_language,
            "topics": list(self.topics),
            "license_spdx": self.license_spdx,
            "description": self.description,
            "is_fork": self.is_fork,
            "is_archived": self.is_archived,
            "is_template": self.is_template,
            "is_mirror": self.is_mirror,
            "commits_count": self.commits_count,
        }


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- criteria

_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(*texts: str | None) -> set[str]:
    words: set[str] = set()
    for text in texts:
        if text:
            words.update(_WORD_RE.findall(text.lower()))
    return words


def evaluate(metadata: RepositoryMetadata, config: SelectionConfig, reference: date) -> str | None:
    """Return the first failing criterion (the rejection reason) or ``None`` if selected."""
    if metadata.is_empty or metadata.default_branch is None:
        return "empty"
    if config.exclude_forks and metadata.is_fork:
        return "fork"
    if config.exclude_archived and metadata.is_archived:
        return "archived"
    if config.exclude_mirrors and metadata.is_mirror:
        return "mirror"
    if config.exclude_templates and metadata.is_template:
        return "template"
    if metadata.stars < config.min_stars:
        return "min_stars"
    if metadata.commits_count is not None and metadata.commits_count < config.min_commits:
        return "min_commits"
    if config.active_within_days is not None:
        cutoff = reference - timedelta(days=config.active_within_days)
        if metadata.pushed_at is None or metadata.pushed_at.astimezone(UTC).date() < cutoff:
            return "inactive"
    name = metadata.full_name.split("/", 1)[-1]
    words = _words(name, metadata.description, *metadata.topics)
    for keyword in config.exclude_keywords:
        if keyword.lower() in words:
            return f"keyword:{keyword.lower()}"
    return None


def reference_date(config: SelectionConfig) -> date:
    return config.reference_date or datetime.now(UTC).date()


# --------------------------------------------------------------------------- stage


def select_repositories(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    *,
    reevaluate: bool = False,
    limit: int | None = None,
) -> dict[str, int]:
    """Fetch metadata for DISCOVERED repositories (plus SELECTED/REJECTED with ``reevaluate``)
    and mark each one SELECTED or REJECTED."""
    statuses = [RepositoryStatus.DISCOVERED]
    if reevaluate:
        statuses += [RepositoryStatus.SELECTED, RepositoryStatus.REJECTED]
    reference = reference_date(study.selection)
    params = {"reevaluate": reevaluate, "limit": limit, "reference_date": reference.isoformat()}
    with track_run(session_factory, RunStage.SELECT, study, params) as run:
        with session_factory() as session:
            query = (
                select(Repository.id, Repository.node_id)
                .where(Repository.status.in_(statuses))
                .order_by(Repository.id)
            )
            if limit is not None:
                query = query.limit(limit)
            pending = session.execute(query).all()
        batch_size = study.selection.graphql_batch_size
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            metadata = fetch_metadata(client, [node_id for _, node_id in batch])
            _apply(session_factory, batch, metadata, study.selection, reference, run)
        return dict(run.stats)


def fetch_metadata(client: GitHubClient, node_ids: Sequence[str]) -> dict[str, RepositoryMetadata]:
    """Metadata by node id; ids GitHub does not resolve (deleted/private) are absent."""
    response = client.graphql(NODES_QUERY, {"ids": list(node_ids)})
    data = response.get("data")
    errors = response.get("errors") or []
    unexpected = [error for error in errors if error.get("type") != "NOT_FOUND"]
    if data is None or unexpected:
        raise SelectionError(f"GraphQL error: {unexpected or errors}")
    return {
        node["id"]: RepositoryMetadata.from_graphql(node)
        for node in data.get("nodes") or []
        if node  # null for unresolvable ids
    }


def _apply(
    session_factory: sessionmaker[Session],
    batch: Sequence[Any],
    metadata: dict[str, RepositoryMetadata],
    config: SelectionConfig,
    reference: date,
    run: RunContext,
) -> None:
    fetched_at = datetime.now(UTC)
    with session_factory() as session, session.begin():
        for repo_id, node_id in batch:
            found = metadata.get(node_id)
            if found is None:
                values: dict[str, Any] = {"rejection_reason": NOT_FOUND}
                reason: str | None = NOT_FOUND
            else:
                reason = evaluate(found, config, reference)
                values = found.as_columns() | {"rejection_reason": reason}
            values["status"] = (
                RepositoryStatus.SELECTED if reason is None else RepositoryStatus.REJECTED
            )
            values["metadata_fetched_at"] = fetched_at
            session.execute(update(Repository).where(Repository.id == repo_id).values(**values))
            run.increment("selected" if reason is None else "rejected")
            if reason is not None:
                run.increment(f"rejected:{reason.split(':', 1)[0]}")


# --------------------------------------------------------------------------- manual additions

REPOSITORY_BY_NAME_QUERY = (
    "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { "
    + REPOSITORY_FIELDS
    + " } }"
)


def add_repositories(
    study: StudyConfig,
    session_factory: sessionmaker[Session],
    client: GitHubClient,
    full_names: Sequence[str],
) -> dict[str, int]:
    """Register repositories by name (e.g. for validation runs) and evaluate the criteria.

    They are tagged in ``discovery_hits`` with query ``manual:add-repo`` so analyses can tell
    them apart from the search-based sample.
    """
    reference = reference_date(study.selection)
    with track_run(
        session_factory, RunStage.SELECT, study, {"add_repositories": list(full_names)}
    ) as run:
        for full_name in full_names:
            owner, _, name = full_name.partition("/")
            if not owner or not name:
                raise SelectionError(f"Expected owner/name, got {full_name!r}")
            response = client.graphql(REPOSITORY_BY_NAME_QUERY, {"owner": owner, "name": name})
            node = (response.get("data") or {}).get("repository")
            if node is None:
                raise SelectionError(f"Repository {full_name} not found: {response.get('errors')}")
            metadata = RepositoryMetadata.from_graphql(node)
            reason = evaluate(metadata, study.selection, reference)
            values = metadata.as_columns() | {
                "id": metadata.database_id,
                "node_id": metadata.node_id,
                "status": RepositoryStatus.SELECTED
                if reason is None
                else RepositoryStatus.REJECTED,
                "rejection_reason": reason,
                "metadata_fetched_at": datetime.now(UTC),
            }
            with session_factory() as session, session.begin():
                statement = insert(Repository).values(**values)
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[Repository.id],
                        set_={key: statement.excluded[key] for key in values if key != "id"},
                    )
                )
                session.execute(
                    insert(DiscoveryHit)
                    .values(
                        repo_id=metadata.database_id, query=MANUAL_QUERY, path="", run_id=run.id
                    )
                    .on_conflict_do_nothing()
                )
            run.increment("added")
            logger.info("Added %s (%s)", metadata.full_name, reason or "selected")
        return dict(run.stats)


def funnel(session: Session) -> list[tuple[str, str | None, int]]:
    """Repository counts per status and rejection reason."""
    rows = session.execute(
        select(Repository.status, Repository.rejection_reason, func.count())
        .group_by(Repository.status, Repository.rejection_reason)
        .order_by(Repository.status, func.count().desc())
    ).all()
    return [(status.value, reason, count) for status, reason, count in rows]
