from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from github import GithubException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.config import Settings
from agm_miner.core.github_client import GitHubClient
from agm_miner.database.models import (
    AgentArtifact,
    ArtifactCommit,
    ArtifactType,
    Author,
    AuthorType,
    Repository,
    RepositoryStatus,
)
from agm_miner.database.session import get_session_factory


logger = logging.getLogger(__name__)
MIN_STARS_THRESHOLD = 50
MAX_COMMITS_PER_FILE = 50


def is_markdown_file(path: str) -> bool:
    return path.lower().endswith(".md")


def count_words(content: str | None) -> int:
    if not content:
        return 0
    return len(re.findall(r"\\b\\w+\\b", content, flags=re.UNICODE))


def should_skip_raw_content(content: str | None, max_bytes: int = 2 * 1024 * 1024) -> bool:
    if content is None:
        return True
    return len(content.encode("utf-8")) > max_bytes


@dataclass(slots=True)
class MiningResult:
    repositories_completed: int = 0
    artifacts_saved: int = 0
    commits_saved: int = 0


def _iter_content_files(client: GitHubClient, full_name: str, path: str = "") -> list[Any]:
    try:
        contents = client.get_contents(full_name, path)
    except GithubException as exc:
        if exc.status == 404:
            return []
        raise

    if not isinstance(contents, list):
        return [contents]

    return contents


def _discover_target_files(repo: Any) -> list[str]:
    tree = repo.get_git_tree(repo.default_branch, recursive=True)

    return sorted(
        {
            element.path
            for element in getattr(tree, "tree", []) or []
            if getattr(element, "type", None) == "blob"
            and (
                element.path.upper().endswith("CLAUDE.MD")
                or element.path.upper().endswith("AGENTS.MD")
                or ".claude/skills/" in element.path.lower()
            )
        }
    )


def _decoded_text(content_file: Any) -> str | None:
    raw_bytes = getattr(content_file, "decoded_content", None)
    if not raw_bytes:
        return None
    return raw_bytes.decode("utf-8", errors="ignore")


def _record_author(
    author_totals: dict[tuple[str, int], dict[str, Any]],
    login: str,
    repo_id: int,
    author_type: AuthorType,
) -> None:
    key = (login, repo_id)
    current = author_totals.get(key)
    if current is None:
        author_totals[key] = {
            "login": login,
            "repo_id": repo_id,
            "total_commits_in_repo": 1,
            "author_type": author_type,
        }
        return

    current["total_commits_in_repo"] += 1
    if current["author_type"] == AuthorType.UNKNOWN and author_type != AuthorType.UNKNOWN:
        current["author_type"] = author_type


def _persist_authors(session: Session, author_totals: dict[tuple[str, int], dict[str, Any]]) -> int:
    if not author_totals:
        return 0

    values = list(author_totals.values())
    stmt = insert(Author).values(values)
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=["login", "repo_id"],
        set_={
            "total_commits_in_repo": Author.total_commits_in_repo + stmt.excluded.total_commits_in_repo,
            "author_type": stmt.excluded.author_type,
        },
    )
    session.execute(upsert_stmt)
    return len(values)


def _upsert_artifact(
    session: Session,
    repo_id: int,
    file_path: str,
    artifact_type: ArtifactType,
    raw_content: str | None,
    size_bytes: int,
) -> tuple[AgentArtifact, bool]:
    artifact = session.execute(
        select(AgentArtifact).where(
            AgentArtifact.repo_id == repo_id,
            AgentArtifact.file_path == file_path,
        )
    ).scalar_one_or_none()
    if artifact is None:
        artifact = AgentArtifact(
            repo_id=repo_id,
            file_path=file_path,
            artifact_type=artifact_type,
            raw_content=raw_content,
            size_bytes=size_bytes,
            word_count=count_words(raw_content),
            readability_score=None,
        )
        session.add(artifact)
        session.flush()
        return artifact, True

    artifact.artifact_type = artifact_type
    artifact.raw_content = raw_content
    artifact.size_bytes = size_bytes
    artifact.word_count = count_words(raw_content)
    return artifact, False


