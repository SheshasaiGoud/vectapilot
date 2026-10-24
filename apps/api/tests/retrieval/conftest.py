"""Postgres fixtures for the search tests (moved unchanged from test_vector_search.py, M3 PR 3).

Only integration tests request them (`RUN_INTEGRATION=1`). The owner
(VECTAPILOT_MIGRATION_DATABASE_URL) registers and removes the test businesses, and in some tests
searches where RLS cannot help; every other row is written and searched as the app role.
"""

import secrets
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.retrieval.corpus import REGISTER, UPROOT
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import business_transaction


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        str(MigrationSettings().migration_database_url.get_secret_value()), poolclass=NullPool
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    """The app role (`vectapilot_app`), under RLS."""
    engine = create_async_engine(
        str(Settings().database_url.get_secret_value()), poolclass=NullPool
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def businesses(owner_engine: AsyncEngine) -> AsyncIterator[tuple[str, str]]:
    """Two fresh fictional businesses, removed with all their rows afterwards."""
    pair = (f"retrieval-a-{secrets.token_hex(4)}", f"retrieval-b-{secrets.token_hex(4)}")
    registered: list[str] = []
    try:
        for business_id in pair:
            async with business_transaction(owner_engine, business_id) as connection:
                await connection.execute(REGISTER, {"b": business_id})
            registered.append(business_id)
        yield pair
    finally:
        for business_id in registered:
            async with business_transaction(owner_engine, business_id) as connection:
                for statement in UPROOT:
                    await connection.execute(statement, {"b": business_id})
