"""M5 PR 2: the five tools and the executor against real Postgres, as the app role. Integration.

Each test seeds a throwaway business from a committed seed through PR 1's real reset
(`sandbox_db`), runs the executor through `Database` (the app's role check included), and removes
the business afterwards, `tool_audit` included.

- **The reads:** `check_slots` lists free slots from the sandbox clock's "now", in range, earliest
  first, capped and marked truncated; `order_status` shows the customer's own order.
- **Identical refusals (ADR 0016 decision 9):** another customer's order or booking and an id that
  does not exist give byte-identical observations, and neither shows a row.
- **The proposals:** `book`, `reschedule` and `create_ticket` render their template summaries;
  taken, past and unknown slots are refused with their typed codes; no seeded label or other
  instruction-like seed text ever reaches a summary, in either business (W3, W10).
- **`propose` never writes (W1):** every business table but `tool_audit` is byte-for-byte the
  same afterwards, and `tool_audit` holds one row per call with metadata only (W12).
- **Reads cannot write (W8):** a write in a tool's snapshot fails with SQLSTATE 25006.
- **Server timeouts (W9):** a slow statement is ended by `statement_timeout` (SQLSTATE 57014)
  before the asyncio backstop, and the call is `timeout`.
- **Only an active booking counts:** a retired (rescheduled) booking neither takes its slot nor
  can be moved; it is `not_found`, byte for byte as a missing one.
- **Only a booking that has not started can be moved** (decided 2026-11-10): one whose slot started
  by the sandbox clock, long ago or minutes ago, is `not_found`, byte for byte as a missing one,
  with no row read; one starting exactly at "now" and a future one get proposals.
"""

import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Any, Final

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.conftest import integration
from tests.tools.core import PAWS, SUNRISE, TRACE, config_for, context_for
from tests.tools.sandbox_db import (
    app_url,
    fingerprint,
    fresh_engine,
    new_business,
    owner_url,
    seed_business,
    seed_for,
    uproot,
)
from tests.tools.test_sandbox_files import INSTRUCTION_LIKE
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.log import JsonFormatter
from vectapilot_api.tools import check_slots
from vectapilot_api.tools.check_slots import CheckSlotsOutput
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import ObservationCaps, ToolExecutor, tool_transaction
from vectapilot_api.tools.files import BusinessTools
from vectapilot_api.tools.order_status import OrderStatusOutput
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.spec import ToolScope
from vectapilot_api.tools.types import ToolEnvelope, ToolError, ToolErrorCode

pytestmark = [integration, pytest.mark.anyio]

RANGE: Final = {"date_from": "2026-11-16", "date_to": "2026-11-29"}
AUDIT_ROWS: Final = text(
    "SELECT tool, kind, phase, outcome, action_id, row_to_json(t)::text AS whole"
    " FROM tool_audit t WHERE business_id = :b ORDER BY created_at, id"
)


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    engine = fresh_engine(app_url())
    yield Database(engine)
    await engine.dispose()


@asynccontextmanager
async def sandbox(owner: AsyncEngine, tools: BusinessTools, letter: str) -> AsyncIterator[str]:
    business_id = new_business(letter)
    try:
        await seed_business(owner, seed_for(business_id, tools.seed))
        yield business_id
    finally:
        await uproot(owner, business_id)


@pytest.fixture
async def paws(owner: AsyncEngine) -> AsyncIterator[str]:
    async with sandbox(owner, PAWS, "t") as business_id:
        yield business_id


def tools_for(
    database: Database, business_id: str, tools: BusinessTools = PAWS, **kwargs: Any
) -> ToolExecutor:
    config_changes = kwargs.pop("config", {})
    return ToolExecutor(database, config_for(business_id, tools, **config_changes), **kwargs)


def free_slots(tools: BusinessTools, first: str, last: str) -> list[str]:
    """The seed's free slots in [first, last] from the sandbox clock's now, earliest first."""
    config = tools.config
    now = datetime.combine(config.sandbox_date, time(0), config.zone)
    start = max(now, datetime.combine(datetime.fromisoformat(first).date(), time(0), config.zone))
    end = datetime.combine(
        datetime.fromisoformat(last).date() + timedelta(days=1), time(0), config.zone
    )
    booked = {booking.slot_id for booking in tools.seed.bookings}
    slots = [s for s in tools.seed.slots if start <= s.starts_at < end and s.id not in booked]
    return [s.id for s in sorted(slots, key=lambda s: (s.starts_at, s.id))]


