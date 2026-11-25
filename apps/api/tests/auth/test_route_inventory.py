"""The route inventory (M8 PR 1; S1, S2, S5, S9, S13, ADR 0019 decisions 2, 3 and 6), on the app
as production builds it. Unit: no database, no Redis.

Read from each route's own dependency tree (what FastAPI will call), not from a list kept by hand,
so a route added later is checked the day it is added:

- **S2:** exactly the seven `X-Business-Id` routes depend on `get_business_id`, and in production
  each one refuses before any business work (400). No session route depends on it.
- **S1:** every `/v1/widget/*` and `/v1/staff/*` route refuses the header first (400), and every
  widget route but the two that come before a session (the config route and session creation)
  needs a customer session. No session route's request body names `customer_ref`, `session_id`
  or `staff_id`, and only session creation's names `business_id` (read from the schema, so PR 2's
  message and confirm routes are checked the day they are added: ADR 0016's IDOR stays closed
  only while the server builds `AgentRequest` and `ConfirmRequest` itself, leo's review).
- **S5:** every staff route but login depends on exactly one `require_staff(role)`, the map equals
  `staff.router.ROUTE_ROLES`, and every non-GET staff route, login included, and session creation
  carry the Origin check.
- **S9, S13:** the session routes, and only they, use `SessionRoute` (`Cache-Control: no-store`, a
  422 that echoes nothing); every existing route keeps FastAPI's own route class. A body FastAPI
  cannot decode (not UTF-8, or not JSON) is refused before any check, typed and `no-store`, on
  every session route that takes a body.
- **ADR 0016 W2, ADR 0019 decision 2:** no staff route reaches the confirm service, and exactly
  two routes can serialize the confirm token: `/v1/agent` and `/v1/widget/messages` (leo's (6)).
- **S6 (M8 PR 2):** the widget's message route takes the guard and both services from the same
  dependency functions as `/v1/agent`, and widget confirm (PR 2b) the confirm service and limits
  from the confirm route's, so a test override wires both identically.
- **The product pages (PR 2b, D2 (a)):** the five page and asset routes are the only routes the
  schema leaves out; each is a GET of FastAPI's own route class that needs no session, no header
  and no Origin (a page view writes nothing and opens no session, S14).
- **One 401 for every route:** without credentials, or with the other surface's credential (a
  bearer token on a staff route, the staff cookie on a widget route), every session route answers
  the same 401 bytes before any lookup.
"""

import json
from collections.abc import Callable, Iterator
from typing import Any, Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from tests.auth.fakes import TOKEN, ScriptedDatabase
from tests.conftest import make_settings
from tests.guards.test_settings import PRODUCTION
from vectapilot_api.actions.router import get_confirm_limits, get_confirm_service
from vectapilot_api.agent.router import (
    get_agent_guard,
    get_agent_service,
    get_read_only_agent_service,
)
from vectapilot_api.auth.cookies import STAFF_COOKIE
from vectapilot_api.auth.dependencies import (
    UNAUTHORIZED,
    StaffAccess,
    get_customer_session,
    refuse_business_header,
    require_same_origin,
)
from vectapilot_api.auth.http import UNREADABLE_REQUEST, SessionRoute
from vectapilot_api.business import REFUSED_ENVIRONMENT, get_business_id
from vectapilot_api.main import create_app
from vectapilot_api.staff.router import ROUTE_ROLES
from vectapilot_api.widget.messages import request_started

Key = tuple[str, str]

HEADER_ROUTES: Final[frozenset[Key]] = frozenset(
    {
        ("POST", "/v1/answer"),
        ("POST", "/v1/kb/documents"),
        ("POST", "/v1/kb/reindex"),
        ("GET", "/v1/kb/search"),
        ("POST", "/v1/agent"),
        ("POST", "/v1/agent/actions/{action_id}/confirm"),
        ("POST", "/v1/extractions"),
    }
)
"""S2's seven header routes (the plan's Facts, `business.py`), a local, test and ci seam (D5)."""
BEFORE_A_SESSION: Final[frozenset[Key]] = frozenset(
    {("GET", "/v1/widget/{business_id}/config"), ("POST", "/v1/widget/sessions")}
)
"""The widget routes a customer reaches before a session exists: files only, and creating one."""
SESSION_PREFIXES: Final = ("/v1/widget/", "/v1/staff/")
PATH_VALUES: Final = {
    "{business_id}": "paws-and-co",
    "{action_id}": "0f0e0d0c-0b0a-4908-8706-050403020100",
    "{handoff_id}": "1f0e0d0c-0b0a-4908-8706-050403020100",
}
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
PAGE_ROUTES: Final[frozenset[Key]] = frozenset(
    {
        ("GET", "/widget/{business_id}"),
        ("GET", "/staff/"),
        ("GET", "/staff/{page}"),
        ("GET", "/_next/static/{asset:path}"),
        ("GET", "/icon.svg"),
    }
)
"""PR 2b's product pages and assets (`ui.pages`): HTML and files, not API operations."""


