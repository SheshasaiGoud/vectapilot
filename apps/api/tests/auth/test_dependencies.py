"""The auth dependencies without a database (S1, S3, S5, S14): one 401 for every failure, the
header refused, a typed 503 when the lookup cannot run, roles, and S5's CSRF check. Probe routes
on the real app (`tests/auth/probes.py`); the lookup answers from `ScriptedDatabase`."""

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import MultipleResultsFound, OperationalError

from tests.auth.fakes import (
    BUSINESS,
    OTHER,
    OTHER_TOKEN,
    SECRET,
    SESSION_ID,
    STAFF_ID,
    TOKEN,
    ScriptedConnection,
    ScriptedDatabase,
    customer_row,
    staff_row,
)
from tests.auth.probes import ADMIN_PROBE, CSRF_PROBE, CUSTOMER_PROBE, REVIEWER_PROBE, probe_app
from tests.conftest import make_settings
from vectapilot_api.auth.cookies import DEV_STAFF_COOKIE, STAFF_COOKIE
from vectapilot_api.auth.dependencies import (
    BUSINESS_HEADER_REFUSED,
    CROSS_SITE_REFUSED,
    JSON_ONLY,
    LOOKUP_UNAVAILABLE,
    ROLE_REFUSED,
    SESSION_STORE_UNAVAILABLE,
    UNAUTHORIZED,
    StaffAccess,
    require_staff,
)
from vectapilot_api.auth.store import StaffRole
from vectapilot_api.auth.tokens import token_sha256
from vectapilot_api.config import Settings

ORIGIN: Final = "https://api.vectapilot.example"
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
BEARER: Final = {"authorization": f"Bearer {TOKEN}"}


@dataclass
class Probe:
    client: TestClient
    app: FastAPI

    def use(self, database: ScriptedDatabase) -> ScriptedDatabase:
        self.app.state.database = database
        return database


def probe_with(settings: Settings) -> Iterator[Probe]:
    app = probe_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
        yield Probe(client, app)


@pytest.fixture
def probe() -> Iterator[Probe]:
    yield from probe_with(make_settings(public_origin=ORIGIN, session_lookup_timeout_seconds=0.05))


def answering(*rows: Any) -> ScriptedDatabase:
    """A database whose one lookup returns `rows`."""
    return ScriptedDatabase(ScriptedConnection([list(rows)]))


def cookie(value: str, name: str = STAFF_COOKIE) -> dict[str, str]:
    return {"cookie": f"{name}={value}"}


def response_key(response: httpx2.Response) -> tuple[int, bytes, str | None, str | None]:
    return (
        response.status_code,
        response.content,
        response.headers.get("cache-control"),
        response.headers.get("www-authenticate"),
    )


# ── The widget's bearer token (get_customer_session) ────────────────────────────────────────


def test_a_live_customer_session_reaches_the_route_from_its_tokens_business(probe: Probe) -> None:
    database = probe.use(answering(customer_row()))

    response = probe.client.get(CUSTOMER_PROBE, headers=BEARER)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "business_id": BUSINESS,
        "session_id": str(SESSION_ID),
        "customer_ref": f"s-{SESSION_ID.hex}",
        "ui_language": "en",
    }
    assert database.opened == [BUSINESS]  # the token's prefix, never a header or a body
    assert database.connection.executed[0][1] == {
        "business_id": BUSINESS,
        "token_sha256": token_sha256(TOKEN),
    }


def test_the_scheme_is_case_insensitive(probe: Probe) -> None:
    probe.use(answering(customer_row()))

    response = probe.client.get(CUSTOMER_PROBE, headers={"authorization": f"bEaReR {TOKEN}"})

    assert response.status_code == 200, response.text


NO_LOOKUP: Final = [
    pytest.param({}, id="no header"),
    pytest.param([("authorization", f"Bearer {TOKEN}")] * 2, id="two headers"),
    pytest.param({"authorization": f"Basic {TOKEN}"}, id="another scheme"),
    pytest.param({"authorization": "Bearer"}, id="no credentials"),
    pytest.param({"authorization": "Bearer "}, id="empty credentials"),
    pytest.param({"authorization": f"Bearer  {TOKEN}"}, id="two spaces"),
    pytest.param({"authorization": f"Bearer {TOKEN} x"}, id="trailing part"),
    pytest.param({"authorization": f"Bearer {SECRET}"}, id="no business prefix"),
    pytest.param({"authorization": f"Bearer {TOKEN}x"}, id="a malformed secret"),
    pytest.param({"authorization": f"Bearer Paws.{SECRET}"}, id="a malformed business"),
    pytest.param({"cookie": f"{STAFF_COOKIE}={TOKEN}"}, id="a staff cookie, no bearer"),
]


