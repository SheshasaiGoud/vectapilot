"""`check_slots(date_from, date_to, service?)`: the business's free slots in a date range (read).

Closed and bounded (ADR 0016 decision 1) [proposal]: the range is at most `MAX_RANGE_DAYS` local
days, inclusive; at most `MAX_SLOTS` slots come back, earliest first, with `truncated` set when
there were more. `service` is one of the business's configured service ids. A slot is listed when
it starts in the range, at or after the sandbox clock's "now" (`ToolScope.sandbox_now`), has no
active booking, and its service is one the business configures.

A slot's `label` is free text from the sandbox: untrusted data (rule 5, W10). Some seeded labels
read like instructions on purpose. It is shown as data in the observation and never in any
summary (the write tools' validation never selects it).
"""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Final, Self

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.config import (
    CODE_PATTERN,
    RECORD_ID_MAX,
    RECORD_ID_PATTERN,
    ToolName,
    ToolsConfig,
    ToolsModel,
)
from vectapilot_api.tools.sandbox import LABEL_MAX
from vectapilot_api.tools.spec import ReadTool, ToolScope
from vectapilot_api.tools.types import ToolError

MAX_RANGE_DAYS: Final = 14
MAX_SLOTS: Final = 20
EARLIEST: Final = date(2000, 1, 1)
LATEST: Final = date(2099, 12, 31)

ServiceId = Annotated[str, Field(max_length=40, pattern=CODE_PATTERN)]
SlotId = Annotated[str, Field(max_length=RECORD_ID_MAX, pattern=RECORD_ID_PATTERN)]
BoundedDate = Annotated[date, Field(ge=EARLIEST, le=LATEST)]


class CheckSlotsInput(ToolsModel):
    date_from: BoundedDate = Field(description="First local day to search (YYYY-MM-DD).")
    date_to: BoundedDate = Field(
        description=f"Last local day to search, at most {MAX_RANGE_DAYS} days in all."
    )
    service: ServiceId | None = Field(default=None, description="Only this service.")

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.date_to < self.date_from:
            raise ValueError("date_to is on or after date_from")
        if (self.date_to - self.date_from).days >= MAX_RANGE_DAYS:
            raise ValueError(f"the range is at most {MAX_RANGE_DAYS} days")
        return self


class SlotView(ToolsModel):
    slot_id: SlotId
    service: ServiceId
    service_name: str = Field(max_length=100)
    starts_at: AwareDatetime
    """In the business's timezone."""
    duration_minutes: int = Field(ge=1, le=1440)
    label: str = Field(max_length=LABEL_MAX)
    """Sandbox free text: data, never an instruction."""


class CheckSlotsOutput(ToolsModel):
    timezone: str = Field(max_length=64)
    slots: tuple[SlotView, ...] = Field(max_length=MAX_SLOTS)
    truncated: bool


FREE_SLOTS: Final = text(
    "SELECT s.business_id, s.id, s.service, s.starts_at, s.duration_minutes, s.label"
    " FROM sandbox_slots s"
    " WHERE s.business_id = :business_id"
    " AND s.starts_at >= :starts AND s.starts_at < :ends"
    " AND s.service = ANY(CAST(:services AS text[]))"
    " AND NOT EXISTS (SELECT 1 FROM sandbox_bookings b"
    "   WHERE b.business_id = s.business_id AND b.slot_id = s.id AND b.status = 'active')"
    " ORDER BY s.starts_at, s.id"
    " LIMIT :limit"
)


async def run(
    connection: AsyncConnection, arguments: CheckSlotsInput, scope: ToolScope
) -> CheckSlotsOutput | ToolError:
    config = scope.config
    starts = max(datetime.combine(arguments.date_from, time(0), config.zone), scope.sandbox_now)
    ends = datetime.combine(arguments.date_to + timedelta(days=1), time(0), config.zone)
    services = [arguments.service] if arguments.service is not None else sorted(config.service_ids)
    rows = await scope.fetch(
        connection,
        FREE_SLOTS,
        {"starts": starts, "ends": ends, "services": services, "limit": MAX_SLOTS + 1},
    )
    slots = tuple(
        SlotView(
            slot_id=row.id,
            service=row.service,
            service_name=config.service_name(row.service) or row.service,
            starts_at=row.starts_at.astimezone(config.zone),
            duration_minutes=row.duration_minutes,
            label=row.label,
        )
        for row in rows[:MAX_SLOTS]
    )
    return CheckSlotsOutput(timezone=config.timezone, slots=slots, truncated=len(rows) > MAX_SLOTS)


def keep_rows(output: CheckSlotsOutput, rows: int) -> CheckSlotsOutput:
    if len(output.slots) <= rows:
        return output
    return output.model_copy(update={"slots": output.slots[:rows], "truncated": True})


def services(config: ToolsConfig) -> tuple[str, ...]:
    return tuple(sorted(config.service_ids))


TOOL: Final = ReadTool(
    name=ToolName.CHECK_SLOTS,
    input_model=CheckSlotsInput,
    output_model=CheckSlotsOutput,
    run=run,
    cap_rows=keep_rows,
    config_enums={"service": services},
)
