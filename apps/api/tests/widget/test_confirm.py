"""`POST /v1/widget/actions/{action_id}/confirm` without a database or Redis (M8 PR 2b; S1, S4,
S6, S9, ADR 0016 W2, W6, ADR 0019 decision 2): leo's seven tests, the parts a unit can prove.

The real app with the guards on (`tests/widget/guarded.py`: the sample businesses' folders,
`FakeLimiter`, the session lookup on `ScriptedDatabase`), and the confirm route's own stand-in
service (`tests/actions/test_confirm_route.StandIn`) answered through `get_confirm_service`, the
dependency both confirm routes share (S6), so both routes reach the very same object:

- (1) a widget message's `confirmation_required` carries the 22-character token in its JSON, and
  widget confirm hands that token, with the session's `customer_ref`, to the service; a repeat
  returns the identical body (the stored result; one write row and the real service:
  `tests/canary/test_widget_confirm.py`);
- (3) a body naming `customer_ref`, tool arguments or anything else is a closed 422 that echoes
  nothing, and a token no customer session has (a staff session's: the lookup filters the kind)
  is the one 401, neither reaching the limiter or the service;
- (4) for every outcome (executed, 409, 404, `confirm_failed`, `outcome_unknown`,
  `limits_unavailable`, 429), status, body and headers equal `/v1/agent/actions/{id}/confirm`'s,
  but the trace id's value and the widget's `Cache-Control: no-store` (S9) [proposal: the plan says
  "except the trace id"; `no-store` is on every widget response by S9];
- (5) the token is in no log record and in no statement the route sends; against Postgres, in no
  audit, hand-off or turn row and no `tool_call` record: `tests/canary/test_widget_confirm.py`;
- (6) the route inventory: `tests/auth/test_route_inventory.py`;
- (7) with the guards off, the limiter's own typed 503 and never the service (S6: no unguarded
  branch).
"""

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import httpx2
import pytest
from sqlalchemy.exc import OperationalError

from tests.actions.test_confirm_route import EXECUTED, FAILED, StandIn
from tests.auth.fakes import (
    BUSINESS,
    SESSION_ID,
    ScriptedConnection,
    ScriptedDatabase,
    customer_row,
)
from tests.widget.guarded import BEARER, BENIGN, GuardedServed, serve_guarded
from tests.widget.helpers import guidance_of
from tests.widget.test_messages import confirmation, stub
from vectapilot_api.actions.router import ACTION_NOT_FOUND, get_confirm_service
from vectapilot_api.actions.tokens import TOKEN_LENGTH, new_confirm_token
from vectapilot_api.actions.types import (
    ActionNotFound,
    ConfirmFailed,
    ConfirmRequest,
    ConfirmResponse,
    OutcomeUnknown,
)
from vectapilot_api.auth.dependencies import SESSION_STORE_UNAVAILABLE, UNAUTHORIZED
from vectapilot_api.auth.http import UNKNOWN_KEY
from vectapilot_api.auth.store import CUSTOMER_SESSION
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall, Mode
from vectapilot_api.guards.limits import RATE_LIMITED
from vectapilot_api.guards.routes import LIMITS_UNAVAILABLE, unavailable_response
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.router import WIDGET_NOT_FOUND

ACTION: Final = UUID("5f0c0b1e-1111-4222-8333-444455556666")
CONFIRM_TOKEN: Final = "Q7zx9TOKENtokenTOKEN_-"  # noqa: S105 (a test token, not a secret)
WIDGET_CONFIRM: Final = f"/v1/widget/actions/{ACTION}/confirm"
AGENT_CONFIRM: Final = f"/v1/agent/actions/{ACTION}/confirm"
CUSTOMER: Final = f"s-{SESSION_ID.hex}"
"""The session's `customer_ref` (S4): the one the widget route builds `ConfirmRequest` with."""
GUIDANCE: Final = guidance_of(BUSINESS)
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
H: Final[dict[str, Any]] = {"headers": BEARER}
APART: Final = frozenset({TRACE_HEADER.lower(), "cache-control"})
"""The headers parity leaves out (leo's (4)): the trace id's value, and the widget's `no-store`."""


@dataclass
class Registered(ScriptedDatabase):
    """`ScriptedDatabase` that also answers the header route's registry lookup: registered."""

    async def business_exists(self, business_id: str) -> bool:
        return True


