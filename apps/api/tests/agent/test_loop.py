"""H1 tests: `run_turn`, the agent loop (guide: docs/learning/m5-agent-loop.md).

`run_turn` is [HAND-WRITE]: shesha writes it (D5 (b)). Until then it raises NotImplementedError,
so every test here is marked `xfail(strict=True, raises=NotImplementedError)` through the one
switch `PENDING_HAND_WRITE` in tests/canary/read_paths.py (the same switch the fake-provider
turns and the `POST /v1/agent` canary write path wait for):

- not implemented yet          → "xfailed" (expected);
- implemented, wrong behaviour → a normal failure (only NotImplementedError counts as expected);
- implemented, right behaviour → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, remove "run_turn" from `PENDING_HAND_WRITE` and run the
full check: every test must then simply pass. Do not edit a test to make it pass; change the code.

Fakes only (tests/agent/fakes.py): `ScriptedStepModel` plays steps, `ScriptedTools` plays
envelopes, and both record what the loop asked. Each expected outcome is built *before* the call.
The section headings follow the contract's numbered rules (`run_turn`'s docstring).
"""

import asyncio
import logging
from typing import Final

import pytest
from sqlalchemy.exc import OperationalError

from tests.agent.fakes import (
    BOOK,
    LIMITS,
    MESSAGE,
    ORDER,
    SLOTS,
    TICKET,
    ScriptedStepModel,
    ScriptedTools,
    as_call,
    failed,
    handoff,
    observed,
    proposed,
    reply,
    seen,
)
from tests.canary.read_paths import pending_hand_write
from tests.intents.fakes import every_gateway_error
from vectapilot_api.agent.loop import run_turn
from vectapilot_api.agent.types import (
    FAILURE_CODES,
    AgentStep,
    ConfirmationRequired,
    LoopHandoffReason,
    LoopLimits,
    ModelHandoffReason,
    Transcript,
    TurnHandoff,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.evalrun.budget import CapReached
from vectapilot_api.gateway.errors import GatewayError, StructuredOutputInvalid
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import ToolEnvelope, ToolErrorCode, ToolKind

pytestmark = [pytest.mark.anyio, *pending_hand_write("run_turn")]

FAILURES: Final = sorted(FAILURE_CODES)
"""`invalid_arguments`, `timeout`, `unavailable`."""
OTHER_GATEWAY_ERRORS: Final = [
    error for error in every_gateway_error() if not isinstance(error, StructuredOutputInvalid)
]


async def turn(
    model: ScriptedStepModel, tools: ScriptedTools, limits: LoopLimits = LIMITS
) -> TurnOutcome:
    return await run_turn(MESSAGE, step_model=model, tools=tools, limits=limits)


# ── Rule 2: reply and handoff end the turn as they are ──────────────────────────────────────


async def test_a_reply_ends_the_turn_as_the_model_wrote_it() -> None:
    expected = TurnReply("Monday has free slots at 10:20.")
    model = ScriptedStepModel([reply("Monday has free slots at 10:20.")])
    tools = ScriptedTools()

    assert await turn(model, tools) == expected
    assert tools.calls == []
    assert model.transcripts == [Transcript(MESSAGE)]  # the first step sees the message only


@pytest.mark.parametrize("reason", list(ModelHandoffReason))
async def test_a_handoff_ends_the_turn_with_the_models_own_reason(
    reason: ModelHandoffReason,
) -> None:
    # `safety` too: the service, not the loop, makes it urgent and adds the guidance (W11).
    expected = TurnHandoff(reason)
    model = ScriptedStepModel([handoff(reason)])
    tools = ScriptedTools()

    assert await turn(model, tools) == expected
    assert tools.calls == []


SAFETY_LATE: Final = [
    pytest.param([], [], 1, id="the only allowed step"),
    pytest.param([SLOTS], [observed(ToolName.CHECK_SLOTS)], 2, id="the last step, after a read"),
    pytest.param(
        [SLOTS], [failed(ToolName.CHECK_SLOTS, ToolErrorCode.TIMEOUT)], 4, id="after a failure"
    ),
    pytest.param(
        [BOOK], [failed(ToolName.BOOK, ToolErrorCode.SLOT_UNAVAILABLE)], 4, id="after a refusal"
    ),
]
"""Earlier steps, their envelopes and the step cap before a `handoff(safety)` [PR 4 review,
zara]: the realistic emergency comes after a read, a failure or on the last allowed step."""


@pytest.mark.parametrize(("before", "envelopes", "max_steps"), SAFETY_LATE)
async def test_a_safety_handoff_wins_after_reads_failures_and_on_the_last_step(
    before: list[AgentStep], envelopes: list[ToolEnvelope], max_steps: int
) -> None:
    # The step is looked at before the cap or the failure count: an emergency must never turn
    # into `step_cap` or `tool_failure` (the service would answer at normal priority, no guidance).
    expected = TurnHandoff(ModelHandoffReason.SAFETY)
    model = ScriptedStepModel([*before, handoff(ModelHandoffReason.SAFETY)])
    tools = ScriptedTools(list(envelopes))

    assert await turn(model, tools, LoopLimits(max_steps=max_steps)) == expected
    assert len(tools.calls) == len(before)


# ── Rule 3: a read tool runs, and its observation goes back to the model ────────────────────


async def test_a_read_tool_runs_with_the_models_arguments() -> None:
    envelope = observed(ToolName.CHECK_SLOTS)
    model = ScriptedStepModel([SLOTS, reply()])
    tools = ScriptedTools([envelope])

    await turn(model, tools)

    assert tools.calls == [
        (
            "run",
            ToolName.CHECK_SLOTS,
            {"date_from": "2026-11-16", "date_to": "2026-11-17", "service": None},
        )
    ]


async def test_the_next_step_sees_the_call_and_only_the_envelopes_observation() -> None:
    # The envelope also holds row business ids (for the eval's leak check): never the model's.
    envelope = observed(ToolName.CHECK_SLOTS, business_ids=frozenset({"another-business"}))
    expected = Transcript(MESSAGE).observe(seen(SLOTS, envelope))
    model = ScriptedStepModel([SLOTS, reply()])
    tools = ScriptedTools([envelope])

    await turn(model, tools)

    assert model.transcripts == [Transcript(MESSAGE), expected]
    assert "another-business" not in repr(model.transcripts[1])


async def test_observations_accumulate_in_call_order() -> None:
    slots, order = observed(ToolName.CHECK_SLOTS, "slots"), observed(ToolName.ORDER_STATUS, "order")
    first = Transcript(MESSAGE).observe(seen(SLOTS, slots))
    second = first.observe(seen(ORDER, order))
    expected = TurnReply("Both found.")
    model = ScriptedStepModel([SLOTS, ORDER, reply("Both found.")])
    tools = ScriptedTools([slots, order])

    assert await turn(model, tools) == expected
    assert model.transcripts == [Transcript(MESSAGE), first, second]


# ── Rule 4: a write tool is proposed, never run ─────────────────────────────────────────────


@pytest.mark.parametrize("write", [BOOK, TICKET], ids=["book", "create_ticket"])
async def test_a_write_tool_is_proposed_never_run(write: AgentStep) -> None:
    envelope = proposed()
    model = ScriptedStepModel([write])
    tools = ScriptedTools([envelope])

    await turn(model, tools)

    assert [(method, tool) for method, tool, _ in tools.calls] == [
        ("propose", as_call(write).tool_name)
    ]


async def test_the_kind_comes_from_the_tools_never_from_a_table_of_the_loops_own() -> None:
    # The runner says check_slots is a write here (made up): the loop must ask `tools.kind` and
    # propose it. A loop with its own list of write tools would `run` it instead. One source of
    # truth (the registry, through the runner) means a new write tool can never slip past W1.
    # (The runner refuses the proposal, so the turn goes on to a reply.)
    expected = TurnReply("I could not check that.")
    model = ScriptedStepModel([SLOTS, reply("I could not check that.")])
    tools = ScriptedTools(
        [failed(ToolName.CHECK_SLOTS, ToolErrorCode.NOT_ENABLED)],
        kinds={ToolName.CHECK_SLOTS: ToolKind.WRITE},
    )

    assert await turn(model, tools) == expected
    assert [(method, tool) for method, tool, _ in tools.calls] == [
        ("propose", ToolName.CHECK_SLOTS)
    ]


async def test_a_valid_proposal_ends_the_turn_with_confirmation_required() -> None:
    envelope = proposed(slot_id="S-17")
    assert envelope.proposal is not None
    expected = ConfirmationRequired(envelope.proposal)
    model = ScriptedStepModel([SLOTS, BOOK])  # no third step: the proposal ends the turn
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS), envelope])

    assert await turn(model, tools) == expected
    assert tools.calls[-1] == ("propose", ToolName.BOOK, {"slot_id": "S-17"})


