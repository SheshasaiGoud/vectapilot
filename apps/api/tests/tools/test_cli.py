"""M5 PR 1: `python -m vectapilot_api.tools sandbox reset`, without a database.

The reset itself is replaced by a recorder; `test_sandbox_reset.py` runs the real one.
"""

import io
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy.exc import DBAPIError

from tests.tools.helpers import REPO_ROOT, SANDBOX_DIR, copy_sandbox
from vectapilot_api.tools.cli import ToolsCliSettings, find_sandbox_dir, main
from vectapilot_api.tools.config import SEED_FILE
from vectapilot_api.tools.sandbox import ResetCounts, SandboxRefused, SandboxSeed

OWNER: Final = "postgresql+asyncpg://owner:not-a-real-secret@127.0.0.1:1/db"


class Recorder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, SandboxSeed]] = []
        self.error = error

    def __call__(self, url: str, seed: SandboxSeed) -> ResetCounts:
        self.calls.append((url, seed))
        if self.error is not None:
            raise self.error
        return ResetCounts(len(seed.slots), len(seed.bookings), len(seed.orders))


def run(
    *args: str,
    environment: str = "local",
    config_dir: Path | None = None,
    url: str | None = OWNER,
    reset: Recorder | None = None,
    cwd: Path = SANDBOX_DIR,
) -> tuple[int, str, str, Recorder]:
    recorder = reset or Recorder()
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["sandbox", "reset", *args],
        settings=lambda: ToolsCliSettings.model_validate(
            {"environment": environment, "tools_config_dir": config_dir}
        ),
        url=lambda: url,
        reset=recorder,
        cwd=lambda: cwd,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue(), recorder


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_a_reset_seeds_the_named_business(environment: str) -> None:
    code, out, err, recorder = run("--business", "paws-and-co", environment=environment)

    assert (code, err) == (0, "")
    assert out == (
        "sandbox reset: paws-and-co: 12 slots, 3 bookings, 4 orders (synthetic seed);"
        " tool_audit untouched\n"
    )
    [(url, seed)] = recorder.calls
    assert url == OWNER and seed.business_id == "paws-and-co"


def test_production_is_refused_before_anything_else() -> None:
    code, out, err, recorder = run("--business", "paws-and-co", environment="production")

    assert (code, out, recorder.calls) == (1, "", [])
    assert err == "sandbox reset refused: the sandbox reset runs only in local, test and ci\n"


