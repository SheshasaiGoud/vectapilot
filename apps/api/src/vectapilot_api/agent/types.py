"""The agent loop's interface (ADR 0016 decisions 4-5, M5 PR 4): everything `run_turn` (H1) reads
and returns, built and tested by the team so that H1 is only the loop.

**The model's step** (decided D2 (a)). Each step is one structured call whose answer must be an
`AgentStep`: `{"step": {...}}` holding exactly one of
- `CallTool`: `{"action": "call_tool", "tool": ..., "arguments": {...}}`, one variant per enabled
  tool, its `arguments` that business's input model (`steps.build_agent_step`);
- `Reply`: `{"action": "reply", "text": ...}`;
- `Handoff`: `{"action": "handoff", "reason": "customer_asked" | "safety" | "other"}`.

**The docstrings and field descriptions of these models are prompt text.** The gateway quotes the
schema's JSON Schema, descriptions included, into the system prompt (and its cache key), so they
are written for the model, and they are pinned by `AGENT_PROMPT_FINGERPRINTS`: changing one is a
new `AGENT_PROMPT_VERSION`. Notes for developers are comments, never docstrings, on these classes.

**The transcript** holds the customer's message and, per tool call, the call and the envelope's
`observation` text, never the envelope itself (its business ids are for the eval's leak check,
W10): `Observation.of` is the only way the loop needs to build one. The loop never sees
`business_id` or `customer_ref`: `StepModel` and `ToolRunner` are bound to them.

**The outcomes** are three: a reply, a write proposal waiting for the customer's confirmation, or
a hand-off with a reason. The service (`service.py`) turns them into the API's response; the
loop's `handoff(safety)` becomes an urgent hand-off with the business's configured guidance there
(W11), never here.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field

from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import ToolEnvelope, ToolErrorCode, ToolKind, WriteProposal

REPLY_MAX: Final = 1500
"""A reply's length bound [proposal]: a chat reply, not a document."""
MAX_STEPS_LIMIT: Final = 10
"""`LoopLimits.max_steps`' ceiling, as `Settings.agent_max_steps`' (a typo cannot make it 100)."""

FAILURE_CODES: Final[frozenset[ToolErrorCode]] = frozenset(
    {ToolErrorCode.INVALID_ARGUMENTS, ToolErrorCode.UNAVAILABLE, ToolErrorCode.TIMEOUT}
)
"""H1 rule 5: the typed errors that count as a tool's *failure*; a second failure of the same tool
in one turn hands off (`tool_failure`). Every other code (`not_found`, `slot_unavailable`,
`not_enabled`) is an ordinary observation the model can act on."""


