"""The one helper that makes every red-team mark (M7 PR 2; scoring rules, "CI semantics", D8).

pytest's JUnit report keeps an xfail's reason but neither `strict` nor `raises`
(`scripts/canary_gate.py`), so the gate cannot tell a hand-made mark from this one. Two tests
close that: `tests/test_redteam_gate.py` proves no other file under `tests/redteam/` names
`xfail`, and `tests/redteam/conftest.py` checks every collected mark's `strict`, `raises` and
reason at collection time.

- `hard`: no mark; the case must pass.
- `known_gap`: `xfail(strict=True, raises=ExpectationMiss, reason="known_gap:<id>")`, so a gap
  that starts to pass fails CI (XPASS(strict)) and the gap list stays true.
- pending: `xfail(strict=True, raises=PendingHandWrite, reason="pending:<PR n|Hn|M5-H1>")`.
- `soft`: no mark; the test records its outcome code as the JUnit property `outcome`
  (`record_outcome`).
A case that needs the database and has a runner also gets the repository's `integration` mark;
a pending one never needs it (it raises before anything runs), so a unit-only run reports no
plain skip.
"""

from collections.abc import Iterable
from typing import Final

import pytest
from _pytest.mark.structures import Mark, ParameterSet

from tests.conftest import integration
from vectapilot_api.redteam.cases import Case
from vectapilot_api.redteam.plan import PENDING_REASONS, Environment, MarkPlan, known_gap_reason
from vectapilot_api.redteam.scoring import ExpectationMiss, PendingHandWrite

XFAIL_ARGUMENTS: Final = frozenset({"strict", "raises", "reason"})


def redteam_param(case: Case, plan: MarkPlan, environment: Environment) -> ParameterSet:
    """`case`'s parameter for `test_case`, id `<case id>`, with its one mark (module docstring)."""
    marks: list[pytest.MarkDecorator] = []
    reason = plan.reason or ""  # every pending or known_gap plan has one; "" fails the hook
    if plan.kind == "pending":
        marks.append(pytest.mark.xfail(strict=True, raises=PendingHandWrite, reason=reason))
    elif plan.kind == "known_gap":
        marks.append(pytest.mark.xfail(strict=True, raises=ExpectationMiss, reason=reason))
    if plan.kind != "pending" and environment == "database":
        marks.append(integration)
    return pytest.param(case.id, marks=marks, id=case.id)


def mark_problems(case_id: str | None, marks: Iterable[Mark]) -> list[str]:
    """What is wrong with one collected test's xfail marks (module docstring): more than one, not
    strict, an argument other than `strict`, `raises` and `reason` (`run=False` would never run
    the case), a `raises` other than the scorer's two, or a reason outside the closed forms or
    not matching its `raises` (a known gap names its own case id). Codes and ids only."""
    xfails = [mark for mark in marks if mark.name == "xfail"]
    if not xfails:
        return []
    if len(xfails) > 1:
        return ["more than one xfail mark"]
    mark = xfails[0]
    kwargs = dict(mark.kwargs)
    problems: list[str] = []
    if mark.args or kwargs.get("strict") is not True:
        problems.append("an xfail that is not strict (or has a condition)")
    if set(kwargs) - XFAIL_ARGUMENTS:
        problems.append("an xfail with an argument other than strict, raises and reason")
    raises, reason = kwargs.get("raises"), kwargs.get("reason")
    if raises is PendingHandWrite:
        if reason not in PENDING_REASONS:
            problems.append("a pending xfail whose reason is not in the closed list")
    elif raises is ExpectationMiss:
        if case_id is None or reason != known_gap_reason(case_id):
            problems.append("a known_gap xfail whose reason is not known_gap:<its case id>")
    else:
        problems.append("an xfail whose raises is not ExpectationMiss or PendingHandWrite")
    return problems


OUTCOME_PROPERTY = "outcome"


def record_outcome(request: pytest.FixtureRequest, code: str) -> None:
    """Record a soft case's outcome code (never text) for the gate, as `record_property("outcome",
    code)` would. That fixture warns under pytest's default `xunit2` report family (pytest 9:
    "incompatible with junit_family"), and this suite runs warnings as errors; the warning is
    about the XML schema, and pytest writes a test's user properties under every family. So this
    appends to the same list the fixture appends to, without switching CI's report family."""
    request.node.user_properties.append((OUTCOME_PROPERTY, code))
