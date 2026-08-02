"""initial schema

Revision ID: 20260719_0001
Revises:
Create Date: 2026-07-19 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260719_0001"
down_revision = None
branch_labels = None
depends_on = None


repository_status = sa.Enum("PENDING", "COMPLETED", "FAILED", name="repository_status")
artifact_type = sa.Enum("ROOT_MD", "SKILL_FILE", name="artifact_type")
author_type = sa.Enum("CORE_DEV", "DOC_ONLY", "UNKNOWN", name="author_type")

repository_status_ref = postgresql.ENUM(
    "PENDING", "COMPLETED", "FAILED", name="repository_status", create_type=False
)
artifact_type_ref = postgresql.ENUM(
    "ROOT_MD", "SKILL_FILE", name="artifact_type", create_type=False
)
author_type_ref = postgresql.ENUM(
    "CORE_DEV", "DOC_ONLY", "UNKNOWN", name="author_type", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    repository_status.create(bind, checkfirst=True)
    artifact_type.create(bind, checkfirst=True)
    author_type.create(bind, checkfirst=True)

    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("status", repository_status_ref, nullable=False),
        sa.Column("stars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("forks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contributors_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_commits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("has_md_instructions", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_skill_files", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("full_name"),
    )
    op.create_index(op.f("ix_repositories_full_name"), "repositories", ["full_name"], unique=False)

    op.create_table(
        "agent_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("artifact_type", artifact_type_ref, nullable=False),
        sa.Column("raw_content", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("readability_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["repo_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_artifacts_repo_id"), "agent_artifacts", ["repo_id"], unique=False)

    op.create_table(
        "authors",
        sa.Column("login", sa.String(length=255), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("total_commits_in_repo", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("author_type", author_type_ref, nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repositories.id"]),
        sa.PrimaryKeyConstraint("login", "repo_id"),
    )
    op.create_index(op.f("ix_authors_repo_id"), "authors", ["repo_id"], unique=False)

    op.create_table(
        "artifact_commits",
        sa.Column("commit_hash", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_login", sa.String(length=255), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("authored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("commit_message", sa.Text(), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deletions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("patch_diff", sa.Text(), nullable=True),
        sa.Column("time_since_last_update_hours", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["artifact_id"], ["agent_artifacts.id"]),
        sa.ForeignKeyConstraint(["repo_id"], ["repositories.id"]),
        sa.ForeignKeyConstraint(
            ["author_login", "repo_id"],
            ["authors.login", "authors.repo_id"],
        ),
        sa.PrimaryKeyConstraint("commit_hash"),
    )
    op.create_index(op.f("ix_artifact_commits_author_login"), "artifact_commits", ["author_login"], unique=False)
    op.create_index(op.f("ix_artifact_commits_repo_id"), "artifact_commits", ["repo_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_artifact_commits_repo_id"), table_name="artifact_commits")
    op.drop_index(op.f("ix_artifact_commits_author_login"), table_name="artifact_commits")
    op.drop_table("artifact_commits")

    op.drop_index(op.f("ix_authors_repo_id"), table_name="authors")
    op.drop_table("authors")

    op.drop_index(op.f("ix_agent_artifacts_repo_id"), table_name="agent_artifacts")
    op.drop_table("agent_artifacts")

    op.drop_index(op.f("ix_repositories_full_name"), table_name="repositories")
    op.drop_table("repositories")

    bind = op.get_bind()
    author_type.drop(bind, checkfirst=True)
    artifact_type.drop(bind, checkfirst=True)
    repository_status.drop(bind, checkfirst=True)
