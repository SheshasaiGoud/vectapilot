"""`create_ticket(category, summary)`: propose a message to the business's staff (write).

Every write needs confirmation, this one included (W4): a ticket is text that staff will read and
act on, so the customer sees exactly what will be stored before it is.

- `category` is one of the business's configured ticket categories (rule 9); the summary shows
  its configured name.
- `summary` is at most `TICKET_SUMMARY_MAX` characters on one trimmed line, without control
  characters (`config.SingleLine`), so it cannot forge a heading or a second line in the
  confirmation text, and without invisible ones (tag characters, zero-width spaces, bidi
  overrides; ZWNJ and ZWJ stay for Hindi and Telugu), so the customer sees every character that
  will be stored.
- **The confirmation text (W3)** shows it under the fixed heading "Your message to staff:",
  JSON-quoted: the quotes are the template's, and a quote inside the text is escaped, so the text
  can never close the quotation and append words of its own. It is the text that will be stored,
  not a statement of fact; it stays untrusted data for every later reader (M7, M8).

It reads no rows: validation is the schema and the configured category.
"""

import json
from typing import Annotated, Final
from uuid import UUID

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.check_slots import ServiceId
from vectapilot_api.tools.config import (
    TICKET_SUMMARY_MAX,
    SingleLine,
    ToolName,
    ToolsConfig,
    ToolsModel,
)
from vectapilot_api.tools.spec import ToolScope, WriteTool
from vectapilot_api.tools.types import ToolError, ToolErrorCode

HEADING: Final = "Your message to staff:"

CategoryId = ServiceId
"""A code (`config.CODE_PATTERN`), narrowed to the business's categories per business."""


class CreateTicketInput(ToolsModel):
    category: CategoryId = Field(description="The ticket's category.")
    summary: Annotated[SingleLine, Field(max_length=TICKET_SUMMARY_MAX)] = Field(
        description="The customer's message to staff, in one line."
    )


class TicketFields(ToolsModel):
    category: str = Field(max_length=100)
    quoted: str = Field(max_length=2 * TICKET_SUMMARY_MAX + 2)
    """The summary as a JSON string literal, quotes included. Only `"` and `\\` are escaped (a
    single line has no control characters), so it is at most twice as long, plus the quotes."""


class CreateTicketOutput(ToolsModel):
    """What PR 3's confirm stores as the executed result."""

    ticket_id: UUID


TEMPLATE: Final = "Send a message to staff ({category}).\n" + HEADING + "\n{quoted}"


async def validate(
    connection: AsyncConnection, arguments: CreateTicketInput, scope: ToolScope
) -> TicketFields | ToolError:
    name = scope.config.category_name(arguments.category)
    if name is None:
        return ToolError.of(ToolErrorCode.INVALID_ARGUMENTS, ("category",))
    return TicketFields(category=name, quoted=json.dumps(arguments.summary, ensure_ascii=False))


def categories(config: ToolsConfig) -> tuple[str, ...]:
    return tuple(sorted(category.id for category in config.ticket_categories))


TOOL: Final = WriteTool(
    name=ToolName.CREATE_TICKET,
    input_model=CreateTicketInput,
    summary_model=TicketFields,
    output_model=CreateTicketOutput,
    validate=validate,
    summary_template=TEMPLATE,
    config_enums={"category": categories},
)