@pytest.mark.parametrize("headers", NO_LOOKUP)
def test_every_malformed_credential_is_the_one_401_without_a_lookup(
    probe: Probe, headers: dict[str, str] | list[tuple[str, str]]
) -> None:
    database = probe.use(answering(customer_row()))
    reference = probe.client.get(CUSTOMER_PROBE)

    response = probe.client.get(CUSTOMER_PROBE, headers=headers)

    assert response_key(response) == response_key(reference)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED_BODY)
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert database.opened == []


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="unknown, expired, revoked or a staff session"),
        pytest.param([customer_row(token_sha256=token_sha256(OTHER_TOKEN))], id="another hash"),
    ],
)
def test_a_token_the_store_does_not_find_is_the_same_401(probe: Probe, rows: list[Any]) -> None:
    # Expiry, revocation and the kind are the statement's own clauses (tests/auth/test_store.py,
    # and against Postgres tests/canary/test_sessions.py): here they all look like "no row".
    reference = probe.client.get(CUSTOMER_PROBE)
    database = probe.use(answering(*rows))

    response = probe.client.get(CUSTOMER_PROBE, headers=BEARER)

    assert response_key(response) == response_key(reference)
    assert database.opened == [BUSINESS]


def test_an_edited_prefix_is_looked_up_in_the_business_it_names(probe: Probe) -> None:
    database = probe.use(answering())

    response = probe.client.get(CUSTOMER_PROBE, headers={"authorization": f"Bearer {OTHER_TOKEN}"})

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED_BODY)
    assert database.opened == [OTHER]
    assert database.connection.executed[0][1]["token_sha256"] == token_sha256(OTHER_TOKEN)


@pytest.mark.parametrize("path", [CUSTOMER_PROBE, REVIEWER_PROBE])
def test_the_business_header_on_a_session_route_is_a_400_before_any_lookup(
    probe: Probe, path: str
) -> None:
    database = probe.use(answering(customer_row()))
    headers = {**BEARER, **cookie(TOKEN), "X-Business-Id": BUSINESS}

    response = probe.client.get(path, headers=headers)

    assert (response.status_code, response.json()) == (
        400,
        {"detail": BUSINESS_HEADER_REFUSED, "error": "business_header_refused"},
    )
    assert response.headers["cache-control"] == "no-store"
    assert database.opened == []


@pytest.mark.parametrize("path", [CUSTOMER_PROBE, REVIEWER_PROBE])
def test_an_empty_business_header_is_still_the_400_before_any_lookup(
    probe: Probe, path: str
) -> None:
    # S1: the header present is the 400, whatever its value (`getlist`, not a truthy `get`).
    database = probe.use(answering(customer_row() if path == CUSTOMER_PROBE else staff_row()))

    response = probe.client.get(path, headers={**BEARER, **cookie(TOKEN), "X-Business-Id": ""})

    assert (response.status_code, response.json()["error"]) == (400, "business_header_refused")
    assert database.opened == []


UNAVAILABLE: Final = [
    pytest.param(lambda: ScriptedDatabase(fail=OSError("connection refused")), id="refused"),
    pytest.param(
        lambda: ScriptedDatabase(fail=OperationalError("SELECT", {}, Exception("down"))),
        id="sqlalchemy",
    ),
    pytest.param(lambda: ScriptedDatabase(hang=True), id="hangs past the timeout"),
    pytest.param(
        lambda: ScriptedDatabase(ScriptedConnection([MultipleResultsFound("two rows")])),
        id="two rows",
    ),
]
"""Factories: each test gets a fresh database (a scripted connection is used up by one lookup)."""


@pytest.mark.parametrize("unavailable", UNAVAILABLE)
@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({"url": CUSTOMER_PROBE, "headers": BEARER}, id="customer"),
        pytest.param({"url": REVIEWER_PROBE, "headers": cookie(TOKEN)}, id="staff"),
    ],
)
def test_a_lookup_that_cannot_run_is_the_typed_503(
    probe: Probe,
    unavailable: Callable[[], ScriptedDatabase],
    request_kwargs: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe.use(unavailable())

    with caplog.at_level(logging.WARNING, logger="vectapilot_api.auth"):
        response = probe.client.get(**request_kwargs)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": SESSION_STORE_UNAVAILABLE, "error": "session_store_unavailable"},
    )
    assert response.headers["cache-control"] == "no-store"
    warnings = [r for r in caplog.records if r.getMessage() == LOOKUP_UNAVAILABLE]
    assert len(warnings) == 1


