"""Mine a synthetic repository, export bronze Parquet and build the analysis datasets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from agm_miner.analysis.datasets import build_datasets
from agm_miner.config import AnalysisConfig, Settings, StudyConfig
from agm_miner.database.models import Base, DiscoveryHit, PipelineRun, Repository, RepositoryStatus
from agm_miner.stages.export import (
    export_bronze,
    export_table,
    latest_export,
    read_manifest,
    table_schema,
)
from agm_miner.stages.mining import MiningOptions, mine_repositories
from agm_miner.stages.selection import MANUAL_QUERY
from tests.gitrepo import RepoBuilder

pytestmark = [pytest.mark.db, pytest.mark.git]

HUMAN = ("Ana Dev", "ana@example.org")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")

V1 = "# Project\n\n## Setup\nInstall with make.\n"
V2 = V1 + "\n## Testing\nRun pytest before pushing.\n"
SKILL = "---\nname: db\ndescription: Database migrations\n---\n# DB\n\nUse alembic.\n"


@pytest.fixture
def bronze(tmp_path: Path, session_factory, repo_builder: RepoBuilder) -> Path:
    repo = repo_builder
    repo.commit(
        "Add app",
        {"src/app.py": "x = 1\n", "README.md": "readme\n"},
        author=HUMAN,
        date="2024-03-01T09:00:00+00:00",
    )
    repo.commit(
        "Add CLAUDE.md\n\nCo-authored-by: Claude <noreply@anthropic.com>",
        {"CLAUDE.md": V1},
        author=HUMAN,
        date="2024-03-02T09:00:00+00:00",
    )
    repo.commit(
        "Document testing",
        {"CLAUDE.md": V2, "tests/test_app.py": "def test():\n    pass\n"},
        author=HUMAN,
        date="2024-03-02T15:00:00+00:00",
    )
    repo.commit(
        "Bump deps", {"pyproject.toml": "[project]\n"}, author=BOT, date="2024-03-10T00:00:00+00:00"
    )
    repo.commit(
        "Add skill",
        {".claude/skills/db/SKILL.md": SKILL},
        author=HUMAN,
        date="2024-04-01T00:00:00+00:00",
    )

    with session_factory() as session, session.begin():
        session.add(
            Repository(
                id=7,
                node_id="R_7",
                full_name="acme/app",
                status=RepositoryStatus.SELECTED,
                stars=120,
                forks=3,
                topics=["python", "agents"],
                pushed_at=datetime(2024, 4, 1, tzinfo=UTC),
            )
        )
        session.flush()
        run = PipelineRun(
            stage="SELECT",
            status="SUCCEEDED",
            code_version="x",
            study_config_hash="h",
            study_config={},
        )
        session.add(run)
        session.flush()
        session.add(DiscoveryHit(repo_id=7, query=MANUAL_QUERY, path="", run_id=run.id))

    settings = Settings(CLONE_DIR=str(tmp_path / "clones"))
    stats = mine_repositories(
        settings, StudyConfig(), session_factory, MiningOptions(), clone_url=lambda _: repo.url
    )
    assert stats["mined"] == 1
    return export_bronze(StudyConfig(), session_factory, tmp_path / "bronze")


def test_bronze_export_contains_every_table_with_typed_columns(bronze: Path) -> None:
    manifest = read_manifest(bronze)
    assert manifest["schema_revision"] == "0001"
    assert set(manifest["tables"]) == {table.name for table in Base.metadata.sorted_tables}
    assert manifest["tables"]["artifacts"] == 2
    assert latest_export(bronze.parent) == bronze

    repositories = pl.read_parquet(bronze / "repositories.parquet")
    assert repositories.schema["topics"] == pl.List(pl.Utf8)
    assert repositories.schema["pushed_at"] == pl.Datetime("us", "UTC")
    assert repositories["topics"][0].to_list() == ["python", "agents"]
    runs = pl.read_parquet(bronze / "pipeline_runs.parquet")
    assert runs.schema["study_config"] == pl.Utf8
    assert json.loads(runs.filter(pl.col("stage") == "MINE")["study_config"][0])["version"] == 1


def test_empty_table_export_keeps_schema(session_factory, tmp_path: Path) -> None:
    table = Base.metadata.tables["commit_coauthors"]
    destination = tmp_path / "coauthors.parquet"
    assert export_table(session_factory.kw["bind"], table, destination) == 0
    frame = pl.read_parquet(destination)
    assert frame.height == 0
    assert frame.schema == table_schema(table)


def test_gold_datasets(bronze: Path, tmp_path: Path) -> None:
    output = tmp_path / "gold"
    counts = build_datasets(bronze, output, AnalysisConfig())
    assert counts["rq2_structure"] == 2

    rq1 = pl.read_parquet(output / "rq1_sections.parquet")
    claude_sections = rq1.filter(pl.col("path") == "CLAUDE.md")
    assert claude_sections["h2"].to_list() == [None, "Setup", "Testing"]
    assert claude_sections["full_name"].unique().to_list() == ["acme/app"]

    rq2 = pl.read_parquet(output / "rq2_structure.parquet").sort("path")
    skill = rq2.filter(pl.col("kind") == "SKILL_MANIFEST").row(0, named=True)
    assert skill["frontmatter_keys"] == ["description", "name"]
    assert skill["skill_name"] == "db"
    claude = rq2.filter(pl.col("path") == "CLAUDE.md").row(0, named=True)
    assert claude["h2"] == 2 and claude["words"] > 0

    rq3 = pl.read_parquet(output / "rq3_changes.parquet").filter(pl.col("path") == "CLAUDE.md")
    first, second = rq3.sort("change_index").iter_rows(named=True)
    assert first["change_type"] == "A" and first["hours_since_previous_change"] is None
    assert first["agents_involved"] == ["claude"] and first["agent_involved"]
    assert first["sections_added"] == ["Project", "Project > Setup"]
    assert second["hours_since_previous_change"] == pytest.approx(6.0)
    assert second["in_burst"] is True
    assert second["sections_added"] == ["Project > Testing"]
    assert second["co_changed_files"] == 1 and second["co_changed_test"] == 1
    # Ana changed 1 code, 1 test, 1 README and 3 artifact files: neither docs- nor code-focused.
    assert second["author_role"] == "mixed"

    roles = pl.read_parquet(output / "rq3_author_roles.parquet")
    bot = roles.filter(pl.col("is_bot")).row(0, named=True)
    assert bot["role"] == "bot" and bot["share_build_config"] == 1.0
    human = roles.filter(~pl.col("is_bot")).row(0, named=True)
    assert human["artifact_commits"] == 3

    features = pl.read_parquet(output / "repo_features.parquet").row(0, named=True)
    assert features["has_claude_md"] and not features["has_agents_md"]
    assert features["skills"] == 1
    assert features["total_commits"] == 5 and features["contributors"] == 1
    assert features["artifact_changes"] == 3 and features["artifact_commits"] == 3
    assert features["manual_only"] is True
    manifest = json.loads((output / "analysis_manifest.json").read_text())
    assert manifest["bronze_manifest"]["schema_revision"] == "0001"
    assert manifest["outputs"]["rq3_changes"] == counts["rq3_changes"]
