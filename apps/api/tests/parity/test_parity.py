"""The widget's red-team parity (M8 PR 2a; S6, S7, ADR 0019 decision 8): every frozen case of the
agent route and the confirm route, through `/v1/agent` and through `/v1/widget/messages`, with the
same decision. Unit: in process, no database, no Redis (`tests/parity/harness.py`).

`test_parity[<run>-<id>]` runs one case in one run (`scripts.parity_gate.RUNS`) and records its
outcome code for the gate; `test_parity_exclusion[<id>]` stands for one case of the closed
exclusion list, checked against the frozen suite. `scripts/parity_gate.py` reads both from the
JUnit report: every frozen id exactly once per run, run or excluded, no skip, and an xfail only
where `/v1/agent` has the same id with the same reason (this module makes none). While H1 is a
stub the `seams` run is `guard_error` on both routes: the `standins` run is the meaningful one,
and the gate prints its outcome distribution. Each `standins` case must also reach its frozen
expectation (`expected` = `met`), but for the gate's closed list `STANDIN_MISSES`, which must
still miss it (noah's PR 2 review: identical on both routes is no proof when a case fires on
neither).

This module holds only the two forms the gate reads (`scripts/parity_gate.py` refuses any other
test name here); the checks of the gate's lists are `tests/parity/test_parity_lists.py`.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.parity_gate import (
    EXCLUDED,
    MET,
    MISSED,
    REASONS,
    RUNS,
    STANDIN_MISSES,
    STANDINS,
)
from tests.parity.harness import (
    INCLUDED,
    REPLAYED,
    parity_app,
    seams_for,
    through_agent,
    through_widget,
)
from tests.redteam.suite import CASES, GENERATOR
from vectapilot_api.redteam.harness import fill


@pytest.fixture(scope="module")
def served() -> Iterator[tuple[FastAPI, TestClient]]:
    with parity_app() as app:
        yield app


@pytest.mark.parametrize(
    ("run", "case_id"),
    [pytest.param(run, case_id, id=f"{run}-{case_id}") for run in RUNS for case_id in INCLUDED],
)
def test_parity(
    run: str, case_id: str, served: tuple[FastAPI, TestClient], request: pytest.FixtureRequest
) -> None:
    app, client = served
    filled = fill(CASES[case_id], GENERATOR)

    agent = through_agent(app, client, filled, seams_for(run, filled))
    widget = through_widget(app, client, filled, seams_for(run, filled))

    assert widget.decision == agent.decision
    assert widget.outcome == agent.outcome
    request.node.user_properties.append(("outcome", agent.outcome))
    request.node.user_properties.append(("decided_by", agent.decided_by or "none"))
    if run == STANDINS:  # the run that means something: it must reach the frozen expectation
        met = agent.outcome == CASES[case_id].expect
        request.node.user_properties.append(("expected", MET if met else MISSED))
        assert met == (case_id not in STANDIN_MISSES), (case_id, agent.outcome)


@pytest.mark.parametrize("case_id", sorted(EXCLUDED))
def test_parity_exclusion(case_id: str) -> None:
    # S7: a closed list of frozen agent-route or confirm-route cases, each with a closed reason.
    assert case_id in REPLAYED
    assert EXCLUDED[case_id] in REASONS