@pytest.mark.parametrize(
    "code", [ToolErrorCode.NOT_FOUND, ToolErrorCode.SLOT_UNAVAILABLE], ids=lambda c: c.value
)
async def test_a_refused_proposal_is_an_observation_and_the_turn_goes_on(
    code: ToolErrorCode,
) -> None:
    refusal = failed(ToolName.BOOK, code)
    expected_seen = Transcript(MESSAGE).observe(seen(BOOK, refusal))
    expected = TurnReply("That slot is taken; shall I look for another?")
    model = ScriptedStepModel([BOOK, reply("That slot is taken; shall I look for another?")])
    tools = ScriptedTools([refusal])

    assert await turn(model, tools) == expected
    assert model.transcripts[1] == expected_seen


# ── Rule 1: the step cap ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("max_steps", [1, 2, 4])
async def test_at_the_step_cap_the_turn_hands_off(max_steps: int) -> None:
    expected = TurnHandoff(LoopHandoffReason.STEP_CAP)
    model = ScriptedStepModel([SLOTS] * max_steps)  # a step beyond the cap would fail the fake
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS)] * max_steps)

    assert await turn(model, tools, LoopLimits(max_steps=max_steps)) == expected
    assert len(model.transcripts) == max_steps
    assert len(tools.calls) == max_steps


