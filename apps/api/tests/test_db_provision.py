"""vectapilot-db-provision without a database: what it refuses, and that no password leaks.

The run against real Postgres is in test_rls.py.
"""

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from vectapilot_api import db_provision
from vectapilot_api.db_provision import ProvisionError, app_password, set_login_password

CANARY = "canary-pw-7f3a"
OWNER = f"postgresql+asyncpg://vectapilot:{CANARY}-owner@127.0.0.1:1/vectapilot"
APP = f"postgresql+asyncpg://vectapilot_app:{CANARY}@127.0.0.1:1/vectapilot"


def test_app_password_comes_from_the_apis_url() -> None:
    assert app_password(OWNER, APP) == CANARY


@pytest.mark.parametrize(
    ("app_url", "message"),
    [
        pytest.param(OWNER, "must log in as vectapilot_app", id="the owner's URL as the API's"),
        pytest.param(
            "postgresql+asyncpg://vectapilot_app@127.0.0.1:1/vectapilot",
            "must contain the app role's password",
            id="no password",
        ),
        pytest.param(
            f"postgresql+asyncpg://vectapilot_app:{CANARY}@127.0.0.1:1/other",
            "must point to the same database",
            id="another database",
        ),
        pytest.param(
            f"postgresql+asyncpg://vectapilot_app:{CANARY}@db.example:1/vectapilot",
            "must point to the same database",
            id="another host",
        ),
    ],
)
def test_app_password_refuses(app_url: str, message: str) -> None:
    with pytest.raises(ProvisionError, match=message) as error:
        app_password(OWNER, app_url)

    assert CANARY not in str(error.value)


@pytest.mark.anyio
async def test_a_failure_names_the_error_class_only() -> None:
    engine = create_async_engine(OWNER)
    try:
        with pytest.raises(ProvisionError) as error:
            await set_login_password(engine, "vectapilot_app", CANARY)
    finally:
        await engine.dispose()

    assert str(error.value).startswith("could not set the password of vectapilot_app (")
    assert CANARY not in str(error.value)
    assert error.value.__suppress_context__


@pytest.fixture
def no_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Run where no `.env` exists, with only the variables a test sets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VECTAPILOT_DATABASE_URL", raising=False)
    monkeypatch.setenv("VECTAPILOT_MIGRATION_DATABASE_URL", OWNER)
    return monkeypatch


def test_main_needs_the_apis_url(no_env_file: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="VECTAPILOT_DATABASE_URL"):
        db_provision.main()


def test_main_refuses_before_connecting(no_env_file: pytest.MonkeyPatch) -> None:
    no_env_file.setenv("VECTAPILOT_DATABASE_URL", OWNER)

    with pytest.raises(SystemExit, match="must log in as vectapilot_app") as error:
        db_provision.main()

    assert CANARY not in str(error.value)
