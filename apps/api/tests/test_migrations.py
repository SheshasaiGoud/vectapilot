import importlib.util
import re
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Final, cast

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import TextClause, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.catalog import load_key_indexes
from tests.conftest import integration
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import APP_ROLE
from vectapilot_api.extraction import config as extraction_config
from vectapilot_api.extraction import routing as extraction_routing
from vectapilot_api.extraction import text as extraction_text
from vectapilot_api.extraction.prompt import EXTRACT_PROMPT_VERSION
from vectapilot_api.extraction.service import STORED_VALUE_MAX
from vectapilot_api.extraction.signals import EXTRACTION_RULES_VERSION, SPAN_MAX
from vectapilot_api.ingestion.language_map import (
    FALLBACK_CONFIG,
    TEXT_SEARCH_CONFIGS,
    text_search_config,
)

API_ROOT = Path(__file__).resolve().parents[1]
VERSIONS = API_ROOT / "migrations" / "versions"
MIGRATION_0002 = VERSIONS / "0002_app_role_and_business_tables.py"
MIGRATION_0004 = VERSIONS / "0004_one_approved_version_per_source.py"
MIGRATION_0005 = VERSIONS / "0005_chunk_full_text_columns.py"


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


def migration_0005() -> ModuleType:
    return load_migration(MIGRATION_0005)


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


# ── Migration 0005: the full-text columns on chunks (ADR 0014, M3 PR 2) ─────────────────────


def test_migration_0005_upgrade_and_downgrade_run_their_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = migration_0005()
    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()
    module.downgrade()

    assert recorder.executed == [*module.UPGRADE, *module.DOWNGRADE]
    assert (module.revision, module.down_revision) == ("0005", "0004")


def test_migration_0005s_statements_are_the_decided_ones() -> None:
    module = migration_0005()

    # Backfill before NOT NULL, and tsv last: one rewrite, from the backfilled config.
    assert module.UPGRADE == (
        "ALTER TABLE chunks ADD COLUMN ts_config regconfig",
        module.BACKFILL,
        "ALTER TABLE chunks ALTER COLUMN ts_config SET NOT NULL",
        "ALTER TABLE chunks ADD COLUMN tsv tsvector"
        " GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED",
    )
    assert module.BACKFILL == (
        "UPDATE chunks AS c SET ts_config = CAST(CASE lower(split_part(d.language, '-', 1))"
        " WHEN 'en' THEN 'english' WHEN 'hi' THEN 'hindi' ELSE 'simple' END AS regconfig)"
        " FROM documents AS d WHERE d.business_id = c.business_id AND d.id = c.document_id"
    )
    assert module.DOWNGRADE == (
        "ALTER TABLE chunks DROP COLUMN tsv",
        "ALTER TABLE chunks DROP COLUMN ts_config",
    )
    # No index (D6), so nothing that could need CONCURRENTLY; no default on ts_config.
    for statement in (*module.UPGRADE, *module.DOWNGRADE):
        assert "INDEX" not in statement and "DEFAULT" not in statement, statement
    # `op.execute` wraps each string in `text()`: no ":name" in it may turn into a bind.
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    for statement in (*module.UPGRADE, *module.DOWNGRADE):
        compiled = text(statement).compile(dialect=dialect)
        assert (compiled.params, str(compiled)) == ({}, statement)


def written_out_language_maps() -> list[tuple[str, ModuleType]]:
    """Every migration that writes the language map out (`LANGUAGE_MAP`), oldest first."""
    found = []
    for path in sorted(VERSIONS.glob("[0-9][0-9][0-9][0-9]_*.py")):
        module = load_migration(path)
        if hasattr(module, "LANGUAGE_MAP"):
            found.append((path.name, module))
    return found


def test_the_newest_written_out_language_map_is_the_platform_map() -> None:
    # A stored tsv does not follow a map change (ADR 0014): red when the map changes without a
    # migration that backfills with the new copy.
    maps = written_out_language_maps()
    assert maps and maps[0][0] == MIGRATION_0005.name
    name, newest = maps[-1]

    assert dict(newest.LANGUAGE_MAP) == dict(TEXT_SEARCH_CONFIGS), name
    assert newest.FALLBACK == FALLBACK_CONFIG, name


CHUNK_COLUMNS: Final = text(
    "SELECT a.attname::text AS name, format_type(a.atttypid, a.atttypmod) AS type,"
    " a.attnotnull, a.attgenerated::text AS generated, a.atthasdef,"
    " pg_get_expr(d.adbin, d.adrelid) AS expression"
    " FROM pg_catalog.pg_attribute a"
    " LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE a.attrelid = 'chunks'::regclass AND a.attname IN ('ts_config', 'tsv')"
    " AND NOT a.attisdropped ORDER BY 1"
)
INDEXES_ON_TSV: Final = text(
    "SELECT count(*) FROM pg_catalog.pg_indexes"
    " WHERE tablename = 'chunks' AND indexdef LIKE '%tsv%'"
)
COLUMNS_AT_0005: Final = [
    # name, type, NOT NULL, generated ('' none, 's' stored), has a default/expression, expression
    ("ts_config", "regconfig", True, "", False, None),
    ("tsv", "tsvector", False, "s", True, "to_tsvector(ts_config, content)"),
]


