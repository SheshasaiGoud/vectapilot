"""One business's `AgentStep` schema (decided D2 (a); ADR 0016 decision 4; M5 PR 4).

`build_agent_step(tools)` returns a Pydantic model whose `step` field is a discriminated union:
- one `CallTool` variant per **enabled** tool, sorted by name, each with `tool` narrowed to that
  name and `arguments` to the business's input model (`ToolExecutor.input_model`: the business's
  services and ticket categories as closed enums, rule 9);
- `Reply` and `Handoff`.

**Why sorted, and why one class name.** The gateway quotes the schema into the system prompt and
hashes it, with the model's module and qualified name, into the cache key. The same enabled set
must give the same schema text byte for byte, whatever order `tools.json` lists the tools in, so
the variants are sorted (`ToolExecutor.enabled` already is), and the model is always called
`AgentStep` in this module: two businesses differ only by what their configuration differs in.

**The model only sees what the business enables.** A disabled tool has no variant, so naming it is
a schema error (the gateway's one repair, then `invalid_model_output`), never a call. No variant
has a `business_id` or `customer_ref` field (W7): the input models have none, and every model
here forbids unknown fields.

`TOOL_DESCRIPTIONS` are each variant's docstring: prompt text, pinned by the prompt's fingerprint.
They say which tools only read and which only propose, since that is the difference the model must
get right (a write is never done here, W1).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final, Literal, Protocol, Union, cast

from pydantic import BaseModel, Field, create_model

from vectapilot_api.agent.types import AgentStep, CallTool, Handoff, Reply
from vectapilot_api.tools.check_slots import MAX_RANGE_DAYS
from vectapilot_api.tools.config import ToolName

TOOL_DESCRIPTIONS: Final[Mapping[ToolName, str]] = MappingProxyType(
    {
        ToolName.BOOK: (
            "Write: propose booking one free slot, by the slot id check_slots listed, for this"
            " customer. It is only proposed: the customer confirms it outside this conversation."
        ),
        ToolName.CHECK_SLOTS: (
            "Read: list the business's free appointment slots between two local dates (at most"
            f" {MAX_RANGE_DAYS} days in all), optionally for one service."
        ),
        ToolName.CREATE_TICKET: (
            "Write: propose sending the customer's message to the business's staff, in one"
            " category. It is only proposed: the customer confirms it outside this conversation."
        ),
        ToolName.ORDER_STATUS: "Read: look up one of this customer's orders by its order id.",
        ToolName.RESCHEDULE: (
            "Write: propose moving one of this customer's bookings to another free slot. It is"
            " only proposed: the customer confirms it outside this conversation."
        ),
    }
)
"""One sentence per platform tool (a test checks every `ToolName` has one)."""


class EnabledTools(Protocol):
    """What the builder needs of a business's tools (`ToolExecutor` has both)."""

    @property
    def enabled(self) -> tuple[ToolName, ...]: ...

    def input_model(self, tool: ToolName) -> type[BaseModel]: ...


def variant_name(tool: ToolName) -> str:
    """`check_slots` → `CallCheckSlots`: the variant's class name (its `$defs` title)."""
    return "Call" + "".join(part.title() for part in tool.value.split("_"))


def call_variant(tool: ToolName, arguments: type[BaseModel]) -> type[CallTool]:
    """The `CallTool` variant of one tool, with the business's input model as its arguments."""
    variant: type[CallTool] = create_model(
        variant_name(tool),
        __base__=CallTool,
        __module__=__name__,
        __doc__=TOOL_DESCRIPTIONS[tool],
        tool=(cast(Any, Literal).__getitem__(tool.value), Field(description="The tool's name.")),
        arguments=(arguments, Field(description="The tool's arguments.")),
    )
    return variant


def build_agent_step(tools: EnabledTools) -> type[AgentStep]:
    """The business's `AgentStep` model (module docstring)."""
    enabled = tuple(sorted(tools.enabled))
    if len(set(enabled)) != len(enabled):
        raise ValueError("each enabled tool appears once")
    calls = [call_variant(tool, tools.input_model(tool)) for tool in enabled]
    members: list[Any] = [Reply, Handoff]
    if len(calls) == 1:
        members.insert(0, calls[0])
    elif calls:
        members.insert(0, Annotated[Union[tuple(calls)], Field(discriminator="tool")])  # noqa: UP007
    step: Any = Annotated[Union[tuple(members)], Field(discriminator="action")]  # noqa: UP007
    model = create_model(
        "AgentStep",
        __base__=AgentStep,
        __module__=__name__,
        __doc__=AgentStep.__doc__,
        step=(step, Field(description="Exactly one of call_tool, reply or handoff.")),
    )
    return cast(type[AgentStep], model)
