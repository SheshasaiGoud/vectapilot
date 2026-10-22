"""Wilson score interval and the seeded percentile bootstrap (`evalrun/intervals.py`).

Reference values: Newcombe, R. G. (1998), "Two-sided confidence intervals for the single
proportion: comparison of seven methods", Statistics in Medicine 17, 857-872, Table I, method 3
(the score interval without continuity correction), to the 4 decimals the table prints. Hand
derivations are in the comments.
"""

import math
import random
from collections.abc import Sequence
from fractions import Fraction
from statistics import NormalDist
from typing import Any

import pytest

from vectapilot_api.evalrun.intervals import (
    bootstrap_interval,
    percentile_ranks,
    resample_indexes,
    wilson_interval,
    z_for,
)

Z = NormalDist().inv_cdf(0.975)


def test_z_for_95_percent_is_the_normal_975_quantile() -> None:
    assert z_for(Fraction(95, 100)) == pytest.approx(1.959964, abs=1e-6)
    assert z_for(Fraction(90, 100)) == pytest.approx(1.644854, abs=1e-6)


@pytest.mark.parametrize(
    ("x", "n", "low", "high"),
    [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0000, 0.1611),
        (1, 29, 0.0061, 0.1718),
        (29, 29, 0.8830, 1.0000),
    ],
    ids=["81-of-263", "15-of-148", "0-of-20", "1-of-29", "29-of-29"],
)
def test_wilson_matches_newcombe_1998_table_1(x: int, n: int, low: float, high: float) -> None:
    interval = wilson_interval(x, n)

    assert (round(interval.low, 4), round(interval.high, 4)) == (low, high)


@pytest.mark.parametrize("n", [1, 9, 20, 263])
def test_wilson_at_zero_successes_is_zero_to_z2_over_n_plus_z2(n: int) -> None:
    # x = 0: centre = (z^2/2)/(n+z^2) and half = z/(n+z^2)*sqrt(z^2/4) = (z^2/2)/(n+z^2),
    # so the lower end is exactly 0 and the upper end z^2/(n+z^2).
    interval = wilson_interval(0, n)

    assert interval.low == 0.0
    assert interval.high == pytest.approx(Z * Z / (n + Z * Z), rel=1e-12)


@pytest.mark.parametrize("n", [1, 9, 29])
def test_wilson_at_n_successes_is_n_over_n_plus_z2_to_one(n: int) -> None:
    # x = n: centre = (n + z^2/2)/(n+z^2), half = (z^2/2)/(n+z^2), so the lower end is n/(n+z^2).
    interval = wilson_interval(n, n)

    assert interval.high == 1.0
    assert interval.low == pytest.approx(n / (n + Z * Z), rel=1e-12)


def test_the_guides_example_seven_of_nine_runs_from_about_45_to_94_percent() -> None:
    # docs/learning/m2-metrics.md §2.6 ("a made-up example, not a result").
    interval = wilson_interval(7, 9)

    assert (round(interval.low, 2), round(interval.high, 2)) == (0.45, 0.94)


@pytest.mark.parametrize(("x", "n"), [(1, 9), (3, 10), (4, 12), (5, 5)])
def test_wilson_mirrors_for_failures_and_holds_the_observed_rate(x: int, n: int) -> None:
    interval, mirror = wilson_interval(x, n), wilson_interval(n - x, n)

    assert interval.low == pytest.approx(1 - mirror.high, abs=1e-12)
    assert interval.high == pytest.approx(1 - mirror.low, abs=1e-12)
    assert 0.0 <= interval.low <= x / n <= interval.high <= 1.0


def test_more_questions_at_the_same_rate_give_a_narrower_interval() -> None:
    small, large = wilson_interval(4, 8), wilson_interval(40, 80)

    assert large.high - large.low < small.high - small.low