@dataclass
class Confirming:
    served: GuardedServed
    service: StandIn
    databases: list[ScriptedDatabase] = field(default_factory=list)

    def use(self, *script: list[Any] | BaseException) -> ScriptedDatabase:
        database = Registered(ScriptedConnection(list(script)))
        self.served.app.state.database = database
        self.databases.append(database)
        return database

    def widget(
        self, body: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None
    ) -> httpx2.Response:
        """Widget confirm with `body` (the token alone by default), after one session lookup."""
        self.use([customer_row()])
        return self.served.client.post(
            WIDGET_CONFIRM,
            json={"confirm_token": CONFIRM_TOKEN} if body is None else body,
            headers={**BEARER, **(headers or {})},
        )

    def agent(self) -> httpx2.Response:
        """The header route, as the same customer: the session's reference, sent by hand."""
        self.use()
        return self.served.client.post(
            AGENT_CONFIRM,
            json={"customer_ref": CUSTOMER, "confirm_token": CONFIRM_TOKEN},
            headers={BUSINESS_HEADER: BUSINESS},
        )


@contextmanager
def confirming(
    answer: ConfirmResponse | BaseException = EXECUTED, *, limit: Mode = "allow", **overrides: Any
) -> Iterator[Confirming]:
    """The guarded app with the stand-in as the confirm service of both routes (S6)."""
    service = StandIn(answer)
    with serve_guarded(limiter=FakeLimiter(limit=limit), **overrides) as served:
        served.app.dependency_overrides[get_confirm_service] = lambda: service
        yield Confirming(served, service)


def comparable(response: httpx2.Response) -> list[tuple[str, str]]:
    return sorted((k, v) for k, v in response.headers.multi_items() if k not in APART)


def statements(confirming: Confirming) -> list[tuple[str, Any]]:
    return [item for database in confirming.databases for item in database.connection.executed]


# ── (1) the token revealed once, then confirmed with the session's customer ──────────────────


def test_a_widget_confirmation_is_confirmed_with_its_token_and_the_sessions_customer() -> None:
    token = new_confirm_token().get_secret_value()
    with confirming() as confirming_:
        served = confirming_.served
        stub(served, confirmation(token))
        served.script([], [customer_row()], [customer_row()])
        turn = served.send(BENIGN).json()["turn"]
        action = turn["action_id"]
        path = f"/v1/widget/actions/{action}/confirm"
        first = served.client.post(path, json={"confirm_token": turn["confirm_token"]}, **H)
        again = served.client.post(path, json={"confirm_token": turn["confirm_token"]}, **H)

    # The reveal is the JSON serializer's (ADR 0016 W2's second route): 22 characters, unmasked.
    assert turn["kind"] == "confirmation_required"
    assert turn["confirm_token"] == token and len(token) == TOKEN_LENGTH
    assert first.status_code == 200 and first.json() == json.loads(EXECUTED.model_dump_json())
    assert (again.status_code, again.content) == (200, first.content)  # the stored result
    assert first.headers["cache-control"] == "no-store"
    [(business, action_id, request, trace), _] = confirming_.service.calls
    assert (business, str(action_id)) == (BUSINESS, action)
    assert request == ConfirmRequest.model_validate(
        {"customer_ref": CUSTOMER, "confirm_token": token}
    )
    assert request.confirm_token.get_secret_value() == token
    assert first.headers[TRACE_HEADER] == trace
    # Counted per session (S4) under the confirm route's own slug, before the service.
    assert LimiterCall("admit", BUSINESS, RouteSlug.CONFIRM, customered=True) in (
        served.limiter.calls
    )


# ── (3) a closed body; the one 401 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"confirm_token": CONFIRM_TOKEN, "customer_ref": "zqxmarker"}, id="customer"),
        pytest.param({"confirm_token": CONFIRM_TOKEN, "slot_id": "S-17"}, id="tool arguments"),
        pytest.param(
            {"confirm_token": CONFIRM_TOKEN, "arguments": {"slot_id": "S-17"}}, id="nested"
        ),
        pytest.param({"confirm_token": CONFIRM_TOKEN, "business_id": "zqxmarker"}, id="business"),
        pytest.param({"confirm_token": CONFIRM_TOKEN, "session_id": "zqxmarker"}, id="session"),
        pytest.param({}, id="no token"),
        pytest.param({"confirm_token": CONFIRM_TOKEN + "x"}, id="long token"),
        pytest.param({"confirm_token": "zqxmarker"}, id="short token"),
    ],
)
def test_a_body_naming_anyone_or_anything_but_the_token_is_a_closed_422(
    body: dict[str, Any],
) -> None:
    with confirming() as confirming_:
        response = confirming_.widget(body)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    for text in (CONFIRM_TOKEN, "zqxmarker", "S-17"):
        assert text not in response.text
    problems = response.json()["problems"]
    assert all(UNKNOWN_KEY in p["loc"] or p["type"] != "extra_forbidden" for p in problems)
    assert confirming_.service.calls == [] and confirming_.served.limiter.calls == []
    assert len(statements(confirming_)) == 1  # the session lookup, then nothing


