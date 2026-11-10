"""The same-business customer canary (M5 PR 2; ADR 0016 decision 9). Integration only.

RLS separates businesses, not customers: inside one business only each tool statement's own
`customer_ref = :customer_ref` keeps one customer's orders and bookings from another. So, in a
throwaway business seeded from the Paws & Co seed (`cust-001` and `cust-002`), each customer gets
an order whose note is that customer's own canary, and:

- as customer X, `order_status` for Y's order and `reschedule`'s proposal for Y's booking never
  show Y's canary or booking, and are byte-identical to asking for an id that does not exist;
- as Y, the same calls show Y's (the positive control: the checks are not vacuous);
- on the app role's connection (under RLS) and on the owner's (no RLS at all): the customer
  clause is the only fence on both, so both must hold;
- and the red proofs: without `order_status`' customer clause X does see Y's canary, and without
  `reschedule`'s X gets a proposal that shows Y's booking.

The business is removed afterwards, `tool_audit` included (`sandbox_db.uproot`).
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.read_paths import EngineTransactions
from tests.conftest import integration
from tests.tools.core import config_for
from tests.tools.sandbox_db import app_url, fresh_engine, owner_url, seeded
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.tools import order_status, reschedule
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.types import ToolContext, ToolEnvelope, ToolError, ToolErrorCode

pytestmark = [integration, pytest.mark.anyio]

X: Final = "cust-001"
Y: Final = "cust-002"
Y_BOOKING: Final = "B-102"
"""cust-002's seeded booking (Paws & Co seed); cust-001 holds B-101 and B-103."""
FREE_SLOT: Final = "S-09"
CUSTOMER_ORDER: Final = text(
    "INSERT INTO sandbox_orders (business_id, id, customer_ref, status, items, note, placed_at)"
    " VALUES (:b, :id, :customer, 'placed', 'Canary kibble', :canary, now())"
)
CUSTOMER_CLAUSES: Final = {
    "order_status": (order_status, "OWN_ORDER", " AND customer_ref = :customer_ref"),
    "reschedule": (reschedule, "OWN_ACTIVE_BOOKING", " AND b.customer_ref = :customer_ref"),
}


@dataclass(frozen=True)
class Customers:
    business_id: str
    canary: dict[str, str]
    order: dict[str, str]


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def app() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(app_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def customers(owner: AsyncEngine) -> AsyncIterator[Customers]:
    async with seeded(owner, "c") as (seed,):
        planted = Customers(
            seed.business_id,
            canary={c: f"canary-{c}-{secrets.token_hex(8)}" for c in (X, Y)},
            order={X: "CX-1", Y: "CY-1"},
        )
        async with business_transaction(owner, seed.business_id) as connection:
            for customer in (X, Y):
                await connection.execute(
                    CUSTOMER_ORDER,
                    {
                        "b": seed.business_id,
                        "id": planted.order[customer],
                        "customer": customer,
                        "canary": planted.canary[customer],
                    },
                )
        yield planted


def source(connection: str, app: AsyncEngine, owner: AsyncEngine) -> TransactionSource:
    """The app role through `Database` (its role check included), or the owner, bare."""
    return Database(app) if connection == "app role" else EngineTransactions(owner)


def as_customer(tools: TransactionSource, planted: Customers) -> ToolExecutor:
    return ToolExecutor(tools, config_for(planted.business_id))


async def order_of(
    tools: ToolExecutor, planted: Customers, customer: str, order_id: str
) -> ToolEnvelope:
    context = ToolContext(business_id=planted.business_id, customer_ref=customer)
    return await tools.run("order_status", {"order_id": order_id}, context)


async def reschedule_of(
    tools: ToolExecutor, planted: Customers, customer: str, booking: str
) -> ToolEnvelope:
    context = ToolContext(business_id=planted.business_id, customer_ref=customer)
    arguments = {"booking_id": booking, "new_slot_id": FREE_SLOT}
    return await tools.propose("reschedule", arguments, context)


CONNECTIONS: Final = ["app role", "owner"]


@pytest.mark.parametrize("connection", CONNECTIONS)
async def test_order_status_never_shows_another_customers_order(
    app: AsyncEngine, owner: AsyncEngine, customers: Customers, connection: str
) -> None:
    tools = as_customer(source(connection, app, owner), customers)

    theirs = await order_of(tools, customers, X, customers.order[Y])
    nobodys = await order_of(tools, customers, X, "NO-SUCH-1")
    own = await order_of(tools, customers, X, customers.order[X])
    as_y = await order_of(tools, customers, Y, customers.order[Y])

    assert customers.canary[Y] not in theirs.observation
    assert theirs.error == ToolError.of(ToolErrorCode.NOT_FOUND)
    assert theirs.observation == nobodys.observation
    assert theirs.business_ids == nobodys.business_ids == frozenset()
    assert customers.canary[X] in own.observation  # the positive controls
    assert customers.canary[Y] in as_y.observation


@pytest.mark.parametrize("connection", CONNECTIONS)
async def test_reschedule_never_proposes_another_customers_booking(
    app: AsyncEngine, owner: AsyncEngine, customers: Customers, connection: str
) -> None:
    tools = as_customer(source(connection, app, owner), customers)

    theirs = await reschedule_of(tools, customers, X, Y_BOOKING)
    nobodys = await reschedule_of(tools, customers, X, "B-999")
    as_y = await reschedule_of(tools, customers, Y, Y_BOOKING)

    assert theirs.proposal is None and Y_BOOKING not in theirs.observation
    assert theirs.error == ToolError.of(ToolErrorCode.NOT_FOUND)
    assert theirs.observation == nobodys.observation
    assert as_y.proposal is not None and Y_BOOKING in as_y.proposal.summary  # positive control


@pytest.mark.parametrize("tool", sorted(CUSTOMER_CLAUSES))
async def test_red_proof_without_its_customer_clause_a_tool_shows_another_customer(
    app: AsyncEngine, customers: Customers, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    module, name, clause = CUSTOMER_CLAUSES[tool]
    statement = str(getattr(module, name))
    assert clause in statement, f"{tool}'s customer clause changed: update this proof"
    monkeypatch.setattr(module, name, text(statement.replace(clause, "", 1)))
    tools = as_customer(Database(app), customers)

    if tool == "order_status":
        theirs = await order_of(tools, customers, X, customers.order[Y])
        assert customers.canary[Y] in theirs.observation  # RLS does not separate customers
    else:
        theirs = await reschedule_of(tools, customers, X, Y_BOOKING)
        assert theirs.proposal is not None and Y_BOOKING in theirs.proposal.summary
