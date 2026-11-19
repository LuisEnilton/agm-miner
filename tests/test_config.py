from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agm_miner.config import StudyConfig, load_study_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_missing_file_means_defaults(tmp_path: Path) -> None:
    assert load_study_config(tmp_path / "absent.yaml") == StudyConfig()


def test_repository_study_file_is_valid() -> None:
    config = load_study_config(PROJECT_ROOT / "study.yaml")
    assert config.selection.min_stars == 50
    assert "filename:AGENTS.md" in config.discovery.queries


def test_repository_study_file_records_the_final_rq1_collection() -> None:
    # The paper's classification: Claude Sonnet 5.5 (effort low) with the Agent READMEs codebook.
    # Whoever replicates from study.yaml must not run another model by default.
    rq1 = load_study_config(PROJECT_ROOT / "study.yaml").rq1
    assert (rq1.provider, rq1.classify_model, rq1.classify_effort) == (
        "anthropic",
        "claude-sonnet-5-5",
        "low",
    )
    assert rq1.codebook == Path("codebooks/seed_agent_readmes.yaml")
    assert rq1.chunk_sections == 30


def test_partial_file_overrides_only_given_keys(tmp_path: Path) -> None:
    path = tmp_path / "study.yaml"
    path.write_text("selection:\n  min_stars: 100\n  reference_date: 2026-01-31\n")
    config = load_study_config(path)
    assert config.selection.min_stars == 100
    assert config.selection.reference_date.isoformat() == "2026-01-31"
    assert config.selection.min_commits == StudyConfig().selection.min_commits


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "study.yaml"
    path.write_text("selection:\n  min_starz: 100\n")
    with pytest.raises(ValidationError):
        load_study_config(path)


def test_fingerprint_is_stable_and_sensitive() -> None:
    base = StudyConfig()
    assert base.fingerprint() == StudyConfig().fingerprint()
    changed = StudyConfig.model_validate({"selection": {"min_stars": 51}})
    assert changed.fingerprint() != base.fingerprint()


def test_with_model_swaps_provider_and_classify_model() -> None:
    rq1 = StudyConfig().rq1
    other = rq1.with_model("openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert other.provider == "openrouter"
    assert other.classify_model == "nvidia/nemotron-3.5-super-vl:free"  # colons kept
    assert other.propose_model == rq1.propose_model and other.codebook == rq1.codebook
    labels = rq1.with_model("anthropic:claude-opus-5-5", "propose")
    assert labels.propose_model == "claude-opus-5-5" and labels.classify_model == rq1.classify_model
    for bad in ("claude-sonnet-5-5", "gemini:", "mistral:large"):
        with pytest.raises(ValueError):
            rq1.with_model(bad)


def test_test_database_url_comes_from_the_environment_then_the_env_file(tmp_path: Path) -> None:
    from tests.conftest import _DEFAULT_TEST_DATABASE_URL, resolve_test_database_url

    env_file = tmp_path / ".env"
    assert resolve_test_database_url({}, env_file) == _DEFAULT_TEST_DATABASE_URL  # no .env
    env_file.write_text("DATABASE_URL=postgresql://x/dev\nTEST_DATABASE_URL=postgresql://x/rq2\n")
    assert resolve_test_database_url({}, env_file) == "postgresql://x/rq2"
    environ = {"TEST_DATABASE_URL": "postgresql://x/ci"}
    assert resolve_test_database_url(environ, env_file) == "postgresql://x/ci"


def test_database_url_is_only_required_by_the_commands_that_use_it(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agm_miner.cli import app
    from agm_miner.config import MissingDatabaseUrlError, Settings, get_settings

    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url is None
    with pytest.raises(MissingDatabaseUrlError, match="analysis commands"):
        settings.require_database_url()
    assert Settings(_env_file=None, DATABASE_URL="postgresql://x/y").require_database_url()

    monkeypatch.chdir(tmp_path)  # no .env here
    get_settings.cache_clear()
    result = CliRunner().invoke(app, ["runs", "list"])
    assert result.exit_code == 1 and "DATABASE_URL is not set" in result.output
    get_settings.cache_clear()
