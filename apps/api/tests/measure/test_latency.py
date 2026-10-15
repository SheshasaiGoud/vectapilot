"""H3 tests: `latency_summary` (guide: docs/learning/m1-gateway.md, section 6).

`latency_summary` is implemented, so the module's `xfail(strict=True)` marker is gone and every
test must simply pass. Do not edit the tests to make them pass; change the code.

Every expected value is worked out by hand; the working is in the comment next to it. The samples
are sorted as x[0] <= x[1] <= ... <= x[n-1]. An expected exclusive-method value in a comment is
what `statistics.quantiles`'s default method gives (Python 3.12.15, 2026-10-15): the trap these
tests guard against. Nothing here is random.
"""

import math

import pytest

from vectapilot_api.measure.latency import LatencySummary, latency_summary

# Tuples, so code that sorts its input in place cannot change them for the tests that follow;
# tests pass `list(...)` copies.
ONE_TO_12 = tuple(float(i) for i in range(1, 13))
ONE_TO_20 = tuple(float(i) for i in range(1, 21))
# 1, ..., 20 in a fixed mixed-up order (the same order on every run).
MIXED_UP = (7, 19, 3, 20, 11, 1, 16, 9, 14, 5, 18, 2, 13, 8, 17, 4, 12, 10, 15, 6)
SHUFFLED_20 = tuple(float(i) for i in MIXED_UP)
# p95 is interpolated, so an equivalent formula may round the last digit differently.
TOL = 1e-9


def test_twelve_samples_have_no_p95() -> None:
    # n = 12 < 20 (the default min_n) → no p95. median = (x[5] + x[6]) / 2 = (6 + 7) / 2 = 6.5.
    # Had it been computed: inclusive 11.45; exclusive 12.35, above the max of 12.
    assert latency_summary(list(ONE_TO_12)) == LatencySummary(
        n=12, min_ms=1.0, median_ms=6.5, max_ms=12.0, p95_ms=None
    )


def test_twenty_samples_have_an_inclusive_p95() -> None:
    summary = latency_summary(list(ONE_TO_20))

    # median = (x[9] + x[10]) / 2 = (10 + 11) / 2 = 10.5
    assert (summary.n, summary.min_ms, summary.median_ms, summary.max_ms) == (20, 1.0, 10.5, 20.0)
    # Inclusive: position 0.95 * (20 - 1) = 18.05, so x[18] + 0.05 * (x[19] - x[18])
    # = 19 + 0.05 * (20 - 19) = 19.05. Exclusive gives 19.95, so this value tells them apart.
    assert summary.p95_ms == pytest.approx(19.05, abs=TOL)
    assert summary.p95_ms is not None
    assert summary.p95_ms <= summary.max_ms


def test_nineteen_samples_have_no_p95() -> None:
    # n = 19 < 20 (the default min_n): one short of ADR 0007's rule, so no p95.
    # median = x[9] = 10 (n is odd: the middle sample). Had it been computed: inclusive
    # position 0.95 * (19 - 1) = 17.1, so 18 + 0.1 * (19 - 18) = 18.1.
    assert latency_summary(list(ONE_TO_20[:19])) == LatencySummary(
        n=19, min_ms=1.0, median_ms=10.0, max_ms=19.0, p95_ms=None
    )


def test_min_n_is_the_smallest_n_with_a_p95() -> None:
    # 12 samples. min_n = 13: n < min_n → no p95.
    assert latency_summary(list(ONE_TO_12), min_n=13).p95_ms is None
    # min_n = 12: n == min_n → a p95. Position 0.95 * (12 - 1) = 10.45, so
    # x[10] + 0.45 * (x[11] - x[10]) = 11 + 0.45 * (12 - 11) = 11.45 (exclusive: 12.35 > 12).
    assert latency_summary(list(ONE_TO_12), min_n=12).p95_ms == pytest.approx(11.45, abs=TOL)


def test_the_order_of_the_samples_does_not_matter() -> None:
    assert sorted(SHUFFLED_20) == list(ONE_TO_20)  # the fixture really is 1, ..., 20

    # Same samples, same summary: n 20, min 1, median 10.5, max 20, p95 19.05.
    assert latency_summary(list(SHUFFLED_20)) == latency_summary(list(ONE_TO_20))


def test_the_input_is_not_modified() -> None:
    samples = list(SHUFFLED_20)

    latency_summary(samples)

    # Still in its mixed-up order: nothing was sorted in place.
    assert samples == list(SHUFFLED_20)


