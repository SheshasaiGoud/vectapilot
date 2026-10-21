"""db.py without a database: the business-id check, the role rules, and the startup gate.

The same code against real Postgres (RLS, pooled connections, unsafe roles) is in test_rls.py.
"""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.conftest import make_settings
from vectapilot_api.config import Settings
from vectapilot_api.db import (
    APP_ROLE,
    Database,
    InvalidBusinessId,
    UnsafeDatabaseRole,
    business_transaction,
    parse_business_id,
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