def test_a_malformed_action_id_is_a_closed_422_that_echoes_it_not() -> None:
    with confirming() as confirming_:
        confirming_.use([customer_row()])
        response = confirming_.served.client.post(
            "/v1/widget/actions/zqxmarker/confirm",
            json={"confirm_token": CONFIRM_TOKEN},
            headers=BEARER,
        )

    assert response.status_code == 422 and "zqxmarker" not in response.text
    assert confirming_.service.calls == []


def test_a_token_no_customer_session_has_is_the_one_401_before_limits_or_service() -> None:
    # A staff session's token sent as a bearer token: the customer lookup filters its kind (S1),
    # so it finds no row, as for an unknown token. Against Postgres with a real staff session:
    # tests/canary/test_widget_confirm.py.
    with confirming() as confirming_:
        database = confirming_.use([])
        staff_as_bearer = confirming_.served.client.post(
            WIDGET_CONFIRM, json={"confirm_token": CONFIRM_TOKEN}, headers=BEARER
        )
        anonymous = confirming_.served.client.post(
            WIDGET_CONFIRM, json={"confirm_token": CONFIRM_TOKEN}
        )
        header = confirming_.widget(headers={BUSINESS_HEADER: BUSINESS})

    [(sql, _)] = database.connection.executed
    assert sql == CUSTOMER_SESSION.text and "kind = 'customer'" in sql
    for response in (staff_as_bearer, anonymous):
        assert (response.status_code, response.json()) == (401, UNAUTHORIZED_BODY)
        assert response.headers["www-authenticate"] == "Bearer"
    assert staff_as_bearer.content == anonymous.content
    assert (header.status_code, header.json()["error"]) == (400, "business_header_refused")
    assert confirming_.service.calls == [] and confirming_.served.limiter.calls == []


@pytest.mark.parametrize("hang", [False, True], ids=["down", "hung"])
def test_with_postgres_down_widget_confirm_is_s14s_typed_503_and_runs_nothing(hang: bool) -> None:
    with confirming() as confirming_:
        served = confirming_.served
        served.app.state.database = (
            ScriptedDatabase(hang=True)
            if hang
            else ScriptedDatabase(fail=OperationalError("SELECT ...", {}, Exception()))
        )
        response = served.client.post(
            WIDGET_CONFIRM, json={"confirm_token": CONFIRM_TOKEN}, headers=BEARER
        )

    assert (response.status_code, response.json()) == (
        503,
        {"detail": SESSION_STORE_UNAVAILABLE, "error": "session_store_unavailable"},
    )
    assert confirming_.service.calls == [] and served.limiter.calls == []


def test_a_session_whose_widget_is_disabled_gets_the_one_404_and_confirms_nothing() -> None:
    with confirming() as confirming_:
        confirming_.served.app.state.widgets = WidgetRegistry.empty()
        response = confirming_.widget()

    assert (response.status_code, response.json()) == (
        404,
        {"detail": WIDGET_NOT_FOUND, "error": "widget_not_found"},
    )
    assert confirming_.service.calls == [] and confirming_.served.limiter.calls == []


def test_a_bug_in_the_service_propagates_never_a_typed_error() -> None:
    with confirming(KeyError("a bug")) as confirming_, pytest.raises(KeyError):
        confirming_.widget()


# ── (4) every outcome is the confirm route's own ─────────────────────────────────────────────