def served_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the app serves. FastAPI 0.143 keeps each included router as one entry
    holding the original router; none of ours adds a prefix or a dependency when included (pinned
    here), so the original router's routes are exactly what is served."""
    routes: list[APIRoute] = []
    for entry in app.routes:
        if isinstance(entry, APIRoute):
            routes.append(entry)
            continue
        original = getattr(entry, "original_router", None)
        if original is None:
            continue  # Starlette's own routes: the docs and the schema
        context = entry.include_context  # type: ignore[attr-defined]
        assert (context.prefix, context.dependencies) == ("", [])
        routes.extend(route for route in original.routes if isinstance(route, APIRoute))
    return routes


def calls(dependant: Dependant) -> list[Callable[..., Any]]:
    """Every callable in a dependency tree, depth first (the route's own endpoint excluded)."""
    found: list[Callable[..., Any]] = []
    for sub in dependant.dependencies:
        if sub.call is not None:
            found.append(sub.call)
        found += calls(sub)
    return found


def keys(route: APIRoute) -> list[Key]:
    return [(method, route.path) for method in sorted(route.methods or ())]


def session_route(route: APIRoute) -> bool:
    return route.path.startswith(SESSION_PREFIXES)


def concrete(path: str) -> str:
    for placeholder, value in PATH_VALUES.items():
        path = path.replace(placeholder, value)
    return path


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(make_settings(**PRODUCTION))


@pytest.fixture(scope="module")
def routes(app: FastAPI) -> dict[Key, APIRoute]:
    return {key: route for route in served_routes(app) for key in keys(route)}


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, base_url="https://testserver") as client:
        app.state.database = ScriptedDatabase()  # no lookup may run: any statement fails
        yield client


def session_keys(routes: dict[Key, APIRoute]) -> list[Key]:
    return sorted(key for key, route in routes.items() if session_route(route))


# ── The inventory itself ─────────────────────────────────────────────────────────────────────


