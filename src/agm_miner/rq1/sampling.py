"""Stratified, deterministic samples of classification units.

Strata are tool x artifact kind x length bucket. Each stratum gets a share of the sample
proportional to its size (largest remainder, at least one unit per non-empty stratum when the
sample is large enough). Within strata, units are taken in a seeded random order, skipping
repositories that already reached ``per_repo_cap`` units, so a few large repositories cannot
dominate. Identical sections (same ``section_key``) count once, represented by their occurrence
with the smallest (repository id, path, position).

The result depends only on the content, the seed and the configuration: units are ordered by
stable fields (never by database ids, which change when a repository is mined again) before the
seeded shuffle, so the same seed yields the same sample from any export of the same data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence

import numpy as np
import polars as pl


def length_bucket(words: int, bins: Sequence[int]) -> int:
    """Index of the length bucket: 0 below ``bins[0]`` words, 1 below ``bins[1]``, ..."""
    for index, limit in enumerate(bins):
        if words < limit:
            return index
    return len(bins)


def with_strata(units: pl.DataFrame, length_bins: Sequence[int]) -> pl.DataFrame:
    buckets = [length_bucket(words, length_bins) for words in units["words"].to_list()]
    return units.with_columns(
        pl.concat_str(
            [pl.col("tool"), pl.col("kind"), pl.Series("bucket", buckets).cast(pl.Utf8)],
            separator="/",
        ).alias("stratum")
    )


def allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Proportional allocation of ``total`` over strata of the given sizes (largest remainder)."""
    available = sum(sizes.values())
    if available == 0:
        return {stratum: 0 for stratum in sizes}
    total = min(total, available)
    exact = {stratum: total * size / available for stratum, size in sizes.items()}
    quota = {stratum: int(value) for stratum, value in exact.items()}
    # Every non-empty stratum is represented when the sample can afford it.
    if total >= sum(1 for size in sizes.values() if size):
        for stratum, size in sizes.items():
            if size and quota[stratum] == 0:
                quota[stratum] = 1
    remaining = total - sum(quota.values())
    by_remainder = sorted(sizes, key=lambda s: (exact[s] - int(exact[s]), s), reverse=True)
    for stratum in by_remainder:
        if remaining <= 0:
            break
        if quota[stratum] < sizes[stratum]:
            quota[stratum] += 1
            remaining -= 1
    while remaining < 0:  # the minimum-one rule overshot: take back from the largest quotas
        largest = max(quota, key=lambda s: (quota[s], s))
        quota[largest] -= 1
        remaining += 1
    return quota


def stratified_sample(
    units: pl.DataFrame,
    size: int,
    *,
    per_repo_cap: int,
    seed: int,
    length_bins: Sequence[int],
    exclude_keys: Iterable[str] = (),
) -> pl.DataFrame:
    """``size`` distinct units (by ``section_key``), stratified and capped per repository.

    ``exclude_keys`` keeps units out of the sample (e.g. those used to create the labels, so the
    saturation and validation samples are fresh).
    """
    excluded = set(exclude_keys)
    pool = (
        with_strata(units, length_bins)
        .filter(~pl.col("section_key").is_in(list(excluded)))
        .sort("section_key", "repo_id", "path", "position")
        .unique("section_key", keep="first", maintain_order=True)
    )
    if pool.is_empty() or size <= 0:
        return pool.head(0)
    rng = np.random.default_rng(seed)
    pool = pool.with_columns(pl.Series("_order", rng.permutation(pool.height))).sort("_order")
    sizes = dict(Counter(pool["stratum"].to_list()))
    quota = allocate(sizes, size)

    per_repo: Counter[int] = Counter()
    chosen: list[int] = []
    rows = pool.select("stratum", "repo_id").to_dicts()
    taken: Counter[str] = Counter()
    for index, row in enumerate(rows):
        in_quota = taken[row["stratum"]] < quota[row["stratum"]]
        if in_quota and per_repo[row["repo_id"]] < per_repo_cap:
            chosen.append(index)
            taken[row["stratum"]] += 1
            per_repo[row["repo_id"]] += 1
    # Strata whose units are concentrated in capped repositories leave a shortfall: fill it with
    # the next units in random order from any stratum, still honouring the cap.
    picked = set(chosen)
    for index, row in enumerate(rows):
        if len(chosen) >= min(size, pool.height):
            break
        if index not in picked and per_repo[row["repo_id"]] < per_repo_cap:
            chosen.append(index)
            picked.add(index)
            per_repo[row["repo_id"]] += 1
    return pool[sorted(chosen)].drop("_order")
