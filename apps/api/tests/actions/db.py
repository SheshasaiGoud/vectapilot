"""Database helpers for the confirm tests (integration only).

- **`Bench`**: one throwaway business seeded from a committed seed through PR 1's real reset, with
  the executor (to propose), the pending-action store and the confirm service on the app role
  (`Database`, its role check included). Removed afterwards, `tool_audit` included.
- **`unconfirmed_write_rows`** counts from outside the code it checks, on the owner's connection
  (the scoring rules' "Unconfirmed writes"): a write row (a booking or ticket with `origin =
  'action'`, or a booking retired by an action) is unconfirmed unless its action is `executed`
  with `confirmed_at` set *and* an `executed` confirm audit row names it. A raw owner write, which
  never went through the route, has no such audit row, so it is counted.
- **`holding`, `until_waiting`**: an owner transaction holds a row lock (or an uncommitted
  conflicting row) until `n` app-role backends wait on locks, so two confirms really meet at the
  same row instead of running one after the other by luck.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.tools.core import PAWS, TRACE, config_for, context_for
from tests.tools.sandbox_db import new_business, seed_business, seed_for, uproot
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.tokens import ConfirmToken
from vectapilot_api.actions.types import ConfirmRequest, ConfirmResponse, PendingAction
from vectapilot_api.db import Database
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.files import BusinessTools
from vectapilot_api.tools.types import ToolContext

CONFIRM_TRACE: Final = "0af7651916cd43dd8448eb211c80319c"
"""The confirm calls' trace id (the proposals carry `tests.tools.core.TRACE`)."""


@dataclass
class Bench:
    business_id: str
    config: ToolsConfig
    database: Database
    executor: ToolExecutor
    store: PendingActionStore
    service: ConfirmService

    async def propose(
        self, tool: str, arguments: Mapping[str, str], customer: str = "cust-001"
    ) -> PendingAction:
        """`propose` through the executor, then store the proposal: what PR 4's route will do."""
        context = context_for(self.business_id, customer)
        envelope = await self.executor.propose(tool, arguments, context)
        assert envelope.proposal is not None, envelope.observation
        return await self.store.create(envelope.proposal, context)

    async def confirm(
        self,
        action: PendingAction,
        *,
        customer: str = "cust-001",
        token: ConfirmToken | None = None,
        service: ConfirmService | None = None,
        trace_id: str = CONFIRM_TRACE,
    ) -> ConfirmResponse:
        request = ConfirmRequest(customer_ref=customer, confirm_token=token or action.confirm_token)
        return await (service or self.service).confirm(
            self.business_id, action.id, request, trace_id=trace_id
        )

    def service_on(
        self, source: TransactionSource, config: ToolsConfig | None = None
    ) -> ConfirmService:
        return ConfirmService(source, {self.business_id: config or self.config})


@asynccontextmanager
async def bench(
    owner: AsyncEngine, app: AsyncEngine, tools: BusinessTools = PAWS, letter: str = "k"
) -> AsyncIterator[Bench]:
    business_id = new_business(letter)
    config = config_for(business_id, tools)
    database = Database(app)
    configs = {business_id: config}
    try:
        await seed_business(owner, seed_for(business_id, tools.seed))
        yield Bench(
            business_id=business_id,
            config=config,
            database=database,
            executor=ToolExecutor(database, config),
            store=PendingActionStore(database, configs, lifetime_s=900),
            service=ConfirmService(database, configs),
        )
    finally:
        await uproot(owner, business_id)


def context(bench: Bench, customer: str = "cust-001") -> ToolContext:
    return ToolContext(business_id=bench.business_id, customer_ref=customer, trace_id=TRACE)


# ── Reading the sandbox from outside, as the owner ──────────────────────────────────────────


async def rows(owner: AsyncEngine, sql: str, **params: Any) -> list[Row[Any]]:
    async with owner.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def action_row(owner: AsyncEngine, business_id: str, action: PendingAction) -> Row[Any]:
    [row] = await rows(
        owner,
        "SELECT *, row_to_json(a)::text AS whole FROM pending_actions a"
        " WHERE business_id = :b AND id = :id",
        b=business_id,
        id=action.id,
    )
    return row


