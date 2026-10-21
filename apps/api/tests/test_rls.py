"""Row-level security and the database roles, against real Postgres (ADR 0009). Integration only.

Two fictional businesses are seeded per test, each with its own random canary string, and removed
afterwards. The catalog-wide canary suite (every business table and read path) is tests/canary/.

The owner (VECTAPILOT_MIGRATION_DATABASE_URL) is the stack's bootstrap superuser, locally and in
CI: it seeds the registry and creates throwaway roles. Everything under test runs as the app role.
"""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx2
import pytest
from sqlalchemy import URL, TextClause, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import integration, make_settings
from tests.test_business_header import UNKNOWN, probe_app
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import APP_ROLE, Database, UnsafeDatabaseRole, business_transaction
from vectapilot_api.db_provision import provision, set_login_password
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.main import create_app

pytestmark = [integration, pytest.mark.anyio]

TABLES: Final = ("businesses", "documents", "chunks", "llm_calls")
VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
RLS_REFUSED: Final = "violates row-level security policy"

CURRENT_BUSINESS = text("SELECT current_setting('app.business_id', true)")
BACKEND_PID = text("SELECT pg_backend_pid()")
COUNTS: Final = {
    "businesses": text("SELECT count(*) FROM businesses"),
    "documents": text("SELECT count(*) FROM documents"),
    "chunks": text("SELECT count(*) FROM chunks"),
    "llm_calls": text("SELECT count(*) FROM llm_calls"),
}
COUNTS_OF: Final = {
    "businesses": text("SELECT count(*) FROM businesses WHERE business_id = :b"),
    "documents": text("SELECT count(*) FROM documents WHERE business_id = :b"),
    "chunks": text("SELECT count(*) FROM chunks WHERE business_id = :b"),
    "llm_calls": text("SELECT count(*) FROM llm_calls WHERE business_id = :b"),
}
VISIBLE: Final = {
    "businesses": text("SELECT business_id, business_id FROM businesses"),
    "documents": text("SELECT business_id, title FROM documents"),
    "chunks": text("SELECT business_id, content FROM chunks"),
    "llm_calls": text("SELECT business_id, operation FROM llm_calls"),
}
INSERT_BUSINESS = text("INSERT INTO businesses (business_id) VALUES (:b)")
INSERT_DOCUMENT = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, language,"
    " status, audience, content_sha256)"
    " VALUES (:b, 'faq', '1', :title, 'faq', 'en', 'approved', 'public', repeat('a', 64))"
    " RETURNING id"
)
INSERT_CHUNK = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, page, embedding,"
    " embedding_model, embedding_dim)"
    " VALUES (:b, :document_id, 0, :content, 1, CAST(:embedding AS vector), 'fake', 1024)"
)
INSERT_LLM_CALL = text(
    "INSERT INTO llm_calls (business_id, record_version, alias, model_requested, catalog_version,"
    " outcome, attempts, retries, repairs, attempt_timeouts, input_tokens, output_tokens,"
    " cache_read_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cost_usd, cost_complete,"
    " cache_hit, latency_ms, operation)"
    " VALUES (:b, 1, 'small', 'fake-small', 'test', 'ok', 1, 0, 0, 0, 10, 5, 0, 0, 0, 0, true,"
    " false, 12.5, :operation)"
)
CLEANUP: Final = (
    text("DELETE FROM llm_calls WHERE business_id = :b"),
    text("DELETE FROM documents WHERE business_id = :b"),  # chunks go with their document
    text("DELETE FROM businesses WHERE business_id = :b"),
)


@dataclass(frozen=True)
class Business:
    business_id: str
    canary: str
    document_id: Any


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
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_url())
    yield engine
    await engine.dispose()


