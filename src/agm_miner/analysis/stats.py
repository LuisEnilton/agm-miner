"""Non-parametric statistics used in the correlation analysis (Wang et al.; Venigalla &
Chimalakonda): Mann-Whitney U / Kruskal-Wallis for group differences, Spearman for monotonic
association, and Cliff's delta as effect size."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True, slots=True)
class GroupComparison:
    statistic: float
    p_value: float
    effect_size: float  # Cliff's delta
    magnitude: str


@dataclass(frozen=True, slots=True)
class Correlation:
    rho: float
    p_value: float
    n: int


def cliffs_delta(x: Sequence[float], y: Sequence[float]) -> float:
    """P(X > Y) - P(X < Y), in [-1, 1]."""
    a = np.asarray(x, dtype=float)
    b = np.asarray(y, dtype=float)
    if a.size == 0 or b.size == 0:
        raise ValueError("Cliff's delta needs two non-empty samples")
    greater = (a[:, None] > b[None, :]).sum()
    less = (a[:, None] < b[None, :]).sum()
    return float(greater - less) / (a.size * b.size)


def delta_magnitude(delta: float) -> str:
    """Romano et al. (2006) thresholds."""
    size = abs(delta)
    if size < 0.147:
        return "negligible"
    if size < 0.33:
        return "small"
    if size < 0.474:
        return "medium"
    return "large"


def mann_whitney(x: Sequence[float], y: Sequence[float]) -> GroupComparison:
    result = stats.mannwhitneyu(x, y, alternative="two-sided")
    delta = cliffs_delta(x, y)
    return GroupComparison(
        float(result.statistic), float(result.pvalue), delta, delta_magnitude(delta)
    )


def kruskal(*groups: Sequence[float]) -> tuple[float, float]:
    result = stats.kruskal(*groups)
    return float(result.statistic), float(result.pvalue)


def spearman(x: Sequence[float], y: Sequence[float]) -> Correlation:
    result = stats.spearmanr(x, y)
    return Correlation(float(result.statistic), float(result.pvalue), len(x))
