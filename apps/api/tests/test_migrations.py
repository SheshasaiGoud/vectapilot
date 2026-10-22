import importlib.util
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.catalog import load_key_indexes
from tests.conftest import integration
from vectapilot_api.config import MigrationSettings, Settings

API_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = API_ROOT / "migrations" / "versions"
MIGRATION_0002 = VERSIONS / "0002_app_role_and_business_tables.py"
MIGRATION_0004 = VERSIONS / "0004_one_approved_version_per_source.py"


def load_migration(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def migration_0002() -> ModuleType:
    return load_migration(MIGRATION_0002)


def migration_0004() -> ModuleType:
    return load_migration(MIGRATION_0004)


def test_migration_history_has_a_single_head() -> None:
    # Two branches adding migrations in parallel can fork the history; Alembic then refuses to
    # `upgrade head`. Catch that in CI instead of on deploy.
    scripts = ScriptDirectory.from_config(Config(str(API_ROOT / "alembic.ini")))

    assert len(scripts.get_heads()) == 1


@integration
@pytest.mark.anyio
async def test_migrations_enable_pgvector() -> None:
    settings = Settings()
    engine = create_async_engine(str(settings.database_url.get_secret_value()))
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            version = result.scalar_one_or_none()
    finally:
        await engine.dispose()

    assert version is not None, "pgvector missing — run `alembic upgrade head` first"


@integration
@pytest.mark.anyio
async def test_the_downgrade_drops_only_a_role_the_upgrade_created() -> None:
    # Migration 0002's own role SQL, on two throwaway roles: one it creates, one that existed
    # before it. Red if the downgrade drops unconditionally (both gone) or the upgrade stops
    # marking the role it creates (both stay).
    module = migration_0002()
    created = f"vectapilot_test_{secrets.token_hex(4)}"
    existing = f"{created}_existing"
    roles = text("SELECT rolname FROM pg_roles WHERE rolname IN (:created, :existing)")
    engine = create_async_engine(str(MigrationSettings().migration_database_url.get_secret_value()))
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"CREATE ROLE {existing} NOLOGIN")
        async with engine.begin() as connection:
            for role in (created, existing):
                await connection.execute(text(module.create_role_sql(role)))
        async with engine.begin() as connection:
            before = set(
                await connection.scalars(roles, {"created": created, "existing": existing})
            )
            for role in (created, existing):
                await connection.execute(text(module.drop_role_sql(role)))
            after = set(await connection.scalars(roles, {"created": created, "existing": existing}))
    finally:
        async with engine.begin() as connection:
            for role in (created, existing):
                await connection.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        await engine.dispose()

    assert before == {created, existing}
    assert after == {existing}


# ── Migration 0004: one approved version per source (ADR 0010) ──────────────────────────────


class RecordingOp:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def test_migration_0004_upgrade_and_downgrade_run_their_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = migration_0004()
    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()
    module.downgrade()

    assert recorder.executed == [*module.UPGRADE, *module.DOWNGRADE]
    assert (module.revision, module.down_revision) == ("0004", "0003")


def test_migration_0004s_index_starts_with_business_id_and_its_error_names_no_data() -> None:
    module = migration_0004()

    assert module.CREATE_INDEX == (
        "CREATE UNIQUE INDEX documents_one_approved_version"
        " ON documents (business_id, source_id) WHERE status = 'approved'"
    )
    assert module.CREATE_INDEX in module.UPGRADE[0]
    # Postgres' own error (it prints the duplicated key) is replaced, not passed on.
    assert "EXCEPTION WHEN unique_violation THEN" in module.UPGRADE[0]
    assert f"RAISE EXCEPTION '{module.DUPLICATES_FOUND}'" in module.UPGRADE[0]
    # Inside RAISE '...': a quote would end the literal, a percent sign is a placeholder.
    assert "'" not in module.DUPLICATES_FOUND and "%" not in module.DUPLICATES_FOUND
    assert module.DOWNGRADE == ("DROP INDEX documents_one_approved_version",)
    # `op.execute` wraps each string in `text()`: no ":name" in it may turn into a bind.
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    for statement in (*module.UPGRADE, *module.DOWNGRADE):
        compiled = text(statement).compile(dialect=dialect)
        assert (compiled.params, str(compiled)) == ({}, statement)


INDEX_EXISTS: Final = text(
    "SELECT count(*) FROM pg_catalog.pg_indexes"
    " WHERE tablename = 'documents' AND indexname = 'documents_one_approved_version'"
)


@asynccontextmanager
async def owner_rolled_back() -> AsyncIterator[AsyncConnection]:
    """An owner connection whose every change, DDL included, is undone at the end."""
    engine = create_async_engine(
        str(MigrationSettings().migration_database_url.get_secret_value()), poolclass=NullPool
    )
    try:
        async with engine.connect() as connection:
            try:
                yield connection
            finally:
                await connection.rollback()
    finally:
        await engine.dispose()


@integration
@pytest.mark.anyio
async def test_migration_0004_downgrades_and_upgrades() -> None:
    # CI ran `alembic upgrade head` before the tests: the index is there. Its own downgrade and
    # upgrade SQL, in one transaction that is rolled back (Postgres DDL is transactional).
    module = migration_0004()
    async with owner_rolled_back() as connection:
        assert await connection.scalar(INDEX_EXISTS) == 1
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))  # as op.execute does
        assert await connection.scalar(INDEX_EXISTS) == 0
        for statement in module.UPGRADE:
            await connection.execute(text(statement))  # as op.execute does
        keys = {key.name: key for key in await load_key_indexes(connection)}

    index = keys[module.INDEX]
    assert (index.columns, index.predicate) == (
        ("business_id", "source_id"),
        "(status = 'approved'::text)",
    )


@integration
@pytest.mark.anyio
async def test_migration_0004_refuses_two_approved_versions_without_naming_them() -> None:
    # A database from before 0004 with two approved versions of one source: the upgrade stops
    # with the fixed message, never Postgres' "Key (business_id, source_id)=(...)" detail.
    module = migration_0004()
    business = f"mig-test-{secrets.token_hex(4)}"
    source = f"source-{secrets.token_hex(4)}"
    insert = text(
        "INSERT INTO documents (business_id, source_id, version, title, source_type,"
        " content_kind, language, status, audience, content_sha256)"
        " VALUES (:b, :s, :v, 'Two approved', 'markdown', 'faq', 'en', 'approved', 'public',"
        " repeat('a', 64))"
    )
    async with owner_rolled_back() as connection:
        await connection.execute(
            text("SELECT set_config('app.business_id', :b, true)"), {"b": business}
        )
        await connection.execute(
            text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business}
        )
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))  # as op.execute does
        for version in ("1", "2"):
            await connection.execute(insert, {"b": business, "s": source, "v": version})
        with pytest.raises(DBAPIError) as refused:
            for statement in module.UPGRADE:
                await connection.execute(text(statement))  # as op.execute does

    message = str(refused.value.orig)
    assert module.DUPLICATES_FOUND in message
    assert business not in message and source not in message
    assert getattr(refused.value.orig, "sqlstate", None) == "23505"