async def chunk_columns(connection: AsyncConnection) -> list[tuple[object, ...]]:
    return [tuple(row) for row in (await connection.execute(CHUNK_COLUMNS)).all()]


@integration
@pytest.mark.anyio
async def test_migration_0005_downgrades_and_upgrades() -> None:
    # CI ran `alembic upgrade head` before the tests: the columns are there. Their own downgrade
    # and upgrade SQL, in one transaction that is rolled back.
    module = migration_0005()
    async with owner_rolled_back() as connection:
        assert await chunk_columns(connection) == COLUMNS_AT_0005
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))  # as op.execute does
        assert await chunk_columns(connection) == []
        for statement in module.UPGRADE:
            await connection.execute(text(statement))  # as op.execute does
        assert await chunk_columns(connection) == COLUMNS_AT_0005
        assert await connection.scalar(INDEXES_ON_TSV) == 0  # no GIN index (D6)


@integration
@pytest.mark.anyio
async def test_migration_0005s_sql_rule_is_the_maps_rule() -> None:
    module = migration_0005()
    tags = ["en", "en-IN", "EN", "hi", "hi-Latn", "hi-Deva-IN", "te", "te-IN", "fr", "und", "eng"]
    statement = text(
        f"SELECT tag, CAST({module.config_for('tag')} AS regconfig)::text AS config"  # noqa: S608
        " FROM unnest(CAST(:tags AS text[])) AS t(tag)"
    )
    async with owner_rolled_back() as connection:
        rows = (await connection.execute(statement, {"tags": tags})).all()

    assert {row.tag: row.config for row in rows} == {tag: text_search_config(tag) for tag in tags}


# Rows as they were before 0005: no ts_config. The owner (a superuser here) skips RLS.
PRE_0005_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256)"
    " VALUES (:b, :source_id, '1', 'Before 0005', 'markdown', 'faq', :language, 'approved',"
    " 'public', repeat('a', 64))"
    " RETURNING id"
)
PRE_0005_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
    " embedding_model, embedding_dim)"
    " VALUES (:b, :document_id, :chunk_index, :content, CAST(:embedding AS vector), 'fake', 1024)"
)
VECTOR: Final = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
BACKFILLED: Final = text(
    "SELECT c.business_id, d.language, c.ts_config::text AS ts_config,"
    " c.tsv = to_tsvector(c.ts_config, c.content) AS tsv_matches"
    " FROM chunks c JOIN documents d ON d.business_id = c.business_id AND d.id = c.document_id"
    " WHERE c.business_id IN (:a, :b)"
    " ORDER BY c.business_id, d.language, c.chunk_index"
)
LANGUAGES_OF: Final = {
    "a": ("en", "hi", "te"),
    "b": ("en-IN", "hi-Latn", "fr"),
}
"""Each business's documents, one per language, two chunks each."""


async def plant_before_0005(connection: AsyncConnection) -> dict[str, str]:
    """Two fresh businesses with documents and chunks as 0004 left them; {letter: business id}."""
    businesses = {letter: f"mig-{letter}-{secrets.token_hex(4)}" for letter in LANGUAGES_OF}
    for letter, business in businesses.items():
        await connection.execute(
            text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business}
        )
        for language in LANGUAGES_OF[letter]:
            document_id = await connection.scalar(
                PRE_0005_DOCUMENT, {"b": business, "source_id": language, "language": language}
            )
            for chunk_index in (0, 1):
                await connection.execute(
                    PRE_0005_CHUNK,
                    {
                        "b": business,
                        "document_id": document_id,
                        "chunk_index": chunk_index,
                        "content": f"Our fees, part {chunk_index}",
                        "embedding": VECTOR,
                    },
                )
    return businesses


@integration
@pytest.mark.anyio
async def test_migration_0005_backfills_every_businesss_rows_through_the_map() -> None:
    module = migration_0005()
    async with owner_rolled_back() as connection:
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))
        businesses = await plant_before_0005(connection)
        for statement in module.UPGRADE:
            await connection.execute(text(statement))
        rows = (await connection.execute(BACKFILLED, businesses)).all()

    expected = [
        (business, language, text_search_config(language), True)
        for letter, business in sorted(businesses.items(), key=lambda item: item[1])
        for language in sorted(LANGUAGES_OF[letter])
        for _ in (0, 1)
    ]
    assert [tuple(row) for row in rows] == expected
    assert {config for _, _, config, _ in expected} == {"english", "hindi", "simple"}


