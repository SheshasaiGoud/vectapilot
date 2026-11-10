"""The registry: every platform tool, by name (ADR 0016 decision 1, M5 PR 2).

One entry per `ToolName`, each from its own module. A business enables a subset in its
`tools.json`; the executor refuses the rest (`not_enabled`). The registry holds no business data:
the per-business parts (enabled set, services, ticket categories, timezone, sandbox date) come
from configuration (rule 9).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from vectapilot_api.tools import book, check_slots, create_ticket, order_status, reschedule
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.spec import AnyTool

REGISTRY: Final[Mapping[ToolName, AnyTool]] = MappingProxyType(
    {
        ToolName.BOOK: book.TOOL,
        ToolName.CHECK_SLOTS: check_slots.TOOL,
        ToolName.CREATE_TICKET: create_ticket.TOOL,
        ToolName.ORDER_STATUS: order_status.TOOL,
        ToolName.RESCHEDULE: reschedule.TOOL,
    }
)
