"""H2 tests: the router's metric functions (guide: docs/learning/m4-metrics.md).

The four functions in `intenteval/metrics.py` are [HAND-WRITE] (D4 (b): written by Claude at
shesha's request, on `m4/metrics`, 2026-11-10). Until then the module was a strict xfail
(`raises=NotImplementedError`); the bodies turned every test into XPASS(strict), and the mark was
then deleted, so every test here must simply pass.

Change the code, never a fixture: every expected value here was worked out by hand (the comments
show the arithmetic), before any code existed.

Every row is made up; no number here is a result.
"""

import math
from fractions import Fraction
from typing import Final

import pytest

from vectapilot_api.intenteval.metrics import (
    COLUMNS,
    ConfusionMatrix,
    Decided,
    ReliabilityBin,
    confusion_matrix,
    expected_calibration_error,
    macro_f1,
    reliability_bins,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

E, PJ, C, R, HR, AR, Q, OOS = DECISION_ORDER

# ── Fixture A: ten rows, one of them fail-closed ─────────────────────────────────────────────
ROWS_A: Final[tuple[tuple[Intent, Decided], ...]] = (
    (E, E),  # 1 right
    (E, PJ),  # 2 an emergency decided as professional judgement
    (E, None),  # 3 fail-closed: wrong for every intent metric
    (PJ, PJ),  # 4 right
    (C, C),  # 5 right
    (C, HR),  # 6 a complaint decided as a request for a human
    (Q, Q),  # 7 right
    (Q, Q),  # 8 right
    (Q, OOS),  # 9
    (OOS, Q),  # 10
)
GOLD_A: Final = tuple(g for g, _ in ROWS_A)
DECIDED_A: Final = tuple(d for _, d in ROWS_A)


def matrix_of(cells: dict[tuple[Intent, Decided], int]) -> ConfusionMatrix:
    return ConfusionMatrix({g: {d: cells.get((g, d), 0) for d in COLUMNS} for g in DECISION_ORDER})


MATRIX_A: Final = matrix_of(
    {(E, E): 1, (E, PJ): 1, (E, None): 1, (PJ, PJ): 1, (C, C): 1, (C, HR): 1, (Q, Q): 2}
    | {(Q, OOS): 1, (OOS, Q): 1}
)


def test_the_matrix_counts_each_row_once_with_a_fail_closed_column() -> None:
    matrix = confusion_matrix(GOLD_A, DECIDED_A)

    assert matrix == MATRIX_A
    assert matrix.decided_total(None) == 1  # the fail-closed column
    assert matrix.total == 10


def test_no_rows_is_an_all_zero_matrix() -> None:
    assert confusion_matrix((), ()) == matrix_of({})


def test_the_matrix_does_not_depend_on_row_order() -> None:
    assert confusion_matrix(GOLD_A[::-1], DECIDED_A[::-1]) == MATRIX_A


@pytest.mark.parametrize(
    ("gold", "decided"),
    [
        ((E, Q), (E,)),  # one decision short
        (("emergency",), (E,)),  # a plain string is not an Intent
        ((E,), ("question",)),
        ((None,), (E,)),  # no gold row is "fail closed"
    ],
    ids=["length", "gold-string", "decided-string", "gold-none"],
)
def test_the_matrix_refuses_what_is_not_rows_of_intents(
    gold: tuple[object, ...], decided: tuple[object, ...]
) -> None:
    with pytest.raises(ValueError):
        confusion_matrix(gold, decided)  # type: ignore[arg-type]


# By hand, per intent (TP, FP, FN; precision, recall, F1 = 2TP / (2TP + FP + FN)):
#   emergency               1, 0, 2 (rows 2 and 3)  P 1/1   R 1/3  F1 2/4 = 1/2
#   professional_judgement  1, 1 (row 2), 0         P 1/2   R 1/1  F1 2/3
#   complaint               1, 0, 1 (row 6)         P 1/1   R 1/2  F1 2/3
#   refund_or_payment       0, 0, 0                 P 0/0→0 R 0/0→0 F1 0/0→0 (the 0/0 rule)
#   human_request           0, 1 (row 6), 0         P 0/1   R 0/0→0 F1 0/1 = 0
#   action_request          0, 0, 0                 all 0 (the 0/0 rule)
#   question                2, 1 (row 10), 1 (row 9) P 2/3  R 2/3  F1 4/6 = 2/3
#   out_of_scope            0, 1 (row 9), 1 (row 10) P 0/1  R 0/1  F1 0/2 = 0
# The fail-closed row 3 is an FN of emergency and an FP of nothing.
# Macro-F1 = (1/2 + 2/3 + 2/3 + 0 + 0 + 0 + 2/3 + 0) / 8 = (5/2) / 8 = 5/16.
# (Accuracy is 5 of 10: rows 1, 4, 5, 7 and 8. Three intents have no gold row at all here, and the
# 0/0 rule scores them 0: why the report shows macro-F1 only with 5+ gold rows and 5+ split units
# of every intent.)
EXPECTED_A: Final = {
    E: (1, 0, 2, Fraction(1), Fraction(1, 3), Fraction(1, 2)),
    PJ: (1, 1, 0, Fraction(1, 2), Fraction(1), Fraction(2, 3)),
    C: (1, 0, 1, Fraction(1), Fraction(1, 2), Fraction(2, 3)),
    R: (0, 0, 0, Fraction(0), Fraction(0), Fraction(0)),
    HR: (0, 1, 0, Fraction(0), Fraction(0), Fraction(0)),
    AR: (0, 0, 0, Fraction(0), Fraction(0), Fraction(0)),
    Q: (2, 1, 1, Fraction(2, 3), Fraction(2, 3), Fraction(2, 3)),
    OOS: (0, 1, 1, Fraction(0), Fraction(0), Fraction(0)),
}


def test_macro_f1_scores_every_intent_exactly_with_the_0_0_rule() -> None:
    result = macro_f1(MATRIX_A)

    assert [s.intent for s in result.per_intent] == list(DECISION_ORDER)
    for scores in result.per_intent:
        got = (
            scores.true_positives,
            scores.false_positives,
            scores.false_negatives,
            scores.precision,
            scores.recall,
            scores.f1,
        )
        assert got == EXPECTED_A[scores.intent], scores.intent
        assert all(type(v) is Fraction for v in got[3:])  # exact, never a float
    assert result.macro_f1 == Fraction(5, 16)
    assert type(result.macro_f1) is Fraction


def test_a_perfect_matrix_scores_1_and_an_empty_one_0() -> None:
    perfect = matrix_of({(i, i): 3 for i in DECISION_ORDER})

    assert macro_f1(perfect).macro_f1 == 1
    assert macro_f1(matrix_of({})).macro_f1 == 0  # every intent 0/0: the rule, not an error


def test_fail_closed_rows_lower_recall_and_never_precision() -> None:
    # Two emergency rows: one decided right, one fail-closed. By hand: TP 1, FP 0, FN 1;
    # P 1/1, R 1/2, F1 2/3. Every other intent has no row: 0 each. Macro = (2/3) / 8 = 1/12.
    result = macro_f1(matrix_of({(E, E): 1, (E, None): 1}))

    emergency = result.per_intent[0]
    assert (emergency.precision, emergency.recall, emergency.f1) == (
        Fraction(1),
        Fraction(1, 2),
        Fraction(2, 3),
    )
    assert result.macro_f1 == Fraction(1, 12)


# ── Fixture B: nine confidences in five bins ─────────────────────────────────────────────────
CONFIDENCES_B: Final = (0.125, 0.25, 0.375, 0.5, 0.6, 0.75, 0.8, 1.0, 0.875)
CORRECT_B: Final = (True, False, True, True, False, True, True, False, True)
# By hand, bin i = min(floor(Fraction(p) * 5), 4):
#   0.125 → 0 · 0.25, 0.375 → 1 · 0.5 → 2 · 0.6 → 2 (!) · 0.75 → 3 · 0.8, 1.0, 0.875 → 4.
#   The float 0.6 is 0.59999999999999997779..., just below 3/5, so it belongs to [2/5, 3/5).
#   (A float product, int(0.6 * 5), rounds to 3.0 and would put it in bin 3: the exact rule
#   decides, not the rounding.) The float 0.8 is 0.80000000000000004440..., above 4/5: bin 4.
# Bin  n  correct  mean confidence              accuracy
#  0   1  1        0.125                        1
#  1   2  1        (0.25 + 0.375) / 2 = 0.3125  1/2
#  2   2  1        (0.5 + 0.6) / 2 = 0.55       1/2
#  3   1  1        0.75                         1
#  4   3  2        (0.8 + 1.0 + 0.875) / 3      2/3
EXPECTED_B: Final = (
    (Fraction(0), Fraction(1, 5), 1, 1, 0.125, Fraction(1)),
    (Fraction(1, 5), Fraction(2, 5), 2, 1, 0.3125, Fraction(1, 2)),
    (Fraction(2, 5), Fraction(3, 5), 2, 1, 0.55, Fraction(1, 2)),
    (Fraction(3, 5), Fraction(4, 5), 1, 1, 0.75, Fraction(1)),
    (Fraction(4, 5), Fraction(1), 3, 2, 2.675 / 3, Fraction(2, 3)),
)


def test_bins_are_equal_width_placed_exactly_with_1_in_the_last() -> None:
    bins = reliability_bins(CONFIDENCES_B, CORRECT_B)

    assert len(bins) == 5
    for got, (low, high, n, correct, mean, accuracy) in zip(bins, EXPECTED_B, strict=True):
        assert (got.low, got.high, got.n, got.correct, got.accuracy) == (
            low,
            high,
            n,
            correct,
            accuracy,
        )
        assert got.mean_confidence == pytest.approx(mean, abs=1e-12)


# Bins are closed on the left, [low, high), and only the last one also holds its high edge.
# With 5 bins, 0.0 and 1.0 are the only edges a float hits exactly (the floats 0.2 and 0.4 sit
# just above 1/5 and 2/5), so the edge rule needs a power-of-two bin count to be visible:
#   (0.0, 5) → 0: the left edge of bin 0 (a right-closed rule, ceil(p·5) - 1, would say -1).
#   (0.2, 5) → 1 and (0.4, 5) → 2: just above the edge, so the next bin up.
#   (0.25, 4) → 1 and (0.5, 4) → 2: exact edges, and an exact edge opens the upper bin.
#   (1.0, 4) → 3: 1.0 is the high edge of the last bin, which keeps it.
@pytest.mark.parametrize(
    ("p", "bins", "index"),
    [(0.0, 5, 0), (0.2, 5, 1), (0.4, 5, 2), (0.25, 4, 1), (0.5, 4, 2), (1.0, 4, 3)],
)
def test_an_exact_edge_opens_the_upper_bin(p: float, bins: int, index: int) -> None:
    table = reliability_bins((p,), (True,), bins=bins)

    assert len(table) == bins
    assert [i for i, b in enumerate(table) if b.n] == [index]
    assert table[index].low == Fraction(index, bins)
    assert table[index].high == Fraction(index + 1, bins)


def test_four_bins_with_one_confidence_on_each_left_edge() -> None:
    # 0, 1/4, 1/2 and 3/4 are exact floats, each the low edge of its own bin.
    table = reliability_bins((0.0, 0.25, 0.5, 0.75), (True, True, False, True), bins=4)

    assert [b.n for b in table] == [1, 1, 1, 1]
    assert [b.mean_confidence for b in table] == [0.0, 0.25, 0.5, 0.75]
    assert [b.accuracy for b in table] == [Fraction(1), Fraction(1), Fraction(0), Fraction(1)]
    assert reliability_bins((0.5,), (True,), bins=2)[1].n == 1


def test_a_confidence_of_0_is_a_probability() -> None:
    # [0, 1] is closed at both ends: a top probability of 0.0 is odd but valid, and it is bin 0.
    table = reliability_bins((0.0,), (False,))

    assert [b.n for b in table] == [1, 0, 0, 0, 0]
    assert table[0].mean_confidence == 0.0
    assert table[0].accuracy == Fraction(0)


def test_int_confidences_are_accepted_and_averaged_as_floats() -> None:
    # The contract takes any finite real number in [0, 1]; the ints 0 and 1 qualify. The mean
    # stays a float (probabilities are floats; only counts are Fractions).
    table = reliability_bins((1, 0), (True, False))

    assert [(b.n, b.mean_confidence) for b in table] == [
        (1, 0.0),
        (0, None),
        (0, None),
        (0, None),
        (1, 1.0),
    ]
    assert type(table[0].mean_confidence) is float
    assert type(table[4].mean_confidence) is float


def test_an_empty_bin_has_no_mean_and_no_accuracy() -> None:
    bins = reliability_bins((0.9, 0.95), (True, False))

    assert [b.n for b in bins] == [0, 0, 0, 0, 2]
    assert all(b.mean_confidence is None and b.accuracy is None for b in bins[:4])
    assert bins[4].accuracy == Fraction(1, 2)


def test_the_bins_do_not_depend_on_row_order() -> None:
    assert reliability_bins(CONFIDENCES_B[::-1], CORRECT_B[::-1]) == reliability_bins(
        CONFIDENCES_B, CORRECT_B
    )


@pytest.mark.parametrize(
    ("confidences", "correct", "bins"),
    [
        ((0.5,), (True, False), 5),  # lengths differ
        ((1.5,), (True,), 5),
        ((-0.1,), (True,), 5),
        ((math.nan,), (True,), 5),
        ((0.5,), (1,), 5),  # an int is not a bool
        ((0.5,), (True,), 0),
        ((True,), (True,), 5),  # a bool is not a confidence, even though True == 1
        ((0.5,), (True,), True),  # a bool is not a bin count (it would mean one bin)
        ((0.5,), (True,), 2.5),  # a float bin count is a ValueError, not range()'s TypeError
    ],
    ids=[
        "length",
        "above-1",
        "negative",
        "nan",
        "not-bool",
        "no-bins",
        "bool-confidence",
        "bins-bool",
        "bins-float",
    ],
)
def test_bins_refuse_what_is_not_a_confidence_table(
    confidences: tuple[object, ...], correct: tuple[object, ...], bins: object
) -> None:
    with pytest.raises(ValueError):
        reliability_bins(confidences, correct, bins=bins)  # type: ignore[arg-type]


# ECE by hand (N = 9): Σ (n_b / N) · |accuracy_b - mean_b| = (1/N) Σ |correct_b - Σ confidences_b|
#   bin 0: |1 - 0.125| = 0.875 · bin 1: |1 - 0.625| = 0.375 · bin 2: |1 - 1.1| = 0.1
#   bin 3: |1 - 0.75| = 0.25 · bin 4: |2 - 2.675| = 0.675
#   ECE = (0.875 + 0.375 + 0.1 + 0.25 + 0.675) / 9 = 2.275 / 9 = 0.25277...
def test_ece_weights_each_bins_gap_by_its_share_of_rows() -> None:
    bins = tuple(
        ReliabilityBin(low, high, n, correct, mean, accuracy)
        for low, high, n, correct, mean, accuracy in EXPECTED_B
    )

    assert expected_calibration_error(bins) == pytest.approx(2.275 / 9, abs=1e-12)


def test_ece_skips_empty_bins_and_refuses_no_rows() -> None:
    one = ReliabilityBin(Fraction(4, 5), Fraction(1), 2, 1, 0.875, Fraction(1, 2))
    empty = ReliabilityBin(Fraction(0), Fraction(1, 5), 0, 0, None, None)

    assert expected_calibration_error((empty, one)) == pytest.approx(0.375, abs=1e-12)
    with pytest.raises(ValueError):
        expected_calibration_error((empty,))