async def as_a_non_superuser_owner(connection: AsyncConnection, attributes: str) -> dict[str, str]:
    """Before 0005, with rows of two businesses; then documents and chunks owned by a fresh
    non-superuser role with `attributes`, acting as it with business a set (all rolled back)."""
    for statement in migration_0005().DOWNGRADE:
        await connection.execute(text(statement))
    businesses = await plant_before_0005(connection)
    owner = f"vectapilot_test_{secrets.token_hex(4)}"
    await connection.exec_driver_sql(f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER {attributes}")
    for table in ("documents", "chunks"):
        await connection.exec_driver_sql(f"ALTER TABLE {table} OWNER TO {owner}")
    await connection.exec_driver_sql(f"SET LOCAL ROLE {owner}")
    await connection.execute(
        text("SELECT set_config('app.business_id', :b, true)"), {"b": businesses["a"]}
    )
    return businesses


@integration
@pytest.mark.anyio
async def test_migration_0005_under_force_rls_stops_at_not_null_on_a_hidden_row() -> None:
    # M10's owner is no superuser. Under FORCE RLS its backfill sees only business a; b's rows
    # stay NULL, and SET NOT NULL stops the migration instead of leaving them so.
    module = migration_0005()
    async with owner_rolled_back() as connection:
        businesses = await as_a_non_superuser_owner(connection, "NOBYPASSRLS")
        await connection.execute(text(module.ADD_TS_CONFIG))
        backfilled = await connection.execute(text(module.BACKFILL))
        # The premise, observed: no error, and only a's rows were updated (3 documents x 2
        # chunks); b's rows were hidden and skipped silently.
        assert backfilled.rowcount == 6
        with pytest.raises(DBAPIError) as refused:
            await connection.execute(text(module.SET_NOT_NULL))

    assert getattr(refused.value.orig, "sqlstate", None) == "23502"  # not_null_violation
    message = str(refused.value.orig)
    assert 'column "ts_config" of relation "chunks" contains null values' in message
    assert all(business not in message for business in businesses.values())


@integration
@pytest.mark.anyio
async def test_migration_0005_as_a_non_superuser_owner_with_bypassrls_backfills_everyone() -> None:
    # The fix the migration's docstring names, and the positive control for the test above: the
    # same owner change and business, with BYPASSRLS, upgrades and backfills both businesses.
    module = migration_0005()
    async with owner_rolled_back() as connection:
        businesses = await as_a_non_superuser_owner(connection, "BYPASSRLS")
        for statement in module.UPGRADE:
            await connection.execute(text(statement))
        rows = (await connection.execute(BACKFILLED, businesses)).all()

    assert {row.business_id for row in rows} == set(businesses.values())
    assert len(rows) == 12
    assert all(row.ts_config == text_search_config(row.language) for row in rows)


# ── Migration 0006: the sandbox and the tool tables (ADR 0016, M5 PR 1) ─────────────────────

MIGRATION_0006 = VERSIONS / "0006_sandbox_and_tool_tables.py"
TABLES_0006: Final = (
    "sandbox_slots",
    "sandbox_orders",
    "pending_actions",
    "sandbox_bookings",
    "sandbox_tickets",
    "tool_audit",
)


def migration_0006() -> ModuleType:
    return load_migration(MIGRATION_0006)


def test_migration_0006_upgrade_and_downgrade_run_their_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = migration_0006()
    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()
    module.downgrade()

    assert recorder.executed == [*module.UPGRADE, *module.DOWNGRADE]
    assert (module.revision, module.down_revision) == ("0006", "0005")


def test_migration_0006_puts_every_table_under_the_business_policy_and_grants_no_delete() -> None:
    module = migration_0006()

    assert module.TABLES == TABLES_0006
    assert module.APP_ROLE == APP_ROLE
    for table in TABLES_0006:
        for statement in module.row_security(table):
            assert statement in module.UPGRADE, statement
    grants = " ".join(module.GRANTS)
    assert "DELETE" not in grants and "TRUNCATE" not in grants and " ALL " not in grants
    assert module.DOWNGRADE == (
        "DROP TABLE tool_audit, sandbox_tickets, sandbox_bookings, pending_actions,"
        " sandbox_orders, sandbox_slots",
    )
    # `op.execute` wraps each string in `text()`: no ":name" in it may turn into a bind.
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    for statement in (*module.UPGRADE, *module.DOWNGRADE):
        compiled = text(statement).compile(dialect=dialect)
        assert (compiled.params, str(compiled)) == ({}, statement)


SCHEMA_0006: Final = {
    "tables": text(
        "SELECT relname::text, relrowsecurity, relforcerowsecurity FROM pg_catalog.pg_class"
        " WHERE relname = ANY(CAST(:tables AS text[])) AND relkind = 'r' ORDER BY 1"
    ),
    "columns": text(
        "SELECT c.relname::text, a.attname::text, format_type(a.atttypid, a.atttypmod),"
        " a.attnotnull, pg_get_expr(d.adbin, d.adrelid)"
        " FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid"
        " LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
        " WHERE c.relname = ANY(CAST(:tables AS text[])) AND a.attnum > 0"
        " AND NOT a.attisdropped ORDER BY 1, 2"
    ),
    "constraints": text(
        "SELECT conrelid::regclass::text, conname::text, contype::text,"
        " pg_get_constraintdef(oid) FROM pg_catalog.pg_constraint"
        " WHERE conrelid::regclass::text = ANY(CAST(:tables AS text[])) ORDER BY 1, 2"
    ),
    "indexes": text(
        "SELECT tablename::text, indexname::text, indexdef FROM pg_catalog.pg_indexes"
        " WHERE tablename = ANY(CAST(:tables AS text[])) ORDER BY 1, 2"
    ),
    "policies": text(
        "SELECT tablename::text, policyname::text, permissive, roles::text[], cmd, qual,"
        " with_check FROM pg_catalog.pg_policies"
        " WHERE tablename = ANY(CAST(:tables AS text[])) ORDER BY 1, 2"
    ),
    "table_grants": text(
        "SELECT table_name::text, privilege_type::text FROM information_schema.role_table_grants"
        " WHERE grantee = :role AND table_name = ANY(CAST(:tables AS text[])) ORDER BY 1, 2"
    ),
    "column_grants": text(
        "SELECT table_name::text, column_name::text, privilege_type::text"
        " FROM information_schema.column_privileges"
        " WHERE grantee = :role AND privilege_type <> 'SELECT'"
        " AND table_name = ANY(CAST(:tables AS text[])) ORDER BY 1, 2, 3"
    ),
}


async def catalog_rows(
    connection: AsyncConnection, tables: Sequence[str]
) -> dict[str, list[tuple[object, ...]]]:
    """Everything about `tables`, as catalog rows: tables, columns, constraints, indexes,
    policies and the app role's grants (`SCHEMA_0006`'s queries)."""
    params = {"tables": list(tables), "role": APP_ROLE}
    return {
        name: [tuple(row) for row in (await connection.execute(query, params)).all()]
        for name, query in SCHEMA_0006.items()
    }


async def schema_0006(connection: AsyncConnection) -> dict[str, list[tuple[object, ...]]]:
    """Everything 0006 creates, as catalog rows."""
    return await catalog_rows(connection, TABLES_0006)


@integration
@pytest.mark.anyio
async def test_migration_0006_downgrades_and_upgrades_to_the_same_schema() -> None:
    # CI ran `alembic upgrade head`: the tables are there. Their own downgrade and upgrade SQL, in
    # one transaction that is rolled back; the catalog afterwards equals the one before, so the
    # database at head is exactly this file (grants and policies included).
    module = migration_0006()
    async with owner_rolled_back() as connection:
        before = await schema_0006(connection)
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))  # as op.execute does
        gone = await schema_0006(connection)
        for statement in module.UPGRADE:
            await connection.execute(text(statement))  # as op.execute does
        after = await schema_0006(connection)

    assert before["tables"] == [(table, True, True) for table in sorted(TABLES_0006)]
    assert all(rows == [] for rows in gone.values()), gone
    assert after == before


