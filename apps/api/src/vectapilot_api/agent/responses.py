"""`POST /v1/agent`'s request and responses (ADR 0016 decisions 2, 7; M5 PR 4).

- **`AgentRequest`** is the whole body: `{"message", "customer_ref"}`, closed (`extra="forbid"`).
  One message per request: there is no history field until M8's widget (ADR 0016 decision 11).
  `customer_ref` is client-asserted, accepted only where `X-Business-Id` is (local, test, ci;
  ADR 0016 decision 9): the IDOR is closed only when M8 authenticates the customer.
- **The response** is one of four kinds, told apart by `kind`, each with the request's trace id:
  - `reply`: the model's text (model text: M7 guards replies);
  - `confirmation_required`: a stored pending action, its one-time `confirm_token`, the typed
    summary the customer confirms (W3) and its database-set expiry (W6). **This response is the
    only place the token is ever revealed** (W2): `ConfirmToken` is a pydantic `Secret`, masked
    in every repr, log extra and Python-mode dump; only this model's JSON serializer reveals it,
    on purpose. **The summary is English-only until M8's widget** (decided 2026-11-10, a known
    gap, not a bug): PR 2's fixed template, whatever the customer's language
    (`summary_language` says so);
  - `handoff`: a reason, a priority and the business's configured emergency guidance when it
    applies (W11): its lines in order, one per list item, never joined (a heading such as
    "Outside clinic hours" belongs on its own line), and never model text;
  - `answer`: `POST /v1/answer`'s response model, unchanged, when the router sends the message to
    RAG (`agent_router = on`).
"""

from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    field_serializer,
)

from vectapilot_api.actions.tokens import ConfirmToken
from vectapilot_api.agent.prompt import AGENT_PROMPT_VERSION
from vectapilot_api.agent.types import REPLY_MAX, LoopHandoffReason, ModelHandoffReason
from vectapilot_api.answer.types import AnswerResponse
from vectapilot_api.gateway.types import OpaqueId, TraceId
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.retrieval.types import Question
from vectapilot_api.tools.config import CustomerRef, ToolName
from vectapilot_api.tools.types import SUMMARY_MAX

GUIDANCE_LINES_MAX: Final = 10
"""`ToolsConfig.emergency_guidance`'s own bound."""


class HandoffReason(StrEnum):
    """Every reason a `/v1/agent` hand-off can carry (a test checks it holds the loop's and the
    model's reasons, value for value)."""

    # The model's own (`ModelHandoffReason`); `safety` is always urgent, with guidance (W11).
    CUSTOMER_ASKED = "customer_asked"
    SAFETY = "safety"
    OTHER = "other"
    # The loop's (`LoopHandoffReason`, H1).
    STEP_CAP = "step_cap"
    TOOL_FAILURE = "tool_failure"
    INVALID_MODEL_OUTPUT = "invalid_model_output"
    MODEL_UNAVAILABLE = "model_unavailable"
    # The service's.
    TURN_TIMEOUT = "turn_timeout"
    """The turn ran past `agent_turn_timeout_seconds`."""
    SAFETY_FLOOR_OFF = "safety_floor_off"
    """W11: a health business while `agent_router` is off. Shows the guidance."""
    AGENT_ERROR = "agent_error"
    """Any exception around the loop or the answer (a bug, a pending stub, a database error)."""
    PROPOSAL_NOT_STORED = "proposal_not_stored"
    """The proposal could not be stored as a pending action: nothing to confirm, nothing written."""
    NOT_CONFIGURED = "not_configured"
    """The business has no tool configuration (`VECTAPILOT_TOOLS_CONFIG_DIR`)."""
    ROUTED = "routed"
    """`agent_router = on`: the router's decision for the message was a hand-off."""
    ROUTER_FAILED = "router_failed"
    """`agent_router = on`: the router failed closed, or raised. For a health business the
    safety step did not clear the message, so the guidance is shown."""


def handoff_reason(reason: ModelHandoffReason | LoopHandoffReason) -> HandoffReason:
    """The API's reason for a loop's hand-off reason (the same value)."""
    return HandoffReason(reason.value)


class AgentRequest(BaseModel):
    """`POST /v1/agent`'s JSON body."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    message: Question = Field(repr=False)
    customer_ref: CustomerRef


class _Response(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: TraceId
    """New per request; this turn's `llm_call`, `tool_call` and `agent_turn` records carry it."""


class ReplyResponse(_Response):
    kind: Literal["reply"] = "reply"
    text: str = Field(min_length=1, max_length=REPLY_MAX, repr=False)
    prompt_version: OpaqueId = AGENT_PROMPT_VERSION


class ConfirmationResponse(_Response):
    kind: Literal["confirmation_required"] = "confirmation_required"
    action_id: UUID
    """The id to confirm: `POST /v1/agent/actions/{action_id}/confirm`. Not a credential."""
    tool: ToolName
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)
    """Exactly what the customer confirms: a fixed template over typed fields (W3)."""
    summary_language: Literal["en"] = "en"
    """English-only until M8's widget, whatever the customer's language (decided 2026-11-10)."""
    expires_at: AwareDatetime
    """Set by the database (W6); after it, a confirm is refused (unless already executed)."""
    confirm_token: ConfirmToken
    """The one-time token, revealed only in this response's JSON (W2)."""
    prompt_version: OpaqueId = AGENT_PROMPT_VERSION

    @field_serializer("confirm_token")
    def _reveal_token(self, token: ConfirmToken, info: SerializationInfo) -> object:
        # The one deliberate reveal (actions/tokens.py): only this response's JSON carries the
        # token. A Python-mode dump (a log extra, a test's `model_dump()`) keeps it masked.
        return token.get_secret_value() if info.mode_is_json() else token


class HandoffResponse(_Response):
    kind: Literal["handoff"] = "handoff"
    reason: HandoffReason
    priority: Priority
    intent: Intent | None = None
    """The router's intent, when `agent_router = on` decided the hand-off; else None."""
    guidance: tuple[str, ...] = Field(default=(), max_length=GUIDANCE_LINES_MAX)
    """The business's configured emergency guidance, one line per item, when W11 shows it."""
    prompt_version: OpaqueId = AGENT_PROMPT_VERSION


class AnswerKindResponse(_Response):
    kind: Literal["answer"] = "answer"
    answer: AnswerResponse
    """`POST /v1/answer`'s response model, unchanged."""


AgentResponse = Annotated[
    ReplyResponse | ConfirmationResponse | HandoffResponse | AnswerKindResponse,
    Field(discriminator="kind"),
]
