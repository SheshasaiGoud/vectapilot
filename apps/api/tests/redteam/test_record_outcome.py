"""M7 PR 2: a soft case's recorded outcome really reaches the JUnit report the gate reads
(noah's PR 2 review).

`marks.record_outcome` appends to the test's user properties instead of calling pytest's
`record_property` fixture, which warns under the default `xunit2` report family (and this suite
runs warnings as errors). A child pytest, under the default family and with warnings as errors,
records one outcome; its JUnit report must hold `<property name="outcome" value="allowed"/>`,
exactly what `scripts/redteam_gate.py` reads. The red proof: the same child without the call has
no such property.

In PR 2 every case is pending, so `test_cases.test_case`'s own soft branch never runs; the last
test here calls it with a scored soft result to pin that it records the outcome (the mutation
check). It lives here, not in the parametrised module, whose every test must be a case.
"""

import xml.etree.ElementTree as ElementTree
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

import tests.redteam.test_cases as suite_tests
from tests.redteam.test_leak_canary import API_DIR, child_output
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.redteam.harness import Scored
from vectapilot_api.redteam.scoring import CaseResult, Verdict

CHILD: Final = f"""
import sys

sys.path.insert(0, {str(API_DIR)!r})

from tests.redteam.marks import record_outcome


def test_case(request):
    record_outcome(request, "allowed")
"""


def outcome_properties(tmp_path: Path) -> list[str | None]:
    # S314: the report is the child pytest's, written into tmp_path by this test.
    root = ElementTree.parse(tmp_path / "child.xml").getroot()  # noqa: S314
    return [p.get("value") for p in root.iter("property") if p.get("name") == "outcome"]


def test_record_outcome_writes_the_property_the_gate_reads(tmp_path: Path) -> None:
    code, written = child_output(tmp_path, CHILD, "-W", "error")

    assert code == 0, written[-2000:]
    assert outcome_properties(tmp_path) == ["allowed"]


def test_red_proof_without_the_call_there_is_no_outcome(tmp_path: Path) -> None:
    silent = CHILD.replace('record_outcome(request, "allowed")', "pass")
    assert silent != CHILD

    code, _ = child_output(tmp_path, silent, "-W", "error")

    assert code == 0
    assert outcome_properties(tmp_path) == []


def test_the_suite_records_a_soft_cases_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CaseResult(case_id="l-007", outcome="allowed", decided_by=DecidedBy.NONE)
    scored = Scored(result=result, verdict=Verdict.PASSED)
    monkeypatch.setattr(suite_tests, "run_case", lambda *args, **kwargs: scored)
    request = SimpleNamespace(node=SimpleNamespace(user_properties=[]))

    suite_tests.test_case("l-007", request)  # type: ignore[arg-type]

    assert request.node.user_properties == [("outcome", "allowed")]