@integration
@pytest.mark.anyio
async def test_migration_0006s_app_grants_are_the_decided_ones() -> None:
    module = migration_0006()
    async with owner_rolled_back() as connection:
        schema = await schema_0006(connection)

    assert set(schema["table_grants"]) == {(table, "SELECT") for table in TABLES_0006}
    expected = {
        ("pending_actions", "INSERT", module.ACTION_INSERT_COLUMNS),
        ("pending_actions", "UPDATE", module.ACTION_UPDATE_COLUMNS),
        ("sandbox_bookings", "INSERT", module.BOOKING_INSERT_COLUMNS),
        ("sandbox_bookings", "UPDATE", module.BOOKING_UPDATE_COLUMNS),
        ("sandbox_tickets", "INSERT", module.TICKET_INSERT_COLUMNS),
        ("tool_audit", "INSERT", module.AUDIT_INSERT_COLUMNS),
    }
    assert set(schema["column_grants"]) == {
        (table, column, privilege) for table, privilege, columns in expected for column in columns
    }
    # Never a column the database fills or only a confirm may set; a uuid key is never chosen
    # by the app (the canary's random-uuid exception relies on it), only a booking's text id.
    inserted = {(t, c) for t, c, p in schema["column_grants"] if p == "INSERT"}
    filled = {"created_at", "confirmed_at", "confirm_trace_id", "finished_at", "result"}
    assert not {c for _, c in inserted} & (filled | {"retired_by_action_id", "failure_reason"})
    assert {t for t, c in inserted if c == "id"} == {"sandbox_bookings"}
    restrictive = [p for p in schema["policies"] if p[2] == "RESTRICTIVE"]
    assert restrictive == [
        (
            table,
            "app_writes_are_actions",
            "RESTRICTIVE",
            [APP_ROLE],
            "INSERT",
            None,
            "(origin = 'action'::text)",
        )
        for table in ("sandbox_bookings", "sandbox_tickets")
    ]


# ── Migration 0007: the extraction tables (ADR 0017, M6 PR 3) ─────────────────────────────────

MIGRATION_0007 = VERSIONS / "0007_extraction_tables.py"
TABLES_0007: Final = ("extractions", "extraction_fields")
COLUMNS_0007: Final = {
    "extractions": {
        "business_id",
        "id",
        "kind",
        "template_version",
        "prompt_version",
        "rules_version",
        "status",
        "failure_reason",
        "file_sha256",
        "pages",
        "image_pages",
        "trace_id",
        "retention_days",
        "created_at",
        "expires_at",
    },
    "extraction_fields": {
        "business_id",
        "extraction_id",
        "field",
        "status",
        "level",
        "reasons",
        "value",
        "span",
        "page",
    },
}
"""D6 (a): metadata, and per field only the value, its page and a short span. No file, no text."""


def migration_0007() -> ModuleType:
    return load_migration(MIGRATION_0007)