async def test_a_proposal_on_the_last_allowed_step_still_ends_the_turn() -> None:
    envelope = proposed()
    assert envelope.proposal is not None
    expected = ConfirmationRequired(envelope.proposal)
    model = ScriptedStepModel([SLOTS, BOOK])
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS), envelope])

    assert await turn(model, tools, LoopLimits(max_steps=2)) == expected


async def test_an_ending_step_on_the_last_allowed_step_is_not_a_cap() -> None:
    expected = TurnReply("Found it.")
    model = ScriptedStepModel([SLOTS, reply("Found it.")])
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS)])

    assert await turn(model, tools, LoopLimits(max_steps=2)) == expected


# ── Rule 5: failures, and the second failure of one tool ────────────────────────────────────


@pytest.mark.parametrize("code", FAILURES, ids=lambda c: c.value)
async def test_one_failure_is_an_observation(code: ToolErrorCode) -> None:
    error = failed(ToolName.CHECK_SLOTS, code)
    expected_seen = Transcript(MESSAGE).observe(seen(SLOTS, error))
    expected = TurnReply("Sorry, the calendar is not answering.")
    model = ScriptedStepModel([SLOTS, reply("Sorry, the calendar is not answering.")])
    tools = ScriptedTools([error])

    assert await turn(model, tools) == expected
    assert model.transcripts[1] == expected_seen


@pytest.mark.parametrize("second", FAILURES, ids=lambda c: c.value)
@pytest.mark.parametrize("first", FAILURES, ids=lambda c: c.value)
async def test_a_second_failure_of_the_same_tool_hands_off(
    first: ToolErrorCode, second: ToolErrorCode
) -> None:
    # Any two failure codes count together: §3.5 hands off when a tool keeps failing.
    expected = TurnHandoff(LoopHandoffReason.TOOL_FAILURE)
    model = ScriptedStepModel([SLOTS, SLOTS])  # no third step
    tools = ScriptedTools(
        [failed(ToolName.CHECK_SLOTS, first), failed(ToolName.CHECK_SLOTS, second)]
    )

    assert await turn(model, tools) == expected


async def test_a_second_failure_counts_even_with_a_success_in_between() -> None:
    expected = TurnHandoff(LoopHandoffReason.TOOL_FAILURE)
    model = ScriptedStepModel([SLOTS, SLOTS, SLOTS])
    tools = ScriptedTools(
        [
            failed(ToolName.CHECK_SLOTS, ToolErrorCode.TIMEOUT),
            observed(ToolName.CHECK_SLOTS),
            failed(ToolName.CHECK_SLOTS, ToolErrorCode.UNAVAILABLE),
        ]
    )

    assert await turn(model, tools) == expected


async def test_a_proposal_that_fails_twice_hands_off() -> None:
    expected = TurnHandoff(LoopHandoffReason.TOOL_FAILURE)
    model = ScriptedStepModel([BOOK, BOOK])
    tools = ScriptedTools(
        [
            failed(ToolName.BOOK, ToolErrorCode.INVALID_ARGUMENTS),
            failed(ToolName.BOOK, ToolErrorCode.TIMEOUT),
        ]
    )

    assert await turn(model, tools) == expected


async def test_failures_of_different_tools_do_not_add_up() -> None:
    expected = TurnReply("Nothing is answering right now.")
    model = ScriptedStepModel([SLOTS, ORDER, reply("Nothing is answering right now.")])
    tools = ScriptedTools(
        [
            failed(ToolName.CHECK_SLOTS, ToolErrorCode.TIMEOUT),
            failed(ToolName.ORDER_STATUS, ToolErrorCode.UNAVAILABLE),
        ]
    )

    assert await turn(model, tools) == expected


