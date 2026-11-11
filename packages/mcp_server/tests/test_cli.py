"""The command: logs on stderr only, and every refusal is one stderr line and exit status 2.

stdout belongs to the MCP protocol, so no test here may see anything on it.
"""

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy.exc import OperationalError

from tests.fakes import CORPUS_DIR, REPO_ROOT, SANDBOX_DIR, fake_executor, stub_database
from tests.settings import UNREACHABLE_DB, make_settings
from vectapilot_api.db import UnsafeDatabaseRole
from vectapilot_mcp import cli
from vectapilot_mcp.cli import REFUSED_EXIT, SDK_LOGGER, configure_logging, main, serve
from vectapilot_mcp.startup import NO_BUSINESS_TOOLS, REFUSED_ENVIRONMENT, Prepared

FLAGS: Final = ["--business-id", "paws-and-co", "--customer-ref", "cust-001"]
SECRET: Final = "not-a-real-secret"  # noqa: S105 - a test value, in no real DSN


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root, sdk = logging.getLogger(), logging.getLogger(SDK_LOGGER)
    handlers, level, sdk_level = root.handlers[:], root.level, sdk.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    sdk.setLevel(sdk_level)


def one_refusal(err: str) -> None:
    """Exactly one stderr line, the command's refusal."""
    assert err.startswith("vectapilot-mcp: ")
    assert err.endswith("\n")
    assert err.count("\n") == 1


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A complete, valid environment whose database is unreachable."""
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "test")
    monkeypatch.setenv("VECTAPILOT_DATABASE_URL", UNREACHABLE_DB)
    monkeypatch.setenv("VECTAPILOT_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("VECTAPILOT_TOOLS_CONFIG_DIR", str(SANDBOX_DIR))
    monkeypatch.setenv("VECTAPILOT_CORPUS_DIR", str(CORPUS_DIR))
    return monkeypatch


def test_logging_goes_to_stderr_never_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("vectapilot_api.tools").info("tool_call", extra={"tool_call": {"x": 1}})
    out, err = capsys.readouterr()
    assert out == ""
    assert '"message": "tool_call"' in err
    root = logging.getLogger()
    assert [type(h) for h in root.handlers] == [logging.StreamHandler]
    assert all(getattr(h, "stream", None) is sys.stderr for h in root.handlers)


def test_the_log_level_setting_takes_effect(capsys: pytest.CaptureFixture[str]) -> None:
    """The root level is the setting's (WARNING is Python's default), so an unpinned logger's
    INFO record reaches stderr."""
    logging.getLogger().setLevel(logging.WARNING)
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("vectapilot_mcp.test").info("unpinned_record")
    out, err = capsys.readouterr()
    assert out == ""
    assert '"message": "unpinned_record"' in err


@pytest.mark.parametrize(
    ("level", "expected"),
    [("DEBUG", logging.INFO), ("INFO", logging.INFO), ("WARNING", logging.WARNING)],
)
def test_the_sdk_logger_is_never_below_info(level: str, expected: int) -> None:
    """Rule 5: at DEBUG the SDK logs a malformed stdin line, the client's raw text, with `%r`."""
    configure_logging(level)
    sdk = logging.getLogger(SDK_LOGGER)
    assert sdk.level == expected
    assert logging.getLogger("mcp.shared.jsonrpc_dispatcher").getEffectiveLevel() == expected


CANARY_CHILD: Final = """
import logging, sys
import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from vectapilot_mcp.cli import SDK_LOGGER, configure_logging

async def main() -> None:
    server = Server("canary")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

configure_logging("DEBUG")
if sys.argv[1] == "unpinned":
    logging.getLogger(SDK_LOGGER).setLevel(logging.NOTSET)
anyio.run(main)
"""
"""A trivial stdio server (no database) with the command's logging at DEBUG; `unpinned` undoes
the SDK pin, the red proof that the test can see a leak."""

PII_CANARY: Final = "pii-canary-7f3a"
MALFORMED: Final = (
    '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"create_ticket"}} ' + PII_CANARY
)
"""The SDK keeps a malformed line's head and tail in its DEBUG record, so the canary sits at the
end, where an attacker would put it."""


