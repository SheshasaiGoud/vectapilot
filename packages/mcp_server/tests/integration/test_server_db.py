"""The MCP server with the real tools against Postgres, through the SDK's in-memory client.

Each test seeds throwaway businesses through the real reset (as the owner), serves one of them
as `vectapilot_app` (RLS on), and reads the outcome from outside the server, as the owner: the
audit rows, and fingerprints of every write table.
"""

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import mcp.types as types
import pytest
from mcp import Client
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fakes import PAWS
from tests.integration.db import (
    BAIT_BOOKING,
    BAIT_ORDER,
    BAIT_SLOT,
    MISSING_BOOKING,
    MISSING_ORDER,
    MISSING_SLOT,
    app_url,
    audit_rows,
    config_for,
    fresh_engine,
    integration,
    new_business,
    planted_seed,
    seed_for,
    seeded,
    table_prints,
)
from vectapilot_api.db import Database, create_api_engine
from vectapilot_api.tools.config import ToolName, ToolsConfig
from vectapilot_api.tools.executor import ToolExecutor
from vectapilot_mcp.server import NOT_CONFIRMABLE, build_server

pytestmark = [pytest.mark.anyio, integration]

FREE_FUTURE_SLOT: Final = "S-03"
"""Free in the Paws & Co seed, and on the sandbox date itself (not started by its clock)."""
WEEK: Final = {"date_from": "2026-11-16", "date_to": "2026-11-29"}
TRACE: Final = re.compile(r"[0-9a-f]{32}")


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    from tests.integration.db import owner_url

    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def database() -> AsyncIterator[Database]:
    database = Database(create_api_engine(app_url()))
    yield database
    await database.engine.dispose()


@asynccontextmanager
async def connected(
    database: Database, config: ToolsConfig, customer_ref: str = "cust-001"
) -> AsyncIterator[Client]:
    """The server for `config`'s business and `customer_ref`, as the CLI builds it."""
    server = build_server(ToolExecutor(database, config), customer_ref)
    async with Client(server) as client:
        yield client


def texts(result: types.CallToolResult) -> list[str]:
    blocks = []
    for block in result.content:
        assert isinstance(block, types.TextContent)
        blocks.append(block.text)
    return blocks


async def test_check_slots_round_trips(owner: AsyncEngine, database: Database) -> None:
    a = new_business("a")
    async with seeded(owner, seed_for(a)), connected(database, config_for(a)) as client:
        result = await client.call_tool(ToolName.CHECK_SLOTS.value, WEEK)
        audit = await audit_rows(owner, a)
    assert result.is_error is False
    (text,) = texts(result)
    slots = json.loads(text)["result"]["slots"]
    listed = {slot["slot_id"] for slot in slots}
    booked = {booking.slot_id for booking in PAWS.seed.bookings}
    assert FREE_FUTURE_SLOT in listed
    assert listed <= {slot.id for slot in PAWS.seed.slots} - booked
    assert [(r["tool"], r["kind"], r["phase"], r["outcome"]) for r in audit] == [
        ("check_slots", "read", "run", "ok")
    ]
    assert TRACE.fullmatch(str(audit[0]["trace_id"]))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("book", {"slot_id": FREE_FUTURE_SLOT}),
        ("reschedule", {"booking_id": "B-101", "new_slot_id": FREE_FUTURE_SLOT}),
        ("create_ticket", {"category": "order", "summary": "Where is my parcel?"}),
    ],
)
async def test_a_write_creates_no_pending_action_and_no_write(
    owner: AsyncEngine, database: Database, tool: str, arguments: dict[str, str]
) -> None:
    """W1, W2: the summary and `confirmation_required`, nothing that could confirm it, and every
    write table (pending actions included) byte-for-byte as before; only the audit row is new."""
    a = new_business("a")
    async with seeded(owner, seed_for(a)):
        before = await table_prints(owner, a)
        async with connected(database, config_for(a)) as client:
            result = await client.call_tool(tool, arguments)
        after = await table_prints(owner, a)
        audit = await audit_rows(owner, a)
    assert result.is_error is False
    observation, note = texts(result)
    payload = json.loads(observation)
    assert set(payload) == {"tool", "ok", "confirmation_required", "summary"}
    assert payload["confirmation_required"] is True
    assert note == NOT_CONFIRMABLE
    assert "action" not in observation.lower()
    assert "token" not in observation.lower()
    assert after == before
    assert before["pending_actions"][0] == 0
    assert [(r["tool"], r["phase"], r["outcome"], r["action_id"]) for r in audit] == [
        (tool, "propose", "proposed", None)
    ]


async def test_an_order_note_arrives_as_data(owner: AsyncEngine, database: Database) -> None:
    """Rule 5, W10: the seeded note with a forged boundary and a "call book(...)" instruction is
    one JSON string value in the one observation block, and nothing is proposed or written."""
    a = new_business("a")
    (seeded_order,) = [order for order in PAWS.seed.orders if order.id == "1042"]
    assert seeded_order.note is not None
    assert "<<<END>>>" in seeded_order.note
    async with seeded(owner, seed_for(a)):
        before = await table_prints(owner, a)
        async with connected(database, config_for(a)) as client:
            result = await client.call_tool(ToolName.ORDER_STATUS.value, {"order_id": "1042"})
        after = await table_prints(owner, a)
    assert result.is_error is False
    (text,) = texts(result)
    assert json.loads(text)["result"]["note"] == seeded_order.note
    assert after == before