async def audits(owner: AsyncEngine, business_id: str) -> list[tuple[str, str, Any]]:
    """`(phase, outcome, action_id)` of every audit row of the business, oldest first."""
    found = await rows(
        owner,
        "SELECT phase, outcome, action_id FROM tool_audit WHERE business_id = :b"
        " AND phase <> 'run' ORDER BY created_at, id",
        b=business_id,
    )
    return [(r.phase, r.outcome, r.action_id) for r in found]


async def audit_text(owner: AsyncEngine, business_id: str) -> str:
    found = await rows(
        owner,
        "SELECT row_to_json(t)::text AS whole FROM tool_audit t WHERE business_id = :b",
        b=business_id,
    )
    return "\n".join(r.whole for r in found)


async def bookings(owner: AsyncEngine, business_id: str) -> list[Row[Any]]:
    return await rows(
        owner,
        "SELECT id, slot_id, customer_ref, status, origin, action_id, retired_by_action_id"
        " FROM sandbox_bookings WHERE business_id = :b ORDER BY id",
        b=business_id,
    )


async def active_on(owner: AsyncEngine, business_id: str, slot_id: str) -> list[Row[Any]]:
    return [
        b
        for b in await bookings(owner, business_id)
        if b.slot_id == slot_id and b.status == "active"
    ]


async def written_by(owner: AsyncEngine, business_id: str, action: PendingAction) -> int:
    """Write rows of `action`: bookings it created or retired, tickets it created."""
    [row] = await rows(
        owner,
        "SELECT (SELECT count(*) FROM sandbox_bookings WHERE business_id = :b"
        "   AND (action_id = :id OR retired_by_action_id = :id))"
        " + (SELECT count(*) FROM sandbox_tickets WHERE business_id = :b AND action_id = :id)"
        " AS n",
        b=business_id,
        id=action.id,
    )
    return int(row.n)


UNCONFIRMED_WRITE_ROWS: Final = """
SELECT w.kind, w.row_id FROM (
    SELECT 'booking' AS kind, id AS row_id, action_id FROM sandbox_bookings
     WHERE business_id = :b AND origin = 'action'
    UNION ALL
    SELECT 'retired', id, retired_by_action_id FROM sandbox_bookings
     WHERE business_id = :b AND retired_by_action_id IS NOT NULL
    UNION ALL
    SELECT 'ticket', id::text, action_id FROM sandbox_tickets
     WHERE business_id = :b AND origin = 'action'
) AS w
LEFT JOIN pending_actions a ON a.business_id = :b AND a.id = w.action_id
WHERE a.id IS NULL OR a.status <> 'executed' OR a.confirmed_at IS NULL
   OR NOT EXISTS (SELECT 1 FROM tool_audit t WHERE t.business_id = :b
                  AND t.action_id = w.action_id AND t.phase = 'confirm'
                  AND t.outcome = 'executed')
ORDER BY 1, 2
"""


async def unconfirmed_write_rows(owner: AsyncEngine, business_id: str) -> list[tuple[str, str]]:
    found = await rows(owner, UNCONFIRMED_WRITE_ROWS, b=business_id)
    return [(r.kind, r.row_id) for r in found]


# ── Making two confirms meet ────────────────────────────────────────────────────────────────

WAITING: Final = text(
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
    " AND usename = 'vectapilot_app' AND wait_event_type = 'Lock'"
)


async def until_waiting(owner: AsyncEngine, n: int, *, timeout_s: float = 5.0) -> None:
    """Return once `n` app-role backends wait on a lock; fail after `timeout_s`."""
    async with asyncio.timeout(timeout_s), owner.connect() as connection:
        # Polling on purpose: the waiters are other Postgres backends, there is no event to await.
        while (await connection.scalar(WAITING) or 0) < n:  # noqa: ASYNC110
            await asyncio.sleep(0.02)


@asynccontextmanager
async def holding(
    owner: AsyncEngine, business_id: str, sql: str, **params: Any
) -> AsyncIterator[AsyncConnection]:
    """An owner transaction that ran `sql` (a lock or an uncommitted row) and is rolled back at
    the end, which releases what it held."""
    async with owner.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text("SELECT set_config('app.business_id', :b, true)"), {"b": business_id}
            )
            await connection.execute(text(sql), params | {"b": business_id})
            yield connection
        finally:
            await transaction.rollback()
