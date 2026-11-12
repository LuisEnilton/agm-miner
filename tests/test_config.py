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


def test_with_classifier_swaps_provider_and_classify_model() -> None:
    rq1 = StudyConfig().rq1
    other = rq1.with_classifier("openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert other.provider == "openrouter"
    assert other.classify_model == "nvidia/nemotron-3.5-super-vl:free"  # colons kept
    assert other.propose_model == rq1.propose_model and other.codebook == rq1.codebook
    for bad in ("claude-sonnet-5-5", "gemini:", "mistral:large"):
        with pytest.raises(ValueError):
            rq1.with_classifier(bad)
