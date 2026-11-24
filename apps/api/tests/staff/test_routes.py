"""Staff login, logout and who-am-I without a database or Redis (M8 PR 1; S1, S3, S5, S9, S14,
D4 (a)): `POST /v1/staff/login`, `POST /v1/staff/logout` and `GET /v1/staff/me` on the real app
with `ScriptedDatabase` and `FakeLimiter` (which no staff route may call: staff login has no
window, step B3). Against Postgres, the link race and the cookie's round trip included:
`tests/canary/test_session_routes.py`."""

from types import SimpleNamespace
from typing import Final, Literal

import httpx2
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.auth.fakes import (
    BUSINESS,
    OTHER_TOKEN,
    SESSION_ID,
    STAFF_ID,
    TOKEN,
    ScriptedDatabase,
    staff_row,
)
from tests.widget.helpers import ORIGIN, widget_settings
from tests.widget.served import NO_STORE, SAME_ORIGIN, Served, inserted, same_bytes, serve
from vectapilot_api.auth.cookies import DEV_STAFF_COOKIE, STAFF_COOKIE
from vectapilot_api.auth.dependencies import (
    CROSS_SITE_REFUSED,
    JSON_ONLY,
    UNAUTHORIZED,
)
from vectapilot_api.auth.http import UNKNOWN_KEY
from vectapilot_api.auth.store import INSERT_SESSION, REVOKE_SESSION, USE_LOGIN_LINK
from vectapilot_api.auth.tokens import parse_token, token_sha256
from vectapilot_api.guards.fake_limiter import FakeLimiter

LOGIN: Final = "/v1/staff/login"
LOGOUT: Final = "/v1/staff/logout"
ME: Final = "/v1/staff/me"
LINK: Final = TOKEN
"""A login link's token (the same synthetic form as a session token)."""
MARKER: Final = "zqxmarker"
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
STAFF_SESSION_ID: Final = SESSION_ID


def used_link(*, enabled: bool | None = True) -> SimpleNamespace:
    """The row `USE_LOGIN_LINK ... RETURNING` gives for `LINK` (`enabled`: the member subquery)."""
    return SimpleNamespace(
        business_id=BUSINESS, staff_id=STAFF_ID, token_sha256=token_sha256(LINK), enabled=enabled
    )


def login(served: Served, body: object = None, **headers: str) -> httpx2.Response:
    return served.client.post(
        LOGIN, json={"token": LINK} if body is None else body, headers={**SAME_ORIGIN, **headers}
    )


def set_cookie(response: httpx2.Response) -> tuple[str, str, set[str]]:
    """The one `Set-Cookie` header's name, value and attributes (lower-cased)."""
    [header] = response.headers.get_list("set-cookie")
    pair, *attributes = header.split("; ")
    name, _, value = pair.partition("=")
    return name, value, {attribute.lower() for attribute in attributes}


def staff_cookie(token: str = TOKEN, name: str = STAFF_COOKIE) -> dict[str, str]:
    return {"cookie": f"{name}={token}"}


# ── POST /v1/staff/login ─────────────────────────────────────────────────────────────────────


def test_a_link_opens_a_new_staff_session_in_one_transaction() -> None:
    with serve() as served:
        database = served.script([used_link()], [inserted(STAFF_SESSION_ID)])
        response = login(served)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers["cache-control"] == NO_STORE
    name, token, attributes = set_cookie(response)
    assert name == STAFF_COOKIE
    assert attributes == {"httponly", "max-age=28800", "path=/", "samesite=strict", "secure"}
    # A fresh token (no fixation), never the link's: the link is used up, the session is new.
    assert token != LINK and parse_token(token).business_id == BUSINESS
    assert database.opened == [BUSINESS]  # one transaction: the use and the insert together
    (use, use_params), (insert, insert_params) = database.connection.executed
    assert (use, use_params) == (
        USE_LOGIN_LINK.text,
        {"business_id": BUSINESS, "token_sha256": token_sha256(LINK)},
    )
    assert insert == INSERT_SESSION.text
    assert insert_params == {
        "business_id": BUSINESS,
        "kind": "staff",
        "staff_id": STAFF_ID,
        "token_sha256": token_sha256(token),
        "channel": "web",
        "ui_language": None,
        "lifetime_s": 28_800,  # P1: 8 hours
    }
    assert token not in str(insert_params) and LINK not in str(insert_params)
    assert served.limiter.calls == []  # no login window (step B3): Redis is never asked


