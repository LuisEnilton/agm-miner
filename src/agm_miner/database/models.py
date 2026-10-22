"""ORM models: the single source of truth for the database schema.

The miner stores raw, re-derivable facts only (repository metadata, commits, file changes and the
content of every artifact version). Derived measures such as readability, section structure or
author roles are computed by the ``analyze`` stage and never stored here.

Enumerations are stored as ``VARCHAR`` + ``CHECK`` constraints (``native_enum=False``) so adding a
value is a plain migration instead of an ``ALTER TYPE``.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from agm_miner.artifacts import ArtifactKind, ArtifactScope, ArtifactTool
from agm_miner.file_categories import FileCategory

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class RunStage(StrEnum):
    DISCOVER = "DISCOVER"
    SELECT = "SELECT"
    MINE = "MINE"
    EXPORT = "EXPORT"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"  # stopped by the user (Ctrl+C / SIGTERM)


class SliceOutcome(StrEnum):
    SPLIT = "SPLIT"  # too many matches; its two halves were searched instead
    COLLECTED = "COLLECTED"  # every reachable page was stored


class RepositoryStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    MINED = "MINED"
    FAILED = "FAILED"


class ChangeType(StrEnum):
    ADDED = "A"
    MODIFIED = "M"
    DELETED = "D"
    RENAMED = "R"
    COPIED = "C"
    TYPE_CHANGED = "T"


class ContentOmission(StrEnum):
    TOO_LARGE = "TOO_LARGE"
    BINARY = "BINARY"
    NOT_UTF8 = "NOT_UTF8"


# --------------------------------------------------------------------------- provenance


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stage: Mapped[RunStage] = mapped_column(_enum(RunStage, "run_stage"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus, "run_status"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    code_version: Mapped[str] = mapped_column(String(64), nullable=False)
    study_config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    study_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    # Operational state written periodically while the run is alive (see runs.track_run):
    # the live progress snapshot and the last time the process gave a sign of life.
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DiscoverySlice(Base):
    """Progress of the size bisection, so an interrupted discovery resumes where it stopped."""

    __tablename__ = "discovery_slices"

    # The exact query sent to the Search API (base query + size qualifier).
    slice_query: Mapped[str] = mapped_column(String(600), primary_key=True)
    base_query: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    outcome: Mapped[SliceOutcome] = mapped_column(
        _enum(SliceOutcome, "slice_outcome"), nullable=False
    )
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DiscoveryHit(Base):
    """Which search query found which file of which repository (sampling provenance)."""

    __tablename__ = "discovery_hits"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    query: Mapped[str] = mapped_column(String(512), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# --------------------------------------------------------------------------- repositories


class Repository(Base):
    __tablename__ = "repositories"

    # GitHub's numeric repository id (stable across renames).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    # Not unique: after a rename or deletion GitHub can give the same name to another
    # repository; the numeric id (and node_id) is the identity.
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # GraphQL global node id (lets the selection stage fetch metadata even after renames).
    node_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    status: Mapped[RepositoryStatus] = mapped_column(
        _enum(RepositoryStatus, "repository_status"),
        nullable=False,
        default=RepositoryStatus.DISCOVERED,
        index=True,
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Metadata snapshot taken by the selection stage (GitHub GraphQL).
    metadata_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    default_branch: Mapped[str | None] = mapped_column(String(255))
    gh_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stars: Mapped[int | None] = mapped_column(Integer)
    forks: Mapped[int | None] = mapped_column(Integer)
    watchers: Mapped[int | None] = mapped_column(Integer)
    disk_usage_kb: Mapped[int | None] = mapped_column(Integer)
    primary_language: Mapped[str | None] = mapped_column(String(128))
    topics: Mapped[list[str] | None] = mapped_column(ARRAY(String(128)))
    license_spdx: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    is_fork: Mapped[bool | None] = mapped_column(Boolean)
    is_archived: Mapped[bool | None] = mapped_column(Boolean)
    is_template: Mapped[bool | None] = mapped_column(Boolean)
    is_mirror: Mapped[bool | None] = mapped_column(Boolean)
    commits_count: Mapped[int | None] = mapped_column(Integer)
    # Commit date of the default branch head (the activity criterion).
    last_commit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Mining bookkeeping.
    mined_head_sha: Mapped[str | None] = mapped_column(String(64))
    mined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mined_run_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_runs.id"))

    artifacts: Mapped[list[Artifact]] = relationship(back_populates="repository")


class RepositoryActivity(Base):
    """Monthly commit activity of the whole default branch (normalises artifact change rates)."""

    __tablename__ = "repository_activity"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    commits: Mapped[int] = mapped_column(Integer, nullable=False)
    authors: Mapped[int] = mapped_column(Integer, nullable=False)


# --------------------------------------------------------------------------- people


class Identity(Base):
    """A contributor within one repository, keyed by (mailmap-normalised) e-mail."""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("repo_id", "identity_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str] = mapped_column(String(512), nullable=False)
    github_login: Mapped[str | None] = mapped_column(String(255))
    commits: Mapped[int] = mapped_column(Integer, nullable=False)
    first_commit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_commit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ContributorActivity(Base):
    """How many commits / file changes each identity made per file category (whole history)."""

    __tablename__ = "contributor_activity"

    identity_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE"), primary_key=True
    )
    file_category: Mapped[FileCategory] = mapped_column(
        _enum(FileCategory, "file_category"), primary_key=True
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    commits: Mapped[int] = mapped_column(Integer, nullable=False)
    file_changes: Mapped[int] = mapped_column(Integer, nullable=False)


# --------------------------------------------------------------------------- artifacts


class Artifact(Base):
    """One artifact path in one repository. Renames/copies link paths via ArtifactChange."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("repo_id", "path"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    tool: Mapped[ArtifactTool] = mapped_column(_enum(ArtifactTool, "artifact_tool"), nullable=False)
    kind: Mapped[ArtifactKind] = mapped_column(_enum(ArtifactKind, "artifact_kind"), nullable=False)
    scope: Mapped[ArtifactScope] = mapped_column(
        _enum(ArtifactScope, "artifact_scope"), nullable=False
    )
    skill_name: Mapped[str | None] = mapped_column(String(255))
    exists_at_head: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Blob at the mined HEAD (always stored as an ArtifactVersion); NULL if deleted.
    head_blob_sha: Mapped[str | None] = mapped_column(String(64))

    repository: Mapped[Repository] = relationship(back_populates="artifacts")


