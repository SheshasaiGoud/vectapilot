"""Executing a confirmed write (ADR 0016 decisions 2-3, M5 PR 3; W1, W5).

**Only the confirm service imports this module** (`vectapilot_api.actions.confirm`; W1, tested by
`tests/actions/test_write_boundary.py`). The agent loop, the executor and the MCP server read and
propose; nothing they import can reach `execute`.

`execute` runs inside the confirm transaction (READ COMMITTED, W8), after the confirm service has
locked the pending action, checked its token and expiry and set `confirmed_at` and
`confirm_trace_id`. It refuses an action without them (`UnconfirmedAction`, a bug): the database
refuses an `executed` action without them too (migration 0006's CHECK), this is the code's own
fence in front of it.

**Re-validation (W5).** Every proposal check runs again here, on the stored arguments, against the
latest committed rows:

1. the tool is still enabled for the business, and the stored arguments still fit the business's
   input model (a configuration change between propose and confirm), else `not_enabled` or
   `invalid_arguments`;
2. `reschedule` locks the customer's active, not-yet-started booking `FOR UPDATE` (the same
   statement as the proposal check, plus the lock) before anything else, so two reschedules of one
   booking are serialized: the second waits, then re-reads the row as `rescheduled` and fails with
   `booking_changed`;
3. the tool's own `validate` (the very function `propose` ran): the slot exists, is one of the
   business's services, has not started by the sandbox clock and has no active booking; for a
   ticket, the category is still configured.

**Two fences per booking.** The lock (step 2) serializes two reschedules of one booking; the
retire itself is also guarded (`... AND status = 'active'`, exactly one row), so even without the
lock a second reschedule would wait on the row, re-check it and retire nothing (`booking_changed`).
A test runs the concurrent case with the lock removed to pin that second fence.

**The write** runs in a SAVEPOINT. A unique violation of `sandbox_bookings_one_active_per_slot`
(another action took the slot between the check and the insert, which READ COMMITTED allows) rolls
back to it and is `slot_unavailable`: the action can still be marked `failed` in the same
transaction. Any other integrity error is a bug and propagates (the whole transaction rolls back).

A booking's id is derived from its action's id (`booking_id_for`), so a second execution of one
action would collide on the primary key as well as on `UNIQUE (business_id, action_id)`.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools import book, create_ticket, reschedule
from vectapilot_api.tools.book import BookInput, BookOutput
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.create_ticket import CreateTicketInput, CreateTicketOutput
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.reschedule import RescheduleInput, RescheduleOutput
from vectapilot_api.tools.spec import ToolScope, WriteTool, business_input_model
from vectapilot_api.tools.types import ToolError, WriteFailure

ONE_ACTIVE_PER_SLOT: Final = "sandbox_bookings_one_active_per_slot"
"""Migration 0006's partial unique index: at most one active booking per slot."""


class UnconfirmedAction(RuntimeError):
    """`execute` was handed an action without `confirmed_at` and `confirm_trace_id`: a bug."""


@dataclass(frozen=True)
class ConfirmedAction:
    """A locked pending action whose confirmation the confirm service has just recorded."""

    id: UUID
    tool: ToolName
    arguments: Mapping[str, object]
    customer_ref: str
    confirmed_at: datetime | None
    confirm_trace_id: str | None


@dataclass(frozen=True)
class Executed:
    """The write happened; `output` is the tool's output model, stored as the action's result."""

    output: BaseModel


@dataclass(frozen=True)
class Failed:
    """Nothing was written; the action is marked `failed` with `reason`."""

    reason: WriteFailure


WriteOutcome = Executed | Failed

INSERT_BOOKING: Final = text(
    "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
    " action_id) VALUES (:business_id, :id, :slot_id, :customer_ref, 'active', 'action',"
    " :action_id)"
)
LOCK_OWN_ACTIVE_BOOKING: Final = text(reschedule.OWN_ACTIVE_BOOKING.text + " FOR UPDATE OF b")
"""The proposal's own booking check, with the booking row locked. In READ COMMITTED a waiting
second reschedule re-checks the WHERE clause on the row the first one committed: no longer
`active`, so no row (`booking_changed`). `OF b`: the slot is not locked (the app role may only read
slots; the unique index protects them)."""
RETIRE_BOOKING: Final = text(
    "UPDATE sandbox_bookings SET status = 'rescheduled', retired_by_action_id = :action_id"
    " WHERE business_id = :business_id AND customer_ref = :customer_ref AND id = :booking_id"
    " AND status = 'active'"
)
INSERT_TICKET: Final = text(
    "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
    " action_id) VALUES (:business_id, :customer_ref, :category, :summary, 'action', :action_id)"
    " RETURNING business_id, id"
)


class _Changed(Exception):
    """Inside the SAVEPOINT: the guarded retire matched no row, so roll the savepoint back."""


def booking_id_for(action_id: UUID) -> str:
    """The new booking's id: `B-` and the first 16 hex digits of its action's id, upper case (a
    record id, migration 0006's pattern)."""
    return f"B-{action_id.hex[:16].upper()}"