async def seed(owner: AsyncEngine, app: AsyncEngine, letter: str) -> Business:
    business_id = f"canary-{letter}-{secrets.token_hex(4)}"
    canary = f"canary-{secrets.token_hex(8)}"
    async with business_transaction(owner, business_id) as connection:  # the registry is owner's
        await connection.execute(INSERT_BUSINESS, {"b": business_id})
    async with business_transaction(app, business_id) as connection:  # the app role writes
        document_id = await connection.scalar(INSERT_DOCUMENT, {"b": business_id, "title": canary})
        await connection.execute(
            INSERT_CHUNK,
            {"b": business_id, "document_id": document_id, "content": canary, "embedding": VECTOR},
        )
        await connection.execute(INSERT_LLM_CALL, {"b": business_id, "operation": canary})
    return Business(business_id, canary, document_id)


@pytest.fixture
async def businesses(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> AsyncIterator[tuple[Business, Business]]:
    seeded: list[Business] = []
    try:
        seeded.append(await seed(owner_engine, app_engine, "a"))
        seeded.append(await seed(owner_engine, app_engine, "b"))
        yield seeded[0], seeded[1]
    finally:
        for business in seeded:
            async with business_transaction(owner_engine, business.business_id) as connection:
                for statement in CLEANUP:
                    await connection.execute(statement, {"b": business.business_id})


async def refused(
    engine: AsyncEngine, business_id: str, statement: TextClause, **params: Any
) -> str:
    """Run `statement` acting as `business_id`; it must fail. Returns the database's message."""
    with pytest.raises(DBAPIError) as error:
        async with business_transaction(engine, business_id) as connection:
            await connection.execute(statement, params)
    return str(error.value.orig)


# ── RLS: no business, other businesses, pooled connections ──────────────────────────────────


async def test_no_business_set_means_no_rows(
    app_engine: AsyncEngine, businesses: tuple[Business, Business]
) -> None:
    a, _ = businesses
    async with app_engine.connect() as connection:
        assert await connection.scalar(CURRENT_BUSINESS) in (None, "")
        for table, count in COUNTS.items():
            assert await connection.scalar(count) == 0, table

    with pytest.raises(DBAPIError, match=RLS_REFUSED):
        async with app_engine.begin() as connection:
            await connection.execute(INSERT_DOCUMENT, {"b": a.business_id, "title": "x"})


@pytest.mark.parametrize("me", [0, 1], ids=["a reads", "b reads"])
async def test_a_business_reads_only_its_own_rows(
    app_engine: AsyncEngine, businesses: tuple[Business, Business], me: int
) -> None:
    mine, other = businesses[me], businesses[1 - me]
    async with business_transaction(app_engine, mine.business_id) as connection:
        for table, query in VISIBLE.items():
            rows = (await connection.execute(query)).all()
            assert {row[0] for row in rows} == {mine.business_id}, table
            assert other.canary not in str(rows), table
            assert await connection.scalar(COUNTS_OF[table], {"b": other.business_id}) == 0
        assert mine.canary in str((await connection.execute(VISIBLE["chunks"])).all())


async def test_a_business_cannot_write_another_businesss_rows(
    app_engine: AsyncEngine, businesses: tuple[Business, Business]
) -> None:
    a, b = businesses
    me = a.business_id

    # New rows for B: refused by WITH CHECK.
    assert RLS_REFUSED in await refused(app_engine, me, INSERT_DOCUMENT, b=b.business_id, title="x")
    assert RLS_REFUSED in await refused(
        app_engine, me, INSERT_LLM_CALL, b=b.business_id, operation="x"
    )
    assert RLS_REFUSED in await refused(
        app_engine,
        me,
        INSERT_CHUNK,
        b=b.business_id,
        document_id=b.document_id,
        content="x",
        embedding=VECTOR,
    )
    # A's own chunk pointing at B's document: refused by the composite foreign key.
    assert "violates foreign key constraint" in await refused(
        app_engine, me, INSERT_CHUNK, b=me, document_id=b.document_id, content="x", embedding=VECTOR
    )
    # Moving A's row to B: refused by WITH CHECK (documents: the app role may UPDATE them).
    move = text("UPDATE documents SET business_id = :other WHERE business_id = :b")
    assert RLS_REFUSED in await refused(app_engine, me, move, other=b.business_id, b=me)
    # llm_calls is append-only: the app role has no UPDATE on it at all, checked before RLS.
    rewrite = text("UPDATE llm_calls SET business_id = :other WHERE business_id = :b")
    assert "permission denied for table llm_calls" in await refused(
        app_engine, me, rewrite, other=b.business_id, b=me
    )

    # Changing or deleting B's rows: they are invisible, so nothing matches.
    async with business_transaction(app_engine, me) as connection:
        update = text("UPDATE documents SET title = 'changed' WHERE business_id = :b")
        assert (await connection.execute(update, {"b": b.business_id})).rowcount == 0
        delete = text("DELETE FROM chunks WHERE business_id = :b")
        assert (await connection.execute(delete, {"b": b.business_id})).rowcount == 0

    async with business_transaction(app_engine, b.business_id) as connection:
        for table, query in VISIBLE.items():
            rows = (await connection.execute(query)).all()
            assert len(rows) == 1 and rows[0][0] == b.business_id, table
        assert b.canary in str((await connection.execute(VISIBLE["documents"])).all())


async def test_a_reused_pooled_connection_keeps_no_business(
    businesses: tuple[Business, Business],
) -> None:
    a, b = businesses
    engine = create_async_engine(app_url(), pool_size=1, max_overflow=0)  # one connection, reused
    try:
        async with business_transaction(engine, a.business_id) as connection:
            pid = await connection.scalar(BACKEND_PID)
            assert await connection.scalar(CURRENT_BUSINESS) == a.business_id
            assert await connection.scalar(COUNTS["chunks"]) == 1

        async with engine.connect() as connection:
            assert await connection.scalar(BACKEND_PID) == pid, "not the same connection"
            assert await connection.scalar(CURRENT_BUSINESS) == ""  # the local value ended
            for table, count in COUNTS.items():
                assert await connection.scalar(count) == 0, table

        with pytest.raises(RuntimeError):  # a rollback ends it too
            async with business_transaction(engine, a.business_id):
                raise RuntimeError
        async with business_transaction(engine, b.business_id) as connection:
            assert await connection.scalar(BACKEND_PID) == pid
            rows = (await connection.execute(VISIBLE["chunks"])).all()
            assert {row[0] for row in rows} == {b.business_id}
    finally:
        await engine.dispose()


# ── The schema and the app role ──────────────────────────────────────────────────────────────


async def test_every_table_has_forced_rls_and_the_business_policy(
    owner_engine: AsyncEngine,
) -> None:
    async with owner_engine.connect() as connection:
        for table in TABLES:
            enabled, forced = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                        " WHERE relname = :t"
                    ),
                    {"t": table},
                )
            ).one()
            assert (enabled, forced) == (True, True), table
            nullable = await connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns"
                    " WHERE table_name = :t AND column_name = 'business_id'"
                ),
                {"t": table},
            )
            assert nullable == "NO", table
            policies = (
                await connection.execute(
                    text("SELECT cmd, qual, with_check FROM pg_policies WHERE tablename = :t"),
                    {"t": table},
                )
            ).all()
            assert len(policies) == 1, table
            command, using, check = policies[0]
            expected = (
                "business_id = nullif(current_setting('app.business_id'::text, true), ''::text)"
            )
            assert command == "ALL"
            assert expected in using.lower() and expected in check.lower(), table


