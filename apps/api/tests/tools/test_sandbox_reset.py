"""M5 PR 1: the sandbox reset against real Postgres, on the owner's connection. Integration only.

The owner is the bootstrap superuser locally and in CI (ADR 0009, "Limit"): RLS never applies to
it, so only each statement's own `business_id = :business_id` keeps a reset inside its business.
Here: resetting A leaves every other row of every business table byte-for-byte the same (B's
sandbox and every M2/M3 table), keeps A's audit rows, and really replaces A's rows (the positive
control); and the same check turns red when one DELETE loses its clause (the red proof).
"""

import asyncio
import io
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import integration
from tests.tools import sandbox_db
from tests.tools.helpers import copy_sandbox
from tests.tools.sandbox_db import PAWS_SEED, fingerprint, fresh_engine, owner_url, seeded
from vectapilot_api.db import business_transaction
from vectapilot_api.tools import cli, sandbox
from vectapilot_api.tools.sandbox import SandboxRefused, reset_sandbox

pytestmark = [integration]

VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
TOKEN_HASH: Final = "a" * 64

# Rows a reset must not touch (M2/M3 tables and the audit) and rows it must replace (an action of
# A's that the seed does not hold), planted as the owner.
PLANT_M2: Final = (
    text(
        "INSERT INTO documents (business_id, source_id, version, title, source_type,"
        " content_kind, language, status, audience, content_sha256)"
        " VALUES (:b, 'faq', '1', :b, 'markdown', 'faq', 'en', 'approved', 'public',"
        " repeat('a', 64))"
    ),
    text(
        "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
        " embedding_model, embedding_dim, ts_config)"
        " SELECT :b, id, 0, :b, CAST(:vector AS vector), 'fake', 1024,"
        " CAST('english' AS regconfig) FROM documents WHERE business_id = :b"
    ),
    text(
        "INSERT INTO llm_calls (business_id, record_version, alias, catalog_version, outcome,"
        " attempts, retries, repairs, attempt_timeouts, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cost_usd,"
        " cost_complete, cache_hit, latency_ms) VALUES (:b, 1, 'small', 'x', 'ok', 1, 0, 0, 0,"
        " 1, 1, 0, 0, 0, 0, true, false, 1)"
    ),
    text(
        "INSERT INTO tool_audit (business_id, tool, kind, phase, outcome, duration_ms)"
        " VALUES (:b, 'check_slots', 'read', 'run', 'ok', 1.5)"
    ),
)
PLANT_ACTION: Final = (
    text(
        "INSERT INTO pending_actions (business_id, tool, arguments, customer_ref,"
        " confirm_token_sha256, status, expires_at, confirmed_at, confirm_trace_id, finished_at,"
        " result) VALUES (:b, 'book', '{\"slot_id\": \"S-09\"}', 'cust-001', :hash, 'executed',"
        " now() + interval '15 minutes', now(), :trace, now(), '{\"booking_id\": \"B-900\"}')"
        " RETURNING id"
    ),
    text(
        "INSERT INTO sandbox_bookings (business_id, id, slot_id, customer_ref, status, origin,"
        " action_id) VALUES (:b, 'B-900', 'S-09', 'cust-001', 'active', 'action', :action)"
    ),
    text(
        "INSERT INTO sandbox_tickets (business_id, customer_ref, category, summary, origin,"
        " action_id) VALUES (:b, 'cust-001', 'other', :b, 'action', :action)"
    ),
)
COUNTS: Final = text(
    "SELECT (SELECT count(*) FROM sandbox_slots WHERE business_id = :b),"
    " (SELECT count(*) FROM sandbox_bookings WHERE business_id = :b),"
    " (SELECT count(*) FROM sandbox_orders WHERE business_id = :b),"
    " (SELECT count(*) FROM sandbox_tickets WHERE business_id = :b),"
    " (SELECT count(*) FROM pending_actions WHERE business_id = :b),"
    " (SELECT count(*) FROM tool_audit WHERE business_id = :b)"
)
SEEDED: Final = (len(PAWS_SEED.slots), len(PAWS_SEED.bookings), len(PAWS_SEED.orders), 0, 0)


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


async def owner_is_superuser(engine: AsyncEngine) -> bool:
    async with engine.connect() as connection:
        return bool(
            await connection.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
        )


async def plant_extra(owner: AsyncEngine, business_id: str) -> None:
    """M2 rows and an audit row (a reset keeps them), and an executed action with its booking
    and ticket (a reset removes them)."""
    params = {"b": business_id, "vector": VECTOR, "hash": TOKEN_HASH, "trace": "1" * 32}
    async with business_transaction(owner, business_id) as connection:
        for statement in PLANT_M2:
            await connection.execute(statement, params)
        action = await connection.scalar(PLANT_ACTION[0], params)
        for statement in PLANT_ACTION[1:]:
            await connection.execute(statement, params | {"action": action})


async def counts(owner: AsyncEngine, business_id: str) -> tuple[int, ...]:
    async with owner.connect() as connection:
        return tuple((await connection.execute(COUNTS, {"b": business_id})).one())


