"""G10's log canary over MCP `tools/call` (M7 PR 6). Unit, no database.

The API's canary (apps/api/tests/guards/test_log_canary.py) covers every HTTP route. The MCP
server is another caller of the same tool core with its own process and its own logging: the
CLI's `configure_logging` writes plain `JsonFormatter` lines (it never loads the guards, so no
`RedactingJsonFormatter`), the SDK's loggers at INFO or above. So nothing it logs may carry a
caller's text in the first place. A synthetic canary goes through `tools/call` in the paths a
caller controls: arguments that fail validation, a write proposed with free text, a tool name
that is not a platform tool, and a tool core that raises with the canary in its exception's
message. Every record any logger writes is formatted as the CLI formats it, `exc_info`
included, and no part of the canary may appear.

The canary is synthetic: the phone number is not dialable (+91 numbers never start with 0) and
the e-mail is under `example` (RFC 2606).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

import pytest
from mcp import Client

from tests.fakes import PAWS, Seen, fake_executor, fake_registry, replaced
from vectapilot_api.log import JsonFormatter
from vectapilot_api.tools.config import ToolName
from vectapilot_mcp.cli import SDK_LOGGER
from vectapilot_mcp.server import build_server

pytestmark = pytest.mark.anyio

WORD: Final = "zqxmcpcanary"
PHONE: Final = "+91 00000 13579"
EMAIL: Final = "mcp.canary@log.example"
PARTS: Final = (WORD, "13579", "00000 13579", "mcp.canary", "log.example")
CANARY: Final = f"{WORD} {PHONE} {EMAIL}"
MODES: Final = ("auto", "legacy")


class Lines(logging.Handler):
    """Every record any logger writes, formatted as the CLI's handler formats it."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        self.lines.append(self.format(record))


@contextmanager
def logged() -> Iterator[Lines]:
    """The CLI's levels (root INFO, the SDK at INFO or above), with `Lines` on the root."""
    root, sdk = logging.getLogger(), logging.getLogger(SDK_LOGGER)
    before = (root.level, sdk.level)
    handler = Lines()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sdk.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(before[0])
        sdk.setLevel(before[1])


def leaked(lines: list[str]) -> list[str]:
    return [part for line in lines for part in PARTS if part in line]


@pytest.mark.parametrize("mode", MODES)
async def test_no_line_holds_a_canary_sent_through_tools_call(mode: str) -> None:
    async def broken(*_: object) -> object:
        raise RuntimeError(f"tool core failed on {CANARY}")

    registry = fake_registry(PAWS.config, Seen())
    replaced(registry, ToolName.CHECK_SLOTS, run=broken)
    server = build_server(fake_executor(PAWS, registry=registry), "cust-001")
    with logged() as captured:
        async with Client(server, mode=mode) as client:
            # Arguments that fail validation (the order id's pattern, an unknown field).
            await client.call_tool("order_status", {"order_id": CANARY, "note": CANARY})
            # A write proposed with free text (the slot id's pattern refuses it: still logged).
            await client.call_tool("book", {"slot_id": CANARY})
            # A tool name that is no platform tool, with the canary in name and arguments.
            await client.call_tool(f"{WORD}_tool", {"free": CANARY})
            # A bug in the tool core whose exception message holds the canary.
            await client.call_tool(
                "check_slots", {"date_from": "2026-11-16", "date_to": "2026-11-17"}
            )
            # A clean read, so the success path's records are in the list too.
            await client.call_tool("order_status", {"order_id": "1041"})

    # The positive controls: the paths ran and wrote their records.
    assert "mcp_tool_failed" in captured.messages
    assert sum(message == "tool_call" for message in captured.messages) >= 4
    assert leaked(captured.lines) == []


def test_the_detector_finds_a_planted_line() -> None:
    """The red proof: the same formatter and search find the canary when a line holds it."""
    with logged() as captured:
        logging.getLogger("vectapilot_mcp").warning("planted %s", CANARY)
    assert set(leaked(captured.lines)) == set(PARTS)
