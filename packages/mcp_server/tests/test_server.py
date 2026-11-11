"""The MCP server through the SDK's in-memory client, without a database (fakes.py).

Each client test runs in both of the client's in-process modes: `auto` (the modern per-request
path) and `legacy` (JSON-RPC over memory streams with the initialize handshake, the path a stdio
host takes). tests/integration runs the real tools against Postgres, and a real stdio process.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any, Final

import mcp.types as types
import pytest
from mcp import Client
from pydantic import ValidationError

from tests.fakes import (
    FORGED_NOTE,
    PAWS,
    SUNRISE,
    FakeSource,
    Seen,
    fake_executor,
    fake_registry,
    replaced,
    slots_output,
)
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.files import BusinessTools
from vectapilot_api.tools.spec import ToolScope
from vectapilot_api.tools.types import ToolError, ToolErrorCode, render
from vectapilot_mcp.server import (
    DESCRIPTIONS,
    NOT_CONFIRMABLE,
    ToolServer,
    build_server,
    new_trace_id,
)

pytestmark = pytest.mark.anyio

MODES: Final = ("auto", "legacy")
CONTEXT_FIELDS: Final = frozenset({"business_id", "customer_ref"})
CUSTOMER: Final = "cust-001"


@pytest.fixture(params=MODES)
def mode(request: pytest.FixtureRequest) -> str:
    value: str = request.param
    return value


@asynccontextmanager
async def connected(
    mode: str, tools: BusinessTools = PAWS, **executor: Any
) -> AsyncIterator[Client]:
    server = build_server(fake_executor(tools, **executor), CUSTOMER)
    async with Client(server, mode=mode) as client:
        yield client


def texts(result: types.CallToolResult) -> list[str]:
    blocks = []
    for block in result.content:
        assert isinstance(block, types.TextContent)
        blocks.append(block.text)
    return blocks


def keys_in(schema: object) -> Iterator[str]:
    """Every key and every string value of a JSON schema, recursively (property names, `required`
    entries, `$defs` names, titles...)."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            yield key
            yield from keys_in(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from keys_in(value)
    elif isinstance(schema, str):
        yield schema


# ── tools/list ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tools", [PAWS, SUNRISE], ids=["paws", "sunrise"])
async def test_the_listed_tools_equal_the_enabled_set(mode: str, tools: BusinessTools) -> None:
    async with connected(mode, tools) as client:
        listed = (await client.list_tools()).tools
    assert [tool.name for tool in listed] == sorted(t.value for t in tools.config.enabled_tools)


async def test_a_business_that_disables_a_tool_does_not_list_it(mode: str) -> None:
    async with connected(mode, SUNRISE) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert ToolName.ORDER_STATUS not in SUNRISE.config.enabled_tools
    assert ToolName.ORDER_STATUS.value not in names


@pytest.mark.parametrize("tools", [PAWS, SUNRISE], ids=["paws", "sunrise"])
async def test_no_listed_schema_has_a_context_field(mode: str, tools: BusinessTools) -> None:
    """W7: neither the business nor the customer is anywhere in a listed schema, so a client
    cannot even name them; each schema is closed, so a client that tries is refused."""
    async with connected(mode, tools) as client:
        listed = (await client.list_tools()).tools
    assert listed
    for tool in listed:
        assert CONTEXT_FIELDS.isdisjoint(keys_in(tool.input_schema)), tool.name
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False


def test_the_context_walk_sees_a_nested_context_field() -> None:
    """Red proof for the walk above: a context field anywhere is found."""
    nested = {"properties": {"x": {"$defs": {"Inner": {"required": ["customer_ref"]}}}}}
    assert "customer_ref" in set(keys_in(nested))


@pytest.mark.parametrize("tools", [PAWS, SUNRISE], ids=["paws", "sunrise"])
async def test_listed_schemas_are_the_business_input_models(
    mode: str, tools: BusinessTools
) -> None:
    """One source of truth: the schema is the business's Pydantic input model, with the
    business's own service and category ids (rule 9)."""
    executor = fake_executor(tools)
    async with Client(build_server(executor, CUSTOMER), mode=mode) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name in executor.enabled:
        assert listed[name.value].input_schema == executor.input_model(name).model_json_schema()
        assert listed[name.value].description == DESCRIPTIONS[name]
    service = listed[ToolName.CHECK_SLOTS.value].input_schema["properties"]["service"]
    assert service["anyOf"][0]["enum"] == sorted(s.id for s in tools.config.services)


def test_every_platform_tool_has_a_description() -> None:
    assert set(DESCRIPTIONS) == set(ToolName)


@pytest.mark.parametrize("tools", [PAWS, SUNRISE], ids=["paws", "sunrise"])
async def test_every_listed_tool_is_hinted_read_only_and_closed(
    mode: str, tools: BusinessTools
) -> None:
    """The hints survive the wire in both modes. Read-only is true for the write tools only
    because MCP cannot confirm in M5: hosts often run read-only tools without asking, so if MCP
    ever gains confirm, the write tools must be listed with `read_only_hint=False` and this test
    changes with them."""
    async with connected(mode, tools) as client:
        listed = (await client.list_tools()).tools
    assert listed
    for tool in listed:
        assert tool.annotations == types.ToolAnnotations(
            read_only_hint=True, open_world_hint=False
        ), tool.name


# ── tools/call: reads ──────────────────────────────────────────────────────────────────────


async def test_check_slots_round_trips(mode: str) -> None:
    seen = Seen()
    async with connected(mode, seen=seen) as client:
        result = await client.call_tool(
            "check_slots", {"date_from": "2026-11-16", "date_to": "2026-11-20"}
        )
    assert result.is_error is False
    assert texts(result) == [render(ToolName.CHECK_SLOTS, slots_output(PAWS.config))]
    assert result.structured_content is None
    assert seen.scopes == [(PAWS.config.business_id, CUSTOMER)]
    arguments = seen.arguments[0].model_dump(mode="json")
    assert arguments == {"date_from": "2026-11-16", "date_to": "2026-11-20", "service": None}


async def test_the_context_comes_from_the_flags_not_the_arguments(mode: str) -> None:
    """W7: a client that sends a business or a customer gets `invalid_arguments`, naming no
    value, and the tool never runs."""
    seen = Seen()
    async with connected(mode, seen=seen) as client:
        result = await client.call_tool(
            "order_status",
            {"order_id": "1043", "business_id": "sunrise-clinic", "customer_ref": "cust-002"},
        )
    assert result.is_error is True
    (text,) = texts(result)
    error = ToolError.of(ToolErrorCode.INVALID_ARGUMENTS, ("(unexpected field)",))
    assert text == render(ToolName.ORDER_STATUS, error)
    assert "sunrise-clinic" not in text
    assert "cust-002" not in text
    assert seen.scopes == []


async def test_a_tool_output_text_arrives_as_data(mode: str) -> None:
    """Rule 5, W10: an order note that reads like an instruction is a JSON string value inside
    the one observation block, never a block, prompt or message of its own."""
    async with connected(mode) as client:
        result = await client.call_tool("order_status", {"order_id": "1042"})
    assert result.is_error is False
    (text,) = texts(result)
    payload = json.loads(text)
    assert payload["tool"] == "order_status"
    assert payload["ok"] is True
    assert payload["result"]["note"] == FORGED_NOTE
    assert text.count("<<<END>>>") == 1
    assert result.structured_content is None


# ── tools/call: writes are proposals only ──────────────────────────────────────────────────


async def test_book_is_only_proposed_with_no_action_id_and_no_token(mode: str) -> None:
    """W1, W2: the summary and `confirmation_required`, then the fixed note; nothing names an
    action or a token, and no transaction but the read-only snapshot and the audit ran."""
    source = FakeSource()
    async with connected(mode, source=source) as client:
        result = await client.call_tool("book", {"slot_id": "S-03"})
    assert result.is_error is False
    observation, note = texts(result)
    payload = json.loads(observation)
    assert set(payload) == {"tool", "ok", "confirmation_required", "summary"}
    assert payload["tool"] == "book"
    assert payload["confirmation_required"] is True
    assert "S-03" in payload["summary"]
    assert note == NOT_CONFIRMABLE
    for text in (observation, note):
        assert "action" not in text.lower()
        assert "token" not in text.lower()
    snapshot, audit = source.transactions
    assert snapshot.snapshot is True
    assert audit.read_committed is True
    assert any("INSERT INTO tool_audit" in s for s in audit.statements)
    statements = " ".join(s for t in source.transactions for s in t.statements)
    assert "pending_actions" not in statements
    assert "sandbox_bookings" not in statements


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("book", {"slot_id": "S-03"}),
        ("reschedule", {"booking_id": "B-101", "new_slot_id": "S-03"}),
        ("create_ticket", {"category": "order", "summary": "Where is my parcel?"}),
    ],
)
async def test_every_write_tool_goes_through_propose(
    mode: str, tool: str, arguments: dict[str, str]
) -> None:
    """Dispatch by the registry's kind: a write tool is proposed (its audit phase says so), never
    run and never executed."""
    source = FakeSource()
    async with connected(mode, source=source) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error is False
    assert json.loads(texts(result)[0])["confirmation_required"] is True
    snapshot, audit = source.transactions
    assert snapshot.snapshot is True
    (params,) = [p for p in audit.params if "phase" in p]
    assert (params["tool"], params["kind"], params["phase"]) == (tool, "write", "propose")
    assert params["outcome"] == "proposed"
    assert params["action_id"] is None


