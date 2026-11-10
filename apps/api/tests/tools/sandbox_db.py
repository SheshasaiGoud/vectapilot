"""Database helpers for the sandbox tests (integration only): throwaway businesses, seeded through
the real reset, and fingerprints of whole tables.

Every business here is fresh (`sbx-<letter>-<hex>`), seeded from a copy of a committed seed with
its id replaced, and removed afterwards, `tool_audit` included: the reset never deletes audit rows,
but a test's throwaway business leaves nothing behind. The fictional businesses' own ids are never
written by a test.
"""

import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.catalog import business_tables
from tests.tools.helpers import SANDBOX_DIR
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.tools.files import read_business_tools
from vectapilot_api.tools.sandbox import RESET_TABLES, SandboxSeed, reset_sandbox

PAWS_SEED: Final = read_business_tools(SANDBOX_DIR / "paws-and-co").seed
"""The committed seed with slots, bookings and orders: every reset table gets rows."""

UPROOT: Final = (
    "tool_audit",
    *RESET_TABLES,
    "llm_calls",
    "chunks",
    "documents",
    "businesses",
)


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


def app_url() -> str:
    return str(Settings().database_url.get_secret_value())


def fresh_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


def seed_for(business_id: str, seed: SandboxSeed = PAWS_SEED) -> SandboxSeed:
    """`seed` for another business (the same ids, customers and text)."""
    return seed.model_copy(update={"business_id": business_id})


def new_business(letter: str) -> str:
    return f"sbx-{letter}-{secrets.token_hex(4)}"


async def seed_business(owner: AsyncEngine, seed: SandboxSeed) -> None:
    """The real reset, committed, as the command runs it."""
    async with business_transaction(owner, seed.business_id) as connection:
        await reset_sandbox(connection, seed)


async def uproot(owner: AsyncEngine, business_id: str) -> None:
    async with business_transaction(owner, business_id) as connection:
        for table in UPROOT:
            await connection.execute(
                text(f"DELETE FROM {table} WHERE business_id = :b"),  # noqa: S608 (constants)
                {"b": business_id},
            )


@asynccontextmanager
async def seeded(owner: AsyncEngine, *letters: str) -> AsyncIterator[list[SandboxSeed]]:
    """Fresh businesses, each reset from the Paws & Co seed; removed afterwards."""
    seeds = [seed_for(new_business(letter)) for letter in letters]
    try:
        for seed in seeds:
            await seed_business(owner, seed)
        yield seeds
    finally:
        for seed in seeds:
            await uproot(owner, seed.business_id)


_FINGERPRINT: Final = (
    "SELECT count(*), coalesce(md5(string_agg(row_to_json(t)::text, chr(10)"
    " ORDER BY row_to_json(t)::text)), '') FROM {table} AS t WHERE {where}"
)


async def fingerprint(
    connection: AsyncConnection, *, except_reset_rows_of: str
) -> Mapping[str, tuple[int, str]]:
    """Every business table (from the catalog, so a new one counts the day it exists), as a row
    count and an MD5 of all its rows' JSON, in a fixed order: every row of every business, except
    `except_reset_rows_of`'s rows in the tables a reset empties. Read on the owner's connection,
    which skips RLS, so it sees every business at once."""
    prints: dict[str, tuple[int, str]] = {}
    for table in await business_tables(connection):
        where = "business_id IS DISTINCT FROM :skip" if table in RESET_TABLES else "true"
        row = (
            await connection.execute(
                text(_FINGERPRINT.format(table=table, where=where)),
                {"skip": except_reset_rows_of},
            )
        ).one()
        prints[table] = (int(row[0]), str(row[1]))
    return prints
