"""Classification of contributors: bots, coding agents and work profile (RQ3).

Role definitions (thresholds in ``StudyConfig.analysis``), computed from the share of an
identity's file changes per category over the repository's whole history:

* ``agent``         — name/e-mail matches a coding agent pattern (takes precedence over ``bot``:
  identities such as ``claude[bot]`` match both);
* ``bot``           — name/e-mail matches a bot pattern;
* ``docs_focused``  — docs + agent artifacts + agent config >= ``docs_focused_min_share``;
* ``code_focused``  — code + tests >= ``code_focused_min_share``;
* ``mixed``         — everything else.

Only ``docs_focused``, ``code_focused`` and ``mixed`` identities are human contributors.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache

from agm_miner.config import AnalysisConfig
from agm_miner.file_categories import FileCategory

DOC_CATEGORIES = frozenset(
    {FileCategory.DOCS, FileCategory.AGENT_ARTIFACT, FileCategory.AGENT_CONFIG}
)
CODE_CATEGORIES = frozenset({FileCategory.CODE, FileCategory.TEST})


@lru_cache(maxsize=64)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


def _signature(name: str, email: str) -> str:
    return f"{name.strip()} <{email.strip()}>"


def is_bot(name: str, email: str, config: AnalysisConfig) -> bool:
    signature = _signature(name, email)
    return any(pattern.search(signature) for pattern in _compiled(tuple(config.bot_patterns)))


def detect_agent(name: str, email: str, config: AnalysisConfig) -> str | None:
    """Name of the coding agent an identity / co-author belongs to, if any."""
    signature = _signature(name, email)
    for agent, patterns in config.agent_patterns.items():
        if any(pattern.search(signature) for pattern in _compiled(tuple(patterns))):
            return agent
    return None


def category_shares(file_changes: Mapping[str, int]) -> dict[str, float]:
    total = sum(file_changes.values())
    if total == 0:
        return {}
    return {category: count / total for category, count in file_changes.items()}


def author_role(
    shares: Mapping[str, float], bot: bool, config: AnalysisConfig, *, agent: str | None = None
) -> str:
    if agent is not None:
        return "agent"
    if bot:
        return "bot"
    docs = sum(shares.get(category.value, 0.0) for category in DOC_CATEGORIES)
    code = sum(shares.get(category.value, 0.0) for category in CODE_CATEGORIES)
    if docs >= config.docs_focused_min_share:
        return "docs_focused"
    if code >= config.code_focused_min_share:
        return "code_focused"
    return "mixed"
