"""What a tool is (ADR 0016 decision 1, M5 PR 2): a closed input model, a closed output model, a
kind, a timeout and, for a write tool, a summary template. One module per tool fills these in;
`registry.py` lists them.

**Every query goes through `ToolScope.fetch`.** It binds `:business_id` and `:customer_ref` from
the injected context (W7), so a tool cannot name another business or customer, and it records the
`business_id` of every row it returns (the envelope's ids for the eval's leak check). A statement
must therefore select `business_id`; one that does not is refused before it runs. The statement's
own `WHERE ... business_id = :business_id` is still required: RLS hides a missing clause on the
app role, and the canary suite runs every tool on the owner's connection too, where only the
clause keeps the other business out.

**Per-business enums.** `check_slots`' `service` and `create_ticket`'s `category` are closed
enums from the business's `tools.json` (rule 9). The platform models type them as codes;
`business_input_model` narrows them to the business's `Literal` values, so the JSON schema the
model and MCP clients see lists exactly that business's ids, and validation refuses any other.

**The sandbox clock.** A slot is in the future when it starts at or after midnight of the
business's `sandbox_date` in its timezone (`sandbox_now`, as PR 1's seed test). It decides which
slots `check_slots` lists and `book`/`reschedule` accept, and which bookings `reschedule` may move
(not one whose slot has started); the database clock decides nothing here.

**Known gap: summaries are English-only until M8's widget** (decided 2026-11-10, shesha). Every
write summary is a fixed English template with English day and month names (`WEEKDAYS`,
`MONTHS`), whatever the business's or the customer's language; only `create_ticket`'s quoted text
stays in the customer's own words. PR 4 and the M5 note say confirmations are English-only until
then.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, Field, create_model
from sqlalchemy import Row, TextClause
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.tools.config import ToolName, ToolsConfig
from vectapilot_api.tools.types import ToolError, ToolKind

READ_TIMEOUT_S: Final = 2.0
WRITE_TIMEOUT_S: Final = 5.0
"""W9's budgets [proposal]: a read tool's whole call, and a write tool's confirm transaction
(PR 3). A proposal is a read (validation only), so `propose` runs within `READ_TIMEOUT_S`."""

WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS: Final = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
"""Fixed English names: `strftime`'s `%A`/`%B` follow the process locale, so a summary would
change with the server's settings."""


class ForeignRowError(RuntimeError):
    """A statement without `business_id` in its result: its rows could not be checked."""


@dataclass
class ToolScope:
    """One call's injected context and the business ids of every row it read."""

    business_id: BusinessId
    customer_ref: str
    config: ToolsConfig
    seen: set[str] = field(default_factory=set)

    @property
    def sandbox_now(self) -> datetime:
        return datetime.combine(self.config.sandbox_date, time(0), self.config.zone)

    async def fetch(
        self,
        connection: AsyncConnection,
        statement: TextClause,
        params: Mapping[str, object] | None = None,
    ) -> Sequence[Row[Any]]:
        """Run `statement` with the context bound and record each row's business."""
        bound = {
            **(params or {}),
            "business_id": self.business_id,
            "customer_ref": self.customer_ref,
        }
        result = await connection.execute(statement, bound)
        columns = tuple(result.keys())
        if "business_id" not in columns:
            raise ForeignRowError("a tool statement selects business_id, so its rows are checked")
        rows = result.all()
        self.seen.update(str(row.business_id) for row in rows)
        return rows


def local_day(moment: datetime, config: ToolsConfig) -> str:
    """`moment` as the business's local day, in words: "Monday 16 November 2026"."""
    local = moment.astimezone(config.zone)
    return f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]} {local.year}"


def local_time(moment: datetime, config: ToolsConfig) -> str:
    """`moment` as the business's local 24-hour time: "09:30"."""
    local = moment.astimezone(config.zone)
    return f"{local.hour:02d}:{local.minute:02d}"


ConfigEnum = Callable[[ToolsConfig], tuple[str, ...]]
"""A field's allowed values for one business, from its configuration."""


@dataclass(frozen=True)
class ReadTool[I: BaseModel, O: BaseModel]:
    """A read tool: runs in a read-only snapshot (W8) and returns its output or a typed error."""

    name: ToolName
    input_model: type[I]
    output_model: type[O]
    run: Callable[[AsyncConnection, I, ToolScope], Awaitable[O | ToolError]]
    timeout_s: float = READ_TIMEOUT_S
    cap_rows: Callable[[O, int], O] | None = None
    """Keeps the first n rows of an output and marks it truncated (W10's row and byte caps)."""
    config_enums: Mapping[str, ConfigEnum] = field(default_factory=dict)

    @property
    def kind(self) -> ToolKind:
        return ToolKind.READ


@dataclass(frozen=True)
class WriteTool[I: BaseModel, S: BaseModel, O: BaseModel]:
    """A write tool. Here it only validates and renders its summary (W1, W3); `output_model` is
    what PR 3's confirm stores as the executed result.

    `validate` reads what the summary needs, by typed columns only (never a slot label, an order
    note or any other free-text column), and returns the summary's fields or a typed refusal.
    `summary_template` is a constant `str.format` template over those fields: values are
    substituted, never parsed, so no field can add text of its own."""

    name: ToolName
    input_model: type[I]
    summary_model: type[S]
    output_model: type[O]
    validate: Callable[[AsyncConnection, I, ToolScope], Awaitable[S | ToolError]]
    summary_template: str
    timeout_s: float = WRITE_TIMEOUT_S
    config_enums: Mapping[str, ConfigEnum] = field(default_factory=dict)

    @property
    def kind(self) -> ToolKind:
        return ToolKind.WRITE

    def summary(self, fields: S) -> str:
        values = {name: str(value) for name, value in fields.model_dump(mode="json").items()}
        return self.summary_template.format_map(values)


AnyTool = ReadTool[Any, Any] | WriteTool[Any, Any, Any]


def business_input_model(tool: AnyTool, config: ToolsConfig) -> type[BaseModel]:
    """`tool`'s input model for one business: each configured enum field narrowed to that
    business's values (a `Literal`), everything else as the platform model. The same class name,
    so the schema's title does not change."""
    base: type[BaseModel] = tool.input_model
    if not tool.config_enums:
        return base
    overrides: dict[str, Any] = {}
    for name, values_of in tool.config_enums.items():
        info = base.model_fields[name]
        values = values_of(config)
        if not values:
            raise ValueError(f"{tool.name.value}: the business configures no {name} values")
        allowed = cast(Any, Literal).__getitem__(values)
        # A fresh Field: the platform field's pattern and length belong to `str`, not a Literal.
        if info.is_required():
            overrides[name] = (allowed, Field(description=info.description))
        else:
            overrides[name] = (allowed | None, Field(default=None, description=info.description))
    return create_model(base.__name__, __base__=base, __module__=base.__module__, **overrides)
