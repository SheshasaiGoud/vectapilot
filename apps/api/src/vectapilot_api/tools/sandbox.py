"""The sandbox seed (`<business_id>/seed.json`) and its reset, as the owner (ADR 0016 decision 6).

**The seed** is synthetic, per business: bookable slots, existing bookings (`origin = 'seed'`) and
orders, two or more customer references, `synthetic: true` and a canary string unique to the
business. The canary appears in at least one seeded label or note, so a leak test that looks for
it has something to find (the positive control). Some free-text values carry instruction-like text
on purpose (W10): tool outputs are data, never instructions (rule 5), and PR 2-4's tests feed them
to the summaries and the loop.

**The reset** reseeds one business: it registers the business if it is missing, deletes that
business's sandbox rows and pending actions, children before parents, and inserts the seed. It
runs as the owner, which skips row-level security (a superuser locally and in CI, ADR 0009
"Limit"), so RLS cannot keep the other business out: every statement names
`business_id = :business_id` itself, and `tests/tools/test_sandbox_reset.py` proves on the owner's
connection that the other business's rows and every other table stay byte-for-byte the same (and
that a statement without its clause is caught). It also runs inside `business_transaction` for
that business, so under a non-superuser owner (M10) RLS adds a second fence; `reset_sandbox`
refuses a connection set to any other business, or none.

It never deletes `tool_audit` (append-only, W12): an audit row outlives the action it names, which
is why `tool_audit.action_id` has no foreign key. Only the command line (`cli.py`) calls it, and
only in `SANDBOX_ENVIRONMENTS`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.tools.config import (
    Code,
    CustomerRef,
    RecordId,
    SingleLine,
    ToolsConfig,
    ToolsConfigError,
    ToolsModel,
    describe_validation_error,
)

SANDBOX_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
"""Where the reset may run: never `production`, nor any environment added later."""

OrderStatus = Literal["placed", "packed", "shipped", "delivered", "cancelled"]
"""Migration 0006's `sandbox_orders.status` values (a test compares)."""

