from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values  # comes with pydantic-settings

_DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg2://research_user:research_password@localhost:5432/agm_test"
)


def resolve_test_database_url(environ: dict[str, str], env_file: Path) -> str:
    """The environment's TEST_DATABASE_URL, else the one in this checkout's ``.env``, else
    ``agm_test``. Reading ``.env`` lets every git worktree use its own test database, so two
    checkouts running the suite at once do not truncate each other's tables."""
    return (
        environ.get("TEST_DATABASE_URL")
        or (dotenv_values(env_file).get("TEST_DATABASE_URL") if env_file.exists() else None)
        or _DEFAULT_TEST_DATABASE_URL
    )


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
TEST_DATABASE_URL = resolve_test_database_url(dict(os.environ), _ENV_FILE)

# Settings are read from the environment; point everything at test resources before any
# agm_miner module is imported.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ.pop("STUDY_CONFIG_PATH", None)

from tests.gitrepo import RepoBuilder  # noqa: E402

pytest_plugins = ["tests.db_fixtures"]


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; tests that change the environment must see it."""
    from agm_miner.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _require_git(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("git") and shutil.which("git") is None:
        pytest.skip("git executable not available")


@pytest.fixture
def repo_builder(tmp_path: Path) -> Iterator[RepoBuilder]:
    yield RepoBuilder(tmp_path / "origin")