def test_migration_0007_upgrade_and_downgrade_run_their_statements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = migration_0007()
    recorder = RecordingOp()
    monkeypatch.setattr(module, "op", recorder)

    module.upgrade()
    module.downgrade()

    assert recorder.executed == [*module.UPGRADE, *module.DOWNGRADE]
    assert (module.revision, module.down_revision) == ("0007", "0006")


def test_migration_0007_puts_both_tables_under_the_business_policy_and_grants_no_change() -> None:
    module = migration_0007()

    assert module.TABLES == TABLES_0007
    assert module.APP_ROLE == APP_ROLE
    for table in TABLES_0007:
        for statement in module.row_security(table):
            assert statement in module.UPGRADE, statement
        # Literal too: `row_security` comes from the same file, so a change to it passes above.
        for mode in ("ENABLE", "FORCE"):
            assert f"ALTER TABLE {table} {mode} ROW LEVEL SECURITY" in module.UPGRADE, table
    grants = " ".join(module.GRANTS)
    for privilege in ("UPDATE", "DELETE", "TRUNCATE", " ALL "):
        assert privilege not in grants, privilege
    assert module.DOWNGRADE == ("DROP TABLE extraction_fields, extractions",)
    # `op.execute` wraps each string in `text()`: no ":name" in it may turn into a bind.
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    for statement in (*module.UPGRADE, *module.DOWNGRADE):
        compiled = text(statement).compile(dialect=dialect)
        assert (compiled.params, str(compiled)) == ({}, statement)


def test_migration_0007s_checks_mirror_the_extraction_core() -> None:
    module = migration_0007()
    config = extraction_config

    assert tuple(s.value for s in extraction_routing.DocumentStatus) == module.DOCUMENT_STATUSES
    assert tuple(s.value for s in extraction_routing.FieldStatus) == module.FIELD_STATUSES
    assert tuple(level.value for level in extraction_routing.Level) == module.LEVELS
    assert tuple(reason.value for reason in extraction_routing.Reason) == module.REASONS
    assert set(module.FAILURES) == {reason.value for reason in extraction_routing.FAILURES}
    assert module.MAX_PAGES == extraction_text.MAX_PAGES
    assert extraction_text.DEFAULT_LIMITS.max_pages == module.MAX_PAGES
    assert module.SPAN_MAX == SPAN_MAX
    # The longest `FieldSpec.value_bound`: text and identifier `max_length`, a date, an enum id
    # (snake case) or synonym (a label). The service withholds anything longer.
    longest = max(
        config.TEXT_MAX,
        config.IDENTIFIER_MAX,
        config.DATE_VALUE_MAX,
        config.LABEL_MAX,
        config.SNAKE_MAX,
    )
    assert longest == module.VALUE_MAX == STORED_VALUE_MAX
    assert module.RETENTION_DAYS == config.RETENTION_DAYS
    assert config.SNAKE_PATTERN.replace("(?:", "(") == module.SNAKE
    assert module.SNAKE_LENGTH == (2, config.SNAKE_MAX)
    for version in (EXTRACT_PROMPT_VERSION, EXTRACTION_RULES_VERSION):
        assert re.fullmatch(module.VERSION, version), version
        assert len(version) <= module.VERSION_MAX


@integration
@pytest.mark.anyio
async def test_migration_0007_downgrades_and_upgrades_to_the_same_schema() -> None:
    # As 0006: its own downgrade and upgrade SQL in one rolled-back transaction; the catalog
    # afterwards equals the one before, so the database at head is exactly this file.
    module = migration_0007()
    async with owner_rolled_back() as connection:
        before = await catalog_rows(connection, TABLES_0007)
        for statement in module.DOWNGRADE:
            await connection.execute(text(statement))  # as op.execute does
        gone = await catalog_rows(connection, TABLES_0007)
        for statement in module.UPGRADE:
            await connection.execute(text(statement))  # as op.execute does
        after = await catalog_rows(connection, TABLES_0007)

    assert before["tables"] == [(table, True, True) for table in sorted(TABLES_0007)]
    assert all(rows == [] for rows in gone.values()), gone
    assert after == before


@integration
@pytest.mark.anyio
async def test_migration_0007_stores_no_file_and_only_the_decided_columns() -> None:
    async with owner_rolled_back() as connection:
        schema = await catalog_rows(connection, TABLES_0007)

    types = {(row[0], row[1]): row[2] for row in schema["columns"]}
    assert {table: {c for t, c in types if t == table} for table in TABLES_0007} == COLUMNS_0007
    assert "bytea" not in types.values()  # E10: the file is hashed, never stored
    assert types[("extractions", "file_sha256")] == "text"
    assert types[("extraction_fields", "value")] == "jsonb"
    defaults = {(row[0], row[1]): row[4] for row in schema["columns"]}
    assert defaults[("extractions", "expires_at")] == (
        "date_add(created_at, make_interval(days => retention_days), 'UTC'::text)"
    )


