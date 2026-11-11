"""Test doubles for the agent's tests: a scripted step model, scripted tools, step and envelope
builders, and a stand-in loop for the service's tests. No database, no network.

**None of these is a loop.** `ScriptedStepModel` and `ScriptedTools` only play their scripts and
record what they were asked; `canned_loop` returns (or raises) one fixed thing without calling
either. H1's `run_turn` is shesha's (D5 (b)): no helper here decides anything a loop decides.

The step schema is the committed Paws & Co configuration's (all five tools enabled): data, not
code, so rule 9 holds for `src/`.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, cast

from pydantic import BaseModel

from tests.tools.helpers import SANDBOX_DIR
from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.agent.types import (
    AgentStep,
    CallTool,
    LoopLimits,
    ModelHandoffReason,
    Observation,
    StepModel,
    ToolRunner,
    Transcript,
    TurnLoop,
    TurnOutcome,
)
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.files import load_tools_dir
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.types import (
    OK,
    PROPOSED,
    ToolEnvelope,
    ToolError,
    ToolErrorCode,
    ToolKind,
    WriteProposal,
    render,
)

TOOLS: Final = load_tools_dir(SANDBOX_DIR)
PAWS: Final = TOOLS["paws-and-co"].config
SUNRISE: Final = TOOLS["sunrise-clinic"].config
NO_DATABASE: Final = cast(TransactionSource, None)
"""For an executor whose tools are never called: the schema builder only reads its models."""
EXECUTOR: Final = ToolExecutor(NO_DATABASE, PAWS)
STEP: Final = build_agent_step(EXECUTOR)
"""Paws & Co's `AgentStep` model: every tool enabled."""

MESSAGE: Final = "Is there a vet slot on Monday? My order 1042 too, please."
LIMITS: Final = LoopLimits(max_steps=4)


def step(payload: Mapping[str, object]) -> AgentStep:
    """An `AgentStep` validated exactly as the gateway validates the model's JSON."""
    return STEP.model_validate({"step": payload})


def call(tool: ToolName, **arguments: object) -> AgentStep:
    return step({"action": "call_tool", "tool": tool.value, "arguments": arguments})


def reply(text: str = "A scripted reply.") -> AgentStep:
    return step({"action": "reply", "text": text})


def handoff(reason: ModelHandoffReason) -> AgentStep:
    return step({"action": "handoff", "reason": reason.value})


SLOTS: Final = call(ToolName.CHECK_SLOTS, date_from="2026-11-16", date_to="2026-11-17")
ORDER: Final = call(ToolName.ORDER_STATUS, order_id="1042")
BOOK: Final = call(ToolName.BOOK, slot_id="S-17")
TICKET: Final = call(ToolName.CREATE_TICKET, category="other", summary="Please call me back.")


def as_call(agent_step: AgentStep) -> CallTool:
    """The `CallTool` inside a step built by `call`."""
    assert isinstance(agent_step.step, CallTool)
    return agent_step.step


def seen(agent_step: AgentStep, envelope: ToolEnvelope) -> Observation:
    """The observation a test expects the model to see for `agent_step`'s call (`Observation.of`,
    the team's own builder)."""
    return Observation.of(as_call(agent_step), envelope)


class FakeResult(BaseModel):
    """A read tool's typed result, as far as the loop cares: something the envelope renders."""

    note: str


def observed(
    tool: ToolName, note: str = "a result", *, business_ids: frozenset[str] = frozenset()
) -> ToolEnvelope:
    """A read tool's successful envelope."""
    result = FakeResult(note=note)
    return ToolEnvelope(
        tool=tool,
        kind=ToolKind.READ,
        outcome=OK,
        result=result,
        observation=render(tool, result),
        business_ids=business_ids,
    )


def failed(tool: ToolName, code: ToolErrorCode) -> ToolEnvelope:
    """A typed error's envelope, for a read or a proposal."""
    error = ToolError.of(code)
    return ToolEnvelope(
        tool=tool,
        kind=REGISTRY[tool].kind,
        outcome=code.value,
        result=error,
        observation=render(tool, error),
        business_ids=frozenset(),
    )


def proposed(tool: ToolName = ToolName.BOOK, slot_id: str = "S-17") -> ToolEnvelope:
    """A valid proposal's envelope."""
    proposal = WriteProposal(
        tool=tool,
        arguments={"slot_id": slot_id},
        summary=f"Book Vet consultation on Wednesday 18 November 2026, slot {slot_id}.",
    )
    return ToolEnvelope(
        tool=tool,
        kind=ToolKind.WRITE,
        outcome=PROPOSED,
        result=proposal,
        observation=render(tool, proposal),
        business_ids=frozenset({PAWS.business_id}),
    )


@dataclass
class ScriptedStepModel:
    """Plays `script` (steps, or exceptions to raise) one per `next_step`, recording each
    transcript it was given. A call beyond the script fails the test."""

    script: list[AgentStep | BaseException]
    transcripts: list[Transcript] = field(default_factory=list)

    async def next_step(self, transcript: Transcript) -> AgentStep:
        self.transcripts.append(transcript)
        if not self.script:
            raise AssertionError(f"ScriptedStepModel: step {len(self.transcripts)} has no script")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class ScriptedTools:
    """Plays `script` (envelopes, or exceptions to raise) one per `run` or `propose`, recording
    `(method, tool, arguments)`. `kind` is the registry's unless `kinds` overrides a tool (or
    raises `kind_error`). A call beyond the script fails the test."""

    script: list[ToolEnvelope | BaseException] = field(default_factory=list)
    calls: list[tuple[str, ToolName, dict[str, object]]] = field(default_factory=list)
    kinds: dict[ToolName, ToolKind] = field(default_factory=dict)
    kind_error: BaseException | None = None

    def kind(self, tool: ToolName) -> ToolKind:
        if self.kind_error is not None:
            raise self.kind_error
        return self.kinds.get(tool, REGISTRY[tool].kind)

    async def _next(
        self, method: str, tool: ToolName, arguments: Mapping[str, object]
    ) -> ToolEnvelope:
        self.calls.append((method, tool, dict(arguments)))
        if not self.script:
            raise AssertionError(f"ScriptedTools: call {len(self.calls)} has no script")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def run(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return await self._next("run", tool, arguments)

    async def propose(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return await self._next("propose", tool, arguments)


def canned_loop(result: TurnOutcome | BaseException | None = None) -> TurnLoop:
    """A stand-in for `run_turn` in the service's tests: returns `result` (or raises it), or
    with None never returns (only a cancel or the turn timeout ends it). It calls neither the
    step model nor the tools: it is not a loop."""

    async def loop(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        if result is None:
            await asyncio.Event().wait()
            raise AssertionError("unreachable: nothing sets the event")
        if isinstance(result, BaseException):
            raise result
        return result

    return loop


def one_step_stand_in(result: TurnOutcome) -> TurnLoop:
    """A stand-in for `run_turn` in the service's tests that asks the step model exactly once
    (from `Transcript(message)`), ignores the answer and returns `result`. It never calls a tool,
    never looks at the step and never decides anything: it exists so a test can see what the
    service handed the step model (the step count, the business's sandbox date and timezone).
    It is not a loop."""

    async def loop(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        await step_model.next_step(Transcript(message))
        return result

    return loop