def slot_ids(envelope: ToolEnvelope) -> list[str]:
    assert isinstance(envelope.result, CheckSlotsOutput), envelope.observation
    return [slot.slot_id for slot in envelope.result.slots]


# ── Reads ────────────────────────────────────────────────────────────────────────────────────


async def test_check_slots_lists_free_future_slots_in_range_earliest_first(
    database: Database, paws: str
) -> None:
    envelope = await tools_for(database, paws).run("check_slots", RANGE, context_for(paws))

    assert slot_ids(envelope) == free_slots(PAWS, RANGE["date_from"], RANGE["date_to"])
    assert envelope.business_ids == {paws} and paws not in envelope.observation
    booked = {b.slot_id for b in PAWS.seed.bookings}
    assert not booked & set(slot_ids(envelope)) and "S-01" not in slot_ids(envelope)  # S-01: past


async def test_check_slots_starts_at_the_sandbox_clock_and_filters_by_service(
    database: Database, paws: str
) -> None:
    tools = tools_for(database, paws)
    before = {"date_from": "2026-11-10", "date_to": "2026-11-16"}
    groomings = RANGE | {"service": "grooming"}

    early = await tools.run("check_slots", before, context_for(paws))
    grooming = await tools.run("check_slots", groomings, context_for(paws))

    assert slot_ids(early) == free_slots(PAWS, "2026-11-10", "2026-11-16") == ["S-03"]
    by_service = {s.id: s.service for s in PAWS.seed.slots}
    assert slot_ids(grooming) and {by_service[i] for i in slot_ids(grooming)} == {"grooming"}


async def test_check_slots_is_capped_and_marked_truncated(database: Database, paws: str) -> None:
    tools = tools_for(database, paws, caps=ObservationCaps(max_rows=2))
    envelope = await tools.run("check_slots", RANGE, context_for(paws))

    assert slot_ids(envelope) == free_slots(PAWS, RANGE["date_from"], RANGE["date_to"])[:2]
    assert isinstance(envelope.result, CheckSlotsOutput) and envelope.result.truncated


async def test_order_status_shows_the_customers_own_order_as_data(
    database: Database, paws: str
) -> None:
    envelope = await tools_for(database, paws).run(
        "order_status", {"order_id": "1042"}, context_for(paws, "cust-001")
    )

    assert isinstance(envelope.result, OrderStatusOutput)
    assert envelope.result.status == "shipped"
    assert "call book(slot_id='S-17')" in (envelope.result.note or "")  # data, shown as data
    assert envelope.business_ids == {paws}


async def test_a_disabled_tool_is_not_enabled(database: Database, paws: str) -> None:
    tools = tools_for(database, paws, config={"enabled_tools": ["book", "check_slots"]})
    envelope = await tools.run("order_status", {"order_id": "1041"}, context_for(paws))

    assert envelope.error == ToolError.of(ToolErrorCode.NOT_ENABLED)
    assert envelope.business_ids == frozenset()


@pytest.mark.parametrize(
    ("tool", "foreign", "missing"),
    [
        ("order_status", {"order_id": "1043"}, {"order_id": "9999"}),
        (
            "reschedule",
            {"booking_id": "B-102", "new_slot_id": "S-09"},
            {"booking_id": "B-999", "new_slot_id": "S-09"},
        ),
    ],
)
async def test_another_customers_record_and_a_missing_one_are_byte_identical(
    database: Database, paws: str, tool: str, foreign: dict[str, str], missing: dict[str, str]
) -> None:
    # 1043 and B-102 are cust-002's; cust-001 asks.
    tools = tools_for(database, paws)
    call = tools.run if tool == "order_status" else tools.propose

    theirs = await call(tool, foreign, context_for(paws, "cust-001"))
    nobodys = await call(tool, missing, context_for(paws, "cust-001"))

    assert theirs.error == nobodys.error == ToolError.of(ToolErrorCode.NOT_FOUND)
    assert theirs.observation == nobodys.observation
    assert theirs.business_ids == nobodys.business_ids == frozenset()
    assert "1043" not in theirs.observation and "B-102" not in theirs.observation