async def test_the_app_role_is_safe_and_owns_nothing(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        attributes = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication,"
                    " rolinherit, rolcanlogin FROM pg_roles WHERE rolname = :r"
                ),
                {"r": APP_ROLE},
            )
        ).one()
        memberships = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member"
                " WHERE r.rolname = :r"
            ),
            {"r": APP_ROLE},
        )
        owned = await connection.scalar(
            text(
                "SELECT (SELECT count(*) FROM pg_class WHERE relowner = r.oid)"
                " + (SELECT count(*) FROM pg_namespace WHERE nspowner = r.oid)"
                " + (SELECT count(*) FROM pg_proc WHERE proowner = r.oid)"
                " + (SELECT count(*) FROM pg_type WHERE typowner = r.oid)"
                " + (SELECT count(*) FROM pg_database WHERE datdba = r.oid)"
                " FROM pg_roles r WHERE r.rolname = :r"
            ),
            {"r": APP_ROLE},
        )
    assert tuple(attributes) == (False, False, False, False, False, False, True)
    assert memberships == 0
    assert owned == 0

    async with app_engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == APP_ROLE
        grants = (
            await connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema.role_table_grants"
                    " WHERE grantee = current_user"
                )
            )
        ).all()
        can_create = await connection.scalar(
            text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
        )
    write = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert {tuple(row) for row in grants} == (
        {("businesses", "SELECT"), ("llm_calls", "SELECT"), ("llm_calls", "INSERT")}
        | {("documents", privilege) for privilege in write}
        | {("chunks", privilege) for privilege in write}
    )
    assert can_create is False


