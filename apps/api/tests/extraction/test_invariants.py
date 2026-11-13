"""M6 PR 2: the invariant check after routing (`extraction/invariants.py`; E3, E4). Team code.

Each case hands `enforce` a decision as a careless or hostile router might build it, and checks
what comes out. The policy is made up and small enough to check by hand.
"""

from typing import Any, Final

import pytest

from vectapilot_api.extraction.invariants import enforce, may_accept, may_be_absent
from vectapilot_api.extraction.routing import (
    FAILURES,
    DocumentSignals,
    DocumentStatus,
    ExtractionDecision,
    FieldDecision,
    FieldSignals,
    FieldStatus,
    Level,
    Reason,
    ReviewPolicy,
)

POLICY: Final = ReviewPolicy(
    fields=("name", "born", "insurer", "policy"),
    required=frozenset({"name", "born"}),
    always_review=frozenset({"policy"}),
)
A, R, X = FieldStatus.ACCEPTED, FieldStatus.REVIEW, FieldStatus.ABSENT
INVARIANT: Final = Reason.ROUTING_INVARIANT
LOWERING: Final = (
    "unsure",
    "from_ocr",
    "shared_value",
    "conflicting_values",
    "date_ambiguous",
    "image_on_page",
)


def ok(name: str, **changes: Any) -> FieldSignals:
    base: dict[str, Any] = {
        "has_value": True,
        "quote_found": True,
        "value_in_quote": True,
        "label_in_quote": True,
    }
    return FieldSignals(name, **(base | changes))


def missing(name: str, **changes: Any) -> FieldSignals:
    return FieldSignals(name, has_value=False, **changes)


def doc(
    *fields: FieldSignals, image: bool = False, failure: Reason | None = None
) -> DocumentSignals:
    return DocumentSignals(fields, has_image_page=image, failure=failure)


def accepted(name: str) -> FieldDecision:
    return FieldDecision(name, A, Level.HIGH)


def absent(name: str) -> FieldDecision:
    return FieldDecision(name, X)


def review(name: str, level: Level | None, *reasons: Reason) -> FieldDecision:
    return FieldDecision(name, R, level, frozenset(reasons))


GOOD_SIGNALS: Final = doc(ok("name"), ok("born"), missing("insurer"), ok("policy"))
GOOD_DECISION: Final = ExtractionDecision.of(
    [
        accepted("name"),
        accepted("born"),
        absent("insurer"),
        review("policy", Level.HIGH, Reason.ALWAYS_REVIEW),
    ]
)


def test_a_justified_decision_passes_unchanged() -> None:
    checked = enforce(GOOD_DECISION, GOOD_SIGNALS, POLICY)
    assert checked.decision == GOOD_DECISION
    assert (checked.corrected, checked.dropped) == ((), 0)


@pytest.mark.parametrize(
    "signals",
    [
        ok("name", quote_found=False),
        ok("name", value_in_quote=False),
        ok("name", label_in_quote=False),
        ok("name", codes=frozenset({Reason.BAD_DATE})),
        *(ok("name", **{flag: True}) for flag in LOWERING),
        missing("name"),
    ],
    ids=["quote_not_found", "value_not_in_quote", "no_label", "a_code", *LOWERING, "no_value"],
)
def test_an_accepted_value_its_signals_do_not_justify_goes_to_review(signals: FieldSignals) -> None:
    decision = ExtractionDecision.of([accepted("name"), *GOOD_DECISION.fields[1:]])
    signals_doc = doc(signals, *GOOD_SIGNALS.fields[1:])

    checked = enforce(decision, signals_doc, POLICY)

    forced = checked.decision.by_field["name"]
    assert forced.status is R and INVARIANT in forced.reasons
    assert forced.level is (Level.LOW if signals.has_value else None)
    assert checked.corrected == ("name",)
    assert checked.decision.status is DocumentStatus.NEEDS_REVIEW


def test_an_accepted_always_review_value_goes_to_review() -> None:
    decision = ExtractionDecision.of([*GOOD_DECISION.fields[:3], accepted("policy")])
    checked = enforce(decision, GOOD_SIGNALS, POLICY)
    assert checked.decision.by_field["policy"] == review("policy", Level.LOW, INVARIANT)
    assert checked.corrected == ("policy",)


