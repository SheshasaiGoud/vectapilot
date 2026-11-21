"""TEST-ONLY seams for the agent runner's own tests (M7 PR 6). **None of these is H1, H2 or H3.**

`CategoryLookup` is a fixed lookup from a whole folded text to one term category: for a text it
knows, it returns a hit for the first term of that category among the terms it is given, and
nothing for any other text. It never looks inside a text, so it cannot be copied into a matcher;
it lets a test drive a frozen case through the real guard pipeline as if H1 had matched it.
`TableCheck` returns fixed findings for the replies it knows (not H3). The masks come from
`tests/guards/agent_helpers.py` (`IdentityMask`, `DigitRunMask`: not H2).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from tests.guards.agent_helpers import DigitRunMask, IdentityMask
from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.guards.output import OutputContext
from vectapilot_api.guards.selftest import screen_text
from vectapilot_api.guards.types import Finding, Folded, TermCategory, TermHit, TermSet
from vectapilot_api.redteam.agent import GuardSeams
from vectapilot_api.redteam.harness import FilledCase, fill
from vectapilot_api.redteam.plan import Build

ALL_WRITTEN: Final = {Build.H1: lambda: True, Build.H2: lambda: True, Build.H3: lambda: True}
"""Probes saying every hand-write is written: the stand-ins below stand in for them."""


class CategoryLookup:
    """NOT H1 (module docstring)."""

    def __init__(self, table: Mapping[str, TermCategory]) -> None:
        self.table = {screen_text(text): category for text, category in table.items()}
        self.calls = 0

    def __call__(self, text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        self.calls += 1
        category = self.table.get(text)
        first = next((t for t in terms.terms if t.category is category), None)
        return () if first is None else (TermHit(first.id, first.category, 0),)


@dataclass
class TableCheck:
    """NOT H3: fixed findings for the replies in `table` (raw text), none for any other."""

    table: Mapping[str, tuple[Finding, ...]] = field(default_factory=dict)
    asked: int = 0

    def __call__(self, reply: str, context: OutputContext) -> tuple[Finding, ...]:
        self.asked += 1
        return self.table.get(reply, ())


def filled(case_id: str) -> FilledCase:
    return fill(CASES[case_id], GENERATOR)


def note_of(case_id: str, index: int = 0) -> str:
    """The `note` field of a case's tool result, filled."""
    case = filled(case_id)
    note = case.case.fixture.tool_results[index].fields["note"]
    assert isinstance(note, str)
    return case.text(note)


def reply_of(case_id: str) -> str:
    """The text of a case's last scripted reply, filled."""
    case = filled(case_id)
    texts = [step.text for step in case.case.fixture.scripted_model if hasattr(step, "text")]
    return case.text(str(texts[-1]))


def seams(
    *,
    hits: Mapping[str, TermCategory] | None = None,
    findings: Mapping[str, tuple[Finding, ...]] | None = None,
    digit_mask: bool = False,
) -> GuardSeams:
    mask = DigitRunMask() if digit_mask else IdentityMask()
    return GuardSeams(
        matcher=CategoryLookup(hits or {}), mask=mask, check=TableCheck(findings or {})
    )
