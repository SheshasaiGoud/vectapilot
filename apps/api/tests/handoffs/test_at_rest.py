"""S11's helper for customer text at rest (`handoffs.at_rest`, M8 PR 2a). Unit.

Masked by H2's real `mask_pii` (written 2026-11-20) on the guards' own clean text; None (stored as
NULL) whenever that cannot be done within the column's bound: never raw, never truncated. Every
category E case of the frozen red-team suite goes through it here; the same cases through the
widget route, red-flagged so they are stored, are the parity suite's "18 of 18"
(`tests/parity/test_pii_at_rest.py`).
"""

from typing import Final

import pytest

from tests.guards.agent_helpers import IdentityMask, RaisingMask
from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.guards.caps import OverCap
from vectapilot_api.guards.pii import MaskingIncomplete
from vectapilot_api.handoffs.at_rest import TEXT_MAX, mask_at_rest
from vectapilot_api.redteam.cases import Category
from vectapilot_api.redteam.harness import fill
from vectapilot_api.redteam.logs import generated_values
from vectapilot_api.redteam.pii import leak_forms
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS

E_CASES: Final = sorted(case.id for case in CASES.values() if case.category is Category.E)
SYNTHETIC_PHONE: Final = "+91 00000 20777"
EMAIL: Final = "x@y.in"
"""Six characters that mask to `[EMAIL]`, seven: a placeholder can be longer than its span."""


def test_the_bound_is_the_messages_own() -> None:
    assert TEXT_MAX == MAX_QUESTION_CHARS == 2_000
    assert len(E_CASES) == 18  # zara's count (M7's category E)


def test_a_message_is_cleaned_and_masked_as_the_guards_do() -> None:
    masked = mask_at_rest(f"  My dog​ ate rat poison, call {SYNTHETIC_PHONE}  ")

    assert masked == "My dog ate rat poison, call [PHONE]"


@pytest.mark.parametrize("case_id", E_CASES)
def test_no_form_of_any_generated_value_survives_masking_at_rest(case_id: str) -> None:
    filled = fill(CASES[case_id], GENERATOR)
    values = generated_values(filled)

    masked = mask_at_rest(filled.input)

    assert values, case_id  # the case holds personal data (the positive control)
    assert masked is not None
    assert not {form for value in values for form in leak_forms(value) if form in masked}


def test_text_over_the_bound_is_none_before_any_work() -> None:
    mask = IdentityMask()

    assert mask_at_rest("a" * (TEXT_MAX + 1), mask=mask) is None
    assert mask.seen == []
    assert mask_at_rest("a" * TEXT_MAX, mask=mask) == "a" * TEXT_MAX


def test_a_masked_text_longer_than_the_bound_is_none_never_truncated() -> None:
    raw = " ".join([EMAIL] * 285)  # 1,994 characters; masked, 285 x 7 + 284 = 2,279
    assert len(raw) <= TEXT_MAX

    assert mask_at_rest(raw) is None
    assert mask_at_rest(" ".join([EMAIL] * 2)) == "[EMAIL] [EMAIL]"


@pytest.mark.parametrize(
    "error",
    [RuntimeError("masker broke"), OverCap("message"), MaskingIncomplete(), ValueError("x")],
    ids=["a bug", "over its cap", "incomplete", "a value error"],
)
def test_a_masker_that_raises_stores_no_text_never_the_raw_one(error: Exception) -> None:
    assert mask_at_rest(f"call {SYNTHETIC_PHONE}", mask=RaisingMask(error)) is None


@pytest.mark.parametrize("raw", ["", "​​", "­"], ids=["empty", "zwsp", "shy"])
def test_nothing_left_after_cleaning_is_none(raw: str) -> None:
    assert mask_at_rest(raw) is None