# ── The staff cookie (require_staff) ────────────────────────────────────────────────────────


def test_a_reviewer_reaches_a_reviewer_route_and_not_an_admin_route(probe: Probe) -> None:
    probe.use(answering(staff_row()))
    allowed = probe.client.get(REVIEWER_PROBE, headers=cookie(TOKEN))
    probe.use(answering(staff_row()))
    refused = probe.client.get(ADMIN_PROBE, headers=cookie(TOKEN))

    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {
        "business_id": BUSINESS,
        "session_id": str(SESSION_ID),
        "staff_id": str(STAFF_ID),
        "role": "reviewer",
        "display_name": "Synthetic Reviewer",
    }
    assert (refused.status_code, refused.json()) == (
        403,
        {"detail": ROLE_REFUSED, "error": "role_refused"},
    )
    assert refused.headers["cache-control"] == "no-store"


def test_an_admin_reaches_both(probe: Probe) -> None:
    for path in (REVIEWER_PROBE, ADMIN_PROBE):
        probe.use(answering(staff_row(role="admin")))
        assert probe.client.get(path, headers=cookie(TOKEN)).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no cookie"),
        pytest.param({"cookie": f"{STAFF_COOKIE}={TOKEN}; {STAFF_COOKIE}={TOKEN}"}, id="two"),
        pytest.param([("cookie", f"{STAFF_COOKIE}={TOKEN}")] * 2, id="two headers"),
        pytest.param(cookie(TOKEN, name=DEV_STAFF_COOKIE), id="the dev name"),
        pytest.param(cookie(TOKEN, name="vp_staff_x"), id="another name"),
        pytest.param({"cookie": STAFF_COOKIE}, id="no value"),
        pytest.param(cookie(f"{TOKEN}x"), id="malformed"),
        pytest.param(BEARER, id="a bearer token"),
    ],
)
def test_every_malformed_staff_credential_is_the_one_401_without_a_lookup(
    probe: Probe, headers: dict[str, str] | list[tuple[str, str]]
) -> None:
    database = probe.use(answering(staff_row()))
    reference = probe.client.get(REVIEWER_PROBE)

    response = probe.client.get(REVIEWER_PROBE, headers=headers)

    assert response_key(response) == response_key(reference)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED_BODY)
    assert "www-authenticate" not in response.headers
    assert database.opened == []


def test_an_unknown_staff_token_is_the_same_401_and_the_body_is_the_customers(probe: Probe) -> None:
    probe.use(answering())
    staff = probe.client.get(REVIEWER_PROBE, headers=cookie(TOKEN))
    probe.use(answering())
    customer = probe.client.get(CUSTOMER_PROBE, headers=BEARER)

    assert (staff.status_code, staff.content) == (customer.status_code, customer.content)


def test_a_cookie_among_others_is_read(probe: Probe) -> None:
    probe.use(answering(staff_row()))

    response = probe.client.get(
        REVIEWER_PROBE, headers={"cookie": f"theme=dark; {STAFF_COOKIE}={TOKEN}; lang=en"}
    )

    assert response.status_code == 200, response.text


def test_under_cookie_insecure_dev_the_dev_name_is_read() -> None:
    settings = make_settings(public_origin=ORIGIN, cookie_insecure_dev=True)
    for probe in probe_with(settings):
        probe.use(answering(staff_row()))
        assert probe.client.get(REVIEWER_PROBE, headers=cookie(TOKEN, DEV_STAFF_COOKIE)).is_success
        probe.use(answering(staff_row()))
        assert probe.client.get(REVIEWER_PROBE, headers=cookie(TOKEN)).status_code == 401


def test_one_dependency_per_role_that_names_its_role() -> None:
    for role in StaffRole:
        access = require_staff(role)
        assert isinstance(access, StaffAccess) and access.role is role
        assert require_staff(role) is access
        assert repr(access) == f"require_staff({role.value!r})"


# ── S5's CSRF check (require_same_origin) ────────────────────────────────────────────────────


def post(probe: Probe, headers: dict[str, str], **kwargs: Any) -> httpx2.Response:
    return probe.client.post(CSRF_PROBE, headers=headers, **kwargs)


def refused_as(response: httpx2.Response, status: int, detail: str, error: str) -> bool:
    return (
        response.status_code == status
        and response.json() == {"detail": detail, "error": error}
        and response.headers["cache-control"] == "no-store"
    )


