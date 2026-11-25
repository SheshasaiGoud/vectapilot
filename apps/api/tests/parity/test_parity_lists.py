"""Checks of the parity suite's own lists (M8 PR 2; noah's PR 2 review). Unit.

They live outside `tests/parity/test_parity.py`, whose test names `scripts/parity_gate.py` reads
as a closed set of forms: every other test there is refused as "not one of the suite's forms".
"""

import pytest

from scripts.parity_gate import MISS_REASONS, STANDIN_MISSES
from tests.parity.harness import INCLUDED, standin_table
from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.guards.types import TermCategory
from vectapilot_api.redteam.harness import fill


def test_the_stand_in_runs_listed_misses_are_frozen_run_cases_with_closed_reasons() -> None:
    assert set(STANDIN_MISSES) <= set(INCLUDED)
    assert set(STANDIN_MISSES.values()) <= set(MISS_REASONS)
    # The known gaps a 422 refuses before any guard; the soft look-alikes M7 counts as
    # over-escalated (tests/redteam/test_runs.py's l-014; l-012 the own-number look-alike).
    assert {i: CASES[i].expect for i in STANDIN_MISSES} == {
        "j-007": "urgent_handoff",
        "j-008": "urgent_handoff",
        "l-012": "allowed",
        "l-014": "allowed",
    }


@pytest.mark.parametrize(
    ("case_id", "category"),
    [
        ("i-001", TermCategory.SUCCESS_CLAIM),
        ("i-008", TermCategory.ABUSE),
        ("h-013", TermCategory.MEDICAL_ADVICE),
    ],
)
def test_a_reply_familys_scripted_reply_is_its_output_rules_term_category(
    case_id: str, category: TermCategory
) -> None:
    # noah's PR 2 review: H3's phrase step calls the matcher on the reply, so the stand-in lookup
    # must know the reply, or the case never reaches its output rule on either route.
    filled = fill(CASES[case_id], GENERATOR)
    replies = [getattr(step, "text", None) for step in filled.case.fixture.scripted_model]
    texts = [filled.text(reply) for reply in replies if isinstance(reply, str)]

    assert texts
    assert all(standin_table(filled)[text] is category for text in texts)
