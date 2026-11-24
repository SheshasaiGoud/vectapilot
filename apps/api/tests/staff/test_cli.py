"""M8 PR 1: `python -m vectapilot_api.staff add|issue-link|disable`, without a database (S5,
D4 (a), rule 5). The owner's transaction is replaced by a recorder; `test_cli_db.py` runs the real
one against Postgres."""

import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy.exc import DBAPIError, OperationalError

from tests.auth.fakes import STAFF_ID
from vectapilot_api.auth.store import StaffRole
from vectapilot_api.auth.tokens import parse_token
from vectapilot_api.staff.cli import (
    LOGIN_PATH,
    Add,
    Command,
    Disable,
    IssueLink,
    Outcome,
    StaffCliSettings,
    login_link,
    main,
    owner_url,
)
from vectapilot_api.staff.owner import IssuedLink

OWNER: Final = "postgresql+asyncpg://owner:not-a-real-secret@127.0.0.1:1/db"
ORIGIN: Final = "https://api.vectapilot.example"
BUSINESS: Final = "paws-and-co"
LINK_TOKEN: Final = f"{BUSINESS}.{'Q' * 21}{'z' * 21}-"
"""A synthetic link token in the exact form (43 base64url characters)."""
EXPIRES: Final = datetime(2026, 11, 24, 10, 15, tzinfo=UTC)
MARKER: Final = "zqxmarker"


class Recorder:
    def __init__(self, outcome: Outcome = None, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, Command]] = []
        self.outcome = outcome
        self.error = error

    def __call__(self, url: str, command: Command) -> Outcome:
        self.calls.append((url, command))
        if self.error is not None:
            raise self.error
        return self.outcome


def run(
    *argv: str,
    environment: str | None = "local",
    public_origin: str | None = ORIGIN,
    url: str | None = OWNER,
    recorder: Recorder | None = None,
    **fields: Any,
) -> tuple[int, str, str, Recorder]:
    recording = recorder or Recorder()
    values: dict[str, Any] = {"public_origin": public_origin, **fields}
    if environment is not None:
        values["environment"] = environment
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        settings=lambda: StaffCliSettings.model_validate(values),
        url=lambda: url,
        run=recording,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue(), recording


ADD: Final = ("add", "--business", BUSINESS, "--name", "Synthetic Reviewer", "--role", "reviewer")
ISSUE: Final = ("issue-link", "--business", BUSINESS, "--staff", str(STAFF_ID))
DISABLE: Final = ("disable", "--business", BUSINESS, "--staff", str(STAFF_ID))


# ── add ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_add_creates_a_member_and_prints_its_id(environment: str) -> None:
    code, out, err, recorder = run(*ADD, environment=environment, recorder=Recorder(STAFF_ID))

    assert (code, err) == (0, "")
    assert out == (
        f"staff add: {BUSINESS}: {STAFF_ID} (reviewer); display names are synthetic only (rule 5)\n"
    )
    assert recorder.calls == [(OWNER, Add(BUSINESS, "Synthetic Reviewer", StaffRole.REVIEWER))]


@pytest.mark.parametrize("name", ["", "  padded  ", f"two\nlines {MARKER}", "x" * 81])
def test_add_refuses_a_display_name_out_of_form(name: str) -> None:
    code, out, err, recorder = run("add", "--business", BUSINESS, "--name", name, "--role", "admin")

    assert (code, out, recorder.calls) == (1, "", [])
    assert err == "staff add refused: --name is one trimmed line of 1-80 characters\n"


def test_add_refuses_a_role_outside_the_two() -> None:
    with pytest.raises(SystemExit) as exited:
        run("add", "--business", BUSINESS, "--name", "X", "--role", "owner")

    assert exited.value.code == 2  # argparse's invalid arguments


# ── issue-link ───────────────────────────────────────────────────────────────────────────────


