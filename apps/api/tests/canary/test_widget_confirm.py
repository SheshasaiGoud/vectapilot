"""Widget confirm against Postgres, as the app role (M8 PR 2b; S1, S4, S6, ADR 0016 W2, W5, W6, W8,
ADR 0019 decision 2): leo's (1), (2) and (5), and the wrong kind of token. Integration only,
under tests/canary/ so the canary gate's no-skip rule covers them.

The real app for a business seeded from the Paws & Co seed (`tests/canary/widget.py`,
`served_tool_widgets`: its tools, its guidance, a widget, the guards on, the limiter in memory),
a customer session made through the real route, and a pending `book` action stored with the real
store for that session's `customer_ref` (S4), as the agent's turn would store it:

- (1) widget confirm executes the action once: 200 with the stored result, every repeat the
  identical body, exactly one booking on the slot, the audit's one `executed` and its `replayed`
  rows;
- (2) two confirms in parallel, both seen waiting on the action's row lock (`until_waiting`, as
  M5's own test): the same 200 body twice, exactly one booking and the audit's `executed` and
  `replayed` (W8: the second re-reads the committed row);
- (5) the turn comes through `POST /v1/widget/messages` (a stand-in guard stores the proposal
  with the real store for the customer the route names, as the agent's turn would: H1 is a
  stub), and "talk to a person" is pressed, so migration 0009's tables hold a turn row and a
  hand-off row (the positive control); the token is then in no audit row, no row of those
  tables, no log record (the confirm's own `tool_call` record included) and no response but the
  turn's (leo's PR 2 review);
- a staff session's token as `Authorization: Bearer` is the one 401 and writes nothing (S1).

Another session's action, in the same business or another, is `test_tool_customers.py`'s.
Every seeded business is removed afterwards (`sessions.SESSION_UPROOT`, then `sandbox_db.uproot`).
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.actions.db import active_on, audit_text, audits, until_waiting, written_by
from tests.canary.sessions import SESSION_UPROOT
from tests.canary.widget import bearer, new_session, served_tool_widgets, session_id_of
from tests.conftest import integration
from tests.tools.core import PAWS, config_for, context_for
from tests.tools.sandbox_db import app_url, fresh_engine, owner_url, seeded
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.agent.responses import AgentRequest, ConfirmationResponse
from vectapilot_api.agent.router import get_agent_guard
from vectapilot_api.auth.dependencies import UNAUTHORIZED
from vectapilot_api.auth.store import SessionKind, customer_ref_for, insert_session
from vectapilot_api.auth.tokens import make_token, parse_token
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.tools.executor import ToolExecutor

pytestmark = [integration, pytest.mark.anyio]

SLOT: Final = "S-09"
WIDGET_TABLES: Final = ("handoffs", "widget_turns", "turn_feedback")


@dataclass
class Served:
    business_id: str
    client: TestClient
    owner: AsyncEngine
    executor: ToolExecutor
    store: PendingActionStore

    async def session(self) -> tuple[str, str]:
        """A new customer session through the real route: its token and its `customer_ref`."""
        token = new_session(self.client, self.business_id)
        return token, customer_ref_for(await session_id_of(self.owner, self.business_id, token))

    async def book(self, customer: str) -> PendingAction:
        """A pending `book` of `SLOT` for `customer`, stored as the agent's turn stores one."""
        context = context_for(self.business_id, customer)
        envelope = await self.executor.propose("book", {"slot_id": SLOT}, context)
        assert envelope.proposal is not None, envelope.observation
        return await self.store.create(envelope.proposal, context)

    def confirm(self, action: PendingAction, token: str, session: str) -> httpx2.Response:
        return self.client.post(
            f"/v1/widget/actions/{action.id}/confirm",
            json={"confirm_token": token},
            headers=bearer(session),
        )


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def served(owner: AsyncEngine) -> AsyncIterator[Served]:
    engine = fresh_engine(app_url())
    async with seeded(owner, "w") as (seed,):
        business_id = seed.business_id
        config = config_for(business_id, PAWS)
        database = Database(engine)
        try:
            with served_tool_widgets([business_id]) as client:
                yield Served(
                    business_id,
                    client,
                    owner,
                    ToolExecutor(database, config),
                    PendingActionStore(database, {business_id: config}, lifetime_s=900),
                )
        finally:
            async with business_transaction(owner, business_id) as connection:
                for statement in SESSION_UPROOT:
                    await connection.execute(statement, {"b": business_id})
            await engine.dispose()


