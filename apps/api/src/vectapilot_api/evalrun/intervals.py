"""Intervals for the eval report (M2 PR 8): Wilson for counts, a seeded bootstrap for means.

Pure functions; no I/O. Every retrieval rate in a report carries one of these next to its `x of n`
(decided 2026-10-22, H2 decisions): small samples make a bare percentage look more certain than it
is (guide docs/learning/m2-metrics.md §2.6).

**Wilson score interval** (for `x of n`, e.g. hit@k and correctness). With z the normal quantile
for the confidence (1.959964 for 95 %):

    centre = (x + z^2/2) / (n + z^2)
    half   = z / (n + z^2) * sqrt(x(n - x)/n + z^2/4)
    interval = [centre - half, centre + half]

It stays inside [0, 1], is never zero-width, and behaves well at small n and at 0 or n successes,
where the textbook (Wald) interval collapses to a point. At x = 0 the lower end is exactly 0 and the
upper end z^2/(n + z^2); at x = n the lower end is n/(n + z^2) and the upper end exactly 1 (both by
algebra; the code returns them exactly). Floats: the interval is for display; the report compares
only its lower end with a §6 target ("not shown at 95 % confidence"), an irrational value for
0 < x, so a float's last bit cannot decide it.

**Percentile bootstrap** (for MRR, which is a mean, not a proportion, so Wilson does not apply).
Draw `resamples` samples of n questions with replacement, compute the statistic on each (H2's
`mean_reciprocal_rank`, passed in by the caller), sort, and read two order statistics: with
alpha = 1 - confidence and B resamples, the lower end is the value of rank ceil(B * alpha/2) and
the upper end the value of rank ceil(B * (1 - alpha/2)) (ranks from 1). For B = 2000 at 95 %:
ranks 50 and 1950.

- **Deterministic and recorded.** Indexes come from `random.Random(seed).random()` only, as
  `int(random() * n)`: Python guarantees that `random()` keeps its sequence for a given seed across
  versions, while `randrange`, `choices` and `sample` may change. The seed and B go into run.json,
  so the same results give the same interval on every machine. The seed is fixed before a run and
  never changed to move an interval (like the split's seed, README "If freeze refuses").
- **Exact values.** The statistic returns a `Fraction`; the ends are two of those values, exact.
- **What it assumes.** Questions are independent draws. Translations and cross-business twins share
  a `split_group`, so they are not: the interval is then too narrow (the report counts them).
- **Its limits.** At small n a percentile bootstrap covers less than its nominal 95 %, and it can
  never reach past the values observed: the ends are resampled statistics, so with every
  question's RR equal (say all 1) every resample gives that value and the interval has zero width,
  while a Wilson interval for the same hits stays wide (10 of 10: 72 % to 100 %). The report shows
  no MRR value below its minimum n, and calls a zero-width interval degenerate, pointing to the
  hit@k Wilson interval instead of printing it as a 95 % interval.
"""

import math
import random
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from fractions import Fraction
from statistics import NormalDist
from typing import Final

DEFAULT_CONFIDENCE: Final = Fraction(95, 100)


def _check_confidence(confidence: Fraction) -> None:
    if not isinstance(confidence, Fraction) or not 0 < confidence < 1:
        raise ValueError("confidence must be a Fraction strictly between 0 and 1")


def _check_count(value: int, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an int")


def z_for(confidence: Fraction) -> float:
    """The two-sided normal quantile for `confidence` (1.959964 for 95 %)."""
    _check_confidence(confidence)
    alpha = 1 - confidence
    return NormalDist().inv_cdf(float(1 - alpha / 2))


@dataclass(frozen=True)
class WilsonInterval:
    """The Wilson score interval of `successes` of `n`, as proportions in [0, 1]."""

    successes: int
    n: int
    low: float
    high: float
    confidence: Fraction


def wilson_interval(
    successes: int, n: int, confidence: Fraction = DEFAULT_CONFIDENCE
) -> WilsonInterval:
    """The Wilson score interval (module docstring). `ValueError` for n < 1 (no interval for an
    empty set), a count outside 0..n, or a non-int count (a bool is refused)."""
    _check_count(successes, "successes")
    _check_count(n, "n")
    if n < 1:
        raise ValueError("n must be at least 1: there is no interval for an empty set")
    if not 0 <= successes <= n:
        raise ValueError("successes must be between 0 and n")
    z = z_for(confidence)
    z2 = z * z
    centre = (successes + z2 / 2) / (n + z2)
    half = z / (n + z2) * math.sqrt(successes * (n - successes) / n + z2 / 4)
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return WilsonInterval(successes=successes, n=n, low=low, high=high, confidence=confidence)


def percentile_ranks(resamples: int, confidence: Fraction) -> tuple[int, int]:
    """The 1-based ranks of the lower and upper ends among `resamples` sorted values:
    ceil(B * alpha/2) (at least 1) and ceil(B * (1 - alpha/2)), computed exactly with `Fraction`."""
    _check_count(resamples, "resamples")
    if resamples < 1:
        raise ValueError("resamples must be at least 1")
    _check_confidence(confidence)
    alpha = 1 - confidence
    low = max(1, math.ceil(resamples * alpha / 2))
    high = math.ceil(resamples * (1 - alpha / 2))
    return low, high


def resample_indexes(
    n: int, resamples: int, draw: Callable[[], float]
) -> Iterator[tuple[int, ...]]:
    """`resamples` tuples of n indexes in 0..n-1, each `int(draw() * n)`, n draws per tuple."""
    for _ in range(resamples):
        yield tuple(min(n - 1, int(draw() * n)) for _ in range(n))


@dataclass(frozen=True)
class BootstrapInterval:
    low: Fraction
    high: Fraction
    n: int
    resamples: int
    seed: int
    confidence: Fraction


def bootstrap_interval[T](
    items: Sequence[T],
    statistic: Callable[[Sequence[T]], Fraction],
    *,
    seed: int,
    resamples: int,
    confidence: Fraction = DEFAULT_CONFIDENCE,
    draw: Callable[[], float] | None = None,
) -> BootstrapInterval:
    """The percentile bootstrap interval of `statistic` over `items` (module docstring).

    `statistic` gets each resample as a new list (an item may appear several times) and must
    return a `Fraction`. `draw` replaces `random.Random(seed).random` (tests script it). Raises
    `ValueError` for no items, a bad seed, resample count or confidence, or a statistic that does
    not return a `Fraction`.
    """
    if len(items) == 0:
        raise ValueError("no items: there is no interval for an empty set")
    _check_count(seed, "seed")
    low_rank, high_rank = percentile_ranks(resamples, confidence)
    draws = random.Random(seed).random if draw is None else draw  # noqa: S311 (not crypto)
    values: list[Fraction] = []
    for indexes in resample_indexes(len(items), resamples, draws):
        value = statistic([items[i] for i in indexes])
        if not isinstance(value, Fraction):
            raise ValueError("the statistic must return a Fraction")
        values.append(value)
    values.sort()
    return BootstrapInterval(
        low=values[low_rank - 1],
        high=values[high_rank - 1],
        n=len(items),
        resamples=resamples,
        seed=seed,
        confidence=confidence,
    )
