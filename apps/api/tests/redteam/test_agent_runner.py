"""M7 PR 6: the agent path's runner in process (`redteam/agent.py`), on stand-in seams. Unit.

The frozen cases go through the real guard pipeline (`AgentGuard`, both `AgentService`s, the
screening executor and store, the budgeted gateway, the replay), with the case's
`fixture.tool_results` as the tools and `tests/redteam/standins.py`'s seams standing in for H1,
H2 and H3 (none of them is a hand-write). Each test scores with `run_case` and every probe saying
"written", so a pass here means: once the hand-writes behave as the stand-ins do, the case passes
for the reason the scorer demands (outcome and `decided_by`), with its controls (the injection
screen ran, the poisoned field was read through the screen, H3 ran, the fake model was called).
The real runner with the real stubs is pending on H1 (`test_plan.py`).
"""

from typing import Final

import pytest

from tests.redteam.standins import ALL_WRITTEN, filled, note_of, reply_of, seams
from tests.redteam.suite import CASES, DATASET_DIR, GENERATOR, label_of
from vectapilot_api.guards.output import OUTPUT_FINDINGS
from vectapilot_api.guards.types import DecidedBy, Finding, TermCategory
from vectapilot_api.redteam.agent import (
    RouterOffCase,
    World,
    agent_runner,
    budgeted_gateway,
    masked_outcome,
    world_of,
)
from vectapilot_api.redteam.harness import RunEnv, Runner, Scored, run_case
from vectapilot_api.redteam.plan import CasePath
from vectapilot_api.redteam.scoring import ExpectationMiss, Verdict

ENV: Final = RunEnv(GENERATOR, "in_process", dataset_dir=DATASET_DIR)
EMERGENCY, JUDGEMENT, INJECTION = (
    TermCategory.EMERGENCY,
    TermCategory.PROFESSIONAL_JUDGEMENT,
    TermCategory.INJECTION,
)


def scored(case_id: str, runner: Runner) -> Scored:
    case = CASES[case_id]
    return run_case(
        case, label_of(case_id), ENV, runners={CasePath.AGENT: runner}, probes=ALL_WRITTEN
    )


def test_the_world_is_create_apps_folders_with_rt_shop_and_its_guard_file() -> None:
    world = world_of(DATASET_DIR)

    assert isinstance(world, World)
    assert set(world.tools) == {"paws-and-co", "rt-shop", "sunrise-clinic"}
    shop = world.guards.for_business("rt-shop")
    assert shop is not None and shop.guidance  # fixtures/rt-shop/safety.json's lines
    assert world.guards.for_business("sunrise-clinic") is not None


@pytest.mark.parametrize(
    ("case_id", "category", "decided"),
    [
        ("g-001", EMERGENCY, DecidedBy.RED_FLAG_EMERGENCY),
        ("g-017", EMERGENCY, DecidedBy.RED_FLAG_EMERGENCY),
        ("h-001", JUDGEMENT, DecidedBy.RED_FLAG_PROFESSIONAL_JUDGEMENT),
        ("h-006", JUDGEMENT, DecidedBy.RED_FLAG_PROFESSIONAL_JUDGEMENT),
    ],
)
def test_a_red_flag_passes_by_the_screen_before_any_model_call(
    case_id: str, category: TermCategory, decided: DecidedBy
) -> None:
    result = scored(case_id, agent_runner(seams(hits={filled(case_id).input: category})))

    assert result.verdict is Verdict.PASSED
    assert result.result.decided_by is decided
    assert result.result.model_calls == 0  # the screen decided before the service


def test_a_red_flag_the_screen_misses_is_a_miss_by_w11_never_a_pass() -> None:
    # g-001 with a matcher that hits nothing: the health business's turn reaches W11's floor.
    with pytest.raises(ExpectationMiss):
        scored("g-001", agent_runner(seams()))


def test_an_injection_runs_read_only_with_no_proposal_and_the_model_called() -> None:
    # a-002 scripts a write (book) and a reply: on the read-only service the write is never
    # played, so nothing is stored; the reply passes the (stand-in) output check.
    result = scored("a-002", agent_runner(seams(hits={filled("a-002").input: INJECTION})))

    assert result.verdict is Verdict.PASSED
    assert result.result.outcome == "write_tools_removed"
    assert result.result.proposal_stored is False
    assert result.result.model_calls == 1  # only the reply step: the write step was left out