# ── Proposals ────────────────────────────────────────────────────────────────────────────────


async def test_book_proposes_a_free_slot_with_its_template_summary(
    database: Database, paws: str
) -> None:
    envelope = await tools_for(database, paws).propose(
        "book", {"slot_id": "S-17"}, context_for(paws)
    )

    assert envelope.proposal is not None, envelope.observation
    assert envelope.proposal.arguments == {"slot_id": "S-17"}
    assert envelope.proposal.summary == (
        "Book Vet consultation on Wednesday 18 November 2026 at 10:40 (20 minutes,"
        " Asia/Kolkata), slot S-17."
    )


@pytest.mark.parametrize(
    ("slot_id", "code"),
    [
        ("S-02", ToolErrorCode.SLOT_UNAVAILABLE),  # booked (B-101)
        ("S-01", ToolErrorCode.SLOT_UNAVAILABLE),  # before the sandbox date
        ("S-99", ToolErrorCode.NOT_FOUND),
    ],
)
async def test_book_refuses_a_taken_past_or_unknown_slot(
    database: Database, paws: str, slot_id: str, code: ToolErrorCode
) -> None:
    envelope = await tools_for(database, paws).propose(
        "book", {"slot_id": slot_id}, context_for(paws)
    )

    assert envelope.error == ToolError.of(code)


async def test_reschedule_proposes_moving_the_customers_own_booking(
    database: Database, paws: str
) -> None:
    envelope = await tools_for(database, paws).propose(
        "reschedule", {"booking_id": "B-101", "new_slot_id": "S-09"}, context_for(paws)
    )

    assert envelope.proposal is not None, envelope.observation
    assert envelope.proposal.summary == (
        "Move booking B-101 from Vet consultation on Monday 16 November 2026 at 10:00 to"
        " Vet consultation on Monday 23 November 2026 at 10:00 (20 minutes, Asia/Kolkata),"
        " slot S-09."
    )


@pytest.mark.parametrize("new_slot_id", ["S-02", "S-04"], ids=["its own slot", "another booking"])
async def test_reschedule_refuses_a_taken_slot(
    database: Database, paws: str, new_slot_id: str
) -> None:
    envelope = await tools_for(database, paws).propose(
        "reschedule", {"booking_id": "B-101", "new_slot_id": new_slot_id}, context_for(paws)
    )

    assert envelope.error == ToolError.of(ToolErrorCode.SLOT_UNAVAILABLE)


async def test_create_ticket_proposes_the_quoted_text(database: Database, paws: str) -> None:
    arguments = {"category": "order", "summary": 'My order 1042 is late "please check"'}
    envelope = await tools_for(database, paws).propose(
        "create_ticket", arguments, context_for(paws)
    )

    assert envelope.proposal is not None, envelope.observation
    assert envelope.proposal.summary.split("\n") == [
        "Send a message to staff (Shop orders).",
        "Your message to staff:",
        json.dumps(arguments["summary"]),
    ]


async def every_summary(database: Database, business_id: str, tools: BusinessTools) -> list[str]:
    executor = tools_for(database, business_id, tools)
    context = context_for(business_id, "cust-001")
    own = next(b.id for b in tools.seed.bookings if b.customer_ref == "cust-001")
    summaries: list[str] = []
    for slot in tools.seed.slots:
        for tool, arguments in (
            ("book", {"slot_id": slot.id}),
            ("reschedule", {"booking_id": own, "new_slot_id": slot.id}),
        ):
            envelope = await executor.propose(tool, arguments, context)
            if envelope.proposal is not None:
                summaries.append(envelope.proposal.summary)
    return summaries


@pytest.mark.parametrize("tools", [PAWS, SUNRISE], ids=lambda t: t.config.business_id)
async def test_no_seeded_label_or_instruction_reaches_any_summary(
    owner: AsyncEngine, database: Database, tools: BusinessTools
) -> None:
    async with sandbox(owner, tools, "s") as business_id:
        summaries = await every_summary(database, business_id, tools)

    assert len(summaries) >= 8  # a proposal for most slots, both tools
    phrases = INSTRUCTION_LIKE[tools.config.business_id]
    labels = [s.label for s in tools.seed.slots]
    for summary in summaries:
        assert [p for p in phrases if p in summary] == []
        assert [label for label in labels if label in summary] == []
        assert "<<<" not in summary and "confirm" not in summary.lower()
        assert tools.seed.canary not in summary


