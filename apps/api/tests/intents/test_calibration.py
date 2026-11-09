"""M4 PR 3: temperature scaling fitted on dev by NLL (`intents/calibration.py`, ADR 0015 dec. 5).

The exact cases are built so that the NLL's minimiser is known in closed form: with every row's
logits `(a, 0, ..., 0)` and a share q of rows labelled with the first class, the fitted softmax
must give that class probability q, so `e^(a/T) / (e^(a/T) + K - 1) = q` and
`T = a / ln(q (K - 1) / (1 - q))`. Every number here is synthetic.
"""

import math
import random
from collections.abc import Callable, Sequence
from typing import Final

import pytest

from vectapilot_api.intents import calibration
from vectapilot_api.intents.calibration import (
    SEARCH_TOLERANCE,
    TEMPERATURE_MAX,
    TEMPERATURE_MIN,
    fit_temperature,
    mean_nll,
)
from vectapilot_api.intents.classifier import softmax

RECOVERY_TOLERANCE: Final = 1e-6


def test_recovers_a_known_temperature_with_two_classes() -> None:
    # Logits (2 ln 3, 0); 3 of 4 rows are class 0: q = 3/4, K = 2 -> T = 2 ln 3 / ln 3 = 2.
    a = 2 * math.log(3)
    fit = fit_temperature([[a, 0.0]] * 4, [0, 0, 0, 1])

    assert fit.temperature == pytest.approx(2.0, rel=RECOVERY_TOLERANCE)
    assert fit.at_bound is None
    assert fit.rows == 4


def test_recovers_a_known_temperature_below_1_with_eight_classes() -> None:
    # K = 8, q = 7/10: e^(a/T) = 0.7 * 7 / 0.3; with a = 0.5 * ln(49/3), T = 0.5.
    a = 0.5 * math.log(0.7 * 7 / 0.3)
    logits = [[a] + [0.0] * 7 for _ in range(10)]
    labels = [0] * 7 + [3, 5, 7]  # which wrong class does not matter: their logits are equal

    fit = fit_temperature(logits, labels)

    assert fit.temperature == pytest.approx(0.5, rel=RECOVERY_TOLERANCE)
    # At the fit the first class's probability is q = 0.7, as the closed form says.
    assert softmax(logits[0], fit.temperature)[0] == pytest.approx(0.7, rel=1e-6)
    assert fit.nll < fit.nll_at_1


def test_recovers_the_temperature_that_generated_seeded_labels() -> None:
    # Labels drawn from softmax(z / 1.7) with a fixed seed: the fit lands near 1.7 (sampling
    # noise at n = 1500 is a few per cent; the bound below is far wider than that).
    rng = random.Random(20261109)  # noqa: S311 (seeded test data, not crypto)
    true_t = 1.7
    logits = [[rng.gauss(0.0, 3.0) for _ in range(8)] for _ in range(1500)]
    labels = [rng.choices(range(8), weights=softmax(row, true_t))[0] for row in logits]

    fit = fit_temperature(logits, labels)

    assert abs(fit.temperature - true_t) / true_t < 0.1
    assert fit.at_bound is None


def test_the_fit_is_the_minimum_over_a_grid_of_temperatures() -> None:
    rng = random.Random(7)  # noqa: S311 (seeded test data, not crypto)
    logits = [[rng.uniform(-4, 4) for _ in range(5)] for _ in range(60)]
    labels = [rng.randrange(5) for _ in logits]

    fit = fit_temperature(logits, labels)
    grid = [TEMPERATURE_MIN * (TEMPERATURE_MAX / TEMPERATURE_MIN) ** (i / 200) for i in range(201)]

    assert fit.nll == mean_nll(logits, labels, fit.temperature)
    assert all(fit.nll <= mean_nll(logits, labels, t) + 1e-12 for t in grid)
    assert fit.nll_at_1 == mean_nll(logits, labels, 1.0)


def test_every_row_right_with_a_margin_hits_the_lower_bound_and_says_so() -> None:
    # The NLL keeps falling as T shrinks: no interior minimum. Reported, never hidden.
    fit = fit_temperature([[3.0, 0.0, -1.0], [0.0, 2.0, 1.0]], [0, 1])

    assert fit.temperature == TEMPERATURE_MIN
    assert fit.at_bound == "lower"


def test_every_row_confidently_wrong_hits_the_upper_bound_and_says_so() -> None:
    fit = fit_temperature([[5.0, 0.0], [0.0, 5.0]], [1, 0])

    assert fit.temperature == TEMPERATURE_MAX
    assert fit.at_bound == "upper"


def test_rows_whose_logits_all_tie_give_t_1_and_the_nll_of_a_uniform_guess() -> None:
    fit = fit_temperature([[0.5, 0.5, 0.5, 0.5], [-2.0, -2.0, -2.0, -2.0]], [0, 3])

    assert (fit.temperature, fit.at_bound) == (1.0, None)
    assert fit.nll == fit.nll_at_1 == pytest.approx(math.log(4), rel=1e-15)


def test_the_fit_is_deterministic_and_does_not_depend_on_the_row_order() -> None:
    rng = random.Random(11)  # noqa: S311 (seeded test data, not crypto)
    rows = [([rng.uniform(-3, 3) for _ in range(8)], rng.randrange(8)) for _ in range(80)]
    shuffled = rows[:]
    random.Random(12).shuffle(shuffled)  # noqa: S311 (seeded test data, not crypto)

    first = fit_temperature([r for r, _ in rows], [y for _, y in rows])
    again = fit_temperature([r for r, _ in rows], [y for _, y in rows])
    reordered = fit_temperature([r for r, _ in shuffled], [y for _, y in shuffled])

    assert first == again == reordered  # fsum: the exactly rounded sum, whatever the order


