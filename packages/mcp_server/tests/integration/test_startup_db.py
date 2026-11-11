"""Startup against Postgres: the role check, the registry check, and the real stdio process."""

import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.fakes import CORPUS_DIR, PAWS, REPO_ROOT, SANDBOX_DIR
from tests.integration.db import (
    app_url,
    fresh_engine,
    integration,
    new_business,
    owner_url,
    seed_for,
    seeded,
    table_prints,
    tools_dir_for,
)
from tests.settings import make_settings
from vectapilot_api.db import UnsafeDatabaseRole
from vectapilot_mcp.server import NOT_CONFIRMABLE
from vectapilot_mcp.startup import NOT_REGISTERED, StartupRefused, prepare

pytestmark = [pytest.mark.anyio, integration]

PII = "canary-pii-7f3a"


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


async def test_the_owner_dsn_is_refused() -> None:
    """ADR 0009, least privilege: the owner bypasses RLS (a superuser locally and in CI), so a
    server connected as it would not stop at one business. `verify_role` refuses it before any
    business query."""
    settings = make_settings(database_url=owner_url(), tools_config_dir=SANDBOX_DIR)
    with pytest.raises(UnsafeDatabaseRole):
        await prepare(settings, "paws-and-co", "cust-001")


async def test_the_app_role_starts_for_a_registered_business(
    owner: AsyncEngine, tmp_path: Path
) -> None:
    a = new_business("a")
    settings = make_settings(
        database_url=app_url(), tools_config_dir=tools_dir_for(tmp_path / "t", a)
    )
    async with seeded(owner, seed_for(a)):
        prepared = await prepare(settings, a, "cust-001")
        try:
            assert prepared.executor.business_id == a
            assert prepared.customer_ref == "cust-001"
            assert prepared.database.role_verified is True
        finally:
            await prepared.database.engine.dispose()


async def test_an_unregistered_business_is_refused(tmp_path: Path) -> None:
    """A tool folder alone is not enough: the business must exist in the database's registry."""
    never_seeded = new_business("z")
    settings = make_settings(
        database_url=app_url(), tools_config_dir=tools_dir_for(tmp_path / "t", never_seeded)
    )
    with pytest.raises(StartupRefused) as refused:
        await prepare(settings, never_seeded, "cust-001")
    assert str(refused.value) == NOT_REGISTERED


async def test_the_command_serves_over_stdio(owner: AsyncEngine, tmp_path: Path) -> None:
    """End to end, as a host runs it: `python -m vectapilot_mcp` as a child process, the client
    over its stdin and stdout. Logs go to stderr, so the protocol stream stays parseable."""
    a = new_business("a")
    tools_dir = tools_dir_for(tmp_path / "t", a)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "VECTAPILOT_ENVIRONMENT": "test",
        "VECTAPILOT_DATABASE_URL": app_url(),
        "VECTAPILOT_REDIS_URL": os.environ.get("VECTAPILOT_REDIS_URL", "redis://127.0.0.1:1/0"),
        "VECTAPILOT_TOOLS_CONFIG_DIR": str(tools_dir),
        "VECTAPILOT_CORPUS_DIR": str(CORPUS_DIR),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vectapilot_mcp", "--business-id", a, "--customer-ref", "cust-001"],
        env=env,
        cwd=REPO_ROOT / "packages" / "mcp_server",
    )
    async with seeded(owner, seed_for(a)):
        before = await table_prints(owner, a)
        with (tmp_path / "stderr.log").open("w", encoding="utf-8") as errlog:
            # A hang (a stuck connect, a pipe deadlock) fails this test by name, not the CI job.
            with anyio.fail_after(30):
                async with Client(stdio_client(parameters, errlog=errlog)) as client:
                    listed = [tool.name for tool in (await client.list_tools()).tools]
                    order = await client.call_tool("order_status", {"order_id": "1042"})
                    booked = await client.call_tool("book", {"slot_id": "S-03"})
                    await client.call_tool(
                        "create_ticket", {"category": "order", "summary": f"Call me at {PII}"}
                    )
        after = await table_prints(owner, a)

    assert listed == sorted(t.value for t in PAWS.config.enabled_tools)
    first = order.content[0]
    assert isinstance(first, types.TextContent)
    assert json.loads(first.text)["result"]["order_id"] == "1042"
    note = booked.content[1]
    assert isinstance(note, types.TextContent)
    assert note.text == NOT_CONFIRMABLE
    assert after == before
    stderr = (tmp_path / "stderr.log").read_text(encoding="utf-8")
    assert '"tool_call"' in stderr  # the positive control: the records are there
    # Rule 5: no argument or customer text in the log (the ticket text, the order and slot ids).
    assert PII not in stderr
    assert '"1042"' not in stderr
    assert "S-03" not in stderr
