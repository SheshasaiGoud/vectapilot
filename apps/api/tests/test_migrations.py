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