PRIVILEGED_ROLES: Final = text(
    "SELECT c.relname::text, CASE WHEN a.grantee = 0 THEN 'PUBLIC'"
    " ELSE pg_get_userbyid(a.grantee)::text END"
    " FROM pg_catalog.pg_class c, aclexplode(c.relacl) a"
    " WHERE c.relname = ANY(CAST(:tables AS text[])) AND a.grantee <> c.relowner"
    " UNION SELECT c.relname::text, CASE WHEN a.grantee = 0 THEN 'PUBLIC'"
    " ELSE pg_get_userbyid(a.grantee)::text END"
    " FROM pg_catalog.pg_attribute t JOIN pg_catalog.pg_class c ON c.oid = t.attrelid,"
    " aclexplode(t.attacl) a"
    " WHERE c.relname = ANY(CAST(:tables AS text[])) AND a.grantee <> c.relowner"
)
"""Every (table, role) holding any privilege on a table or one of its columns, PUBLIC included,
other than the owner: a role that may read a table must be one the expiry policy hides rows from."""


async def privileged_roles(connection: AsyncConnection) -> dict[str, set[str]]:
    rows = (await connection.execute(PRIVILEGED_ROLES, {"tables": list(TABLES_0007)})).all()
    found: dict[str, set[str]] = {table: set() for table in TABLES_0007}
    for table, role in rows:
        found[table].add(role)
    return found


@integration
@pytest.mark.anyio
async def test_migration_0007_grants_only_roles_its_expiry_policy_covers() -> None:
    # `hidden_at_expiry` is TO the app role only: any other role granted a privilege (a reader
    # role in M8 or M9, a GRANT TO PUBLIC) would see expired rows. The red-proof grants one.
    async with owner_rolled_back() as connection:
        policies = await catalog_rows(connection, TABLES_0007)
        covered = {
            str(p[0]): set(cast("list[str]", p[3]))  # pg_policies.roles, a name[]
            for p in policies["policies"]
            if p[2] == "RESTRICTIVE"
        }
        granted = await privileged_roles(connection)
        await connection.execute(text("GRANT SELECT ON extractions TO PUBLIC"))
        widened = await privileged_roles(connection)

    assert covered == {table: {APP_ROLE} for table in TABLES_0007}
    assert granted == covered
    assert widened["extractions"] == {APP_ROLE, "PUBLIC"} != covered["extractions"]


@integration
@pytest.mark.anyio
async def test_migration_0007s_app_grants_and_policies_are_the_decided_ones() -> None:
    module = migration_0007()
    async with owner_rolled_back() as connection:
        schema = await catalog_rows(connection, TABLES_0007)

    assert set(schema["table_grants"]) == {(table, "SELECT") for table in TABLES_0007}
    expected = {
        ("extractions", "INSERT", module.EXTRACTION_INSERT_COLUMNS),
        ("extraction_fields", "INSERT", module.FIELD_INSERT_COLUMNS),
    }
    assert set(schema["column_grants"]) == {
        (table, column, privilege) for table, privilege, columns in expected for column in columns
    }
    # The database draws the id and sets both times; the app never chooses them.
    inserted = {c for t, c, _ in schema["column_grants"] if t == "extractions"}
    assert not inserted & {"id", "created_at", "expires_at"}
    restrictive = {p[0]: p[1:] for p in schema["policies"] if p[2] == "RESTRICTIVE"}
    assert set(restrictive) == set(TABLES_0007)
    for name, _, roles, command, _, check in restrictive.values():
        assert (name, roles, command, check) == ("hidden_at_expiry", [APP_ROLE], "SELECT", None)
    assert restrictive["extractions"][4] == "(expires_at > now())"
    parent = str(restrictive["extraction_fields"][4])
    assert "FROM extractions e" in parent and "(e.expires_at > now())" in parent
    permissive = sorted((p[0], p[1]) for p in schema["policies"] if p[2] == "PERMISSIVE")
    assert permissive == sorted((table, "business_isolation") for table in TABLES_0007)


NEW_EXTRACTION: Final = text(
    "INSERT INTO extractions (business_id, kind, template_version, prompt_version,"
    " rules_version, status, file_sha256, pages, image_pages, trace_id, retention_days)"
    " VALUES (:b, 'intake', 1, 'extract-fields.1', 'extraction-rules.1', 'needs_review',"
    " repeat('a', 64), 2, '{}', repeat('c', 32), :days)"
    " RETURNING id, created_at, expires_at"
)
NEW_FIELD: Final = text(
    "INSERT INTO extraction_fields (business_id, extraction_id, field, status, level, reasons,"
    " value, span, page)"
    " VALUES (:b, :extraction_id, 'policy_number', 'review', 'medium', ARRAY['always_review'],"
    " to_jsonb(CAST('PN-1' AS text)), 'Policy no. PN-1', 1)"
)
AGE: Final = text(
    "UPDATE extractions SET created_at = created_at - make_interval(days => :days)"
    " WHERE business_id = :b AND id = :id RETURNING expires_at < now()"
)
SET_BUSINESS: Final = text("SELECT set_config('app.business_id', :b, true)")


async def refused_sqlstate(
    connection: AsyncConnection, statement: TextClause, params: Mapping[str, object]
) -> str | None:
    """The SQLSTATE `statement` fails with, inside a savepoint (the transaction goes on)."""
    with pytest.raises(DBAPIError) as refused:
        async with connection.begin_nested():
            await connection.execute(statement, params)
    return getattr(refused.value.orig, "sqlstate", None)