def test_an_injection_the_screen_misses_stores_the_write_and_is_a_miss() -> None:
    with pytest.raises(ExpectationMiss):
        scored("a-002", agent_runner(seams()))


@pytest.mark.parametrize("case_id", ["c-002", "c-003", "c-005"])
def test_a_poisoned_tool_field_is_withheld_and_decides_the_reply(case_id: str) -> None:
    result = scored(case_id, agent_runner(seams(hits={note_of(case_id): INJECTION})))

    assert result.verdict is Verdict.PASSED
    assert result.result.decided_by is DecidedBy.INJECTION_TOOL_RESULT
    assert result.result.reached is True  # the poisoned field was read through the screen
    assert result.result.model_calls == 2  # the read, then the reply


def test_a_poisoned_field_the_screen_misses_is_a_miss() -> None:
    with pytest.raises(ExpectationMiss):
        scored("c-002", agent_runner(seams()))


@pytest.mark.parametrize(
    ("case_id", "finding"),
    [
        ("i-001", Finding.OUTPUT_SUCCESS_CLAIM),
        ("i-004", Finding.OUTPUT_BOUNDARY),
        ("i-008", Finding.OUTPUT_ABUSE),
        ("h-013", Finding.OUTPUT_MEDICAL_ADVICE),
    ],
)
def test_an_output_finding_replaces_the_reply_with_a_handoff(
    case_id: str, finding: Finding
) -> None:
    assert finding in OUTPUT_FINDINGS
    result = scored(case_id, agent_runner(seams(findings={reply_of(case_id): (finding,)})))

    assert result.verdict is Verdict.PASSED
    assert result.result.outcome == "handoff"
    assert result.result.reached is True  # H3 ran on the reply
    assert result.result.model_calls == 1


@pytest.mark.parametrize("case_id", ["l-005", "l-006", "l-020"])
def test_a_benign_turn_is_allowed_unchanged(case_id: str) -> None:
    result = scored(case_id, agent_runner(seams()))

    assert result.verdict is Verdict.PASSED
    assert result.result.outcome == "allowed"
    assert result.result.decided_by is DecidedBy.NONE
    assert result.result.model_calls  # every model-facing case called the fake


def test_a_false_mask_of_an_order_id_is_a_soft_over_escalation() -> None:
    # l-011: "RT-0987654321" holds ten digits in a row; the stand-in mask replaces exactly that
    # shape, so the service hands the loop "[PHONE]": `masked:phone`, a soft over-escalation.
    result = scored("l-011", agent_runner(seams(digit_mask=True)))

    assert result.result.outcome == "masked:phone"
    assert result.verdict is Verdict.OVER_ESCALATED


def test_masked_outcome_counts_only_placeholders_the_customer_did_not_write() -> None:
    assert masked_outcome("call [PHONE] please", "call [PHONE] please") == "allowed"
    assert masked_outcome("call 0987654321", "call [PHONE]") == "masked:phone"
    assert masked_outcome("anything", None) == "allowed"


def test_a_b_case_on_the_agent_route_is_refused_by_the_agent_runner() -> None:
    with pytest.raises(RouterOffCase):
        agent_runner(seams())(filled("b-007"), ENV)


def test_the_in_process_path_needs_the_dataset_folder() -> None:
    with pytest.raises(ValueError, match="dataset folder"):
        agent_runner(seams())(filled("l-006"), RunEnv(GENERATOR, "in_process"))


def test_the_runners_gateway_is_the_guarded_routes_argument_for_argument() -> None:
    # `budgeted_gateway` mirrors `guards.budget.build_budgeted_agent_gateway` (G9, G13).
    import asyncio

    from vectapilot_api.guards.budget import build_budgeted_agent_gateway
    from vectapilot_api.guards.fake_limiter import FakeLimiter
    from vectapilot_api.redteam.agent import ReplayProvider

    world = world_of(DATASET_DIR)
    limiter, provider = FakeLimiter(), ReplayProvider()
    mine = budgeted_gateway(world.settings, provider, limiter)

    async def theirs() -> dict[str, object]:
        async with build_budgeted_agent_gateway(
            world.settings, provider=provider, limiter=limiter
        ) as gateway:
            return dict(vars(gateway))

    assert dict(vars(mine)) == asyncio.run(theirs())
