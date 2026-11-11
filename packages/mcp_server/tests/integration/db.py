"""Database helpers for the integration tests: throwaway businesses seeded through the real
reset, as the owner, and removed afterwards (`tool_audit` included), as apps/api's
tests/tools/sandbox_db.py does. The fictional businesses' own ids are never written by a test.

Integration only: `RUN_INTEGRATION=1`, with `VECTAPILOT_DATABASE_URL` (the app role) and
`VECTAPILOT_MIGRATION_DATABASE_URL` (the owner) pointing at a migrated database.
"""

import json
import os
import secrets
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import runs_integration
from tests.fakes import PAWS, SANDBOX_DIR
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE, ToolsConfig
from vectapilot_api.tools.sandbox import (
    RESET_TABLES,
    SandboxSeed,
    SeedBooking,
    SeedOrder,
    SeedSlot,
    reset_sandbox,
)

integration = pytest.mark.skipif(
    not runs_integration(os.environ),
    reason="needs a migrated Postgres; set RUN_INTEGRATION=1",
)

UPROOT: Final = ("tool_audit", *RESET_TABLES, "llm_calls", "chunks", "documents", "businesses")
"""Every table a throwaway business can have rows in, children first."""

BAIT_SLOT: Final = "S-91"
BAIT_BOOKED_SLOT: Final = "S-90"
BAIT_BOOKING: Final = "B-190"
BAIT_ORDER: Final = "1090"
"""Ids only business A's seed has (`planted_seed`): seen by A, never by B. `BAIT_SLOT` is free
(so `check_slots` lists it for A); `BAIT_BOOKED_SLOT` holds `BAIT_BOOKING`."""
MISSING_SLOT: Final = "S-99"
MISSING_BOOKING: Final = "B-999"
MISSING_ORDER: Final = "9999"
"""Ids no seed has: a foreign id must answer byte-for-byte as these do."""


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


def app_url() -> str:
    return str(Settings().database_url.get_secret_value())


def fresh_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, poolclass=NullPool)


def new_business(letter: str) -> str:
    return f"mcp-{letter}-{secrets.token_hex(4)}"


def seed_for(business_id: str, seed: SandboxSeed = PAWS.seed) -> SandboxSeed:
    """`seed` for another business, with a canary of its own."""
    return seed.model_copy(update={"business_id": business_id, "canary": f"canary-{business_id}"})


def planted_seed(business_id: str) -> SandboxSeed:
    """The Paws & Co seed plus a free bait slot, a booked one with its booking and an order for
    `cust-001`, carrying the business's canary, so a leak test has something to find (the
    positive control)."""
    seed = seed_for(business_id)
    canary = seed.canary
    starts = datetime.combine(PAWS.config.sandbox_date, time(15, 0), PAWS.config.zone)
    free_slot = SeedSlot(
        id=BAIT_SLOT,
        service=PAWS.config.services[0].id,
        starts_at=starts + timedelta(days=1),
        duration_minutes=20,
        label=f"Bait room {canary}",
    )
    booked_slot = free_slot.model_copy(
        update={"id": BAIT_BOOKED_SLOT, "starts_at": starts + timedelta(days=2)}
    )
    booking = SeedBooking(id=BAIT_BOOKING, slot_id=BAIT_BOOKED_SLOT, customer_ref="cust-001")
    order = SeedOrder(
        id=BAIT_ORDER,
        customer_ref="cust-001",
        status="placed",
        items=f"Bait kibble {canary}",
        note=f"Bait note {canary}",
        placed_at=starts,
    )
    return seed.model_copy(
        update={
            "slots": (*seed.slots, free_slot, booked_slot),
            "bookings": (*seed.bookings, booking),
            "orders": (*seed.orders, order),
        }
    )


def config_for(business_id: str, **changes: object) -> ToolsConfig:
    data = PAWS.config.model_dump(mode="json") | {"business_id": business_id} | changes
    return ToolsConfig.model_validate(data)


async def seed_business(owner: AsyncEngine, seed: SandboxSeed) -> None:
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
async def seeded(owner: AsyncEngine, *seeds: SandboxSeed) -> AsyncIterator[None]:
    """`seeds` reset into the database, each removed afterwards."""
    try:
        for seed in seeds:
            await seed_business(owner, seed)
        yield
    finally:
        for seed in seeds:
            await uproot(owner, seed.business_id)


_PRINT: Final = (
    "SELECT count(*), coalesce(md5(string_agg(row_to_json(t)::text, chr(10)"
    " ORDER BY row_to_json(t)::text)), '') FROM {table} AS t WHERE business_id = :b"
)
"""Filled with a constant table name only (`table_prints`)."""


async def table_prints(owner: AsyncEngine, business_id: str) -> Mapping[str, tuple[int, str]]:
    """Row count and an MD5 of every row of the business's write tables, read as the owner."""
    prints: dict[str, tuple[int, str]] = {}
    async with business_transaction(owner, business_id) as connection:
        for table in ("pending_actions", "sandbox_bookings", "sandbox_tickets"):
            row = (
                await connection.execute(text(_PRINT.format(table=table)), {"b": business_id})
            ).one()
            prints[table] = (int(row[0]), str(row[1]))
    return prints


async def audit_rows(owner: AsyncEngine, business_id: str) -> list[dict[str, object]]:
    async with business_transaction(owner, business_id) as connection:
        result = await connection.execute(
            text(
                "SELECT tool, kind, phase, outcome, trace_id, action_id FROM tool_audit"
                " WHERE business_id = :b ORDER BY created_at"
            ),
            {"b": business_id},
        )
        return [dict(row._mapping) for row in result]


def tools_dir_for(destination: Path, *business_ids: str) -> Path:
    """A tool configuration folder holding a copy of Paws & Co's files per business id."""
    destination.mkdir()
    for business_id in business_ids:
        folder = destination / business_id
        shutil.copytree(SANDBOX_DIR / "paws-and-co", folder)
        for name in (TOOLS_FILE, SEED_FILE):
            data = json.loads((folder / name).read_text(encoding="utf-8"))
            data["business_id"] = business_id
            (folder / name).write_text(json.dumps(data), encoding="utf-8")
    return destination