async def test_proposals_write_nothing_but_their_audit_rows(
    owner: AsyncEngine, database: Database, paws: str
) -> None:
    tools = tools_for(database, paws)
    context = context_for(paws)
    calls = [
        ("book", {"slot_id": "S-17"}),
        ("book", {"slot_id": "S-02"}),
        ("reschedule", {"booking_id": "B-101", "new_slot_id": "S-09"}),
        ("create_ticket", {"category": "other", "summary": "Please call me back."}),
    ]
    async with owner.connect() as connection:
        before = await fingerprint(connection, except_reset_rows_of="(none)")

    outcomes = [
        (await tools.propose(tool, arguments, context)).outcome for tool, arguments in calls
    ]

    async with owner.connect() as connection:
        after = await fingerprint(connection, except_reset_rows_of="(none)")
        audit = (await connection.execute(AUDIT_ROWS, {"b": paws})).all()
    assert outcomes == ["proposed", "slot_unavailable", "proposed", "proposed"]
    changed = {table for table in before if before[table] != after.get(table)}
    assert changed == {"tool_audit"}
    assert [(r.tool, r.kind, r.phase, r.outcome, r.action_id) for r in audit] == [
        ("book", "write", "propose", "proposed", None),
        ("book", "write", "propose", "slot_unavailable", None),
        ("reschedule", "write", "propose", "proposed", None),
        ("create_ticket", "write", "propose", "proposed", None),
    ]


async def test_audit_and_log_records_hold_no_argument_value(
    owner: AsyncEngine, database: Database, paws: str, caplog: pytest.LogCaptureFixture
) -> None:
    tools = tools_for(database, paws)
    context = context_for(paws, "cust-002")
    ticket = "Zebra-unique ticket text 7731"
    with caplog.at_level(logging.INFO, logger="vectapilot_api.tools"):
        await tools.run("order_status", {"order_id": "1043"}, context)
        await tools.run("check_slots", RANGE | {"service": "vaccination"}, context)
        await tools.propose("reschedule", {"booking_id": "B-102", "new_slot_id": "S-17"}, context)
        await tools.propose("create_ticket", {"category": "feedback", "summary": ticket}, context)
    async with owner.connect() as connection:
        audit = [row.whole for row in (await connection.execute(AUDIT_ROWS, {"b": paws})).all()]

    # Formatted as production formats them: the metadata lives in `extra`, which caplog.text
    # never shows.
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert len(audit) == 4
    assert rendered.count('"message": "tool_call"') == 4  # not vacuous
    for value in ("1043", "B-102", "S-17", "vaccination", "2026-11-29", ticket, "cust-002"):
        assert value not in rendered, value
        assert all(value not in row for row in audit), value


# ── Only an active booking counts ────────────────────────────────────────────────────────────

RETIRED_BOOKING_ID: Final = "B-900"
RETIRED_SLOT_ID: Final = "S-17"
"""A free slot of the Paws & Co seed: a retired booking of it must leave it free."""
EXECUTED_RESCHEDULE: Final = text(
    "INSERT INTO pending_actions (business_id, tool, arguments, customer_ref,"
    " confirm_token_sha256, status, expires_at, confirmed_at, confirm_trace_id, finished_at,"
    " result) VALUES (:b, 'reschedule', CAST('{}' AS jsonb), 'cust-001', repeat('a', 64),"
    " 'executed', now() + interval '10 minutes', now(), :trace, now(), CAST('{}' AS jsonb))"
    " RETURNING id"
)
RETIRED_BOOKING: Final = text(
    "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
    " retired_by_action_id) VALUES (:b, :id, :slot, 'cust-001', 'rescheduled', 'seed', :action)"
)


