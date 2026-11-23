"""`uv run python -m vectapilot_api.guards scan --business <id>` (M7 PR 7; ADR 0018 part 10).

Lists one business's approved chunks that the chunk screen flags, by id and code, for staff to
act on (`scan.py`). It connects as the API does, as `vectapilot_app` from
`VECTAPILOT_DATABASE_URL`, so the role check and RLS scope every read to the business
(`Database.business_transaction`); it never holds the owner's credential and writes nothing.

**Before any connection** it reads the guard folder (`VECTAPILOT_GUARD_CONFIG_DIR`, with
`VECTAPILOT_CORPUS_DIR`, as `create_app` does) and runs G14's self-test on the matcher it will use
[proposal]: a matcher that fails its own terms would list nothing and read as "clean". While H1
is a stub the self-test says `matcher_unavailable` and the scan is refused: nothing is listed
(G3).

**Output** (stdout): `scan.ScanReport.lines`, a count line, then `<chunk id> <codes>` per flagged
chunk; never a chunk's text, a term or a term id. Refusals and failures go to stderr, naming
settings, files, class names and SQLSTATEs only (a database message can hold row values).

Exit codes: 0 scanned (flagged chunks or none) · 1 refused or failed (nothing listed) · 2 invalid
arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TextIO

from pydantic import PostgresDsn, Secret, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.db import Database, InvalidBusinessId, UnsafeDatabaseRole, parse_business_id
from vectapilot_api.guards.config import (
    BusinessGuards,
    GuardConfig,
    GuardConfigError,
    load_guard_dir,
)
from vectapilot_api.guards.match import match_terms
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.scan import DatabaseChunks, ScanFailed, ScanReport, scan_business
from vectapilot_api.guards.selftest import (
    SelftestError,
    SelftestResult,
    SelftestStatus,
    run_selftest,
)

PROG = "python -m vectapilot_api.guards"


class ScanSettings(BaseSettings):
    """What the command reads: the API's own database URL (the app role) and the guard folder."""

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: Secret[PostgresDsn]
    guard_config_dir: Path
    corpus_dir: Path


Scan = Callable[[str, str, BusinessGuards, Matcher], Coroutine[object, object, ScanReport]]
"""(the app role's URL, the business, its guards, the matcher) -> the report."""


async def scan_as_app(
    url: str, business_id: str, guards: BusinessGuards, matcher: Matcher
) -> ScanReport:
    """The scan on its own engine as `vectapilot_app` (`Database`: the role check first). One
    connection, closed on return (`NullPool`): nothing is pooled, so no business can carry over;
    `hide_parameters` as the API's engine (`db.create_api_engine`)."""
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        return await scan_business(DatabaseChunks(Database(engine)), business_id, guards, matcher)
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG, description="Guard commands (M7).")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser(
        "scan", help="list one business's approved chunks the chunk screen flags (ids and codes)"
    )
    scan.add_argument("--business", required=True, help="the business id to scan")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Callable[[], ScanSettings] = ScanSettings,
    load: Callable[[Path, Path], GuardConfig] = load_guard_dir,
    matcher: Matcher = match_terms,
    selftest: Callable[[GuardConfig, Matcher], SelftestResult] = run_selftest,
    scan: Scan = scan_as_app,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    arguments = build_parser().parse_args(argv)

    def refuse(message: str) -> int:
        print(f"guards scan refused, nothing listed: {message}", file=stderr)
        return 1

    try:
        business_id = parse_business_id(arguments.business)
    except InvalidBusinessId:
        return refuse("--business is not a valid business id")
    try:
        scan_settings = settings()
    except ValidationError as error:
        names = sorted(
            {f"VECTAPILOT_{str(e['loc'][0]).upper()}" for e in error.errors() if e["loc"]}
        )
        return refuse(f"set {', '.join(names) or 'the settings'}")
    try:
        config = load(scan_settings.guard_config_dir, scan_settings.corpus_dir)
        checked = selftest(config, matcher)
    except (GuardConfigError, SelftestError) as error:
        return refuse(str(error))  # names files, locations and variants, never a term
    except Exception as error:  # a matcher that raised otherwise: fail closed, the class only
        return refuse(f"the self-test raised {type(error).__name__}")
    if checked.status is not SelftestStatus.PASSED:
        return refuse(f"the term matcher is not available ({checked.status.value}, M7 H1)")
    url = str(scan_settings.database_url.get_secret_value())
    try:
        report = asyncio.run(scan(url, business_id, config.for_business(business_id), matcher))
    except UnsafeDatabaseRole:
        return refuse("VECTAPILOT_DATABASE_URL must connect as vectapilot_app (ADR 0009)")
    except ScanFailed as failed:
        print(
            f"guards scan failed, nothing listed: the screen raised {failed.error_type}",
            file=stderr,
        )
        return 1
    except (SQLAlchemyError, OSError) as failure:
        state = getattr(getattr(failure, "orig", None), "sqlstate", None)
        detail = type(failure).__name__ + (f" (SQLSTATE {state})" if state else "")
        print(f"guards scan failed, nothing listed: {detail}", file=stderr)
        return 1
    for line in report.lines():
        print(line, file=stdout)
    return 0