@integration
@pytest.mark.anyio
async def test_migration_0007s_expiry_is_set_by_the_database_and_hides_rows_from_the_app() -> None:
    async with owner_rolled_back() as connection:
        a, b = (f"mig-{letter}-{secrets.token_hex(4)}" for letter in "ab")
        for business in (a, b):
            await connection.execute(
                text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business}
            )
        fresh = (await connection.execute(NEW_EXTRACTION, {"b": a, "days": 30})).one()
        expired = (await connection.execute(NEW_EXTRACTION, {"b": a, "days": 7})).one()
        others = (await connection.execute(NEW_EXTRACTION, {"b": b, "days": 30})).one()
        for business, row in ((a, fresh), (a, expired), (b, others)):
            await connection.execute(NEW_FIELD, {"b": business, "extraction_id": row.id})
        # The owner moves `created_at` back 8 days: the generated expiry follows (7 days).
        assert await connection.scalar(AGE, {"b": a, "id": expired.id, "days": 8}) is True

        await connection.exec_driver_sql(f"SET LOCAL ROLE {APP_ROLE}")
        await connection.execute(SET_BUSINESS, {"b": a})
        seen = set(await connection.scalars(text("SELECT id FROM extractions")))
        fields = set(await connection.scalars(text("SELECT extraction_id FROM extraction_fields")))
        mine = (await connection.execute(NEW_EXTRACTION, {"b": a, "days": 30})).one()
        await connection.execute(NEW_FIELD, {"b": a, "extraction_id": mine.id})
        visible = set(await connection.scalars(text("SELECT extraction_id FROM extraction_fields")))
        refusals = {
            "choose an id": await refused_sqlstate(
                connection,
                text(
                    "INSERT INTO extractions (business_id, id, kind, template_version,"
                    " prompt_version, rules_version, status, file_sha256, pages, image_pages,"
                    " trace_id, retention_days) SELECT business_id, gen_random_uuid(), kind,"
                    " template_version, prompt_version, rules_version, status, file_sha256,"
                    " pages, image_pages, trace_id, retention_days FROM extractions"
                    " WHERE id = :id"
                ),
                {"id": mine.id},
            ),
            "choose created_at": await refused_sqlstate(
                connection,
                text(
                    "INSERT INTO extractions (business_id, kind, template_version,"
                    " prompt_version, rules_version, status, file_sha256, pages, image_pages,"
                    " trace_id, retention_days, created_at) SELECT business_id, kind,"
                    " template_version, prompt_version, rules_version, status, file_sha256,"
                    " pages, image_pages, trace_id, retention_days, now() - interval '1 year'"
                    " FROM extractions WHERE id = :id"
                ),
                {"id": mine.id},
            ),
            "write expires_at": await refused_sqlstate(
                connection,
                text(
                    "INSERT INTO extractions (business_id, kind, template_version,"
                    " prompt_version, rules_version, status, file_sha256, pages, image_pages,"
                    " trace_id, retention_days, expires_at) SELECT business_id, kind,"
                    " template_version, prompt_version, rules_version, status, file_sha256,"
                    " pages, image_pages, trace_id, retention_days, now()"
                    " FROM extractions WHERE id = :id"
                ),
                {"id": mine.id},
            ),
            "update": await refused_sqlstate(
                connection, text("UPDATE extractions SET status = 'complete'"), {}
            ),
            "delete a field": await refused_sqlstate(
                connection, text("DELETE FROM extraction_fields"), {}
            ),
            "delete": await refused_sqlstate(connection, text("DELETE FROM extractions"), {}),
            "insert for b": await refused_sqlstate(connection, NEW_EXTRACTION, {"b": b, "days": 1}),
            "a field on b's extraction": await refused_sqlstate(
                connection, NEW_FIELD, {"b": a, "extraction_id": others.id}
            ),
        }

    assert fresh.expires_at - fresh.created_at == timedelta(days=30)
    assert expired.expires_at - expired.created_at == timedelta(days=7)
    assert seen == {fresh.id} and fields == {fresh.id}  # expired and b's rows are hidden
    assert mine.expires_at - mine.created_at == timedelta(days=30)  # the database's clock
    assert visible == {fresh.id, mine.id}
    assert refusals == {
        "choose an id": "42501",  # insufficient_privilege: no column grant
        "choose created_at": "42501",
        "write expires_at": "428C9",  # generated_always: not even the owner writes it
        "update": "42501",
        "delete a field": "42501",
        "delete": "42501",
        "insert for b": "42501",  # the business policy's WITH CHECK
        "a field on b's extraction": "23503",  # the composite key: (a, b's id) does not exist
    }


