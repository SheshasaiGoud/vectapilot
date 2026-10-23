"""The canary leakage suite: a hard gate (ADR 0009; business-context §5, §6). Integration only.

Two fictional businesses get a random canary in every business table, and the same `BAIT` chunk
(`read_paths.py`, "The bait") so that a search path has the other business's data to leak. The
tables come from the catalog (every relation with a `business_id` column, `catalog.py`), never
from a list here, so a new table is checked the day it is created: until `plant` seeds it, the
positive control in the reads below fails for it ("shows nothing of ..."). Every check also
proves itself on purpose: the `test_red_proof_*` tests break one protection inside a rolled-back
transaction (Postgres DDL is transactional) and assert the same check turns red. CI fails when
none of this ran (scripts/canary_gate.py).

As tests/test_rls.py: the owner (VECTAPILOT_MIGRATION_DATABASE_URL) is the bootstrap superuser
locally and in CI; it seeds the registry and breaks things on purpose. Everything checked for
leaks runs as the app role.
"""

import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, event, text
from sqlalchemy.engine.interfaces import AdaptedConnection, DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry, NullPool, PoolResetState

from tests.canary import checks
from tests.canary.catalog import (
    business_tables,
    load_foreign_keys,
    load_key_indexes,
    load_policies,
    load_relations,
    load_role_facts,
)
from tests.canary.checks import Planted
from tests.canary.read_paths import (
    BAIT,
    READ_PATHS,
    read_path_params,
    sql_on_every_business_table,
)
from tests.canary.write_paths import WRITE_PATHS
from tests.conftest import integration
from vectapilot_api import db
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import APP_ROLE, business_transaction, create_api_engine
from vectapilot_api.embeddings.base import EmbeddingSpec
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.kb.store import ONE_APPROVED_INDEX, vector_literal
from vectapilot_api.main import create_app

pytestmark = [integration, pytest.mark.anyio]

KNOWN_TABLES: Final = {"businesses", "documents", "chunks", "llm_calls"}
"""A floor, not the list: the catalog must find at least these (proves the query works)."""

VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
CURRENT_BUSINESS = text("SELECT current_setting('app.business_id', true)")
BACKEND_PID = text("SELECT pg_backend_pid()")

# Each business gets source `canary` version 1, approved: write_paths.approve_a_new_version
# approves version 2 of it, and the other business's version 1 must stay approved. Its chunk
# carries model id `fake`, not the configured one, so write_paths.reindex has something to do.
# Each business also gets source `bait`, approved and public, titled with its own canary: one chunk
# whose text is the shared BAIT, stored under the configured embedding model with the fake
# embedder's vector of BAIT. PR 6's API read paths ask BAIT, so the other business's bait is a
# same-model candidate scoring 1.0: a leaking route would show its title, the other canary.
PLANT: Final = (
    text(
        "INSERT INTO documents (business_id, source_id, version, title, source_type,"
        " content_kind, language, status, audience, content_sha256)"
        " VALUES (:b, 'canary', '1', :canary, 'markdown', 'faq', 'en', 'approved', 'public',"
        " repeat('c', 64))"
    ),
    text(
        "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
        " embedding_model, embedding_dim)"
        " SELECT :b, id, 0, :canary, CAST(:embedding AS vector), 'fake', 1024 FROM documents"
        " WHERE business_id = :b AND source_id = 'canary'"
    ),
    text(
        "INSERT INTO documents (business_id, source_id, version, title, source_type,"
        " content_kind, language, status, audience, content_sha256)"
        " VALUES (:b, 'bait', '1', :canary, 'markdown', 'faq', 'en', 'approved', 'public',"
        " repeat('d', 64))"
    ),
    text(
        "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
        " embedding_model, embedding_dim)"
        " SELECT :b, id, 0, :bait, CAST(:bait_embedding AS vector), :model,"
        " CAST(:dimension AS integer)"
        " FROM documents WHERE business_id = :b AND source_id = 'bait'"
    ),
    text(
        "INSERT INTO llm_calls (business_id, record_version, alias, catalog_version, outcome,"
        " attempts, retries, repairs, attempt_timeouts, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cost_usd,"
        " cost_complete, cache_hit, latency_ms, operation)"
        " VALUES (:b, 1, 'small', 'canary', 'ok', 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, true, false, 1,"
        " :canary)"
    ),
)
UPROOT: Final = (
    text("DELETE FROM llm_calls WHERE business_id = :b"),
    text("DELETE FROM documents WHERE business_id = :b"),  # chunks go with their document
    text("DELETE FROM businesses WHERE business_id = :b"),
)


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


