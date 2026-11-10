"""`reschedule(booking_id, new_slot_id)`: propose moving one of the customer's active bookings to
a free slot (write; ADR 0016 decision 2).

`validate` reads the booking by business, customer and id, `active` only (ADR 0016 decision 9),
and only while its slot has not started by the sandbox clock (`starts_at >= sandbox_now`; decided
2026-11-10, shesha): another customer's booking, a retired one, a started one and one that does
not exist all give the same `not_found`. A started booking is filtered out in SQL, so its row is
never read and even the envelope's row ids match a missing booking's (no existence oracle); the
same check runs again on the row as a backstop. A booking starting exactly at "now" can still be
moved, as `book` accepts a slot starting exactly then. The new slot must pass `book.free_slot`'s
checks; the booking's own slot is taken by the booking itself, so "moving" to it is
`slot_unavailable`. PR 3's confirm runs every check again with both rows locked (W5).

The summary (W3) shows both slots through `book.slot_fields`: typed values only, never a label.
"""

from typing import Final

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.book import BookingId, SlotFields, free_slot, slot_fields
from vectapilot_api.tools.check_slots import SlotId
from vectapilot_api.tools.config import ToolName, ToolsModel
from vectapilot_api.tools.spec import ToolScope, WriteTool
from vectapilot_api.tools.types import ToolError, ToolErrorCode


class RescheduleInput(ToolsModel):
    booking_id: BookingId = Field(description="The customer's booking to move.")
    new_slot_id: SlotId = Field(description="The free slot to move it to, from check_slots.")


class RescheduleOutput(ToolsModel):
    """What PR 3's confirm stores as the executed result."""

    retired_booking_id: BookingId
    booking_id: BookingId
    slot_id: SlotId


OWN_ACTIVE_BOOKING: Final = text(
    "SELECT b.business_id, b.id AS booking_id, s.id, s.service, s.starts_at, s.duration_minutes"
    " FROM sandbox_bookings b"
    " JOIN sandbox_slots s ON s.business_id = b.business_id AND s.id = b.slot_id"
    " WHERE b.business_id = :business_id AND b.customer_ref = :customer_ref"
    " AND b.id = :booking_id AND b.status = 'active'"
    " AND s.starts_at >= :sandbox_now"
)
"""Typed columns only. A row's `id` is its slot's, so `book.slot_fields` reads it as a slot.
`:sandbox_now` is the sandbox clock's (`ToolScope.sandbox_now`), never the database's."""

TEMPLATE: Final = (
    "Move booking {booking_id} from {old_service} on {old_day} at {old_time}"
    " to {new_service} on {new_day} at {new_time} ({new_minutes} minutes, {timezone}),"
    " slot {new_slot_id}."
)


class RescheduleSummary(ToolsModel):
    """`RescheduleFields`, flat for the template."""

    booking_id: BookingId
    old_service: str = Field(max_length=100)
    old_day: str = Field(max_length=40)
    old_time: str = Field(max_length=5)
    new_service: str = Field(max_length=100)
    new_day: str = Field(max_length=40)
    new_time: str = Field(max_length=5)
    new_minutes: int = Field(ge=1, le=1440)
    timezone: str = Field(max_length=64)
    new_slot_id: SlotId

    @classmethod
    def of(cls, booking_id: str, old: SlotFields, new: SlotFields) -> "RescheduleSummary":
        return cls(
            booking_id=booking_id,
            old_service=old.service,
            old_day=old.day,
            old_time=old.time,
            new_service=new.service,
            new_day=new.day,
            new_time=new.time,
            new_minutes=new.minutes,
            timezone=new.timezone,
            new_slot_id=new.slot_id,
        )


async def validate(
    connection: AsyncConnection, arguments: RescheduleInput, scope: ToolScope
) -> RescheduleSummary | ToolError:
    rows = await scope.fetch(
        connection,
        OWN_ACTIVE_BOOKING,
        {"booking_id": arguments.booking_id, "sandbox_now": scope.sandbox_now},
    )
    if len(rows) != 1 or rows[0].starts_at < scope.sandbox_now:
        return ToolError.of(ToolErrorCode.NOT_FOUND)
    old = slot_fields(rows[0], scope)
    if old is None:
        return ToolError.of(ToolErrorCode.NOT_FOUND)
    new = await free_slot(connection, arguments.new_slot_id, scope)
    if isinstance(new, ToolError):
        return new
    return RescheduleSummary.of(arguments.booking_id, old, new)


TOOL: Final = WriteTool(
    name=ToolName.RESCHEDULE,
    input_model=RescheduleInput,
    summary_model=RescheduleSummary,
    output_model=RescheduleOutput,
    validate=validate,
    summary_template=TEMPLATE,
)
