"""What the database holds after a scenario, read from outside the code it checks (M5 PR 6; scoring
rules, "Unconfirmed writes" and "Duplicate writes").

The harness never trusts what the loop or the service report: after each turn (and its confirms)
it reads the sandbox **on the owner's connection**, the same one that reset it, with plain SELECTs.
The owner skips row-level security (a superuser locally and in CI, ADR 0009 "Limit"), so every
statement names `business_id = :b` itself, and nothing here writes (a test checks every statement
is a SELECT, and the W2 fence in tests/actions/test_write_boundary.py allows this module to *read*
`confirmed_at` and `confirm_trace_id`, never to set them).

- `write_rows`: every write row of the business: a booking an action created (`origin =
  'action'`), a booking an action retired (`retired_by_action_id`), a ticket an action created;
  each with its action's status, whether `confirmed_at` is set, its confirm trace id, whether
  the action was proposed under this scenario's turn trace id, and whether an `executed` confirm
  audit row names the action. The sandbox is reset before every scenario, so every such row should
  be the scenario's own; a row of an action proposed under any other trace (the API container, the
  MCP server, a stray dev confirm on the same business) is still listed, and H2 counts it as
  unconfirmed, never hidden.
- `turn_proposals`: the pending actions stored under one turn's trace id, with their tool and
  stored arguments (the harness keeps only the key arguments: never a ticket's text).
- `active_bookings`: active bookings per customer.
- `audit_calls`: the turn's tool calls as `tool_audit` recorded them (tool, kind, phase, outcome),
  by trace id: the reads made and the typed errors met.
"""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tooleval.runinfo import WriteRow
from vectapilot_api.tools.config import ToolName

WRITE_ROWS: Final = text(
    """
    SELECT w.kind, w.row_id, w.action_id::text AS action_id, w.slot_id, w.category,
           a.status AS action_status,
           a.confirmed_at IS NOT NULL AS confirmed_at_set,
           a.confirm_trace_id AS confirm_trace_id,
           coalesce(a.trace_id = :turn, false) AS action_trace_ours,
           EXISTS (SELECT 1 FROM tool_audit t
                    WHERE t.business_id = :b AND t.action_id = w.action_id
                      AND t.phase = 'confirm' AND t.outcome = 'executed') AS executed_audit
      FROM (
        SELECT 'booking' AS kind, id AS row_id, action_id, slot_id, NULL::text AS category
          FROM sandbox_bookings WHERE business_id = :b AND origin = 'action'
        UNION ALL
        SELECT 'retired', id, retired_by_action_id, NULL::text, NULL::text
          FROM sandbox_bookings WHERE business_id = :b AND retired_by_action_id IS NOT NULL
        UNION ALL
        SELECT 'ticket', id::text, action_id, NULL::text, category
          FROM sandbox_tickets WHERE business_id = :b AND origin = 'action'
      ) AS w
      LEFT JOIN pending_actions a ON a.business_id = :b AND a.id = w.action_id
     ORDER BY 1, 2
    """
)

TURN_PROPOSALS: Final = text(
    "SELECT id::text AS action_id, tool, arguments::text AS arguments FROM pending_actions"
    " WHERE business_id = :b AND trace_id = :trace ORDER BY created_at, id"
)

ACTIVE_BOOKINGS: Final = text(
    "SELECT customer_ref, count(*) AS n FROM sandbox_bookings"
    " WHERE business_id = :b AND status = 'active' GROUP BY customer_ref ORDER BY customer_ref"
)

AUDIT_CALLS: Final = text(
    "SELECT tool, kind, phase, outcome FROM tool_audit"
    " WHERE business_id = :b AND trace_id = :trace ORDER BY created_at, id"
)

STATEMENTS: Final = (WRITE_ROWS, TURN_PROPOSALS, ACTIVE_BOOKINGS, AUDIT_CALLS)
"""Every statement this module sends (each a SELECT naming `:b`; a test checks)."""


async def write_rows(
    connection: AsyncConnection, business_id: str, turn: str, confirm_traces: frozenset[str]
) -> tuple[WriteRow, ...]:
    """Every write row of `business_id`; `turn` is the scenario's turn trace id and
    `confirm_traces` are its own confirm calls."""
    found = (await connection.execute(WRITE_ROWS, {"b": business_id, "turn": turn})).all()
    return tuple(
        WriteRow(
            kind=row.kind,
            row_id=row.row_id,
            action_id=row.action_id,
            slot_id=row.slot_id,
            category=row.category,
            action_status=row.action_status,
            confirmed_at_set=bool(row.confirmed_at_set),
            action_trace_ours=bool(row.action_trace_ours),
            confirm_trace_ours=row.confirm_trace_id in confirm_traces,
            executed_audit=bool(row.executed_audit),
        )
        for row in found
    )


@dataclass(frozen=True)
class StoredProposal:
    action_id: str
    tool: ToolName
    arguments: dict[str, object]
    """The stored, validated arguments (the harness keeps only the key arguments)."""


async def turn_proposals(
    connection: AsyncConnection, business_id: str, trace_id: str
) -> tuple[StoredProposal, ...]:
    found = (await connection.execute(TURN_PROPOSALS, {"b": business_id, "trace": trace_id})).all()
    proposals = []
    for row in found:
        arguments = json.loads(row.arguments)
        if not isinstance(arguments, dict):  # pragma: no cover (migration 0006's CHECK)
            raise ValueError("a stored action's arguments are an object")
        proposals.append(StoredProposal(row.action_id, ToolName(row.tool), arguments))
    return tuple(proposals)


async def active_bookings(connection: AsyncConnection, business_id: str) -> Counter[str]:
    found = (await connection.execute(ACTIVE_BOOKINGS, {"b": business_id})).all()
    return Counter({row.customer_ref: int(row.n) for row in found})


@dataclass(frozen=True)
class AuditCall:
    tool: str
    kind: str
    phase: str
    outcome: str


async def audit_calls(
    connection: AsyncConnection, business_id: str, trace_id: str
) -> tuple[AuditCall, ...]:
    found = (await connection.execute(AUDIT_CALLS, {"b": business_id, "trace": trace_id})).all()
    return tuple(AuditCall(row.tool, row.kind, row.phase, row.outcome) for row in found)
