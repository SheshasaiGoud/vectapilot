"""M6 PR 3: `python -m vectapilot_api.extraction purge`, without a database.

The purge itself is replaced by a recorder; tests/extraction/test_store_db.py runs the real one.
As M5's sandbox reset (tests/tools/test_cli.py): the environment has no default and must say
local, test or ci; nothing connects before every refusal is checked; a database error prints its
type and SQLSTATE, never its text.
"""

import io
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy.exc import DBAPIError

from vectapilot_api.extraction.cli import ExtractionCliSettings, main
from vectapilot_api.extraction.purge import PURGE_REFUSED, PurgeRefused

OWNER: Final = "postgresql+asyncpg://owner:not-a-real-secret@127.0.0.1:1/db"
CANARY: Final = "zq6purge-canary"


class Recorder:
    def __init__(self, error: BaseException | None = None, purged: int = 2) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error = error
        self.purged = purged

    def __call__(self, url: str, business_id: str) -> int:
        self.calls.append((url, business_id))
        if self.error is not None:
            raise self.error
        return self.purged


def run(
    *args: str,
    environment: str = "local",
    url: str | None = OWNER,
    purge: Recorder | None = None,
) -> tuple[int, str, str, Recorder]:
    recorder = purge or Recorder()
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["purge", *args],
        settings=lambda: ExtractionCliSettings.model_validate({"environment": environment}),
        url=lambda: url,
        purge=recorder,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue(), recorder


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_a_purge_deletes_the_named_businesss_expired_extractions(environment: str) -> None:
    code, out, err, recorder = run("--business", "paws-and-co", environment=environment)

    assert (code, err) == (0, "")
    assert out == (
        "extraction purge: paws-and-co: 2 expired extraction(s) deleted, with their fields;"
        " unexpired extractions untouched\n"
    )
    assert recorder.calls == [(OWNER, "paws-and-co")]


def test_production_is_refused_before_anything_else() -> None:
    code, out, err, recorder = run("--business", "paws-and-co", environment="production")

    assert (code, out, recorder.calls) == (1, "", [])
    assert err == "extraction purge refused: the purge runs only in local, test and ci\n"


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ("production", "the purge runs only in local, test and ci"),
        (None, "set VECTAPILOT_ENVIRONMENT to local, test or ci"),  # no default: fail closed
        ("staging", "set VECTAPILOT_ENVIRONMENT to local, test or ci"),
    ],
)
def test_the_environment_guard_reads_the_real_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str | None, message: str
) -> None:
    monkeypatch.chdir(tmp_path)  # no `.env` here: the guard reads the process environment
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

    code = main(["purge", "--business", "paws-and-co"], url=url, purge=recorder, err=err)

    assert (code, asked, recorder.calls) == (1, [], [])
    assert err.getvalue() == f"extraction purge refused: {message}\n"


def test_the_real_environment_lets_local_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "local")
    recorder = Recorder()

    code = main(
        ["purge", "--business", "paws-and-co"],
        url=lambda: OWNER,
        purge=recorder,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert (code, len(recorder.calls)) == (0, 1)
    assert ExtractionCliSettings().environment == "local"


@pytest.mark.parametrize(
    ("args", "options", "message"),
    [
        (["--business", "Not A Business"], {}, "--business is not a valid business id"),
        (
            ["--business", "paws-and-co"],
            {"url": None},
            "VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set",
        ),
        (
            ["--business", "paws-and-co"],
            {"purge": Recorder(error=PurgeRefused(PURGE_REFUSED))},
            PURGE_REFUSED,
        ),
    ],
)
def test_refusals_name_the_rule(args: list[str], options: dict[str, Any], message: str) -> None:
    code, out, err, _ = run(*args, **options)

    assert (code, out) == (1, "")
    assert err == f"extraction purge refused: {message}\n"


def test_a_missing_business_is_an_argument_error() -> None:
    with pytest.raises(SystemExit) as caught:
        run()
    assert caught.value.code == 2


def test_a_database_error_prints_its_type_and_sqlstate_never_its_text() -> None:
    class Orig(Exception):
        sqlstate = "40P01"

    error = DBAPIError("DELETE ...", {"business_id": CANARY}, Orig(CANARY))
    code, out, err, _ = run("--business", "paws-and-co", purge=Recorder(error=error))

    assert (code, out) == (1, "")
    assert err == "extraction purge failed, nothing changed: DBAPIError (SQLSTATE 40P01)\n"
    assert CANARY not in err


def test_an_os_error_prints_its_type_only() -> None:
    code, _, err, _ = run("--business", "paws-and-co", purge=Recorder(error=OSError(CANARY)))

    assert (code, err) == (1, "extraction purge failed, nothing changed: OSError\n")
