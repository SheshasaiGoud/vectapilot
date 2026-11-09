"""Temperature scaling (ADR 0015 decision 5, M4 PR 3): one temperature T, fitted on dev.

The classifier's probabilities are `softmax(logits / T)`. A logistic regression fitted on about 400
agent-drafted rows is usually mis-calibrated on human-written messages (too sure, or not sure
enough), and the router's threshold τ means "this sure"; so its probabilities must mean what they
say. One parameter is fitted on dev by **negative log-likelihood** (the mean of `-log p(gold)`):

- **Why one temperature:** a single number cannot overfit about 80 dev rows the way per-class
  Platt or isotonic calibration can, and dividing every logit by the same T > 0 never changes the
  argmax, so calibration changes confidence, never the predicted intent.
- **Why a bounded one-dimensional search, no library:** the mean NLL is convex in 1/T (a log-sum-exp
  of linear functions of 1/T minus a linear one), so it is unimodal in T and in log T. A
  golden-section search over log T between `TEMPERATURE_MIN` and `TEMPERATURE_MAX` finds the
  minimum with the standard library only, deterministically (no random start, a fixed tie rule).
- **Why bounds, and why a fit at a bound is reported:** if every dev row is right with a margin,
  the NLL keeps falling as T shrinks and has no interior minimum; if every row is confidently
  wrong, it keeps falling as T grows. Then the data cannot place T, and `TemperatureFit.at_bound`
  says so instead of hiding it. The bounds are a sanity range (a factor of 20 either way), not a
  prior.
- **Deterministic and order-free:** each row's term is computed on its own and the terms are
  summed with `math.fsum` (the exactly rounded sum), so the fit does not depend on the row order.

No numpy, no scipy, no logging; rows are numbers only, never text.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

TEMPERATURE_MIN: Final = 0.05
TEMPERATURE_MAX: Final = 20.0
"""The search range for T. `ClassifierArtifact` refuses a temperature outside it."""

SEARCH_TOLERANCE: Final = 1e-9
"""The search stops when the bracket on log T is narrower than this (T to about 1e-9, relative)."""

_INVERSE_PHI: Final = (math.sqrt(5) - 1) / 2

Bound = Literal["lower", "upper"]


@dataclass(frozen=True)
class TemperatureFit:
    """The fitted temperature and what it did to the dev NLL (both means over `rows` rows)."""

    temperature: float
    nll: float
    """The mean NLL at `temperature`."""
    nll_at_1: float
    """The mean NLL of the raw probabilities (T = 1), for the raw-against-calibrated comparison."""
    rows: int
    at_bound: Bound | None
    """`lower` or `upper` when the minimum lies on a bound of the search range: the data cannot
    place T there (see the module docstring). None for an interior minimum."""


def mean_nll(logits: Sequence[Sequence[float]], labels: Sequence[int], temperature: float) -> float:
    """The mean of `-log softmax(row / temperature)[label]` over the rows, computed stably:
    `log Σ exp((z_j - max z) / T) - (z_label - max z) / T`, so no exponent is ever positive."""
    _check(logits, labels)
    if not (math.isfinite(temperature) and temperature > 0):
        raise ValueError("temperature must be a finite number > 0")
    return _mean_nll(logits, labels, temperature)


def fit_temperature(logits: Sequence[Sequence[float]], labels: Sequence[int]) -> TemperatureFit:
    """The temperature in [`TEMPERATURE_MIN`, `TEMPERATURE_MAX`] that minimises the mean NLL of
    `labels` (each the index of the gold class in its row of `logits`).

    Every row has the same number of classes (at least 2) and finite logits; there is at least one
    row. If every row's logits are all equal, every T gives the same NLL (log K): the answer is
    T = 1, nothing to calibrate. On equal NLL values the search keeps the smaller T, so the result
    is the same on every run.
    """
    _check(logits, labels)
    nll_at_1 = _mean_nll(logits, labels, 1.0)
    if all(max(row) == min(row) for row in logits):
        return TemperatureFit(1.0, nll_at_1, nll_at_1, len(logits), None)

    def f(log_t: float) -> float:
        return _mean_nll(logits, labels, math.exp(log_t))

    lo, hi = math.log(TEMPERATURE_MIN), math.log(TEMPERATURE_MAX)
    a, b = lo, hi
    c, d = b - _INVERSE_PHI * (b - a), a + _INVERSE_PHI * (b - a)
    fc, fd = f(c), f(d)
    while b - a > SEARCH_TOLERANCE:
        if fc <= fd:  # the minimum is in [a, d]; a tie keeps the smaller T
            b, d, fd = d, c, fc
            c = b - _INVERSE_PHI * (b - a)
            fc = f(c)
        else:  # the minimum is in [c, b]
            a, c, fc = c, d, fd
            d = a + _INVERSE_PHI * (b - a)
            fd = f(d)
    middle = (a + b) / 2
    # The bounds themselves are checked exactly: a monotone NLL has its minimum there.
    candidates = [(f(lo), 0, lo), (f(middle), 1, middle), (f(hi), 2, hi)]
    best_nll, which, log_t = min(candidates)
    at_bound: Bound | None = None
    if which == 0 or middle - lo <= SEARCH_TOLERANCE:
        at_bound = "lower"
    elif which == 2 or hi - middle <= SEARCH_TOLERANCE:
        at_bound = "upper"
    temperature = {0: TEMPERATURE_MIN, 2: TEMPERATURE_MAX}.get(which, math.exp(log_t))
    return TemperatureFit(temperature, best_nll, nll_at_1, len(logits), at_bound)


def _mean_nll(
    logits: Sequence[Sequence[float]], labels: Sequence[int], temperature: float
) -> float:
    terms: list[float] = []
    for row, label in zip(logits, labels, strict=True):
        top = max(row)
        shifted = [(z - top) / temperature for z in row]  # every value <= 0: exp never overflows
        terms.append(math.log(math.fsum(math.exp(s) for s in shifted)) - shifted[label])
    return math.fsum(terms) / len(terms)


def _check(logits: Sequence[Sequence[float]], labels: Sequence[int]) -> None:
    """Refuse what would make the NLL meaningless. Messages name positions, never values."""
    if not logits:
        raise ValueError("calibration needs at least one row")
    if len(logits) != len(labels):
        raise ValueError("there must be exactly one label per row of logits")
    width = len(logits[0])
    if width < 2:
        raise ValueError("every row needs at least two classes")
    for index, (row, label) in enumerate(zip(logits, labels, strict=True)):
        if len(row) != width:
            raise ValueError(f"row {index} has another number of classes than row 0")
        if not all(isinstance(z, float | int) and not isinstance(z, bool) for z in row):
            raise ValueError(f"row {index} holds a value that is not a number")
        if not all(math.isfinite(z) for z in row):
            raise ValueError(f"row {index} holds a logit that is not finite")
        if isinstance(label, bool) or not isinstance(label, int) or not 0 <= label < width:
            raise ValueError(f"the label of row {index} is not a class index")
