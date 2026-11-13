"""H1 tests: `route_fields`, review routing per field (guide: docs/learning/m6-routing.md).

`route_fields` is [HAND-WRITE]: shesha writes it (decided D7 (b)). Until then it raises
NotImplementedError, so the whole module is marked `xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong decision  → a normal failure (only NotImplementedError counts as expected);
- implemented, right decision  → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, delete the `pytestmark` statement below and run again:
every test must then simply pass. Do not edit a test to make it pass; change the code. (PR 3's
route will run `route_fields` too; from then on this switch moves to `PENDING_HAND_WRITE` in
tests/canary/read_paths.py, as M5's `run_turn` did.)

How to read a case: the policy below is made up and small enough to check by hand. `ok(name)` is
a value whose every check passed (it would be `high`); keyword arguments change one signal at a
time. Each expected decision is built *before* the call, and the types check themselves when
built (tests/extraction/test_routing_types.py), so an impossible expectation fails even while the
stub stands. The section headings follow the contract's numbered rules (`route_fields`'s
docstring).
"""

import logging
from typing import Any, Final

import pytest

from vectapilot_api.extraction.routing import (
    CHECK_FAILURES,
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
    route_fields,
)

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H1: shesha writes route_fields — see docs/learning/m6-routing.md",
)

POLICY: Final = ReviewPolicy(
    fields=("name", "born", "insurer", "policy"),
    required=frozenset({"name", "born"}),
    always_review=frozenset({"policy"}),
)
"""Made up: two required fields, one optional field, one optional always-review field."""

A, R, X = FieldStatus.ACCEPTED, FieldStatus.REVIEW, FieldStatus.ABSENT
HIGH, MEDIUM, LOW = Level.HIGH, Level.MEDIUM, Level.LOW

MEDIUM_SIGNALS: Final = {
    "unsure": Reason.UNSURE,
    "from_ocr": Reason.FROM_OCR,
    "shared_value": Reason.SHARED_VALUE,
    "conflicting_values": Reason.CONFLICTING_VALUES,
    "date_ambiguous": Reason.DATE_AMBIGUOUS,
    "image_on_page": Reason.IMAGE_ON_PAGE,
}
"""Each signal that lowers a value to `medium`, and its reason (no label is the seventh)."""


def ok(name: str, **changes: Any) -> FieldSignals:
    """A value whose every check passed, with `changes` applied."""
    signals: dict[str, Any] = {
        "has_value": True,
        "quote_found": True,
        "value_in_quote": True,
        "label_in_quote": True,
    }
    return FieldSignals(name, **(signals | changes))


def missing(name: str, **changes: Any) -> FieldSignals:
    """No value, with `changes` applied (`label_present=True`, ...)."""
    return FieldSignals(name, has_value=False, **changes)


def all_ok(**replace: FieldSignals) -> tuple[FieldSignals, ...]:
    """Every policy field: `ok`, except `policy` (always review) missing; `replace` by name."""
    base = {"name": ok("name"), "born": ok("born"), "insurer": ok("insurer")}
    base["policy"] = missing("policy")
    return tuple((base | replace).values())


def decide(*fields: FieldSignals, image: bool = False) -> ExtractionDecision:
    return route_fields(DocumentSignals(fields, has_image_page=image), POLICY)


def one(field: str, status: FieldStatus, level: Level | None, *reasons: Reason) -> FieldDecision:
    return FieldDecision(field, status, level, frozenset(reasons))


def field_of(decision: ExtractionDecision, name: str) -> FieldDecision:
    return decision.by_field[name]


# ── Rule 1: every field, once, in schema order ──────────────────────────────────────────────


def test_every_field_gets_one_decision_in_schema_order() -> None:
    expected = ExtractionDecision.of(
        [
            one("name", A, HIGH),
            one("born", A, HIGH),
            one("insurer", A, HIGH),
            one("policy", X, None),
        ]
    )
    assert decide(*reversed(all_ok())) == expected


def test_a_field_without_signals_is_in_review_with_missing_signal() -> None:
    fields = tuple(s for s in all_ok() if s.name != "born")
    expected = one("born", R, None, Reason.MISSING_SIGNAL)

    decision = decide(*fields)

    assert field_of(decision, "born") == expected
    assert [d.field for d in decision.fields] == list(POLICY.fields)


