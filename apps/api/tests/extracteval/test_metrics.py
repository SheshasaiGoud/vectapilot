"""H2 tests: the extraction metric functions (guide: docs/learning/m6-metrics.md).

The five functions in `extracteval/metrics.py` are [HAND-WRITE] (D7 (b): Claude writes H2 at
shesha's request). Until then they raise NotImplementedError, so the whole module is marked
`xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong answer    → a normal failure (only NotImplementedError counts as expected);
- implemented, right answer    → "XPASS(strict)", a failure on purpose: that is the signal.

When no test here is xfailed any more, delete the `pytestmark` statement below and run again:
every test must then simply pass. Change the code, never a fixture: every expected value here was
worked out by hand (the comments show the counting), before any code existed. Each expected
result is built before the call, and the result types check themselves when built
(test_metrics_types.py), so an impossible expectation fails even while the stubs stand.

Every field below is made up; no number here is a result.
"""

from collections.abc import Sequence
from typing import Any, Final

import pytest

from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.extracteval.metrics import (
    Accuracy,
    DocumentCount,
    FieldCount,
    ReviewPrecision,
    ScoredField,
    SilentErrors,
    field_accuracy,
    review_precision,
    review_rate,
    silent_errors,
    straight_through,
)

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H2 (M6): extracteval/metrics.py — see docs/learning/m6-metrics.md",
)

S, P = "sunrise-clinic", "paws-and-co"
OK, NEEDS = "complete", "needs_review"


def f(
    document: str,
    field: str,
    status: str,
    *,
    business: str = S,
    doc_status: str = NEEDS,
    level: str | None = None,
    reasons: Sequence[str] = (),
    value: bool = True,
    gold_null: bool = False,
    correct: bool = True,
) -> ScoredField:
    """A scored field (`value`: the system returned one)."""
    return ScoredField(
        document=document,
        business_id=business,
        field=field,
        scored=True,
        document_status=doc_status,
        status=status,
        level=level,
        reasons=tuple(reasons),
        has_value=value,
        gold_null=gold_null,
        correct=correct,
    )


def not_scored(document: str, field: str, business: str = S) -> ScoredField:
    return ScoredField(
        document=document,
        business_id=business,
        field=field,
        scored=False,
        document_status=None,
        status=None,
        level=None,
        reasons=(),
        has_value=False,
        gold_null=False,
        correct=None,
    )


def keys(fields: Sequence[ScoredField]) -> list[str]:
    return [item.key for item in fields]


# ── Fixture A: seven documents, 15 scored fields ─────────────────────────────────────────────
#  doc  business  document      field    status    level   value  gold   correct  why
#  d0   S         not scored    name     -         -       -      -      -        not run
#  d1   S         complete      name     accepted  high    yes    yes    yes
#  d1                           born     accepted  high    yes    yes    yes
#  d1                           insurer  absent    -       no     null   yes      both null
#  d2   S         needs_review  name     accepted  high    yes    yes    NO       silent
#  d2                           born     review    medium  yes    yes    yes      label_not_in_quote
#  d2                           insurer  absent    -       no     yes    NO       silent: absent,
#                                                                                 gold has a value
#  d3   S         needs_review  name     review    -       no     yes    NO       truncated (E3)
#  d3                           born     review    -       no     yes    NO       truncated (E3)
#  d3                           insurer  review    -       no     null   yes      truncated (E3)
#  d4   P         needs_review  pet      review    low     yes    yes    NO       quote_not_found
#  d4                           owner    review    -       no     null   yes      image_page,
#                                                                                 label_present
#  d4                           clinic   accepted  high    yes    yes    yes
#  d5   P         complete      pet      accepted  high    yes    yes    yes
#  d5                           owner    accepted  high    yes    yes    NO       silent
#  d5                           clinic   accepted  high    yes    yes    yes
#  d6   P         not scored    pet, owner, clinic                                 harness error
FIXTURE_A: Final[tuple[ScoredField, ...]] = (
    not_scored("d6", "pet", P),
    f("d1", "name", "accepted", doc_status=OK, level="high"),
    f("d1", "born", "accepted", doc_status=OK, level="high"),
    f("d1", "insurer", "absent", doc_status=OK, value=False, gold_null=True),
    f("d2", "name", "accepted", level="high", correct=False),
    f("d2", "born", "review", level="medium", reasons=("label_not_in_quote",)),
    f("d2", "insurer", "absent", value=False, correct=False),
    f("d3", "name", "review", reasons=("truncated",), value=False, correct=False),
    f("d3", "born", "review", reasons=("truncated",), value=False, correct=False),
    f("d3", "insurer", "review", reasons=("truncated",), value=False, gold_null=True),
    f("d4", "pet", "review", business=P, level="low", reasons=("quote_not_found",), correct=False),
    f(
        "d4",
        "owner",
        "review",
        business=P,
        reasons=("image_page", "label_present"),
        value=False,
        gold_null=True,
    ),
    f("d4", "clinic", "accepted", business=P, level="high"),
    f("d5", "pet", "accepted", business=P, doc_status=OK, level="high"),
    f("d5", "owner", "accepted", business=P, doc_status=OK, level="high", correct=False),
    f("d5", "clinic", "accepted", business=P, doc_status=OK, level="high"),
    not_scored("d6", "owner", P),
    not_scored("d0", "name"),
    not_scored("d6", "clinic", P),
)
"""Not-scored fields are spread through the input on purpose: order must not matter."""


