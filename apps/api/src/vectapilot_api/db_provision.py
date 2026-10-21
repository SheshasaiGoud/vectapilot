"""Turn on the app role's login and set its password (ADR 0009, "Role and password").

Run after `alembic upgrade head` (migration 0002 creates `vectapilot_app` NOLOGIN, without a
password, so no secret is ever in a migration):

    uv run vectapilot-db-provision

It reads two URLs (`MigrationSettings`): the owner's, to connect, and the API's, whose user must be
`vectapilot_app` and whose password becomes the role's. So the API's URL is the password's one
source, and changing it in `infra/.env` then running `make up` again rotates it. Idempotent.

The password is sent as an SQL literal (`ALTER ROLE` takes no bind parameters), quoted by Postgres'
own `format('%L')`. Postgres stores only its SCRAM hash. Error messages carry the error's class
name only, never the statement, which holds the password.
"""

import asyncio
from typing import Final

from sqlalchemy import make_url, pool, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from vectapilot_api.config import MigrationSettings
from vectapilot_api.db import APP_ROLE

_ALTER_ROLE: Final = text("SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :role, :password)")


class ProvisionError(RuntimeError):
    """Provisioning refused or failed. Messages never contain a URL or a password."""


def app_password(owner_url: str, app_url: str) -> str:
    """The password to set on `APP_ROLE`, after checking the API's URL really is the app role's.

    Refuses a URL for any other user: running this with the owner's URL as the API's would
    otherwise change the owner's password and point the API at a superuser.
    """
    owner, app = make_url(owner_url), make_url(app_url)
    if app.username != APP_ROLE:
        raise ProvisionError(f"VECTAPILOT_DATABASE_URL must log in as {APP_ROLE}")
    if not app.password:
        raise ProvisionError("VECTAPILOT_DATABASE_URL must contain the app role's password")
    if (owner.host, owner.port, owner.database) != (app.host, app.port, app.database):
        raise ProvisionError("both database URLs must point to the same database")
    return str(app.password)


async def set_login_password(engine: AsyncEngine, role: str, password: str) -> None:
    """`ALTER ROLE <role> WITH LOGIN PASSWORD <password>`, run by `engine`'s (owner) role."""
    try:
        async with engine.begin() as connection:
            result = await connection.execute(_ALTER_ROLE, {"role": role, "password": password})
            # Raw driver SQL: SQLAlchemy's `text()` would treat a ":word" in the password as a
            # bind parameter.
            await connection.exec_driver_sql(result.scalar_one())
    except (OSError, SQLAlchemyError) as error:
        reason = type(error).__name__
        raise ProvisionError(f"could not set the password of {role} ({reason})") from None


async def provision(owner_url: str, app_url: str) -> None:
    password = app_password(owner_url, app_url)
    engine = create_async_engine(owner_url, poolclass=pool.NullPool)
    try:
        await set_login_password(engine, APP_ROLE, password)
    finally:
        await engine.dispose()


def main() -> None:
    settings = MigrationSettings()
    if settings.database_url is None:
        raise SystemExit("VECTAPILOT_DATABASE_URL (the API's URL, as vectapilot_app) is required")
    owner_url = str(settings.migration_database_url.get_secret_value())
    app_url = str(settings.database_url.get_secret_value())
    try:
        asyncio.run(provision(owner_url, app_url))
    except ProvisionError as error:
        raise SystemExit(str(error)) from None
    print(f"{APP_ROLE}: login enabled, password set")