class ArtifactVersion(Base):
    """A distinct content (git blob) an artifact path had at some point."""

    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "blob_sha"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), nullable=False
    )
    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    file_mode: Mapped[str] = mapped_column(String(6), nullable=False)
    is_symlink: Mapped[bool] = mapped_column(Boolean, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_omitted: Mapped[ContentOmission | None] = mapped_column(
        _enum(ContentOmission, "content_omission")
    )


# --------------------------------------------------------------------------- commits


class Commit(Base):
    """A non-merge commit of the default branch that touched at least one artifact."""

    __tablename__ = "commits"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Identity resolved with the repository's .mailmap; the raw values are kept alongside.
    author_identity_id: Mapped[int] = mapped_column(
        ForeignKey("identities.id"), nullable=False, index=True
    )
    author_name: Mapped[str] = mapped_column(String(512), nullable=False)
    author_email: Mapped[str] = mapped_column(String(512), nullable=False)
    committer_name: Mapped[str] = mapped_column(String(512), nullable=False)
    committer_email: Mapped[str] = mapped_column(String(512), nullable=False)
    authored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    parents: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)


class CommitCoauthor(Base):
    """``Co-authored-by`` trailers (e.g. commits co-written by coding agents)."""

    __tablename__ = "commit_coauthors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repo_id", "sha"], ["commits.repo_id", "commits.sha"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    email: Mapped[str] = mapped_column(String(512), nullable=False)


class CommitFile(Base):
    """Every file changed by a commit that touched an artifact (co-change / trigger analysis).

    Recorded without rename detection: a renamed file appears as a deletion plus an addition.
    """

    __tablename__ = "commit_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repo_id", "sha"], ["commits.repo_id", "commits.sha"], ondelete="CASCADE"
        ),
    )

    repo_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    change_type: Mapped[ChangeType] = mapped_column(
        _enum(ChangeType, "change_type"), nullable=False
    )
    file_category: Mapped[FileCategory] = mapped_column(
        _enum(FileCategory, "file_category"), nullable=False
    )


class ArtifactChange(Base):
    """One commit's change to one artifact path."""

    __tablename__ = "artifact_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repo_id", "sha"], ["commits.repo_id", "commits.sha"], ondelete="CASCADE"
        ),
        Index("ix_artifact_changes_repo_sha", "repo_id", "sha"),
    )

    artifact_id: Mapped[int] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), primary_key=True
    )
    sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    change_type: Mapped[ChangeType] = mapped_column(
        _enum(ChangeType, "change_type"), nullable=False
    )
    # Source path for renames/copies (may or may not be an artifact itself).
    source_path: Mapped[str | None] = mapped_column(String(1024))
    similarity: Mapped[int | None] = mapped_column(Integer)
    # NULL for binary files.
    additions: Mapped[int | None] = mapped_column(Integer)
    deletions: Mapped[int | None] = mapped_column(Integer)
    # Blob after / before the change; NULL when the path was deleted / did not exist before.
    # For renames and copies the previous blob belongs to ``source_path``.
    blob_sha: Mapped[str | None] = mapped_column(String(64))
    previous_blob_sha: Mapped[str | None] = mapped_column(String(64))
    # This artifact's version after the change (NULL for deletions).
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("artifact_versions.id", ondelete="CASCADE")
    )