# ── §1 Field accuracy ────────────────────────────────────────────────────────────────────────


def test_field_accuracy_on_fixture_a() -> None:
    # Scored: d1 to d5, 3 fields each = 15. Correct: d1 3 + d2 1 (born) + d3 1 (insurer)
    # + d4 2 (owner, clinic) + d5 2 (pet, clinic) = 9. Wrong: the other 6, sorted as strings.
    expected = Accuracy(
        correct=9,
        n=15,
        wrong=("d2/insurer", "d2/name", "d3/born", "d3/name", "d4/pet", "d5/owner"),
        not_scored=("d0", "d6"),
    )
    assert field_accuracy(FIXTURE_A) == expected


def test_field_accuracy_of_nothing_is_zero_of_zero() -> None:
    assert field_accuracy(()) == Accuracy(correct=0, n=0, wrong=(), not_scored=())


def test_field_accuracy_with_no_scored_document_lists_each_document_once() -> None:
    fields = [not_scored("d6", "pet", P), not_scored("d6", "owner", P), not_scored("d0", "name")]
    assert field_accuracy(fields) == Accuracy(correct=0, n=0, wrong=(), not_scored=("d0", "d6"))


def test_field_accuracy_counts_every_status_e3_failures_included() -> None:
    # A truncated document whose gold is null everywhere: all three in review, no value, and all
    # three correct (both null). Status never decides correctness.
    fields = [
        f("d9", name, "review", reasons=("truncated",), value=False, gold_null=True)
        for name in ("name", "born", "insurer")
    ]
    assert field_accuracy(fields) == Accuracy(correct=3, n=3, wrong=(), not_scored=())


def test_field_accuracy_sorts_keys_as_strings() -> None:
    # "d10/a" < "d2/a" < "d2/b" as strings (no natural sort).
    fields = [
        f("d2", "b", "accepted", level="high", correct=False),
        f("d10", "a", "review", level="low", reasons=("bad_date",), correct=False),
        f("d2", "a", "absent", value=False, correct=False),
    ]
    expected = Accuracy(correct=0, n=3, wrong=("d10/a", "d2/a", "d2/b"), not_scored=())
    assert field_accuracy(fields) == expected


def test_field_accuracy_per_level_as_the_harness_calls_it() -> None:
    # high: d1/name, d1/born, d2/name ✗, d4/clinic, d5/pet, d5/owner ✗, d5/clinic → 5 of 7.
    # medium: d2/born → 1 of 1. low: d4/pet ✗ → 0 of 1.
    by_level = {
        level: [x for x in FIXTURE_A if x.scored and x.level == level]
        for level in ("high", "medium", "low")
    }
    assert field_accuracy(by_level["high"]) == Accuracy(5, 7, ("d2/name", "d5/owner"), ())
    assert field_accuracy(by_level["medium"]) == Accuracy(1, 1, (), ())
    assert field_accuracy(by_level["low"]) == Accuracy(0, 1, ("d4/pet",), ())


def test_accepted_precision_as_the_harness_calls_it() -> None:
    # Accepted: the seven high fields above (accepted is high only) → 5 of 7.
    accepted = [x for x in FIXTURE_A if x.status == "accepted"]
    assert field_accuracy(accepted) == Accuracy(5, 7, ("d2/name", "d5/owner"), ())


# ── §2 Silent errors ─────────────────────────────────────────────────────────────────────────


def test_silent_errors_on_fixture_a() -> None:
    # Accepted and not correct: d2/name, d5/owner. Absent with a gold value: d2/insurer.
    # (d1/insurer is absent with a null gold: correct, not silent.) 3 of 15 scored fields.
    # Documents with at least one: d2, d5 → 2 of 5 scored documents.
    expected = SilentErrors(
        fields=FieldCount(k=3, n=15, fields=("d2/insurer", "d2/name", "d5/owner")),
        documents=DocumentCount(x=2, n=5, documents=("d2", "d5"), interval=wilson_interval(2, 5)),
    )
    assert silent_errors(FIXTURE_A) == expected


