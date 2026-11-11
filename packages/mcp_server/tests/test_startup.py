"""Startup's checks without a database (tests/integration has the role and registry checks).

Each refusal is fixed text: it names the flag, setting or rule, never the value given.
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from tests.fakes import CORPUS_DIR, SANDBOX_DIR, stub_database
from tests.settings import UNREACHABLE_DB, make_settings
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, UnsafeDatabaseRole
from vectapilot_api.tools.config import ToolsConfigError
from vectapilot_mcp import startup
from vectapilot_mcp.startup import (
    BAD_BUSINESS,
    BAD_CUSTOMER,
    DATABASE_DOWN,
    NO_BUSINESS_TOOLS,
    NO_TOOLS_DIR,
    NOT_REGISTERED,
    REFUSED_ENVIRONMENT,
    StartupRefused,
    business_config,
    check_database,
    prepare,
    require_sandbox_environment,
    validate_flags,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("environment", ["production", "staging"])
async def test_refused_outside_local_test_and_ci(environment: str, tmp_path: Path) -> None:
    """An allowlist: `production` and any environment added later are refused, before the flags,
    any file or the database (the folder does not exist and the database is unreachable)."""
    settings = make_settings().model_copy(
        update={"environment": environment, "tools_config_dir": tmp_path / "missing"}
    )
    with pytest.raises(StartupRefused) as refused:
        await prepare(settings, "Not A Business", "Not A Customer")
    assert str(refused.value) == REFUSED_ENVIRONMENT


def test_the_default_environment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: Settings defaults the environment to `local`, which is in the allowlist, so a
    host config that forgets VECTAPILOT_ENVIRONMENT must still be refused."""
    monkeypatch.delenv("VECTAPILOT_ENVIRONMENT", raising=False)
    settings = Settings(
        _env_file=None,
        database_url=UNREACHABLE_DB,
        redis_url="redis://127.0.0.1:1/0",
        llm_provider="fake",
        embeddings_provider="fake",
        tools_config_dir=SANDBOX_DIR,
        corpus_dir=CORPUS_DIR,
    )
    assert settings.environment == "local"  # the default: in the allowlist, but not chosen
    with pytest.raises(StartupRefused) as refused:
        require_sandbox_environment(settings)
    assert str(refused.value) == REFUSED_ENVIRONMENT


def test_an_explicit_local_environment_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive control for the default's refusal: the same value, set on purpose."""
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "local")
    settings = Settings(
        _env_file=None,
        database_url=UNREACHABLE_DB,
        redis_url="redis://127.0.0.1:1/0",
        llm_provider="fake",
        embeddings_provider="fake",
    )
    require_sandbox_environment(settings)


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_the_sandbox_environments_pass_the_first_check(environment: str) -> None:
    require_sandbox_environment(make_settings(environment=environment))


@pytest.mark.parametrize("business_id", ["Paws", "a", "paws_and_co", "../etc", "x" * 41, ""])
def test_a_bad_business_flag_is_refused_without_its_value(business_id: str) -> None:
    with pytest.raises(StartupRefused) as refused:
        validate_flags(business_id, "cust-001")
    assert str(refused.value) == BAD_BUSINESS


@pytest.mark.parametrize("customer_ref", ["Cust-001", "c1", "cust 001", "cust-001\n", "x" * 41])
def test_a_bad_customer_flag_is_refused_without_its_value(customer_ref: str) -> None:
    with pytest.raises(StartupRefused) as refused:
        validate_flags("paws-and-co", customer_ref)
    assert str(refused.value) == BAD_CUSTOMER


def test_good_flags_pass() -> None:
    flags = validate_flags("paws-and-co", "cust-001")
    assert (flags.business_id, flags.customer_ref) == ("paws-and-co", "cust-001")


def test_the_tool_configuration_folder_is_required() -> None:
    with pytest.raises(StartupRefused) as refused:
        business_config(make_settings(tools_config_dir=None, corpus_dir=None), "paws-and-co")
    assert str(refused.value) == NO_TOOLS_DIR


def test_a_business_without_a_tool_folder_is_refused() -> None:
    with pytest.raises(StartupRefused) as refused:
        business_config(make_settings(), "acme-shop")
    assert str(refused.value) == NO_BUSINESS_TOOLS


def test_the_business_config_is_its_tools_json() -> None:
    config = business_config(make_settings(), "sunrise-clinic")
    assert config.business_id == "sunrise-clinic"


def test_an_invalid_tool_folder_stops_startup(tmp_path: Path) -> None:
    """The whole folder is validated, as the API does: one broken business stops the server
    even when it serves another."""
    (tmp_path / "paws-and-co").mkdir()
    (tmp_path / "paws-and-co" / "tool.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ToolsConfigError):
        business_config(make_settings(tools_config_dir=tmp_path), "sunrise-clinic")
    assert (SANDBOX_DIR / "sunrise-clinic" / "tools.json").is_file()


async def test_an_unreachable_database_stops_startup() -> None:
    """Strict, unlike the API's deferred role check: no database, no server."""
    with pytest.raises((OSError, SQLAlchemyError)):
        await prepare(make_settings(), "paws-and-co", "cust-001")


@pytest.mark.parametrize("hang", ["role", "registry"])
async def test_a_database_that_never_answers_is_refused_in_time(
    hang: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound covers both queries: a hung database stops startup instead of holding the host."""
    monkeypatch.setattr(startup, "STARTUP_TIMEOUT_S", 0.01)
    _, database = stub_database(hang=hang)
    with pytest.raises(StartupRefused) as refused:
        await asyncio.wait_for(check_database(database, "paws-and-co"), timeout=5)
    assert str(refused.value) == DATABASE_DOWN


async def test_the_role_check_runs_before_the_registry() -> None:
    """A unit twin of tests/integration's owner-DSN test: `UnsafeDatabaseRole` propagates."""
    _, database = stub_database(role_error=UnsafeDatabaseRole("the role is not vectapilot_app"))
    with pytest.raises(UnsafeDatabaseRole):
        await check_database(database, "paws-and-co")


async def test_an_unregistered_business_is_refused() -> None:
    """A unit twin of tests/integration's registry test."""
    _, database = stub_database(registered=False)
    with pytest.raises(StartupRefused) as refused:
        await check_database(database, "paws-and-co")
    assert str(refused.value) == NOT_REGISTERED


async def test_a_registered_business_passes() -> None:
    _, database = stub_database()
    await check_database(database, "paws-and-co")


@pytest.mark.parametrize("raised", [StartupRefused(NOT_REGISTERED), asyncio.CancelledError()])
async def test_a_refusal_after_the_engine_exists_disposes_it(
    raised: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`except BaseException`: a refusal and a cancellation both close the pool."""
    stub, database = stub_database()

    async def refuse(_database: Database, _business_id: str) -> None:
        raise raised

    monkeypatch.setattr(startup, "open_database", lambda _settings: database)
    monkeypatch.setattr(startup, "check_database", refuse)
    with pytest.raises(type(raised)):
        await prepare(make_settings(), "paws-and-co", "cust-001")
    assert stub.engine.disposed == 1


async def test_a_successful_prepare_leaves_the_engine_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub, database = stub_database()
    monkeypatch.setattr(startup, "open_database", lambda _settings: database)
    prepared = await prepare(make_settings(), "paws-and-co", "cust-001")
    assert prepared.database is database
    assert prepared.customer_ref == "cust-001"
    assert stub.engine.disposed == 0
