"""Paired hierarchical bootstrap and Wilson bounds with explicit sample counts."""

import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist, fmean


@dataclass(frozen=True)
class Estimate:
    value: float | None
    n: int
    ci_low: float | None
    ci_high: float | None
    clusters: int | None = None


def wilson(successes: int, n: int, confidence: float = 0.95) -> Estimate:
    if (
        type(n) is not int
        or type(successes) is not int
        or not 0 <= successes <= n
        or not 0 < confidence < 1
    ):
        raise ValueError("Invalid binomial counts/confidence")
    if n == 0:
        return Estimate(None, 0, None, None)
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    proportion = successes / n
    denominator = 1 + z * z / n
    center = (proportion + z * z / (2 * n)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n)) / denominator
    return Estimate(
        proportion,
        n,
        0.0 if successes == 0 else max(0, center - half),
        1.0 if successes == n else min(1, center + half),
    )


def quantile(values: Sequence[float], p: float) -> float:
    if not values or not 0 <= p <= 1 or any(not math.isfinite(v) for v in values):
        raise ValueError("Invalid quantile input")
    ordered = sorted(values)
    offset = (len(ordered) - 1) * p
    low = math.floor(offset)
    high = math.ceil(offset)
    return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)


def paired_bootstrap(
    left: Mapping[str, Sequence[float]],
    right: Mapping[str, Sequence[float]],
    families: dict[str, str],
    *,
    resamples: int = 10000,
    seed: int = 20261010,
    confidence: float = 0.95,
) -> Estimate:
    """Average repeated generations within question, then resample families and their questions."""
    if (
        set(left) != set(right)
        or not set(left) <= families.keys()
        or resamples < 100
        or not 0 < confidence < 1
    ):
        raise ValueError("Paired case sets, family identities and bootstrap settings must match")
    if not left:
        return Estimate(None, 0, None, None, 0)
    clusters: dict[str, list[float]] = defaultdict(list)
    differences = []
    for case in sorted(left):
        a, b = left[case], right[case]
        if not a or len(a) != len(b) or any(not math.isfinite(v) for v in [*a, *b]):
            raise ValueError("Every paired case requires the same finite repetition count")
        delta = fmean(b) - fmean(a)
        differences.append(delta)
        clusters[families[case]].append(delta)
    groups = [clusters[key] for key in sorted(clusters)]
    rng = random.Random(seed)
    samples = []
    for _ in range(resamples):
        total = 0.0
        count = 0
        for _ in groups:
            group = rng.choice(groups)
            for _ in group:
                total += rng.choice(group)
                count += 1
        samples.append(total / count)
    alpha = (1 - confidence) / 2
    return Estimate(
        fmean(differences),
        len(differences),
        quantile(samples, alpha),
        quantile(samples, 1 - alpha),
        len(groups),
    )
