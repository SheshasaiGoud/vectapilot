"""The team-built parts of `extracteval/metrics.py` (passing now): H2's input and result types check
themselves when built, so an impossible expectation in test_metrics.py fails even while the stubs
stand, and the bundle wires the five functions. Every value is made up."""

from typing import Any

import pytest

from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.extracteval import metrics
from vectapilot_api.extracteval.metrics import (
    H2_EXTRACT_METRICS,
    Accuracy,
    DocumentCount,
    FieldCount,
    ReviewPrecision,
    ScoredField,
    SilentErrors,
)


def scored(**changes: Any) -> ScoredField:
    base: dict[str, Any] = {
        "document": "d1",
        "business_id": "b-one",
        "field": "name",
        "scored": True,
        "document_status": "complete",
        "status": "accepted",
        "level": "high",
        "reasons": (),
        "has_value": True,
        "gold_null": False,
        "correct": True,
    }
    return ScoredField(**(base | changes))


def test_a_scored_field_and_its_key() -> None:
    assert scored().key == "d1/name"
    not_run = scored(
        scored=False, document_status=None, status=None, level=None, has_value=False, correct=None
    )
    assert not_run.key == "d1/name"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": None}, "a scored field has a status"),
        ({"document_status": None}, "a scored field has a status"),
        ({"correct": None}, "a scored field has a status"),
        ({"scored": False}, "not scored has no status"),
        (
            {"scored": False, "status": None, "document_status": None, "correct": None},
            "not scored has no status",  # has_value is still True
        ),
        ({"status": "accept"}, "status is accepted, review or absent"),
        ({"document_status": "done"}, "document_status is complete or needs_review"),
        ({"level": "certain"}, "level is high, medium or low"),
        ({"reasons": ("unsure", "not_found")}, "reasons are sorted"),
        ({"reasons": ("unsure", "unsure")}, "reasons are sorted"),
    ],
)
def test_an_impossible_scored_field_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        scored(**changes)


def test_the_results_accept_what_the_rules_allow() -> None:
    Accuracy(correct=1, n=2, wrong=("d1/born",), not_scored=("d0", "d9"))
    FieldCount(k=0, n=0, fields=())
    DocumentCount(x=1, n=5, documents=("d1",), interval=wilson_interval(1, 5))
    DocumentCount(x=0, n=0, documents=(), interval=None)


@pytest.mark.parametrize(
    "bad",
    [
        {"correct": 3, "n": 2, "wrong": (), "not_scored": ()},
        {"correct": True, "n": 1, "wrong": (), "not_scored": ()},
        {"correct": 1, "n": 2, "wrong": (), "not_scored": ()},
        {"correct": 0, "n": 2, "wrong": ("d2/a", "d1/a"), "not_scored": ()},
        {"correct": 1, "n": 1, "wrong": (), "not_scored": ("d9", "d0")},
    ],
)
def test_accuracy_checks_itself(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Accuracy(**bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"k": 1, "n": 0, "fields": ("d1/a",)},
        {"k": 1, "n": 2, "fields": ()},
        {"k": 2, "n": 2, "fields": ("d1/a", "d1/a")},
        {"k": -1, "n": 2, "fields": ()},
    ],
)
def test_field_count_checks_itself(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        FieldCount(**bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"x": 0, "n": 0, "documents": (), "interval": wilson_interval(0, 1)},
        {"x": 1, "n": 5, "documents": ("d1",), "interval": None},
        {"x": 1, "n": 5, "documents": ("d1",), "interval": wilson_interval(2, 5)},
        {"x": 1, "n": 5, "documents": (), "interval": wilson_interval(1, 5)},
    ],
)
def test_document_count_carries_the_wilson_interval_of_x_of_n(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        DocumentCount(**bad)


def test_silent_errors_name_the_same_documents_twice() -> None:
    documents = DocumentCount(x=1, n=3, documents=("d2",), interval=wilson_interval(1, 3))
    SilentErrors(FieldCount(k=2, n=9, fields=("d2/insurer", "d2/name")), documents)
    with pytest.raises(ValueError, match="exactly those of the silent fields"):
        SilentErrors(FieldCount(k=1, n=9, fields=("d3/name",)), documents)


def test_review_precision_checks_itself() -> None:
    ReviewPrecision(
        wrong=1,
        n=2,
        fields=("d4/pet",),
        without_value=4,
        by_reason=(("image_page", 1), ("label_present", 1), ("truncated", 3)),
        null_gold=2,
    )
    good: dict[str, Any] = {
        "wrong": 0,
        "n": 0,
        "fields": (),
        "without_value": 1,
        "by_reason": (("not_found", 1),),
        "null_gold": 0,
    }
    ReviewPrecision(**good)
    for change in (
        {"null_gold": 2},
        {"by_reason": (("truncated", 1), ("not_found", 1))},
        {"by_reason": (("not_found", 0),)},
        {"by_reason": (("not_found", 2),)},
        {"by_reason": ()},
        {"without_value": 0, "null_gold": 0},
        {"wrong": 1, "fields": ()},
    ):
        with pytest.raises(ValueError):
            ReviewPrecision(**(good | change))


def test_the_bundle_wires_the_five_functions() -> None:
    assert H2_EXTRACT_METRICS.field_accuracy is metrics.field_accuracy
    assert H2_EXTRACT_METRICS.silent_errors is metrics.silent_errors
    assert H2_EXTRACT_METRICS.review_rate is metrics.review_rate
    assert H2_EXTRACT_METRICS.review_precision is metrics.review_precision
    assert H2_EXTRACT_METRICS.straight_through is metrics.straight_through