def test_issue_link_prints_the_token_only_in_the_fragment() -> None:
    recorder = Recorder(IssuedLink(LINK_TOKEN, EXPIRES))

    code, out, err, _ = run(*ISSUE, recorder=recorder, staff_link_lifetime_seconds=600)

    assert (code, err) == (0, "")
    link, notice = out.splitlines()
    assert link == f"{ORIGIN}{LOGIN_PATH}#b={BUSINESS}&t={LINK_TOKEN}"
    parts = urlsplit(link)
    # The fragment never reaches a server log or a Referer (D4 (a), S3): no query at all.
    assert (parts.scheme + "://" + parts.netloc, parts.path, parts.query) == (
        ORIGIN,
        "/staff/login",
        "",
    )
    assert parse_qs(parts.fragment) == {"b": [BUSINESS], "t": [LINK_TOKEN]}
    assert LINK_TOKEN not in link.partition("#")[0]
    assert notice == (
        "single use, valid until 2026-11-24T10:15:00+00:00 (database clock); a bearer credential:"
        " open it, never paste it into a chat or a ticket"
    )
    assert recorder.calls == [(OWNER, IssueLink(BUSINESS, STAFF_ID, 600, ORIGIN))]


def test_the_links_token_is_a_session_tokens_form_for_its_business() -> None:
    assert parse_token(LINK_TOKEN).business_id == BUSINESS
    assert login_link("http://localhost:8000", BUSINESS, LINK_TOKEN).startswith(
        "http://localhost:8000/staff/login#"
    )


def test_issue_link_needs_the_public_origin() -> None:
    code, out, err, recorder = run(*ISSUE, public_origin=None)

    assert (code, out, recorder.calls) == (1, "", [])
    assert err == (
        "staff issue-link refused: VECTAPILOT_PUBLIC_ORIGIN is not set: the link names the API's"
        " origin\n"
    )


def test_issue_link_for_no_enabled_member_is_refused_after_the_database_answered() -> None:
    code, out, err, recorder = run(*ISSUE, recorder=Recorder(None))

    assert (code, out) == (1, "")
    assert (
        err == "staff issue-link refused: no enabled staff member with that id in this business\n"
    )
    assert len(recorder.calls) == 1


def test_an_issued_links_repr_never_shows_its_token() -> None:
    assert LINK_TOKEN not in repr(IssuedLink(LINK_TOKEN, EXPIRES))


# ── disable ──────────────────────────────────────────────────────────────────────────────────


def test_disable_prints_what_it_disabled() -> None:
    code, out, err, recorder = run(*DISABLE, recorder=Recorder(True))

    assert (code, err) == (0, "")
    assert out == f"staff disable: {BUSINESS}: {STAFF_ID} disabled\n"
    assert recorder.calls == [(OWNER, Disable(BUSINESS, STAFF_ID))]


def test_disable_of_no_enabled_member_is_refused() -> None:
    code, _, err, _ = run(*DISABLE, recorder=Recorder(False))

    assert code == 1
    assert "no enabled staff member with that id in this business" in err


# ── Refusals before any connection ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [ADD, ISSUE, DISABLE], ids=["add", "issue-link", "disable"])
def test_production_is_refused_before_anything_else(argv: tuple[str, ...]) -> None:
    code, out, err, recorder = run(*argv, environment="production")

    assert (code, out, recorder.calls) == (1, "", [])
    assert err.endswith(
        "refused: staff commands run only in local, test and ci (production login is M10's)\n"
    )


def test_a_missing_environment_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No default (as the sandbox reset): this command holds the owner's credential.
    monkeypatch.chdir(tmp_path)  # no `.env` here
    monkeypatch.delenv("VECTAPILOT_ENVIRONMENT", raising=False)

    code, _, err, recorder = run(*ADD, environment=None)

    assert (code, recorder.calls) == (1, [])
    assert err == "staff add refused: set VECTAPILOT_ENVIRONMENT to local, test or ci\n"