def test_a_tuple_works_like_a_list() -> None:
    # Any sequence: a tuple has no `.sort()`, and it must give the same summary.
    assert latency_summary(SHUFFLED_20) == latency_summary(list(SHUFFLED_20))


def test_equal_samples_each_count_as_a_sample() -> None:
    summary = latency_summary([5.0, 7.0, 5.0, 5.0], min_n=2)

    # Sorted: 5, 5, 5, 7, so n = 4 (not 2 distinct values), min 5, max 7,
    # median = (x[1] + x[2]) / 2 = (5 + 5) / 2 = 5.
    assert (summary.n, summary.min_ms, summary.median_ms, summary.max_ms) == (4, 5.0, 5.0, 7.0)
    # Position 0.95 * (4 - 1) = 2.85, so x[2] + 0.85 * (x[3] - x[2]) = 5 + 0.85 * 2 = 6.7
    # (exclusive: 8.5, above the max of 7).
    assert summary.p95_ms == pytest.approx(6.7, abs=TOL)


@pytest.mark.parametrize(
    ("samples", "min_n", "expected"),
    [
        # statistics.quantiles (inclusive) gives 123.45600000000002: above the max.
        pytest.param(
            [123.456] * 20,
            20,
            LatencySummary(n=20, min_ms=123.456, median_ms=123.456, max_ms=123.456, p95_ms=123.456),
            id="twenty equal",
        ),
        # statistics.quantiles (inclusive) gives 123.45599999999999: below the min and median.
        pytest.param(
            [123.456] * 3,
            2,
            LatencySummary(n=3, min_ms=123.456, median_ms=123.456, max_ms=123.456, p95_ms=123.456),
            id="three equal",
        ),
        # Median = x[1] = 123.456; statistics.quantiles (inclusive) gives 123.45599999999999,
        # below the median.
        pytest.param(
            [1.0, 123.456, 123.456],
            2,
            LatencySummary(n=3, min_ms=1.0, median_ms=123.456, max_ms=123.456, p95_ms=123.456),
            id="top two equal",
        ),
    ],
)
def test_float_rounding_never_pushes_p95_out_of_median_to_max(
    samples: list[float], min_n: int, expected: LatencySummary
) -> None:
    # In exact arithmetic p95 is 123.456 in all three; in floats the interpolation lands a few
    # units in the last place off. Clamped into [median, max], it is exactly 123.456 again.
    assert latency_summary(samples, min_n=min_n) == expected


def test_a_single_sample_has_no_p95() -> None:
    # n = 1 < min_n = 2: min, median and max are that sample; there is no p95.
    assert latency_summary([42.0], min_n=2) == LatencySummary(
        n=1, min_ms=42.0, median_ms=42.0, max_ms=42.0, p95_ms=None
    )


@pytest.mark.parametrize(
    "samples",
    [
        # inclusive 19.5 (position 0.95 → 10 + 0.95 * 10); exclusive 28.5
        pytest.param([10.0, 20.0], id="10,20"),
        # inclusive 2.9 (position 1.9 → 2 + 0.9 * 1); exclusive 3.8
        pytest.param([1.0, 2.0, 3.0], id="1,2,3"),
        # inclusive 6.7 (see the equal-samples test); exclusive 8.5
        pytest.param([5.0, 5.0, 5.0, 7.0], id="5,5,5,7"),
        # inclusive 11.45; exclusive 12.35
        pytest.param(list(ONE_TO_12), id="1..12"),
        # inclusive 19.05; exclusive 19.95 (from n = 19 even exclusive stays inside the data,
        # but only in exact arithmetic: see the next case)
        pytest.param(list(ONE_TO_20), id="1..20"),
        # inclusive 123.456 after the clamp; statistics.quantiles gives 123.45600000000002 with
        # either method, above the max
        pytest.param([123.456] * 20, id="20 x 123.456"),
    ],
)
def test_the_p95_stays_inside_the_data(samples: list[float]) -> None:
    # The exclusive-method trap: on every input here but 1..20 the default method of
    # statistics.quantiles puts p95 above the largest sample. The inclusive method, clamped,
    # never leaves [median, max]. (A p95 outside that range cannot even be built:
    # LatencySummary refuses it with a ValidationError.)
    summary = latency_summary(samples, min_n=2)

    assert summary.p95_ms is not None
    assert summary.min_ms <= summary.median_ms <= summary.p95_ms <= summary.max_ms


