"""PostgreSQL fixtures: a dedicated test database migrated with Alembic (so the migrations
themselves are exercised), truncated between tests. Tests marked ``db`` are skipped when the
server is unreachable."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from tests.conftest import TEST_DATABASE_URL

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": parsed.database}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    try:
        _ensure_database(TEST_DATABASE_URL)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at {TEST_DATABASE_URL}: {exc.orig}")

    from alembic import command
    from alembic.config import Config

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(pg_engine: Engine) -> Iterator[sessionmaker[Session]]:
    from agm_miner.database.models import Base

    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    with pg_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield sessionmaker(bind=pg_engine, autoflush=False, expire_on_commit=False)
