"""The Gemini SDK is an optional extra: everything but the Gemini provider works without it."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

from agm_miner.config import Rq1Config, Settings
from agm_miner.rq1.backends import make_backend
from agm_miner.rq1.llm import LLMRequestError


def test_the_cli_and_the_backends_do_not_import_the_gemini_sdk() -> None:
    code = (
        "import sys, agm_miner.cli, agm_miner.rq1.backends, agm_miner.rq1.stages; "
        "assert 'google.genai' not in sys.modules, 'google.genai was imported'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_gemini_without_the_extra_says_how_to_install_it(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "google.genai", None)  # import fails, as when not installed
    monkeypatch.delitem(sys.modules, "agm_miner.rq1.gemini", raising=False)
    with pytest.raises(LLMRequestError, match="poetry install -E gemini"):
        importlib.import_module("agm_miner.rq1.gemini")
    settings = Settings(_env_file=None, DATABASE_URL="postgresql://unused", GEMINI_API_KEY="k")
    with pytest.raises(LLMRequestError, match="google-genai"):
        make_backend(settings, Rq1Config(provider="gemini"), "classify")
