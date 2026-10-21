"""The canary leakage suite: a hard gate (ADR 0009; business-context §5, §6). Integration only.

Two fictional businesses get a random canary in every business table. The tables come from the
catalog (every relation with a `business_id` column, `catalog.py`), never from a list here, so a
new table is checked the day it is created: until `plant` seeds it, the positive control in the
reads below fails for it ("shows nothing of ..."). Every check also proves itself on purpose: the
`test_red_proof_*` tests break one protection inside a rolled-back transaction (Postgres DDL is
transactional) and assert the same check turns red. CI fails when none of this ran
(scripts/canary_gate.py).

As tests/test_rls.py: the owner (VECTAPILOT_MIGRATION_DATABASE_URL) is the bootstrap superuser
locally and in CI; it seeds the registry and breaks things on purpose. Everything checked for
leaks runs as the app role.
"""

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

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
from tests.canary.read_paths import READ_PATHS, sql_on_every_business_table
from tests.canary.write_paths import WRITE_PATHS
from tests.conftest import integration
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import APP_ROLE, business_transaction
from vectapilot_api.main import create_app

pytestmark = [integration, pytest.mark.anyio]

KNOWN_TABLES: Final = {"businesses", "documents", "chunks", "llm_calls"}
"""A floor, not the list: the catalog must find at least these (proves the query works)."""

VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
CURRENT_BUSINESS = text("SELECT current_setting('app.business_id', true)")
BACKEND_PID = text("SELECT pg_backend_pid()")

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
    """The app role through one pooled connection, reused by every checkout (as main.py's)."""
    engine = create_async_engine(app_url(), pool_pre_ping=True, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


async def plant(owner: AsyncEngine, app: AsyncEngine, letter: str) -> Planted:
    planted = Planted(f"canary-{letter}-{secrets.token_hex(4)}", f"canary-{secrets.token_hex(8)}")
    params = {"b": planted.business_id, "canary": planted.canary, "embedding": VECTOR}
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


@pytest.mark.parametrize("path", sorted(READ_PATHS))
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


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason="known gap (PR 2 nit 3, shesha decides): nothing RESETs app.business_id when a"
    " connection returns to the pool, so a session-level set_config/SET survives into the next"
    " checkout. business_transaction never does one; this turns red (XPASS) once a RESET lands.",
)
async def test_a_session_level_business_does_not_survive_the_pool(
    pooled_engine: AsyncEngine, planted: tuple[Planted, Planted]
) -> None:
    a, _ = planted
    async with pooled_engine.begin() as connection:  # the bug: `false` = for the whole session
        pid = await connection.scalar(BACKEND_PID)
        await connection.execute(
            text("SELECT set_config('app.business_id', :b, false)"), {"b": a.business_id}
        )

    async with pooled_engine.connect() as connection:
        if await connection.scalar(BACKEND_PID) != pid:
            pytest.fail("not the same connection: the test proves nothing")  # not an xfail
        counts = await row_counts(connection)
    assert set(counts.values()) == {0}, counts


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