def test_silent_errors_per_business_as_the_harness_calls_it() -> None:
    # Sunrise: d1, d2, d3 scored (9 fields); silent d2/insurer, d2/name → 2 of 9; 1 of 3 docs.
    # Paws: d4, d5 scored (6 fields; d6 not scored); silent d5/owner → 1 of 6; 1 of 2 docs.
    sunrise = [x for x in FIXTURE_A if x.business_id == S]
    paws = [x for x in FIXTURE_A if x.business_id == P]
    assert silent_errors(sunrise) == SilentErrors(
        FieldCount(2, 9, ("d2/insurer", "d2/name")),
        DocumentCount(1, 3, ("d2",), wilson_interval(1, 3)),
    )
    assert silent_errors(paws) == SilentErrors(
        FieldCount(1, 6, ("d5/owner",)),
        DocumentCount(1, 2, ("d5",), wilson_interval(1, 2)),
    )


def test_a_wrong_field_in_review_is_never_silent() -> None:
    # A person looks at it: a review field is never a silent error, with or without a value.
    fields = [
        f("d7", "name", "review", level="low", reasons=("value_not_in_quote",), correct=False),
        f("d7", "born", "review", reasons=("not_found",), value=False, correct=False),
    ]
    expected = SilentErrors(FieldCount(0, 2, ()), DocumentCount(0, 1, (), wilson_interval(0, 1)))
    assert silent_errors(fields) == expected


def test_an_absent_field_is_silent_only_when_its_gold_has_a_value() -> None:
    fields = [
        f("d8", "insurer", "absent", doc_status=OK, value=False, gold_null=True),
        f("d8", "policy", "absent", doc_status=OK, value=False, correct=False),
    ]
    expected = SilentErrors(
        FieldCount(1, 2, ("d8/policy",)), DocumentCount(1, 1, ("d8",), wilson_interval(1, 1))
    )
    assert silent_errors(fields) == expected


def test_an_accepted_value_with_a_null_gold_is_silent() -> None:
    # The most dangerous silent error: the document does not visibly give the insurer (gold null:
    # invented, hidden-only or planted), yet a value was accepted. d11/insurer: accepted and not
    # correct (a value against a null gold) → silent. d11/name: accepted, correct → not.
    # 1 of 2 scored fields; 1 of 1 scored document (zara-safety, PR 4 review).
    fields = [
        f("d11", "insurer", "accepted", doc_status=OK, level="high", gold_null=True, correct=False),
        f("d11", "name", "accepted", doc_status=OK, level="high"),
    ]
    expected = SilentErrors(
        FieldCount(1, 2, ("d11/insurer",)), DocumentCount(1, 1, ("d11",), wilson_interval(1, 1))
    )
    assert silent_errors(fields) == expected


def test_a_document_with_two_silent_fields_counts_once() -> None:
    fields = [
        f("d9", "name", "accepted", doc_status=OK, level="high", correct=False),
        f("d9", "born", "accepted", doc_status=OK, level="high", correct=False),
        f("d10", "name", "accepted", doc_status=OK, level="high"),
    ]
    expected = SilentErrors(
        FieldCount(2, 3, ("d9/born", "d9/name")),
        DocumentCount(1, 2, ("d9",), wilson_interval(1, 2)),
    )
    assert silent_errors(fields) == expected


def test_silent_errors_of_nothing_have_no_interval() -> None:
    expected = SilentErrors(FieldCount(0, 0, ()), DocumentCount(0, 0, (), None))
    assert silent_errors(()) == expected
    assert silent_errors([not_scored("d0", "name")]) == expected


# ── §3 Review rate (and review recall, as the harness calls it) ──────────────────────────────


def test_review_rate_on_fixture_a() -> None:
    # In review: d2/born, d3/name, d3/born, d3/insurer, d4/pet, d4/owner → 6 of 15.
    expected = FieldCount(
        k=6,
        n=15,
        fields=("d2/born", "d3/born", "d3/insurer", "d3/name", "d4/owner", "d4/pet"),
    )
    assert review_rate(FIXTURE_A) == expected


def test_review_recall_as_the_harness_calls_it() -> None:
    # The 6 fields that are not correct: d2/insurer (absent), d2/name (accepted), d3/born,
    # d3/name, d4/pet (review), d5/owner (accepted). In review: 3 of 6. The 3 misses are
    # exactly the silent errors.
    wrong = [x for x in FIXTURE_A if x.scored and x.correct is False]
    result = review_rate(wrong)
    assert result == FieldCount(k=3, n=6, fields=("d3/born", "d3/name", "d4/pet"))
    misses = sorted(set(keys(wrong)) - set(result.fields))
    assert misses == ["d2/insurer", "d2/name", "d5/owner"]


def test_review_rate_of_nothing_is_zero_of_zero() -> None:
    assert review_rate([not_scored("d0", "name")]) == FieldCount(0, 0, ())


