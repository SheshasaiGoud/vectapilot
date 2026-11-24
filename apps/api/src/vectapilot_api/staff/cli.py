"""`uv run python -m vectapilot_api.staff add|issue-link|disable` (M8 PR 1; S5, D4 (a)).

The owner's commands for staff access, as the owner (`VECTAPILOT_MIGRATION_DATABASE_URL`, as
Alembic and `python -m vectapilot_api.tools`), one transaction each (`staff.owner`):

    add --business <id> --name "<synthetic display name>" --role reviewer|admin
    issue-link --business <id> --staff <staff id>
    disable --business <id> --staff <staff id>

- **`add`** prints the new member's id. Display names are synthetic (rule 5: no real person's
  name while Q4c is open); the command cannot tell, so it says so.
- **`issue-link`** prints `<public_origin>/staff/login#b=<business_id>&t=<token>`, valid for
  `VECTAPILOT_STAFF_LINK_LIFETIME_SECONDS` (P1: 15 minutes) and usable once. The token is in the
  URL **fragment**, which a browser never sends to a server, so it never reaches an access log or
  a `Referer` (S3). `b=` repeats the token's own prefix for the login page to show [proposal]; the
  API reads only the token. The link is a bearer credential printed in a terminal (the plan's
  Risks): it is never logged or stored, only its SHA-256.
- **`disable`** takes effect on the member's next request, and revokes every live session of
  theirs in the same transaction (`staff.owner.disable_member`).

**Refused** (exit 1, before any connection): an environment outside local, test and ci, or none
(`VECTAPILOT_ENVIRONMENT` has no default here: this command holds the owner's credential, as the
sandbox reset); no owner URL; a business id, staff id or name that is not valid; `issue-link`
without `VECTAPILOT_PUBLIC_ORIGIN`; and, after the database answered, a member that is missing,
disabled (for `issue-link`) or already disabled. A database error prints its type and SQLSTATE
only, never its text. Exit codes: 0 done · 1 refused or failed · 2 invalid arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, TextIO
from uuid import UUID

from pydantic import Field, TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.auth.origins import Origin
from vectapilot_api.auth.store import DISPLAY_NAME_MAX, MAX_LINK_LIFETIME_S, StaffRole
from vectapilot_api.config import Environment, MigrationSettings
from vectapilot_api.db import InvalidBusinessId, business_transaction, parse_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.staff.owner import IssuedLink, add_member, disable_member, issue_link
from vectapilot_api.tools.config import SingleLine

STAFF_CLI_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
"""Where the command runs until M10 decides production staff onboarding (D4 (a))."""
LOGIN_PATH: Final = "/staff/login"
"""The dashboard's login page (PR 5), served by the API under D2 (a)."""

_DISPLAY_NAME: Final = TypeAdapter(Annotated[SingleLine, Field(max_length=DISPLAY_NAME_MAX)])


