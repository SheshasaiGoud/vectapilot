"""`uv run python -m vectapilot_api.extraction purge --business <id>` (M6 PR 3, E10).

Deletes one business's expired extractions and their fields, as the owner
(`VECTAPILOT_MIGRATION_DATABASE_URL`, as Alembic), in one transaction (`purge.purge_expired`): on
any error nothing changes. It reads no configuration folder: a business whose extraction file was
removed still has rows that must expire and go.

**Refused** (exit 1, before any connection): an environment outside `local`, `test` and `ci`, or
none (`VECTAPILOT_ENVIRONMENT` has no default here, unlike the API's `local`: this command holds
the owner's credential, so a shell that only has an owner URL must not pass the guard by saying
nothing, as M5's sandbox reset); a malformed business id; no owner URL. A database error prints its
type and SQLSTATE only, never its text (a row's values could be in it). Exit codes: 0 purged ·
1 refused or failed · 2 invalid arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.config import Environment, MigrationSettings
from vectapilot_api.db import InvalidBusinessId, business_transaction, parse_business_id
from vectapilot_api.extraction.purge import (
    PURGE_ENVIRONMENTS,
    PURGE_REFUSED,
    PurgeRefused,
    purge_expired,
)


class ExtractionCliSettings(BaseSettings):
    """What the command reads besides the owner URL: the environment, required."""

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Environment


def owner_url() -> str | None:
    try:
        return str(MigrationSettings().migration_database_url.get_secret_value())
    except ValidationError:
        return None


async def purge_with_owner(url: str, business_id: str) -> int:
    """`purge_expired` in one owner transaction for `business_id` (committed on success)."""
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with business_transaction(engine, business_id) as connection:
            return await purge_expired(connection, business_id)
    finally:
        await engine.dispose()


Purge = Callable[[str, str], int]


def run_purge(url: str, business_id: str) -> int:
    return asyncio.run(purge_with_owner(url, business_id))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.extraction", description="Extraction commands (M6)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    purge = commands.add_parser(
        "purge", help="delete one business's expired extractions (owner; local/test/ci)"
    )
    purge.add_argument("--business", required=True, help="the business id to purge")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Callable[[], ExtractionCliSettings] = ExtractionCliSettings,
    url: Callable[[], str | None] = owner_url,
    purge: Purge = run_purge,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    arguments = build_parser().parse_args(argv)

    def refuse(message: str) -> int:
        print(f"extraction purge refused: {message}", file=stderr)
        return 1

    try:
        cli_settings = settings()
    except ValidationError:
        return refuse("set VECTAPILOT_ENVIRONMENT to local, test or ci")
    if cli_settings.environment not in PURGE_ENVIRONMENTS:
        return refuse("the purge runs only in local, test and ci")
    try:
        business_id = parse_business_id(arguments.business)
    except InvalidBusinessId:
        return refuse("--business is not a valid business id")
    owner = url()
    if owner is None:
        return refuse("VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set")
    try:
        purged = purge(owner, business_id)
    except PurgeRefused:
        return refuse(PURGE_REFUSED)
    except (SQLAlchemyError, OSError) as failure:
        state = getattr(getattr(failure, "orig", None), "sqlstate", None)
        detail = type(failure).__name__ + (f" (SQLSTATE {state})" if state else "")
        print(f"extraction purge failed, nothing changed: {detail}", file=stderr)
        return 1
    print(
        f"extraction purge: {business_id}: {purged} expired extraction(s) deleted, with their"
        " fields; unexpired extractions untouched",
        file=stdout,
    )
    return 0