@pytest.mark.parametrize(
    ("x", "n", "message"),
    [
        (0, 0, "n must be at least 1"),
        (3, 2, "between 0 and n"),
        (-1, 5, "between 0 and n"),
        (True, 5, "successes must be an int"),
        (1, 5.0, "n must be an int"),
    ],
    ids=["empty", "above-n", "negative", "bool", "float-n"],
)
def test_wilson_refuses_what_is_not_a_count(x: Any, n: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        wilson_interval(x, n)


@pytest.mark.parametrize("confidence", [Fraction(0), Fraction(1), 0.95])
def test_a_confidence_must_be_a_fraction_strictly_between_0_and_1(confidence: Any) -> None:
    with pytest.raises(ValueError, match="confidence"):
        wilson_interval(1, 2, confidence)


@pytest.mark.parametrize(
    ("resamples", "confidence", "ranks"),
    [
        # B*alpha/2 and B*(1 - alpha/2), rounded up, ranks from 1.
        (2000, Fraction(95, 100), (50, 1950)),
        (1000, Fraction(95, 100), (25, 975)),
        (40, Fraction(95, 100), (1, 39)),
        (39, Fraction(95, 100), (1, 39)),  # ceil(0.975) = 1, ceil(38.025) = 39
        (4, Fraction(1, 2), (1, 3)),
        (1, Fraction(95, 100), (1, 1)),
    ],
)
def test_percentile_ranks(resamples: int, confidence: Fraction, ranks: tuple[int, int]) -> None:
    assert percentile_ranks(resamples, confidence) == ranks


def test_resample_indexes_take_int_of_draw_times_n() -> None:
    draws = iter([0.1, 0.6, 0.6, 0.6, 0.1, 0.1, 0.9, 0.2, 0.999999])

    assert list(resample_indexes(2, 4, lambda: next(draws))) == [(0, 1), (1, 1), (0, 0), (1, 0)]


def test_a_scripted_bootstrap_by_hand() -> None:
    # Items [0, 1], 4 resamples from the draws above: (0,1) (1,1) (0,0) (1,0). The statistic is a
    # sum: 1, 2, 0, 1 -> sorted [0, 1, 1, 2]. At 50 %: alpha = 1/2, ranks ceil(4*1/4) = 1 and
    # ceil(4*3/4) = 3, so the interval is [0, 1].
    draws = iter([0.1, 0.6, 0.6, 0.6, 0.1, 0.1, 0.9, 0.2])
    seen: list[list[Fraction]] = []

    def total(sample: Sequence[Fraction]) -> Fraction:
        seen.append(list(sample))
        return sum(sample, Fraction(0))

    interval = bootstrap_interval(
        [Fraction(0), Fraction(1)],
        total,
        seed=1,
        resamples=4,
        confidence=Fraction(1, 2),
        draw=lambda: next(draws),
    )

    assert (interval.low, interval.high) == (Fraction(0), Fraction(1))
    assert seen == [[0, 1], [1, 1], [0, 0], [1, 0]]
    assert (interval.n, interval.resamples, interval.seed) == (2, 4, 1)


def test_the_default_draws_are_random_random_of_the_seed() -> None:
    # The contract that keeps the bootstrap reproducible across Python versions: only
    # `Random(seed).random()` is used (its sequence is guaranteed), as `int(random() * n)`.
    expected_rng = random.Random(7)  # noqa: S311
    expected = [tuple(int(expected_rng.random() * 3) for _ in range(3)) for _ in range(5)]
    rng = random.Random(7)  # noqa: S311

    assert list(resample_indexes(3, 5, rng.random)) == expected


def test_the_same_seed_gives_the_same_interval_and_another_seed_other_draws() -> None:
    items = [Fraction(i % 3, 3) for i in range(12)]

    def largest(sample: Sequence[Fraction]) -> Fraction:
        return max(sample)

    def run(seed: int) -> tuple[Fraction, Fraction]:
        interval = bootstrap_interval(items, largest, seed=seed, resamples=300)
        return interval.low, interval.high

    assert run(20261022) == run(20261022)
    first = list(resample_indexes(12, 3, random.Random(1).random))  # noqa: S311
    second = list(resample_indexes(12, 3, random.Random(2).random))  # noqa: S311
    assert first != second


def test_a_pinned_bootstrap_stays_the_same() -> None:
    # Golden values, computed once with this code on 2026-10-22 (Python 3.12): they change only if
    # the draws or the percentile rule change, which would change every recorded interval. The
    # neighbouring seed gives other values, so the pin does depend on the seed.
    items = [Fraction(i, 10) for i in range(10)]

    def total(sample: Sequence[Fraction]) -> Fraction:
        return sum(sample, Fraction(0))

    pinned = bootstrap_interval(items, total, seed=20261022, resamples=1000)
    neighbour = bootstrap_interval(items, total, seed=20261023, resamples=1000)

    assert (pinned.low, pinned.high) == (Fraction(27, 10), Fraction(31, 5))
    assert (neighbour.low, neighbour.high) == (Fraction(13, 5), Fraction(63, 10))


def test_a_constant_statistic_gives_a_zero_width_interval() -> None:
    interval = bootstrap_interval([1, 2, 3], lambda sample: Fraction(5, 7), seed=3, resamples=50)

    assert (interval.low, interval.high) == (Fraction(5, 7), Fraction(5, 7))


@pytest.mark.parametrize(
    ("items", "statistic", "kwargs", "message"),
    [
        ([], lambda s: Fraction(0), {"seed": 1, "resamples": 10}, "no items"),
        ([1], lambda s: Fraction(0), {"seed": 1, "resamples": 0}, "resamples must be at least"),
        ([1], lambda s: Fraction(0), {"seed": True, "resamples": 10}, "seed must be an int"),
        ([1], lambda s: 0.5, {"seed": 1, "resamples": 10}, "must return a Fraction"),
    ],
    ids=["empty", "no-resamples", "bool-seed", "float-statistic"],
)
def test_the_bootstrap_refuses_bad_input(
    items: list[int], statistic: Any, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        bootstrap_interval(items, statistic, **kwargs)


def test_wilson_is_not_wald() -> None:
    # Wald at 0 of 20 is [0, 0], a point; Wilson is never zero-width.
    interval = wilson_interval(0, 20)

    assert interval.high > 0
    assert math.isclose(interval.high, 0.16112515805281927, rel_tol=1e-9)