class StaffCliSettings(BaseSettings):
    """What the command reads besides the owner URL: the environment (required), the API's public
    origin (for the link) and the link's lifetime."""

    model_config = SettingsConfigDict(
        env_prefix="VECTAPILOT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Environment
    public_origin: Origin | None = None
    staff_link_lifetime_seconds: int = Field(default=900, gt=0, le=MAX_LINK_LIFETIME_S)


@dataclass(frozen=True)
class Add:
    business_id: BusinessId
    display_name: str
    role: StaffRole


@dataclass(frozen=True)
class IssueLink:
    business_id: BusinessId
    staff_id: UUID
    lifetime_s: int
    public_origin: str
    """The API's origin the printed link names (`VECTAPILOT_PUBLIC_ORIGIN`, required here)."""


@dataclass(frozen=True)
class Disable:
    business_id: BusinessId
    staff_id: UUID


Command = Add | IssueLink | Disable
Outcome = UUID | IssuedLink | bool | None
"""`add`: the new id · `issue-link`: the link, or None (no enabled member) · `disable`: whether
one was disabled now."""


async def run_with_owner(url: str, command: Command) -> Outcome:
    """`command` in one owner transaction of its business (committed on success)."""
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with business_transaction(engine, command.business_id) as connection:
            if isinstance(command, Add):
                return await add_member(
                    connection, command.business_id, command.display_name, command.role
                )
            if isinstance(command, IssueLink):
                return await issue_link(
                    connection, command.business_id, command.staff_id, lifetime_s=command.lifetime_s
                )
            return await disable_member(connection, command.business_id, command.staff_id)
    finally:
        await engine.dispose()


Run = Callable[[str, Command], Outcome]


def run_owner(url: str, command: Command) -> Outcome:
    return asyncio.run(run_with_owner(url, command))


def owner_url() -> str | None:
    try:
        return str(MigrationSettings().migration_database_url.get_secret_value())
    except ValidationError:
        return None


def login_link(public_origin: str, business_id: str, token: str) -> str:
    """The link `issue-link` prints: the token only in the fragment (module docstring)."""
    return f"{public_origin}{LOGIN_PATH}#b={business_id}&t={token}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.staff",
        description="Staff members and one-time login links (M8; owner; local, test and ci).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="add a staff member (synthetic display name)")
    add.add_argument("--business", required=True, help="the business id")
    add.add_argument("--name", required=True, help="a synthetic display name, 1-80 characters")
    add.add_argument("--role", required=True, choices=[role.value for role in StaffRole])
    for name, text in (
        ("issue-link", "print a one-time login link for an enabled member"),
        ("disable", "disable a member (from their next request)"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("--business", required=True, help="the business id")
        command.add_argument("--staff", required=True, help="the staff member's id (a uuid)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Callable[[], StaffCliSettings] = StaffCliSettings,
    url: Callable[[], str | None] = owner_url,
    run: Run = run_owner,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    arguments = build_parser().parse_args(argv)
    action: str = arguments.command

    def refuse(message: str) -> int:
        print(f"staff {action} refused: {message}", file=stderr)
        return 1

    try:
        cli_settings = settings()
    except ValidationError as error:
        if any(e["loc"][:1] == ("environment",) for e in error.errors()):
            return refuse("set VECTAPILOT_ENVIRONMENT to local, test or ci")
        return refuse(
            "VECTAPILOT_PUBLIC_ORIGIN or VECTAPILOT_STAFF_LINK_LIFETIME_SECONDS is not valid"
        )
    if cli_settings.environment not in STAFF_CLI_ENVIRONMENTS:
        return refuse("staff commands run only in local, test and ci (production login is M10's)")
    try:
        business_id = parse_business_id(arguments.business)
    except InvalidBusinessId:
        return refuse("--business is not a valid business id")
    command: Command
    if action == "add":
        try:
            display_name = _DISPLAY_NAME.validate_python(arguments.name)
        except ValidationError:
            return refuse(f"--name is one trimmed line of 1-{DISPLAY_NAME_MAX} characters")
        command = Add(business_id, display_name, StaffRole(arguments.role))
    else:
        try:
            staff_id = UUID(arguments.staff)
        except ValueError:
            return refuse("--staff is not a staff id (a uuid)")
        if action == "disable":
            command = Disable(business_id, staff_id)
        elif cli_settings.public_origin is None:
            return refuse("VECTAPILOT_PUBLIC_ORIGIN is not set: the link names the API's origin")
        else:
            command = IssueLink(
                business_id,
                staff_id,
                cli_settings.staff_link_lifetime_seconds,
                cli_settings.public_origin,
            )
    owner = url()
    if owner is None:
        return refuse("VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set")
    try:
        outcome = run(owner, command)
    except (SQLAlchemyError, OSError) as error:
        state = getattr(getattr(error, "orig", None), "sqlstate", None)
        detail = type(error).__name__ + (f" (SQLSTATE {state})" if state else "")
        print(f"staff {action} failed, nothing changed: {detail}", file=stderr)
        return 1
    return _report(command, outcome, stdout, refuse)


def _report(
    command: Command, outcome: Outcome, stdout: TextIO, refuse: Callable[[str], int]
) -> int:
    business = command.business_id
    if isinstance(command, Add) and isinstance(outcome, UUID):
        print(
            f"staff add: {business}: {outcome} ({command.role.value}); display names are"
            " synthetic only (rule 5)",
            file=stdout,
        )
        return 0
    if isinstance(command, IssueLink) and isinstance(outcome, IssuedLink):
        print(login_link(command.public_origin, business, outcome.token), file=stdout)
        print(
            f"single use, valid until {outcome.expires_at.isoformat()} (database clock);"
            " a bearer credential: open it, never paste it into a chat or a ticket",
            file=stdout,
        )
        return 0
    if isinstance(command, Disable) and outcome is True:
        print(f"staff disable: {business}: {command.staff_id} disabled", file=stdout)
        return 0
    # `issue-link` found no enabled member, or `disable` found none still enabled (`add` always
    # returns an id).
    return refuse("no enabled staff member with that id in this business")