# ── §4 Review precision ──────────────────────────────────────────────────────────────────────


def test_review_precision_on_fixture_a() -> None:
    # Reviewed with a value: d2/born (correct), d4/pet (wrong) → 1 of 2 wrong.
    # Reviewed without a value: d3/name, d3/born, d3/insurer (truncated), d4/owner (image_page,
    # label_present) → 4; by reason: image_page 1, label_present 1, truncated 3; null gold:
    # d3/insurer, d4/owner → 2.
    expected = ReviewPrecision(
        wrong=1,
        n=2,
        fields=("d4/pet",),
        without_value=4,
        by_reason=(("image_page", 1), ("label_present", 1), ("truncated", 3)),
        null_gold=2,
    )
    assert review_precision(FIXTURE_A) == expected


def test_review_precision_ignores_accepted_and_absent_fields() -> None:
    fields = [
        f("d7", "name", "accepted", level="high", correct=False),
        f("d7", "born", "absent", value=False, correct=False),
        f("d7", "insurer", "review", level="medium", reasons=("unsure",)),
    ]
    expected = ReviewPrecision(wrong=0, n=1, fields=(), without_value=0, by_reason=(), null_gold=0)
    assert review_precision(fields) == expected


def test_review_precision_counts_a_field_under_each_of_its_reasons() -> None:
    fields = [
        f("d8", "a", "review", reasons=("image_page", "not_found"), value=False, correct=False),
        f("d8", "b", "review", reasons=("not_found",), value=False, correct=False),
        f("d8", "c", "review", reasons=("label_present",), value=False, gold_null=True),
    ]
    expected = ReviewPrecision(
        wrong=0,
        n=0,
        fields=(),
        without_value=3,
        by_reason=(("image_page", 1), ("label_present", 1), ("not_found", 2)),
        null_gold=1,
    )
    assert review_precision(fields) == expected


def test_review_precision_of_nothing_is_empty() -> None:
    expected = ReviewPrecision(wrong=0, n=0, fields=(), without_value=0, by_reason=(), null_gold=0)
    assert review_precision(()) == expected


# ── §5 Straight-through documents ────────────────────────────────────────────────────────────


def test_straight_through_on_fixture_a() -> None:
    # d1: complete, every field correct → yes. d2, d3, d4: needs_review → no. d5: complete, but
    # d5/owner is wrong → no. 1 of 5 scored documents (d0 and d6 are not scored).
    expected = DocumentCount(x=1, n=5, documents=("d1",), interval=wilson_interval(1, 5))
    assert straight_through(FIXTURE_A) == expected


def test_a_document_in_review_is_never_straight_through() -> None:
    # Every field correct, but a person had to look at one.
    fields = [
        f("d7", "name", "accepted", level="high"),
        f("d7", "born", "review", level="medium", reasons=("date_ambiguous",)),
    ]
    assert straight_through(fields) == DocumentCount(0, 1, (), wilson_interval(0, 1))


def test_a_complete_document_with_a_missed_value_is_not_straight_through() -> None:
    # Complete (nothing in review), but an absent field's gold has a value.
    fields = [
        f("d8", "name", "accepted", doc_status=OK, level="high"),
        f("d8", "insurer", "absent", doc_status=OK, value=False, correct=False),
        f("d9", "name", "accepted", doc_status=OK, level="high"),
        f("d9", "insurer", "absent", doc_status=OK, value=False, gold_null=True),
    ]
    assert straight_through(fields) == DocumentCount(1, 2, ("d9",), wilson_interval(1, 2))


def test_a_complete_document_with_an_invented_value_is_not_straight_through() -> None:
    # d11 is complete, but its insurer was accepted where the gold is null (not correct): not
    # straight-through. d12 is complete and correct. 1 of 2 scored documents.
    fields = [
        f("d11", "insurer", "accepted", doc_status=OK, level="high", gold_null=True, correct=False),
        f("d11", "name", "accepted", doc_status=OK, level="high"),
        f("d12", "name", "accepted", doc_status=OK, level="high"),
    ]
    assert straight_through(fields) == DocumentCount(1, 2, ("d12",), wilson_interval(1, 2))


def test_straight_through_of_nothing_has_no_interval() -> None:
    assert straight_through(()) == DocumentCount(0, 0, (), None)
    assert straight_through([not_scored("d6", "pet", P)]) == DocumentCount(0, 0, (), None)


# ── Purity ───────────────────────────────────────────────────────────────────────────────────


def test_the_functions_leave_their_input_as_it_was() -> None:
    given: list[Any] = list(FIXTURE_A)
    field_accuracy(given)
    silent_errors(given)
    review_rate(given)
    review_precision(given)
    straight_through(given)
    assert given == list(FIXTURE_A)