def run_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str | None
) -> tuple[int, str, list[str], Recorder]:
    """`main` with the real settings factory, in a folder without a `.env`: the guard reads the
    process environment itself (no injected settings)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("VECTAPILOT_TOOLS_CONFIG_DIR", raising=False)
    if environment is None:
        monkeypatch.delenv("VECTAPILOT_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", environment)
    asked: list[str] = []
    recorder = Recorder()
    err = io.StringIO()

    def url() -> str:
        asked.append("url")
        return OWNER

    code = main(
        ["sandbox", "reset", "--business", "paws-and-co", "--config-dir", str(SANDBOX_DIR)],
        url=url,
        reset=recorder,
        out=io.StringIO(),
        err=err,
    )
    return code, err.getvalue(), asked, recorder


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ("production", "the sandbox reset runs only in local, test and ci"),
        (None, "set VECTAPILOT_ENVIRONMENT to local, test or ci"),  # no default: fail closed
        ("staging", "set VECTAPILOT_ENVIRONMENT to local, test or ci"),
    ],
)
def test_the_environment_guard_reads_the_real_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str | None, message: str
) -> None:
    code, err, asked, recorder = run_from_the_environment(monkeypatch, tmp_path, environment)

    assert (code, asked, recorder.calls) == (1, [], [])
    assert err == f"sandbox reset refused: {message}\n"


def test_the_real_environment_lets_local_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The positive control for the guard above: the same path runs once the variable says local.
    code, err, asked, recorder = run_from_the_environment(monkeypatch, tmp_path, "local")

    assert (code, err, asked, len(recorder.calls)) == (0, "", ["url"], 1)
    assert ToolsCliSettings().environment == "local"


def test_an_invalid_config_dir_setting_is_refused() -> None:
    err = io.StringIO()
    code = main(
        ["sandbox", "reset", "--business", "paws-and-co"],
        settings=lambda: ToolsCliSettings.model_validate(
            {"environment": "local", "tools_config_dir": 5}
        ),
        url=lambda: OWNER,
        reset=Recorder(),
        err=err,
    )

    assert (code, err.getvalue()) == (
        1,
        "sandbox reset refused: VECTAPILOT_TOOLS_CONFIG_DIR is not valid\n",
    )


@pytest.mark.parametrize(
    ("args", "options", "message"),
    [
        (["--business", "Not A Business"], {}, "--business is not a valid business id"),
        (
            ["--business", "northwind-shop"],
            {},
            "northwind-shop has no sandbox in the tool configuration folder",
        ),
        (
            ["--business", "paws-and-co"],
            {"url": None},
            "VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set",
        ),
    ],
)
def test_refusals_name_the_rule(args: list[str], options: dict[str, Any], message: str) -> None:
    code, out, err, recorder = run(*args, **options)

    assert (code, out, recorder.calls) == (1, "", [])
    assert err == f"sandbox reset refused: {message}\n"


def test_an_invalid_folder_is_refused_whole(tmp_path: Path) -> None:
    folder = copy_sandbox(tmp_path / "sandbox")
    (folder / "sunrise-clinic" / SEED_FILE).unlink()  # another business's file

    code, _, err, recorder = run("--business", "paws-and-co", "--config-dir", str(folder))

    assert (code, recorder.calls) == (1, [])
    assert err == "sandbox reset refused: sunrise-clinic: missing seed.json\n"


def test_the_folder_comes_from_the_flag_then_the_setting_then_the_repository(
    tmp_path: Path,
) -> None:
    renamed = copy_sandbox(tmp_path / "a", {"paws-and-co": "paws-copy"})
    other = copy_sandbox(tmp_path / "b", {"paws-and-co": "paws-other"})

    assert run("--business", "paws-copy", "--config-dir", str(renamed), config_dir=other)[0] == 0
    assert run("--business", "paws-other", config_dir=other)[0] == 0
    assert run("--business", "paws-and-co", cwd=tmp_path)[0] == 1  # no data/sandbox above
    assert find_sandbox_dir(SANDBOX_DIR / "paws-and-co") == SANDBOX_DIR
    assert find_sandbox_dir(REPO_ROOT) == SANDBOX_DIR  # the folder itself, not only its parents
    assert find_sandbox_dir(tmp_path) is None


def test_a_database_error_prints_its_type_and_sqlstate_never_its_text() -> None:
    class Orig(Exception):
        sqlstate = "23505"

    error = DBAPIError("INSERT ...", {"label": "secret-row-value"}, Orig("Key (id)=(S-17)"))
    code, out, err, _ = run("--business", "paws-and-co", reset=Recorder(error))

    assert (code, out) == (1, "")
    assert err == "sandbox reset failed, nothing changed: DBAPIError (SQLSTATE 23505)\n"


def test_a_connection_error_is_reported_without_a_traceback() -> None:
    # The database is down: asyncpg raises an OSError, not an SQLAlchemyError.
    code, out, err, _ = run("--business", "paws-and-co", reset=Recorder(ConnectionRefusedError()))

    assert (code, out) == (1, "")
    assert err == "sandbox reset failed, nothing changed: ConnectionRefusedError\n"


def test_a_refusal_from_the_reset_itself_is_reported() -> None:
    code, _, err, _ = run("--business", "paws-and-co", reset=Recorder(SandboxRefused("fixed")))

    assert (code, err) == (1, "sandbox reset refused: fixed\n")


def test_arguments_are_required() -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["sandbox", "reset"], err=io.StringIO())
    assert exit_.value.code == 2
