"""The widget's fixtures against the API's own models (M8 PR 4's fixture guard; S9, S16, D11).

`apps/web/fixtures/widget/*.json` are the bodies the widget's unit and browser tests are built on.
Each case names the class its body validates against (`model`); this guard validates every case
in JSON mode, as the API sends it, so a widget test can never rest on a shape the API cannot
produce, and a change to a response model that the widget has not followed fails here, in the
`api` job, before it reaches a customer.

- **Response models** (`widget.models`, `actions.types.ConfirmResponse`): a strict
  `model_validate_json` on the body (the widget's parser needs a JSON `true`, never `"yes"`), at
  a status the route declares for it (a 409 confirm is `failed`, a 200 one `executed`).
- **The rules that link `turn` and `handoff`** (`MessageResponse`; zara's PR 4 review): a
  hand-off turn always carries a `HandoffQueued` (`NOT_QUEUED` when the store failed), every
  other kind none, and a queued row never invites a follow-up that the turn's own priority and
  reason forbid (`handoffs.store.invites_follow_up`: a row's rank only rises, S17).
- **Error bodies**, mirrored here because the API builds them as plain JSON: `TypedError` is
  exactly `{detail, error}` (`kb.errors.kb_error_handler`) with a code on `ROUTE_CODES`' list for
  that route and status, each code named in the module that raises it (zara's PR 4 review: the
  widget branches on exact codes, so a code from another route must fail here);
  `RateLimited` is `guards.limits.limited_response`'s 429 with a matching `Retry-After`;
  `ClosedValidation` is `auth.http`'s closed 422.
- **`none`**: a body the widget must survive and the API never sends (an unknown kind, a 500):
  it must *not* validate against the route's success model, so it cannot drift into a real case.
- **Routes**: each file's `route` is a registered widget route, and each case's status is one it
  declares (`none` cases excepted).

The red proof (`test_the_guard_refuses`) runs the same check on broken copies of real cases.
"""

import copy
import inspect
import json
import re
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from vectapilot_api.actions.router import CONFIRM_ERRORS
from vectapilot_api.actions.types import ConfirmResponse
from vectapilot_api.agent.responses import HandoffResponse
from vectapilot_api.auth import dependencies as auth_dependencies
from vectapilot_api.guards import routes as guard_routes
from vectapilot_api.guards.limits import RATE_LIMITED, RATE_LIMITED_DETAIL
from vectapilot_api.guards.types import DecidedBy
from vectapilot_api.handoffs.store import invites_follow_up
from vectapilot_api.kb.errors import INVALID_REQUEST
from vectapilot_api.widget import confirm, feedback, messages
from vectapilot_api.widget import router as widget_router
from vectapilot_api.widget.models import (
    HandoffQueued,
    HandoffStateResponse,
    MessageResponse,
    SessionCreated,
    WidgetConfigResponse,
)

API_ROOT: Final = Path(__file__).resolve().parents[2]
FIXTURES: Final = API_ROOT.parent / "web" / "fixtures" / "widget"

# The fixtures this guard was written against (2026-11-25): a glob that finds fewer is a moved
# folder or a deleted file, never a silent pass.
MIN_FILES: Final = 7
MIN_CASES: Final = 71


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TypedError(_Closed):
    """`kb.errors.kb_error_handler`'s body: a fixed text and a code, nothing else."""

    detail: StrictStr = Field(min_length=1)
    error: StrictStr = Field(pattern=r"^[a-z][a-z_]*$")


class RateLimited(_Closed):
    """`guards.limits.limited_response`'s 429."""

    detail: StrictStr
    error: StrictStr
    retry_after_seconds: StrictInt = Field(ge=0)
    guidance: list[StrictStr]


class Problem(_Closed):
    loc: list[StrictStr]
    type: StrictStr


class ClosedValidation(_Closed):
    """`auth.http`'s closed 422: locations and error types, never the input."""

    detail: StrictStr
    error: StrictStr
    problems: list[Problem] = Field(min_length=1)


# ── Error codes per route and status (zara's PR 4 review) ─────────────────────────────────────