def secret(action: PendingAction) -> str:
    return action.confirm_token.get_secret_value()


async def widget_rows(owner: AsyncEngine, business_id: str) -> str:
    """Every row of migration 0009's tables for the business, as JSON text (the owner's view)."""
    found: list[str] = []
    async with business_transaction(owner, business_id) as connection:
        for table in WIDGET_TABLES:
            result = await connection.execute(
                text(f"SELECT row_to_json(t)::text FROM {table} t WHERE business_id = :b"),  # noqa: S608
                {"b": business_id},
            )
            found += [str(row[0]) for row in result]
    return "\n".join(found)


# ── (1) once, and every repeat the same body ─────────────────────────────────────────────────


async def test_widget_confirm_executes_once_and_every_repeat_returns_its_body(
    served: Served, owner: AsyncEngine
) -> None:
    session, customer = await served.session()
    action = await served.book(customer)

    first = served.confirm(action, secret(action), session)
    repeats = [served.confirm(action, secret(action), session) for _ in range(2)]

    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["status"], body["tool"], body["action_id"]) == ("executed", "book", str(action.id))
    assert body["result"]["slot_id"] == SLOT
    assert [(r.status_code, r.content) for r in repeats] == [(200, first.content)] * 2
    assert first.headers["cache-control"] == "no-store"
    assert len({first.headers["X-Trace-Id"], *(r.headers["X-Trace-Id"] for r in repeats)}) == 3
    [booking] = await active_on(owner, served.business_id, SLOT)
    assert booking.customer_ref == customer  # the session's own reference (S4)
    outcomes = [outcome for phase, outcome, _ in await audits(owner, served.business_id)]
    assert outcomes.count("executed") == 1 and outcomes.count("replayed") == 2


# ── (2) two in parallel ──────────────────────────────────────────────────────────────────────


async def test_two_parallel_widget_confirms_write_once_and_answer_the_same_body(
    served: Served, owner: AsyncEngine
) -> None:
    session, customer = await served.session()
    action = await served.book(customer)

    async with business_transaction(owner, served.business_id) as holder:
        # Hold the action's row lock: both confirms wait at their `FOR UPDATE` read (W8).
        await holder.execute(
            text("SELECT 1 FROM pending_actions WHERE business_id = :b AND id = :id FOR UPDATE"),
            {"b": served.business_id, "id": action.id},
        )
        racing = asyncio.gather(
            asyncio.to_thread(served.confirm, action, secret(action), session),
            asyncio.to_thread(served.confirm, action, secret(action), session),
        )
        await until_waiting(owner, 2)  # both are at the lock (leo's PR 2 review), never a sleep
    first, second = await racing

    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.content == second.content
    assert len(await active_on(owner, served.business_id, SLOT)) == 1
    assert await written_by(owner, served.business_id, action) == 1
    confirms = [o for phase, o, _ in await audits(owner, served.business_id) if phase == "confirm"]
    assert sorted(confirms) == ["executed", "replayed"]


# ── (5) the token in no row and no record ────────────────────────────────────────────────────