Canary = Annotated[str, Field(min_length=16, max_length=64, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
"""At least 16 characters, so it cannot match other text by chance and fake a leak or hide one."""

SLOT_DURATION_MINUTES: Final = (5, 480)
LABEL_MAX: Final = 200
ITEMS_MAX: Final = 300
NOTE_MAX: Final = 500
"""Migration 0006's CHECKs (a test compares), so a seed that passes here never meets a bare
SQLSTATE there."""


class SeedSlot(ToolsModel):
    id: RecordId
    service: Code
    starts_at: AwareDatetime
    duration_minutes: int = Field(ge=SLOT_DURATION_MINUTES[0], le=SLOT_DURATION_MINUTES[1])
    label: Annotated[SingleLine, Field(max_length=LABEL_MAX)]
    """Free text shown by `check_slots`: untrusted data (W10)."""


class SeedBooking(ToolsModel):
    """An existing booking. Always `active` and `origin = 'seed'`: a seed never holds a write."""

    id: RecordId
    slot_id: RecordId
    customer_ref: CustomerRef


class SeedOrder(ToolsModel):
    id: RecordId
    customer_ref: CustomerRef
    status: OrderStatus
    items: Annotated[SingleLine, Field(max_length=ITEMS_MAX)]
    note: Annotated[SingleLine, Field(max_length=NOTE_MAX)] | None = None
    """Free text shown by `order_status`: untrusted data (W10)."""
    placed_at: AwareDatetime
    expected_delivery: date | None = None


class SandboxSeed(ToolsModel):
    """`seed.json`: one business's synthetic sandbox."""

    business_id: BusinessId
    synthetic: bool
    canary: Canary
    customers: tuple[CustomerRef, ...] = Field(min_length=2)
    slots: tuple[SeedSlot, ...]
    bookings: tuple[SeedBooking, ...]
    orders: tuple[SeedOrder, ...]

    @field_validator("synthetic")
    @classmethod
    def _must_be_synthetic(cls, synthetic: bool) -> bool:
        if not synthetic:
            raise ValueError("the sandbox seed is synthetic (ADR 0016 decision 6)")
        return synthetic

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """The database's keys and checks, before the database (so a bad seed names its rule)."""
        if len(set(self.customers)) != len(self.customers):
            raise ValueError("each customer is listed once")
        for rows, name in (
            (self.slots, "slot"),
            (self.bookings, "booking"),
            (self.orders, "order"),
        ):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError(f"each {name} id is listed once")
        slot_ids = {slot.id for slot in self.slots}
        if any(booking.slot_id not in slot_ids for booking in self.bookings):
            raise ValueError("every booking's slot is a seeded slot")
        if len({booking.slot_id for booking in self.bookings}) != len(self.bookings):
            raise ValueError("at most one active booking per slot")
        owners = [booking.customer_ref for booking in self.bookings]
        owners += [order.customer_ref for order in self.orders]
        if any(owner not in self.customers for owner in owners):
            raise ValueError("every booking and order belongs to a listed customer")
        free_text = [slot.label for slot in self.slots]
        free_text += [order.note for order in self.orders if order.note is not None]
        if not any(self.canary in value for value in free_text):
            raise ValueError("the canary appears in at least one slot label or order note")
        return self


def parse_seed(text_: str, *, where: str) -> SandboxSeed:
    """Parse and validate one `seed.json`; `ToolsConfigError` names `where`, fields and rule."""
    try:
        return SandboxSeed.model_validate_json(text_)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise ToolsConfigError(f"{where} is not a valid sandbox seed: {detail}")


def seed_problems(seed: SandboxSeed, config: ToolsConfig) -> list[str]:
    """How `seed` disagrees with its business's `config`, as fixed text (empty when it agrees)."""
    problems: list[str] = []
    if seed.business_id != config.business_id:
        problems.append("seed.json and tools.json name different businesses")
    if any(slot.service not in config.service_ids for slot in seed.slots):
        problems.append("every slot's service is one of tools.json's services")
    for slot in seed.slots:
        offset = slot.starts_at.utcoffset()
        if offset != slot.starts_at.astimezone(config.zone).utcoffset():
            problems.append("every slot's start carries the business timezone's offset")
            break
    return problems


# ── The reset ────────────────────────────────────────────────────────────────────────────────

RESET_TABLES: Final = (
    "sandbox_tickets",
    "sandbox_bookings",
    "sandbox_orders",
    "pending_actions",
    "sandbox_slots",
)
"""What a reset empties for one business, children before parents (migration 0006's composite
foreign keys have no ON DELETE CASCADE). Never `tool_audit`."""

APPEND_ONLY: Final = frozenset({"tool_audit"})

_CURRENT_BUSINESS: Final = text("SELECT current_setting('app.business_id', true)")
REGISTER: Final = text(
    "INSERT INTO businesses (business_id) VALUES (:business_id)"
    " ON CONFLICT (business_id) DO NOTHING"
)
"""The registry is the owner's to write (migration 0002); the seed's business is registered once."""
DELETES: Final = tuple(
    text(f"DELETE FROM {table} WHERE business_id = :business_id")  # noqa: S608 (constants)
    for table in RESET_TABLES
)
INSERT_SLOT: Final = text(
    "INSERT INTO sandbox_slots (business_id, id, service, starts_at, duration_minutes, label)"
    " VALUES (:business_id, :id, :service, :starts_at, :duration_minutes, :label)"
)
INSERT_BOOKING: Final = text(
    "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin)"
    " VALUES (:business_id, :id, :slot_id, :customer_ref, 'active', 'seed')"
)
INSERT_ORDER: Final = text(
    "INSERT INTO sandbox_orders (business_id, id, customer_ref, status, items, note, placed_at,"
    " expected_delivery)"
    " VALUES (:business_id, :id, :customer_ref, :status, :items, :note, :placed_at,"
    " :expected_delivery)"
)
RESET_STATEMENTS: Final = (REGISTER, *DELETES, INSERT_SLOT, INSERT_BOOKING, INSERT_ORDER)
"""Every statement a reset sends, in order; each names `:business_id` (a test checks)."""


class SandboxRefused(RuntimeError):
    """The reset would run where it must not. Fixed text, never a value."""


@dataclass(frozen=True)
class ResetCounts:
    slots: int
    bookings: int
    orders: int


def _rows(business_id: str, rows: Sequence[ToolsModel]) -> list[Mapping[str, object]]:
    """Each row's fields plus its business: the INSERT's parameters, `business_id` in every one."""
    return [{"business_id": business_id, **row.model_dump()} for row in rows]


async def reset_sandbox(connection: AsyncConnection, seed: SandboxSeed) -> ResetCounts:
    """Reseed `seed`'s business on `connection`, which must be an owner connection inside
    `business_transaction(engine, seed.business_id)` (the caller commits). Refuses a connection
    set to another business, or to none, before it sends anything else."""
    current = await connection.scalar(_CURRENT_BUSINESS)
    if current != seed.business_id:
        raise SandboxRefused(
            "the reset runs inside business_transaction for the seed's own business"
        )
    params = {"business_id": seed.business_id}
    await connection.execute(REGISTER, params)
    for statement in DELETES:
        await connection.execute(statement, params)
    batches = (
        (INSERT_SLOT, _rows(seed.business_id, seed.slots)),
        (INSERT_BOOKING, _rows(seed.business_id, seed.bookings)),
        (INSERT_ORDER, _rows(seed.business_id, seed.orders)),
    )
    for statement, rows in batches:
        if rows:  # executemany refuses an empty list
            await connection.execute(statement, rows)
    return ResetCounts(len(seed.slots), len(seed.bookings), len(seed.orders))