NAMED: Final[list[tuple[ModuleType, str]]] = []
"""Each listed code and the module that raises it by name (`test_every_listed_code_is_raised`)."""


def _raised_in(module: ModuleType, *codes: str) -> frozenset[str]:
    NAMED.extend((module, code) for code in codes)
    return frozenset(codes)


def _confirm_code(code: str, status_code: int) -> str:
    """A code from `actions.router.CONFIRM_ERRORS` at `status_code`: imported, never retyped."""
    found = {entry[1] for entry in CONFIRM_ERRORS.values() if entry[0] == status_code}
    assert code in found, f"CONFIRM_ERRORS has no {code!r} at {status_code}"
    return code


_LIMITS_UNAVAILABLE: Final = frozenset({DecidedBy.LIMITS_UNAVAILABLE.value})
"""The limiter's 503 on the widget's own routes (`raise _error(503, DecidedBy...)`)."""
_HEADER_REFUSED: Final = _raised_in(auth_dependencies, "business_header_refused")
_UNAUTHORIZED: Final = _raised_in(auth_dependencies, "unauthorized")
_SESSION_STORE: Final = _raised_in(auth_dependencies, "session_store_unavailable")
_WIDGET_GONE: Final = _raised_in(widget_router, "widget_not_found")
_SESSION_ROUTE: Final[dict[int, frozenset[str]]] = {
    400: _HEADER_REFUSED,
    401: _UNAUTHORIZED,
    503: _SESSION_STORE,
}
"""Every route behind `get_customer_session`: the header refusal, the one 401, S14's 503."""

ROUTE_CODES: Final[dict[tuple[str, str, int], frozenset[str]]] = {
    **{
        ("GET", "/v1/widget/{}/config", s): c
        for s, c in {400: _HEADER_REFUSED, 404: _WIDGET_GONE}.items()
    },
    **{
        ("POST", "/v1/widget/sessions", s): c
        for s, c in {
            400: _HEADER_REFUSED,
            403: _raised_in(auth_dependencies, "cross_site_refused"),
            404: _WIDGET_GONE,
            415: _raised_in(auth_dependencies, "json_only"),
            503: _LIMITS_UNAVAILABLE | _SESSION_STORE,
        }.items()
    },
    **{("DELETE", "/v1/widget/session", s): c for s, c in _SESSION_ROUTE.items()},
    **{
        ("POST", "/v1/widget/messages", s): c
        for s, c in {**_SESSION_ROUTE, 404: _WIDGET_GONE}.items()
    },
    **{
        ("POST", "/v1/widget/handoffs", s): c
        for s, c in {**_SESSION_ROUTE, 404: _WIDGET_GONE}.items()
    },
    **{
        ("GET", "/v1/widget/handoffs/{}", s): c
        for s, c in {
            **_SESSION_ROUTE,
            404: _WIDGET_GONE | _raised_in(messages, "handoff_not_found"),
            503: _SESSION_STORE
            | _LIMITS_UNAVAILABLE
            | _raised_in(messages, "handoff_store_unavailable"),
        }.items()
    },
    **{
        ("POST", "/v1/widget/actions/{}/confirm", s): c
        for s, c in {
            **_SESSION_ROUTE,
            404: _WIDGET_GONE | {_confirm_code("action_not_found", 404)},
            503: _SESSION_STORE
            | _raised_in(guard_routes, "limits_unavailable")
            | {_confirm_code("confirm_failed", 503), _confirm_code("outcome_unknown", 503)},
        }.items()
    },
    **{
        ("POST", "/v1/widget/feedback", s): c
        for s, c in {
            **_SESSION_ROUTE,
            404: _WIDGET_GONE | _raised_in(feedback, "turn_not_found"),
            409: _raised_in(feedback, "already_rated"),
            503: _SESSION_STORE
            | _LIMITS_UNAVAILABLE
            | _raised_in(feedback, "feedback_store_unavailable"),
        }.items()
    },
}
"""The typed-error codes each widget route can send at each status, from the module that raises
them. A fixture's code must be on its own route's list: `action_not_found` is the only code the
widget reads as "Expired. Nothing was done.", so one borrowed from another route must fail."""