def app_url() -> str:
    return str(Settings().database_url.get_secret_value())


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def fresh_engine() -> AsyncIterator[AsyncEngine]:
    """The app role; every connect opens a new backend (no pool)."""
    engine = create_async_engine(app_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def pooled_engine() -> AsyncIterator[AsyncEngine]:
    """The app role through one pooled connection, reused by every checkout, WITHOUT the
    pool-return reset: the reused-connection tests prove `business_transaction`'s
    transaction-local `set_config` on its own, and the red proofs need the bare pool."""
    engine = create_async_engine(app_url(), pool_pre_ping=True, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


@pytest.fixture
async def api_pooled_engine() -> AsyncIterator[AsyncEngine]:
    """The app role as main.py's engine (`db.create_api_engine`, with the pool-return reset),
    through one pooled connection reused by every checkout."""
    engine = create_api_engine(app_url(), pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


def configured_embedding() -> EmbeddingSpec:
    """The embedding model and dimension the app is configured with (as `build_embedder` resolves
    them), never a hard-coded id. Fake only: the bait's vector is the fake embedder's."""
    settings = Settings()
    assert settings.embeddings_provider == "fake", "the canary bait needs the fake embedder"
    return load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )


async def plant(owner: AsyncEngine, app: AsyncEngine, letter: str) -> Planted:
    planted = Planted(f"canary-{letter}-{secrets.token_hex(4)}", f"canary-{secrets.token_hex(8)}")
    spec = configured_embedding()
    params = {
        "b": planted.business_id,
        "canary": planted.canary,
        "embedding": VECTOR,
        "bait": BAIT,
        "bait_embedding": vector_literal(fake_vector(BAIT, spec.dimension)),
        "model": spec.model,
        "dimension": spec.dimension,
    }
    async with business_transaction(owner, planted.business_id) as connection:
        await connection.execute(text("INSERT INTO businesses (business_id) VALUES (:b)"), params)
    async with business_transaction(app, planted.business_id) as connection:  # the app role writes
        for statement in PLANT:
            await connection.execute(statement, params)
    return planted


@pytest.fixture
async def planted(
    owner_engine: AsyncEngine, fresh_engine: AsyncEngine
) -> AsyncIterator[tuple[Planted, Planted]]:
    seeded: list[Planted] = []
    try:
        seeded.append(await plant(owner_engine, fresh_engine, "a"))
        seeded.append(await plant(owner_engine, fresh_engine, "b"))
        yield seeded[0], seeded[1]
    finally:
        for business in seeded:
            async with business_transaction(owner_engine, business.business_id) as connection:
                for statement in UPROOT:
                    await connection.execute(statement, {"b": business.business_id})


async def visible_businesses(connection: AsyncConnection) -> dict[str, set[str]]:
    """Per business table, the business ids this connection can see."""
    seen: dict[str, set[str]] = {}
    for table in await business_tables(connection):
        result = await connection.exec_driver_sql(
            f"SELECT DISTINCT business_id::text FROM {table}"  # noqa: S608 (catalog name)
        )
        seen[table] = set(result.scalars().all())
    return seen


async def row_counts(connection: AsyncConnection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in await business_tables(connection):
        result = await connection.exec_driver_sql(f"SELECT count(*) FROM {table}")  # noqa: S608
        counts[table] = int(result.scalar_one())
    return counts


def only(seen: dict[str, set[str]], business: Planted) -> bool:
    """Every table shows rows, and only `business`'s (non-empty: the positive control)."""
    return bool(seen) and all(ids == {business.business_id} for ids in seen.values())


@asynccontextmanager
async def rolled_back(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """An owner connection whose every change, DDL included, is undone at the end."""
    async with engine.connect() as connection:
        try:
            yield connection
        finally:
            await connection.rollback()


async def owner_is_superuser(connection: AsyncConnection) -> bool:
    superuser = await connection.scalar(
        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    )
    return bool(superuser)


# ── The catalog: RLS, policies, keys, the app role ───────────────────────────────────────────


async def test_the_catalog_finds_the_business_tables(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        tables = await business_tables(connection)

    assert set(tables) >= KNOWN_TABLES, tables


async def test_every_business_table_has_rls_enabled_and_forced(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        assert checks.relation_problems(await load_relations(connection)) == []


async def test_every_policy_is_exactly_the_business_policy(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        tables = await business_tables(connection)
        assert checks.policy_problems(tables, await load_policies(connection)) == []


async def test_every_unique_key_starts_with_business_id(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        assert checks.key_problems(await load_key_indexes(connection)) == []


async def test_the_key_check_reads_the_partial_one_approved_version_index(
    owner_engine: AsyncEngine,
) -> None:
    # Migration 0004 (ADR 0010): the catalog query must see a partial index, with its WHERE, or
    # the check above would pass by not looking at it.
    async with owner_engine.connect() as connection:
        found = {k.name: k for k in await load_key_indexes(connection)}

    index = found[ONE_APPROVED_INDEX]
    assert (index.table, index.primary, index.columns) == (
        "documents",
        False,
        ("business_id", "source_id"),
    )
    assert index.predicate == "(status = 'approved'::text)"


async def test_foreign_keys_into_business_tables_keep_the_business(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        keys = await load_foreign_keys(connection)
        assert checks.foreign_key_problems(keys, await business_tables(connection)) == []


async def test_the_suite_runs_as_an_app_role_that_cannot_skip_rls(
    owner_engine: AsyncEngine, fresh_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        assert checks.app_role_problems(await load_role_facts(connection, APP_ROLE)) == []
    async with fresh_engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == APP_ROLE


# ── Reads: no business, the other business, fresh and reused connections ────────────────────


async def test_no_business_means_no_rows_on_a_fresh_connection(
    fresh_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    for _ in range(2):  # two separate backends
        async with fresh_engine.connect() as connection:
            assert await connection.scalar(CURRENT_BUSINESS) is None  # never set in this session
            counts = await row_counts(connection)
        assert counts and set(counts.values()) == {0}, counts


async def test_no_business_means_no_rows_on_a_reused_pooled_connection(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    for business in planted:
        async with business_transaction(pooled_engine, business.business_id) as connection:
            pid = await connection.scalar(BACKEND_PID)
            assert only(await visible_businesses(connection), business)  # it carried a business

        async with pooled_engine.connect() as connection:
            assert await connection.scalar(BACKEND_PID) == pid, "not the same connection"
            counts = await row_counts(connection)
        assert counts and set(counts.values()) == {0}, counts


@pytest.mark.parametrize("me", [0, 1], ids=["as a", "as b"])
async def test_a_business_sees_only_its_own_rows_on_a_fresh_connection(
    fresh_engine: AsyncEngine, planted: tuple[Planted, Planted], me: int
) -> None:
    async with business_transaction(fresh_engine, planted[me].business_id) as connection:
        seen = await visible_businesses(connection)

    assert only(seen, planted[me]), seen


@pytest.mark.parametrize("me", [0, 1], ids=["as a", "as b"])
async def test_a_business_sees_only_its_own_rows_on_a_reused_pooled_connection(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted], me: int
) -> None:
    mine, other = planted[me], planted[1 - me]
    async with business_transaction(pooled_engine, other.business_id) as connection:
        pid = await connection.scalar(BACKEND_PID)
        assert only(await visible_businesses(connection), other)  # served the other one first

    async with business_transaction(pooled_engine, mine.business_id) as connection:
        assert await connection.scalar(BACKEND_PID) == pid, "not the same connection"
        seen = await visible_businesses(connection)

    assert only(seen, mine), seen


@pytest.mark.parametrize("path", read_path_params())  # a pending stub is a strict xfail
@pytest.mark.parametrize("me", [0, 1], ids=["as a", "as b"])
async def test_no_read_path_shows_another_businesss_canary(
    fresh_engine: AsyncEngine, planted: tuple[Planted, Planted], me: int, path: str
) -> None:
    mine, other = planted[me], planted[1 - me]
    outputs = await READ_PATHS[path](fresh_engine, mine)

    assert checks.canary_leaks(outputs, mine, other) == []


# ── Writes through the API: never another business's rows, never without a business ────────


@pytest.fixture
def api() -> Iterator[TestClient]:
    """The real app, as the app role (Settings from the environment, as in CI)."""
    with TestClient(create_app(Settings())) as client:
        yield client


@pytest.mark.parametrize("path", sorted(WRITE_PATHS))
@pytest.mark.parametrize("me", [0, 1], ids=["as a", "as b"])
async def test_no_write_path_changes_another_businesss_rows(
    fresh_engine: AsyncEngine,
    planted: tuple[Planted, Planted],
    api: TestClient,
    me: int,
    path: str,
) -> None:
    mine, other = planted[me], planted[1 - me]
    other_before = await sql_on_every_business_table(fresh_engine, other)
    mine_before = await sql_on_every_business_table(fresh_engine, mine)

    response = WRITE_PATHS[path](api, mine.business_id)

    assert response.is_success, response.text
    assert await sql_on_every_business_table(fresh_engine, other) == other_before
    # The positive control: the path really wrote, for its own business.
    assert await sql_on_every_business_table(fresh_engine, mine) != mine_before


@pytest.mark.parametrize("path", sorted(WRITE_PATHS))
async def test_a_write_path_without_a_business_is_refused(
    fresh_engine: AsyncEngine, planted: tuple[Planted, Planted], api: TestClient, path: str
) -> None:
    before = [await sql_on_every_business_table(fresh_engine, b) for b in planted]

    response = WRITE_PATHS[path](api, None)

    assert response.status_code == 400, response.text
    assert [await sql_on_every_business_table(fresh_engine, b) for b in planted] == before


async def test_red_proof_a_write_into_another_business_is_caught(
    owner_engine: AsyncEngine, fresh_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    a, b = planted
    before = await sql_on_every_business_table(fresh_engine, b)
    # Acting as a through a role that skips RLS (the owner, a superuser here), with a filter that
    # reaches b too: the write the protections exist to stop.
    async with business_transaction(owner_engine, a.business_id) as connection:
        assert await owner_is_superuser(connection), "needs an owner that skips RLS"
        await connection.execute(
            text("UPDATE chunks SET embedding_model = 'red-proof' WHERE business_id IN (:a, :b)"),
            {"a": a.business_id, "b": b.business_id},
        )

    assert await sql_on_every_business_table(fresh_engine, b) != before


# ── The pool-return reset (M3 PR 1, ADR 0009 amendment): a session-level business ───────────
# `business_transaction` never sets one; these tests do it on purpose (the bug), return the
# connection to the pool and check it out again.


@dataclass(frozen=True)
class NextCheckout:
    same_backend: bool
    """The same Postgres backend as before the return (False: the pool opened a new one)."""
    seen: dict[str, set[str]]
    """Per business table, the business ids the next checkout can see."""


async def after_a_session_level_business(engine: AsyncEngine, business: Planted) -> NextCheckout:
    """Set `business` for the whole session (`false`), commit, return the connection; then what
    the next checkout sees."""
    async with engine.begin() as connection:
        pid = await connection.scalar(BACKEND_PID)
        await connection.execute(
            text("SELECT set_config('app.business_id', :b, false)"), {"b": business.business_id}
        )

    async with engine.connect() as connection:
        same = await connection.scalar(BACKEND_PID) == pid
        return NextCheckout(same, await visible_businesses(connection))


def nothing_visible(seen: dict[str, set[str]]) -> bool:
    return bool(seen) and all(ids == set() for ids in seen.values())


async def test_a_session_level_business_does_not_survive_the_pool(
    api_pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    after = await after_a_session_level_business(api_pooled_engine, planted[0])

    assert after.same_backend, "not the same connection: the test proves nothing"
    assert nothing_visible(after.seen), after.seen


async def test_red_proof_without_the_pool_reset_a_session_level_business_survives(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    a, _ = planted
    after = await after_a_session_level_business(pooled_engine, a)  # the bare pool: no reset

    assert after.same_backend, "not the same connection: the test proves nothing"
    assert only(after.seen, a), after.seen  # the next checkout is still business a


async def test_a_failed_pool_reset_throws_the_connection_away(
    api_pooled_engine: AsyncEngine,
    planted: tuple[Planted, Planted],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Postgres refuses an unknown parameter, so the hook raises on the real connection.
    monkeypatch.setattr(db, "RESET_BUSINESS", "RESET vectapilot_no_such_setting")

    after = await after_a_session_level_business(api_pooled_engine, planted[0])

    # SQLAlchemy logged it and invalidated the connection: the next checkout is a new backend.
    assert "Exception during reset or similar" in caplog.text
    assert not after.same_backend
    assert nothing_visible(after.seen), after.seen


def leave_a_transaction_open(business_id: str) -> Callable[[Connection], int]:
    """Through the DBAPI connection (SQLAlchemy's `Connection` never sees a transaction): set the
    business for the session and commit it, then start a new transaction and return the
    connection with it still open. The pool's own rollback then comes after the reset hook."""

    def run(connection: Connection) -> int:
        dbapi = connection.connection.dbapi_connection
        assert dbapi is not None
        cursor = dbapi.cursor()
        cursor.execute(
            "SELECT pg_backend_pid(), set_config('app.business_id', $1, false)", (business_id,)
        )
        row = cursor.fetchone()
        assert row is not None
        dbapi.commit()
        cursor.execute("SELECT 1")  # a new transaction, left open
        return int(row[0])

    return run


async def after_a_transaction_left_open(engine: AsyncEngine, business: Planted) -> NextCheckout:
    async with engine.connect() as connection:
        pid = await connection.run_sync(leave_a_transaction_open(business.business_id))

    async with engine.connect() as connection:
        same = await connection.scalar(BACKEND_PID) == pid
        return NextCheckout(same, await visible_businesses(connection))


async def test_the_pool_reset_is_not_undone_by_the_pools_own_rollback(
    api_pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    after = await after_a_transaction_left_open(api_pooled_engine, planted[0])

    assert after.same_backend, "not the same connection: the test proves nothing"
    assert nothing_visible(after.seen), after.seen


async def test_red_proof_a_reset_inside_the_open_transaction_is_rolled_back(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    # The hook without its rollback: the RESET runs inside the open transaction, the pool rolls
    # that back after the hook, and Postgres undoes the RESET with it.
    def reset_only(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        reset_state: PoolResetState,
    ) -> None:
        assert isinstance(dbapi_connection, AdaptedConnection)
        dbapi_connection.run_async(lambda connection: connection.execute(db.RESET_BUSINESS))

    event.listen(pooled_engine.sync_engine, "reset", reset_only)
    a, _ = planted
    after = await after_a_transaction_left_open(pooled_engine, a)

    assert after.same_backend, "not the same connection: the test proves nothing"
    assert only(after.seen, a), after.seen


async def after_a_close_without_commit(engine: AsyncEngine, business: Planted) -> NextCheckout:
    """Set `business` for the session and commit, then leave a new transaction open on the
    `Connection` and close it. `Connection.close()` rolls that back itself and tells the pool
    (`transaction_was_reset=True`), so the pool skips its own rollback. The commit is needed:
    Postgres undoes a session-level `set_config` with the transaction it ran in, and the test
    would then pass without any hook."""
    async with engine.connect() as connection:
        pid = await connection.scalar(BACKEND_PID)
        await connection.execute(
            text("SELECT set_config('app.business_id', :b, false)"), {"b": business.business_id}
        )
        await connection.commit()
        await connection.execute(text("SELECT 1"))  # autobegin: a new transaction, never committed
        assert connection.in_transaction()

    async with engine.connect() as connection:
        same = await connection.scalar(BACKEND_PID) == pid
        return NextCheckout(same, await visible_businesses(connection))


def record_reset_states(engine: AsyncEngine) -> list[bool]:
    """`transaction_was_reset` of every pool return, in order (a listener after the hook)."""
    states: list[bool] = []

    def record(
        dbapi_connection: DBAPIConnection,
        connection_record: ConnectionPoolEntry,
        reset_state: PoolResetState,
    ) -> None:
        states.append(reset_state.transaction_was_reset)

    event.listen(engine.sync_engine, "reset", record)
    return states


async def test_the_pool_reset_runs_when_the_connection_rolled_back_itself(
    api_pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    states = record_reset_states(api_pooled_engine)

    after = await after_a_close_without_commit(api_pooled_engine, planted[0])

    assert states[0] is True, "not the path where the Connection rolled back: proves nothing"
    assert after.same_backend, "not the same connection: the test proves nothing"
    assert nothing_visible(after.seen), after.seen


async def test_red_proof_a_close_without_commit_keeps_a_committed_session_business(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    # The bare pool: the Connection's own rollback does not undo the committed session setting.
    a, _ = planted
    after = await after_a_close_without_commit(pooled_engine, a)

    assert after.same_backend, "not the same connection: the test proves nothing"
    assert only(after.seen, a), after.seen


# ── Proved on purpose: break one protection, the same check turns red ────────────────────────


async def test_red_proof_a_role_that_skips_rls_leaks_the_canary(
    owner_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    a, b = planted
    async with owner_engine.connect() as connection:
        assert await owner_is_superuser(connection), "needs an owner that skips RLS"
        owner = str(await connection.scalar(text("SELECT current_user")))
        # Connecting as the owner: the role check is red...
        assert "is a superuser" in checks.app_role_problems(
            await load_role_facts(connection, owner)
        )

    # ...and so is the canary check, reading through it.

    leaks = checks.canary_leaks(await sql_on_every_business_table(owner_engine, a), a, b)
    assert any(b.canary in problem for problem in leaks), leaks


async def test_red_proof_dropping_force(owner_engine: AsyncEngine) -> None:
    async with rolled_back(owner_engine) as connection:
        await connection.exec_driver_sql("ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY")
        problems = checks.relation_problems(await load_relations(connection))

    assert problems == ["chunks: row-level security is not forced (its owner skips it)"]


async def test_red_proof_a_unique_key_without_business_id(owner_engine: AsyncEngine) -> None:
    async with rolled_back(owner_engine) as connection:
        await connection.exec_driver_sql(
            "CREATE UNIQUE INDEX canary_probe_key ON chunks (document_id, chunk_index)"
        )
        problems = checks.key_problems(await load_key_indexes(connection))

    assert len(problems) == 1 and "canary_probe_key" in problems[0], problems


async def test_red_proof_a_partial_unique_key_without_business_id(
    owner_engine: AsyncEngine,
) -> None:
    # Migration 0004's index with business_id forgotten: a predicate does not make it safe.
    async with rolled_back(owner_engine) as connection:
        await connection.exec_driver_sql(
            "CREATE UNIQUE INDEX canary_probe_partial_key ON documents (source_id)"
            " WHERE status = 'approved'"
        )
        problems = checks.key_problems(await load_key_indexes(connection))

    assert len(problems) == 1 and "canary_probe_partial_key" in problems[0], problems
    assert "WHERE (status = 'approved'::text)" in problems[0], problems


async def test_red_proof_an_open_extra_policy(owner_engine: AsyncEngine) -> None:
    async with rolled_back(owner_engine) as connection:
        await connection.exec_driver_sql("CREATE POLICY canary_probe_open ON chunks USING (true)")
        problems = checks.policy_problems(
            await business_tables(connection), await load_policies(connection)
        )

    assert len(problems) == 1 and "canary_probe_open" in problems[0], problems


async def test_red_proof_an_app_role_that_owns_a_table(owner_engine: AsyncEngine) -> None:
    async with rolled_back(owner_engine) as connection:
        await connection.exec_driver_sql(f"ALTER TABLE chunks OWNER TO {APP_ROLE}")
        problems = checks.app_role_problems(await load_role_facts(connection, APP_ROLE))

    assert len(problems) == 1 and problems[0].startswith("owns "), problems