@pytest.mark.parametrize(
    ("signals", "image"),
    [
        (missing("insurer", label_present=True), False),  # E4: its label is printed
        (missing("insurer"), True),  # E4: a page has an image
        (ok("insurer"), False),  # it has a value
    ],
)
def test_an_absent_field_its_signals_do_not_justify_goes_to_review(
    signals: FieldSignals, image: bool
) -> None:
    signals_doc = doc(ok("name"), ok("born"), signals, ok("policy"), image=image)
    checked = enforce(GOOD_DECISION, signals_doc, POLICY)
    forced = checked.decision.by_field["insurer"]
    assert forced.status is R and forced.reasons == {INVARIANT}
    assert checked.corrected == ("insurer",)


def test_a_missing_required_field_is_never_absent() -> None:
    decision = ExtractionDecision.of([accepted("name"), absent("born"), *GOOD_DECISION.fields[2:]])
    signals_doc = doc(ok("name"), missing("born"), missing("insurer"), ok("policy"))
    checked = enforce(decision, signals_doc, POLICY)
    assert checked.decision.by_field["born"] == review("born", None, INVARIANT)


def test_a_field_without_signals_is_never_accepted_or_absent() -> None:
    signals_doc = doc(ok("born"), ok("policy"))  # no signals for name and insurer
    checked = enforce(GOOD_DECISION, signals_doc, POLICY)
    assert checked.corrected == ("name", "insurer")
    assert checked.decision.by_field["name"] == review("name", None, INVARIANT)


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_after_a_failure_nothing_is_accepted_or_absent(failure: Reason) -> None:
    signals_doc = doc(*GOOD_SIGNALS.fields, failure=failure)
    checked = enforce(GOOD_DECISION, signals_doc, POLICY)
    assert checked.corrected == ("name", "born", "insurer")
    assert checked.decision.by_field["name"].reasons == {INVARIANT, failure}
    assert all(d.status is R for d in checked.decision.fields)


def test_a_left_out_field_is_put_in_review_and_an_unknown_one_is_dropped() -> None:
    decision = ExtractionDecision.of(
        [accepted("name"), accepted("born"), accepted("stranger"), GOOD_DECISION.fields[3]]
    )
    checked = enforce(decision, GOOD_SIGNALS, POLICY)
    assert [d.field for d in checked.decision.fields] == list(POLICY.fields)
    assert checked.decision.by_field["insurer"] == review("insurer", None, INVARIANT)
    assert (checked.corrected, checked.dropped) == (("insurer",), 1)


def test_the_result_is_in_schema_order_whatever_the_decisions_order() -> None:
    decision = ExtractionDecision.of(list(reversed(GOOD_DECISION.fields)))
    checked = enforce(decision, GOOD_SIGNALS, POLICY)
    assert [d.field for d in checked.decision.fields] == list(POLICY.fields)
    assert checked.corrected == ()


def test_a_level_goes_with_a_value_only() -> None:
    decision = ExtractionDecision.of(
        [
            review("name", None, Reason.UNSURE),  # a value without a level
            accepted("born"),
            review("insurer", Level.MEDIUM, Reason.LABEL_PRESENT),  # no value, with a level
            GOOD_DECISION.fields[3],
        ]
    )
    checked = enforce(decision, GOOD_SIGNALS, POLICY)
    assert checked.decision.by_field["name"] == review("name", Level.LOW, Reason.UNSURE, INVARIANT)
    assert checked.decision.by_field["insurer"] == review(
        "insurer", None, Reason.LABEL_PRESENT, INVARIANT
    )
    assert checked.corrected == ("name", "insurer")


def test_the_predicates_read_the_policy_and_the_document() -> None:
    assert may_accept(ok("name"), POLICY, doc())
    assert not may_accept(ok("policy"), POLICY, doc())
    assert not may_accept(None, POLICY, doc())
    assert may_be_absent(missing("insurer"), POLICY, doc())
    assert not may_be_absent(missing("name"), POLICY, doc())
    assert not may_be_absent(missing("insurer"), POLICY, doc(image=True))
    assert not may_be_absent(None, POLICY, doc())
