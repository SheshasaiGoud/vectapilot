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

**M8 PR 2b: session X against session Y on widget confirm** (S1, S4, ADR 0016's IDOR closed). A
widget session's `customer_ref` is its own (`s-<hex>`), so the same customer clause separates two
sessions: in two businesses seeded from the same seed and served with their widgets
(`tests/canary/widget.py`), session Y of the same business and a session of the other business
each confirm X's action with X's own token and get the one 404, byte-identical to an unknown id,
and nothing is written; X's own confirm then executes it (the positive control). The red proof:
without the customer clause in the confirm's locked read (`actions.confirm.LOCK_ACTION`), session
Y's confirm with X's token is no longer the one 404: it reaches X's action (its audit row says
`rolled_back`, and the server raises), and `MARK_CONFIRMED`'s own customer clause stops the write
(execute's customer check stands behind it). So that clause, not RLS, is what keeps one session
from even seeing another's action (W6). The
businesses' session rows go first (`sessions.SESSION_UPROOT`).
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.actions.db import active_on, audits, written_by
from tests.canary.read_paths import EngineTransactions
from tests.canary.sessions import SESSION_UPROOT
from tests.canary.widget import bearer, new_session, served_tool_widgets, session_id_of
from tests.conftest import integration
from tests.tools.core import config_for, context_for
from tests.tools.sandbox_db import app_url, fresh_engine, owner_url, seeded
from vectapilot_api.actions import confirm as confirm_module
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.auth.store import customer_ref_for
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


# ── M8 PR 2b: session X against session Y on widget confirm ──────────────────────────────────


@dataclass(frozen=True)
class WidgetBusinesses:
    a: str
    b: str
    client: TestClient


@pytest.fixture
async def widget_businesses(owner: AsyncEngine) -> AsyncIterator[WidgetBusinesses]:
    async with seeded(owner, "x", "y") as (a, b):
        try:
            with served_tool_widgets([a.business_id, b.business_id]) as client:
                yield WidgetBusinesses(a.business_id, b.business_id, client)
        finally:
            for business_id in (a.business_id, b.business_id):
                async with business_transaction(owner, business_id) as connection:
                    for statement in SESSION_UPROOT:
                        await connection.execute(statement, {"b": business_id})


async def book_for(
    app: AsyncEngine, owner: AsyncEngine, business_id: str, session: str
) -> PendingAction:
    """A pending `book` of `FREE_SLOT` for `session`'s own `customer_ref` (S4), stored with the
    real store as the agent's turn stores one."""
    customer = customer_ref_for(await session_id_of(owner, business_id, session))
    config = config_for(business_id)
    context = context_for(business_id, customer)
    envelope = await ToolExecutor(Database(app), config).propose(
        "book", {"slot_id": FREE_SLOT}, context
    )
    assert envelope.proposal is not None, envelope.observation
    store = PendingActionStore(Database(app), {business_id: config}, lifetime_s=900)
    return await store.create(envelope.proposal, context)


def widget_confirm(
    client: TestClient, action_id: object, token: str, session: str
) -> httpx2.Response:
    return client.post(
        f"/v1/widget/actions/{action_id}/confirm",
        json={"confirm_token": token},
        headers=bearer(session),
    )


async def test_no_session_confirms_another_sessions_action_on_the_widget(
    app: AsyncEngine, owner: AsyncEngine, widget_businesses: WidgetBusinesses
) -> None:
    served = widget_businesses
    x, y = new_session(served.client, served.a), new_session(served.client, served.a)
    other = new_session(served.client, served.b)
    action = await book_for(app, owner, served.a, x)
    token = action.confirm_token.get_secret_value()

    def confirm(action_id: object, session: str) -> httpx2.Response:
        return widget_confirm(served.client, action_id, token, session)

    same_business = confirm(action.id, y)
    other_business = confirm(action.id, other)
    unknown = confirm(uuid4(), x)

    refusals = [same_business, other_business, unknown]
    assert {(r.status_code, r.content) for r in refusals} == {(404, unknown.content)}
    assert unknown.json()["error"] == "action_not_found"
    assert await written_by(owner, served.a, action) == 0
    assert await active_on(owner, served.b, FREE_SLOT) == []
    # The positive control: X's own session confirms it, once.
    own = confirm(action.id, x)
    assert own.status_code == 200, own.text
    assert len(await active_on(owner, served.a, FREE_SLOT)) == 1


CONFIRM_CUSTOMER_CLAUSE: Final = " AND customer_ref = :customer_ref"


async def test_red_proof_without_the_confirms_customer_clause_session_y_reaches_xs_action(
    app: AsyncEngine,
    owner: AsyncEngine,
    widget_businesses: WidgetBusinesses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = widget_businesses
    x, y = new_session(served.client, served.a), new_session(served.client, served.a)
    action = await book_for(app, owner, served.a, x)
    statement = str(confirm_module.LOCK_ACTION)
    assert CONFIRM_CUSTOMER_CLAUSE in statement, "LOCK_ACTION's clause changed: update this proof"
    monkeypatch.setattr(
        confirm_module, "LOCK_ACTION", text(statement.replace(CONFIRM_CUSTOMER_CLAUSE, "", 1))
    )
    # The shared client (leo's PR 2 review): it runs on the lifespan's event loop, which the
    # app's pooled engine is bound to, and raises the server's exception. A second, un-entered
    # client would run each request on a new loop and fail in the session lookup instead.
    with pytest.raises(RuntimeError, match="could not be marked confirmed"):
        # No longer the one 404: Y's read found X's action, and MARK_CONFIRMED's own customer
        # clause is what stopped it (`actions/confirm.py`), with execute's check behind it.
        widget_confirm(served.client, action.id, action.confirm_token.get_secret_value(), y)

    reached = [row for row in await audits(owner, served.a) if row[2] == action.id]
    assert ("confirm", "rolled_back", action.id) in reached
    assert await written_by(owner, served.a, action) == 0  # the later checks still held