def test_the_fit_never_changes_the_argmax() -> None:
    rng = random.Random(3)  # noqa: S311 (seeded test data, not crypto)
    logits = [[rng.uniform(-3, 3) for _ in range(8)] for _ in range(40)]
    labels = [rng.randrange(8) for _ in logits]
    fit = fit_temperature(logits, labels)

    for row in logits:
        raw, calibrated = softmax(row, 1.0), softmax(row, fit.temperature)
        assert raw.index(max(raw)) == calibrated.index(max(calibrated))


def test_the_bounds_are_ordered_and_the_tolerance_is_tight() -> None:
    assert 0 < TEMPERATURE_MIN < 1 < TEMPERATURE_MAX
    # The documented sanity range, a factor of 20 either way; the artifact enforces it too.
    # Changing it is an ADR 0015 decision 5 change, not a tweak.
    assert (TEMPERATURE_MIN, TEMPERATURE_MAX) == (0.05, 20.0)
    assert SEARCH_TOLERANCE <= 1e-6


def test_on_equal_nll_values_the_search_keeps_the_smaller_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An objective that is flat for T in [1/e, e] and rises outside it: every T on the plateau
    # is a minimum, so only the tie rule decides. "Keep the smaller T" walks to the plateau's
    # lower edge, 1/e. (A flat objective, not real rows: on real rows the ties sit on a
    # floating-point plateau, where libm differences between platforms could flip them.)
    def plateau(logits: object, labels: object, temperature: float) -> float:
        return max(0.0, abs(math.log(temperature)) - 1.0)

    monkeypatch.setattr(calibration, "_mean_nll", plateau)

    fit = fit_temperature([[1.0, 0.0]], [0])

    assert fit.temperature == pytest.approx(math.exp(-1), rel=1e-6)
    assert (fit.nll, fit.at_bound) == (0.0, None)


def test_mean_nll_sums_its_terms_exactly_whatever_the_row_order() -> None:
    # A row (a, 0) labelled 1 has the term a exactly for a >= 37 (e^-a vanishes next to 1), and
    # (0, 35) labelled 1 a tiny term (about 6.7e-16). The terms 2^60, 64, 64 and the tiny one sum
    # to just above 2^60 + 128, the midpoint between two floats (their spacing is 256 there), so
    # the exactly rounded total is 2^60 + 256. Python 3.12's built-in sum compensates (Neumaier),
    # but its correction 128 + 6.7e-16 rounds to 128, an exact tie, and gives 2^60: only fsum's
    # single rounding gets the documented "exactly rounded, order-free" sum.
    rows = [[2.0**60, 0.0], [64.0, 0.0], [64.0, 0.0], [0.0, 35.0]]
    labels = [1, 1, 1, 1]
    terms = [mean_nll([row], [label], 1.0) for row, label in zip(rows, labels, strict=True)]

    forward = mean_nll(rows, labels, 1.0)
    backward = mean_nll(rows[::-1], labels[::-1], 1.0)

    assert terms[:3] == [2.0**60, 64.0, 64.0] and 0 < terms[3] < 1e-15
    assert forward == backward == math.fsum(terms) / 4 == (2.0**60 + 256) / 4


def test_mean_nll_by_hand_and_with_huge_logits() -> None:
    # Two equal logits: -ln(1/2) = ln 2 for any T.
    assert mean_nll([[0.0, 0.0]], [0], 3.0) == pytest.approx(math.log(2), rel=1e-15)
    # Huge logits stay finite: the gold class certain gives 0, the other 1e300 / T.
    assert mean_nll([[1e300, 0.0]], [0], 1.0) == 0.0
    assert mean_nll([[1e300, 0.0]], [1], 2.0) == pytest.approx(5e299, rel=1e-12)


@pytest.mark.parametrize(
    ("logits", "labels", "message"),
    [
        ([], [], "at least one row"),
        ([[1.0, 0.0]], [0, 1], "one label per row"),
        ([[1.0]], [0], "at least two classes"),
        ([[1.0, 0.0], [1.0, 0.0, 2.0]], [0, 0], "row 1 has another number"),
        ([[1.0, math.nan]], [0], "row 0 holds a logit that is not finite"),
        ([[1.0, math.inf]], [0], "row 0 holds a logit that is not finite"),
        ([[1.0, True]], [0], "row 0 holds a value that is not a number"),
        ([[1.0, 0.0]], [2], "the label of row 0"),
        ([[1.0, 0.0]], [-1], "the label of row 0"),
        ([[1.0, 0.0]], [True], "the label of row 0"),
    ],
    ids=[
        "empty",
        "label count",
        "one class",
        "ragged",
        "nan",
        "inf",
        "bool logit",
        "label too large",
        "negative label",
        "bool label",
    ],
)
@pytest.mark.parametrize(
    "entry",
    [fit_temperature, lambda logits, labels: mean_nll(logits, labels, 1.0)],
    ids=["fit_temperature", "mean_nll"],
)
def test_unusable_rows_are_refused(
    entry: Callable[[Sequence[Sequence[float]], Sequence[int]], object],
    logits: list[list[float]],
    labels: list[int],
    message: str,
) -> None:
    # Both public entry points refuse: PR 4's calibrate and reports call mean_nll directly.
    with pytest.raises(ValueError, match=message):
        entry(logits, labels)


@pytest.mark.parametrize("temperature", [0.0, -1.0, math.inf, math.nan])
def test_mean_nll_refuses_a_temperature_that_is_not_positive_and_finite(
    temperature: float,
) -> None:
    with pytest.raises(ValueError, match="temperature"):
        mean_nll([[1.0, 0.0]], [0], temperature)