def _save_commit_history(
    session: Session,
    client: GitHubClient,
    repository: Repository,
    file_path: str,
    artifact: AgentArtifact,
    author_totals: dict[tuple[str, int], dict[str, Any]],
    pending_commits: dict[str, dict[str, Any]],
) -> int:
    saved_count = 0
    previous_authored_at: datetime | None = None

    existing_hashes = set(
        session.execute(
            select(ArtifactCommit.commit_hash).where(ArtifactCommit.artifact_id == artifact.id)
        ).scalars().all()
    )

    commit_history = client.get_commits(repository.full_name, file_path)
    processed_commits_in_loop = 0

    for commit in commit_history:
        if processed_commits_in_loop >= MAX_COMMITS_PER_FILE:
            logger.warning(
                "Limite de %s commits atingido para %s no repo %s. Interrompendo histórico.",
                MAX_COMMITS_PER_FILE,
                file_path,
                repository.full_name,
            )
            break

        commit_sha = getattr(commit, "sha", None)
        if not commit_sha:
            continue

        if commit_sha in existing_hashes or commit_sha in pending_commits:
            continue

        try:
            detailed_commit = client.get_commit(repository.full_name, commit_sha)
        except Exception as exc:
            logger.error("Falha ao buscar detalhes do commit %s: %s", commit_sha, exc)
            continue

        commit_author = getattr(commit, "author", None)
        commit_author_name = getattr(getattr(commit, "commit", None), "author", None)
        author_login = (
            getattr(commit_author, "login", None)
            or getattr(commit_author_name, "name", None)
            or f"unknown-{commit_sha[:12]}"
        )
        author_type = AuthorType.CORE_DEV if commit_author else AuthorType.UNKNOWN
        _record_author(author_totals, author_login, repository.id, author_type)

        authored_at = getattr(commit_author_name, "date", None)
        if authored_at is None:
            authored_at = datetime.now(UTC)

        if previous_authored_at is None:
            time_since_last_update_hours = 0
        else:
            time_since_last_update_hours = max(
                0,
                int((previous_authored_at - authored_at).total_seconds() // 3600),
            )

        file_patch = None
        additions = getattr(getattr(commit, "stats", None), "additions", 0) or 0
        deletions = getattr(getattr(commit, "stats", None), "deletions", 0) or 0
        for changed_file in getattr(detailed_commit, "files", []) or []:
            if getattr(changed_file, "filename", None) == file_path:
                file_patch = getattr(changed_file, "patch", None)
                additions = getattr(changed_file, "additions", additions) or additions
                deletions = getattr(changed_file, "deletions", deletions) or deletions
                break

        if commit_sha not in pending_commits:
            pending_commits[commit_sha] = {
                "commit_hash": commit_sha,
                "artifact_id": artifact.id,
                "repo_id": repository.id,
                "author_login": author_login,
                "authored_at": authored_at,
                "commit_message": getattr(getattr(commit, "commit", None), "message", None),
                "additions": additions,
                "deletions": deletions,
                "patch_diff": file_patch,
                "time_since_last_update_hours": time_since_last_update_hours,
            }
            saved_count += 1
            processed_commits_in_loop += 1
        previous_authored_at = authored_at

    return saved_count


def mine_pending_repositories(
    settings: Settings,
    session_factory: sessionmaker[Session] | None = None,
    client: GitHubClient | None = None,
) -> MiningResult:
    session_factory = session_factory or get_session_factory(settings)
    github_client = client or GitHubClient(settings)

    repositories_completed = 0
    artifacts_saved = 0
    commits_saved = 0

    with session_factory() as session:
        pending_repositories = session.execute(
            select(Repository).where(Repository.status == RepositoryStatus.PENDING)
        ).scalars().all()

        for repository in pending_repositories:
            logger.info("Mining repository: %s", repository.full_name)
            repo_name = repository.full_name
            
            try:
                gh_repo = github_client.get_repository(repo_name)
                repository.stars = getattr(gh_repo, "stargazers_count", 0) or 0
                logger.info(1)
                if repository.stars < MIN_STARS_THRESHOLD:
                    logger.info(
                        "Skipping repository %s due to stars=%s below threshold %s.",
                        repo_name,
                        repository.stars,
                        MIN_STARS_THRESHOLD,
                    )
                    logger.info(2)
                    repository.status = RepositoryStatus.COMPLETED
                    repositories_completed += 1
                    session.commit()
                    continue
                logger.info(3)    
                repository.forks = getattr(gh_repo, "forks_count", 0) or 0
                repository.total_commits = 0
                repository.contributors_count = 0

                target_files = _discover_target_files(gh_repo)
                repository.has_md_instructions = any(
                    path.upper().endswith("CLAUDE.MD") or path.upper().endswith("AGENTS.MD")
                    for path in target_files
                )
                repository.has_skill_files = any(
                    path.lower().startswith(".claude/skills/") for path in target_files
                )
                logger.info(4)
                for file_path in target_files:
                    author_totals: dict[tuple[str, int], dict[str, Any]] = {}
                    pending_commits: dict[str, dict[str, Any]] = {}
                    logger.info(5)
                    content_file = github_client.get_contents(repo_name, file_path)
                    if isinstance(content_file, list) or not is_markdown_file(file_path):
                        continue

                    size_bytes = int(getattr(content_file, "size", 0) or 0)
                    raw_content = _decoded_text(content_file)
                    if size_bytes > 2 * 1024 * 1024 or should_skip_raw_content(raw_content):
                        logger.warning("Skipping raw content for %s in repo %s due to size or decode issues.", file_path, repo_name)
                        raw_content = None
                    logger.info(6)
                    artifact_type = (
                        ArtifactType.ROOT_MD
                        if file_path.upper().endswith("CLAUDE.MD") or file_path.upper().endswith("AGENTS.MD")
                        else ArtifactType.SKILL_FILE
                    )
                    artifact, artifact_created = _upsert_artifact(
                        session=session,
                        repo_id=repository.id,
                        file_path=file_path,
                        artifact_type=artifact_type,
                        raw_content=raw_content,
                        size_bytes=size_bytes,
                    )
                    if artifact_created:
                        artifacts_saved += 1
                    logger.info(7)
                    commits_saved += _save_commit_history(
                        session=session,
                        client=github_client,
                        repository=repository,
                        file_path=file_path,
                        artifact=artifact,
                        author_totals=author_totals,
                        pending_commits=pending_commits,
                    )
                    
                    _persist_authors(session, author_totals)
                    if pending_commits:
                        stmt = insert(ArtifactCommit).values(list(pending_commits.values()))
                        stmt = stmt.on_conflict_do_nothing(index_elements=["commit_hash"])
                        session.execute(stmt)

                repository.status = RepositoryStatus.COMPLETED
                logger.info(8)
                repositories_completed += 1
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Failed to mine repository %s", repo_name)

    logger.info(
        "Completed mining of %s repositories; saved %s artifacts and %s commits",
        repositories_completed,
        artifacts_saved,
        commits_saved,
    )
    return MiningResult(
        repositories_completed=repositories_completed,
        artifacts_saved=artifacts_saved,
        commits_saved=commits_saved,
    )