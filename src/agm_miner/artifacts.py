"""Single source of truth for what counts as an agent-configuration artifact.

Discovery (search queries), mining (git pathspecs, classification) and analysis all go through
:func:`classify_path`, so matching rules can only change in one place.

Scope of the study: Claude Code (``CLAUDE.md``, ``CLAUDE.local.md``, ``.claude/skills/**``) and
``AGENTS.md``. New tools are added by extending :class:`ArtifactTool` and :func:`classify_path`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import PurePosixPath


class ArtifactTool(StrEnum):
    CLAUDE = "CLAUDE"
    AGENTS = "AGENTS"


class ArtifactKind(StrEnum):
    INSTRUCTION = "INSTRUCTION"
    SKILL_MANIFEST = "SKILL_MANIFEST"
    SKILL_RESOURCE = "SKILL_RESOURCE"


class ArtifactScope(StrEnum):
    ROOT = "ROOT"
    NESTED = "NESTED"


@dataclass(frozen=True, slots=True)
class ArtifactMatch:
    tool: ArtifactTool
    kind: ArtifactKind
    scope: ArtifactScope
    skill_name: str | None = None


_CLAUDE_INSTRUCTION_NAMES = frozenset({"claude.md", "claude.local.md"})
_AGENTS_INSTRUCTION_NAMES = frozenset({"agents.md"})
_SKILL_MANIFEST_NAME = "skill.md"

# Git pathspecs that select a superset of artifact paths; results are always re-checked with
# classify_path. ``**/`` also matches the repository root.
GIT_PATHSPECS: tuple[str, ...] = (
    ":(glob,icase)**/CLAUDE.md",
    ":(glob,icase)**/CLAUDE.local.md",
    ":(glob,icase)**/AGENTS.md",
    ":(glob)**/.claude/skills/**",
)


def classify_path(path: str) -> ArtifactMatch | None:
    """Classify a repository-relative POSIX path, or return ``None`` if it is not an artifact.

    * ``CLAUDE.md`` / ``CLAUDE.local.md`` / ``AGENTS.md`` (any case, any directory) are
      instructions. They are ``ROOT`` at the repository root or directly in the root ``.claude/``
      directory (both are loaded as project memory by Claude Code), ``NESTED`` elsewhere.
    * Files below ``.claude/skills/<name>/`` belong to skill ``<name>``; ``SKILL.md`` directly in
      the skill directory is its manifest, anything else is a resource. Skill directories below
      the repository root are ``NESTED``.
    """
    parts = PurePosixPath(path).parts
    if not parts or path.endswith("/"):
        return None

    skill_match = _classify_skill(parts)
    if skill_match is not None:
        return skill_match

    basename = parts[-1].lower()
    parent = parts[:-1]
    is_root = len(parent) == 0 or (len(parent) == 1 and parent[0] == ".claude")
    scope = ArtifactScope.ROOT if is_root else ArtifactScope.NESTED

    if basename in _CLAUDE_INSTRUCTION_NAMES:
        return ArtifactMatch(ArtifactTool.CLAUDE, ArtifactKind.INSTRUCTION, scope)
    if basename in _AGENTS_INSTRUCTION_NAMES:
        return ArtifactMatch(ArtifactTool.AGENTS, ArtifactKind.INSTRUCTION, scope)
    return None


def _classify_skill(parts: tuple[str, ...]) -> ArtifactMatch | None:
    for index in range(len(parts) - 1):
        if parts[index] != ".claude" or parts[index + 1] != "skills":
            continue
        remainder = parts[index + 2 :]
        if not remainder:
            return None
        scope = ArtifactScope.ROOT if index == 0 else ArtifactScope.NESTED
        if len(remainder) == 1:
            # A file directly inside .claude/skills/ (e.g. a README) belongs to no skill.
            return ArtifactMatch(ArtifactTool.CLAUDE, ArtifactKind.SKILL_RESOURCE, scope)
        skill_name = remainder[0]
        is_manifest = len(remainder) == 2 and remainder[1].lower() == _SKILL_MANIFEST_NAME
        kind = ArtifactKind.SKILL_MANIFEST if is_manifest else ArtifactKind.SKILL_RESOURCE
        return ArtifactMatch(ArtifactTool.CLAUDE, kind, scope, skill_name)
    return None


def is_excluded(path: str, exclude_globs: list[str] | tuple[str, ...]) -> bool:
    """True when ``path`` matches one of the exclusion globs (``**`` spans directories)."""
    return any(_glob_match(path, pattern) for pattern in exclude_globs)


def _glob_match(path: str, pattern: str) -> bool:
    if pattern.startswith("**/"):
        # "**/x/**" must also match "x/..." at the root.
        return fnmatchcase(path, pattern) or fnmatchcase(path, pattern[3:])
    return fnmatchcase(path, pattern)


def classify_included(
    path: str, exclude_globs: list[str] | tuple[str, ...]
) -> ArtifactMatch | None:
    if is_excluded(path, exclude_globs):
        return None
    return classify_path(path)