@pytest.mark.parametrize(
    "fields",
    [
        {"public_origin": f"https://{MARKER}.example/path"},
        {"staff_link_lifetime_seconds": 3_601},
        {"staff_link_lifetime_seconds": 0},
    ],
    ids=["an origin with a path", "a lifetime over an hour", "no lifetime"],
)
def test_invalid_settings_are_refused_without_their_values(fields: dict[str, Any]) -> None:
    code, _, err, recorder = run(*ISSUE, **fields)

    assert (code, recorder.calls) == (1, [])
    assert err == (
        "staff issue-link refused: VECTAPILOT_PUBLIC_ORIGIN or"
        " VECTAPILOT_STAFF_LINK_LIFETIME_SECONDS is not valid\n"
    )
    assert MARKER not in err


@pytest.mark.parametrize("business", ["Paws", "../etc", "x" * 41, MARKER.upper()])
def test_a_bad_business_id_is_refused(business: str) -> None:
    code, _, err, recorder = run("disable", "--business", business, "--staff", str(STAFF_ID))

    assert (code, recorder.calls) == (1, [])
    assert err == "staff disable refused: --business is not a valid business id\n"


@pytest.mark.parametrize("argv", [ISSUE, DISABLE], ids=["issue-link", "disable"])
def test_a_staff_id_that_is_not_a_uuid_is_refused(argv: tuple[str, ...]) -> None:
    code, _, err, recorder = run(*argv[:-1], MARKER)

    assert (code, recorder.calls) == (1, [])
    assert err.endswith("refused: --staff is not a staff id (a uuid)\n")
    assert MARKER not in err


def test_without_the_owners_url_nothing_runs() -> None:
    code, _, err, recorder = run(*ADD, url=None)

    assert (code, recorder.calls) == (1, [])
    assert err == (
        "staff add refused: VECTAPILOT_MIGRATION_DATABASE_URL (the owner's URL) is not set\n"
    )


@pytest.mark.parametrize(
    "error",
    [
        DBAPIError("INSERT ... zqxmarker", {}, type("Orig", (Exception,), {"sqlstate": "23503"})()),
        OperationalError(f"SELECT {MARKER}", {}, OSError(MARKER)),
        OSError(MARKER),
    ],
    ids=["a foreign key", "a connection", "the network"],
)
def test_a_database_error_prints_its_type_and_sqlstate_only(error: BaseException) -> None:
    code, out, err, _ = run(*ADD, recorder=Recorder(error=error))

    assert (code, out) == (1, "")
    assert err.startswith("staff add failed, nothing changed: ")
    assert MARKER not in err
    if isinstance(error, DBAPIError) and not isinstance(error, OperationalError):
        assert err.endswith("DBAPIError (SQLSTATE 23503)\n")


# ── The process environment ──────────────────────────────────────────────────────────────────


def test_the_owner_url_comes_from_the_migration_setting_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no `.env` here
    monkeypatch.delenv("VECTAPILOT_MIGRATION_DATABASE_URL", raising=False)
    assert owner_url() is None

    monkeypatch.setenv("VECTAPILOT_MIGRATION_DATABASE_URL", OWNER)
    assert owner_url() == OWNER


def test_the_real_settings_read_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "ci")
    monkeypatch.setenv("VECTAPILOT_PUBLIC_ORIGIN", "http://localhost:8000")
    monkeypatch.delenv("VECTAPILOT_STAFF_LINK_LIFETIME_SECONDS", raising=False)

    settings = StaffCliSettings()

    assert (settings.environment, settings.public_origin) == ("ci", "http://localhost:8000")
    assert settings.staff_link_lifetime_seconds == 900  # P1: 15 minutes


def test_the_module_runs_as_a_command() -> None:
    done = subprocess.run(
        [sys.executable, "-m", "vectapilot_api.staff", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "add" in done.stdout and "issue-link" in done.stdout and "disable" in done.stdout
