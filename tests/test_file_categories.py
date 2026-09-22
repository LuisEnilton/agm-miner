from __future__ import annotations

import pytest

from agm_miner.file_categories import FileCategory, categorize


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("CLAUDE.md", FileCategory.AGENT_ARTIFACT),
        (".claude/skills/x/SKILL.md", FileCategory.AGENT_ARTIFACT),
        (".claude/settings.json", FileCategory.AGENT_CONFIG),
        (".cursorrules", FileCategory.AGENT_CONFIG),
        (".github/copilot-instructions.md", FileCategory.AGENT_CONFIG),
        ("GEMINI.md", FileCategory.AGENT_CONFIG),
        ("tests/test_api.py", FileCategory.TEST),
        ("src/app.test.ts", FileCategory.TEST),
        ("pkg/server_test.go", FileCategory.TEST),
        ("src/test_utils.py", FileCategory.TEST),
        (".github/workflows/ci.yml", FileCategory.CI),
        (".gitlab-ci.yml", FileCategory.CI),
        ("README.md", FileCategory.DOCS),
        ("docs/architecture.png", FileCategory.DOCS),
        ("docs/conf.py", FileCategory.CODE),
        ("LICENSE", FileCategory.DOCS),
        ("pyproject.toml", FileCategory.BUILD_CONFIG),
        ("Dockerfile", FileCategory.BUILD_CONFIG),
        ("config/app.yaml", FileCategory.BUILD_CONFIG),
        ("src/main.py", FileCategory.CODE),
        ("web/App.tsx", FileCategory.CODE),
        ("assets/logo.png", FileCategory.ASSET),
        ("bin/something", FileCategory.OTHER),
    ],
)
def test_categorize(path: str, category: FileCategory) -> None:
    assert categorize(path) is category


def test_excluded_artifact_is_not_an_artifact() -> None:
    assert categorize("node_modules/x/CLAUDE.md", ("**/node_modules/**", "node_modules/**")) is (
        FileCategory.DOCS
    )
