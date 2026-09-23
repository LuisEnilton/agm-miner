from __future__ import annotations

import pytest

from agm_miner.artifacts import (
    ArtifactKind,
    ArtifactMatch,
    ArtifactScope,
    ArtifactTool,
    classify_included,
    classify_path,
    is_excluded,
)

CLAUDE, AGENTS = ArtifactTool.CLAUDE, ArtifactTool.AGENTS
INSTRUCTION = ArtifactKind.INSTRUCTION
MANIFEST, RESOURCE = ArtifactKind.SKILL_MANIFEST, ArtifactKind.SKILL_RESOURCE
ROOT, NESTED = ArtifactScope.ROOT, ArtifactScope.NESTED


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("CLAUDE.md", ArtifactMatch(CLAUDE, INSTRUCTION, ROOT)),
        ("claude.md", ArtifactMatch(CLAUDE, INSTRUCTION, ROOT)),
        ("CLAUDE.local.md", ArtifactMatch(CLAUDE, INSTRUCTION, ROOT)),
        (".claude/CLAUDE.md", ArtifactMatch(CLAUDE, INSTRUCTION, ROOT)),
        ("packages/api/CLAUDE.md", ArtifactMatch(CLAUDE, INSTRUCTION, NESTED)),
        ("AGENTS.md", ArtifactMatch(AGENTS, INSTRUCTION, ROOT)),
        ("Agents.md", ArtifactMatch(AGENTS, INSTRUCTION, ROOT)),
        ("services/web/AGENTS.md", ArtifactMatch(AGENTS, INSTRUCTION, NESTED)),
        (".claude/skills/pdf/SKILL.md", ArtifactMatch(CLAUDE, MANIFEST, ROOT, "pdf")),
        (".claude/skills/pdf/skill.md", ArtifactMatch(CLAUDE, MANIFEST, ROOT, "pdf")),
        (".claude/skills/pdf/scripts/fill.py", ArtifactMatch(CLAUDE, RESOURCE, ROOT, "pdf")),
        (".claude/skills/pdf/docs/SKILL.md", ArtifactMatch(CLAUDE, RESOURCE, ROOT, "pdf")),
        (".claude/skills/README.md", ArtifactMatch(CLAUDE, RESOURCE, ROOT, None)),
        ("apps/x/.claude/skills/db/SKILL.md", ArtifactMatch(CLAUDE, MANIFEST, NESTED, "db")),
        # A CLAUDE.md inside a skill is part of the skill, not a standalone instruction.
        (".claude/skills/pdf/CLAUDE.md", ArtifactMatch(CLAUDE, RESOURCE, ROOT, "pdf")),
    ],
)
def test_classifies_artifacts(path: str, expected: ArtifactMatch) -> None:
    assert classify_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "NOTCLAUDE.md",
        "MY_AGENTS.md",
        "docs/claude.markdown",
        "CLAUDE.md.bak",
        "README.md",
        ".claude/settings.json",
        ".claude/commands/review.md",
        ".claude/skills",
        "skills/pdf/SKILL.md",
        "",
    ],
)
def test_rejects_non_artifacts(path: str) -> None:
    assert classify_path(path) is None


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        ("node_modules/pkg/CLAUDE.md", True),
        ("web/node_modules/pkg/AGENTS.md", True),
        ("vendor/lib/CLAUDE.md", True),
        ("src/vendorized/CLAUDE.md", False),
        ("CLAUDE.md", False),
    ],
)
def test_exclusion_globs(path: str, excluded: bool) -> None:
    globs = ["node_modules/**", "**/node_modules/**", "vendor/**", "**/vendor/**"]
    assert is_excluded(path, globs) is excluded
    assert (classify_included(path, globs) is None) is excluded
