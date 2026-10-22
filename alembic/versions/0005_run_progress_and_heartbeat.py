"""run progress and heartbeat

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "pipeline_runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("pipeline_runs", "heartbeat_at")
    op.drop_column("pipeline_runs", "progress")
