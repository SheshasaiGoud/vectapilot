"""S3's log canary on the session routes (M8 PR 1): a token, its secret, its SHA-256, the staff
cookie and the `Authorization` header never reach a log line. Unit: the real app on
`ScriptedDatabase` and the fake limiter.

Every case sends a credential (a bearer token, the staff cookie, a login link in the body, or all
of them at once) through each of PR 1's six routes, PR 2a's three widget routes and PR 2b's two
(widget confirm, feedback), on its success path, its 401 and its typed 503 (whose warning is the
one record these routes write of their own, with the error's class name only). PR 2a's message
route and "talk to a person" also write `widget_store_failed` when the hand-off store fails after
the session was found (class name only), and so does PR 2b's feedback when its rating cannot be
stored: the database's error text holds the tokens there too. Widget confirm's body holds a
one-time confirm token (`CONFIRM_TOKEN`, ADR 0016 W2), searched for in every case. The tokens the
routes mint (the new session's, the new staff cookie's) are read from the responses and searched
for too.

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

from tests.actions.test_confirm_route import EXECUTED, StandIn
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
from tests.widget.guarded import HANDOFF_ID, stored_row
from tests.widget.helpers import ORIGIN, widget_settings
from tests.widget.served import inserted
from vectapilot_api.auth.cookies import STAFF_COOKIE
from vectapilot_api.auth.dependencies import LOOKUP_UNAVAILABLE
from vectapilot_api.auth.tokens import token_sha256
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.main import create_app
from vectapilot_api.widget.messages import STORE_FAILED

MESSAGES: Final = "/v1/widget/messages"
CONFIRM: Final = f"/v1/widget/actions/{HANDOFF_ID}/confirm"
FEEDBACK: Final = "/v1/widget/feedback"
CONFIRM_TOKEN: Final = "Q7zx9TOKENtokenTOKEN_-"  # noqa: S105 (a test token, not a secret)
CONFIRM_BODY: Final = {"confirm_token": CONFIRM_TOKEN}
RATING: Final = {"trace_id": "7ace" * 8, "rating": "up"}
TURN_ROW: Final = SimpleNamespace(id=HANDOFF_ID)
HANDOFFS: Final = "/v1/widget/handoffs"
STATUS: Final = f"{HANDOFFS}/{HANDOFF_ID}"
HANDOFF_STATE_ROW: Final = SimpleNamespace(
    id=HANDOFF_ID,
    status="open",
    reply=None,
    replied_at=None,
    priority="normal",
    reason="customer_asked",
)
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
    confirm: object | None = None
    """PR 2b: the confirm service both confirm routes share (`StandIn`), when the case needs one."""


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
    # PR 2a: the widget's message and hand-off routes (the guard on the registered seams; the
    # hand-off store's statements scripted after the session lookup).
    "message": Case(
        "POST", MESSAGES, ([customer_row()], [stored_row()], []), 200, {"message": "Hello."}
    ),
    "message, unknown": Case("POST", MESSAGES, ([],), 401, {"message": "Hello."}),
    "message, store down": Case("POST", MESSAGES, (down(),), 503, {"message": "Hello."}),
    "message, hand-off store fails": Case(
        "POST", MESSAGES, ([customer_row()], down()), 200, {"message": "Hello."}
    ),
    "talk to a person": Case("POST", HANDOFFS, ([customer_row()], [stored_row("normal")]), 200),
    "talk to a person, unknown": Case("POST", HANDOFFS, ([],), 401),
    "talk to a person, store down": Case("POST", HANDOFFS, (down(),), 503),
    "talk to a person, hand-off store fails": Case(
        "POST", HANDOFFS, ([customer_row()], down()), 200
    ),
    "hand-off status": Case("GET", STATUS, ([customer_row()], [HANDOFF_STATE_ROW]), 200),
    "hand-off status, unknown": Case("GET", STATUS, ([],), 401),
    "hand-off status, store down": Case("GET", STATUS, (down(),), 503),
    # PR 2b: widget confirm (the confirm route's stand-in service) and feedback.
    "widget confirm": Case(
        "POST", CONFIRM, ([customer_row()],), 200, CONFIRM_BODY, StandIn(EXECUTED)
    ),
    "widget confirm, unknown": Case("POST", CONFIRM, ([],), 401, CONFIRM_BODY, StandIn(EXECUTED)),
    "widget confirm, store down": Case(
        "POST", CONFIRM, (down(),), 503, CONFIRM_BODY, StandIn(EXECUTED)
    ),
    "feedback": Case("POST", FEEDBACK, ([customer_row()], [TURN_ROW], [TURN_ROW]), 201, RATING),
    "feedback, unknown": Case("POST", FEEDBACK, ([],), 401, RATING),
    "feedback, store down": Case("POST", FEEDBACK, (down(),), 503, RATING),
    "feedback, rating store fails": Case("POST", FEEDBACK, ([customer_row()], down()), 503, RATING),
}
STORE_FAILS: Final = ("message, hand-off store fails", "talk to a person, hand-off store fails")


def exchange(case: Case) -> Exchange:
    app = create_app(widget_settings(), limiter=FakeLimiter())

    def started(app: FastAPI) -> None:
        app.state.database = ScriptedDatabase(ScriptedConnection(list(case.script)))
        if case.confirm is not None:
            app.state.confirm_service = case.confirm

    def send(client: TestClient) -> httpx2.Response:
        return client.request(case.method, case.path, json=case.body, headers=EVERY_CREDENTIAL)

    return Exchange(app, send, case.status, started)


def minted(response: httpx2.Response) -> list[str]:
    """The tokens a response hands out: a new session's (its JSON) or a new staff cookie's."""
    if response.status_code == 201 and "token" in response.json():  # not feedback's 201
        return [response.json()["token"]]
    if response.headers.get("set-cookie", "").startswith(f"{STAFF_COOKIE}="):
        return [set_cookie(response)[1]]
    return []


def forbidden(logged: Logged) -> list[str]:
    tokens = [TOKEN, LINK, *minted(logged.response)]
    return [
        *tokens,
        CONFIRM_TOKEN,  # PR 2b: widget confirm's one-time token (W2)
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


@pytest.mark.parametrize("name", STORE_FAILS)
def test_a_failed_hand_off_store_logs_its_warning_with_the_class_name_only(name: str) -> None:
    # PR 2a's positive control: the store's failure (its text holding both tokens) was reached and
    # swallowed into `queued: false`, and its one warning holds no value.
    logged = run(exchange(CASES[name]))

    [warning] = [r for r in logged.records if r.getMessage() == STORE_FAILED]
    assert warning.reason == "OperationalError"  # type: ignore[attr-defined]
    assert warning.exc_info is None
    body = logged.response.json()
    assert body.get("handoff", body) == {"id": None, "queued": False, "follow_up": False}


def test_a_rating_that_cannot_be_stored_logs_its_warning_with_the_class_name_only() -> None:
    # PR 2b's positive control: feedback's store failure (its text holding both tokens) was
    # reached and answered with its typed 503, and its one warning holds no value.
    logged = run(exchange(CASES["feedback, rating store fails"]))

    [warning] = [r for r in logged.records if r.getMessage() == STORE_FAILED]
    assert warning.reason == "OperationalError"  # type: ignore[attr-defined]
    assert warning.exc_info is None
    assert logged.response.json()["error"] == "feedback_store_unavailable"


def test_widget_confirm_reached_its_service_with_the_token() -> None:
    # PR 2b's positive control: the success case did carry the confirm token to the service, so
    # the search above had something to find.
    case = CASES["widget confirm"]
    logged = run(exchange(case))

    assert logged.response.status_code == 200
    assert isinstance(case.confirm, StandIn)
    request = case.confirm.calls[-1][2]
    assert request.confirm_token.get_secret_value() == CONFIRM_TOKEN
