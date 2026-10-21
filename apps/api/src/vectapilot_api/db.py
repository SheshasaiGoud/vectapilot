"""Database access scoped to one business (ADR 0004, ADR 0009).

Every unit of work runs inside `business_transaction`: it opens a transaction and, before anything
else, runs `set_config('app.business_id', <id>, true)`. `true` makes the setting local to that
transaction, so it ends at commit or rollback and a pooled connection never carries one business
into the next request. Row-level security then filters every business-owned table to that business.

The API connects as `vectapilot_app`, which must not be a superuser, have BYPASSRLS or be a member
of a role that does (any of those skips RLS), nor own a relation or be a member of its owner (an
owner can run `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`). `Database.verify_role` checks that
once per process: at startup when the database answers (an unsafe role stops startup), otherwise
before the first business transaction (fail closed: no business query ever runs before the check
passed).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vectapilot_api.gateway.types import BusinessId

logger = logging.getLogger("vectapilot_api.db")

APP_ROLE: Final = "vectapilot_app"
"""The role the API logs in as; created by migration 0002, given its password by
`vectapilot-db-provision`."""

_SET_BUSINESS: Final = text("SELECT set_config('app.business_id', :business_id, true)")

# `pg_has_role(..., 'MEMBER')` is true for a role it could `SET ROLE` to, directly or through
# other roles; a membership in a superuser or BYPASSRLS role is as unsafe as the attribute itself.
# The same test finds relations the role owns: a role is a member of itself, and a member of the
# owner can act as the owner. Schemas named `pg_*` (Postgres reserves the prefix: catalog, toast,
# this session's temporary tables) and information_schema are system schemas, not ours.
_ROLE_ATTRIBUTES: Final = text(
    """
    SELECT r.rolsuper, r.rolbypassrls,
           EXISTS (SELECT 1 FROM pg_catalog.pg_roles p
                   WHERE (p.rolsuper OR p.rolbypassrls) AND p.oid <> r.oid
                     AND pg_has_role(r.oid, p.oid, 'MEMBER')) AS member_of_privileged,
           EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                   JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                   WHERE NOT starts_with(n.nspname, 'pg_')
                     AND n.nspname <> 'information_schema'
                     AND pg_has_role(r.oid, c.relowner, 'MEMBER')) AS owns_relations
    FROM pg_catalog.pg_roles r
    WHERE r.rolname = current_user
    """
)

_BUSINESS_EXISTS: Final = text(
    "SELECT EXISTS (SELECT 1 FROM businesses WHERE business_id = :business_id)"
)

_business_id: Final = TypeAdapter(BusinessId)


class InvalidBusinessId(ValueError):
    """Not a `BusinessId`. The message never contains the value: it may be caller input."""


class UnsafeDatabaseRole(RuntimeError):
    """The API's database role would skip row-level security. Startup must stop."""


def parse_business_id(value: object) -> BusinessId:
    """`value` as a `BusinessId`, or `InvalidBusinessId` (without the value, unlike pydantic's)."""
    try:
        return _business_id.validate_python(value)
    except ValidationError:
        raise InvalidBusinessId("not a valid business id") from None


def role_problems(
    *, superuser: bool, bypassrls: bool, member_of_privileged: bool, owns_relations: bool
) -> list[str]:
    """What lets a role skip or switch off row-level security, as text; empty when it is safe."""
    problems: list[str] = []
    if superuser:
        problems.append("is a superuser")
    if bypassrls:
        problems.append("has BYPASSRLS")
    if member_of_privileged:
        problems.append("is a member of a superuser or BYPASSRLS role")
    if owns_relations:
        problems.append("owns a table or other relation, or is a member of its owner")
    return problems


@asynccontextmanager
async def business_transaction(
    engine: AsyncEngine, business_id: str
) -> AsyncIterator[AsyncConnection]:
    """A transaction in which only `business_id`'s rows exist. Commits on success, rolls back on
    an exception; either way the business setting ends with the transaction.

    The one place that sets the business. One business per transaction: never call `set_config`
    for another business inside it. The API goes through `Database.business_transaction`, which
    checks the role first; owner scripts and tests may call this directly.
    """
    valid = parse_business_id(business_id)
    async with engine.begin() as connection:
        await connection.execute(_SET_BUSINESS, {"business_id": valid})
        yield connection


class Database:
    """The API's engine plus the role check that must pass before any business query."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._role_verified = False

    @property
    def role_verified(self) -> bool:
        return self._role_verified

    async def verify_role(self) -> None:
        """Raise `UnsafeDatabaseRole` if the connection's role would skip RLS. Checked once."""
        if self._role_verified:
            return
        async with self.engine.connect() as connection:
            row = (await connection.execute(_ROLE_ATTRIBUTES)).one()
        problems = role_problems(
            superuser=row.rolsuper,
            bypassrls=row.rolbypassrls,
            member_of_privileged=row.member_of_privileged,
            owns_relations=row.owns_relations,
        )
        if problems:
            raise UnsafeDatabaseRole(
                f"the API's database role {', '.join(problems)}: row-level security would not"
                f" apply. Connect as {APP_ROLE} (VECTAPILOT_DATABASE_URL, ADR 0009)"
            )
        self._role_verified = True

    async def verify_role_at_startup(self, timeout_s: float) -> None:
        """`verify_role`, but a database that is down or slow defers the check instead of
        stopping startup: liveness must not depend on the database (`/readyz` reports it), and
        `business_transaction` runs the check before the first business query anyway."""
        try:
            async with asyncio.timeout(timeout_s):
                await self.verify_role()
        except (TimeoutError, OSError, SQLAlchemyError) as error:
            logger.warning("database_role_check_deferred", extra={"reason": type(error).__name__})

    @asynccontextmanager
    async def business_transaction(self, business_id: str) -> AsyncIterator[AsyncConnection]:
        """`business_transaction(self.engine, business_id)` once the role check has passed."""
        valid = parse_business_id(business_id)  # before the database: a bad id costs no query
        await self.verify_role()
        async with business_transaction(self.engine, valid) as connection:
            yield connection

    async def business_exists(self, business_id: str) -> bool:
        """Whether the registry holds `business_id`. RLS shows only that row, and the query also
        names it (defence in depth)."""
        async with self.business_transaction(business_id) as connection:
            result = await connection.execute(_BUSINESS_EXISTS, {"business_id": business_id})
            return bool(result.scalar_one())