OUTCOMES: Final = [
    pytest.param(EXECUTED, "allow", 200, "executed", id="executed"),
    pytest.param(FAILED, "allow", 409, "failed", id="failed"),
    pytest.param(ActionNotFound(), "allow", 404, "action_not_found", id="404"),
    pytest.param(ConfirmFailed(), "allow", 503, "confirm_failed", id="confirm_failed"),
    pytest.param(OutcomeUnknown(), "allow", 503, "outcome_unknown", id="outcome_unknown"),
    pytest.param(EXECUTED, "raise", 503, LIMITS_UNAVAILABLE, id="limits_unavailable"),
    pytest.param(EXECUTED, "refuse", 429, RATE_LIMITED, id="429"),
]


@pytest.mark.parametrize(("answer", "limit", "status", "code"), OUTCOMES)
def test_every_outcome_equals_the_confirm_routes_but_for_the_trace_id_and_no_store(
    answer: ConfirmResponse | BaseException, limit: Mode, status: int, code: str
) -> None:
    with confirming(answer, limit=limit) as confirming_:
        agent = confirming_.agent()
        widget = confirming_.widget()

    assert agent.status_code == widget.status_code == status
    assert widget.content == agent.content  # the body, byte for byte
    body = widget.json()
    assert body.get("error", body.get("status")) == code
    assert comparable(widget) == comparable(agent)
    assert widget.headers["cache-control"] == "no-store" and "cache-control" not in agent.headers
    assert len({agent.headers[TRACE_HEADER], widget.headers[TRACE_HEADER]}) == 2
    if status == 429:
        assert body["guidance"] == GUIDANCE  # a health business's 429 (G3), from one object
        assert widget.headers["retry-after"] == agent.headers["retry-after"]
    calls = confirming_.service.calls
    if limit == "allow":  # the same service object, asked the same thing by both routes
        [(b1, a1, r1, t1), (b2, a2, r2, t2)] = calls
        assert (b1, a1, r1) == (b2, a2, r2) == (BUSINESS, ACTION, r1)
        assert r1.customer_ref == CUSTOMER
        assert (t1, t2) == (agent.headers[TRACE_HEADER], widget.headers[TRACE_HEADER])
    else:  # the limits answered first, on both routes: the service never ran
        assert calls == []


def test_the_parity_comparison_sees_a_header_that_differs() -> None:
    # The positive control for `comparable`: one changed header (or a missing one) is a difference.
    plain = httpx2.Response(200, headers={"x-trace-id": "a" * 32, "retry-after": "30"})
    other = httpx2.Response(200, headers={"x-trace-id": "b" * 32, "retry-after": "31"})
    bare = httpx2.Response(200, headers={"x-trace-id": "c" * 32})

    assert comparable(plain) != comparable(other) and comparable(plain) != comparable(bare)
    assert comparable(plain) == [("retry-after", "30")]


# ── (5) the token in no log record and no statement ──────────────────────────────────────────


@pytest.mark.parametrize(("answer", "limit", "status", "code"), OUTCOMES)
def test_the_token_reaches_no_log_record_and_no_statement(
    answer: ConfirmResponse | BaseException,
    limit: Mode,
    status: int,
    code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = logging.getLogger()
    with confirming(answer, limit=limit) as confirming_:
        root.addHandler(caplog.handler)  # after `create_app`, which installs the app's handlers
        try:
            with caplog.at_level(logging.DEBUG):
                response = confirming_.widget()
        finally:
            root.removeHandler(caplog.handler)

    assert response.status_code == status
    assert caplog.records  # the access path and the limiter's `guard` record, at least
    for record in caplog.records:
        assert CONFIRM_TOKEN not in record.getMessage()
        assert CONFIRM_TOKEN not in json.dumps(vars(record), default=str)
    assert CONFIRM_TOKEN not in json.dumps(statements(confirming_), default=str)
    assert CONFIRM_TOKEN not in response.text


# ── (7) the guards off: the limiter's own 503, never the service ─────────────────────────────


def test_with_the_guards_off_widget_confirm_is_the_limiters_503_and_never_the_service() -> None:
    with confirming(guard_routes="off") as confirming_:
        assert confirming_.served.app.state.confirm_limits is None
        response = confirming_.widget()

    assert response.status_code == 503
    assert response.content == unavailable_response().body  # the body Redis down gives
    assert response.json()["error"] == LIMITS_UNAVAILABLE
    assert len(response.headers[TRACE_HEADER]) == 32
    assert response.headers["cache-control"] == "no-store"
    assert confirming_.service.calls == [] and confirming_.served.limiter.calls == []
    assert response.json()["detail"] != ACTION_NOT_FOUND
