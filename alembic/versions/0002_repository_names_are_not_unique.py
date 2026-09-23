"""repository names are not unique

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22 16:21:45.457025
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_repositories_full_name"), "repositories", type_="unique")
    op.create_index(op.f("ix_repositories_full_name"), "repositories", ["full_name"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_repositories_full_name"), table_name="repositories")
    op.create_unique_constraint(
        op.f("uq_repositories_full_name"),
        "repositories",
        ["full_name"],
        postgresql_nulls_not_distinct=False,
    )
