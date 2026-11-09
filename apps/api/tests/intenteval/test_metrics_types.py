"""H2 scaffold: the result types around the metric functions, built by the team (these pass now).

They check their own shape, so H2's tests can build every expected value before the call and an
impossible result cannot leave a metric function. They hold values; the arithmetic is H2's.
"""

from fractions import Fraction
from typing import Any

import pytest

from vectapilot_api.intenteval import metrics
from vectapilot_api.intenteval.metrics import (
    COLUMNS,
    H2_INTENT_METRICS,
    RELIABILITY_BINS,
    ClassScores,
    ConfusionMatrix,
    MacroF1,
    ReliabilityBin,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent


def zeros() -> dict[Intent, dict[Intent | None, int]]:
    return {g: dict.fromkeys(COLUMNS, 0) for g in DECISION_ORDER}


def test_columns_are_the_intents_then_fail_closed() -> None:
    assert (*DECISION_ORDER, None) == COLUMNS
    assert RELIABILITY_BINS == 5


def test_a_matrix_reads_cells_and_totals_and_is_read_only() -> None:
    cells = zeros()
    cells[Intent.EMERGENCY][Intent.EMERGENCY] = 2
    cells[Intent.EMERGENCY][None] = 1
    cells[Intent.QUESTION][Intent.EMERGENCY] = 1
    matrix = ConfusionMatrix(cells)
    cells[Intent.QUESTION][Intent.QUESTION] = 9  # the caller's dict, changed afterwards

    assert matrix.cell(Intent.EMERGENCY, None) == 1
    assert matrix.gold_total(Intent.EMERGENCY) == 3
    assert matrix.decided_total(Intent.EMERGENCY) == 3
    assert matrix.decided_total(None) == 1
    assert matrix.total == 4
    with pytest.raises(TypeError):
        matrix.counts[Intent.QUESTION][Intent.QUESTION] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "change",
    ["row-order", "missing-row", "missing-fail-closed", "negative", "bool", "float"],
)
def test_a_matrix_of_the_wrong_shape_is_refused(change: str) -> None:
    cells: dict[Any, dict[Any, Any]] = zeros()
    if change == "row-order":
        cells = dict(reversed(cells.items()))
    elif change == "missing-row":
        del cells[Intent.OUT_OF_SCOPE]
    elif change == "missing-fail-closed":
        del cells[Intent.QUESTION][None]
    else:
        cells[Intent.QUESTION][Intent.QUESTION] = {"negative": -1, "bool": True, "float": 1.0}[
            change
        ]
    with pytest.raises(ValueError):
        ConfusionMatrix(cells)


def scores(intent: Intent = Intent.QUESTION, **changes: Any) -> ClassScores:
    values: dict[str, Any] = {
        "intent": intent,
        "true_positives": 2,
        "false_positives": 1,
        "false_negatives": 1,
        "precision": Fraction(2, 3),
        "recall": Fraction(2, 3),
        "f1": Fraction(2, 3),
    }
    return ClassScores(**(values | changes))


def test_class_scores_hold_counts_and_exact_fractions() -> None:
    assert scores().support == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"precision": 0.5},  # a float: never
        {"recall": Fraction(3, 2)},
        {"f1": Fraction(-1, 2)},
        {"true_positives": -1},
        {"false_negatives": True},
    ],
    ids=["float", "above-1", "negative", "negative-count", "bool-count"],
)
def test_class_scores_refuse_floats_and_impossible_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        scores(**changes)


def test_macro_f1_holds_every_intent_in_decision_order() -> None:
    per_intent = tuple(scores(i) for i in DECISION_ORDER)

    assert MacroF1(per_intent, Fraction(2, 3)).macro_f1 == Fraction(2, 3)
    with pytest.raises(ValueError, match="decision order"):
        MacroF1(per_intent[1:], Fraction(2, 3))
    with pytest.raises(ValueError, match="decision order"):
        MacroF1(per_intent[::-1], Fraction(2, 3))
    with pytest.raises(ValueError, match="exact Fraction"):
        MacroF1(per_intent, 0.5)  # type: ignore[arg-type]


def test_reliability_bins_check_themselves() -> None:
    ReliabilityBin(Fraction(4, 5), Fraction(1), 2, 1, 0.875, Fraction(1, 2))
    ReliabilityBin(Fraction(0), Fraction(1, 5), 0, 0, None, None)


@pytest.mark.parametrize(
    "fields",
    [
        (Fraction(0), Fraction(1, 5), 0, 0, 0.0, None),  # empty with a mean
        (Fraction(0), Fraction(1, 5), 0, 0, None, Fraction(0)),  # empty with an accuracy
        (Fraction(0), Fraction(1, 5), 2, 3, 0.1, Fraction(3, 2)),  # correct > n
        (Fraction(0), Fraction(1, 5), 2, 1, 0.1, Fraction(1, 3)),  # accuracy not correct/n
        (Fraction(0), Fraction(1, 5), 2, 1, 1.5, Fraction(1, 2)),  # mean above 1
        (Fraction(1, 5), Fraction(1, 5), 1, 1, 0.2, Fraction(1)),  # no width
        (0.0, Fraction(1, 5), 1, 1, 0.1, Fraction(1)),  # a float edge
    ],
    ids=["empty-mean", "empty-acc", "correct-above-n", "acc", "mean", "width", "float-edge"],
)
def test_impossible_bins_are_refused(fields: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError):
        ReliabilityBin(*fields)


def test_the_harness_calls_h2_through_the_bundle() -> None:
    assert H2_INTENT_METRICS.confusion_matrix is metrics.confusion_matrix
    assert H2_INTENT_METRICS.macro_f1 is metrics.macro_f1
    assert H2_INTENT_METRICS.reliability_bins is metrics.reliability_bins
    assert H2_INTENT_METRICS.expected_calibration_error is metrics.expected_calibration_error