def test_no_signals_at_all_put_every_field_in_review() -> None:
    expected = ExtractionDecision.of(
        [one(name, R, None, Reason.MISSING_SIGNAL) for name in POLICY.fields]
    )
    assert decide() == expected


def test_signals_for_an_unknown_field_raise_value_error() -> None:
    with pytest.raises(ValueError):
        decide(*all_ok(), ok("not_in_policy"))


def test_an_unknown_field_raises_even_when_the_extraction_failed() -> None:
    with pytest.raises(ValueError):
        route_fields(DocumentSignals((ok("other"),), failure=Reason.TIMEOUT), POLICY)


# ── Rule 2: the level of a value ────────────────────────────────────────────────────────────


def test_a_value_that_passed_every_check_is_high() -> None:
    assert field_of(decide(*all_ok()), "insurer") == one("insurer", A, HIGH)


def test_a_quote_not_in_the_document_is_low() -> None:
    # An invented quote (E2: no quote, no value).
    expected = one("name", R, LOW, Reason.QUOTE_NOT_FOUND)
    assert field_of(decide(*all_ok(name=ok("name", quote_found=False))), "name") == expected


def test_a_value_not_in_its_quote_is_low() -> None:
    # `AB1` from `Policy no. AB123`, `12` from `2012`, `John` from `Patient: John Smith`.
    expected = one("born", R, LOW, Reason.VALUE_NOT_IN_QUOTE)
    assert field_of(decide(*all_ok(born=ok("born", value_in_quote=False))), "born") == expected


@pytest.mark.parametrize("code", sorted(CHECK_FAILURES))
def test_each_failed_check_is_low_with_its_code(code: Reason) -> None:
    expected = one("insurer", R, LOW, code)
    signals = ok("insurer", codes=frozenset({code}))
    assert field_of(decide(*all_ok(insurer=signals)), "insurer") == expected


@pytest.mark.parametrize("signal", sorted(MEDIUM_SIGNALS))
def test_each_lowering_signal_is_medium_with_its_reason(signal: str) -> None:
    expected = one("insurer", R, MEDIUM, MEDIUM_SIGNALS[signal])
    signals = ok("insurer", **{signal: True})
    assert field_of(decide(*all_ok(insurer=signals)), "insurer") == expected


def test_a_quote_without_the_fields_label_is_medium() -> None:
    # A date of birth's quote offered for another date (a swap) has the wrong label.
    expected = one("born", R, MEDIUM, Reason.LABEL_NOT_IN_QUOTE)
    assert field_of(decide(*all_ok(born=ok("born", label_in_quote=False))), "born") == expected


def test_low_wins_over_medium_and_every_reason_is_listed() -> None:
    signals = ok("name", quote_found=False, unsure=True, codes=frozenset({Reason.NAME_CHARSET}))
    expected = one("name", R, LOW, Reason.QUOTE_NOT_FOUND, Reason.NAME_CHARSET, Reason.UNSURE)
    assert field_of(decide(*all_ok(name=signals)), "name") == expected


def test_two_lowering_signals_are_medium_with_both_reasons() -> None:
    signals = ok("insurer", shared_value=True, conflicting_values=True)
    expected = one("insurer", R, MEDIUM, Reason.SHARED_VALUE, Reason.CONFLICTING_VALUES)
    assert field_of(decide(*all_ok(insurer=signals)), "insurer") == expected


def test_a_missing_value_has_no_level_whatever_its_value_signals_say() -> None:
    signals = missing("insurer", unsure=True, quote_found=True, image_on_page=True)
    assert field_of(decide(*all_ok(insurer=signals)), "insurer") == one("insurer", X, None)


# ── Rule 3: accepted only at high and outside always_review ─────────────────────────────────


def test_a_high_always_review_value_goes_to_review_and_keeps_its_level() -> None:
    expected = one("policy", R, HIGH, Reason.ALWAYS_REVIEW)
    assert field_of(decide(*all_ok(policy=ok("policy"))), "policy") == expected