def test_a_cookie_the_browser_already_holds_is_ignored_and_replaced() -> None:
    with serve() as served:
        database = served.script([used_link()], [inserted()], [used_link()], [inserted()])
        first = login(served, cookie=f"{STAFF_COOKIE}={OTHER_TOKEN}")
        second = login(served)

    first_token, second_token = set_cookie(first)[1], set_cookie(second)[1]
    assert len({first_token, second_token, OTHER_TOKEN, LINK}) == 4  # a new session every login
    assert [statement for statement, _ in database.connection.executed].count(
        INSERT_SESSION.text
    ) == 2


def test_under_the_insecure_dev_flag_the_cookie_has_no_secure_and_no_host_prefix() -> None:
    with serve(widget_settings(cookie_insecure_dev=True)) as served:
        served.script([used_link()], [inserted()])
        response = login(served)

    name, _, attributes = set_cookie(response)
    assert name == DEV_STAFF_COOKIE
    assert attributes == {"httponly", "max-age=28800", "path=/", "samesite=strict"}


def test_a_link_that_is_unknown_used_or_expired_is_the_one_401() -> None:
    with serve() as served:
        database = served.script([])
        response = login(served)
        served.use(ScriptedDatabase())
        me = served.client.get(ME)

    assert response.json() == UNAUTHORIZED_BODY
    assert same_bytes(response, me)  # the staff 401, byte for byte
    assert "set-cookie" not in response.headers
    assert [statement for statement, _ in database.connection.executed] == [USE_LOGIN_LINK.text]


@pytest.mark.parametrize("enabled", [False, None], ids=["disabled", "member not found"])
def test_a_disabled_members_link_is_used_up_and_is_the_one_401(enabled: bool | None) -> None:
    # Zara's review: the link use reads the member in the same statement, so a disabled member's
    # link is used up (the one transaction commits) and no session is stored for it.
    with serve() as served:
        database = served.script([used_link(enabled=enabled)])
        response = login(served)
        served.script([])  # the same link again: used, so the statement returns no row
        again = login(served)

    assert response.json() == UNAUTHORIZED_BODY
    assert same_bytes(response, again)  # byte for byte the 401 of a used or unknown link
    assert "set-cookie" not in response.headers
    assert [statement for statement, _ in database.connection.executed] == [USE_LOGIN_LINK.text]


@pytest.mark.parametrize(
    "token",
    [MARKER, f"{BUSINESS}.{MARKER}", f"Paws.{'A' * 43}", LINK + "x", ""],
    ids=["not a token", "a short secret", "a bad business", "too long", "empty"],
)
def test_a_malformed_link_is_refused_before_any_lookup(token: str) -> None:
    with serve() as served:
        response = login(served, {"token": token})
        database = served.app.state.database

    assert response.status_code in (401, 422)
    if token:
        assert response.json() == UNAUTHORIZED_BODY
    assert MARKER not in response.text
    assert served.limiter.calls == [] and database.opened == []


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": ""}, id="an empty Origin"),
        pytest.param({"origin": "https://evil.example"}, id="another site"),
        pytest.param({"origin": "https://www.sunrise-clinic.example"}, id="a widget host page"),
        pytest.param({"origin": ORIGIN + "/"}, id="the origin with a slash"),
        pytest.param({"origin": ORIGIN.upper()}, id="the origin in upper case"),
        pytest.param({"origin": "null"}, id="an opaque origin"),
        pytest.param({"sec-fetch-site": "cross-site"}, id="a cross-site fetch"),
        pytest.param({"sec-fetch-site": "same-site"}, id="a same-site fetch"),
        pytest.param({"sec-fetch-site": "none"}, id="a typed URL"),
    ],
)
def test_login_csrf_another_origin_is_a_403_before_any_work(headers: dict[str, str]) -> None:
    with serve() as served:
        response = login(served, **headers)
        database = served.app.state.database

    assert response.status_code == 403
    assert response.json() == {"detail": CROSS_SITE_REFUSED, "error": "cross_site_refused"}
    assert response.headers["cache-control"] == NO_STORE
    assert "set-cookie" not in response.headers
    assert served.limiter.calls == [] and database.opened == []


