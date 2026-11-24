"""The widget's session routes without a database or Redis (M8 PR 1; S1, S3, S5, S8, S9, S14,
S16, P1, P7): `GET /v1/widget/{business_id}/config`, `POST /v1/widget/sessions` and
`DELETE /v1/widget/session`, on the real app with `ScriptedDatabase` and `FakeLimiter`
(`tests/widget/served.py`). The same routes against Postgres: `tests/canary/test_session_routes.py`.
"""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import httpx2
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from tests.auth.fakes import (
    BUSINESS,
    OTHER,
    SESSION_ID,
    TOKEN,
    ScriptedConnection,
    ScriptedDatabase,
    customer_row,
)
from tests.conftest import make_settings
from tests.widget.helpers import (
    BUSINESSES,
    ORIGIN,
    copy_widget,
    edit_widget,
    guidance_of,
    widget_settings,
)
from tests.widget.served import (
    EXPIRES_AT,
    NO_STORE,
    SAME_ORIGIN,
    Served,
    inserted,
    same_bytes,
    serve,
)
from vectapilot_api.auth.dependencies import (
    BUSINESS_HEADER_REFUSED,
    CROSS_SITE_REFUSED,
    SESSION_STORE_UNAVAILABLE,
    UNAUTHORIZED,
)
from vectapilot_api.auth.http import UNKNOWN_KEY
from vectapilot_api.auth.store import INSERT_SESSION, REVOKE_SESSION
from vectapilot_api.auth.tokens import SECRET_LENGTH, parse_token, token_sha256
from vectapilot_api.config import WIDGET_CLIENT_MARGIN_SECONDS
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import RATE_LIMITED, RATE_LIMITED_DETAIL
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.models import SessionCreated
from vectapilot_api.widget.router import LANGUAGE_NOT_OFFERED, LIMITS_UNAVAILABLE, WIDGET_NOT_FOUND

SESSIONS: Final = "/v1/widget/sessions"
SESSION: Final = "/v1/widget/session"
MARKER: Final = "zqxmarker"
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
NOT_FOUND_BODY: Final = {"detail": WIDGET_NOT_FOUND, "error": "widget_not_found"}
BEARER: Final = {"authorization": f"Bearer {TOKEN}"}


def config_path(business_id: str) -> str:
    return f"/v1/widget/{business_id}/config"


def create(served: Served, body: dict[str, Any] | None = None, **headers: str) -> httpx2.Response:
    payload = body if body is not None else {"business_id": BUSINESS, "ui_language": "en"}
    return served.client.post(SESSIONS, json=payload, headers={**SAME_ORIGIN, **headers})


