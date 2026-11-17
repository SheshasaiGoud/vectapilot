"""M7 PR 2: the replay stand-in for M5's H1 `run_turn` (`redteam/replay.py`).

It plays a case's frozen steps in order and never chooses: the step model is asked once per
step (so the fake provider receives every call) but its answer is ignored, and the only envelope
fact it reads is the proposal its scripted write makes. What it plays is fixed before the turn,
on the read-only service without the write steps. The scripted step model and tools are
`tests/agent/fakes.py`'s, which are not loops either; the last test plays two frozen A cases
through the real gateway and rt-shop's real read-only step schema.
"""

import asyncio
import json
from typing import Final, cast

import pytest

from tests.agent.fakes import (
    LIMITS,
    ScriptedStepModel,
    ScriptedTools,
    failed,
    handoff,
    observed,
    proposed,
    reply,
)
from tests.answer.apps import gateway_for
from tests.redteam.suite import CASES, DATASET_DIR
from vectapilot_api.agent.step_model import GatewayStepModel
from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.agent.types import (
    ConfirmationRequired,
    LoopLimits,
    ModelHandoffReason,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.redteam.businesses import fixture_business_tools
from vectapilot_api.redteam.cases import AnswerStep, CallToolStep, HandoffStep, ReplyStep
from vectapilot_api.redteam.replay import (
    AgentScriptStep,
    ScriptError,
    agent_steps,
    answer_texts,
    playable,
    replay_loop,
    step_json,
    step_replies,
)
from vectapilot_api.tools.config import WRITE_TOOLS, ToolName, ToolsConfig
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.types import ToolErrorCode

SLOTS = CallToolStep(action="call_tool", tool=ToolName.CHECK_SLOTS, arguments={"date_from": "x"})
BOOK = CallToolStep(action="call_tool", tool=ToolName.BOOK, arguments={"slot_id": "S-17"})
REPLY = ReplyStep(action="reply", text="Done {{pii:phone_in}}")
HANDOFF = HandoffStep(action="handoff", reason=ModelHandoffReason.SAFETY)


def play(
    steps: list[AgentScriptStep],
    model: ScriptedStepModel,
    tools: ScriptedTools,
    limits: LoopLimits = LIMITS,
    *,
    read_only: bool = False,
) -> TurnOutcome:
    loop = replay_loop(steps, str.upper, read_only=read_only)
    return asyncio.run(loop("hello", step_model=model, tools=tools, limits=limits))


def test_it_plays_the_script_whatever_the_model_answers() -> None:
    # The model answers "hand off" every time; the replay plays its own script regardless.
    model = ScriptedStepModel([handoff(ModelHandoffReason.OTHER)] * 2)
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS, "slot S-17 free")])

    outcome = play([SLOTS, REPLY], model, tools)

    assert outcome == TurnReply("DONE {{PII:PHONE_IN}}")  # the fill ran on the reply
    assert tools.calls == [("run", ToolName.CHECK_SLOTS, {"date_from": "X"})]
    assert [len(t.observations) for t in model.transcripts] == [0, 1]
    assert "slot S-17 free" in model.transcripts[1].observations[0].text


def test_the_replay_stops_at_its_scripted_write_with_the_proposal() -> None:
    model = ScriptedStepModel([reply(), reply()])
    tools = ScriptedTools([proposed()])

    outcome = play([BOOK, REPLY], model, tools)

    assert isinstance(outcome, ConfirmationRequired)
    assert tools.calls[0][0] == "propose"
    assert len(model.transcripts) == 1


def test_a_scripted_write_the_sandbox_refuses_is_a_fixture_error() -> None:
    model = ScriptedStepModel([reply(), reply()])
    tools = ScriptedTools([failed(ToolName.BOOK, ToolErrorCode.NOT_ENABLED)])

    with pytest.raises(ScriptError, match="refused a scripted write"):
        play([BOOK, HANDOFF], model, tools)
    assert len(model.transcripts) == 1  # nothing is played after it


