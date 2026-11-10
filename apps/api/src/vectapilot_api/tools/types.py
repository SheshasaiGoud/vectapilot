"""The tool core's shared types (ADR 0016 decision 1, M5 PR 2).

- **`ToolContext`** is what the executor injects (W7): the business, the customer and the trace id
  come from the request (or the MCP process's flags), never from a tool's arguments. No input
  model has a `business_id` or `customer_ref` field (a test walks every schema), and every input
  model forbids unknown fields, so a model that writes one gets `invalid_arguments`.
- **`ToolError`** is a typed error with a fixed message per code. It never carries a value: not an
  id the caller asked for, not a database message. So a foreign customer's order and an order that
  does not exist give byte-identical errors (ADR 0016 decision 9), and an error cannot echo
  instruction-like text back to the model. `fields` names schema fields only, never an unknown key
  the caller sent (that key is caller text).
- **`WriteProposal`** is what `propose` returns for a write tool: the validated arguments and a
  summary rendered from a fixed template over typed fields (W3). Nothing is written: PR 3 stores
  it as a pending action, and only the confirm route executes it.
- **`ToolEnvelope`** is the executor's internal result (ADR 0016 decision 1; the scoring rules'
  "Leak check first"): the business ids of every row the call read, next to the typed result and
  its rendered, capped `observation`. Only `observation` may reach a prompt or an MCP client; the
  ids are for the eval harness's leak check and never appear in it.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import BaseModel, Field, model_validator

from vectapilot_api.gateway.types import BusinessId, TraceId
from vectapilot_api.tools.config import (
    TICKET_SUMMARY_MAX,
    WRITE_TOOLS,
    CustomerRef,
    ToolName,
    ToolsModel,
)


class ToolKind(StrEnum):
    """A read runs at once in a read-only snapshot; a write is only ever proposed here (W1)."""

    READ = "read"
    WRITE = "write"


class ToolErrorCode(StrEnum):
    """The typed failures a tool call can return (M5 PR 2; the loop's contract, H1)."""

    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    NOT_ENABLED = "not_enabled"
    SLOT_UNAVAILABLE = "slot_unavailable"


class WriteFailure(StrEnum):
    """Why a confirmed action wrote nothing (`pending_actions.failure_reason`, M5 PR 3). Here, not
    in `tools.execute`, so the confirm route's types can name it without importing the one module
    only the confirm service may import (W1)."""

    SLOT_UNAVAILABLE = "slot_unavailable"
    BOOKING_CHANGED = "booking_changed"
    NOT_ENABLED = "not_enabled"
    INVALID_ARGUMENTS = "invalid_arguments"


ERROR_MESSAGES: Final[Mapping[ToolErrorCode, str]] = {
    ToolErrorCode.INVALID_ARGUMENTS: "The arguments do not fit this tool's schema.",
    ToolErrorCode.NOT_FOUND: "Nothing matching was found.",
    ToolErrorCode.UNAVAILABLE: "This tool is unavailable right now.",
    ToolErrorCode.TIMEOUT: "This tool did not answer in time.",
    ToolErrorCode.NOT_ENABLED: "This tool is not enabled here.",
    ToolErrorCode.SLOT_UNAVAILABLE: "That slot cannot be booked: it is taken or in the past.",
}
"""One fixed sentence per code: what the model reads. No value is ever formatted into one."""

UNEXPECTED_FIELD: Final = "(unexpected field)"
"""What `ToolError.fields` shows for a key the schema does not have (the key itself is caller
text, so it is never repeated)."""

OK: Final = "ok"
PROPOSED: Final = "proposed"
"""The two success outcomes in `tool_audit.outcome` and the `tool_call` log record; every other
outcome is a `ToolErrorCode` value (or `cancelled`, in the log only)."""

FieldName = Annotated[
    str, Field(max_length=40, pattern=r"^([a-z][a-z0-9_]{0,38}|\(unexpected field\))$")
]
SUMMARY_MAX: Final = 1200
"""A rendered summary's bound: the longest template plus a quoted 500-character ticket text
(JSON-escaped, so it can grow) fits."""


class ToolContext(ToolsModel):
    """Who a call is for, injected by the executor's caller (W7), never chosen by the model."""

    business_id: BusinessId
    customer_ref: CustomerRef
    trace_id: TraceId | None = None


class ToolError(ToolsModel):
    """A typed failure: a code, its fixed message and, for `invalid_arguments`, the schema fields
    that failed."""

    code: ToolErrorCode
    message: str = Field(max_length=200)
    fields: tuple[FieldName, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _fixed_message(self) -> Self:
        if self.message != ERROR_MESSAGES[self.code]:
            raise ValueError("a tool error carries its code's fixed message")
        return self

    @classmethod
    def of(cls, code: ToolErrorCode, fields: tuple[str, ...] = ()) -> "ToolError":
        return cls(code=code, message=ERROR_MESSAGES[code], fields=fields)


class WriteProposal(ToolsModel):
    """A write tool's validated call, never executed here (W1, W3). PR 3 stores it as a pending
    action with the context's `customer_ref`; the summary is what the customer confirms."""

    tool: ToolName
    arguments: Mapping[str, Annotated[str, Field(max_length=TICKET_SUMMARY_MAX)]] = Field(
        max_length=4
    )
    """The validated input, as the JSON the pending action stores (every write input is text,
    bounded by its input model)."""
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)

    @model_validator(mode="after")
    def _write_tool(self) -> Self:
        if self.tool not in WRITE_TOOLS:
            raise ValueError("only a write tool is proposed")
        return self


def render(tool: ToolName | None, result: BaseModel) -> str:
    """The observation text of `result`: compact JSON of the typed result only, UTF-8 as is."""
    name = None if tool is None else tool.value
    payload: dict[str, object]
    if isinstance(result, ToolError):
        payload = {"tool": name, "ok": False, "error": result.model_dump(mode="json")}
    elif isinstance(result, WriteProposal):
        payload = {
            "tool": name,
            "ok": True,
            "confirmation_required": True,
            "summary": result.summary,
        }
    else:
        payload = {"tool": name, "ok": True, "result": result.model_dump(mode="json")}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class ToolEnvelope:
    """One call's internal result. `observation` is the only part meant for a model or a client.

    `business_ids` holds the business of every row the call read (an empty set when it read
    none), including rows of another business if a query ever leaked: then the result is
    `unavailable`, and the eval harness stops on the ids (exit 4)."""

    tool: ToolName | None
    kind: ToolKind | None
    outcome: str
    result: BaseModel
    observation: str
    business_ids: frozenset[str]

    @property
    def error(self) -> ToolError | None:
        return self.result if isinstance(self.result, ToolError) else None

    @property
    def proposal(self) -> WriteProposal | None:
        return self.result if isinstance(self.result, WriteProposal) else None
