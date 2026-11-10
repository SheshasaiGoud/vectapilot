"""M5 PR 1: migration 0006's rules, against real Postgres. Integration only.

- **Isolation as the app role:** two fresh businesses seeded with the same ids; each sees only its
  own booking and order under the shared id, no business sees nothing, a row for another business
  is refused, and a booking cannot point at a slot only the other business has (composite key).
- **Least privilege:** the app role cannot write slots or orders, delete anything, rewrite an
  action's arguments, insert an already-confirmed action, or insert a seed-origin row; it can do
  exactly what a confirm (PR 3) needs (the positive control).
- **Exactly once in the schema (W5):** a double booking, a second row for one action, a
  reschedule without its action, one action retiring two bookings, and an executed action without
  a confirmation are each refused by the database, with the SQLSTATE that says so.
- **A write only for an executed action of its own customer (W2, W5, W6):** a booking, ticket or
  retirement for an action that was never executed, or for another customer's action, is refused
  at commit, and an executed action that wrote a row cannot be reopened, by the app role too.

Every write that is expected to fail runs in a SAVEPOINT inside a transaction that is rolled
back, so nothing of it stays. The action keys are checked at commit (DEFERRABLE INITIALLY
DEFERRED), and these transactions never commit, so `refused` and the positive controls run the
deferred checks themselves (`SET CONSTRAINTS ALL IMMEDIATE`), as a commit would.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.conftest import integration
from tests.tools.sandbox_db import app_url, fresh_engine, owner_url, seeded
from vectapilot_api.db import business_transaction
from vectapilot_api.tools.sandbox import SandboxSeed

pytestmark = [integration, pytest.mark.anyio]

TOKEN_HASH: Final = "b" * 64
TRACE: Final = "2" * 32

PENDING: Final = text(
    "INSERT INTO pending_actions (business_id, tool, arguments, customer_ref,"
    " confirm_token_sha256, status, expires_at)"
    " VALUES (:b, 'book', CAST(:arguments AS jsonb), 'cust-001', :hash, 'pending',"
    " now() + interval '15 minutes') RETURNING id"
)
CONFIRM: Final = text(
    "UPDATE pending_actions SET status = 'executed', confirmed_at = now(),"
    " confirm_trace_id = :trace, finished_at = now(), result = CAST(:result AS jsonb)"
    " WHERE business_id = :b AND id = :action"
)
EXECUTED_ACTION: Final = text(
    "INSERT INTO pending_actions (business_id, tool, arguments, customer_ref,"
    " confirm_token_sha256, status, expires_at, confirmed_at, confirm_trace_id, finished_at,"
    " result) VALUES (:b, 'reschedule', '{}', 'cust-001', :hash, 'executed',"
    " now() + interval '15 minutes', now(), :trace, now(), '{}') RETURNING id"
)
"""An executed action of cust-001, written directly as the owner (the app role cannot)."""
AT_COMMIT: Final = text("SET CONSTRAINTS ALL IMMEDIATE")
"""Runs the deferred checks now, as a commit would, inside a transaction that never commits."""
BOOK: Final = text(
    "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
    " action_id) VALUES (:b, :id, :slot, 'cust-001', 'active', 'action', :action)"
)


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
async def pair(owner: AsyncEngine) -> AsyncIterator[tuple[SandboxSeed, SandboxSeed]]:
    async with seeded(owner, "a", "b") as (a, b):
        yield a, b


@asynccontextmanager
async def rolled_back(engine: AsyncEngine, business_id: str) -> AsyncIterator[AsyncConnection]:
    """A transaction for `business_id` that is always rolled back."""
    async with engine.connect() as connection:
        try:
            await connection.execute(
                text("SELECT set_config('app.business_id', :b, true)"), {"b": business_id}
            )
            yield connection
        finally:
            await connection.rollback()


async def refused(
    connection: AsyncConnection, statement: str, params: dict[str, Any] | None = None
) -> str:
    """Run `statement` and then the deferred checks in a SAVEPOINT; it must fail. Its SQLSTATE."""
    with pytest.raises(DBAPIError) as error:
        async with connection.begin_nested():
            await connection.execute(text(statement), params or {})
            await connection.execute(AT_COMMIT)
    return str(getattr(error.value.orig, "sqlstate", ""))


# ── Isolation, as the app role ───────────────────────────────────────────────────────────────


async def test_a_shared_booking_and_order_id_shows_only_the_own_business(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    lookups = {
        "booking": text("SELECT business_id FROM sandbox_bookings WHERE id = 'B-101'"),
        "order": text("SELECT business_id FROM sandbox_orders WHERE id = '1042'"),
        "slot": text("SELECT business_id FROM sandbox_slots WHERE id = 'S-17'"),
    }
    for me in pair:
        async with business_transaction(app, me.business_id) as connection:
            for name, query in lookups.items():
                assert list((await connection.execute(query)).scalars()) == [me.business_id], name
    async with app.connect() as connection:  # no business
        for query in lookups.values():
            assert list((await connection.execute(query)).scalars()) == []


async def test_the_owner_sees_both_under_the_shared_id(
    owner: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    # The premise of the bait: without RLS, both businesses' rows answer the same id.
    a, b = pair
    async with owner.connect() as connection:
        found = set(
            (
                await connection.execute(
                    text(
                        "SELECT business_id FROM sandbox_orders WHERE id = '1042'"
                        " AND business_id IN (:a, :b)"
                    ),
                    {"a": a.business_id, "b": b.business_id},
                )
            ).scalars()
        )
    assert found == {a.business_id, b.business_id}


async def test_a_row_for_another_business_is_refused(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    a, b = pair
    async with rolled_back(app, a.business_id) as connection:
        state = await refused(
            connection,
            "INSERT INTO tool_audit (business_id, tool, kind, phase, outcome, duration_ms)"
            " VALUES (:other, 'check_slots', 'read', 'run', 'ok', 1)",
            {"other": b.business_id},
        )
    assert state == "42501"  # new row violates row-level security policy


async def test_a_booking_cannot_point_at_a_slot_only_the_other_business_has(
    owner: AsyncEngine, app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    a, b = pair
    async with business_transaction(owner, b.business_id) as connection:
        await connection.execute(
            text(
                "INSERT INTO sandbox_slots (business_id, id, service, starts_at, duration_minutes,"
                " label) VALUES (:b, 'S-ONLY-B', 'grooming', now(), 30, 'only b')"
            ),
            {"b": b.business_id},
        )
    async with rolled_back(app, a.business_id) as connection:
        action = await connection.scalar(
            PENDING,
            {"b": a.business_id, "arguments": '{"slot_id": "S-ONLY-B"}', "hash": TOKEN_HASH},
        )
        state = await refused(
            connection,
            str(BOOK),
            {"b": a.business_id, "id": "B-900", "slot": "S-ONLY-B", "action": action},
        )
    assert state == "23503"  # foreign_key_violation: (a, S-ONLY-B) does not exist


# ── Least privilege, as the app role ──────────────────────────────────────────────────────────

APP_REFUSED: Final = {
    "insert a slot": "INSERT INTO sandbox_slots (business_id, id, service, starts_at,"
    " duration_minutes, label) VALUES (:b, 'S-99', 'grooming', now(), 30, 'x')",
    "update an order": "UPDATE sandbox_orders SET status = 'delivered' WHERE business_id = :b",
    "delete a booking": "DELETE FROM sandbox_bookings WHERE business_id = :b",
    "delete an action": "DELETE FROM pending_actions WHERE business_id = :b",
    "delete the audit": "DELETE FROM tool_audit WHERE business_id = :b",
    "update the audit": "UPDATE tool_audit SET outcome = 'ok' WHERE business_id = :b",
    "truncate the audit": "TRUNCATE tool_audit",
    "rewrite a booking's slot": "UPDATE sandbox_bookings SET slot_id = 'S-09'"
    " WHERE business_id = :b",
    "rewrite an action's arguments": "UPDATE pending_actions SET arguments = '{}'"
    " WHERE business_id = :b",
    "choose an action's id": "INSERT INTO pending_actions (id, business_id, tool, arguments,"
    " customer_ref, confirm_token_sha256, status, expires_at) VALUES (gen_random_uuid(), :b,"
    " 'book', '{}', 'cust-001', repeat('c', 64), 'pending', now() + interval '1 minute')",
    "insert a confirmed action": "INSERT INTO pending_actions (business_id, tool, arguments,"
    " customer_ref, confirm_token_sha256, status, expires_at, confirmed_at) VALUES (:b,"
    " 'book', '{}', 'cust-001', repeat('c', 64), 'pending', now() + interval '1 minute',"
    " now())",
}


@pytest.mark.parametrize("name", sorted(APP_REFUSED))
async def test_the_app_role_may_not(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed], name: str
) -> None:
    a, _ = pair
    async with rolled_back(app, a.business_id) as connection:
        state = await refused(connection, APP_REFUSED[name], {"b": a.business_id})
    assert state == "42501", name  # insufficient_privilege


@pytest.mark.parametrize("table", ["sandbox_bookings", "sandbox_tickets"])
async def test_the_app_role_cannot_insert_a_seed_origin_row(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed], table: str
) -> None:
    # A write row the app inserts is always an action's, so it cannot pass as seed data.
    a, _ = pair
    statements = {
        "sandbox_bookings": "INSERT INTO sandbox_bookings (business_id, id, slot_id,"
        " customer_ref, status, origin) VALUES (:b, 'B-901', 'S-09', 'cust-001', 'active', 'seed')",
        "sandbox_tickets": "INSERT INTO sandbox_tickets (business_id, customer_ref, category,"
        " summary, origin) VALUES (:b, 'cust-001', 'other', 'x', 'seed')",
    }
    async with rolled_back(app, a.business_id) as connection:
        state = await refused(connection, statements[table], {"b": a.business_id})
    assert state == "42501"  # the restrictive policy app_writes_are_actions


async def test_the_app_role_can_do_what_a_confirm_needs(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    # The positive control for the refusals above (PR 3's confirm, in miniature).
    a, _ = pair
    params = {"b": a.business_id, "hash": TOKEN_HASH, "trace": TRACE}
    async with rolled_back(app, a.business_id) as connection:
        action = await connection.scalar(PENDING, params | {"arguments": '{"slot_id": "S-09"}'})
        await connection.execute(BOOK, params | {"id": "B-900", "slot": "S-09", "action": action})
        await connection.execute(
            text(
                "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary,"
                " origin, action_id) VALUES (:b, 'cust-001', 'other', 'x', 'action', :action)"
            ),
            params | {"action": action},
        )
        await connection.execute(
            text(
                "INSERT INTO tool_audit (business_id, tool, kind, phase, outcome, duration_ms,"
                " trace_id, action_id) VALUES (:b, 'book', 'write', 'confirm', 'executed', 3,"
                " :trace, :action)"
            ),
            params | {"action": action},
        )
        await connection.execute(
            CONFIRM, params | {"action": action, "result": '{"booking_id": "B-900"}'}
        )
        await connection.execute(AT_COMMIT)  # the action keys hold, as at commit
        status = await connection.scalar(
            text("SELECT status FROM pending_actions WHERE id = :action"), {"action": action}
        )
    assert status == "executed"


# ── Exactly once, in the schema (owner, rolled back) ─────────────────────────────────────────


async def an_action(connection: AsyncConnection, business_id: str) -> object:
    return await connection.scalar(
        PENDING, {"b": business_id, "arguments": '{"slot_id": "S-09"}', "hash": TOKEN_HASH}
    )


async def an_executed_action(connection: AsyncConnection, business_id: str) -> object:
    return await connection.scalar(
        EXECUTED_ACTION, {"b": business_id, "hash": TOKEN_HASH, "trace": TRACE}
    )


CASES: Final[dict[str, tuple[str, str]]] = {
    # name: (statement, SQLSTATE). :b is the business, :action a fresh pending action of it and
    # :executed an executed one, both cust-001's. In the Paws & Co seed, B-101 and B-103 are
    # cust-001's bookings and B-102 is cust-002's.
    "a second active booking for a booked slot": (
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
        " action_id) VALUES (:b, 'B-902', 'S-02', 'cust-002', 'active', 'action', :action)",
        "23505",
    ),
    "a seed row with an action": (
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
        " action_id) VALUES (:b, 'B-903', 'S-09', 'cust-001', 'active', 'seed', :action)",
        "23514",
    ),
    "an action row without its action": (
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin)"
        " VALUES (:b, 'B-904', 'S-09', 'cust-001', 'active', 'action')",
        "23514",
    ),
    "a reschedule without the retiring action": (
        "UPDATE sandbox_bookings SET status = 'rescheduled' WHERE business_id = :b"
        " AND id = 'B-101'",
        "23514",
    ),
    "an active booking marked retired": (
        "UPDATE sandbox_bookings SET retired_by_action_id = :action WHERE business_id = :b"
        " AND id = 'B-101'",
        "23514",
    ),
    "one action retiring two bookings": (
        # Both cust-001's and the action executed: only the unique index can refuse it.
        "UPDATE sandbox_bookings SET status = 'rescheduled', retired_by_action_id = :executed"
        " WHERE business_id = :b AND id IN ('B-101', 'B-103')",
        "23505",
    ),
    "a booking for an action that was never executed": (
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
        " action_id) VALUES (:b, 'B-907', 'S-09', 'cust-001', 'active', 'action', :action)",
        "23503",
    ),
    "a ticket for an action that was never executed": (
        "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
        " action_id) VALUES (:b, 'cust-001', 'other', 'x', 'action', :action)",
        "23503",
    ),
    "a retirement by an action that was never executed": (
        "UPDATE sandbox_bookings SET status = 'rescheduled', retired_by_action_id = :action"
        " WHERE business_id = :b AND id = 'B-101'",
        "23503",
    ),
    "a booking for another customer's action": (
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
        " action_id) VALUES (:b, 'B-908', 'S-09', 'cust-002', 'active', 'action', :executed)",
        "23503",
    ),
    "a ticket for another customer's action": (
        "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
        " action_id) VALUES (:b, 'cust-002', 'other', 'x', 'action', :executed)",
        "23503",
    ),
    "a retirement of another customer's booking": (
        "UPDATE sandbox_bookings SET status = 'rescheduled', retired_by_action_id = :executed"
        " WHERE business_id = :b AND id = 'B-102'",
        "23503",
    ),
    "two tickets for one action": (
        "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
        " action_id) VALUES (:b, 'cust-001', 'other', 'x', 'action', :action),"
        " (:b, 'cust-001', 'other', 'y', 'action', :action)",
        "23505",
    ),
    "an executed action that was never confirmed": (
        "UPDATE pending_actions SET status = 'executed', finished_at = now(), result = '{}'"
        " WHERE business_id = :b AND id = :action",
        "23514",
    ),
    "a failed action without its reason": (
        "UPDATE pending_actions SET status = 'failed', finished_at = now(), result = '{}',"
        " confirmed_at = now(), confirm_trace_id = repeat('3', 32)"
        " WHERE business_id = :b AND id = :action",
        "23514",
    ),
    "an action that lives longer than an hour": (
        "UPDATE pending_actions SET expires_at = created_at + interval '2 hours'"
        " WHERE business_id = :b AND id = :action",
        "23514",
    ),
    "a confirmation without its trace id": (
        "UPDATE pending_actions SET confirmed_at = now() WHERE business_id = :b AND id = :action",
        "23514",
    ),
    "a ticket longer than 500 characters": (
        "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
        " action_id) VALUES (:b, 'cust-001', 'other', repeat('x', 501), 'action', :action)",
        "23514",
    ),
    "a lower-case slot id": (
        "INSERT INTO sandbox_slots (business_id, id, service, starts_at, duration_minutes,"
        " label) VALUES (:b, 's-99', 'grooming', now(), 30, 'x')",
        "23514",
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_schema_refuses(
    owner: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed], name: str
) -> None:
    a, _ = pair
    statement, expected = CASES[name]
    async with rolled_back(owner, a.business_id) as connection:
        params = {
            "b": a.business_id,
            "action": await an_action(connection, a.business_id),
            "executed": await an_executed_action(connection, a.business_id),
        }
        state = await refused(connection, statement, params)
    assert state == expected, name


async def test_a_reschedule_retires_one_booking_and_frees_its_slot(
    owner: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    # The positive control for the cases above: what PR 3's reschedule writes is accepted, and the
    # old slot can then be booked again.
    a, _ = pair
    params: dict[str, Any] = {"b": a.business_id}
    async with rolled_back(owner, a.business_id) as connection:
        params["action"] = await an_action(connection, a.business_id)
        await connection.execute(
            text(
                "UPDATE sandbox_bookings SET status = 'rescheduled', retired_by_action_id = :action"
                " WHERE business_id = :b AND id = 'B-101' AND status = 'active'"
            ),
            params,
        )
        await connection.execute(BOOK, params | {"id": "B-905", "slot": "S-09"})
        params["second"] = await an_action(connection, a.business_id)
        await connection.execute(
            BOOK, params | {"id": "B-906", "slot": "S-02", "action": params["second"]}
        )
        for action in (params["action"], params["second"]):  # PR 3 marks each executed
            await connection.execute(
                CONFIRM, {"b": a.business_id, "action": action, "trace": TRACE, "result": "{}"}
            )
        await connection.execute(AT_COMMIT)  # every action key holds, as at commit
        active = (
            await connection.execute(
                text(
                    "SELECT id FROM sandbox_bookings WHERE business_id = :b AND status = 'active'"
                    " ORDER BY id"
                ),
                params,
            )
        ).scalars()
        assert list(active) == ["B-102", "B-103", "B-905", "B-906"]


async def test_the_app_role_cannot_write_for_a_pending_action_nor_reopen_an_executed_one(
    app: AsyncEngine, pair: tuple[SandboxSeed, SandboxSeed]
) -> None:
    # zara's PR 1 review input, as the app role: a ticket for a still-pending action, and an
    # executed action that wrote a booking moved back to pending.
    a, _ = pair
    params = {"b": a.business_id, "hash": TOKEN_HASH, "trace": TRACE}
    async with rolled_back(app, a.business_id) as connection:
        pending = await connection.scalar(PENDING, params | {"arguments": "{}"})
        ticket = await refused(
            connection,
            "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
            " action_id) VALUES (:b, 'cust-001', 'other', 'x', 'action', :action)",
            {"b": a.business_id, "action": pending},
        )
        action = await connection.scalar(PENDING, params | {"arguments": '{"slot_id": "S-09"}'})
        await connection.execute(BOOK, params | {"id": "B-900", "slot": "S-09", "action": action})
        await connection.execute(CONFIRM, params | {"action": action, "result": "{}"})
        await connection.execute(AT_COMMIT)  # the positive control: executed, so it holds
        reopen = await refused(
            connection,
            "UPDATE pending_actions SET status = 'pending', finished_at = NULL, result = NULL"
            " WHERE business_id = :b AND id = :action",
            {"b": a.business_id, "action": action},
        )

    assert (ticket, reopen) == ("23503", "23503")  # foreign_key_violation, both
