"""Team stand-ins for the harness's tests: canned H2 metrics and a scripted stand-in for the loop.

**None of these is H2 or H1.**
- `canned_metrics` returns fixed results whatever the input: it let the summary and the harness
  be tested before H2 (`tooleval/metrics.py`) was written, and keeps their tests independent of
  it. It computes nothing, so it can never pass for the real definitions
  (tests/tooleval/test_metrics.py holds those).
- `UNWRITTEN_H2` raises `NotImplementedError` from every function, as the stubs did: the harness's
  "H2 not written yet" path stays tested now that the real H2 is written.
- `scripted_stand_in` is not a loop: it never asks the step model, never looks at a result and
  decides nothing. For each message it plays the one action the test wrote down for it (a read and
  a fixed reply, a proposal, a hand-off, a reply, or an exception), through the real tools it is
  given, so the harness's database counts can be tested before shesha writes `run_turn`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, NoReturn

from vectapilot_api.agent.types import (
    ConfirmationRequired,
    LoopLimits,
    ModelHandoffReason,
    StepModel,
    ToolRunner,
    Transcript,
    TurnHandoff,
    TurnLoop,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.tooleval.metrics import (
    DuplicateWrites,
    IdCount,
    ToolMetrics,
    ToolSuccess,
    WriteCount,
)
from vectapilot_api.tools.config import ToolName

CANNED_SUCCESS: Final = ToolSuccess(
    successes=1, n=2, misses=("canned-miss",), not_scored=(), interval=wilson_interval(1, 2)
)
CANNED_WRITES: Final = WriteCount(k=0, n=3, rows=())
CANNED_DUPLICATES: Final = DuplicateWrites(extra=0, n=3, scenarios=(), over_booked=())
CANNED_IDS: Final = IdCount(k=0, n=1, ids=())


def canned_metrics(success: ToolSuccess = CANNED_SUCCESS) -> ToolMetrics:
    """Fixed results whatever the input (module docstring)."""
    return ToolMetrics(
        tool_success=lambda lines: success,
        unconfirmed_writes=lambda lines: CANNED_WRITES,
        duplicate_writes=lambda lines: CANNED_DUPLICATES,
        unrequested_proposals=lambda lines: CANNED_IDS,
        safety_misses=lambda lines: CANNED_IDS,
    )


def _unwritten(lines: object) -> NoReturn:
    raise NotImplementedError("TEST-ONLY: H2 unwritten")


UNWRITTEN_H2: Final = ToolMetrics(
    tool_success=_unwritten,
    unconfirmed_writes=_unwritten,
    duplicate_writes=_unwritten,
    unrequested_proposals=_unwritten,
    safety_misses=_unwritten,
)
"""H2 as the stubs were (module docstring): every function raises `NotImplementedError`."""


@dataclass(frozen=True)
class Propose:
    tool: ToolName
    arguments: Mapping[str, str]


@dataclass(frozen=True)
class ReadThenReply:
    tool: ToolName
    arguments: Mapping[str, object]
    text: str = "TEST-ONLY scripted reply."


@dataclass(frozen=True)
class Hand:
    reason: ModelHandoffReason


@dataclass(frozen=True)
class Raise:
    error: Exception


Action = Propose | ReadThenReply | Hand | Raise


def scripted_stand_in(script: Mapping[str, Action]) -> TurnLoop:
    """A stand-in for `run_turn` (module docstring): `script` maps a message to its one action."""

    async def stand_in(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        action = script[message]
        if isinstance(action, Raise):
            raise action.error
        if isinstance(action, Hand):
            return TurnHandoff(action.reason)
        if isinstance(action, ReadThenReply):
            await tools.run(action.tool, action.arguments)
            return TurnReply(action.text)
        envelope = await tools.propose(action.tool, action.arguments)
        if envelope.proposal is None:
            return TurnReply("TEST-ONLY: the scripted proposal was refused.")
        return ConfirmationRequired(envelope.proposal)

    return stand_in


def ask_once_stand_in() -> TurnLoop:
    """A stand-in for `run_turn` that asks the step model exactly once (from `Transcript(message)`),
    ignores the answer and hands off (`other`). It never calls a tool and decides nothing: it lets
    a test see the dry run's model path (the capped gateway, the plan provider, the records and the
    cost) before H1 exists."""

    async def stand_in(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        await step_model.next_step(Transcript(message))
        return TurnHandoff(ModelHandoffReason.OTHER)

    return stand_in