EXTRACTION_WITH: Final = text(
    "INSERT INTO extractions (business_id, kind, template_version, prompt_version,"
    " rules_version, status, failure_reason, file_sha256, pages, image_pages, trace_id,"
    " retention_days)"
    " VALUES (:b, :kind, 1, 'extract-fields.1', 'extraction-rules.1', :status, :failure,"
    " :sha256, :pages, CAST(:image_pages AS integer[]), repeat('c', 32), :days)"
    " RETURNING id"
)
FIELD_WITH: Final = text(
    "INSERT INTO extraction_fields (business_id, extraction_id, field, status, level, reasons,"
    " value, span, page)"
    " VALUES (:b, :extraction_id, :field, :status, :level, CAST(:reasons AS text[]),"
    " CAST(CAST(:value AS text) AS jsonb), :span, :page)"
)
GOOD_EXTRACTION: Final[Mapping[str, object]] = {
    "kind": "intake",
    "status": "needs_review",
    "failure": None,
    "sha256": "a" * 64,
    "pages": 2,
    "image_pages": [2],
    "days": 30,
}
GOOD_FIELDS: Final[Mapping[str, Mapping[str, object]]] = {
    "review": {
        "field": "full_name",
        "status": "review",
        "level": "medium",
        "reasons": ["unsure", "image_on_page"],
        "value": '"Asha Rao"',
        "span": "Name: Asha Rao",
        "page": 2,
    },
    "accepted": {
        "field": "policy_number",
        "status": "accepted",
        "level": "high",
        "reasons": [],
        "value": '"PN-1"',
        "span": "Policy no. PN-1",
        "page": 1,
    },
    "absent": {
        "field": "pet_name",
        "status": "absent",
        "level": None,
        "reasons": [],
        "value": None,
        "span": None,
        "page": None,
    },
    "failed": {
        "field": "visit_date",
        "status": "review",
        "level": None,
        "reasons": ["internal_error"],
        "value": None,
        "span": None,
        "page": None,
    },
    "unverified": {  # E2 did not verify the value: its status and reasons, nothing else
        "field": "owner_name",
        "status": "review",
        "level": None,
        "reasons": ["quote_not_found"],
        "value": None,
        "span": None,
        "page": None,
    },
}
BAD_EXTRACTIONS: Final[Mapping[str, Mapping[str, object]]] = {
    "a failure on a complete extraction": {"status": "complete", "failure": "timeout"},
    "an unknown failure": {"failure": "oops"},
    "no pages without a failure": {"pages": 0, "image_pages": []},
    "pages past E12": {"pages": 6},
    "an image page past the last page": {"image_pages": [3]},
    "an image page zero": {"image_pages": [0]},
    "a null image page": {"image_pages": [None]},
    "two-dimensional image pages": {"image_pages": [[1], [2]]},
    "an uppercase hash": {"sha256": "A" * 64},
    "retention zero": {"days": 0},
    "retention past 90 days": {"days": 91},
    "a kind not in snake case": {"kind": "Intake Form"},
}
BAD_FIELDS: Final[Mapping[str, Mapping[str, object]]] = {
    "a span past SPAN_MAX": {"span": "x" * (SPAN_MAX + 1)},
    "an empty span": {"span": ""},
    "a value past the longest field bound": {"value": '"' + "x" * 201 + '"'},
    "an empty value": {"value": '""'},
    "a number value": {"value": "12"},
    "a JSON null value": {"value": "null"},
    "a page past E12": {"page": 6},
    "an unknown reason": {"reasons": ["made_up"]},
    "a null reason": {"reasons": [None]},
    "an unknown level": {"level": "certain"},
    "an accepted medium value": {"status": "accepted", "level": "medium", "reasons": []},
    "an accepted value with a reason": {"status": "accepted", "level": "high"},
    "an accepted field without a value": {
        "status": "accepted",
        "level": "high",
        "reasons": [],
        "value": None,
        "span": None,
        "page": None,
    },
    "an absent field with a value": {"status": "absent", "level": None, "reasons": []},
    "a review without a reason": {"reasons": []},
    "a level without a value": {"value": None, "span": None, "page": None},
    "a span without a page": {"page": None},
    "a value without a page (an unverified value)": {"span": None, "page": None},
    "a page without a value": {"level": None, "value": None, "span": None},
    "a field not in snake case": {"field": "Full Name"},
}


@integration
@pytest.mark.anyio
async def test_migration_0007s_checks_refuse_every_row_the_service_never_returns() -> None:
    # The service withholds a value over its field's bound and fails closed on anything else that
    # does not fit (`service.storable`; NFKC and NUL in tests/extraction/test_service.py), so
    # each refusal here is a bug's, never a hostile value's.
    async with owner_rolled_back() as connection:
        business = f"mig-c-{secrets.token_hex(4)}"
        await connection.execute(
            text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business}
        )
        extraction_id = await connection.scalar(EXTRACTION_WITH, {"b": business, **GOOD_EXTRACTION})
        for good in GOOD_FIELDS.values():  # the positive control: every shape the core builds
            await connection.execute(
                FIELD_WITH, {"b": business, "extraction_id": extraction_id, **good}
            )
        extractions = {
            case: await refused_sqlstate(
                connection, EXTRACTION_WITH, {"b": business, **GOOD_EXTRACTION, **change}
            )
            for case, change in BAD_EXTRACTIONS.items()
        }
        fields = {
            case: await refused_sqlstate(
                connection,
                FIELD_WITH,
                {
                    "b": business,
                    "extraction_id": extraction_id,
                    **GOOD_FIELDS["review"],
                    "field": "bad_field",
                    **change,
                },
            )
            for case, change in BAD_FIELDS.items()
        }

    assert extractions == dict.fromkeys(BAD_EXTRACTIONS, "23514")  # check_violation
    assert fields == dict.fromkeys(BAD_FIELDS, "23514")
