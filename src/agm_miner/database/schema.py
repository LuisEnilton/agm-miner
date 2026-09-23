"""Check that a database is at the schema revision this code expects before using it.

Running the pipeline against an outdated or foreign schema fails late and obscurely (e.g. an
enum value written by an older version); this check fails early with the fix to apply.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class SchemaMismatchError(RuntimeError):
    """The database schema does not match the code's migrations."""


@lru_cache
def _script_directory() -> ScriptDirectory:
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def expected_revision() -> str:
    return _script_directory().get_current_head()


def known_revisions() -> set[str]:
    return {revision.revision for revision in _script_directory().walk_revisions()}


def schema_problem(current: str | None, head: str, known: set[str]) -> str | None:
    """Explain why ``current`` is not usable (``None`` when it is the expected head)."""
    if current == head:
        return None
    if current is None:
        return "the database has no schema yet. Create it with `poetry run alembic upgrade head`."
    if current in known:
        return (
            f"the database is at schema revision {current}, but this code expects {head}. "
            "Update it with `poetry run alembic upgrade head`."
        )
    return (
        f"the database is at schema revision {current}, which this code does not know (it was "
        "created by an incompatible version of agm-miner). Point DATABASE_URL to a new database "
        "and run `poetry run alembic upgrade head` (see README, section "
        '"Banco criado por uma versão antiga").'
    )


def ensure_current_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        current = None
        if inspect(connection).has_table("alembic_version"):
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    problem = schema_problem(current, expected_revision(), known_revisions())
    if problem is not None:
        raise SchemaMismatchError(f"Cannot use {engine.url.render_as_string()}: {problem}")
