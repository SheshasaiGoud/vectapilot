"""M6 PR 2: the types H1 reads and returns (team code, `extraction/routing.py`), passing now.

H1's own tests are in tests/extraction/test_routing.py (strict xfails until shesha writes
`route_fields`). These check that the types refuse impossible inputs and decisions, so an expected
decision in those tests can never be one H1 is not allowed to return.
"""

import pytest

from tests.extraction.fakes import referral, vaccination
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
)


def test_the_check_failures_are_the_plans_reason_codes() -> None:
    assert {r.value for r in CHECK_FAILURES} == {
        "bad_date",
        "bad_pattern",
        "bad_enum",
        "too_long",
        "name_charset",
        "quote_too_long",
        "rule_failed",
        "value_cut",
    }
    assert {r.value for r in FAILURES} == {
        "model_unavailable",
        "invalid_output",
        "truncated",
        "timeout",
        "internal_error",
    }
    assert not CHECK_FAILURES & FAILURES


def test_signals_refuse_a_code_that_is_not_a_check_failure() -> None:
    assert FieldSignals("a", has_value=True, codes=frozenset({Reason.BAD_DATE})).codes
    with pytest.raises(ValueError, match="check failures"):
        FieldSignals("a", has_value=True, codes=frozenset({Reason.UNSURE}))


def test_document_signals_refuse_duplicates_and_unknown_failures() -> None:
    with pytest.raises(ValueError, match="at most once"):
        DocumentSignals((FieldSignals("a", has_value=False), FieldSignals("a", has_value=True)))
    with pytest.raises(ValueError, match="E3"):
        DocumentSignals((), failure=Reason.UNSURE)
    assert DocumentSignals((), failure=Reason.TRUNCATED).failure is Reason.TRUNCATED


def test_a_policy_names_its_own_fields_once() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        ReviewPolicy(())
    with pytest.raises(ValueError, match="each once"):
        ReviewPolicy(("a", "a"))
    with pytest.raises(ValueError, match="fields of the policy"):
        ReviewPolicy(("a",), required=frozenset({"b"}))
    with pytest.raises(ValueError, match="fields of the policy"):
        ReviewPolicy(("a",), always_review=frozenset({"b"}))


def test_the_policy_of_a_kind_comes_from_its_configuration() -> None:
    assert ReviewPolicy.for_kind(referral()) == ReviewPolicy(
        fields=(
            "patient_name",
            "date_of_birth",
            "referring_doctor",
            "referral_date",
            "insurer",
            "policy_number",
        ),
        required=frozenset({"patient_name", "date_of_birth", "referring_doctor", "referral_date"}),
        always_review=frozenset({"policy_number"}),
    )
    paws = ReviewPolicy.for_kind(vaccination())
    assert paws.always_review == frozenset()
    assert "valid_until" not in paws.required and "issuing_clinic" not in paws.required


@pytest.mark.parametrize(
    ("status", "level", "reasons"),
    [
        (FieldStatus.ACCEPTED, Level.MEDIUM, frozenset()),
        (FieldStatus.ACCEPTED, None, frozenset()),
        (FieldStatus.ACCEPTED, Level.HIGH, frozenset({Reason.UNSURE})),
        (FieldStatus.ABSENT, Level.HIGH, frozenset()),
        (FieldStatus.ABSENT, None, frozenset({Reason.NOT_FOUND})),
        (FieldStatus.REVIEW, Level.LOW, frozenset()),
        (FieldStatus.REVIEW, None, frozenset()),
    ],
)
def test_an_impossible_field_decision_cannot_be_built(
    status: FieldStatus, level: Level | None, reasons: frozenset[Reason]
) -> None:
    with pytest.raises(ValueError):
        FieldDecision("a", status, level, reasons)


def test_the_possible_field_decisions() -> None:
    assert FieldDecision("a", FieldStatus.ACCEPTED, Level.HIGH).reasons == frozenset()
    assert FieldDecision("a", FieldStatus.ABSENT).level is None
    review = FieldDecision("a", FieldStatus.REVIEW, Level.HIGH, frozenset({Reason.ALWAYS_REVIEW}))
    assert review.level is Level.HIGH
    assert FieldDecision("a", FieldStatus.REVIEW, None, frozenset({Reason.NOT_FOUND})).level is None


def test_a_document_is_complete_exactly_when_no_field_is_in_review() -> None:
    accepted = FieldDecision("a", FieldStatus.ACCEPTED, Level.HIGH)
    absent = FieldDecision("b", FieldStatus.ABSENT)
    review = FieldDecision("c", FieldStatus.REVIEW, None, frozenset({Reason.NOT_FOUND}))

    assert ExtractionDecision.of([accepted, absent]).status is DocumentStatus.COMPLETE
    assert ExtractionDecision.of([accepted, review]).status is DocumentStatus.NEEDS_REVIEW
    with pytest.raises(ValueError, match="complete exactly"):
        ExtractionDecision(DocumentStatus.COMPLETE, (accepted, review))
    with pytest.raises(ValueError, match="complete exactly"):
        ExtractionDecision(DocumentStatus.NEEDS_REVIEW, (accepted,))
    with pytest.raises(ValueError, match="each once"):
        ExtractionDecision.of([accepted, accepted])
    with pytest.raises(ValueError, match="at least one field"):
        ExtractionDecision.of([])


def test_a_decision_can_be_read_by_field_and_compares_by_value() -> None:
    accepted = FieldDecision("a", FieldStatus.ACCEPTED, Level.HIGH)
    decision = ExtractionDecision.of([accepted])
    assert decision.by_field["a"] is accepted
    assert decision == ExtractionDecision.of([FieldDecision("a", FieldStatus.ACCEPTED, Level.HIGH)])