def test_the_inventory_is_every_route_the_app_documents(
    app: FastAPI, routes: dict[Key, APIRoute]
) -> None:
    schema = {
        (method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item
    }

    assert set(routes) - PAGE_ROUTES == schema
    assert set(routes) >= PAGE_ROUTES and not PAGE_ROUTES & schema
    # PR 1's three widget and three staff routes, PR 2a's three widget routes and PR 2b's two.
    assert len(session_keys(routes)) == 11


def test_the_pages_need_no_session_no_header_and_no_origin(routes: dict[Key, APIRoute]) -> None:
    # S14: a page view opens no session and spends no window; the API routes it calls do the rest.
    session_checks = {get_customer_session, refuse_business_header, require_same_origin}
    for key in PAGE_ROUTES:
        route = routes[key]
        assert not isinstance(route, SessionRoute), key
        assert not session_checks & set(calls(route.dependant)), key
        assert not any(isinstance(call, StaffAccess) for call in calls(route.dependant)), key
        assert get_business_id not in calls(route.dependant), key
        assert route.body_field is None and not route.include_in_schema, key


# ── S2: the header seam, never reachable in production ───────────────────────────────────────


def test_exactly_the_seven_header_routes_depend_on_the_header(routes: dict[Key, APIRoute]) -> None:
    depending = {key for key, route in routes.items() if get_business_id in calls(route.dependant)}

    assert depending == HEADER_ROUTES


@pytest.mark.parametrize("key", sorted(HEADER_ROUTES), ids=" ".join)
def test_in_production_each_header_route_refuses_before_any_business_work(
    client: TestClient, key: Key
) -> None:
    method, path = key

    response = client.request(method, concrete(path), headers={"X-Business-Id": "sunrise-clinic"})

    assert (response.status_code, response.json()) == (400, {"detail": REFUSED_ENVIRONMENT})
    assert client.app.state.database.opened == []  # type: ignore[attr-defined]


# ── S1: the session routes ───────────────────────────────────────────────────────────────────


def test_no_session_route_depends_on_the_header(routes: dict[Key, APIRoute]) -> None:
    for key in session_keys(routes):
        assert get_business_id not in calls(routes[key].dependant), key


SESSION_NAMES: Final = frozenset({"customer_ref", "session_id", "staff_id"})
"""S1, S4: whom only a session row names. ADR 0016's IDOR stays closed only while no session
route takes one of them from a client (leo's review: `POST /v1/widget/messages` declaring
`AgentRequest` as its body would reopen it, with every other test green)."""
NAMES_THE_BUSINESS: Final[frozenset[Key]] = frozenset({("POST", "/v1/widget/sessions")})
"""The one request before a session exists names its business (B2's proposal)."""


def body_names(schema: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """Every property name anywhere in an operation's request body, each `$ref` resolved."""
    components = schema.get("components", {}).get("schemas", {})
    names: set[str] = set()
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            seen.add(ref)
            walk(components[ref.rsplit("/", 1)[-1]])
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            walk(value)

    walk(operation.get("requestBody", {}))
    return names


def test_no_session_route_body_names_whom_only_the_session_names(
    app: FastAPI, routes: dict[Key, APIRoute]
) -> None:
    schema = app.openapi()
    for key in session_keys(routes):
        names = body_names(schema, schema["paths"][key[1]][key[0].lower()])
        assert not names & SESSION_NAMES, key
        assert ("business_id" in names) == (key in NAMES_THE_BUSINESS), key
    # The positive controls: the walk reads real bodies, and the header route's names the customer.
    assert body_names(schema, schema["paths"]["/v1/staff/login"]["post"]) == {"token"}
    assert {"customer_ref", "message"} <= body_names(schema, schema["paths"]["/v1/agent"]["post"])
    confirm = schema["paths"]["/v1/agent/actions/{action_id}/confirm"]["post"]
    assert "customer_ref" in body_names(schema, confirm)


def test_every_widget_route_but_the_two_before_a_session_needs_one(
    routes: dict[Key, APIRoute],
) -> None:
    widget = {key for key in session_keys(routes) if key[1].startswith("/v1/widget/")}
    without = {key for key in widget if get_customer_session not in calls(routes[key].dependant)}

    assert without == BEFORE_A_SESSION
    assert widget > BEFORE_A_SESSION  # at least one route needs a session (the positive control)


EVERY_SESSION_ROUTE: Final[frozenset[Key]] = frozenset(
    {
        ("GET", "/v1/widget/{business_id}/config"),
        ("POST", "/v1/widget/sessions"),
        ("DELETE", "/v1/widget/session"),
        ("POST", "/v1/widget/messages"),
        ("POST", "/v1/widget/handoffs"),
        ("GET", "/v1/widget/handoffs/{handoff_id}"),
        ("POST", "/v1/widget/actions/{action_id}/confirm"),
        ("POST", "/v1/widget/feedback"),
        ("POST", "/v1/staff/login"),
        ("POST", "/v1/staff/logout"),
        ("GET", "/v1/staff/me"),
    }
)
"""PR 1's six routes, PR 2a's three widget routes and PR 2b's two (confirm, feedback)."""


@pytest.mark.parametrize("key", sorted(EVERY_SESSION_ROUTE), ids=" ".join)
def test_every_session_route_refuses_the_business_header_first(
    client: TestClient, routes: dict[Key, APIRoute], key: Key
) -> None:
    # No Origin, no credential and no body: the header is the first thing each route refuses.
    # The message route's clock (`request_started`, noah's PR 2 review) comes first and refuses
    # nothing: it reads `time.perf_counter()` only.
    found = calls(routes[key].dependant)
    if key == ("POST", "/v1/widget/messages"):
        assert found[0] is request_started
        found = found[1:]
    assert request_started not in found
    first = found[0]
    assert first in (refuse_business_header, get_customer_session) or isinstance(first, StaffAccess)

    response = client.request(key[0], concrete(key[1]), headers={"X-Business-Id": "paws-and-co"})

    assert response.status_code == 400
    assert response.json()["error"] == "business_header_refused"
    assert response.headers["cache-control"] == "no-store"


def test_the_parametrized_list_is_every_session_route(routes: dict[Key, APIRoute]) -> None:
    assert set(session_keys(routes)) == EVERY_SESSION_ROUTE


# ── S5: roles and the Origin check ───────────────────────────────────────────────────────────


def test_every_staff_route_maps_to_exactly_one_role_and_the_map_is_the_routers(
    routes: dict[Key, APIRoute],
) -> None:
    derived: dict[Key, Any] = {}
    for key in session_keys(routes):
        if not key[1].startswith("/v1/staff/"):
            continue
        accesses = [call for call in calls(routes[key].dependant) if isinstance(call, StaffAccess)]
        assert len(accesses) <= 1, key
        derived[key] = accesses[0].role if accesses else None

    assert derived == ROUTE_ROLES
    assert [key for key, role in derived.items() if role is None] == [("POST", "/v1/staff/login")]


def test_every_non_get_staff_route_and_session_creation_check_the_origin(
    routes: dict[Key, APIRoute],
) -> None:
    for key in session_keys(routes):
        checked = require_same_origin in calls(routes[key].dependant)
        staff_write = key[1].startswith("/v1/staff/") and key[0] != "GET"
        if staff_write or key == ("POST", "/v1/widget/sessions"):
            assert checked, key
        if key[0] == "GET":
            assert not checked, key  # a GET changes nothing (and a browser may omit Origin)
    assert ("POST", "/v1/staff/login") in routes  # login is one of them (login CSRF)


def response_schemas(schema: dict[str, Any], operation: dict[str, Any]) -> set[str]:
    """Every component schema an operation's responses can serialize, each `$ref` resolved."""
    components = schema.get("components", {}).get("schemas", {})
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            if name not in seen:
                seen.add(name)
                walk(components[name])
        for value in node.values():
            walk(value)

    walk(operation.get("responses", {}))
    return seen


CARRY_THE_CONFIRM_TOKEN: Final[frozenset[Key]] = frozenset(
    {("POST", "/v1/agent"), ("POST", "/v1/widget/messages")}
)
"""ADR 0016 W2's "one response that carries the token", named twice by ADR 0019 (leo's (6))."""


def test_no_staff_route_reaches_the_confirm_service_or_its_token(
    app: FastAPI, routes: dict[Key, APIRoute]
) -> None:
    schema = app.openapi()
    for key in session_keys(routes):
        if not key[1].startswith("/v1/staff/"):
            continue
        assert not {get_confirm_service, get_confirm_limits} & set(calls(routes[key].dependant))
        operation = schema["paths"][key[1]][key[0].lower()]
        assert "ConfirmationResponse" not in response_schemas(schema, operation), key
        assert "confirm_token" not in json.dumps(operation), key


def test_exactly_the_agent_and_the_widget_message_routes_can_serialize_the_confirm_token(
    app: FastAPI, routes: dict[Key, APIRoute]
) -> None:
    schema = app.openapi()
    carrying = {
        key
        for key in set(routes) - PAGE_ROUTES
        if "ConfirmationResponse"
        in response_schemas(schema, schema["paths"][key[1]][key[0].lower()])
    }

    assert carrying == CARRY_THE_CONFIRM_TOKEN


def test_the_widget_message_route_takes_agents_guard_and_services_from_its_functions(
    routes: dict[Key, APIRoute],
) -> None:
    # S6: the same dependency functions (`agent/router.py`), so one override wires both routes,
    # and the guard is called by the route, never run as a screening dependency (M7 G13).
    same = {get_agent_guard, get_agent_service, get_read_only_agent_service}
    for key in CARRY_THE_CONFIRM_TOKEN:
        assert same <= set(calls(routes[key].dependant)), key


CONFIRMING: Final[frozenset[Key]] = frozenset(
    {
        ("POST", "/v1/agent/actions/{action_id}/confirm"),
        ("POST", "/v1/widget/actions/{action_id}/confirm"),
    }
)
"""The two routes that may reach the confirm service (ADR 0019 decision 2)."""


def test_exactly_the_two_confirm_routes_reach_the_confirm_service_through_its_functions(
    routes: dict[Key, APIRoute],
) -> None:
    # S6 (PR 2b): widget confirm takes the confirm service and the limiter pre-check from the
    # confirm route's own dependency functions; no other route reaches either.
    same = {get_confirm_service, get_confirm_limits}
    reaching = {key for key, route in routes.items() if same & set(calls(route.dependant))}

    assert reaching == CONFIRMING
    for key in CONFIRMING:
        assert same <= set(calls(routes[key].dependant)), key
    widget = calls(routes[("POST", "/v1/widget/actions/{action_id}/confirm")].dependant)
    assert get_customer_session in widget and get_business_id not in widget


# ── S9, S13: `no-store` on the new routes only ───────────────────────────────────────────────


def test_the_session_routes_and_only_they_are_session_routes(routes: dict[Key, APIRoute]) -> None:
    for key, route in routes.items():
        assert isinstance(route, SessionRoute) == session_route(route), key


BODY_ROUTES: Final = [
    ("POST", "/v1/widget/sessions"),
    ("POST", "/v1/widget/messages"),
    ("POST", "/v1/widget/actions/{action_id}/confirm"),
    ("POST", "/v1/widget/feedback"),
    ("POST", "/v1/staff/login"),
]
"""The session routes that take a body (FastAPI decodes it before any dependency runs)."""
UNDECODABLE: Final = [
    pytest.param(b'{"token": "\xff\xfe zqxmarker"}', 400, id="not UTF-8"),
    pytest.param(b'{"token": "zqxmarker', 422, id="not JSON"),
]


def test_the_body_routes_are_every_session_route_with_a_body(routes: dict[Key, APIRoute]) -> None:
    with_body = {key for key in session_keys(routes) if routes[key].body_field is not None}

    assert with_body == set(BODY_ROUTES)


@pytest.mark.parametrize(("content", "status"), UNDECODABLE)
@pytest.mark.parametrize("key", BODY_ROUTES, ids=" ".join)
def test_a_body_that_cannot_be_decoded_is_refused_first_typed_and_no_store(
    client: TestClient, key: Key, content: bytes, status: int
) -> None:
    # Zara's review: FastAPI reads the body before the header, the Origin and any lookup, so these
    # come first, and write nothing. The 400 is FastAPI's own HTTPException, typed here.
    headers = {
        "content-type": "application/json",
        "origin": "https://evil.example",
        "X-Business-Id": "sunrise-clinic",
    }

    response = client.request(key[0], concrete(key[1]), content=content, headers=headers)

    assert response.status_code == status
    assert response.json()["error"] == "invalid_request"
    if status == 400:
        assert response.json() == {"detail": UNREADABLE_REQUEST, "error": "invalid_request"}
    assert response.headers["cache-control"] == "no-store"
    assert "zqxmarker" not in response.text and "sunrise-clinic" not in response.text
    assert client.app.state.database.opened == []  # type: ignore[attr-defined]


# ── One 401 ──────────────────────────────────────────────────────────────────────────────────

NEEDS_A_SESSION: Final = [
    ("DELETE", "/v1/widget/session", "bearer"),
    ("POST", "/v1/widget/messages", "bearer"),
    ("POST", "/v1/widget/handoffs", "bearer"),
    ("GET", "/v1/widget/handoffs/{handoff_id}", "bearer"),
    ("POST", "/v1/widget/actions/{action_id}/confirm", "bearer"),
    ("POST", "/v1/widget/feedback", "bearer"),
    ("POST", "/v1/staff/logout", "cookie"),
    ("GET", "/v1/staff/me", "cookie"),
]


def credentials(kind: str) -> dict[str, str]:
    """The other surface's credential: a staff cookie on a widget route, a bearer token on a
    staff route (S1). Each is well-formed, so only the route's own reading refuses it."""
    if kind == "bearer":
        return {"cookie": f"{STAFF_COOKIE}={TOKEN}"}
    return {"authorization": f"Bearer {TOKEN}"}


def test_every_session_route_gives_the_same_401_before_any_lookup(
    client: TestClient, routes: dict[Key, APIRoute]
) -> None:
    needing = {
        key
        for key in session_keys(routes)
        if get_customer_session in calls(routes[key].dependant)
        or any(isinstance(c, StaffAccess) for c in calls(routes[key].dependant))
    }
    assert needing == {(method, path) for method, path, _ in NEEDS_A_SESSION}
    origin = {"origin": "https://api.vectapilot.example"}  # PRODUCTION's public origin

    answers: list[httpx2.Response] = []
    for method, path, kind in NEEDS_A_SESSION:
        for headers in ({}, credentials(kind)):
            response = client.request(method, concrete(path), headers={**origin, **headers})
            assert response.json() == UNAUTHORIZED_BODY, (method, path, headers)
            assert response.headers["cache-control"] == "no-store"
            assert response.headers.get("www-authenticate") == (
                "Bearer" if kind == "bearer" else None
            )
            answers.append(response)

    assert len({response.content for response in answers}) == 1  # one body, byte for byte
    assert client.app.state.database.opened == []  # type: ignore[attr-defined]