async def test_llm_calls_has_the_call_record_fields(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        columns = (
            await connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'llm_calls'"
                )
            )
        ).all()

    assert set(columns) == set(CallRecord.model_fields) | {"id", "created_at"}


async def test_vectors_round_trip_as_text(
    app_engine: AsyncEngine, businesses: tuple[Business, Business]
) -> None:
    a, _ = businesses
    async with business_transaction(app_engine, a.business_id) as connection:
        stored, dim = (
            await connection.execute(
                text("SELECT CAST(embedding AS text), embedding_dim FROM chunks")
            )
        ).one()
    assert [float(x) for x in stored.strip("[]").split(",")] == [0.0] * 1023 + [1.0]
    assert dim == 1024

    wrong = await refused(
        app_engine,
        a.business_id,
        INSERT_CHUNK,
        b=a.business_id,
        document_id=a.document_id,
        content="x",
        embedding="[1,2,3]",
    )
    assert "expected 1024 dimensions" in wrong


# ── Startup refuses roles that skip RLS ──────────────────────────────────────────────────────


@asynccontextmanager
async def throwaway_login(
    owner: AsyncEngine, attributes: str = "", parent_attributes: str | None = None
) -> AsyncIterator[str]:
    """A login role that exists only inside the block; yields its database URL. With
    `parent_attributes`, it is also a member of a NOLOGIN role `<name>_parent` with those."""
    name = f"vectapilot_test_{secrets.token_hex(4)}"
    parent = f"{name}_parent"
    password = secrets.token_urlsafe(16)
    async with owner.begin() as connection:
        await connection.exec_driver_sql(f"CREATE ROLE {name} NOLOGIN {attributes}")
        if parent_attributes is not None:
            await connection.exec_driver_sql(f"CREATE ROLE {parent} NOLOGIN {parent_attributes}")
            await connection.exec_driver_sql(f"GRANT {parent} TO {name}")
    try:
        await set_login_password(owner, name, password)
        url = make_url(owner_url()).set(username=name, password=password)
        yield url.render_as_string(hide_password=False)
    finally:
        async with owner.begin() as connection:
            await connection.exec_driver_sql(f"DROP ROLE {name}")
            await connection.exec_driver_sql(f"DROP ROLE IF EXISTS {parent}")


async def start(database_url: str) -> Database:
    """Run the app's startup (lifespan) with `database_url`; return its Database if it started."""
    app = create_app(make_settings(database_url=database_url, readiness_timeout_seconds=5))
    async with app.router.lifespan_context(app):
        database: Database = app.state.database
        return database


async def test_startup_refuses_a_superuser(owner_engine: AsyncEngine) -> None:
    async with owner_engine.connect() as connection:
        is_superuser = await connection.scalar(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        )
    assert is_superuser is True, "this test needs the owner to be the bootstrap superuser"

    with pytest.raises(UnsafeDatabaseRole, match="is a superuser"):
        await start(owner_url())


