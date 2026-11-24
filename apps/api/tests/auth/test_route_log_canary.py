"""S3's log canary on the session routes (M8 PR 1): a token, its secret, its SHA-256, the staff
cookie and the `Authorization` header never reach a log line. Unit: the real app on
`ScriptedDatabase` and the fake limiter.

Every case sends a credential (a bearer token, the staff cookie, a login link in the body, or all
of them at once) through each of PR 1's six routes, on its success path, its 401 and its typed 503
(whose warning is the one record these routes write of their own, with the error's class name
only). The tokens the routes mint (the new session's, the new staff cookie's) are read from the
responses and searched for too.

What is searched is what is written: every record formatted by the app's own formatter, the
server's access line and error line included (`tests/guards/test_log_canary.py`'s `run`), and
also each raw record's message and attributes, so a value that a formatter happened to drop
still fails the test.
"""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from tests.auth.fakes import (
    OTHER,
    OTHER_TOKEN,
    SECRET,
    STAFF_ID,
    TOKEN,
    ScriptedConnection,
    ScriptedDatabase,
    customer_row,
    staff_row,
)
from tests.guards.test_log_canary import Exchange, Logged, run
from tests.staff.test_routes import set_cookie
from tests.widget.helpers import ORIGIN, widget_settings
from tests.widget.served import inserted
from vectapilot_api.auth.cookies import STAFF_COOKIE
from vectapilot_api.auth.dependencies import LOOKUP_UNAVAILABLE
from vectapilot_api.auth.tokens import token_sha256
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.main import create_app

LINK: Final = OTHER_TOKEN
"""A login link's token in these cases (the other synthetic token, so both are searched)."""
EVERY_CREDENTIAL: Final = {
    "authorization": f"Bearer {TOKEN}",
    "cookie": f"{STAFF_COOKIE}={TOKEN}; theme=dark",
    "origin": ORIGIN,
}
"""Sent on every case: a route reads only its own credential, and none may log the other."""


USED_LINK: Final = SimpleNamespace(
    business_id=OTHER, staff_id=STAFF_ID, token_sha256=token_sha256(LINK), enabled=True
)
"""What `USE_LOGIN_LINK ... RETURNING` gives for `LINK`."""


def down() -> OperationalError:
    """A database failure whose own text holds both tokens: it must never be logged."""
    return OperationalError(f"SELECT {TOKEN} {LINK}", {}, OSError(f"refused {TOKEN}"))


@dataclass(frozen=True)
class Case:
    method: str
    path: str
    script: tuple[list[Any] | BaseException, ...]
    status: int
    body: dict[str, Any] | None = None


CASES: Final[dict[str, Case]] = {
    "config": Case("GET", "/v1/widget/paws-and-co/config", (), 200),
    "new session": Case(
        "POST",
        "/v1/widget/sessions",
        ([inserted()],),
        201,
        {"business_id": "paws-and-co", "ui_language": "en"},
    ),
    "new session, store down": Case(
        "POST",
        "/v1/widget/sessions",
        (down(),),
        503,
        {"business_id": "paws-and-co", "ui_language": "en"},
    ),
    "end session": Case("DELETE", "/v1/widget/session", ([customer_row()], [object()]), 204),
    "end session, unknown": Case("DELETE", "/v1/widget/session", ([],), 401),
    "end session, store down": Case("DELETE", "/v1/widget/session", (down(),), 503),
    "login": Case("POST", "/v1/staff/login", ([USED_LINK], [inserted()]), 204, {"token": LINK}),
    "login, used link": Case("POST", "/v1/staff/login", ([],), 401, {"token": LINK}),
    "login, store down": Case("POST", "/v1/staff/login", (down(),), 503, {"token": LINK}),
    "logout": Case("POST", "/v1/staff/logout", ([staff_row()], [object()]), 204),
    "logout, unknown": Case("POST", "/v1/staff/logout", ([],), 401),
    "logout, store down": Case("POST", "/v1/staff/logout", (down(),), 503),
    "me": Case("GET", "/v1/staff/me", ([staff_row()],), 200),
    "me, unknown": Case("GET", "/v1/staff/me", ([],), 401),
    "me, store down": Case("GET", "/v1/staff/me", (down(),), 503),
}


def exchange(case: Case) -> Exchange:
    app = create_app(widget_settings(), limiter=FakeLimiter())

    def started(app: FastAPI) -> None:
        app.state.database = ScriptedDatabase(ScriptedConnection(list(case.script)))

    def send(client: TestClient) -> httpx2.Response:
        return client.request(case.method, case.path, json=case.body, headers=EVERY_CREDENTIAL)

    return Exchange(app, send, case.status, started)


def minted(response: httpx2.Response) -> list[str]:
    """The tokens a response hands out: a new session's (its JSON) or a new staff cookie's."""
    if response.status_code == 201:
        return [response.json()["token"]]
    if response.headers.get("set-cookie", "").startswith(f"{STAFF_COOKIE}="):
        return [set_cookie(response)[1]]
    return []


def forbidden(logged: Logged) -> list[str]:
    tokens = [TOKEN, LINK, *minted(logged.response)]
    return [
        *tokens,
        *(token.rpartition(".")[2] for token in tokens),  # each secret alone
        *(token_sha256(token) for token in tokens),
        SECRET,
        EVERY_CREDENTIAL["authorization"],
        EVERY_CREDENTIAL["cookie"],
    ]


@pytest.mark.parametrize("name", CASES)
def test_no_credential_reaches_a_log_line(name: str) -> None:
    case = CASES[name]

    logged = run(exchange(case))

    assert logged.response.status_code == case.status, logged.response.text
    assert logged.access, "no access line: the capture is broken"
    written = [*logged.lines, *logged.server_errors]
    raw = [f"{record.getMessage()} {vars(record)}" for record in logged.records]
    for secret in forbidden(logged):
        assert [line for line in written if secret in line] == [], name
        assert [text for text in raw if secret in text] == [], name


@pytest.mark.parametrize("name", [name for name in CASES if name.endswith("store down")])
def test_a_typed_503_logs_its_warning_with_the_class_name_only(name: str) -> None:
    # The positive control: these cases do write a record of their own, and it holds no value.
    logged = run(exchange(CASES[name]))

    [warning] = [r for r in logged.records if r.getMessage() == LOOKUP_UNAVAILABLE]
    assert warning.reason == "OperationalError"  # type: ignore[attr-defined]
    assert warning.exc_info is None


def test_the_minted_tokens_are_searched_for() -> None:
    # The positive control for `minted`: both routes that hand a token out are read.
    session = run(exchange(CASES["new session"]))
    login = run(exchange(CASES["login"]))

    assert len(minted(session.response)) == 1 and len(minted(login.response)) == 1
    assert minted(session.response)[0] != minted(login.response)[0]
