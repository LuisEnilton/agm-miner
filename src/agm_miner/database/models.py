from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ArtifactType(str, enum.Enum):
    ROOT_MD = "ROOT_MD"
    SKILL_FILE = "SKILL_FILE"


class AuthorType(str, enum.Enum):
    CORE_DEV = "CORE_DEV"
    DOC_ONLY = "DOC_ONLY"
    UNKNOWN = "UNKNOWN"

# Considerar Criar estado REJECTED para repositorios que não atendem aos critérios de qualidade do AGM Miner.
class RepositoryStatus(str, enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    status: Mapped[RepositoryStatus] = mapped_column(
        Enum(RepositoryStatus, name="repository_status"), nullable=False, default=RepositoryStatus.PENDING
    )
    stars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    forks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contributors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_commits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_md_instructions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_skill_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )  

    artifacts: Mapped[list["AgentArtifact"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )
    authors: Mapped[list["Author"]] = relationship(back_populates="repository")


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    artifact_type: Mapped[ArtifactType] = mapped_column(Enum(ArtifactType, name="artifact_type"), nullable=False)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    readability_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="artifacts")
    commits: Mapped[list["ArtifactCommit"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )


class Author(Base):
    __tablename__ = "authors"

    login: Mapped[str] = mapped_column(String(255), primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), primary_key=True, index=True)
    total_commits_in_repo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    author_type: Mapped[AuthorType] = mapped_column(
        Enum(AuthorType, name="author_type"), nullable=False
    )

    repository: Mapped[Repository] = relationship(back_populates="authors")
    commits: Mapped[list["ArtifactCommit"]] = relationship(
        back_populates="author",
        foreign_keys="ArtifactCommit.author_login, ArtifactCommit.repo_id",
    )


class ArtifactCommit(Base):
    __tablename__ = "artifact_commits"

    commit_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_artifacts.id"), nullable=False
    )
    author_login: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False, index=True)
    authored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    commit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    additions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    patch_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    time_since_last_update_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    artifact: Mapped[AgentArtifact] = relationship(back_populates="commits")
    author: Mapped[Author] = relationship(
        back_populates="commits",
        foreign_keys=[author_login, repo_id],
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["author_login", "repo_id"],
            ["authors.login", "authors.repo_id"],
        ),
    )