def test_login_csrf_an_absent_origin_is_a_403() -> None:
    with serve() as served:
        response = served.client.post(LOGIN, json={"token": LINK})

    assert response.status_code == 403
    assert response.json()["error"] == "cross_site_refused"
    assert served.limiter.calls == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"token": "paws-and-co.x"}', "text/plain"),
        (b"token=paws-and-co.x", "application/x-www-form-urlencoded"),
        (b"--b\r\n\r\n--b--\r\n", "multipart/form-data; boundary=b"),
    ],
    ids=["text/plain", "a form", "multipart"],
)
def test_login_csrf_a_simple_request_body_is_a_415(content: bytes, content_type: str) -> None:
    # The three bodies a cross-site HTML form can send without a CORS preflight.
    with serve() as served:
        response = served.client.post(
            LOGIN, content=content, headers={**SAME_ORIGIN, "content-type": content_type}
        )

    assert response.status_code == 415
    assert response.json() == {"detail": JSON_ONLY, "error": "json_only"}
    assert served.limiter.calls == []


@pytest.mark.parametrize(
    "extra", [{"business_id": BUSINESS}, {"staff_id": str(STAFF_ID)}, {MARKER: MARKER}]
)
def test_a_login_body_with_any_other_field_is_a_422_that_echoes_nothing(
    extra: dict[str, str],
) -> None:
    with serve() as served:
        response = login(served, {"token": LINK, **extra})

    assert response.status_code == 422
    assert response.headers["cache-control"] == NO_STORE
    assert response.json()["problems"] == [
        {"loc": ["body", UNKNOWN_KEY], "type": "extra_forbidden"}
    ]
    for value in (*extra, *extra.values(), LINK):
        assert value not in response.text
    assert served.limiter.calls == []


def test_the_business_header_on_login_is_a_400() -> None:
    with serve() as served:
        response = login(served, **{"X-Business-Id": BUSINESS})

    assert response.status_code == 400
    assert response.json()["error"] == "business_header_refused"
    assert served.limiter.calls == []


@pytest.mark.parametrize("limit", ["refuse", "raise"], ids=["a full window", "Redis down"])
def test_a_login_never_asks_redis(limit: Literal["refuse", "raise"]) -> None:
    # Step B3 (zara's review): no login window. One keyed by the token's business prefix, which
    # the caller writes, could only lock staff out; the link's 256 bits and single use are the
    # control. So neither a full window nor Redis being down changes a login.
    with serve(limiter=FakeLimiter(limit=limit)) as served:
        served.script([used_link()], [inserted()])
        response = login(served)

    assert response.status_code == 204, response.text
    assert served.limiter.calls == []


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([OperationalError("UPDATE", {}, OSError())], id="the link use fails"),
        pytest.param(
            [[used_link()], IntegrityError("INSERT", {}, Exception("never shown"))],
            id="the insert fails after the use",
        ),
    ],
)
def test_a_login_that_cannot_be_stored_is_the_typed_503_and_sets_no_cookie(
    script: list[object],
) -> None:
    # In Postgres the use and the insert share one transaction, so a failed insert leaves the link
    # unused (rolled back): the canary suite checks it.
    with serve() as served:
        served.script(*script)  # type: ignore[arg-type]
        response = login(served)

    assert response.status_code == 503
    assert response.json()["error"] == "session_store_unavailable"
    assert "set-cookie" not in response.headers and "never shown" not in response.text


def test_a_hung_login_is_cut_off_at_the_lookup_timeout() -> None:
    with serve() as served:
        served.use(ScriptedDatabase(hang=True))
        response = login(served)

    assert response.status_code == 503


# ── POST /v1/staff/logout ────────────────────────────────────────────────────────────────────