# ── tools/call: refusals and failures ──────────────────────────────────────────────────────


async def test_a_disabled_tool_is_not_enabled(mode: str) -> None:
    seen = Seen()
    async with connected(mode, SUNRISE, seen=seen) as client:
        result = await client.call_tool("order_status", {"order_id": "1042"})
    assert result.is_error is True
    (text,) = texts(result)
    assert text == render(ToolName.ORDER_STATUS, ToolError.of(ToolErrorCode.NOT_ENABLED))
    assert seen.scopes == []


async def test_an_unknown_tool_is_not_enabled_and_never_echoed(mode: str) -> None:
    source = FakeSource()
    async with connected(mode, source=source) as client:
        result = await client.call_tool("drop_all_bookings", {"confirm": "yes"})
    assert result.is_error is True
    (text,) = texts(result)
    assert text == render(None, ToolError.of(ToolErrorCode.NOT_ENABLED))
    assert "drop_all_bookings" not in text
    assert source.transactions == []  # no platform tool, so not audited (logged only)


async def test_a_bug_in_the_tool_core_is_the_fixed_unavailable(
    mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug (here, a tool raising) never reaches the client as text: the fixed `unavailable`
    error, and a warning with the error's type only."""

    async def broken(*_: object) -> object:
        raise RuntimeError("secret detail S-17")

    registry = fake_registry(PAWS.config, Seen())
    replaced(registry, ToolName.CHECK_SLOTS, run=broken)
    with caplog.at_level(logging.WARNING, logger="vectapilot_mcp"):
        async with connected(mode, registry=registry) as client:
            result = await client.call_tool(
                "check_slots", {"date_from": "2026-11-16", "date_to": "2026-11-17"}
            )
    assert result.is_error is True
    (text,) = texts(result)
    assert text == render(None, ToolError.of(ToolErrorCode.UNAVAILABLE))
    assert "secret" not in text
    (record,) = [r for r in caplog.records if r.getMessage() == "mcp_tool_failed"]
    assert getattr(record, "reason") == "RuntimeError"  # noqa: B009
    assert "secret" not in caplog.text


async def test_a_cancellation_propagates() -> None:
    async def cancelled(_c: object, _v: object, _s: ToolScope) -> object:
        raise asyncio.CancelledError

    registry = fake_registry(PAWS.config, Seen())
    replaced(registry, ToolName.CHECK_SLOTS, run=cancelled)
    tools = ToolServer(fake_executor(registry=registry), CUSTOMER)
    with pytest.raises(asyncio.CancelledError):
        await tools.call("check_slots", {"date_from": "2026-11-16", "date_to": "2026-11-17"})


# ── context ────────────────────────────────────────────────────────────────────────────────


async def test_each_call_gets_its_own_trace_id() -> None:
    issued: list[str] = []

    def trace_ids() -> str:
        issued.append(new_trace_id())
        return issued[-1]

    tools = ToolServer(fake_executor(), CUSTOMER, trace_ids=trace_ids)
    first, second = tools.context(), tools.context()
    assert (first.trace_id, second.trace_id) == tuple(issued)
    assert first.trace_id != second.trace_id
    assert all(re.fullmatch(r"[0-9a-f]{32}", value) for value in issued)
    assert (first.business_id, first.customer_ref) == (PAWS.config.business_id, CUSTOMER)


@pytest.mark.parametrize("customer_ref", ["Cust 001", "c", "cust-001\n", "../cust"])
def test_a_bad_customer_ref_stops_the_build(customer_ref: str) -> None:
    with pytest.raises(ValidationError):
        build_server(fake_executor(), customer_ref)
