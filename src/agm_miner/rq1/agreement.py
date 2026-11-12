"""Agreement between two raters of the RQ1 classification (pure).

Each rater gives every section a set of category ids (multi-label). Agreement is measured per
category, as two binary decisions per section (has / has not the category):

* Cohen's kappa (chance-corrected agreement);
* precision, recall and F1 of rater ``b`` against reference rater ``a`` (e.g. the LLM against
  the human);
* prevalence of the category for each rater.

Macro averages and 95% bootstrap confidence intervals (resampling sections, seeded) summarise
the categories; micro precision, recall and F1 pool the decisions of every category (the
summary Chatlatanagulchai et al. report for their GPT-5 classifier). Kappa is undefined when
both raters never (or always) use a category; such categories are reported with ``None`` and
left out of the macro average.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float | None:
    if len(a) != len(b) or not a:
        raise ValueError("both raters must label the same, non-empty set of sections")
    x, y = np.asarray(a, dtype=bool), np.asarray(b, dtype=bool)
    observed = float(np.mean(x == y))
    px, py = float(np.mean(x)), float(np.mean(y))
    expected = px * py + (1 - px) * (1 - py)
    if expected == 1.0:  # both raters constant and identical: agreement says nothing
        return None
    return (observed - expected) / (1 - expected)


@dataclass(frozen=True, slots=True)
class CategoryAgreement:
    category: str
    n: int
    prevalence_a: float
    prevalence_b: float
    kappa: float | None
    precision: float | None
    recall: float | None
    f1: float | None


def _binary(labels: Sequence[frozenset[str]], category: str) -> list[bool]:
    return [category in chosen for chosen in labels]


def category_agreement(
    a: Sequence[frozenset[str]], b: Sequence[frozenset[str]], category: str
) -> CategoryAgreement:
    x, y = _binary(a, category), _binary(b, category)
    true_positive = sum(1 for i, j in zip(x, y, strict=True) if i and j)
    predicted, actual = sum(y), sum(x)
    precision = true_positive / predicted if predicted else None
    recall = true_positive / actual if actual else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return CategoryAgreement(
        category,
        len(x),
        round(actual / len(x), 4),
        round(predicted / len(y), 4),
        cohen_kappa(x, y),
        precision,
        recall,
        f1,
    )


def _macro(rows: Sequence[CategoryAgreement], field: str) -> float | None:
    values = [getattr(row, field) for row in rows if getattr(row, field) is not None]
    return float(np.mean(values)) if values else None


def _micro(
    a: Sequence[frozenset[str]], b: Sequence[frozenset[str]], categories: Sequence[str]
) -> dict[str, float | None]:
    wanted = set(categories)
    true_positive = sum(len(x & y & wanted) for x, y in zip(a, b, strict=True))
    predicted = sum(len(y & wanted) for y in b)
    actual = sum(len(x & wanted) for x in a)
    precision = true_positive / predicted if predicted else None
    recall = true_positive / actual if actual else None
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"micro_precision": precision, "micro_recall": recall, "micro_f1": f1}


def agreement(
    a: Mapping[str, frozenset[str]],
    b: Mapping[str, frozenset[str]],
    categories: Sequence[str],
    *,
    bootstrap: int = 1000,
    seed: int = 0,
) -> dict[str, Any]:
    """Per-category and macro agreement over the sections both raters labelled."""
    keys = sorted(set(a) & set(b))
    if not keys:
        raise ValueError("the raters have no section in common")
    left, right = [a[k] for k in keys], [b[k] for k in keys]
    rows = [category_agreement(left, right, category) for category in categories]
    exact = float(np.mean([x == y for x, y in zip(left, right, strict=True)]))

    rng = np.random.default_rng(seed)
    samples: dict[str, list[float]] = {"kappa": [], "f1": []}
    for _ in range(bootstrap):
        indices = rng.integers(0, len(keys), len(keys))
        resampled_a = [left[i] for i in indices]
        resampled_b = [right[i] for i in indices]
        resampled = [category_agreement(resampled_a, resampled_b, c) for c in categories]
        for field, values in samples.items():
            value = _macro(resampled, field)
            if value is not None:
                values.append(value)

    def interval(values: list[float]) -> list[float] | None:
        if not values:
            return None
        low, high = np.percentile(values, [2.5, 97.5])
        return [round(float(low), 4), round(float(high), 4)]

    return {
        "sections": len(keys),
        "exact_match": round(exact, 4),
        "macro_kappa": _macro(rows, "kappa"),
        "macro_kappa_ci95": interval(samples["kappa"]),
        "macro_f1": _macro(rows, "f1"),
        "macro_f1_ci95": interval(samples["f1"]),
        **_micro(left, right, categories),
        "categories": [asdict(row) for row in rows],
    }
