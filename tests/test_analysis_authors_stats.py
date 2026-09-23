from __future__ import annotations

import pytest

from agm_miner.analysis.authors import author_role, category_shares, detect_agent, is_bot
from agm_miner.analysis.stats import cliffs_delta, delta_magnitude, mann_whitney, spearman
from agm_miner.config import AnalysisConfig

CONFIG = AnalysisConfig()


@pytest.mark.parametrize(
    ("name", "email", "agent"),
    [
        ("Claude", "noreply@anthropic.com", "claude"),
        ("claude[bot]", "209825114+claude[bot]@users.noreply.github.com", "claude"),
        ("Copilot", "198982749+Copilot@users.noreply.github.com", "copilot"),
        ("copilot-swe-agent[bot]", "x@users.noreply.github.com", "copilot"),
        ("Cursor Agent", "cursoragent@cursor.com", "cursor"),
        ("chatgpt-codex-connector[bot]", "x@users.noreply.github.com", "codex"),
        # Trailer observed in openai/codex.
        ("Codex", "noreply@openai.com", "codex"),
        ("Ana (aider)", "ana@example.org", "aider"),
        # Humans who happen to be called Claude are not agents.
        ("Claude Dupont", "claude.dupont@example.fr", None),
        ("Ana", "ana@example.org", None),
    ],
)
def test_detect_agent(name: str, email: str, agent: str | None) -> None:
    assert detect_agent(name, email, CONFIG) == agent


@pytest.mark.parametrize(
    ("name", "email", "bot"),
    [
        ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", True),
        ("renovate-bot", "bot@renovateapp.com", True),
        ("github-actions", "41898282+github-actions[bot]@users.noreply.github.com", True),
        ("Ana", "ana@example.org", False),
    ],
)
def test_is_bot(name: str, email: str, bot: bool) -> None:
    assert is_bot(name, email, CONFIG) is bot


def test_roles() -> None:
    def role(file_changes: dict[str, int]) -> str:
        return author_role(category_shares(file_changes), False, CONFIG)

    assert role({"docs": 7, "agent_artifact": 2, "code": 1}) == "docs_focused"
    assert role({"code": 5, "test": 2, "docs": 3}) == "code_focused"
    assert role({"code": 3, "docs": 3, "ci": 4}) == "mixed"
    assert author_role({}, True, CONFIG) == "bot"
    # Coding agents are not people, whatever files they change; "claude[bot]" is also a bot.
    assert author_role(category_shares({"code": 9}), False, CONFIG, agent="claude") == "agent"
    assert author_role({}, True, CONFIG, agent="claude") == "agent"
    assert category_shares({}) == {}


def test_cliffs_delta() -> None:
    assert cliffs_delta([3, 4, 5], [1, 2]) == 1.0
    assert cliffs_delta([1, 2], [3, 4, 5]) == -1.0
    assert cliffs_delta([1, 2, 3], [1, 2, 3]) == 0.0
    assert delta_magnitude(0.1) == "negligible" and delta_magnitude(-0.5) == "large"
    with pytest.raises(ValueError):
        cliffs_delta([], [1])


def test_mann_whitney_and_spearman() -> None:
    comparison = mann_whitney([10, 11, 12, 13, 14], [1, 2, 3, 4, 5])
    assert comparison.p_value < 0.05 and comparison.effect_size == 1.0
    correlation = spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10])
    assert correlation.rho == pytest.approx(1.0) and correlation.n == 5
