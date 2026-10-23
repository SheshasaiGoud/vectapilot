"""Database access scoped to one business (ADR 0004, ADR 0009).

Every unit of work runs inside `business_transaction`: it opens a transaction and, before anything
else, runs `set_config('app.business_id', <id>, true)`. `true` makes the setting local to that
transaction, so it ends at commit or rollback and a pooled connection never carries one business
into the next request. Row-level security then filters every business-owned table to that business.

The API connects as `vectapilot_app`, which must not be a superuser, have BYPASSRLS or be a member
of a role that does (any of those skips RLS), nor own a relation or be a member of its owner (an
owner can run `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`). Nor may a new connection already
have a business (a default `app.business_id` from the role, the database or the connection
options). `Database.verify_role` checks all of that once per process: at startup when the
database answers (an unsafe role stops startup), otherwise before the first business transaction
(fail closed: no business query ever runs before the check passed).

Defence in depth (M3 PR 1, ADR 0009 amendment): the API's engine (`create_api_engine`) also resets
`app.business_id` whenever a connection returns to its pool (`reset_business_on_return`), so a
session-level `SET` or `set_config(..., false)`, which `business_transaction` never does, cannot
reach the next checkout either.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event, text
from sqlalchemy.engine.interfaces import AdaptedConnection, DBAPIConnection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry, PoolResetState

from vectapilot_api.gateway.types import BusinessId

logger = logging.getLogger("vectapilot_api.db")

APP_ROLE: Final = "vectapilot_app"
"""The role the API logs in as; created by migration 0002, given its password by
`vectapilot-db-provision`."""

_SET_BUSINESS: Final = text("SELECT set_config('app.business_id', :business_id, true)")

RESET_BUSINESS: Final = "RESET app.business_id"
"""What `reset_business_on_return` sends: the session value goes back to the session default:
unset (the policies read NULL and `''` alike as no business). A default from the role, the
database or the connection options would come back, so `verify_role` refuses one
(`require_no_default_business`). Only this setting: `RESET ALL` would also undo any session
setting made after connect (none today), and `DISCARD ALL` would also drop the prepared statements
SQLAlchemy's asyncpg cache still names. asyncpg's own reset query (`RESET ALL` and more) runs only
in asyncpg's pool, which SQLAlchemy does not use."""

# `pg_has_role(..., 'MEMBER')` is true for a role it could `SET ROLE` to, directly or through
# other roles; a membership in a superuser or BYPASSRLS role is as unsafe as the attribute itself.
# The same test finds relations the role owns: a role is a member of itself, and a member of the
# owner can act as the owner. Schemas named `pg_*` (Postgres reserves the prefix: catalog, toast,
# this session's temporary tables) and information_schema are system schemas, not ours.
# `session_business`: this connection's `app.business_id` before any transaction set one (`true`:
# NULL instead of an error when the session never had it).
_ROLE_ATTRIBUTES: Final = text(
    """
    SELECT r.rolsuper, r.rolbypassrls,
           current_setting('app.business_id', true) AS session_business,
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


DEFAULT_BUSINESS_REFUSED: Final = (
    "a new database connection already has app.business_id set (a default from the role, the"
    " database or the connection options): every connection would start as that business, and the"
    " pool-return RESET would restore it. Remove the default (VECTAPILOT_DATABASE_URL, ADR 0009)"
)
"""`require_no_default_business`'s refusal (shesha, 2026-10-23). Fixed text: it names the setting,
never its value."""


class UnsafeDatabaseRole(RuntimeError):
    """The API's database role or connection would skip or weaken row-level security. Startup
    must stop."""


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


def require_no_default_business(session_business: str | None) -> None:
    """Raise `UnsafeDatabaseRole` unless a new connection's `app.business_id` is unset: NULL (never
    set in the session) or `''` (what a transaction-local `set_config` leaves behind). Anything
    else is a business every connection starts with. The error never contains the value."""
    if session_business is None or session_business == "":
        return
    raise UnsafeDatabaseRole(DEFAULT_BUSINESS_REFUSED)


def reset_business_on_return(
    dbapi_connection: DBAPIConnection,
    connection_record: ConnectionPoolEntry,
    reset_state: PoolResetState,
) -> None:
    """SQLAlchemy's pool `reset` event: `RESET app.business_id` before the connection is pooled.

    Why `reset` and not `checkin` (SQLAlchemy 2.1.4, `pool/base.py`): `reset` runs on every return
    to the pool, and an exception it raises is caught by `_finalize_fairy`, logged ("Exception
    during reset or similar") and the connection invalidated: closed, never pooled again, and the
    request carries on. An exception in a `checkin` listener would skip `pool._return_conn` (the
    pool loses the slot) and reach the request. So if this RESET fails, the connection that may
    still carry a business is thrown away (fail closed). A request's cancel or deadline does not
    interrupt it. SQLAlchemy runs the close in its own shielded task and waits for it, so the
    request ends only after the RESET has finished. Only an exception, or a cancel of that close
    task itself, invalidates the connection. Known limit (shesha, 2026-10-23: a statement timeout
    for every database statement, this RESET included, comes later): until then a database that
    stops answering holds the request until the connection itself fails.

    Order: the event runs before the pool's own rollback (`_ConnectionFairy._reset`), and a RESET
    inside a transaction that is then rolled back is undone (Postgres reverts SET and RESET with
    the transaction). So any open transaction is rolled back first (a no-op when the `Connection`
    already ended it), then the RESET runs outside a transaction on the driver connection: one
    round trip, no prepared statement.

    Skipped when the connection is being terminated instead of pooled (`terminate_only`: a
    detached connection, or one left to the garbage collector, where no I/O is possible).
    """
    if reset_state.terminate_only or not reset_state.asyncio_safe:
        return
    if not isinstance(dbapi_connection, AdaptedConnection):
        raise TypeError("the pool-return reset needs an asyncio driver connection (asyncpg)")
    dbapi_connection.rollback()
    dbapi_connection.run_async(lambda connection: connection.execute(RESET_BUSINESS))


def create_api_engine(url: str, **pool_options: int) -> AsyncEngine:
    """The API's engine (ADR 0009): a pool of `vectapilot_app` connections that each reset the
    business when they return (`reset_business_on_return`). `hide_parameters`: a database error's
    text never carries the bound values (document text, vectors) into a log or a traceback.
    `pool_options` (`pool_size`, `max_overflow`) are for tests that need a pool of one."""
    engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True, **pool_options)
    # Pool events attach to the sync engine; an AsyncEngine refuses them.
    event.listen(engine.sync_engine, "reset", reset_business_on_return)
    return engine


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
        """Raise `UnsafeDatabaseRole` if the connection's role would skip RLS, or the connection
        starts with a business (`require_no_default_business`, checked after the role). Checked
        once."""
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
        require_no_default_business(row.session_business)
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