def test_the_apis_own_origin_passes_with_or_without_a_json_body(probe: Probe) -> None:
    same = {"origin": ORIGIN, "sec-fetch-site": "same-origin"}

    assert post(probe, same).json() == {"passed": True}
    assert post(probe, {"origin": ORIGIN}).status_code == 200  # Sec-Fetch-Site is optional
    assert post(probe, same, json={"a": 1}).status_code == 200
    assert (
        post(
            probe, {**same, "content-type": "application/json; charset=utf-8"}, content=b"{}"
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no origin"),
        pytest.param({"origin": "https://evil.example"}, id="another origin"),
        pytest.param({"origin": "null"}, id="null"),
        pytest.param({"origin": ORIGIN + "/"}, id="near miss"),
        pytest.param({"origin": "http://api.vectapilot.example"}, id="http"),
        pytest.param({"origin": ORIGIN, "sec-fetch-site": "cross-site"}, id="cross-site"),
        pytest.param({"origin": ORIGIN, "sec-fetch-site": "same-site"}, id="same-site"),
        pytest.param({"origin": ORIGIN, "sec-fetch-site": "none"}, id="none"),
    ],
)
def test_any_other_origin_or_fetch_site_is_a_403(probe: Probe, headers: dict[str, str]) -> None:
    response = post(probe, headers)

    assert refused_as(response, 403, CROSS_SITE_REFUSED, "cross_site_refused")


def test_two_origin_headers_are_a_403(probe: Probe) -> None:
    response = probe.client.post(CSRF_PROBE, headers=[("origin", ORIGIN), ("origin", ORIGIN)])

    assert refused_as(response, 403, CROSS_SITE_REFUSED, "cross_site_refused")


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        pytest.param("application/x-www-form-urlencoded", b"a=1", id="form"),
        pytest.param("multipart/form-data; boundary=x", b"--x--", id="multipart"),
        pytest.param("text/plain", b'{"a": 1}', id="text/plain"),
        pytest.param(None, b'{"a": 1}', id="no content type"),
        pytest.param("application/json-patch+json", b"[]", id="another json type"),
    ],
)
def test_a_body_that_is_not_json_is_a_415(
    probe: Probe, content_type: str | None, body: bytes
) -> None:
    headers = {"origin": ORIGIN}
    if content_type is not None:
        headers["content-type"] = content_type

    response = post(probe, headers, content=body)

    assert refused_as(response, 415, JSON_ONLY, "json_only")


def test_two_content_types_are_a_415(probe: Probe) -> None:
    response = probe.client.post(
        CSRF_PROBE,
        headers=[
            ("origin", ORIGIN),
            ("content-type", "application/json"),
            ("content-type", "text/plain"),
        ],
        content=b"{}",
    )

    assert refused_as(response, 415, JSON_ONLY, "json_only")


def test_without_a_public_origin_nothing_passes() -> None:
    for probe in probe_with(make_settings(public_origin=None)):
        response = post(probe, {"origin": ORIGIN, "sec-fetch-site": "same-origin"})
        assert refused_as(response, 403, CROSS_SITE_REFUSED, "cross_site_refused")


# ── S3: never in a log line ──────────────────────────────────────────────────────────────────


def test_no_token_hash_or_cookie_reaches_a_log_record(
    probe: Probe, caplog: pytest.LogCaptureFixture
) -> None:
    secrets_ = (TOKEN, SECRET, token_sha256(TOKEN), token_sha256(OTHER_TOKEN))
    # (the customer route's database, the staff route's): found, not found, unavailable.
    databases: list[tuple[Callable[[], ScriptedDatabase], Callable[[], ScriptedDatabase]]] = [
        (lambda: answering(customer_row()), lambda: answering(staff_row())),
        (answering, answering),
        (lambda: ScriptedDatabase(fail=OSError(f"refused {TOKEN}")),) * 2,
    ]
    with caplog.at_level(logging.DEBUG):
        for for_customer, for_staff in databases:
            probe.use(for_customer())
            probe.client.get(CUSTOMER_PROBE, headers=BEARER)
            probe.use(for_staff())
            probe.client.get(REVIEWER_PROBE, headers=cookie(TOKEN))
        probe.client.get(CUSTOMER_PROBE, headers={"authorization": f"Bearer {TOKEN}x"})

    assert caplog.records, "nothing was logged: the check would be vacuous"
    for record in caplog.records:
        text = f"{record.getMessage()} {vars(record)}"
        assert not any(secret in text for secret in secrets_), record.getMessage()
