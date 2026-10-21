import importlib.util
import secrets
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import integration
from vectapilot_api.config import MigrationSettings, Settings

API_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_0002 = API_ROOT / "migrations" / "versions" / "0002_app_role_and_business_tables.py"


def migration_0002() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION_0002)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
