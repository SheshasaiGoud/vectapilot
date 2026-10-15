"""Tests for `LatencySummary`, the result type of H3.

The type is built by the team (not [HAND-WRITE]), so these tests are not xfailed: they pass now.
H3's own tests are in test_latency.py.
"""

import math
from typing import Any

import pytest
from pydantic import ValidationError

from vectapilot_api.measure.latency import LatencySummary

# 1, ..., 20: n 20, min 1, median 10.5, max 20, inclusive p95 19.05.
VALID: dict[str, Any] = {"n": 20, "min_ms": 1.0, "median_ms": 10.5, "max_ms": 20.0, "p95_ms": 19.05}


def build(**changes: Any) -> LatencySummary:
    return LatencySummary.model_validate(VALID | changes)


def test_a_consistent_summary_builds() -> None:
    summary = build()

    assert summary == LatencySummary(n=20, min_ms=1.0, median_ms=10.5, max_ms=20.0, p95_ms=19.05)
    assert build(p95_ms=None).p95_ms is None


def test_the_boundaries_are_allowed() -> None:
    # All equal, from 2 samples: min == median == p95 == max is consistent.
    summary = build(n=2, min_ms=5.0, median_ms=5.0, max_ms=5.0, p95_ms=5.0)

    assert summary.p95_ms == 5.0
    assert build(min_ms=0.0).min_ms == 0.0  # 0 ms is a latency (>= 0), not an error


def test_ints_become_floats() -> None:
    summary = build(min_ms=1, median_ms=10, max_ms=20, p95_ms=19)

    for value in (summary.min_ms, summary.median_ms, summary.max_ms, summary.p95_ms):
        assert type(value) is float


def test_the_summary_is_frozen() -> None:
    summary = build()

    with pytest.raises(ValidationError, match="frozen"):
        summary.n = 21  # type: ignore[misc]


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="extra"):
        build(p99_ms=19.9)


@pytest.mark.parametrize("n", [0, -1, True, 20.0, "20"])
def test_n_is_a_positive_int(n: object) -> None:
    # Strict: a bool, a float or a string is never taken as a count.
    with pytest.raises(ValidationError):
        build(n=n)


@pytest.mark.parametrize("field", ["min_ms", "median_ms", "max_ms", "p95_ms"])
@pytest.mark.parametrize("value", [-0.5, math.nan, math.inf, -math.inf, True, "19.0"])
def test_every_latency_is_a_finite_non_negative_number(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        build(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"min_ms": 11.0}, id="min above median"),
        pytest.param({"max_ms": 10.0, "p95_ms": None}, id="median above max"),
        pytest.param({"p95_ms": 20.5}, id="p95 above max"),
        pytest.param({"p95_ms": 10.0}, id="p95 below median"),
        pytest.param(
            {"n": 1, "min_ms": 5.0, "median_ms": 5.0, "max_ms": 5.0, "p95_ms": 5.0},
            id="p95 of one sample",
        ),
    ],
)
def test_a_summary_that_contradicts_itself_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"min_ms": -1234.5}, id="field rule"),
        pytest.param({"p95_ms": 1234.5}, id="consistency rule"),
    ],
)
def test_errors_never_show_the_value(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as excinfo:
        build(**changes)

    assert "1234.5" not in str(excinfo.value)
