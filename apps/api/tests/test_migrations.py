from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import integration
from vectapilot_api.config import Settings

API_ROOT = Path(__file__).resolve().parents[1]


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
