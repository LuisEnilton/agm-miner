from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://research_user:research_password@localhost:5432/agm_test",
)

# Settings are read from the environment; point everything at test resources before any
# agm_miner module is imported.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ.pop("STUDY_CONFIG_PATH", None)

from tests.gitrepo import RepoBuilder  # noqa: E402

pytest_plugins = ["tests.db_fixtures"]


@pytest.fixture(autouse=True)
def _require_git(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("git") and shutil.which("git") is None:
        pytest.skip("git executable not available")


@pytest.fixture
def repo_builder(tmp_path: Path) -> Iterator[RepoBuilder]:
    yield RepoBuilder(tmp_path / "origin")