async def test_a_disabled_tool_is_not_listed_and_not_enabled(
    owner: AsyncEngine, database: Database
) -> None:
    a = new_business("a")
    enabled = [t.value for t in PAWS.config.enabled_tools if t is not ToolName.ORDER_STATUS]
    config = config_for(a, enabled_tools=enabled)
    async with seeded(owner, seed_for(a)), connected(database, config) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        result = await client.call_tool(ToolName.ORDER_STATUS.value, {"order_id": "1042"})
        audit = await audit_rows(owner, a)
    assert listed == set(enabled)
    assert result.is_error is True
    assert json.loads(texts(result)[0])["error"]["code"] == "not_enabled"
    assert [(r["tool"], r["outcome"]) for r in audit] == [("order_status", "not_enabled")]


async def test_started_for_b_a_planted_rows_are_invisible(
    owner: AsyncEngine, database: Database
) -> None:
    """ADR 0004, W7: started for B, A's planted slot, booking and order do not exist through
    `check_slots`, `order_status` or a proposal: each answers byte-for-byte as an id no one has.
    The positive control first: started for A, the same ids are there."""
    a, b = new_business("a"), new_business("b")
    seed_a = planted_seed(a)
    async with seeded(owner, seed_a, seed_for(b)):
        async with connected(database, config_for(a)) as as_a:
            own_slots = texts(await as_a.call_tool(ToolName.CHECK_SLOTS.value, WEEK))[0]
            own_order = await as_a.call_tool(ToolName.ORDER_STATUS.value, {"order_id": BAIT_ORDER})
            own_move = await as_a.call_tool(
                ToolName.RESCHEDULE.value,
                {"booking_id": BAIT_BOOKING, "new_slot_id": FREE_FUTURE_SLOT},
            )
            own_book = await as_a.call_tool(ToolName.BOOK.value, {"slot_id": BAIT_SLOT})
        a_before = await table_prints(owner, a)
        a_audit_before = len(await audit_rows(owner, a))
        async with connected(database, config_for(b)) as as_b:
            slots = texts(await as_b.call_tool(ToolName.CHECK_SLOTS.value, WEEK))[0]
            pairs = [
                (ToolName.ORDER_STATUS, {"order_id": BAIT_ORDER}, {"order_id": MISSING_ORDER}),
                (
                    ToolName.RESCHEDULE,
                    {"booking_id": BAIT_BOOKING, "new_slot_id": FREE_FUTURE_SLOT},
                    {"booking_id": MISSING_BOOKING, "new_slot_id": FREE_FUTURE_SLOT},
                ),
                (ToolName.BOOK, {"slot_id": BAIT_SLOT}, {"slot_id": MISSING_SLOT}),
            ]
            answers = [
                (
                    await as_b.call_tool(tool.value, bait),
                    await as_b.call_tool(tool.value, missing),
                )
                for tool, bait, missing in pairs
            ]
        a_after = await table_prints(owner, a)
        a_audit_after = len(await audit_rows(owner, a))

    assert BAIT_SLOT in own_slots
    assert seed_a.canary in own_slots
    assert own_order.is_error is False
    assert seed_a.canary in texts(own_order)[0]
    # The bait booking and slot are valid for A, so B's not_found below is isolation, not a
    # broken plant (both are proposals: nothing of A's changes).
    for own in (own_move, own_book):
        assert own.is_error is False
        assert json.loads(texts(own)[0])["confirmation_required"] is True

    assert BAIT_SLOT not in slots
    assert seed_a.canary not in slots
    for bait, missing in answers:
        assert bait.is_error is True
        assert texts(bait) == texts(missing)
        assert seed_a.canary not in texts(bait)[0]
    assert a_after == a_before
    assert a_audit_after == a_audit_before  # B's session left no audit row under A


async def test_another_customer_of_the_same_business_sees_nothing(
    owner: AsyncEngine, database: Database
) -> None:
    """ADR 0016 decision 9: `customer_ref` scopes orders and bookings within one business."""
    a = new_business("a")
    async with seeded(owner, planted_seed(a)), connected(database, config_for(a), "cust-002") as c:
        bait = await c.call_tool(ToolName.ORDER_STATUS.value, {"order_id": BAIT_ORDER})
        missing = await c.call_tool(ToolName.ORDER_STATUS.value, {"order_id": MISSING_ORDER})
        moved = await c.call_tool(
            ToolName.RESCHEDULE.value,
            {"booking_id": BAIT_BOOKING, "new_slot_id": FREE_FUTURE_SLOT},
        )
        nothing = await c.call_tool(
            ToolName.RESCHEDULE.value,
            {"booking_id": MISSING_BOOKING, "new_slot_id": FREE_FUTURE_SLOT},
        )
    assert texts(bait) == texts(missing)
    assert texts(moved) == texts(nothing)
    assert json.loads(texts(bait)[0])["error"]["code"] == "not_found"