@pytest.fixture
async def retired(owner: AsyncEngine, paws: str) -> str:
    """cust-001's booking of `RETIRED_SLOT_ID`, retired by an executed reschedule (the owner
    writes both; the business's uproot removes them)."""
    async with business_transaction(owner, paws) as connection:
        action = await connection.scalar(EXECUTED_RESCHEDULE, {"b": paws, "trace": TRACE})
        await connection.execute(
            RETIRED_BOOKING,
            {"b": paws, "id": RETIRED_BOOKING_ID, "slot": RETIRED_SLOT_ID, "action": action},
        )
    return paws


async def test_a_retired_booking_leaves_its_slot_free_and_cannot_be_moved(
    database: Database, retired: str
) -> None:
    tools = tools_for(database, retired)
    context = context_for(retired, "cust-001")
    move = {"booking_id": RETIRED_BOOKING_ID, "new_slot_id": "S-09"}

    listed = await tools.run("check_slots", RANGE, context)
    booked = await tools.propose("book", {"slot_id": RETIRED_SLOT_ID}, context)
    moved = await tools.propose("reschedule", move, context)
    missing = await tools.propose("reschedule", move | {"booking_id": "B-999"}, context)

    assert RETIRED_SLOT_ID in slot_ids(listed)
    assert slot_ids(listed) == free_slots(PAWS, RANGE["date_from"], RANGE["date_to"])
    assert booked.proposal is not None, booked.observation
    assert moved.error == ToolError.of(ToolErrorCode.NOT_FOUND)
    assert moved.observation == missing.observation


# ── Only a booking that has not started can be moved ─────────────────────────────────────────

PAWS_NOW: Final = datetime.combine(PAWS.config.sandbox_date, time(0), PAWS.config.zone)
TIMED_SLOT: Final = text(
    "INSERT INTO sandbox_slots (business_id, id, service, starts_at, duration_minutes, label)"
    " VALUES (:b, :id, 'vet_consultation', :starts_at, 20, 'Vet room 1')"
)
ACTIVE_BOOKING: Final = text(
    "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin)"
    " VALUES (:b, :id, :slot, 'cust-001', 'active', 'seed')"
)
TIMED_BOOKINGS: Final = {
    "B-901": ("S-01", None),  # the seed's past slot (13 November): started days ago
    "B-902": ("S-90", PAWS_NOW - timedelta(minutes=10)),  # started, still in progress
    "B-903": ("S-91", PAWS_NOW),  # starts exactly at the sandbox clock's "now"
}
"""cust-001's extra active bookings, `booking id: (slot id, a new slot's start or None for a
seeded slot)`. B-101 (16 November, 10:00) is the future one."""


@pytest.fixture
async def timed(owner: AsyncEngine, paws: str) -> str:
    """Paws & Co with `TIMED_BOOKINGS` planted by the owner (the business's uproot removes them)."""
    async with business_transaction(owner, paws) as connection:
        for booking_id, (slot_id, starts_at) in TIMED_BOOKINGS.items():
            if starts_at is not None:
                await connection.execute(
                    TIMED_SLOT, {"b": paws, "id": slot_id, "starts_at": starts_at}
                )
            await connection.execute(ACTIVE_BOOKING, {"b": paws, "id": booking_id, "slot": slot_id})
    return paws


@pytest.mark.parametrize("booking_id", ["B-901", "B-902"], ids=["days ago", "in progress"])
async def test_a_started_booking_is_byte_identical_to_a_missing_one(
    owner: AsyncEngine, database: Database, timed: str, booking_id: str
) -> None:
    tools = tools_for(database, timed)
    context = context_for(timed, "cust-001")
    move = {"booking_id": booking_id, "new_slot_id": "S-09"}

    started = await tools.propose("reschedule", move, context)
    missing = await tools.propose("reschedule", move | {"booking_id": "B-999"}, context)

    assert started.error == missing.error == ToolError.of(ToolErrorCode.NOT_FOUND)
    assert started.observation == missing.observation
    assert started.outcome == missing.outcome
    assert started.business_ids == missing.business_ids == frozenset()  # no row was read
    assert booking_id not in started.observation
    async with owner.connect() as connection:
        audit = (await connection.execute(AUDIT_ROWS, {"b": timed})).all()
    assert [(r.tool, r.phase, r.outcome) for r in audit] == [
        ("reschedule", "propose", "not_found")
    ] * 2


