"""Alembic migration environment.

The database URL comes from the app's typed `Settings` (`VECTAPILOT_DATABASE_URL`), never from
alembic.ini, so no credentials are ever written to a committed file.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from vectapilot_api.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models yet. From M2, the models' metadata goes here so that
# `alembic revision --autogenerate` can draft migrations from model changes.
target_metadata = None


def database_url() -> str:
    return str(Settings().database_url)


def run_migrations_offline() -> None:
    """Print the SQL instead of running it (`alembic upgrade head --sql`), e.g. for review."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