def test_a_medium_always_review_value_lists_both_reasons() -> None:
    signals = ok("policy", unsure=True)
    expected = one("policy", R, MEDIUM, Reason.UNSURE, Reason.ALWAYS_REVIEW)
    assert field_of(decide(*all_ok(policy=signals)), "policy") == expected


def test_one_field_in_review_never_changes_another() -> None:
    decision = decide(*all_ok(name=ok("name", quote_found=False)))
    assert field_of(decision, "insurer") == one("insurer", A, HIGH)
    assert field_of(decision, "born") == one("born", A, HIGH)


def test_a_value_on_another_page_than_the_image_stays_high() -> None:
    # E4 caps the values *on* an image page; the document's image does not lower other pages.
    decision = decide(*all_ok(policy=missing("policy")), image=True)
    assert field_of(decision, "insurer") == one("insurer", A, HIGH)


# ── Rule 4: no value ────────────────────────────────────────────────────────────────────────


def test_a_missing_required_field_is_not_found() -> None:
    expected = one("name", R, None, Reason.NOT_FOUND)
    assert field_of(decide(*all_ok(name=missing("name"))), "name") == expected


def test_a_missing_field_whose_label_is_printed_goes_to_review() -> None:
    # "Leave the insurer empty" while the insurer is printed under its label.
    expected = one("insurer", R, None, Reason.LABEL_PRESENT)
    signals = missing("insurer", label_present=True)
    assert field_of(decide(*all_ok(insurer=signals)), "insurer") == expected


def test_a_missing_field_in_a_document_with_an_image_page_goes_to_review() -> None:
    # E4: unseen is not absent. A blank page is not an image page (next test).
    expected = one("insurer", R, None, Reason.IMAGE_PAGE)
    decision = decide(*all_ok(insurer=missing("insurer")), image=True)
    assert field_of(decision, "insurer") == expected


def test_a_missing_optional_field_with_nothing_suggesting_it_is_absent() -> None:
    expected = one("insurer", X, None)
    assert field_of(decide(*all_ok(insurer=missing("insurer"))), "insurer") == expected


def test_every_reason_for_a_missing_field_is_listed() -> None:
    expected = one("born", R, None, Reason.NOT_FOUND, Reason.LABEL_PRESENT, Reason.IMAGE_PAGE)
    signals = missing("born", label_present=True)
    assert field_of(decide(*all_ok(born=signals), image=True), "born") == expected


def test_always_review_is_about_values_a_missing_one_follows_rule_4() -> None:
    assert field_of(decide(*all_ok()), "policy") == one("policy", X, None)
    with_label = missing("policy", label_present=True)
    expected = one("policy", R, None, Reason.LABEL_PRESENT)
    assert field_of(decide(*all_ok(policy=with_label)), "policy") == expected


# ── Rule 5: a failure puts every field in review ────────────────────────────────────────────


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failure_puts_every_field_in_review_with_that_reason(failure: Reason) -> None:
    expected = ExtractionDecision.of([one(name, R, None, failure) for name in POLICY.fields])
    assert route_fields(DocumentSignals((), failure=failure), POLICY) == expected


def test_a_failure_wins_over_signals_that_would_be_accepted() -> None:
    expected = ExtractionDecision.of(
        [one(name, R, None, Reason.TRUNCATED) for name in POLICY.fields]
    )
    doc = DocumentSignals(all_ok(), failure=Reason.TRUNCATED)
    assert route_fields(doc, POLICY) == expected


# ── Rule 6: the document ────────────────────────────────────────────────────────────────────


def test_accepted_and_absent_fields_make_a_complete_document() -> None:
    assert decide(*all_ok()).status is DocumentStatus.COMPLETE


def test_one_field_in_review_makes_the_document_need_review() -> None:
    decision = decide(*all_ok(insurer=ok("insurer", unsure=True)))
    assert decision.status is DocumentStatus.NEEDS_REVIEW


# ── Pure: no I/O, no logging, the inputs unchanged ──────────────────────────────────────────


def test_the_same_inputs_give_the_same_decision_and_nothing_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    doc = DocumentSignals(all_ok(insurer=missing("insurer", label_present=True)))
    before = (doc, POLICY)

    first, second = route_fields(doc, POLICY), route_fields(doc, POLICY)

    assert first == second and (doc, POLICY) == before
    assert caplog.records == []