class _Step(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class ModelHandoffReason(StrEnum):
    """Why you hand the conversation to staff."""

    CUSTOMER_ASKED = "customer_asked"
    SAFETY = "safety"
    OTHER = "other"


class LoopHandoffReason(StrEnum):
    # Hand-offs the loop makes itself (H1 rules 1, 5, 6, 7); never offered to the model.
    STEP_CAP = "step_cap"
    TOOL_FAILURE = "tool_failure"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    MODEL_UNAVAILABLE = "model_unavailable"


TurnHandoffReason = ModelHandoffReason | LoopHandoffReason


class CallTool(_Step):
    """Call one tool with its arguments."""

    # `steps.build_agent_step` makes one subclass per enabled tool, narrowing `tool` to that
    # tool's name and `arguments` to the business's input model, with the tool's description as
    # the docstring. This base is never a variant itself.
    action: Literal["call_tool"]
    tool: ToolName
    arguments: BaseModel

    @property
    def tool_name(self) -> ToolName:
        """`tool` as a `ToolName` (a variant's field holds the name's value)."""
        return ToolName(self.tool)

    def argument_values(self) -> dict[str, object]:
        """The arguments as JSON values: what `ToolRunner.run` and `propose` take."""
        values: dict[str, object] = self.arguments.model_dump(mode="json")
        return values


class Reply(_Step):
    """Reply to the customer and end the turn."""

    action: Literal["reply"]
    text: str = Field(
        min_length=1,
        max_length=REPLY_MAX,
        description="Your reply to the customer, in the language of their message, built only"
        " from the tool results. Never say that something was booked, moved or sent.",
    )


class Handoff(_Step):
    """Hand the conversation to the business's staff and end the turn."""

    action: Literal["handoff"]
    reason: ModelHandoffReason = Field(
        description='"safety" for an emergency, a safety risk or distress, or a request for a'
        " clinician's or vet's judgement; \"customer_asked\" when the customer asks for a person;"
        ' "other" when your tools cannot help.'
    )


class AgentStep(_Step):
    """Your next step for the customer's message in the MESSAGE block: exactly one action."""

    # Built per business (`steps.build_agent_step`) with `step` as a discriminated union over
    # `action` (and, for calls, `tool`); this base only types the field for the loop.
    step: CallTool | Reply | Handoff


@dataclass(frozen=True)
class Observation:
    """One tool call of the turn and what the model may read of its result."""

    tool: ToolName
    arguments: Mapping[str, object]
    """The call's arguments as the model chose them (model output: untrusted, shown as data)."""
    text: str
    """The envelope's `observation`: the typed result or typed error as capped JSON (W10)."""

    @classmethod
    def of(cls, call: CallTool, envelope: ToolEnvelope) -> Self:
        """The observation of `call`'s result: only `envelope.observation` crosses into the
        prompt, never the envelope's business ids or typed result object."""
        return cls(tool=call.tool_name, arguments=call.argument_values(), text=envelope.observation)


@dataclass(frozen=True)
class Transcript:
    """What the model has seen so far in this turn: the message, then each observation in order.
    One message per turn: no history until M8's widget (ADR 0016 decision 11)."""

    message: str
    observations: tuple[Observation, ...] = ()

    def observe(self, observation: Observation) -> "Transcript":
        """This transcript with `observation` appended (a new value: transcripts never change)."""
        return replace(self, observations=(*self.observations, observation))


@dataclass(frozen=True)
class TurnReply:
    """The model's reply, as it wrote it (model text: M7 guards replies)."""

    text: str


@dataclass(frozen=True)
class ConfirmationRequired:
    """A valid write proposal: the turn ends, and the service stores it as a pending action whose
    token only the HTTP response carries (W2). Nothing is written until the customer confirms."""

    proposal: WriteProposal


@dataclass(frozen=True)
class TurnHandoff:
    """The turn ends with a hand-off to staff, for the model's reason or the loop's own."""

    reason: TurnHandoffReason


TurnOutcome = TurnReply | ConfirmationRequired | TurnHandoff


@dataclass(frozen=True)
class LoopLimits:
    """The loop's bounds (Settings: `agent_max_steps`). The turn's time limit is the service's."""

    max_steps: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or not 1 <= self.max_steps <= MAX_STEPS_LIMIT
        ):
            raise ValueError(f"max_steps must be an int in [1, {MAX_STEPS_LIMIT}]")


class StepModel(Protocol):
    """The model's next step for a transcript (`GatewayStepModel`; fakes in tests). Raises the
    gateway's typed errors: `StructuredOutputInvalid` for output that failed the schema after the
    gateway's one repair, another `GatewayError` for anything else the gateway refused or lost."""

    async def next_step(self, transcript: Transcript) -> AgentStep: ...


class ToolRunner(Protocol):
    """One business's tools, bound to one request's customer (`BoundTools`; fakes in tests).
    Reads and proposals only: nothing here can execute a write (W1)."""

    def kind(self, tool: ToolName) -> ToolKind: ...

    async def run(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        """Run a read tool: its typed result or typed error, in the envelope."""
        ...

    async def propose(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        """Validate a write tool's call: a `WriteProposal` or a typed refusal, in the envelope."""
        ...


class TurnLoop(Protocol):
    """`run_turn`'s signature, so the service can be tested with a stand-in loop."""

    async def __call__(
        self, message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome: ...
