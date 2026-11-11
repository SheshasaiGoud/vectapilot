"""The MCP server: one business's enabled tools, for one customer (ADR 0016 decision 8).

Built on the SDK's low-level `Server`, not its decorator API (`MCPServer`): the decorators derive
a schema from a Python function's signature, while our schemas already exist as the business's
Pydantic input models (`ToolExecutor.input_model`), closed and bounded, with the business's own
service and category enums. One source of truth, so the agent and an MCP client see the same
schema.

- **`tools/list`** lists exactly the business's enabled tools, sorted (`ToolExecutor.enabled`),
  each with its input model's JSON schema and a fixed description. Built once: the configuration
  does not change while the process runs.
- **`tools/call`** builds the `ToolContext` from the process's flags (W7) and a fresh trace id,
  then dispatches by the registry's kind: a read tool runs, a write tool is proposed, and any
  other name goes to `run`, which answers `not_enabled` without repeating it. The client's tool
  name and arguments are caller text: they choose a tool and its arguments, never the business or
  the customer.
- **The result is the observation** (`ToolEnvelope.observation`): the typed result or typed error
  as compact JSON, one text block, the same text the agent's model reads. Tool output stays data:
  free text from the sandbox (an order note, a slot label) is a JSON string value inside it, never
  a separate block, a prompt or an instruction (rule 5, W10). The envelope's business ids are for
  the eval's leak check and never leave.
- **A proposal is not confirmable here.** Its result is the summary with `confirmation_required`
  plus one fixed sentence saying nothing was stored. There is no action id and no token, and no
  pending action exists (the executor's `propose` writes only its audit row).
- **Failures are typed, never echoed.** A typed tool error is an `is_error` result with the error's
  fixed message. A bug in the tool core (any other exception) becomes the fixed `unavailable`
  error, logged with its type only (the executor has already left its `tool_call` record); a
  cancellation propagates.
"""

import logging
import secrets
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final

import mcp.types as types
from mcp.server import Server, ServerRequestContext

from vectapilot_api.tools.check_slots import MAX_RANGE_DAYS
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import ToolExecutor, platform_name
from vectapilot_api.tools.types import (
    ToolContext,
    ToolEnvelope,
    ToolError,
    ToolErrorCode,
    ToolKind,
    render,
)

SERVER_NAME: Final = "vectapilot-tools"
SERVER_VERSION: Final = "0.1.0"

DESCRIPTIONS: Final[Mapping[ToolName, str]] = MappingProxyType(
    {
        ToolName.BOOK: (
            "Propose booking one free slot (by a slot id check_slots listed) for this customer."
            " Returns the summary the customer would confirm. Nothing is booked: confirming is"
            " not available through MCP."
        ),
        ToolName.CHECK_SLOTS: (
            "List the business's free appointment slots between two local dates (at most"
            f" {MAX_RANGE_DAYS} days in all), optionally for one service."
        ),
        ToolName.CREATE_TICKET: (
            "Propose sending the customer's message to the business's staff, in one category."
            " Returns the summary the customer would confirm. Nothing is sent: confirming is not"
            " available through MCP."
        ),
        ToolName.ORDER_STATUS: "Look up one of this customer's orders by its order id.",
        ToolName.RESCHEDULE: (
            "Propose moving one of this customer's bookings to another free slot. Returns the"
            " summary the customer would confirm. Nothing is moved: confirming is not available"
            " through MCP."
        ),
    }
)
"""One fixed description per platform tool (a test checks every `ToolName` has one). Not the
agent's `TOOL_DESCRIPTIONS`: those are pinned by the agent prompt's fingerprint, and they say the
customer confirms elsewhere, which over MCP is not true in M5."""

NOT_CONFIRMABLE: Final = (
    "Nothing was stored, booked or sent. Confirming is not available through MCP: this is only"
    " the summary of what the customer would confirm."
)
"""The second text block of every proposal result (fixed text)."""