def _check_typed(case: Mapping[str, Any]) -> None:
    TypedError.model_validate_json(json.dumps(case["body"]), strict=True)


def _check_typed_code(case: Mapping[str, Any], method: str, path: str) -> None:
    """After `_check_typed`: the code is one this route sends at this status (`ROUTE_CODES`)."""
    body = TypedError.model_validate_json(json.dumps(case["body"]), strict=True)
    allowed = ROUTE_CODES.get((method, path, case["status"]), frozenset())
    assert body.error in allowed, (
        f"{method} {path} never answers {case['status']} {body.error!r}: {sorted(allowed)}"
    )


def _check_limited(case: Mapping[str, Any]) -> None:
    body = RateLimited.model_validate_json(json.dumps(case["body"]), strict=True)
    assert (body.detail, body.error) == (RATE_LIMITED_DETAIL, RATE_LIMITED)
    headers = case.get("headers", {})
    assert headers.get("Retry-After") == str(body.retry_after_seconds), "Retry-After differs"


def _check_closed_422(case: Mapping[str, Any]) -> None:
    body = ClosedValidation.model_validate_json(json.dumps(case["body"]), strict=True)
    assert (body.detail, body.error) == (INVALID_REQUEST, "invalid_request")


def _model(cls: type[BaseModel]) -> Callable[[Mapping[str, Any]], None]:
    def check(case: Mapping[str, Any]) -> None:
        cls.model_validate_json(json.dumps(case["body"]), strict=True)

    return check


def _check_message(case: Mapping[str, Any]) -> None:
    """`MessageResponse`'s shape, then the rules that link `turn` and `handoff` (module
    docstring; `widget.messages.post_message`)."""
    body = MessageResponse.model_validate_json(json.dumps(case["body"]), strict=True)
    if not isinstance(body.turn, HandoffResponse):
        assert body.handoff is None, f"a {body.turn.kind} turn carries no hand-off"
        return
    assert body.handoff is not None, "a hand-off turn always carries HandoffQueued (S8, S14)"
    if body.handoff.queued and not invites_follow_up(body.turn.priority, body.turn.reason):
        assert not body.handoff.follow_up, (
            f"a {body.turn.priority} {body.turn.reason} turn's row never invites a follow-up (S17)"
        )


def _check_confirm(case: Mapping[str, Any]) -> None:
    body = ConfirmResponse.model_validate_json(json.dumps(case["body"]), strict=True)
    want = {200: "executed", 409: "failed"}[case["status"]]
    assert body.status.value == want, f"a {case['status']} confirm is {want}"


ERROR_STATUSES: Final = frozenset({400, 401, 403, 404, 409, 415, 503})
CHECKS: Final[dict[str, tuple[Callable[[Mapping[str, Any]], None], frozenset[int]]]] = {
    "WidgetConfigResponse": (_model(WidgetConfigResponse), frozenset({200})),
    "SessionCreated": (_model(SessionCreated), frozenset({201})),
    "MessageResponse": (_check_message, frozenset({200})),
    "HandoffQueued": (_model(HandoffQueued), frozenset({200})),
    "HandoffStateResponse": (_model(HandoffStateResponse), frozenset({200})),
    "ConfirmResponse": (_check_confirm, frozenset({200, 409})),
    "TypedError": (_check_typed, ERROR_STATUSES),  # and its code per route: `check_case`
    "RateLimited": (_check_limited, frozenset({429})),
    "ClosedValidation": (_check_closed_422, frozenset({422})),
}
SUCCESS_MODEL: Final[dict[str, type[BaseModel]]] = {
    "MessageResponse": MessageResponse,
    "HandoffQueued": HandoffQueued,
    "HandoffStateResponse": HandoffStateResponse,
    "ConfirmResponse": ConfirmResponse,
    "WidgetConfigResponse": WidgetConfigResponse,
    "SessionCreated": SessionCreated,
}


