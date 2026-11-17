"""The frozen red-team suite, one test per case: `test_case[<case id>]` (M7 PR 2; D8 (a)).

Every case runs through `run_case`, the scorer `redteam run` uses too, and carries exactly one
mark from `marks.redteam_param` (hard: none; known_gap and pending: strict xfails; soft: its
outcome code recorded as the JUnit property `outcome`). `scripts/redteam_gate.py` reads this
module's JUnit results: every frozen id exactly once, no plain skip, only closed reasons, soft
over-escalations within the frozen ceiling, and with `--final` no pending reason but
`pending:M5-H1`.

In PR 2 no guard is built, so every case is pending on the PR that registers its path's runner
(`redteam.plan.FRONTIER`); each turns into its gate label when that PR lands. The pure categories
(A, E, G, H, I, L) will run in process against the guard objects; F, J, K and one end-to-end case
per surface through the module-scoped served app (`served.py`), behind the `integration` mark.
"""

from collections.abc import Iterator

import pytest

from tests.redteam.marks import record_outcome, redteam_param
from tests.redteam.served import ServedSuite, served_suite
from tests.redteam.suite import CASES, END_TO_END, GENERATOR, label_of
from vectapilot_api.redteam.harness import RUNNERS, RunEnv, run_case
from vectapilot_api.redteam.plan import environment_of, mark_plan, path_of

PARAMS = [
    redteam_param(
        case,
        mark_plan(case, label_of(case.id), RUNNERS),
        environment_of(case, END_TO_END),
    )
    for case in sorted(CASES.values(), key=lambda c: c.id)
]


@pytest.fixture(scope="module")
def redteam_app() -> Iterator[ServedSuite]:
    """The served app for the database cases, built once per module (CI time)."""
    yield from served_suite()


@pytest.mark.parametrize("case_id", PARAMS)
def test_case(case_id: str, request: pytest.FixtureRequest) -> None:
    case = CASES[case_id]
    label = label_of(case_id)
    environment = environment_of(case, END_TO_END)
    runnable = path_of(case) in RUNNERS
    app = request.getfixturevalue("redteam_app") if runnable and environment == "database" else None

    scored = run_case(case, label, RunEnv(GENERATOR, environment, app))

    if label == "soft":
        record_outcome(request, scored.result.outcome)