@pytest.mark.anyio
async def test_a_reset_of_a_leaves_every_other_row_byte_for_byte_unchanged(
    owner: AsyncEngine,
) -> None:
    assert await owner_is_superuser(owner), "needs an owner that skips RLS"
    async with seeded(owner, "a", "b") as (a, b):
        for business in (a, b):
            await plant_extra(owner, business.business_id)
        assert await counts(owner, a.business_id) == (*SEEDED[:1], 4, 4, 1, 1, 1)
        async with owner.connect() as connection:
            before = await fingerprint(connection, except_reset_rows_of=a.business_id)

        await cli.reset_with_owner(owner_url(), a)  # the command's own path, committed

        async with owner.connect() as connection:
            after = await fingerprint(connection, except_reset_rows_of=a.business_id)
        a_counts = await counts(owner, a.business_id)
        b_counts = await counts(owner, b.business_id)

    assert set(before) >= {*sandbox.RESET_TABLES, "tool_audit", "documents", "chunks", "llm_calls"}
    assert after == before  # B's sandbox, every M2/M3 table, every audit row
    # The positive control: A's action rows are gone, A's seed is back, A's audit row stays.
    assert a_counts == (*SEEDED, 1)
    assert b_counts == (len(PAWS_SEED.slots), 4, 4, 1, 1, 1)


@pytest.mark.anyio
async def test_red_proof_a_delete_without_its_business_clause_is_caught(
    owner: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    clause = " WHERE business_id = :business_id"
    broken = tuple(
        text(str(statement).replace(clause, ""))
        if "sandbox_orders" in str(statement)
        else statement
        for statement in sandbox.DELETES
    )
    assert broken != sandbox.DELETES, "the reset's DELETE changed: update this proof"
    monkeypatch.setattr(sandbox, "DELETES", broken)
    async with seeded(owner, "a", "b") as (a, _), owner.connect() as connection:
        try:
            before = await fingerprint(connection, except_reset_rows_of=a.business_id)
            await connection.execute(
                text("SELECT set_config('app.business_id', :b, true)"), {"b": a.business_id}
            )
            await reset_sandbox(connection, a)  # inside this transaction, rolled back
            after = await fingerprint(connection, except_reset_rows_of=a.business_id)
        finally:
            await connection.rollback()

    assert after["sandbox_orders"] != before["sandbox_orders"]  # B's orders were deleted too
    assert {t: p for t, p in after.items() if t != "sandbox_orders"} == {
        t: p for t, p in before.items() if t != "sandbox_orders"
    }


@pytest.mark.anyio
async def test_the_reset_refuses_a_connection_for_another_business_or_none(
    owner: AsyncEngine,
) -> None:
    async with seeded(owner, "a", "b") as (a, b):
        before = await counts(owner, b.business_id)
        with pytest.raises(SandboxRefused):
            async with business_transaction(owner, b.business_id) as connection:
                await reset_sandbox(connection, a)
        with pytest.raises(SandboxRefused):
            async with owner.begin() as connection:
                await reset_sandbox(connection, a)
        after = await counts(owner, b.business_id)

    assert before == after


@pytest.mark.anyio
async def test_a_reset_is_repeatable(owner: AsyncEngine) -> None:
    async with seeded(owner, "a") as (a,):
        first = await counts(owner, a.business_id)
        await cli.reset_with_owner(owner_url(), a)
        second = await counts(owner, a.business_id)
        async with owner.connect() as connection:
            labels = set(
                (
                    await connection.execute(
                        text("SELECT label FROM sandbox_slots WHERE business_id = :b"),
                        {"b": a.business_id},
                    )
                ).scalars()
            )

    assert first == second == (*SEEDED, 0)
    assert labels == {slot.label for slot in PAWS_SEED.slots}
    assert any(PAWS_SEED.canary in label for label in labels)


def test_the_command_resets_a_business_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sync on purpose: the command runs its own event loop (asyncio.run). A throwaway copy of the
    # committed folder, Paws & Co renamed, through `main` with the real settings and owner URL.
    # The environment is required (no default), so the test names it.
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "test")
    business = f"sbx-cli-{secrets.token_hex(4)}"
    folder = copy_sandbox(tmp_path / "sandbox", {"paws-and-co": business})
    out, err = io.StringIO(), io.StringIO()

    async def check_and_uproot() -> tuple[int, ...]:
        engine = fresh_engine(owner_url())
        try:
            return await counts(engine, business)
        finally:
            await sandbox_db.uproot(engine, business)
            await engine.dispose()

    try:
        code = cli.main(
            ["sandbox", "reset", "--business", business, "--config-dir", str(folder)],
            out=out,
            err=err,
        )
    finally:
        found = asyncio.run(check_and_uproot())

    assert (code, err.getvalue()) == (0, "")
    assert out.getvalue().startswith(f"sandbox reset: {business}: 12 slots, 3 bookings, 4 orders")
    assert found == (*SEEDED, 0)
