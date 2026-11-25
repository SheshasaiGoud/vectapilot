"""`POST /v1/widget/messages` without a database or Redis (M8 PR 2a; S1, S6, S8, S11, S14, S17,
D6 (a), D15 (a)): the real app with the guards on, the sample businesses' folders, `FakeLimiter`,
`ScriptedDatabase` for the session lookup and the hand-off store's statements, and `LookupMatcher`
standing in for H1 (`tests/widget/guarded.py`). Against Postgres:
`tests/canary/test_widget_routes.py`.

Both sample businesses are health types with `agent_router` off, so every message the real
pipeline serves is a hand-off (an emergency: `safety`; anything else: W11's `safety_floor_off`).
The other kinds, the outer bound and the guard's absence use a stub guard through the same
dependency function the route shares with `/v1/agent` (S6).
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Final

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from tests.auth.fakes import BUSINESS, SESSION_ID, TOKEN, ScriptedDatabase, customer_row
from tests.guards.agent_helpers import LookupMatcher
from tests.widget.guarded import (
    BEARER,
    BENIGN,
    HANDOFF_ID,
    RAT_POISON,
    GuardedServed,
    RecordingHandoffStore,
    serve_guarded,
    stored_row,
)
from tests.widget.helpers import guidance_of
from vectapilot_api.actions.tokens import TOKEN_LENGTH, new_confirm_token
from vectapilot_api.agent.responses import (
    AgentRequest,
    ConfirmationResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.router import get_agent_guard, get_agent_service
from vectapilot_api.auth.dependencies import SESSION_STORE_UNAVAILABLE, get_customer_session
from vectapilot_api.auth.http import UNKNOWN_KEY
from vectapilot_api.auth.store import CustomerSession, customer_ref_for
from vectapilot_api.guards.agent import GuardedResponse
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import RATE_LIMITED
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.handoffs.at_rest import mask_at_rest
from vectapilot_api.handoffs.store import (
    INSERT_TURN,
    STORE_HANDOFF,
    NewHandoff,
    StoredHandoff,
    TurnKind,
    TurnRow,
)
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.messages import (
    STORE_FAILED,
    TURN_BOUND,
    bounded_turn,
    guards_not_configured,
    turn_timeout,
)
from vectapilot_api.widget.router import WIDGET_NOT_FOUND

GUIDANCE: Final = guidance_of(BUSINESS)
SYNTHETIC_PHONE: Final = "+91 00000 20777"
"""Synthetic (the log canary's), in a shape H2's Indian phone pattern masks."""
EXPIRES: Final = "2026-11-25T06:00:00Z"


@pytest.fixture
def served() -> Iterator[GuardedServed]:
    with serve_guarded() as app:
        yield app


@contextmanager
def captured(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            yield
    finally:
        root.removeHandler(caplog.handler)


def statements(database: Any) -> list[tuple[str, dict[str, Any]]]:
    return list(database.connection.executed)


def store_params(database: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The hand-off statement's and the turn row's parameters, in that order (one transaction)."""
    executed = statements(database)
    assert [text for text, _ in executed[1:]] == [STORE_HANDOFF.text, INSERT_TURN.text]
    return executed[1][1], executed[2][1]


class StubGuard:
    """A guard that answers `response` (or waits forever), recording what it was asked. Only
    reachable through `get_agent_guard`, the dependency `/v1/agent` shares (S6)."""

    def __init__(self, response: GuardedResponse | None = None) -> None:
        self.response = response
        self.asked: list[tuple[str, AgentRequest, str]] = []

    async def respond(
        self,
        business_id: str,
        request: AgentRequest,
        *,
        trace_id: str,
        service: object,
        read_only: object,
    ) -> GuardedResponse:
        self.asked.append((business_id, request, trace_id))
        if self.response is None:
            await asyncio.Event().wait()
        assert self.response is not None
        return self.response


def stub(served: GuardedServed, response: GuardedResponse | None) -> StubGuard:
    guard = StubGuard(response)
    served.app.dependency_overrides[get_agent_guard] = lambda: guard
    return guard


# ── S1: identity from the session, a closed body ─────────────────────────────────────────────


def test_an_emergency_hands_off_urgent_with_guidance_and_the_hand_off_is_queued(
    served: GuardedServed,
) -> None:
    database = served.script([stored_row()], [])

    response = served.send(RAT_POISON)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    turn = body["turn"]
    assert (turn["kind"], turn["reason"], turn["priority"]) == ("handoff", "safety", "urgent")
    assert turn["guidance"] == GUIDANCE  # S8: first, verbatim, from the guard file
    # The row exists; urgent and `safety`, so never "a person will reply" (D11).
    assert body["handoff"] == {"id": str(HANDOFF_ID), "queued": True, "follow_up": False}
    assert response.headers[TRACE_HEADER] == turn["trace_id"]
    assert served.limiter.calls == []  # the red-flag screen first (M7 G1): no Redis
    # S1: the business and the customer came from the session, in one business transaction each.
    assert database.opened == [BUSINESS, BUSINESS]
    handoff, row = store_params(database)
    assert handoff == {
        "business_id": BUSINESS,
        "session_id": SESSION_ID,
        "trace_id": turn["trace_id"],
        "origin": "turn",
        "reason": "safety",
        "priority": "urgent",
        "intent": None,
        "channel": "web",
        "ui_language": "en",
        "guidance_shown": GUIDANCE,  # the lines sent (S8), never re-read later
        "message_masked": mask_at_rest(RAT_POISON),  # S11's helper
        "retention_days": 30,  # P5's default, from the widget file
    }
    assert row["trace_id"] == turn["trace_id"] and row["kind"] == "handoff"
    assert (row["reason"], row["priority"], row["session_id"]) == ("safety", "urgent", SESSION_ID)
    assert (row["intent"], row["answer_outcome"]) == (None, None)
    assert isinstance(row["server_latency_ms"], int) and row["server_latency_ms"] >= 0


LOOKUP_S: Final = 0.2
TURN_S: Final = 0.2
STORE_S: Final = 1.0
"""noah's PR 2 review: the session lookup, the guarded turn and the store each take a known time
(floors: `asyncio.sleep` never returns early), far apart so the bounds below cannot overlap."""


class SlowStore(RecordingHandoffStore):
    async def record_turn(self, turn: TurnRow, handoff: NewHandoff | None) -> StoredHandoff | None:
        await asyncio.sleep(STORE_S)
        return await super().record_turn(turn, handoff)


class SlowGuard(StubGuard):
    async def respond(
        self,
        business_id: str,
        request: AgentRequest,
        *,
        trace_id: str,
        service: object,
        read_only: object,
    ) -> GuardedResponse:
        await asyncio.sleep(TURN_S)
        return ReplyResponse(trace_id=trace_id, text="Hello.")


def test_the_turn_rows_latency_runs_from_before_the_lookup_to_the_response_built(
    served: GuardedServed,
) -> None:
    # noah's PR 2 review: the clock starts before the session lookup (so a slow lookup counts) and
    # stops before the store write (which the row cannot hold). Lower bound: lookup + turn; upper:
    # below lookup + turn + store.
    async def slow_lookup() -> CustomerSession:
        await asyncio.sleep(LOOKUP_S)
        return CustomerSession(BUSINESS, SESSION_ID, customer_ref_for(SESSION_ID), "en")

    store = SlowStore()
    served.use_store(store)
    served.app.dependency_overrides[get_agent_guard] = lambda: SlowGuard()
    served.app.dependency_overrides[get_customer_session] = slow_lookup

    response = served.send(BENIGN)

    assert response.status_code == 200, response.text
    [row] = store.turns
    assert (
        (LOOKUP_S + TURN_S) * 1000 <= row.server_latency_ms < (LOOKUP_S + TURN_S + STORE_S) * 1000
    )


def test_the_service_gets_the_sessions_customer_never_one_from_the_client(
    served: GuardedServed,
) -> None:
    guard = stub(served, ReplyResponse(trace_id="a" * 32, text="Hello."))
    served.script([])

    served.send(BENIGN)

    [(business_id, request, _)] = guard.asked
    assert business_id == BUSINESS
    assert request == AgentRequest(message=BENIGN, customer_ref=f"s-{SESSION_ID.hex}")


@pytest.mark.parametrize(
    "body",
    [
        {"message": BENIGN, "customer_ref": "zqxmarker"},
        {"message": BENIGN, "business_id": "sunrise-clinic"},
        {"message": BENIGN, "session_id": "zqxmarker"},
        {"message": "x" * (MAX_QUESTION_CHARS + 1)},
        {"message": "   "},
        {},
    ],
    ids=["customer_ref", "business_id", "session_id", "over length", "blank", "no message"],
)
def test_a_body_naming_anyone_or_not_a_message_is_a_closed_422_that_runs_nothing(
    served: GuardedServed, body: dict[str, Any]
) -> None:
    guard = stub(served, None)
    database = served.script()

    response = served.client.post(
        "/v1/widget/messages", json=body, headers={"authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "zqxmarker" not in response.text and "sunrise-clinic" not in response.text
    assert all(
        UNKNOWN_KEY in p["loc"] or p["type"] != "extra_forbidden" for p in problems(response)
    )
    assert guard.asked == []
    assert len(statements(database)) == 1  # the session lookup, then nothing


def problems(response: Any) -> list[dict[str, Any]]:
    found = response.json().get("problems", [])
    assert isinstance(found, list)
    return found


def test_the_business_header_and_a_missing_session_are_refused_before_any_turn(
    served: GuardedServed,
) -> None:
    guard = stub(served, None)
    database = served.script()

    header = served.send(BENIGN, **{"X-Business-Id": BUSINESS})
    anonymous = served.client.post("/v1/widget/messages", json={"message": BENIGN})

    assert (header.status_code, header.json()["error"]) == (400, "business_header_refused")
    assert (anonymous.status_code, anonymous.json()["error"]) == (401, "unauthorized")
    assert guard.asked == [] and statements(database) == []


def test_a_session_whose_widget_is_no_longer_enabled_gets_the_config_routes_one_404(
    served: GuardedServed,
) -> None:
    guard = stub(served, None)
    served.app.state.widgets = WidgetRegistry.empty()
    served.script()

    response = served.send(RAT_POISON)

    assert (response.status_code, response.json()) == (
        404,
        {"detail": WIDGET_NOT_FOUND, "error": "widget_not_found"},
    )
    assert guard.asked == []


@pytest.mark.parametrize("hang", [False, True], ids=["down", "hung"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/widget/messages"),
        ("POST", "/v1/widget/handoffs"),
        ("GET", f"/v1/widget/handoffs/{HANDOFF_ID}"),
    ],
    ids=["message", "talk to a person", "status"],
)
def test_with_postgres_down_every_route_is_s14s_typed_503_and_runs_nothing(
    served: GuardedServed, method: str, path: str, hang: bool
) -> None:
    # S14: the session lookup cannot run (down, or hung past its own timeout): a typed 503, never
    # a 500, before any guard, limiter, store or model.
    guard = stub(served, None)
    store = RecordingHandoffStore()
    served.use_store(store)
    served.app.state.database = (
        ScriptedDatabase(hang=True)
        if hang
        else ScriptedDatabase(fail=OperationalError("SELECT ...", {}, Exception()))
    )

    body = {"message": RAT_POISON} if path.endswith("messages") else None
    response = served.client.request(method, path, json=body, headers=BEARER)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": SESSION_STORE_UNAVAILABLE, "error": "session_store_unavailable"},
    )
    assert response.headers["cache-control"] == "no-store"
    assert guard.asked == [] and served.limiter.calls == [] and served.provider.calls == []
    assert (store.turns, store.written, store.presses) == ([], [], [])


# ── S6: the same guard, the same 429, no unguarded branch ────────────────────────────────────


def test_a_full_window_is_the_guards_own_429_with_guidance_and_writes_no_row() -> None:
    with serve_guarded(limiter=FakeLimiter(limit="refuse")) as served:
        database = served.script()
        response = served.send(BENIGN)

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == RATE_LIMITED and body["guidance"] == GUIDANCE  # a health business
    assert response.headers["retry-after"] == "30"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers[TRACE_HEADER]) == 32
    # Per session (S4: the session's customer_ref), with no change to the limiter.
    assert served.limiter.calls[0] == LimiterCall("admit", BUSINESS, RouteSlug.AGENT, True)
    assert len(statements(database)) == 1  # no turn ran: no row [proposal]


def test_with_redis_down_an_emergency_on_a_session_is_still_urgent_guided_and_queued() -> None:
    # S14: a message on an existing session needs the API and Postgres only.
    with serve_guarded(limiter=FakeLimiter(limit="raise", budget="raise")) as served:
        served.script([stored_row()], [])
        body = served.send(RAT_POISON).json()

    assert (body["turn"]["reason"], body["turn"]["priority"]) == ("safety", "urgent")
    assert body["turn"]["guidance"] == GUIDANCE
    assert body["handoff"]["queued"] is True


def test_a_message_the_screen_clears_reaches_w11_through_the_same_pipeline() -> None:
    with serve_guarded() as served:
        database = served.script([stored_row("normal")], [])
        body = served.send(BENIGN).json()

    assert (body["turn"]["reason"], body["turn"]["priority"]) == ("safety_floor_off", "normal")
    assert body["turn"]["guidance"] == GUIDANCE
    handoff, _ = store_params(database)
    assert handoff["reason"] == "safety_floor_off" and handoff["guidance_shown"] == GUIDANCE
    assert [call.operation for call in served.limiter.calls] == ["admit", "remaining"]


@pytest.mark.parametrize(
    ("business", "health", "priority", "guided"),
    [("paws-and-co", True, "urgent", True), ("rt-shop", False, "high", False)],
)
def test_with_the_guards_off_a_message_is_a_guards_not_configured_hand_off_never_the_service(
    business: str, health: bool, priority: str, guided: bool
) -> None:
    # S6: no `if guard is None` branch to the service; tested under guard_routes = "off".
    class NoService:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError("the service must not run with the guards off")

    with serve_guarded(guard_routes="off") as served:
        assert served.app.state.agent_guard is None
        served.app.dependency_overrides[get_agent_service] = NoService
        widget = served.app.state.widgets.enabled("paws-and-co")
        served.app.state.widgets = WidgetRegistry(
            {business: replace(widget, health=health, file=widget.file)}
        )
        row = customer_row(business_id=business)
        served.script([stored_row(priority)], [])
        served.app.state.database.connection.script[0] = [row]
        response = served.send(RAT_POISON)

    body = response.json()
    assert response.status_code == 200, response.text
    assert (body["turn"]["reason"], body["turn"]["priority"]) == ("guards_not_configured", priority)
    assert body["turn"]["guidance"] == (GUIDANCE if guided else [])
    assert body["handoff"]["queued"] is True
    assert served.provider.calls == []


def test_the_rows_carry_the_turns_the_sessions_and_the_widgets_values(
    served: GuardedServed,
) -> None:
    # Rule 9, S17 (mutation probe, PR 2): retention from the widget file, the language from the
    # session, and the hand-off's reason, priority and intent from the turn: nothing defaulted.
    widget = served.app.state.widgets.enabled(BUSINESS)
    file = widget.file.model_copy(update={"handoff_retention_days": 7})
    served.app.state.widgets = WidgetRegistry({BUSINESS: replace(widget, file=file)})
    stub(
        served,
        HandoffResponse(
            trace_id="e" * 32,
            reason=HandoffReason.OTHER,
            priority=Priority.NORMAL,
            intent=Intent.EMERGENCY,
            guidance=(),
        ),
    )
    database = served.script([stored_row("normal")], [])
    database.connection.script[0] = [customer_row(ui_language="te")]

    served.send(BENIGN)

    handoff, row = store_params(database)
    assert handoff["retention_days"] == 7
    assert (handoff["ui_language"], row["ui_language"]) == ("te", "te")
    assert (handoff["reason"], handoff["priority"], handoff["intent"]) == (
        "other",
        "normal",
        "emergency",
    )
    assert (row["reason"], row["priority"]) == ("other", "normal")


def test_the_stored_guidance_is_the_lines_sent_never_the_widgets(served: GuardedServed) -> None:
    # S8 (mutation probe, PR 2): `guidance_shown` holds the lines the customer was sent, never
    # the widget's guidance re-read.
    assert GUIDANCE  # the positive control: the widget has guidance a re-read would store
    turn = HandoffResponse(
        trace_id="d" * 32, reason=HandoffReason.OTHER, priority=Priority.HIGH, guidance=()
    )
    stub(served, turn)
    database = served.script([stored_row("high")], [])

    body = served.send(BENIGN).json()

    assert body["turn"]["guidance"] == []
    handoff, _ = store_params(database)
    assert handoff["guidance_shown"] == []


# ── S8, S14: honest hand-offs and a store that fails ─────────────────────────────────────────


def test_a_failed_store_keeps_the_guidance_gives_queued_false_and_is_never_a_5xx(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The driver's error holds a parameter (the worst a bug can do); only its class is logged.
    message = f"{RAT_POISON} Call me on {SYNTHETIC_PHONE}"
    leak = OperationalError("INSERT ...", {"message_masked": SYNTHETIC_PHONE}, Exception())
    with serve_guarded(matcher=LookupMatcher({message: {"vet_rat_poison"}})) as served:
        served.script(leak)
        with captured(caplog):  # after `create_app`, which installs the app's own handlers
            response = served.send(message)

    assert response.status_code == 200
    body = response.json()
    # Never "a person will reply".
    assert body["handoff"] == {"id": None, "queued": False, "follow_up": False}
    assert (body["turn"]["reason"], body["turn"]["guidance"]) == ("safety", GUIDANCE)
    [record] = [r for r in caplog.records if r.getMessage() == STORE_FAILED]
    assert getattr(record, "reason") == "OperationalError"  # noqa: B009
    assert getattr(record, "trace_id") == body["turn"]["trace_id"]  # noqa: B009
    assert SYNTHETIC_PHONE not in json.dumps(vars(record), default=str)


def test_a_hung_store_ends_at_its_own_timeout_with_the_response_unchanged() -> None:
    store = RecordingHandoffStore(hang=True)
    with serve_guarded(handoff_store_timeout_seconds=0.05) as served:
        served.use_store(store)
        served.script()
        response = served.send(RAT_POISON)

    body = response.json()
    assert response.status_code == 200
    assert (body["turn"]["reason"], body["turn"]["priority"]) == ("safety", "urgent")
    assert body["turn"]["guidance"] == GUIDANCE
    assert body["handoff"] == {"id": None, "queued": False, "follow_up": False}


def test_queued_is_true_only_when_the_store_returned_the_row(served: GuardedServed) -> None:
    store = RecordingHandoffStore()
    served.use_store(store)
    served.script([customer_row()])  # the second message's session lookup

    first = served.send(RAT_POISON).json()["handoff"]
    second = served.send(RAT_POISON).json()["handoff"]

    assert first["queued"] and first == second  # S17: one row, the same id
    [only] = store.open.values()
    assert str(only.stored.id) == first["id"] and only.stored.count == 2


# ── D15 (a): one outer bound ─────────────────────────────────────────────────────────────────


def bound(served: GuardedServed, seconds: float) -> None:
    settings = served.app.state.settings
    served.app.state.settings = settings.model_copy(update={"widget_turn_bound_seconds": seconds})


def test_a_turn_past_the_outer_bound_is_an_urgent_turn_timeout_with_guidance_and_queued(
    served: GuardedServed, caplog: pytest.LogCaptureFixture
) -> None:
    stub(served, None)  # never answers
    bound(served, 0.05)
    database = served.script([stored_row()], [])

    with caplog.at_level(logging.WARNING, logger="vectapilot_api.widget"):
        body = served.send(BENIGN).json()

    turn = body["turn"]
    assert (turn["reason"], turn["priority"], turn["guidance"]) == (
        "turn_timeout",
        "urgent",
        GUIDANCE,
    )
    assert body["handoff"]["queued"] is True
    handoff, _ = store_params(database)
    assert handoff["reason"] == "turn_timeout"
    assert [r.getMessage() for r in caplog.records].count(TURN_BOUND) == 1


def test_the_widgets_own_hand_offs_follow_the_guards_guidance_rule(served: GuardedServed) -> None:
    health = served.app.state.widgets.enabled(BUSINESS)
    other = replace(health, health=False)

    assert (turn_timeout(health, "a" * 32).priority, turn_timeout(other, "a" * 32).priority) == (
        Priority.URGENT,
        Priority.NORMAL,
    )
    assert turn_timeout(other, "a" * 32).guidance == ()
    assert guards_not_configured(other, "a" * 32).guidance == ()  # high, not health: none
    assert guards_not_configured(health, "a" * 32).guidance == tuple(GUIDANCE)


@pytest.mark.anyio
async def test_a_timeout_that_is_not_the_bounds_own_propagates() -> None:
    class Raising(StubGuard):
        async def respond(self, *args: Any, **kwargs: Any) -> GuardedResponse:
            raise TimeoutError("raised inside, not the bound")

    session = CustomerSession(BUSINESS, SESSION_ID, f"s-{SESSION_ID.hex}", "en")
    request = AgentRequest(message=BENIGN, customer_ref=session.customer_ref)

    with pytest.raises(TimeoutError, match="raised inside"):
        await bounded_turn(
            Raising(),  # type: ignore[arg-type]
            session,
            request,
            trace_id="a" * 32,
            service=None,  # type: ignore[arg-type]
            read_only=None,
            bound_s=5.0,
        )


# ── Every kind passes through unchanged; the confirm token is revealed once ──────────────────


def confirmation(token: str) -> ConfirmationResponse:
    from uuid import UUID

    return ConfirmationResponse.model_validate(
        {
            "trace_id": "b" * 32,
            "action_id": UUID(int=5),
            "tool": "book",
            "summary": "Book a TEST-ONLY slot.",
            "expires_at": EXPIRES,
            "confirm_token": token,
        }
    )


def test_a_confirmation_carries_its_token_once_and_no_row_or_log_holds_it(
    served: GuardedServed, caplog: pytest.LogCaptureFixture
) -> None:
    # Leo's (1) and (5): ADR 0016 W2's second route. FastAPI validates the response model and
    # serializes in JSON mode, the one place `ConfirmationResponse` reveals its token.
    token = new_confirm_token().get_secret_value()
    stub(served, confirmation(token))
    database = served.script([])

    with caplog.at_level(logging.DEBUG):
        response = served.send(BENIGN)

    body = response.json()
    assert response.status_code == 200
    assert body["turn"]["kind"] == "confirmation_required"
    assert body["turn"]["confirm_token"] == token and len(token) == TOKEN_LENGTH == 22
    assert "*" not in body["turn"]["confirm_token"]
    assert body["handoff"] is None
    [(text, row)] = statements(database)[1:]
    assert text == INSERT_TURN.text
    assert (row["kind"], row["reason"], row["priority"]) == ("confirmation_required", None, None)
    assert token not in json.dumps(statements(database), default=str)
    assert token not in caplog.text


@pytest.mark.parametrize(
    "turn",
    [
        ReplyResponse(trace_id="c" * 32, text="We open at nine."),
        HandoffResponse(
            trace_id="d" * 32, reason=HandoffReason.OTHER, priority=Priority.HIGH, guidance=()
        ),
    ],
    ids=["reply", "handoff"],
)
def test_the_turn_is_the_guards_response_byte_for_byte(
    served: GuardedServed, turn: ReplyResponse | HandoffResponse
) -> None:
    stub(served, turn)
    if turn.kind == "handoff":
        database = served.script([stored_row("high")], [])
    else:
        database = served.script([])

    body = served.send(BENIGN).json()

    assert body["turn"] == turn.model_dump(mode="json")
    assert (body["handoff"] is None) == (turn.kind == "reply")
    # The turn row (and a hand-off's row) carry the trace id the body carries: the one a rating
    # will name (PR 2b). This stub answers its own id, unlike the real guard, which echoes the
    # route's: the row follows the body.
    [(text, row)] = statements(database)[-1:]
    assert (text, row["trace_id"]) == (INSERT_TURN.text, turn.trace_id)
    if turn.kind == "handoff":
        assert statements(database)[1][1]["trace_id"] == turn.trace_id


def test_the_route_returns_a_429_json_response_from_the_guard_untouched_but_for_headers(
    served: GuardedServed,
) -> None:
    limited = JSONResponse({"detail": "x", "error": RATE_LIMITED}, status_code=429)
    stub(served, limited)
    served.script()

    response = served.send(BENIGN)

    assert (response.status_code, response.json()) == (429, {"detail": "x", "error": RATE_LIMITED})
    assert response.headers["cache-control"] == "no-store"


def test_the_turn_kind_enum_is_the_agent_routes_four_kinds() -> None:
    kinds = {
        model.model_fields["kind"].default
        for model in (ReplyResponse, ConfirmationResponse, HandoffResponse)
    } | {"answer"}

    assert {kind.value for kind in TurnKind} == kinds