@pytest.mark.parametrize(
    ("booking_id", "moved_from"),
    [
        ("B-903", "Monday 16 November 2026 at 00:00"),
        ("B-101", "Monday 16 November 2026 at 10:00"),
    ],
    ids=["starting exactly now", "future"],
)
async def test_a_booking_that_has_not_started_can_be_moved(
    database: Database, timed: str, booking_id: str, moved_from: str
) -> None:
    envelope = await tools_for(database, timed).propose(
        "reschedule", {"booking_id": booking_id, "new_slot_id": "S-09"}, context_for(timed)
    )

    assert envelope.proposal is not None, envelope.observation
    assert envelope.proposal.summary == (
        f"Move booking {booking_id} from Vet consultation on {moved_from} to"
        " Vet consultation on Monday 23 November 2026 at 10:00 (20 minutes, Asia/Kolkata),"
        " slot S-09."
    )


# ── Read-only snapshots and server timeouts ──────────────────────────────────────────────────

SMUGGLE: Final = text(
    "INSERT INTO tool_audit (business_id, tool, kind, phase, outcome, duration_ms)"
    " VALUES (:business_id, 'check_slots', 'read', 'run', 'smuggled', 0)"
)
SMUGGLED: Final = text(
    "SELECT count(*) FROM tool_audit WHERE business_id = :b AND outcome = 'smuggled'"
)


async def test_a_write_in_a_tools_snapshot_fails_with_25006(database: Database, paws: str) -> None:
    with pytest.raises(DBAPIError) as refused:
        async with tool_transaction(database, paws, 2.0, read_only=True) as connection:
            await connection.execute(SMUGGLE, {"business_id": paws})

    assert getattr(refused.value.orig, "sqlstate", None) == "25006"


async def test_a_read_tool_that_writes_is_unavailable_and_writes_nothing(
    owner: AsyncEngine, database: Database, paws: str
) -> None:
    async def smuggles(connection: AsyncConnection, arguments: Any, scope: ToolScope) -> Any:
        await connection.execute(SMUGGLE, {"business_id": scope.business_id})

    registry = {
        **REGISTRY,
        ToolName.CHECK_SLOTS: dataclasses.replace(check_slots.TOOL, run=smuggles),
    }
    envelope = await tools_for(database, paws, registry=registry).run(
        "check_slots", RANGE, context_for(paws)
    )

    assert envelope.error == ToolError.of(ToolErrorCode.UNAVAILABLE)
    async with business_transaction(owner, paws) as connection:
        assert await connection.scalar(SMUGGLED, {"b": paws}) == 0


async def test_a_slow_statement_hits_the_server_timeout(database: Database, paws: str) -> None:
    with pytest.raises(DBAPIError) as cancelled:
        async with tool_transaction(database, paws, 0.4, read_only=True) as connection:
            await connection.execute(text("SELECT pg_sleep(5)"))

    assert getattr(cancelled.value.orig, "sqlstate", None) == "57014"


async def test_a_slow_tool_is_ended_by_the_server_before_the_backstop(
    owner: AsyncEngine, database: Database, paws: str
) -> None:
    seen: list[str | None] = []

    async def sleeps(connection: AsyncConnection, arguments: Any, scope: ToolScope) -> Any:
        try:
            await connection.execute(text("SELECT pg_sleep(5)"))
        except DBAPIError as error:
            seen.append(getattr(error.orig, "sqlstate", None))
            raise

    slow = dataclasses.replace(check_slots.TOOL, run=sleeps, timeout_s=1.0)
    registry = {**REGISTRY, ToolName.CHECK_SLOTS: slow}
    async with asyncio.timeout(3.0):
        envelope = await tools_for(database, paws, registry=registry).run(
            "check_slots", RANGE, context_for(paws)
        )

    assert seen == ["57014"]  # the server's statement_timeout (750 ms), not asyncio's 1 s
    assert envelope.error == ToolError.of(ToolErrorCode.TIMEOUT)
    async with owner.connect() as connection:
        outcomes = [row.outcome for row in (await connection.execute(AUDIT_ROWS, {"b": paws}))]
    assert outcomes == ["timeout"]
