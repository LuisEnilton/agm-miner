from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from agm_miner.database.models import Base

pytestmark = pytest.mark.db


def test_migrations_match_models(pg_engine) -> None:
    """The schema built by `alembic upgrade head` must equal the ORM models."""
    with pg_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