def test_the_p95_is_not_rounded() -> None:
    summary = latency_summary([1.0, 2.0, 3.0001, 7.1234567], min_n=2)

    # Position 0.95 * (4 - 1) = 2.85, so x[2] + 0.85 * (x[3] - x[2])
    # = 3.0001 + 0.85 * (7.1234567 - 3.0001) = 3.0001 + 0.85 * 4.1233567 = 3.0001 + 3.504853195
    # = 6.504953195 (statistics.quantiles: 6.504953195000001; exclusive: 10.215974225, above the
    # max). Rounded to 3 decimals it would be 6.505, which is 0.000047 off.
    assert summary.p95_ms == pytest.approx(6.504953195, abs=TOL)


def test_every_value_is_a_float_also_for_int_samples() -> None:
    summary = latency_summary([1, 2, 3], min_n=2)

    # statistics.median([1, 2, 3]) is the int 2; the summary holds the float 2.0.
    # p95: position 0.95 * (3 - 1) = 1.9, so 2 + 0.9 * (3 - 2) = 2.9.
    assert type(summary.n) is int
    for value in (summary.min_ms, summary.median_ms, summary.max_ms, summary.p95_ms):
        assert type(value) is float
    assert (summary.n, summary.min_ms, summary.median_ms, summary.max_ms) == (3, 1.0, 2.0, 3.0)
    assert summary.p95_ms == pytest.approx(2.9, abs=TOL)


def test_zero_is_a_valid_sample() -> None:
    # Only a negative sample is an error, and 0 ms is not negative (LatencySummary allows 0 too).
    summary = latency_summary([0.0, 1.0], min_n=2)

    # median = (x[0] + x[1]) / 2 = (0 + 1) / 2 = 0.5
    assert (summary.n, summary.min_ms, summary.median_ms, summary.max_ms) == (2, 0.0, 0.5, 1.0)
    # Position 0.95 * (2 - 1) = 0.95, so x[0] + 0.95 * (x[1] - x[0]) = 0 + 0.95 * (1 - 0) = 0.95.
    assert summary.p95_ms == pytest.approx(0.95, abs=TOL)


@pytest.mark.parametrize("samples", [pytest.param([], id="list"), pytest.param((), id="tuple")])
def test_no_samples_is_an_error(samples: list[float] | tuple[float, ...]) -> None:
    with pytest.raises(ValueError) as excinfo:
        latency_summary(samples)

    # A plain ValueError, not a subclass such as statistics.StatisticsError ...
    assert excinfo.type is ValueError
    # ... raised by latency_summary itself, with a message: not min()'s or max()'s own plain
    # ValueError (Python 3.12.15: "min() iterable argument is empty"; before 3.12:
    # "min() arg is an empty sequence").
    message = str(excinfo.value)
    assert message
    assert not message.startswith(("min()", "max()"))


@pytest.mark.parametrize(
    "samples",
    [
        pytest.param([-1234.5], id="negative"),
        pytest.param([1.0, -1234.5, 3.0], id="negative in the middle"),
        pytest.param([math.nan], id="nan"),
        pytest.param([1.0, 2.0, math.nan, 3.0], id="nan in the middle"),
        # NaN compares false with everything, so sorted() takes this list as already in order and
        # leaves the NaN where it is (deterministic in CPython): min 1, median 2 and max 4 never
        # see it, and LatencySummary would accept the result. Only a check of every sample
        # catches it.
        pytest.param([1.0, math.nan, 2.0, 3.0, 4.0], id="nan that sorting leaves in place"),
        pytest.param([1.0, math.inf], id="inf"),
        pytest.param([-math.inf, 2.0], id="minus inf"),
    ],
)
def test_a_negative_or_non_finite_sample_is_an_error(samples: list[float]) -> None:
    # n = 1 to 5 < 20, so no p95 is computed: the check must not depend on one.
    with pytest.raises(ValueError) as excinfo:
        latency_summary(samples)

    # Raised up front, before any summary is built: not LatencySummary's ValidationError.
    assert excinfo.type is ValueError


def test_the_error_message_never_contains_a_sample() -> None:
    with pytest.raises(ValueError) as excinfo:
        latency_summary([*ONE_TO_20, -1234.5])

    assert str(excinfo.value)  # it names the broken rule ...
    assert "1234.5" not in str(excinfo.value)  # ... but not the value


@pytest.mark.parametrize("min_n", [1, 0, -20])
def test_min_n_below_2_is_an_error(min_n: int) -> None:
    # Valid samples, so only min_n can be the reason.
    with pytest.raises(ValueError) as excinfo:
        latency_summary(list(ONE_TO_20), min_n=min_n)

    assert excinfo.type is ValueError