@pytest.mark.parametrize(
    ("call", "tool", "code"),
    [
        (ORDER, ToolName.ORDER_STATUS, ToolErrorCode.NOT_FOUND),
        (BOOK, ToolName.BOOK, ToolErrorCode.SLOT_UNAVAILABLE),
        (SLOTS, ToolName.CHECK_SLOTS, ToolErrorCode.NOT_ENABLED),
    ],
    ids=["not_found", "slot_unavailable", "not_enabled"],
)
async def test_other_codes_are_observations_that_never_count(
    call: AgentStep, tool: ToolName, code: ToolErrorCode
) -> None:
    expected = TurnReply("I could not find that.")
    model = ScriptedStepModel([call, call, call, reply("I could not find that.")])
    tools = ScriptedTools([failed(tool, code)] * 3)

    assert await turn(model, tools) == expected


# ── Rule 6: the model's errors ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("after_a_read", [False, True], ids=["first step", "after a read"])
async def test_schema_invalid_output_hands_off_as_invalid_model_output(after_a_read: bool) -> None:
    expected = TurnHandoff(LoopHandoffReason.INVALID_MODEL_OUTPUT)
    invalid = StructuredOutputInvalid(problems=(("step", "union_tag_invalid"),))
    model = ScriptedStepModel([SLOTS, invalid] if after_a_read else [invalid])
    tools = ScriptedTools([observed(ToolName.CHECK_SLOTS)] if after_a_read else [])

    assert await turn(model, tools) == expected


@pytest.mark.parametrize("error", OTHER_GATEWAY_ERRORS, ids=lambda e: type(e).__name__)
async def test_any_other_gateway_error_hands_off_as_model_unavailable(error: GatewayError) -> None:
    expected = TurnHandoff(LoopHandoffReason.MODEL_UNAVAILABLE)
    model = ScriptedStepModel([error])

    assert await turn(model, ScriptedTools()) == expected


@pytest.mark.parametrize("error", [ValueError("a bug"), KeyError("step"), TimeoutError()])
async def test_any_other_exception_from_the_step_model_propagates_unchanged(
    error: Exception,
) -> None:
    model = ScriptedStepModel([error])

    with pytest.raises(type(error)) as raised:
        await turn(model, ScriptedTools())
    assert raised.value is error


# ── Rule 7: any other exception from the tools hands off ────────────────────────────────────


@pytest.mark.parametrize(
    "error",
    [RuntimeError("a bug"), OperationalError("SELECT 1", {}, Exception()), ValueError("caller")],
    ids=["RuntimeError", "OperationalError", "ValueError"],
)
@pytest.mark.parametrize("call", [SLOTS, BOOK], ids=["run", "propose"])
async def test_any_other_exception_from_the_tools_hands_off_as_tool_failure(
    call: AgentStep, error: Exception
) -> None:
    expected = TurnHandoff(LoopHandoffReason.TOOL_FAILURE)
    model = ScriptedStepModel([call])
    tools = ScriptedTools([error])

    assert await turn(model, tools) == expected


async def test_an_exception_from_kind_hands_off_as_tool_failure() -> None:
    expected = TurnHandoff(LoopHandoffReason.TOOL_FAILURE)
    model = ScriptedStepModel([SLOTS])
    tools = ScriptedTools(kind_error=KeyError("check_slots"))

    assert await turn(model, tools) == expected


# ── Rule 8: what propagates, and what is never here ─────────────────────────────────────────


@pytest.mark.parametrize("where", ["step model", "tools"])
@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), CapReached("cap")], ids=["CancelledError", "CapReached"]
)
async def test_a_cancel_and_the_eval_cap_always_propagate(where: str, error: BaseException) -> None:
    model = ScriptedStepModel([error] if where == "step model" else [SLOTS])
    tools = ScriptedTools([] if where == "step model" else [error])

    with pytest.raises(type(error)) as raised:
        await turn(model, tools)
    assert raised.value is error


async def test_the_loop_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    model = ScriptedStepModel(
        [SLOTS, ORDER, StructuredOutputInvalid(problems=(("step", "missing"),))]
    )
    tools = ScriptedTools(
        [failed(ToolName.CHECK_SLOTS, ToolErrorCode.TIMEOUT), observed(ToolName.ORDER_STATUS)]
    )

    await turn(model, tools)

    # The fakes log nothing either, so every record would be the loop's.
    assert caplog.records == []
