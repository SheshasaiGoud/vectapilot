"""H3 — summarise latency samples. [HAND-WRITE] piece, written by Claude at shesha's request
(2026-10-15); shesha's explain-back is still part of its acceptance.

Guide: docs/learning/m1-gateway.md, section 6 · Tests: tests/measure/test_latency.py.

`LatencySummary` is built by the team and already tested (tests/measure/test_latency_types.py).
"""

import math
import statistics
from collections.abc import Sequence
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Milliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class LatencySummary(BaseModel):
    """n, min, median, max and p95 of one set of latency samples, in milliseconds.

    Frozen and closed (`extra="forbid"`). Strict: an int becomes a float, but a bool or a string is
    never taken as a number. Every value is finite and >= 0, and a summary that contradicts itself
    cannot be built: `min_ms <= median_ms <= max_ms` and, when there is a p95,
    `median_ms <= p95_ms <= max_ms` with at least 2 samples. Errors name the rule, never a value
    (`hide_input_in_errors`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    n: int = Field(ge=1)
    min_ms: Milliseconds
    median_ms: Milliseconds
    max_ms: Milliseconds
    p95_ms: Milliseconds | None

    @model_validator(mode="after")
    def _is_consistent(self) -> Self:
        if not self.min_ms <= self.median_ms <= self.max_ms:
            raise ValueError("min_ms <= median_ms <= max_ms must hold")
        if self.p95_ms is not None:
            if self.n < 2:
                raise ValueError("a p95 needs at least 2 samples")
            if not self.median_ms <= self.p95_ms <= self.max_ms:
                raise ValueError("median_ms <= p95_ms <= max_ms must hold")
        return self


def latency_summary(samples_ms: Sequence[float], *, min_n: int = 20) -> LatencySummary:
    """Summarise latency samples, in milliseconds: n, min, median, max and p95.

    Contract:

    - **n** is the number of samples. Every sample counts once: nothing is dropped, trimmed or
      merged, so two equal samples are two samples. Choosing the samples is the caller's job
      (PR 8, ADR 0007): one per distinct prompt, and timed-out calls are never dropped.
    - **min** and **max** are the smallest and the largest sample.
    - **median** follows `statistics.median`: the middle sample when n is odd, the mean of the two
      middle samples when n is even (1, 2, ..., 12 gives 6.5).
    - **p95** is the inclusive-method 95th percentile: the last of the 19 cut points that
      `statistics.quantiles(samples, n=20, method="inclusive")` returns, clamped into
      [median, max] (see "Float rounding"). It is `None` when n < `min_n` (default 20, ADR 0007's
      rule); n equal to `min_n` gives a p95.
    - **Why inclusive, not exclusive.** The default (exclusive) method extrapolates past the data
      when n is small: for 1, 2, ..., 12 its p95 is 12.35, above the largest sample (12), and for
      the two samples 10 and 20 it is 28.5. The inclusive method interpolates between neighbouring
      samples, so it stays inside the data: 11.45 and 19.5.
    - **Float rounding.** In exact arithmetic the inclusive p95 lies between the median and the
      max. In floats its interpolation can land a few units in the last place outside: with
      Python 3.12 (2026-10-15), twenty samples of 123.456 give 123.45600000000002 (above the max)
      and three give 123.45599999999999 (below the median). Clamping into [median, max] changes the
      value by at most those few units, and equal samples give exactly that sample.
    - **Types.** Samples are real numbers: floats or ints. n is an int; every other value is a
      float, also when the samples are ints. A bool is not a latency, even though Python treats it
      as an int: it is outside this contract, and what the function does with one is unspecified
      (no test covers it).
    - **Input.** Any sequence, in any order. The result does not depend on the order, and the
      input is never modified (not sorted in place).
    - **Errors.** A plain `ValueError`, raised by this function before anything is computed (not
      a subclass such as `statistics.StatisticsError` or Pydantic's `ValidationError`), when there
      are no samples, when a sample is negative (0 is allowed), NaN or infinite, or when `min_n` is
      below 2 (a percentile of one sample is just that sample). The message names the broken rule
      and never contains a sample value.
    """
    # Check everything first; messages name the rule, never a sample value.
    if min_n < 2:
        raise ValueError("min_n must be at least 2")
    if len(samples_ms) == 0:
        raise ValueError("latency_summary needs at least one sample")
    if any(not math.isfinite(sample) or sample < 0 for sample in samples_ms):
        raise ValueError("every sample must be a finite number of milliseconds >= 0")

    ordered = sorted(float(sample) for sample in samples_ms)  # a new list: the input is untouched
    median = statistics.median(ordered)
    p95 = None
    if len(ordered) >= min_n:
        p95 = statistics.quantiles(ordered, n=20, method="inclusive")[-1]  # the 19th cut point
        p95 = min(max(p95, median), ordered[-1])  # float rounding can land just outside: clamp
    return LatencySummary(
        n=len(ordered), min_ms=ordered[0], median_ms=median, max_ms=ordered[-1], p95_ms=p95
    )