@pytest.mark.parametrize(("mode", "leaks"), [("pinned", False), ("unpinned", True)])
def test_a_malformed_line_stays_out_of_the_log_at_debug(mode: str, leaks: bool) -> None:
    done = subprocess.run(  # noqa: S603 - our own interpreter and fixed code
        [sys.executable, "-I", "-c", CANARY_CHILD, mode],
        input=MALFORMED + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert PII_CANARY not in done.stdout
    assert (PII_CANARY in done.stderr) is leaks


def test_an_unset_environment_is_refused(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed: without VECTAPILOT_ENVIRONMENT, Settings' default `local` is not enough."""
    environment.delenv("VECTAPILOT_ENVIRONMENT")
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"vectapilot-mcp: {REFUSED_ENVIRONMENT}\n"


def test_a_business_without_tools_is_refused(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--business-id", "acme-shop", "--customer-ref", "cust-001"]) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err == f"vectapilot-mcp: {NO_BUSINESS_TOOLS}\n"


def test_an_unreachable_database_is_refused_naming_its_type_only(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("vectapilot-mcp: the database is unavailable (")
    assert SECRET not in err
    assert "127.0.0.1" not in err


def test_missing_settings_are_refused_without_values(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    environment.delenv("VECTAPILOT_DATABASE_URL")
    environment.setenv("VECTAPILOT_REDIS_URL", f"redis://:{SECRET}@127.0.0.1:1/0x")
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("vectapilot-mcp: invalid settings: ")
    assert "database_url" in err
    assert SECRET not in err


@pytest.mark.parametrize(
    "argv",
    [["--business-id", "paws-and-co"], ["--customer-ref", "cust-001"], []],
    ids=["no-customer", "no-business", "neither"],
)
def test_both_flags_are_required(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(argv)
    assert exited.value.code == 2
    assert capsys.readouterr().out == ""


def test_a_driver_error_is_refused_naming_its_type_only(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong password or a missing role arrives as a SQLAlchemy error whose message names the
    role and the host: the refusal names its type only."""

    async def failing(*_args: Any) -> Prepared:
        raise OperationalError(
            "SELECT 1",
            {},
            Exception('password authentication failed for user "vectapilot_app" host=10.0.0.9'),
        )

    environment.setattr(cli, "prepare", failing)
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "vectapilot-mcp: the database is unavailable (OperationalError)\n"
    assert "vectapilot_app" not in err
    assert "10.0.0.9" not in err


def test_an_invalid_tool_folder_is_one_refusal_line(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / "paws-and-co").mkdir()
    (tmp_path / "paws-and-co" / "tool.json").write_text("{}", encoding="utf-8")
    environment.setenv("VECTAPILOT_TOOLS_CONFIG_DIR", str(tmp_path))
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    one_refusal(err)


def test_an_unsafe_role_is_one_refusal_line(
    environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The owner DSN's refusal (tests/integration runs the real one) through the command."""

    async def unsafe(*_args: Any) -> Prepared:
        raise UnsafeDatabaseRole("the database role is not vectapilot_app")

    environment.setattr(cli, "prepare", unsafe)
    assert main(FLAGS) == REFUSED_EXIT
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "vectapilot-mcp: the database role is not vectapilot_app\n"


@pytest.mark.parametrize(
    "raised",
    [OSError("broken pipe"), OperationalError("x", {}, Exception("y")), RuntimeError("boom")],
    ids=["os", "sqlalchemy", "runtime"],
)
def test_an_error_after_startup_is_not_a_refusal_and_disposes_the_engine(
    raised: Exception, environment: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit status 2 means "never started": an error while serving keeps its own type (and its
    traceback, on stderr), and the engine is closed on the way out."""
    stub, database = stub_database()

    async def prepared(*_args: Any) -> Prepared:
        return Prepared(fake_executor(), "cust-001", database)

    def failing_build(*_args: Any) -> Any:
        raise raised

    environment.setattr(cli, "prepare", prepared)
    environment.setattr(cli, "build_server", failing_build)
    with pytest.raises(type(raised)):
        main(FLAGS)
    assert stub.engine.disposed == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "vectapilot-mcp: " not in err


def test_serve_disposes_the_engine_when_serving_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    stub, database = stub_database()

    async def prepared(*_args: Any) -> Prepared:
        return Prepared(fake_executor(), "cust-001", database)

    def failing_build(*_args: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "prepare", prepared)
    monkeypatch.setattr(cli, "build_server", failing_build)
    with pytest.raises(RuntimeError):
        asyncio.run(serve(make_settings(), "paws-and-co", "cust-001"))
    assert stub.engine.disposed == 1


def test_the_module_runs_as_a_process_and_keeps_stdout_clean() -> None:
    """`python -m vectapilot_mcp` as a host would start it: a refusal leaves stdout empty."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VECTAPILOT_") and key != "RUN_INTEGRATION"
    }
    env |= {
        "VECTAPILOT_ENVIRONMENT": "test",
        "VECTAPILOT_DATABASE_URL": UNREACHABLE_DB,
        "VECTAPILOT_REDIS_URL": "redis://127.0.0.1:1/0",
        "VECTAPILOT_TOOLS_CONFIG_DIR": str(SANDBOX_DIR),
        "VECTAPILOT_CORPUS_DIR": str(CORPUS_DIR),
    }
    done = subprocess.run(  # noqa: S603 - our own interpreter and module, fixed arguments
        [sys.executable, "-m", "vectapilot_mcp", *FLAGS],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT / "packages" / "mcp_server",
        timeout=60,
        check=False,
    )
    assert done.returncode == REFUSED_EXIT
    assert done.stdout == ""
    assert done.stderr.startswith("vectapilot-mcp: the database is unavailable (")
