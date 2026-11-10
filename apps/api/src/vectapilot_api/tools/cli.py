"""`uv run python -m vectapilot_api.tools sandbox reset --business <id>` (M5 PR 1).

Reseeds one business's sandbox from `<config dir>/<business_id>/seed.json`, as the owner
(`VECTAPILOT_MIGRATION_DATABASE_URL`, as Alembic): it registers the business if it is missing,
deletes that business's sandbox rows and pending actions, inserts the seed, and leaves
`tool_audit` alone (`sandbox.reset_sandbox`). One transaction: on any error nothing changes.

The config dir is `--config-dir`, else `VECTAPILOT_TOOLS_CONFIG_DIR`, else the nearest
`data/sandbox` at or above the current directory (the repository's). The whole folder is
validated first, as the API does at startup, so a reset never seeds a business the API would
refuse.

**Refused** (exit 1, before any connection): an environment outside `local`, `test` and `ci`, or
none (`VECTAPILOT_ENVIRONMENT` has no default here, unlike the API's `local`: this command holds the
owner's credential, so a shell that only has an owner URL must not pass the guard by saying
nothing [PR 1 review, diego and zara]); no owner URL; a business with no
folder; an invalid folder. A database error prints its type and SQLSTATE only, never its text
(a key's values would be in it). Exit codes: 0 reset · 1 refused or failed · 2 invalid arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, TextIO

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.config import Environment, MigrationSettings
from vectapilot_api.db import InvalidBusinessId, business_transaction, parse_business_id
from vectapilot_api.tools.config import ToolsConfigError
from vectapilot_api.tools.files import load_tools_dir
from vectapilot_api.tools.sandbox import (
    SANDBOX_ENVIRONMENTS,
    ResetCounts,
    SandboxRefused,
    SandboxSeed,
    reset_sandbox,
)

DEFAULT_DIR: Final = Path("data", "sandbox")
"""The repository's sandbox folder, relative to the repository root."""


class ToolsCliSettings(BaseSettings):
    """What the command reads besides the owner URL: the environment (required) and the config
    dir."""

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Environment
    tools_config_dir: Path | None = None


def find_sandbox_dir(start: Path) -> Path | None:
    """The nearest `data/sandbox` at or above `start`."""
    for folder in (start.resolve(), *start.resolve().parents):
        if (folder / DEFAULT_DIR).is_dir():
            return folder / DEFAULT_DIR
    return None


def owner_url() -> str | None:
    try:
        return str(MigrationSettings().migration_database_url.get_secret_value())
    except ValidationError:
        return None


async def reset_with_owner(url: str, seed: SandboxSeed) -> ResetCounts:
    """`reset_sandbox` in one owner transaction for the seed's business (committed on success)."""
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with business_transaction(engine, seed.business_id) as connection:
            return await reset_sandbox(connection, seed)
    finally:
        await engine.dispose()


Reset = Callable[[str, SandboxSeed], ResetCounts]


def run_reset(url: str, seed: SandboxSeed) -> ResetCounts:
    return asyncio.run(reset_with_owner(url, seed))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.tools", description="Tool and sandbox commands (M5)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sandbox = commands.add_parser("sandbox", help="the sandbox backend")
    actions = sandbox.add_subparsers(dest="action", required=True)
    reset = actions.add_parser("reset", help="reseed one business's sandbox (owner; local/test/ci)")
    reset.add_argument("--business", required=True, help="the business id to reseed")
    reset.add_argument("--config-dir", type=Path, help="the tool configuration folder")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Callable[[], ToolsCliSettings] = ToolsCliSettings,
    url: Callable[[], str | None] = owner_url,
    reset: Reset = run_reset,
    cwd: Callable[[], Path] = Path.cwd,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    arguments = build_parser().parse_args(argv)

    def refuse(message: str) -> int:
        print(f"sandbox reset refused: {message}", file=stderr)
        return 1

    try:
        cli_settings = settings()
    except ValidationError as error:
        if any(e["loc"][:1] == ("environment",) for e in error.errors()):
            return refuse("set VECTAPILOT_ENVIRONMENT to local, test or ci")
        return refuse("VECTAPILOT_TOOLS_CONFIG_DIR is not valid")
    if cli_settings.environment not in SANDBOX_ENVIRONMENTS:
        return refuse("the sandbox reset runs only in local, test and ci")
    try:
        business_id = parse_business_id(arguments.business)
    except InvalidBusinessId:
        return refuse("--business is not a valid business id")
    directory = arguments.config_dir or cli_settings.tools_config_dir or find_sandbox_dir(cwd())
    if directory is None:
        return refuse("no --config-dir, no VECTAPILOT_TOOLS_CONFIG_DIR and no data/sandbox found")
    try:
        businesses = load_tools_dir(directory)
    except ToolsConfigError as error:
        return refuse(str(error))
    if business_id not in businesses:
        return refuse(f"{business_id} has no sandbox in the tool configuration folder")
    owner = url()
    if owner is None:
        return refuse("VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set")
    seed = businesses[business_id].seed
    try:
        counts = reset(owner, seed)
    except SandboxRefused as error:
        return refuse(str(error))
    except (SQLAlchemyError, OSError) as error:
        state = getattr(getattr(error, "orig", None), "sqlstate", None)
        detail = type(error).__name__ + (f" (SQLSTATE {state})" if state else "")
        print(f"sandbox reset failed, nothing changed: {detail}", file=stderr)
        return 1
    print(
        f"sandbox reset: {business_id}: {counts.slots} slots, {counts.bookings} bookings,"
        f" {counts.orders} orders (synthetic seed); tool_audit untouched",
        file=stdout,
    )
    return 0