def _placeholders(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def widget_routes() -> dict[tuple[str, str], APIRoute]:
    """Every widget route the API registers, keyed by method and path (placeholders unnamed)."""
    found: dict[tuple[str, str], APIRoute] = {}
    for module in (widget_router, messages, confirm, feedback):
        for route in module.router.routes:
            assert isinstance(route, APIRoute)
            for method in route.methods or ():
                found[(method, _placeholders(route.path))] = route
    return found


def declared_statuses(route: APIRoute) -> frozenset[int]:
    return frozenset({route.status_code or 200, *(int(code) for code in route.responses)})


def check_case(route_line: str, case: Mapping[str, Any]) -> None:
    """One case: its route and status, then its body (module docstring). Raises on a mismatch."""
    method, path = route_line.split(" ", 1)
    route = widget_routes().get((method, _placeholders(path)))
    assert route is not None, f"{route_line} is not a widget route"
    name = case["model"]
    if name == "none":
        success = route.response_model
        body = case["body"]
        if body is None:
            assert case["status"] in {204, 500}, "only a 204 or a 500 has no body"
            return
        assert isinstance(success, type) and issubclass(success, BaseModel)
        try:
            success.model_validate_json(json.dumps(body))
        except ValidationError:
            return
        raise AssertionError(f"a `none` case is a valid {success.__name__}")
    assert name in CHECKS, f"unknown model {name!r}"
    check, statuses = CHECKS[name]
    assert case["status"] in statuses, f"{name} is never a {case['status']}"
    assert case["status"] in declared_statuses(route), (
        f"{route_line} never answers {case['status']}"
    )
    check(case)
    if name == "TypedError":
        _check_typed_code(case, method, _placeholders(path))


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def all_cases() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path in fixture_files():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        for name, case in loaded["cases"].items():
            yield f"{path.stem}:{name}", loaded["route"], case


CASES: Final = list(all_cases())


def test_the_fixtures_are_there_and_synthetic() -> None:
    files = fixture_files()
    assert len(files) >= MIN_FILES, f"{FIXTURES} holds {len(files)} fixture files"
    assert len(CASES) >= MIN_CASES
    for path in files:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["synthetic"] is True, path.name


@pytest.mark.parametrize(("route_line", "case"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_every_fixture_matches_the_api(route_line: str, case: dict[str, Any]) -> None:
    check_case(route_line, case)


def _case(case_id: str) -> tuple[str, dict[str, Any]]:
    for found_id, route_line, case in CASES:
        if found_id == case_id:
            return route_line, copy.deepcopy(case)
    raise LookupError(case_id)


def _drop(key: str) -> Callable[[dict[str, Any]], None]:
    return lambda body: body.pop(key)


def _set(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    return lambda body: body.__setitem__(key, value)


def _handoff_set(key: str, value: object) -> Callable[[dict[str, Any]], None]:
    return lambda body: body["handoff"].__setitem__(key, value)


def _handoff_drop(key: str) -> Callable[[dict[str, Any]], None]:
    return lambda body: body["handoff"].pop(key)


BROKEN: Final[list[tuple[str, str, Callable[[dict[str, Any]], None]]]] = [
    ("press without follow_up", "handoffs:created", _drop("follow_up")),
    ("press: follow_up without a row", "handoffs:not-stored", _set("follow_up", True)),
    ("press with an extra field", "handoffs:created", _set("priority", "normal")),
    (
        "message: follow_up as text",
        "messages:handoff-normal-queued",
        _handoff_set("follow_up", "yes"),
    ),
    ("message: queued without an id", "messages:handoff-normal-queued", _handoff_set("id", None)),
    ("message without follow_up", "messages:handoff-normal-queued", _handoff_drop("follow_up")),
    ("status without follow_up", "handoff-status:open", _drop("follow_up")),
    ("status unknown", "handoff-status:open", _set("status", "waiting")),
    ("typed error with an extra key", "confirm:widget-not-found", _set("retry_after_seconds", 1)),
    ("typed error, unknown code", "confirm:widget-not-found", _set("error", "widget_gone_fishing")),
    # zara's PR 4 review: a real code from another route, or not a code at all.
    (
        "confirm 404 with the status read's code",
        "confirm:widget-not-found",
        _set("error", "handoff_not_found"),
    ),
    ("confirm 404 with a word, not a code", "confirm:widget-not-found", _set("error", "message")),
    (
        "message 503 with confirm's code",
        "messages:store-unavailable",
        _set("error", "outcome_unknown"),
    ),
    (
        "status read 404 with confirm's code",
        "handoff-status:not-found",
        _set("error", "action_not_found"),
    ),
    (
        "safety turn whose row invites a follow-up",
        "messages:handoff-safety",
        _handoff_set("follow_up", True),
    ),
    ("hand-off turn without HandoffQueued", "messages:handoff-safety", _set("handoff", None)),
    (
        "answer with a queued hand-off",
        "messages:answer",
        _set(
            "handoff",
            {"id": "6f1c2a9e-0d4b-4c8e-9a51-2b7e3f4a5c61", "queued": True, "follow_up": True},
        ),
    ),
    ("429 without guidance", "messages:rate-limited", _drop("guidance")),
    ("config guidance not in en", "config:sunrise-clinic", _set("guidance_language", "hi")),
]


def _wrong_status(case: dict[str, Any]) -> None:
    case["status"] = 409


def _header_mismatch(case: dict[str, Any]) -> None:
    case["headers"]["Retry-After"] = "99"


def _valid_none(case: dict[str, Any]) -> None:
    case["model"] = "none"


def _undeclared(case: dict[str, Any]) -> None:
    case["status"] = 403  # a typed error, but the press route never answers 403


CASE_BROKEN: Final[list[tuple[str, str, Callable[[dict[str, Any]], None]]]] = [
    ("an executed confirm at 409", "confirm:executed", _wrong_status),
    ("Retry-After differs from the body", "messages:rate-limited", _header_mismatch),
    ("a valid body labelled none", "messages:answer", _valid_none),
    ("a status the route never declares", "handoffs:unauthorized", _undeclared),
]


@pytest.mark.parametrize(
    ("case_id", "breaks"),
    [(case_id, breaks) for _, case_id, breaks in BROKEN],
    ids=[label for label, _, _ in BROKEN],
)
def test_the_guard_refuses_a_broken_body(
    case_id: str, breaks: Callable[[dict[str, Any]], None]
) -> None:
    route_line, case = _case(case_id)
    check_case(route_line, copy.deepcopy(case))  # the real case passes
    breaks(case["body"])
    with pytest.raises((AssertionError, ValidationError)):
        check_case(route_line, case)


@pytest.mark.parametrize(
    ("case_id", "breaks"),
    [(case_id, breaks) for _, case_id, breaks in CASE_BROKEN],
    ids=[label for label, _, _ in CASE_BROKEN],
)
def test_the_guard_refuses_a_broken_case(
    case_id: str, breaks: Callable[[dict[str, Any]], None]
) -> None:
    route_line, case = _case(case_id)
    check_case(route_line, copy.deepcopy(case))
    breaks(case)
    with pytest.raises((AssertionError, ValidationError)):
        check_case(route_line, case)


def test_every_listed_code_is_raised_by_its_module() -> None:
    """`ROUTE_CODES` cannot drift from the source: each code is quoted where it is raised."""
    assert NAMED
    for module, code in NAMED:
        assert f'"{code}"' in inspect.getsource(module), f"{module.__name__} never names {code!r}"


def test_every_listed_route_answers_that_status() -> None:
    routes = widget_routes()
    for method, path, status_code in ROUTE_CODES:
        route = routes.get((method, path))
        assert route is not None, f"{method} {path} is not a widget route"
        assert status_code in declared_statuses(route), (
            f"{method} {path} never answers {status_code}"
        )


def test_the_guard_refuses_an_unknown_route() -> None:
    _, case = _case("handoffs:created")
    with pytest.raises(AssertionError):
        check_case("POST /v1/widget/handoff", case)