TOOL_ANNOTATIONS: Final = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
"""Hints for the client, and true for every tool here: reads only read, and a proposal over MCP
validates without writing anything but the server's own audit row. The sandbox is closed (no
outside system), hence `open_world_hint=False`. Hosts often run read-only tools without asking the
user, so if MCP ever gains confirm (M8 or later), the write tools must be listed with
`read_only_hint=False` (tests/test_server.py pins these hints)."""

logger = logging.getLogger("vectapilot_mcp")


def new_trace_id() -> str:
    """A fresh trace id per call (32 lowercase hex, the gateway's `TraceId`), so each call's
    audit row and `tool_call` record can be found on their own."""
    return secrets.token_hex(16)


def listed_tool(executor: ToolExecutor, name: ToolName) -> types.Tool:
    """`name` as MCP lists it: its business input model's JSON schema, never a context field."""
    return types.Tool(
        name=name.value,
        description=DESCRIPTIONS[name],
        input_schema=executor.input_model(name).model_json_schema(),
        annotations=TOOL_ANNOTATIONS,
    )


def call_result(envelope: ToolEnvelope) -> types.CallToolResult:
    """The MCP result of one call: the observation only (and, for a proposal, the fixed note)."""
    content: list[types.ContentBlock] = [types.TextContent(text=envelope.observation)]
    if envelope.proposal is not None:
        content.append(types.TextContent(text=NOT_CONFIRMABLE))
    return types.CallToolResult(content=content, is_error=envelope.error is not None)


def unavailable_result() -> types.CallToolResult:
    """The fixed result for a bug in the tool core: `unavailable`, nothing of the error."""
    error = ToolError.of(ToolErrorCode.UNAVAILABLE)
    return types.CallToolResult(
        content=[types.TextContent(text=render(None, error))], is_error=True
    )


class ToolServer:
    """The two handlers, bound to one business's executor and one customer."""

    def __init__(
        self,
        executor: ToolExecutor,
        customer_ref: str,
        *,
        trace_ids: Callable[[], str] = new_trace_id,
    ) -> None:
        # Validates the customer reference now (and the business against the executor's), so a
        # bad flag stops startup instead of failing every call.
        ToolContext(business_id=executor.business_id, customer_ref=customer_ref)
        self._executor = executor
        self._customer_ref = customer_ref
        self._trace_ids = trace_ids
        self._tools = tuple(listed_tool(executor, name) for name in executor.enabled)

    @property
    def tools(self) -> tuple[types.Tool, ...]:
        return self._tools

    def context(self) -> ToolContext:
        """The injected context of one call (W7): the flags, never the client's arguments."""
        return ToolContext(
            business_id=self._executor.business_id,
            customer_ref=self._customer_ref,
            trace_id=self._trace_ids(),
        )

    async def list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=list(self._tools))

    async def call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await self.call(params.name, params.arguments or {})

    async def call(self, tool: str, arguments: Mapping[str, object]) -> types.CallToolResult:
        """Run a read tool or propose a write tool; anything else is the executor's
        `not_enabled`. Never executes a write (W1)."""
        name = platform_name(tool)
        context = self.context()
        try:
            if name is not None and self._executor.kind(name) is ToolKind.WRITE:
                envelope = await self._executor.propose(tool, arguments, context)
            else:
                envelope = await self._executor.run(tool, arguments, context)
        except Exception as error:
            logger.warning(
                "mcp_tool_failed",
                extra={"business_id": self._executor.business_id, "reason": type(error).__name__},
            )
            return unavailable_result()
        return call_result(envelope)


def build_server(
    executor: ToolExecutor,
    customer_ref: str,
    *,
    trace_ids: Callable[[], str] = new_trace_id,
) -> Server[Any]:
    """The MCP server for `executor`'s business and `customer_ref`'s customer: tools only."""
    tools = ToolServer(executor, customer_ref, trace_ids=trace_ids)
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=tools.list_tools,
        on_call_tool=tools.call_tool,
    )