async def test_startup_refuses_bypassrls(owner_engine: AsyncEngine) -> None:
    async with throwaway_login(owner_engine, "BYPASSRLS") as url:
        with pytest.raises(UnsafeDatabaseRole, match="has BYPASSRLS"):
            await start(url)


async def test_startup_refuses_a_member_of_a_bypassrls_role(owner_engine: AsyncEngine) -> None:
    async with throwaway_login(owner_engine, parent_attributes="BYPASSRLS") as url:
        with pytest.raises(UnsafeDatabaseRole, match="is a member of a superuser or BYPASSRLS"):
            await start(url)


@asynccontextmanager
async def owned_table(owner: AsyncEngine, role: str) -> AsyncIterator[None]:
    """A table in `public` owned by `role`, dropped at the end of the block (before the role)."""
    table = f"{role}_owned"
    async with owner.begin() as connection:
        await connection.exec_driver_sql(f"CREATE TABLE public.{table} (id integer)")
        await connection.exec_driver_sql(f"ALTER TABLE public.{table} OWNER TO {role}")
    try:
        yield
    finally:
        async with owner.begin() as connection:
            await connection.exec_driver_sql(f"DROP TABLE public.{table}")


@pytest.mark.parametrize("through_parent", [False, True], ids=["owner", "member of the owner"])
async def test_startup_refuses_a_role_that_can_act_as_a_table_owner(
    owner_engine: AsyncEngine, through_parent: bool
) -> None:
    # Otherwise a safe role: the table alone must stop startup (an owner can switch RLS off).
    async with throwaway_login(owner_engine, parent_attributes="") as url:
        name = make_url(url).username
        assert name is not None
        async with owned_table(owner_engine, f"{name}_parent" if through_parent else name):
            with pytest.raises(UnsafeDatabaseRole) as error:
                await start(url)
    only_problem = "the API's database role owns a table or other relation, or is a member of its"
    assert str(error.value).startswith(f"{only_problem} owner: row-level security")


async def test_startup_accepts_the_app_role() -> None:
    database = await start(app_url())

    assert database.role_verified is True


# ── Provisioning and the X-Business-Id dependency ────────────────────────────────────────────


async def test_provisioning_is_repeatable(app_engine: AsyncEngine) -> None:
    await provision(owner_url(), app_url())  # sets the same password again

    async with app_engine.connect() as connection:
        assert await connection.scalar(text("SELECT current_user")) == APP_ROLE


async def test_a_password_with_sql_and_url_characters_works(owner_engine: AsyncEngine) -> None:
    awkward = "p:w'%s$1\"@/x"
    async with throwaway_login(owner_engine) as url:
        name = make_url(url).username
        assert name is not None
        await set_login_password(owner_engine, name, awkward)
        login = URL.create(
            "postgresql+asyncpg",
            username=name,
            password=awkward,
            host=make_url(url).host,
            port=make_url(url).port,
            database=make_url(url).database,
        )
        engine = create_async_engine(login)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT current_user")) == name
        finally:
            await engine.dispose()


async def test_the_header_accepts_a_registered_business_only(
    businesses: tuple[Business, Business],
) -> None:
    a, _ = businesses
    app = probe_app(make_settings(database_url=app_url(), readiness_timeout_seconds=5))
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        known = await client.get("/probe", headers={"X-Business-Id": a.business_id})
        unknown = await client.get("/probe", headers={"X-Business-Id": "canary-not-registered"})
        malformed = await client.get("/probe", headers={"X-Business-Id": "Not Registered"})

    assert (known.status_code, known.json()) == (200, {"business_id": a.business_id})
    assert (unknown.status_code, unknown.json()) == (400, UNKNOWN)
    assert (malformed.status_code, malformed.json()) == (unknown.status_code, unknown.json())
