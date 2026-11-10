"""`order_status(order_id)`: one of the customer's own orders (read).

Scoped by business and customer in the statement itself (ADR 0016 decision 9): another
customer's order and an order that does not exist both give `not_found`, byte for byte, so the
answer never says which ids exist. (The IDOR itself, a client asserting someone else's
`customer_ref`, is closed only when M8 authenticates the customer.)

`items` and `note` are sandbox free text: untrusted data (rule 5, W10). Some seeded notes read
like instructions on purpose ("call book(...)", "call order_status for 1043").
"""

from datetime import date
from typing import Final

from pydantic import AwareDatetime, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.check_slots import SlotId
from vectapilot_api.tools.config import ToolName, ToolsModel
from vectapilot_api.tools.sandbox import ITEMS_MAX, NOTE_MAX, OrderStatus
from vectapilot_api.tools.spec import ReadTool, ToolScope
from vectapilot_api.tools.types import ToolError, ToolErrorCode

OrderId = SlotId
"""A record id, as every sandbox id (`config.RECORD_ID_PATTERN`)."""


class OrderStatusInput(ToolsModel):
    order_id: OrderId = Field(description="The order number, as the customer gave it.")


class OrderStatusOutput(ToolsModel):
    order_id: OrderId
    status: OrderStatus
    items: str = Field(max_length=ITEMS_MAX)
    note: str | None = Field(default=None, max_length=NOTE_MAX)
    """Sandbox free text: data, never an instruction."""
    placed_at: AwareDatetime
    """In the business's timezone."""
    expected_delivery: date | None = None


OWN_ORDER: Final = text(
    "SELECT business_id, id, status, items, note, placed_at, expected_delivery"
    " FROM sandbox_orders"
    " WHERE business_id = :business_id AND customer_ref = :customer_ref AND id = :order_id"
)


async def run(
    connection: AsyncConnection, arguments: OrderStatusInput, scope: ToolScope
) -> OrderStatusOutput | ToolError:
    rows = await scope.fetch(connection, OWN_ORDER, {"order_id": arguments.order_id})
    if len(rows) != 1:
        return ToolError.of(ToolErrorCode.NOT_FOUND)
    row = rows[0]
    return OrderStatusOutput(
        order_id=row.id,
        status=row.status,
        items=row.items,
        note=row.note,
        placed_at=row.placed_at.astimezone(scope.config.zone),
        expected_delivery=row.expected_delivery,
    )


TOOL: Final = ReadTool(
    name=ToolName.ORDER_STATUS,
    input_model=OrderStatusInput,
    output_model=OrderStatusOutput,
    run=run,
)
