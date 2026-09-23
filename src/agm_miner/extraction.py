"""Turn a git clone into the raw facts stored by the miner (no database access here).

Passes over the repository, in order:

1. ``git log`` (compact): author identity of every commit, with the HEAD ``.mailmap`` applied.
2. ``git log --raw --no-renames``: file changes of every non-merge commit. This pass drives the
   contributor profile (commits per file category), monthly activity, and it finds every commit
   that touched an artifact, including artifacts deleted before HEAD. It needs no blobs.
3. ``git ls-tree HEAD``: which artifacts exist at the mined HEAD.
4. Blob prefetch for artifact versions (and rename candidates), in batches.
5. ``git diff-tree -M -C`` on commits that added an artifact: rename/copy lineage
   (e.g. ``CLAUDE.md`` renamed to ``AGENTS.md`` and replaced by a symlink).
6. ``git diff-tree --numstat`` restricted to artifact paths: lines added/removed.
7. Full metadata (message, committer, trailers) of commits that touched artifacts.
8. ``git cat-file --batch``: the content of every artifact version.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from agm_miner.artifacts import GIT_PATHSPECS, ArtifactMatch, classify_included
from agm_miner.config import MiningConfig
from agm_miner.database.models import ChangeType, ContentOmission
from agm_miner.file_categories import FileCategory, categorize
from agm_miner.git.parsing import (
    GITLINK_MODE,
    SYMLINK_MODE,
    RawChange,
    github_login_from_email,
    parse_coauthors,
)
from agm_miner.git.repository import GitRepository

logger = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


# --------------------------------------------------------------------------- records


@dataclass(slots=True)
class IdentityRecord:
    key: str
    name: str
    email: str
    github_login: str | None
    commits: int
    first_commit_at: datetime
    last_commit_at: datetime


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    path: str
    match: ArtifactMatch
    exists_at_head: bool
    head_blob_sha: str | None


@dataclass(frozen=True, slots=True)
class VersionRecord:
    path: str
    blob_sha: str
    file_mode: str
    is_symlink: bool
    size_bytes: int
    content: str | None
    content_omitted: ContentOmission | None


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    path: str
    sha: str
    change_type: ChangeType
    source_path: str | None
    similarity: int | None
    additions: int | None
    deletions: int | None
    blob_sha: str | None
    previous_blob_sha: str | None


@dataclass(frozen=True, slots=True)
class CommitRecord:
    sha: str
    parents: tuple[str, ...]
    identity_key: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    authored_at: datetime
    committed_at: datetime
    message: str
    coauthors: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CommitFileRecord:
    sha: str
    path: str
    change_type: ChangeType
    file_category: FileCategory


@dataclass(slots=True)
class ActivityCounter:
    commits: int = 0
    file_changes: int = 0


@dataclass(slots=True)
class Extraction:
    head_sha: str | None
    identities: dict[str, IdentityRecord] = field(default_factory=dict)
    contributor_activity: dict[tuple[str, FileCategory], ActivityCounter] = field(
        default_factory=dict
    )
    monthly_activity: dict[date, tuple[int, int]] = field(default_factory=dict)
    artifacts: dict[str, ArtifactRecord] = field(default_factory=dict)
    versions: list[VersionRecord] = field(default_factory=list)
    changes: list[ChangeRecord] = field(default_factory=list)
    commits: list[CommitRecord] = field(default_factory=list)
    commit_files: list[CommitFileRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def identity_key(name: str, email: str) -> str:
    normalized_email = email.strip().lower()
    if normalized_email:
        return normalized_email
    return f"name:{name.strip().lower()}"


def _month(moment: datetime) -> date:
    utc = moment.astimezone(UTC)
    return date(utc.year, utc.month, 1)


def decode_content(data: bytes, max_bytes: int) -> tuple[str | None, ContentOmission | None]:
    if len(data) > max_bytes:
        return None, ContentOmission.TOO_LARGE
    if b"\0" in data[:8192]:
        return None, ContentOmission.BINARY
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, ContentOmission.NOT_UTF8


@dataclass(slots=True)
class _AuthorStats:
    names: Counter[str] = field(default_factory=Counter)
    email: str = ""
    commits: int = 0
    first: datetime | None = None
    last: datetime | None = None


# --------------------------------------------------------------------------- extraction


def extract_repository(repo: GitRepository, config: MiningConfig) -> Extraction:
    head_sha = repo.head_sha()
    extraction = Extraction(head_sha=head_sha)
    if head_sha is None:
        return extraction

    excludes = tuple(config.exclude_path_globs)

    def classify(path: str) -> ArtifactMatch | None:
        return classify_included(path, excludes)

    # 1. authors -------------------------------------------------------------------------
    repo.enable_mailmap(head_sha, config.prefetch_batch_size)
    author_of: dict[str, str] = {}
    merges: set[str] = set()
    stats: dict[str, _AuthorStats] = defaultdict(_AuthorStats)
    monthly_commits: Counter[date] = Counter()
    monthly_authors: dict[date, set[str]] = defaultdict(set)
    for commit in repo.iter_compact_log():
        key = identity_key(commit.author_name, commit.author_email)
        author_of[commit.sha] = key
        if commit.is_merge:
            merges.add(commit.sha)
            continue
        author = stats[key]
        author.names[commit.author_name] += 1
        author.email = commit.author_email
        author.commits += 1
        author.first = (
            commit.authored_at if author.first is None else min(author.first, commit.authored_at)
        )
        author.last = (
            commit.authored_at if author.last is None else max(author.last, commit.authored_at)
        )
        month = _month(commit.authored_at)
        monthly_commits[month] += 1
        monthly_authors[month].add(key)

    for key, author in stats.items():
        name = author.names.most_common(1)[0][0]
        extraction.identities[key] = IdentityRecord(
            key=key,
            name=name,
            email=author.email,
            github_login=github_login_from_email(author.email),
            commits=author.commits,
            first_commit_at=author.first,
            last_commit_at=author.last,
        )
    extraction.monthly_activity = {
        month: (monthly_commits[month], len(monthly_authors[month])) for month in monthly_commits
    }

    # 2. raw changes -----------------------------------------------------------------------
    artifact_changes_by_commit: dict[str, list[RawChange]] = {}
    all_changes_by_commit: dict[str, list[RawChange]] = {}
    commit_order: list[str] = []
    for diff in repo.iter_raw_log():
        if diff.sha in merges or not diff.changes:
            continue
        key = author_of[diff.sha]
        categories = Counter(categorize(change.path, excludes) for change in diff.changes)
        for category, count in categories.items():
            counter = extraction.contributor_activity.setdefault((key, category), ActivityCounter())
            counter.commits += 1
            counter.file_changes += count

        if FileCategory.AGENT_ARTIFACT not in categories:
            continue
        commit_order.append(diff.sha)
        all_changes_by_commit[diff.sha] = diff.changes
        artifact_changes_by_commit[diff.sha] = [
            change for change in diff.changes if classify(change.path) is not None
        ]
        for change in diff.changes:
            extraction.commit_files.append(
                CommitFileRecord(
                    sha=diff.sha,
                    path=change.path,
                    change_type=ChangeType(change.status),
                    file_category=categorize(change.path, excludes),
                )
            )

    # 3. artifacts at HEAD -----------------------------------------------------------------
    head_entries = {
        entry.path: entry
        for entry in repo.ls_tree(head_sha)
        if entry.object_type == "blob" and classify(entry.path) is not None
    }

    artifact_paths = set(head_entries)
    for changes in artifact_changes_by_commit.values():
        artifact_paths.update(change.path for change in changes)
    for path in sorted(artifact_paths):
        head_entry = head_entries.get(path)
        extraction.artifacts[path] = ArtifactRecord(
            path=path,
            match=classify(path),
            exists_at_head=head_entry is not None,
            head_blob_sha=head_entry.sha if head_entry is not None else None,
        )

    # Versions to store: every blob an artifact path ever pointed to.
    version_modes: dict[tuple[str, str], str] = {}
    for changes in artifact_changes_by_commit.values():
        for change in changes:
            if change.old_blob_or_none and change.old_mode != GITLINK_MODE:
                version_modes.setdefault((change.path, change.old_blob), change.old_mode)
            if change.new_blob_or_none and change.new_mode != GITLINK_MODE:
                version_modes.setdefault((change.path, change.new_blob), change.new_mode)
    for path, entry in head_entries.items():
        version_modes.setdefault((path, entry.sha), entry.mode)

    # 4-5. rename / copy lineage -------------------------------------------------------------
    rename_commits, exact_renames, candidate_blobs = _rename_candidates(
        all_changes_by_commit, artifact_changes_by_commit, config.rename_candidate_limit
    )
    repo.prefetch_blobs(
        {blob for _, blob in version_modes} | candidate_blobs, config.prefetch_batch_size
    )
    lineage: dict[tuple[str, str], RawChange] = dict(exact_renames)
    if rename_commits:
        for diff in repo.iter_diff_tree(
            rename_commits, detect_renames=True, raw=True, numstat=False
        ):
            added = _added_paths(artifact_changes_by_commit.get(diff.sha, []))
            for change in diff.changes:
                if change.status in ("R", "C") and change.path in added:
                    lineage[(diff.sha, change.path)] = change

    # 6. numstat of artifact paths -------------------------------------------------------------
    numstat: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for diff in repo.iter_diff_tree(
        commit_order, detect_renames=False, raw=False, numstat=True, pathspecs=GIT_PATHSPECS
    ):
        for entry in diff.numstat:
            numstat[(diff.sha, entry.path)] = (entry.additions, entry.deletions)

    # 7. full commit metadata ------------------------------------------------------------------
    for commit in repo.iter_full_commits(commit_order):
        extraction.commits.append(
            CommitRecord(
                sha=commit.sha,
                parents=commit.parents,
                identity_key=author_of[commit.sha],
                author_name=commit.raw_author_name,
                author_email=commit.raw_author_email,
                committer_name=commit.committer_name,
                committer_email=commit.committer_email,
                authored_at=commit.authored_at,
                committed_at=commit.committed_at,
                message=commit.message.rstrip("\n"),
                coauthors=tuple(parse_coauthors(commit.message)),
            )
        )
    if len(extraction.commits) != len(commit_order):
        raise ExtractionError(
            f"Expected metadata for {len(commit_order)} commits, got {len(extraction.commits)}"
        )

    # Change records ---------------------------------------------------------------------------
    for sha in commit_order:
        for change in artifact_changes_by_commit[sha]:
            additions, deletions = numstat.get((sha, change.path), (None, None))
            change_type = ChangeType(change.status)
            source_path = None
            similarity = None
            previous_blob = change.old_blob_or_none
            renamed = lineage.get((sha, change.path))
            if renamed is not None:
                change_type = ChangeType(renamed.status)
                source_path = renamed.source_path
                similarity = renamed.similarity
                previous_blob = renamed.old_blob_or_none
                if renamed.old_blob == renamed.new_blob:
                    additions, deletions = 0, 0
                else:
                    additions, deletions = repo.blob_numstat(renamed.old_blob, renamed.new_blob)
            extraction.changes.append(
                ChangeRecord(
                    path=change.path,
                    sha=sha,
                    change_type=change_type,
                    source_path=source_path,
                    similarity=similarity,
                    additions=additions,
                    deletions=deletions,
                    blob_sha=change.new_blob_or_none,
                    previous_blob_sha=previous_blob,
                )
            )

    # 8. contents ------------------------------------------------------------------------------
    blobs_needed = sorted({blob for _, blob in version_modes})
    contents = {blob.sha: blob for blob in repo.read_blobs(blobs_needed)}
    for (path, blob_sha), mode in sorted(version_modes.items()):
        blob = contents.get(blob_sha)
        if blob is None or blob.data is None:
            raise ExtractionError(f"Blob {blob_sha} of {path} is missing from the clone")
        content, omitted = decode_content(blob.data, config.max_content_bytes)
        extraction.versions.append(
            VersionRecord(
                path=path,
                blob_sha=blob_sha,
                file_mode=mode,
                is_symlink=mode == SYMLINK_MODE,
                size_bytes=blob.size,
                content=content,
                content_omitted=omitted,
            )
        )
    return extraction


def _added_paths(changes: list[RawChange]) -> set[str]:
    return {change.path for change in changes if change.status == "A"}


def _rename_candidates(
    all_changes: dict[str, list[RawChange]],
    artifact_changes: dict[str, list[RawChange]],
    candidate_limit: int,
) -> tuple[list[str], dict[tuple[str, str], RawChange], set[str]]:
    """Plan rename/copy detection for commits that added an artifact.

    Returns the commits to analyse with ``diff-tree -M -C``, the exact (same blob) renames found
    directly in commits too large for full detection, and the blobs full detection will read.
    """
    detect: list[str] = []
    exact: dict[tuple[str, str], RawChange] = {}
    blobs: set[str] = set()
    for sha, changes in artifact_changes.items():
        added_artifacts = [change for change in changes if change.status == "A"]
        if not added_artifacts:
            continue
        everything = all_changes[sha]
        sources = [
            change
            for change in everything
            if change.status in ("D", "M", "T")
            and change.old_blob_or_none
            and change.old_mode != GITLINK_MODE
        ]
        targets = [
            change
            for change in everything
            if change.status == "A" and change.new_mode != GITLINK_MODE
        ]
        if len(sources) + len(targets) <= candidate_limit:
            detect.append(sha)
            blobs.update(change.old_blob for change in sources)
            blobs.update(change.new_blob for change in targets)
            continue
        # Too many candidates: keep only exact matches; a deleted source is a rename and wins
        # over a modified one (copy).
        by_blob: dict[str, RawChange] = {}
        for source in sorted(sources, key=lambda change: change.status == "D"):
            by_blob[source.old_blob] = source
        for target in added_artifacts:
            source = by_blob.get(target.new_blob)
            if source is None:
                continue
            exact[(sha, target.path)] = RawChange(
                old_mode=source.old_mode,
                new_mode=target.new_mode,
                old_blob=source.old_blob,
                new_blob=target.new_blob,
                status="R" if source.status == "D" else "C",
                similarity=100,
                path=target.path,
                source_path=source.path,
            )
    return detect, exact, blobs
