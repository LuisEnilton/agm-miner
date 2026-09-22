from __future__ import annotations

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from agm_miner.cli import app
from agm_miner.database.schema import (
    SchemaMismatchError,
    ensure_current_schema,
    expected_revision,
    known_revisions,
    schema_problem,
)


def test_expected_revision_is_the_latest_migration() -> None:
    head = expected_revision()
    assert head in known_revisions()
    assert schema_problem(head, head, known_revisions()) is None


@pytest.mark.parametrize(
    ("current", "hint"),
    [
        (None, "has no schema yet"),
        ("0001", "alembic upgrade head"),
        # Revision of the pre-refactor schema (statuses PENDING/COMPLETED).
        ("20260719_0001", "incompatible version"),
    ],
)
def test_schema_problems_explain_the_fix(current: str | None, hint: str) -> None:
    assert hint in schema_problem(current, "0003", {"0001", "0002", "0003"})


@pytest.mark.db
def test_database_at_head_is_accepted(pg_engine) -> None:
    ensure_current_schema(pg_engine)


@pytest.mark.db
def test_outdated_database_is_refused_before_any_work(pg_engine, session_factory) -> None:
    head = expected_revision()
    with pg_engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '20260719_0001'"))
    try:
        with pytest.raises(SchemaMismatchError, match="incompatible version") as error:
            ensure_current_schema(pg_engine)
        assert "research_password" not in str(error.value)  # credentials are masked

        result = CliRunner().invoke(app, ["runs", "funnel"])
        assert result.exit_code == 1
        assert "incompatible version" in result.output and "Traceback" not in result.output
    finally:
        with pg_engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :head"), {"head": head}
            )