class ProposingGuard:
    """Stands in for the agent's turn behind `get_agent_guard` (H1 is a stub): stores a `book`
    proposal with the real store for the customer the route names, and answers its confirmation
    as `AgentService` does. Runs on the app's loop; `served.store`'s engine has no pool."""

    def __init__(self, served: Served) -> None:
        self.served = served
        self.actions: list[PendingAction] = []

    async def respond(
        self,
        business_id: str,
        request: AgentRequest,
        *,
        trace_id: str,
        service: object,
        read_only: object,
    ) -> ConfirmationResponse:
        assert business_id == self.served.business_id
        action = await self.served.book(request.customer_ref)
        self.actions.append(action)
        return ConfirmationResponse(
            trace_id=trace_id,
            action_id=action.id,
            tool=action.tool,
            summary=action.summary,
            expires_at=action.expires_at,
            confirm_token=action.confirm_token,
        )


async def test_the_confirm_token_reaches_no_row_and_no_log_record(
    served: Served, owner: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    session, customer = await served.session()
    guard = ProposingGuard(served)
    app = served.client.app
    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_agent_guard] = lambda: guard
    root = logging.getLogger()
    root.addHandler(caplog.handler)  # after `create_app`, which installs the app's handlers
    try:
        with caplog.at_level(logging.DEBUG):
            turn = served.client.post(
                "/v1/widget/messages",
                json={"message": "Book S-09, please."},
                headers=bearer(session),
            )
            token = turn.json()["turn"]["confirm_token"]
            [action] = guard.actions
            done = served.confirm(action, token, session)
            refused = served.confirm(action, "Q7zx9TOKENtokenTOKEN_-", session)
            pressed = served.client.post("/v1/widget/handoffs", headers=bearer(session))
    finally:
        root.removeHandler(caplog.handler)
        app.dependency_overrides.pop(get_agent_guard, None)

    assert turn.status_code == 200, turn.text
    assert turn.json()["turn"]["kind"] == "confirmation_required"
    assert token == secret(action) and "*" not in token  # revealed once, in the turn
    assert (done.status_code, refused.status_code) == (200, 404)
    assert pressed.status_code == 200 and pressed.json()["queued"] is True
    [booking] = await active_on(owner, served.business_id, SLOT)
    assert booking.customer_ref == customer  # stored for the session's own reference (S4)
    records = [r for r in caplog.records if r.getMessage() == "tool_call"]
    assert records, "no tool_call record: the capture is broken"
    for record in caplog.records:
        assert token not in f"{record.getMessage()} {json.dumps(vars(record), default=str)}"
    assert token not in await audit_text(owner, served.business_id)
    stored = await widget_rows(owner, served.business_id)
    # The positive control: the turn row and the press's hand-off row are there to search.
    assert '"kind":"confirmation_required"' in stored and '"origin":"button"' in stored
    assert token not in stored
    assert token not in done.text and token not in refused.text and token not in pressed.text


# ── S1: the wrong kind of token ──────────────────────────────────────────────────────────────


async def test_a_staff_sessions_token_as_bearer_is_the_one_401_and_confirms_nothing(
    served: Served, owner: AsyncEngine
) -> None:
    session, customer = await served.session()
    action = await served.book(customer)
    staff_token = make_token(served.business_id)
    async with business_transaction(owner, served.business_id) as connection:
        staff_id = await connection.scalar(
            text(
                "INSERT INTO staff_users (business_id, display_name, role)"
                " VALUES (:b, 'TEST-ONLY reviewer', 'reviewer') RETURNING id"
            ),
            {"b": served.business_id},
        )
        await insert_session(
            connection,
            parse_token(staff_token),
            kind=SessionKind.STAFF,
            lifetime_s=3_600,
            staff_id=staff_id,
        )

    staff = served.confirm(action, secret(action), staff_token)
    anonymous = served.client.post(
        f"/v1/widget/actions/{action.id}/confirm", json={"confirm_token": secret(action)}
    )

    assert staff.json() == anonymous.json() == {"detail": UNAUTHORIZED, "error": "unauthorized"}
    assert staff.content == anonymous.content and staff.status_code == 401
    assert await active_on(owner, served.business_id, SLOT) == []
    # The positive control: the customer's own session confirms it.
    assert served.confirm(action, secret(action), session).status_code == 200