def slot_taken(error: IntegrityError) -> bool:
    """Whether `error` is the one-active-booking-per-slot index (the slot was just taken)."""
    cause = getattr(error.orig, "__cause__", None)
    return getattr(cause, "constraint_name", None) == ONE_ACTIVE_PER_SLOT


async def execute(
    connection: AsyncConnection, action: ConfirmedAction, scope: ToolScope
) -> WriteOutcome:
    """Re-validate and write `action` on `connection` (inside the confirm transaction)."""
    if action.confirmed_at is None or action.confirm_trace_id is None:
        raise UnconfirmedAction("execute runs only an action the confirm route has confirmed")
    if action.customer_ref != scope.customer_ref:
        raise UnconfirmedAction("the action's customer is not the scope's")
    config = scope.config
    if action.tool not in config.enabled_tools:
        return Failed(WriteFailure.NOT_ENABLED)
    spec = REGISTRY[action.tool]
    if not isinstance(spec, WriteTool):
        raise UnconfirmedAction("only a write tool is executed")
    try:
        arguments = business_input_model(spec, config).model_validate(dict(action.arguments))
    except ValidationError:
        return Failed(WriteFailure.INVALID_ARGUMENTS)
    if isinstance(arguments, BookInput):
        return await _book(connection, action, arguments, scope)
    if isinstance(arguments, RescheduleInput):
        return await _reschedule(connection, action, arguments, scope)
    if isinstance(arguments, CreateTicketInput):
        return await _create_ticket(connection, action, arguments, scope)
    raise UnconfirmedAction("no write for this tool")  # pragma: no cover (the registry's three)


async def _book(
    connection: AsyncConnection, action: ConfirmedAction, arguments: BookInput, scope: ToolScope
) -> WriteOutcome:
    if isinstance(await book.validate(connection, arguments, scope), ToolError):
        return Failed(WriteFailure.SLOT_UNAVAILABLE)  # gone, taken or started: nothing to book
    booking_id = booking_id_for(action.id)
    try:
        async with connection.begin_nested():
            await connection.execute(
                INSERT_BOOKING,
                {
                    "business_id": scope.business_id,
                    "id": booking_id,
                    "slot_id": arguments.slot_id,
                    "customer_ref": scope.customer_ref,
                    "action_id": action.id,
                },
            )
    except IntegrityError as error:
        if slot_taken(error):
            return Failed(WriteFailure.SLOT_UNAVAILABLE)
        raise
    return Executed(BookOutput(booking_id=booking_id, slot_id=arguments.slot_id))


async def _reschedule(
    connection: AsyncConnection,
    action: ConfirmedAction,
    arguments: RescheduleInput,
    scope: ToolScope,
) -> WriteOutcome:
    locked = await scope.fetch(
        connection,
        LOCK_OWN_ACTIVE_BOOKING,
        {"booking_id": arguments.booking_id, "sandbox_now": scope.sandbox_now},
    )
    if len(locked) != 1 or locked[0].starts_at < scope.sandbox_now:
        return Failed(WriteFailure.BOOKING_CHANGED)
    # The booking is ours, locked and movable; any refusal now is about the new slot.
    if isinstance(await reschedule.validate(connection, arguments, scope), ToolError):
        return Failed(WriteFailure.SLOT_UNAVAILABLE)
    booking_id = booking_id_for(action.id)
    try:
        async with connection.begin_nested():
            retired = await connection.execute(
                RETIRE_BOOKING,
                {
                    "business_id": scope.business_id,
                    "customer_ref": scope.customer_ref,
                    "booking_id": arguments.booking_id,
                    "action_id": action.id,
                },
            )
            if retired.rowcount != 1:
                raise _Changed
            await connection.execute(
                INSERT_BOOKING,
                {
                    "business_id": scope.business_id,
                    "id": booking_id,
                    "slot_id": arguments.new_slot_id,
                    "customer_ref": scope.customer_ref,
                    "action_id": action.id,
                },
            )
    except _Changed:  # the second fence: reached only if the lock above were ever lost
        return Failed(WriteFailure.BOOKING_CHANGED)
    except IntegrityError as error:
        if slot_taken(error):
            return Failed(WriteFailure.SLOT_UNAVAILABLE)
        raise
    return Executed(
        RescheduleOutput(
            retired_booking_id=arguments.booking_id,
            booking_id=booking_id,
            slot_id=arguments.new_slot_id,
        )
    )


async def _create_ticket(
    connection: AsyncConnection,
    action: ConfirmedAction,
    arguments: CreateTicketInput,
    scope: ToolScope,
) -> WriteOutcome:
    if isinstance(await create_ticket.validate(connection, arguments, scope), ToolError):
        return Failed(WriteFailure.INVALID_ARGUMENTS)  # the category is no longer configured
    rows = await scope.fetch(
        connection,
        INSERT_TICKET,
        {
            "category": arguments.category,
            "summary": arguments.summary,
            "action_id": action.id,
        },
    )
    return Executed(CreateTicketOutput(ticket_id=rows[0].id))