class SqlState(Exception):
    """A driver error with a SQLSTATE, as asyncpg's (`IntegrityError.orig`)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("driver error text that is never shown")
        self.sqlstate = sqlstate


def integrity(sqlstate: str) -> IntegrityError:
    return IntegrityError("INSERT INTO sessions ...", {}, SqlState(sqlstate))


# ── GET /v1/widget/{business_id}/config (files only, S14, S16) ───────────────────────────────


@pytest.mark.parametrize("business_id", BUSINESSES)
def test_the_config_route_sends_the_texts_and_the_guard_files_guidance(business_id: str) -> None:
    with serve() as served:
        response = served.client.get(config_path(business_id))

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == NO_STORE
    body = response.json()
    assert body["business_id"] == business_id
    assert body["guidance"] == guidance_of(business_id)  # S8: verbatim, from the guard file
    assert body["guidance_language"] == "en"  # D9: the guard files are English only
    assert body["languages"] == [texts["language"] for texts in body["texts"]]
    assert set(body) == {
        "business_id",
        "display_name",
        "languages",
        "texts",
        "guidance",
        "guidance_language",
        "accent_color",
    }
    for texts in body["texts"]:
        assert set(texts) == {
            "language",
            "greeting",
            "handoff_follow_up",
            "urgent_follow_up",
            "contact_fallback",
            "quick_replies",
        }


def test_the_config_route_answers_with_postgres_and_redis_down() -> None:
    # S14: the widget caches this for every failure state, so it reads files only.
    limiter = FakeLimiter(limit="raise", budget="raise")
    with serve(limiter=limiter) as served:
        database = served.use(ScriptedDatabase(fail=OperationalError("SELECT 1", {}, OSError())))
        response = served.client.get(config_path(BUSINESS))

    assert response.status_code == 200, response.text
    assert database.opened == [] and limiter.calls == []


def test_a_business_without_an_enabled_widget_is_one_404(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    edit_widget(folder, OTHER, lambda data: data.__setitem__("enabled", False))
    with serve(widget_settings(widget_config_dir=folder)) as served:
        responses = [
            served.client.get(config_path(business_id))
            for business_id in (OTHER, "rt-unknown", "Not-A-Business", MARKER)
        ]

    assert responses[0].json() == NOT_FOUND_BODY
    assert same_bytes(*responses) and responses[0].status_code == 404
    assert all(MARKER not in r.text for r in responses)


def test_without_a_widget_folder_every_business_is_the_404() -> None:
    with serve(make_settings(public_origin=ORIGIN)) as served:
        response = served.client.get(config_path(BUSINESS))

    assert (response.status_code, response.json()) == (404, NOT_FOUND_BODY)
    assert response.headers["cache-control"] == NO_STORE


def test_the_business_header_on_the_config_route_is_a_400() -> None:
    with serve() as served:
        response = served.client.get(config_path(BUSINESS), headers={"X-Business-Id": BUSINESS})

    assert response.status_code == 400
    assert response.json() == {
        "detail": BUSINESS_HEADER_REFUSED,
        "error": "business_header_refused",
    }
    assert response.headers["cache-control"] == NO_STORE


# ── POST /v1/widget/sessions ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("business_id", "language"), [(BUSINESS, "te"), (OTHER, "hi")])
def test_a_new_session_returns_its_token_once_and_stores_only_its_hash(
    business_id: str, language: str
) -> None:
    with serve() as served:
        database = served.script([inserted()])
        response = create(served, {"business_id": business_id, "ui_language": language})

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == NO_STORE
    body = response.json()
    token = body["token"]
    # S3: `<business_id>.<43 base64url characters>`, revealed in this JSON only (never masked).
    assert parse_token(token).business_id == business_id
    assert len(token.rpartition(".")[2]) == SECRET_LENGTH and "*" not in token
    assert body == {
        "token": token,
        "token_type": "bearer",
        "expires_at": EXPIRES_AT.isoformat().replace("+00:00", "Z"),
        "lifetime_seconds": 43_200,  # P1: 12 hours
        "ui_language": language,
        "message_timeout_seconds": 60.0 + WIDGET_CLIENT_MARGIN_SECONDS,  # P7, D15 (a): 65 s
        "confirm_timeout_seconds": 15.0,  # P7
    }
    # One statement, in the business's own transaction: the hash, never the token.
    assert database.opened == [business_id]
    [(statement, params)] = database.connection.executed
    assert statement == INSERT_SESSION.text
    assert params == {
        "business_id": business_id,
        "kind": "customer",
        "staff_id": None,
        "token_sha256": token_sha256(token),
        "channel": "web",
        "ui_language": language,
        "lifetime_s": 43_200,
    }
    assert token not in str(params)
    assert served.limiter.calls == [LimiterCall("admit", business_id, RouteSlug.SESSION)]


def test_every_session_gets_a_fresh_token() -> None:
    with serve() as served:
        served.script([inserted()], [inserted()])
        first, second = create(served), create(served)

    assert first.status_code == second.status_code == 201
    assert first.json()["token"] != second.json()["token"]


def test_the_timings_come_from_settings() -> None:
    settings = widget_settings(
        customer_session_lifetime_seconds=3_600,
        widget_turn_bound_seconds=80.0,
        widget_confirm_timeout_seconds=20.0,
    )
    with serve(settings) as served:
        database = served.script([inserted()])
        body = create(served).json()

    assert (body["lifetime_seconds"], body["message_timeout_seconds"]) == (3_600, 85.0)
    assert body["confirm_timeout_seconds"] == 20.0
    assert database.connection.executed[0][1]["lifetime_s"] == 3_600


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no Origin"),
        pytest.param({"origin": "https://evil.example"}, id="another site"),
        pytest.param({"origin": "https://www.paws-and-co.example"}, id="the host page's origin"),
        pytest.param({"origin": "http://testserver"}, id="the test client's"),
        pytest.param({"origin": "null"}, id="an opaque origin"),
        pytest.param({**SAME_ORIGIN, "sec-fetch-site": "cross-site"}, id="cross-site fetch"),
        pytest.param({**SAME_ORIGIN, "sec-fetch-site": "same-site"}, id="same-site fetch"),
    ],
)
def test_a_session_is_created_only_from_the_apis_own_origin(headers: dict[str, str]) -> None:
    with serve() as served:
        response = served.client.post(
            SESSIONS, json={"business_id": BUSINESS, "ui_language": "en"}, headers=headers
        )
        database = served.app.state.database

    assert response.status_code == 403
    assert response.json() == {"detail": CROSS_SITE_REFUSED, "error": "cross_site_refused"}
    assert response.headers["cache-control"] == NO_STORE
    assert served.limiter.calls == [] and database.opened == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b'{"business_id": "paws-and-co", "ui_language": "en"}', "text/plain"),
        (b"business_id=paws-and-co&ui_language=en", "application/x-www-form-urlencoded"),
        (b'{"business_id": "paws-and-co", "ui_language": "en"}', "multipart/form-data; b=x"),
    ],
    ids=["text/plain", "a form", "multipart"],
)
def test_a_body_that_is_not_json_is_refused_before_any_work(
    content: bytes, content_type: str
) -> None:
    with serve() as served:
        response = served.client.post(
            SESSIONS, content=content, headers={**SAME_ORIGIN, "content-type": content_type}
        )
        database = served.app.state.database

    assert response.status_code == 415, response.text
    assert response.headers["cache-control"] == NO_STORE
    assert served.limiter.calls == [] and database.opened == []


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_ref": f"cust-{MARKER}"},
        {"session_id": str(SESSION_ID)},
        {"token": TOKEN},
        {MARKER: MARKER},
    ],
    ids=["customer_ref", "session_id", "token", "anything"],
)
def test_a_body_with_any_other_field_is_a_422_that_echoes_nothing(extra: dict[str, str]) -> None:
    body = {"business_id": BUSINESS, "ui_language": "en", **extra}
    with serve() as served:
        response = create(served, body)
        database = served.app.state.database

    assert response.status_code == 422
    assert response.headers["cache-control"] == NO_STORE
    assert response.json()["problems"] == [
        {"loc": ["body", UNKNOWN_KEY], "type": "extra_forbidden"}
    ]
    for value in (*extra, *extra.values()):
        assert value not in response.text
    assert served.limiter.calls == [] and database.opened == []


@pytest.mark.parametrize("business_id", ["rt-unknown", "Not A Business", ""])
def test_a_business_without_a_widget_gets_the_404_and_spends_no_window(business_id: str) -> None:
    with serve() as served:
        response = create(served, {"business_id": business_id, "ui_language": "en"})

    if business_id == "rt-unknown":
        assert (response.status_code, response.json()) == (404, NOT_FOUND_BODY)
    else:  # not a business id at all: the body's own 422
        assert response.status_code == 422
    assert served.limiter.calls == []


def test_a_disabled_widget_gets_the_same_404(tmp_path: Path) -> None:
    folder = copy_widget(tmp_path / "widget")
    edit_widget(folder, BUSINESS, lambda data: data.__setitem__("enabled", False))
    with serve(widget_settings(widget_config_dir=folder)) as served:
        disabled = create(served)
        unknown = create(served, {"business_id": "rt-unknown", "ui_language": "en"})

    assert same_bytes(disabled, unknown) and disabled.status_code == 404
    assert served.limiter.calls == []


def test_a_language_the_widget_does_not_offer_is_a_422() -> None:
    with serve() as served:
        response = create(served, {"business_id": BUSINESS, "ui_language": "hi"})

    assert response.status_code == 422
    assert response.json() == {"detail": LANGUAGE_NOT_OFFERED, "error": "language_not_offered"}
    assert served.limiter.calls == []


@pytest.mark.parametrize("language", ["EN", "english", "", "e"])
def test_a_language_out_of_form_is_the_bodys_422(language: str) -> None:
    with serve() as served:
        response = create(served, {"business_id": BUSINESS, "ui_language": language})

    assert response.status_code == 422
    assert response.json()["problems"][0]["loc"] == ["body", "ui_language"]


def test_the_business_header_is_a_400_before_anything_else() -> None:
    with serve() as served:
        response = served.client.post(
            SESSIONS,
            json={"business_id": BUSINESS, "ui_language": "en"},
            headers={"X-Business-Id": OTHER},  # and no Origin
        )

    assert response.status_code == 400
    assert response.json()["error"] == "business_header_refused"
    assert served.limiter.calls == []


@pytest.mark.parametrize("business_id", BUSINESSES)
def test_a_full_creation_window_is_a_429_with_the_health_businesss_guidance(
    business_id: str,
) -> None:
    # Both sample businesses are health types (G3: their 429 carries the guidance).
    with serve(limiter=FakeLimiter(limit="refuse", retry_after_s=17)) as served:
        response = create(served, {"business_id": business_id, "ui_language": "en"})
        database = served.app.state.database

    assert response.status_code == 429
    assert response.headers["retry-after"] == "17"
    assert response.headers["cache-control"] == NO_STORE
    assert response.json() == {
        "detail": RATE_LIMITED_DETAIL,
        "error": RATE_LIMITED,
        "retry_after_seconds": 17,
        "guidance": guidance_of(business_id),
    }
    assert database.opened == []


def test_a_non_health_businesss_429_carries_no_guidance() -> None:
    # G3: guidance only for a health business. Both sample businesses are health types, so this
    # one is made non-health with guidance lines it must not send (the mutation review).
    with serve(limiter=FakeLimiter(limit="refuse", retry_after_s=9)) as served:
        widgets: WidgetRegistry = served.app.state.widgets
        not_health = replace(
            widgets.businesses[BUSINESS], health=False, guidance=("Synthetic guidance line.",)
        )
        served.app.state.widgets = WidgetRegistry(MappingProxyType({BUSINESS: not_health}))
        response = create(served)

    assert response.status_code == 429
    assert response.json()["guidance"] == []


def test_the_session_token_is_masked_in_every_python_dump() -> None:
    # S3: only the 201's JSON reveals the token; a log extra or a debug dump of the model never.
    created = SessionCreated(
        token=TOKEN,
        expires_at=EXPIRES_AT,
        lifetime_seconds=43_200,
        ui_language="en",
        message_timeout_seconds=65.0,
        confirm_timeout_seconds=15.0,
    )

    assert TOKEN not in repr(created)
    assert TOKEN not in str(created.model_dump())
    assert created.model_dump(mode="json")["token"] == TOKEN


def test_redis_down_refuses_a_new_session_with_a_typed_503() -> None:
    # S14: creation fails closed; the widget shows its cached guidance and contact (S16).
    with serve(limiter=FakeLimiter(limit="raise")) as served:
        response = create(served)
        database = served.app.state.database

    assert response.status_code == 503
    assert response.json() == {"detail": LIMITS_UNAVAILABLE, "error": "limits_unavailable"}
    assert response.headers["cache-control"] == NO_STORE
    assert database.opened == []


@pytest.mark.parametrize(
    "failure",
    [OperationalError("INSERT", {}, OSError()), integrity("23505"), OSError("down")],
    ids=["database down", "another constraint", "network"],
)
def test_a_store_that_cannot_insert_is_the_typed_503(failure: Exception) -> None:
    with serve() as served:
        served.script(failure)
        response = create(served)

    assert response.status_code == 503
    assert response.json() == {
        "detail": SESSION_STORE_UNAVAILABLE,
        "error": "session_store_unavailable",
    }
    assert "never shown" not in response.text


def test_a_hung_store_is_cut_off_at_the_lookup_timeout() -> None:
    with serve() as served:
        served.use(ScriptedDatabase(hang=True))
        response = create(served)

    assert response.status_code == 503
    assert response.json()["error"] == "session_store_unavailable"


def test_an_unregistered_business_is_the_widget_404() -> None:
    # A widget file for a business the registry does not hold: the foreign key refuses the row.
    with serve() as served:
        served.script(integrity("23503"))
        response = create(served)

    assert (response.status_code, response.json()) == (404, NOT_FOUND_BODY)


# ── DELETE /v1/widget/session ("End chat", S18) ──────────────────────────────────────────────


def test_ending_a_session_revokes_the_bearers_own_session() -> None:
    with serve() as served:
        database = served.script([customer_row()], [object()])
        response = served.client.delete(SESSION, headers=BEARER)

    assert response.status_code == 204, response.text
    assert response.content == b""
    assert response.headers["cache-control"] == NO_STORE
    assert database.opened == [BUSINESS, BUSINESS]  # the lookup, then the revoke
    statement, params = database.connection.executed[1]
    assert statement == REVOKE_SESSION.text
    assert params == {"business_id": BUSINESS, "session_id": SESSION_ID}


def test_a_session_revoked_a_moment_earlier_still_ends_with_204() -> None:
    with serve() as served:
        served.script([customer_row()], [])
        response = served.client.delete(SESSION, headers=BEARER)

    assert response.status_code == 204


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no token"),
        pytest.param({"authorization": f"Bearer {MARKER}"}, id="not a token"),
        pytest.param({"cookie": f"__Host-vp_staff={TOKEN}"}, id="the staff cookie"),
    ],
)
def test_without_a_customer_session_ending_is_the_one_401(headers: dict[str, str]) -> None:
    with serve() as served:
        response = served.client.delete(SESSION, headers=headers)
        served.script([])  # a well-formed token the store does not find (or of another kind)
        unknown = served.client.delete(SESSION, headers=BEARER)

    assert response.json() == UNAUTHORIZED_BODY
    assert response.headers["www-authenticate"] == "Bearer"
    assert same_bytes(response, unknown)
    assert MARKER not in response.text


def test_the_lookup_asks_for_a_customer_session_only() -> None:
    # S1: a staff token as a bearer token is not found, because the lookup filters its kind.
    with serve() as served:
        database = served.script([])
        response = served.client.delete(SESSION, headers=BEARER)

    assert response.status_code == 401
    assert "kind = 'customer'" in database.connection.executed[0][0]


def test_the_business_header_next_to_a_session_is_a_400() -> None:
    with serve() as served:
        response = served.client.delete(SESSION, headers={**BEARER, "X-Business-Id": OTHER})
        database = served.app.state.database

    assert response.status_code == 400
    assert response.json()["error"] == "business_header_refused"
    assert database.opened == []


@pytest.mark.parametrize("hang", [False, True], ids=["down", "hung"])
def test_a_revoke_that_cannot_run_is_the_typed_503(hang: bool) -> None:
    with serve() as served:
        connection = ScriptedConnection(
            [[customer_row()], OperationalError("UPDATE", {}, OSError())]
        )
        served.use(ScriptedDatabase(connection))
        if hang:

            class Hanging(ScriptedConnection):
                async def execute(self, statement: Any, params: Any = None) -> Any:
                    if self.executed:
                        await asyncio.Event().wait()
                    return await super().execute(statement, params)

            served.use(ScriptedDatabase(Hanging([[customer_row()]])))
        response = served.client.delete(SESSION, headers=BEARER)

    assert response.status_code == 503
    assert response.json()["error"] == "session_store_unavailable"
