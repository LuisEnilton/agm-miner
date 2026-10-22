"""Mine a synthetic repository, export bronze Parquet and build the analysis datasets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

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

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def alembic_head() -> str:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


HUMAN = ("Ana Dev", "ana@example.org")
BOT = ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com")
AGENT = ("Claude", "noreply@anthropic.com")  # commits authored by the coding agent itself

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
        "Refactor app", {"src/app.py": "x = 2\n"}, author=AGENT, date="2024-03-20T00:00:00+00:00"
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
    assert manifest["schema_revision"] == alembic_head()
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
    with session_factory.kw["bind"].connect() as connection:
        assert export_table(connection, table, destination) == 0
    frame = pl.read_parquet(destination)
    assert frame.height == 0
    assert frame.schema == table_schema(table)


# The synthetic artifacts are tiny and have one human author: these thresholds keep them all.
EVERYTHING = AnalysisConfig(min_artifact_bytes=0, min_human_contributors=1)


def test_gold_datasets(bronze: Path, tmp_path: Path) -> None:
    output = tmp_path / "gold"
    counts = build_datasets(bronze, output, EVERYTHING)
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
    agent = roles.filter(pl.col("agent") == "claude").row(0, named=True)
    assert agent["role"] == "agent" and not agent["is_bot"]
    human = roles.filter(pl.col("name") == HUMAN[0]).row(0, named=True)
    assert human["artifact_commits"] == 3 and human["role"] == "mixed"

    features = pl.read_parquet(output / "repo_features.parquet").row(0, named=True)
    assert features["has_claude_md"] and not features["has_agents_md"]
    assert features["skills"] == 1
    # The agent's commit counts as activity, but the agent is not a contributor.
    assert features["total_commits"] == 6 and features["contributors"] == 1
    assert features["artifact_changes"] == 3 and features["artifact_commits"] == 3
    assert features["manual_only"] is True
    manifest = json.loads((output / "analysis_manifest.json").read_text())
    assert manifest["bronze_manifest"]["schema_revision"] == alembic_head()
    assert manifest["outputs"]["rq3_changes"] == counts["rq3_changes"]
    assert manifest["sample"]["sample_repositories"] == 1


def test_gold_datasets_hold_only_the_study_sample(bronze: Path, tmp_path: Path) -> None:
    output = tmp_path / "gold"
    counts = build_datasets(bronze, output, AnalysisConfig())

    # The artifacts are smaller than 2 KB: nothing is in the sample, but silver keeps it all.
    assert counts["rq1_sections"] == counts["rq2_structure"] == counts["rq3_changes"] == 0
    assert counts["rq3_author_roles"] == 0
    assert counts["silver_version_metrics"] > 0
    sample = pl.read_parquet(output / "silver_artifact_sample.parquet")
    assert sample.height == 2 and not sample["in_sample"].any()
    features = pl.read_parquet(output / "repo_features.parquet").row(0, named=True)
    assert features["in_sample"] is False
    assert features["exclusion_reason"] == "min_artifact_bytes"
    assert features["human_contributors"] == 1
    manifest = json.loads((output / "analysis_manifest.json").read_text())
    assert manifest["sample"] == {
        "mined_repositories": 1,
        "sample_repositories": 0,
        "excluded_repositories": {"min_artifact_bytes": 1},
        "artifacts": 2,
        "sample_artifacts": 0,
    }


def test_interrupted_export_leaves_no_dataset(session_factory, tmp_path: Path, monkeypatch) -> None:
    from agm_miner.stages import export as export_module

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(export_module, "export_table", interrupt)
    with pytest.raises(KeyboardInterrupt):
        export_bronze(StudyConfig(), session_factory, tmp_path / "bronze")
    assert list((tmp_path / "bronze").iterdir()) == []
    with pytest.raises(FileNotFoundError):
        latest_export(tmp_path / "bronze")


def test_export_reads_one_consistent_snapshot(session_factory, tmp_path: Path, monkeypatch) -> None:
    from agm_miner.stages import export as export_module

    seen = []
    original = export_module.export_table

    def spy(connection, table, destination, on_rows=None):
        seen.append((id(connection), connection.get_isolation_level(), connection.in_transaction()))
        return original(connection, table, destination, on_rows)

    monkeypatch.setattr(export_module, "export_table", spy)
    export_bronze(StudyConfig(), session_factory, tmp_path / "bronze")
    assert len({connection_id for connection_id, _, _ in seen}) == 1
    assert {(level, active) for _, level, active in seen} == {("REPEATABLE READ", True)}


def test_interrupted_analysis_keeps_previous_output(
    bronze: Path, tmp_path: Path, monkeypatch
) -> None:
    from agm_miner.analysis import datasets

    output = tmp_path / "gold"
    build_datasets(bronze, output, AnalysisConfig())
    before = sorted(path.name for path in output.iterdir())

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(datasets, "build_repo_features", interrupt)
    with pytest.raises(KeyboardInterrupt):
        build_datasets(bronze, output, AnalysisConfig())
    assert sorted(path.name for path in output.iterdir()) == before
    assert not (tmp_path / "gold.partial").exists()