def test_the_read_only_replay_and_its_fake_script_leave_out_every_write_step() -> None:
    model = ScriptedStepModel([reply(), reply()])
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS)])

    outcome = play([SLOTS, BOOK, REPLY], model, tools, read_only=True)

    assert outcome == TurnReply("DONE {{PII:PHONE_IN}}")
    assert [call[0] for call in tools.calls] == ["run"]  # never a proposal
    assert len(model.transcripts) == 2
    replies = step_replies([SLOTS, BOOK, REPLY], read_only=True)
    assert [json.loads(r)["step"]["action"] for r in replies] == ["call_tool", "reply"]


@pytest.mark.parametrize(
    ("steps", "read_only"),
    [
        ([], False),
        ([SLOTS], False),  # a script that runs out
        ([SLOTS, BOOK], True),  # ends in a write, which the read-only service never plays
        ([BOOK], True),
    ],
)
def test_a_script_that_does_not_end_the_turn_is_refused_when_built(
    steps: list[AgentScriptStep], read_only: bool
) -> None:
    with pytest.raises(ScriptError):
        replay_loop(steps, read_only=read_only)


def test_a_script_longer_than_the_step_cap_is_refused_before_any_model_call() -> None:
    model = ScriptedStepModel([reply()] * 3)
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS)] * 2)

    with pytest.raises(ScriptError, match="step cap"):
        play([SLOTS, SLOTS, REPLY], model, tools, LoopLimits(max_steps=2))
    assert model.transcripts == []


def test_the_step_models_error_propagates_to_the_service() -> None:
    model = ScriptedStepModel([ValueError("schema refused")])

    with pytest.raises(ValueError, match="schema refused"):
        play([BOOK], model, ScriptedTools())


def test_an_answer_step_is_refused_and_split_out() -> None:
    answer = AnswerStep(action="answer", text="An answer.")

    with pytest.raises(ScriptError, match="answer route"):
        replay_loop([answer])
    case = CASES["b-007"]
    assert answer_texts(case) and not agent_steps(case)


def test_step_json_is_the_agents_structured_output_with_texts_filled() -> None:
    assert step_json(REPLY, str.upper) == {
        "step": {"action": "reply", "text": "DONE {{PII:PHONE_IN}}"}
    }
    assert step_json(HANDOFF) == {"step": {"action": "handoff", "reason": "safety"}}
    replies = step_replies([SLOTS, BOOK])
    assert [json.loads(r)["step"]["tool"] for r in replies] == ["check_slots", "book"]


NO_DATABASE: Final = cast(TransactionSource, None)


@pytest.mark.parametrize(("case_id", "calls"), [("a-001", 1), ("a-005", 2)])
def test_an_a_case_replays_on_the_real_read_only_schema_and_gateway(
    case_id: str, calls: int
) -> None:
    # leo's PR 2 review: the scripted write used to reach the read-only schema, whose refusal
    # spent the fake's next reply in the gateway's repair, so no A case could pass.
    case = CASES[case_id]
    shop = fixture_business_tools(DATASET_DIR, "rt-shop").config
    read_only = ToolsConfig.model_validate(
        shop.model_dump() | {"enabled_tools": sorted(set(shop.enabled_tools) - WRITE_TOOLS)}
    )
    schema = build_agent_step(ToolExecutor(NO_DATABASE, read_only))
    steps = agent_steps(case)
    provider = FakeProvider([FakeReply(text=t) for t in step_replies(steps, read_only=True)])
    model = GatewayStepModel(
        gateway_for(provider),
        schema,
        business_id="rt-shop",
        trace_id="1" * 32,
        today=read_only.sandbox_date,
        timezone=read_only.timezone,
    )
    reads = playable(steps, read_only=True)[:-1]
    tools = ScriptedTools([observed(s.tool) for s in reads if isinstance(s, CallToolStep)])

    loop = replay_loop(steps, read_only=True)
    outcome = asyncio.run(loop("m", step_model=model, tools=tools, limits=LIMITS))

    assert isinstance(outcome, TurnReply)
    assert len(provider.calls) == calls  # one call per played step: no repair
    assert all(call[0] == "run" for call in tools.calls)  # nothing proposed