def test_logout_revokes_this_session_and_clears_the_cookie() -> None:
    with serve() as served:
        database = served.script([staff_row()], [object()])
        response = served.client.post(LOGOUT, headers={**SAME_ORIGIN, **staff_cookie()})

    assert response.status_code == 204, response.text
    assert response.headers["cache-control"] == NO_STORE
    name, value, attributes = set_cookie(response)
    assert (name, value) == (STAFF_COOKIE, '""')
    assert {"max-age=0", "path=/", "secure", "httponly", "samesite=strict"} <= attributes
    statement, params = database.connection.executed[1]
    assert (statement, params) == (
        REVOKE_SESSION.text,
        {"business_id": BUSINESS, "session_id": STAFF_SESSION_ID},
    )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no Origin"),
        pytest.param({"origin": "https://evil.example"}, id="another site"),
        pytest.param({**SAME_ORIGIN, "sec-fetch-site": "cross-site"}, id="a cross-site fetch"),
    ],
)
def test_logout_csrf_another_origin_is_a_403_before_the_lookup(headers: dict[str, str]) -> None:
    # Bodiless: the Origin check is the control here (S5), with SameSite=Strict on the cookie.
    with serve() as served:
        response = served.client.post(LOGOUT, headers={**headers, **staff_cookie()})
        database = served.app.state.database

    assert response.status_code == 403
    assert response.json()["error"] == "cross_site_refused"
    assert "set-cookie" not in response.headers
    assert database.opened == []


def test_logout_without_a_session_is_the_one_401() -> None:
    with serve() as served:
        response = served.client.post(LOGOUT, headers=SAME_ORIGIN)

    assert response.json() == UNAUTHORIZED_BODY
    assert "www-authenticate" not in response.headers  # a cookie, not a bearer scheme


def test_a_logout_that_cannot_revoke_is_the_typed_503() -> None:
    with serve() as served:
        served.script([staff_row()], OperationalError("UPDATE", {}, OSError()))
        response = served.client.post(LOGOUT, headers={**SAME_ORIGIN, **staff_cookie()})

    assert response.status_code == 503
    assert "set-cookie" not in response.headers


def test_an_admin_may_log_out_too() -> None:
    with serve() as served:
        served.script([staff_row(role="admin")], [object()])
        response = served.client.post(LOGOUT, headers={**SAME_ORIGIN, **staff_cookie()})

    assert response.status_code == 204


# ── GET /v1/staff/me ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["reviewer", "admin"])
def test_me_names_the_sessions_business_member_and_role(role: str) -> None:
    with serve() as served:
        database = served.script([staff_row(role=role)])
        response = served.client.get(ME, headers=staff_cookie())

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == NO_STORE
    assert response.json() == {
        "business_id": BUSINESS,
        "staff_id": str(STAFF_ID),
        "role": role,
        "display_name": "Synthetic Reviewer",
    }
    assert database.opened == [BUSINESS]  # the token's business, read under its RLS


def test_me_asks_for_a_staff_session_of_an_enabled_member_only() -> None:
    # S1: a customer token as the cookie is not found (the lookup filters its kind), and a
    # disabled member is refused on every request.
    with serve() as served:
        database = served.script([])
        response = served.client.get(ME, headers=staff_cookie())

    assert response.json() == UNAUTHORIZED_BODY
    [(statement, _)] = database.connection.executed
    assert "s.kind = 'staff'" in statement and "u.disabled_at IS NULL" in statement


def test_a_staff_token_as_a_bearer_token_opens_no_staff_route() -> None:
    with serve() as served:
        response = served.client.get(ME, headers={"authorization": f"Bearer {TOKEN}"})
        database = served.app.state.database

    assert response.json() == UNAUTHORIZED_BODY
    assert database.opened == []


def test_the_business_header_next_to_a_staff_session_is_a_400() -> None:
    with serve() as served:
        response = served.client.get(ME, headers={**staff_cookie(), "X-Business-Id": BUSINESS})

    assert response.status_code == 400
    assert response.json()["error"] == "business_header_refused"


def test_a_lookup_that_cannot_run_is_the_typed_503() -> None:
    with serve() as served:
        served.script(OperationalError("SELECT", {}, OSError()))
        response = served.client.get(ME, headers=staff_cookie())

    assert response.status_code == 503
    assert response.headers["cache-control"] == NO_STORE
