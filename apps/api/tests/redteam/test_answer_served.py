"""M7 PR 8: the answer path's runner through the served app (integration: real Postgres).

The suite's database environment (`served.py`) with the answer runner on stand-in seams
(`standins.py`: none of them is H1, H2 or H3), scored by `run_case` with every probe saying
"written". A pass here means the answer path's database leg is wired: the route, the business
lookup, the case's planted rows found by the real vector search as the app role (RLS), the
per-case pipeline behind the route's two guard dependencies, and the `guard` record:
- b-001: of the two planted chunks the poisoned one is retrieved, dropped by the chunk screen and
  never reaches the answer model, which answers from the other;
- h-009: H3's (stand-in) finding on the answer replaces it with a `guard_output` hand-off.
The served app itself keeps `guard_answer` off, so k-001's search stays plain.
"""

from collections.abc import Iterator

import pytest

from tests.conftest import integration
from tests.redteam.served import ANSWER_CASES, ServedSuite, served_suite
from tests.redteam.standins import ALL_WRITTEN, filled, seams
from tests.redteam.suite import CASES, DATASET_DIR, GENERATOR, label_of
from vectapilot_api.guards.types import DecidedBy, Finding, TermCategory
from vectapilot_api.redteam.agent import GuardSeams
from vectapilot_api.redteam.answer import answer_runner
from vectapilot_api.redteam.chunks import poisoned
from vectapilot_api.redteam.harness import RunEnv, Scored, run_case
from vectapilot_api.redteam.plan import CasePath
from vectapilot_api.redteam.replay import answer_texts
from vectapilot_api.redteam.scoring import ExpectationMiss, Verdict

pytestmark = [integration]


@pytest.fixture(scope="module")
def redteam_app() -> Iterator[ServedSuite]:
    yield from served_suite()


def scored(served: ServedSuite, case_id: str, stand_ins: GuardSeams) -> Scored:
    runner = answer_runner(stand_ins)
    return run_case(
        CASES[case_id],
        label_of(case_id),
        RunEnv(GENERATOR, "database", served, DATASET_DIR),
        runners={CasePath.ANSWER: runner},
        probes=ALL_WRITTEN,
    )


def test_the_answer_cases_rows_are_planted_and_the_app_keeps_guard_answer_off(
    redteam_app: ServedSuite,
) -> None:
    assert ANSWER_CASES == ("b-001", "h-009")
    for case_id in ANSWER_CASES:
        planted = redteam_app.answer_chunks[case_id]
        assert set(planted) == {chunk.id for chunk in CASES[case_id].fixture.chunks}
    assert redteam_app.app.state.answer_guard is None  # each case brings its own pipeline


def test_b_001_the_poisoned_row_is_retrieved_dropped_and_never_reaches_the_model(
    redteam_app: ServedSuite,
) -> None:
    case = filled("b-001")
    [slug] = poisoned(case.case)
    text = case.text(next(c.text for c in case.case.fixture.chunks if c.id == slug))
    result = scored(redteam_app, "b-001", seams(hits={text: TermCategory.INJECTION}))

    assert result.verdict is Verdict.PASSED
    assert (result.result.outcome, result.result.decided_by) == (
        "dropped",
        DecidedBy.INJECTION_CHUNK,
    )
    assert result.result.reached is True  # the real search returned it before the screen
    assert result.result.model_calls == 1


def test_b_001_a_screen_that_misses_lets_the_row_reach_the_model(
    redteam_app: ServedSuite,
) -> None:
    with pytest.raises(ExpectationMiss):
        scored(redteam_app, "b-001", seams())


def test_h_009_an_output_finding_on_the_answer_is_a_guard_output_handoff(
    redteam_app: ServedSuite,
) -> None:
    case = filled("h-009")
    answer = case.text(answer_texts(case.case)[-1])
    stand_ins = seams(findings={answer: (Finding.OUTPUT_MEDICAL_ADVICE,)})
    result = scored(redteam_app, "h-009", stand_ins)

    assert result.verdict is Verdict.PASSED
    assert (result.result.outcome, result.result.handoff_reason) == ("handoff", "guard_output")
    assert result.result.decided_by is DecidedBy.OUTPUT_MEDICAL_ADVICE
    assert result.result.reached is True
    assert result.result.model_calls == 1
