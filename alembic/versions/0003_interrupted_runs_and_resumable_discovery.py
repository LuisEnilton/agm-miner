"""interrupted runs and resumable discovery

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22 16:50:36.848320
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_RUN_STATUS_CHECK = "ck_pipeline_runs_run_status"


def _replace_run_status_check(values: tuple[str, ...]) -> None:
    # Autogenerate does not see enum CHECK constraints (native_enum=False) changing.
    op.drop_constraint(op.f(_RUN_STATUS_CHECK), "pipeline_runs", type_="check")
    allowed = ", ".join(f"'{value}'" for value in values)
    op.create_check_constraint(op.f(_RUN_STATUS_CHECK), "pipeline_runs", f"status IN ({allowed})")


def upgrade() -> None:
    _replace_run_status_check(("RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"))
    op.create_table(
        "discovery_slices",
        sa.Column("slice_query", sa.String(length=600), nullable=False),
        sa.Column("base_query", sa.String(length=512), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum(
                "SPLIT",
                "COLLECTED",
                name="slice_outcome",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["pipeline_runs.id"], name=op.f("fk_discovery_slices_run_id_pipeline_runs")
        ),
        sa.PrimaryKeyConstraint("slice_query", name=op.f("pk_discovery_slices")),
    )
    op.create_index(
        op.f("ix_discovery_slices_base_query"), "discovery_slices", ["base_query"], unique=False
    )


def downgrade() -> None:
    op.execute("UPDATE pipeline_runs SET status = 'FAILED' WHERE status = 'INTERRUPTED'")
    _replace_run_status_check(("RUNNING", "SUCCEEDED", "FAILED"))
    op.drop_index(op.f("ix_discovery_slices_base_query"), table_name="discovery_slices")
    op.drop_table("discovery_slices")
