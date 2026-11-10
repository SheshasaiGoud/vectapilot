"""Typed values of pending actions and their confirm route (M5 PR 3; W2, W5, W6, W9).

- **`ConfirmRequest`** is the whole body of `POST /v1/agent/actions/{id}/confirm`: the customer
  and the token, closed (`extra="forbid"`). It has no tool arguments: the stored ones run (W2).
- **`ConfirmResponse`** is what a confirm returns, the first time and on every repeat: the action,
  its tool, `executed` with the tool's typed output, or `failed` with a typed reason, and a fixed
  English sentence for the customer (confirmations are English-only until M8's widget, decided
  2026-11-10). Never free text from a row.
- **The errors** are three, each one fixed body: every refusal (unknown, expired, wrong token,
  another business's or customer's) is the same `ActionNotFound` 404 (W6), so the route is no
  oracle for which ids exist; `ConfirmFailed` means this attempt's transaction rolled back
  before its COMMIT (a database or network error, not a timeout), so this attempt changed
  nothing; `OutcomeUnknown` means the outcome is unknown (a timeout or a failure while
  committing), and the caller retries confirm, which returns the stored result or executes once
  (W9). Neither 503 ever says the action was not written: a parallel confirm may have run it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from vectapilot_api.actions.tokens import ConfirmToken
from vectapilot_api.tools.book import BookOutput
from vectapilot_api.tools.config import CustomerRef, ToolName, ToolsModel
from vectapilot_api.tools.create_ticket import CreateTicketOutput
from vectapilot_api.tools.reschedule import RescheduleOutput
from vectapilot_api.tools.types import WriteFailure


class ActionStatus(StrEnum):
    """Migration 0006's `pending_actions.status` values (a test compares)."""

    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"


class ConfirmRequest(ToolsModel):
    """The confirm call's body: who confirms, and the one-time token. Nothing else (W2)."""

    customer_ref: CustomerRef
    confirm_token: ConfirmToken


WriteResult = BookOutput | RescheduleOutput | CreateTicketOutput
"""An executed action's stored output: the write tool's output model."""

EXECUTED_MESSAGE: Final = "Done: the confirmed action was carried out."
FAILURE_MESSAGES: Final[Mapping[WriteFailure, str]] = {
    WriteFailure.SLOT_UNAVAILABLE: "That slot is no longer free, so nothing was booked.",
    WriteFailure.BOOKING_CHANGED: (
        "That booking has changed since it was proposed, so nothing was moved."
    ),
    WriteFailure.NOT_ENABLED: "This action is no longer available here, so nothing was done.",
    WriteFailure.INVALID_ARGUMENTS: (
        "This action no longer fits this business's settings, so nothing was done."
    ),
}
"""One fixed sentence per outcome; no value is ever formatted into one."""


class ConfirmResponse(ToolsModel):
    """A confirmed action's stored result (W5: a repeated confirm returns the same body)."""

    action_id: UUID
    tool: ToolName
    status: Literal[ActionStatus.EXECUTED, ActionStatus.FAILED]
    result: WriteResult | None = None
    failure_reason: WriteFailure | None = None
    message: str = Field(max_length=200)


@dataclass(frozen=True)
class PendingAction:
    """A stored proposal, waiting for its confirm call (W2, W6). `confirm_token` is the only copy
    of the token there will ever be: PR 4's `/v1/agent` returns it to the customer, and nothing
    else may see it. `expires_at` is the database's clock (`now()` plus the configured lifetime)."""

    id: UUID
    tool: ToolName
    summary: str
    expires_at: datetime
    confirm_token: ConfirmToken


class ActionNotFound(Exception):
    """Every refusal: unknown, expired, wrong token, another business's or customer's (W6)."""


class ConfirmFailed(Exception):
    """This attempt's transaction rolled back before its COMMIT (not a timeout): this attempt
    changed nothing. A parallel confirm of the same action may still have run it."""


class OutcomeUnknown(Exception):
    """A timeout or a failure while committing: the caller cannot know, so it retries (W9)."""
