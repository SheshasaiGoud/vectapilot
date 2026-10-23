"""db.py without a database: the business-id check, the role rules, and the startup gate.

The same code against real Postgres (RLS, pooled connections, unsafe roles) is in test_rls.py.
"""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, Final, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry, PoolResetState

from tests.conftest import make_settings
from vectapilot_api.config import Settings
from vectapilot_api.db import (
    APP_ROLE,
    DEFAULT_BUSINESS_REFUSED,
    Database,
    InvalidBusinessId,
    UnsafeDatabaseRole,
    business_transaction,
    create_api_engine,
    parse_business_id,
    require_no_default_business,
    reset_business_on_return,
    role_problems,
)
from vectapilot_api.main import create_app

MIGRATION_0002 = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "0002_app_role_and_business_tables.py"
)


def test_parse_business_id_accepts_a_slug() -> None:
    assert parse_business_id("sunrise-clinic") == "sunrise-clinic"


@pytest.mark.parametrize(
    "value",
    [None, "", "ab", "a" * 41, "Sunrise-Clinic", "sunrise\n", "-abc", "ab--cd", "a b c", 123],
)
def test_parse_business_id_refuses_without_echoing_the_value(value: object) -> None:
    with pytest.raises(InvalidBusinessId) as error:
        parse_business_id(value)

    assert str(error.value) == "not a valid business id"
    assert error.value.__suppress_context__, "pydantic's error (which holds the input) is hidden"


OWNS: Final = "owns a table or other relation, or is a member of its owner"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((False, False, False, False), []),
        ((True, False, False, False), ["is a superuser"]),
        ((False, True, False, False), ["has BYPASSRLS"]),
        ((False, False, True, False), ["is a member of a superuser or BYPASSRLS role"]),
        ((False, False, False, True), [OWNS]),
        (
            (True, True, True, True),
            [
                "is a superuser",
                "has BYPASSRLS",
                "is a member of a superuser or BYPASSRLS role",
                OWNS,
            ],
        ),
    ],
)
def test_role_problems(flags: tuple[bool, bool, bool, bool], expected: list[str]) -> None:
    superuser, bypassrls, member, owns = flags
    problems = role_problems(
        superuser=superuser, bypassrls=bypassrls, member_of_privileged=member, owns_relations=owns
    )
    assert problems == expected


@pytest.mark.parametrize("unset", [None, ""], ids=["never set", "left by a transaction"])
def test_a_connection_without_a_default_business_passes(unset: str | None) -> None:
    require_no_default_business(unset)


@pytest.mark.parametrize("default", ["sunrise-clinic", " ", "canary-default-1f2e3d"])
def test_a_connection_with_a_default_business_is_refused_without_the_value(default: str) -> None:
    with pytest.raises(UnsafeDatabaseRole) as error:
        require_no_default_business(default)

    assert str(error.value) == DEFAULT_BUSINESS_REFUSED
    assert error.value.__cause__ is None and error.value.__context__ is None


def test_the_default_business_refusal_names_the_setting_the_variable_and_the_adr() -> None:
    for name in ("app.business_id", "VECTAPILOT_DATABASE_URL", "ADR 0009"):
        assert name in DEFAULT_BUSINESS_REFUSED


def test_the_migration_creates_the_role_the_api_expects() -> None:
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION_0002)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.APP_ROLE == APP_ROLE


@pytest.mark.anyio
async def test_a_bad_business_id_is_refused_before_any_connection(settings: Settings) -> None:
    # The URL is unreachable: had either helper connected first, the error would be a connection
    # error, not InvalidBusinessId.
    engine = create_async_engine(str(settings.database_url.get_secret_value()))
    try:
        with pytest.raises(InvalidBusinessId):
            async with business_transaction(engine, "Not A Business"):
                pass
        with pytest.raises(InvalidBusinessId):
            async with Database(engine).business_transaction("Not A Business"):
                pass
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_no_business_query_runs_before_the_role_check_passed(settings: Settings) -> None:
    engine = create_async_engine(str(settings.database_url.get_secret_value()))
    database = Database(engine)
    try:
        with pytest.raises((OSError, SQLAlchemyError)):
            async with database.business_transaction("sunrise-clinic"):
                pytest.fail("the transaction must not open while the role is unchecked")
    finally:
        await engine.dispose()

    assert database.role_verified is False


def test_startup_defers_the_role_check_when_the_database_is_down(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):
        assert app.state.database.role_verified is False
        assert app.state.database.engine is app.state.db_engine


def test_startup_defers_the_role_check_when_the_database_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hangs(self: Database) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(Database, "verify_role", hangs)
    app = create_app(make_settings(readiness_timeout_seconds=0.1))

    with TestClient(app):  # starts within the timeout instead of hanging
        assert app.state.database.role_verified is False


def test_an_unsafe_role_stops_startup_and_closes_the_engine(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    async def unsafe(self: Database) -> None:
        raise UnsafeDatabaseRole("the API's database role is a superuser")

    disposed: list[AsyncEngine] = []
    original_dispose = AsyncEngine.dispose

    async def spy_dispose(self: AsyncEngine, *args: Any, **kwargs: Any) -> None:
        disposed.append(self)
        await original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(Database, "verify_role", unsafe)
    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)
    app = create_app(settings)

    with pytest.raises(UnsafeDatabaseRole), TestClient(app):
        pytest.fail("startup must not complete")

    assert len(disposed) == 1
    assert not hasattr(app.state, "database")


# ── The pool-return reset (M3 PR 1, ADR 0009 amendment) ─────────────────────────────────────
# Its effect on real connections (the RESET, a failed RESET, the pool's own rollback after it) is
# proved against Postgres in tests/canary/test_leakage.py; here, where it is installed and when it
# stays out of the way.

UNUSED_RECORD: Final = cast(ConnectionPoolEntry, None)
"""The hook never reads the connection record."""


class Untouchable:
    """A stand-in DBAPI connection that fails the test if the hook uses it."""

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"the hook touched the connection ({name})")


@pytest.mark.parametrize(
    "state",
    [
        PoolResetState(transaction_was_reset=False, terminate_only=True, asyncio_safe=False),
        PoolResetState(transaction_was_reset=False, terminate_only=True, asyncio_safe=True),
    ],
    ids=["garbage-collected", "detached"],
)
def test_the_pool_reset_leaves_a_connection_that_is_being_terminated_alone(
    state: PoolResetState,
) -> None:
    # Not pooled again (SQLAlchemy closes or drops it), and with no event loop for the GC case.
    connection = cast(DBAPIConnection, Untouchable())

    reset_business_on_return(connection, UNUSED_RECORD, state)


def test_the_pool_reset_refuses_a_connection_without_an_asyncio_driver() -> None:
    # Raising is the fail-closed path: SQLAlchemy then invalidates the connection.
    state = PoolResetState(transaction_was_reset=True, terminate_only=False, asyncio_safe=True)

    with pytest.raises(TypeError, match="asyncio driver connection"):
        reset_business_on_return(cast(DBAPIConnection, object()), UNUSED_RECORD, state)


@pytest.mark.anyio
async def test_the_api_engine_resets_the_business_on_every_return() -> None:
    engine = create_api_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")  # never connects
    try:
        assert event.contains(engine.sync_engine, "reset", reset_business_on_return)
    finally:
        await engine.dispose()


def test_create_app_uses_the_api_engine(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app):  # the lifespan builds the engine (the database is down: never used)
        assert event.contains(app.state.db_engine.sync_engine, "reset", reset_business_on_return)
