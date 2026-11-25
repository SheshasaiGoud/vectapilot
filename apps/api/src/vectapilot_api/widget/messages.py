"""The widget's message and hand-off routes (M8 PR 2a; S1, S6, S8, S11, S14, S17, D6 (a), D15 (a),
ADR 0019 decisions 2 and 7).

Each route needs a customer session (`auth.dependencies.get_customer_session`): the business and
the customer (`customer_ref`, S4) come from the session row only, a session route refuses
`X-Business-Id` (400), every failure of authentication is the one 401, and a lookup that cannot
run is S14's typed 503. The session's business must still have an enabled widget, else the
config route's one 404 (`widget_not_found`) [proposal: disabling a widget stops its live
sessions too; the widget shows its cached guidance on any non-success, S16]. Every response
carries `Cache-Control: no-store` and every 422 echoes nothing (`auth.http.SessionRoute`).

**`POST /v1/widget/messages {message}`** → `{turn, handoff}`. `turn` is exactly `/v1/agent`'s
response, through **the same objects** (S6, M7 G13): the route takes `get_agent_service`,
`get_read_only_agent_service` and `get_agent_guard` from `agent.router`, so a test override wires
both routes identically, builds `AgentRequest` itself from the message and the session's
`customer_ref`, and calls the guard with the same arguments `/v1/agent` passes. A full rate
window is the guard's own 429, returned as built (S6's same mapping). There is **no unguarded
branch**: with `guard_routes = "off"` (local, test and ci only) there is no guard, and the route
answers a `guards_not_configured` hand-off (urgent with the guidance for a health business, as
`AgentGuard` answers a business without a guard folder) and never calls the service.
- **One outer bound** (D15 (a)): the guard's whole response within `widget_turn_bound_seconds`
  (at least the router's and the turn's own limits, so it only catches an overrun). Past it, a
  `turn_timeout` hand-off: urgent with the guidance for a health business, normal for any other,
  as the service's own `turn_timeout` [proposal for the second].
- **After the guarded response is built** (outside the measured services, S13): one transaction,
  bounded by `handoff_store_timeout_seconds` (S14) while Postgres answers (an unresponsive server
  holds the request until the connection fails, `db.py`'s known limit; the widget's client
  timeout and S16's cached guidance are the customer's control there), writes the turn's metadata
  row (D6 (a): kind, a hand-off's reason, priority and intent, an answer's outcome, trace id,
  latency; never text) and, for a hand-off, stores or raises the session's open hand-off (S17)
  with the message masked by S11's helper (`handoffs.at_rest`) and the guidance lines the
  customer was sent. `handoff.queued` is true only when that transaction committed, and
  `handoff.follow_up` only for a queued row that is neither urgent nor `safety` as stored (D11,
  `handoffs.store.invites_follow_up`). A failed or hung store is never a 5xx and never changes
  the turn: the customer gets the same response, guidance first, with `queued: false` (S8). The
  failure is logged as `widget_store_failed` with the error's class name and the trace id only.
- **The latency** the turn row records (`server_latency_ms`, noah's PR 2 review) runs from when
  the route starts (`request_started`, resolved before the session lookup, so the lookup is in
  it) to the response built: the store write (at most `handoff_store_timeout_seconds`) and the
  serialization are not in it, and a turn whose store failed has no row. It is not §6's "message
  received → reply sent", which M9's traces measure.
- A 429 writes no row: no turn ran, and a refused request must not cost a write [proposal]. A
  red-flag message skips the limiter (M7 G1), so its turn rows are bounded only per request;
  its hand-off rows are bounded by S17.
- The response carries `X-Trace-Id`, the same id as the turn's records and rows.

**`POST /v1/widget/handoffs`** ("talk to a person"): no body, no guard, no limiter and no model
call (S17). It stores a `customer_asked` hand-off (`origin = button`, normal, no message, no
guidance) or returns the session's open one, whatever made it, unchanged: idempotent per session.
`{id, queued: true, follow_up}` (the open row's D11, as the message route's), or `{id: null,
queued: false, follow_up: false}` when the store failed (never a 5xx: the widget then shows its
cached guidance and the contact fallback, S14, S16).

**`GET /v1/widget/handoffs/{handoff_id}`**: the session's own hand-off's status and reply
(D7 (a)), and `follow_up` from the row as it is now (D11: a raise to urgent or `safety` stops
the widget's polling). First the per-session Redis window (`RouteSlug.HANDOFF_STATUS`, S17),
failing closed: a full window is a 429 (with the guidance for a health business, as every 429),
Redis down a typed 503 `limits_unavailable`. Then the read: another session's hand-off, in the
same business or not, an unknown id and an expired one are one 404 (`handoff_not_found`); a read
that cannot run is a typed 503 (`handoff_store_unavailable`). Polling is never the emergency
path: the widget polls only while `follow_up` is true (S8, D11).

Nothing here logs a message, a token, a header or a masked text.
"""

import asyncio
import logging
import time
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.agent.responses import (
    AgentRequest,
    AnswerKindResponse,
    ConfirmationResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.router import (
    get_agent_guard,
    get_agent_service,
    get_read_only_agent_service,
)
from vectapilot_api.agent.service import AgentService
from vectapilot_api.auth.dependencies import NO_STORE, get_customer_session
from vectapilot_api.auth.http import SessionRoute
from vectapilot_api.auth.store import CustomerSession
from vectapilot_api.config import Settings
from vectapilot_api.guards.agent import AgentGuard, GuardedResponse
from vectapilot_api.guards.limits import GuardRefused, Limiter, RefusalKind, limited_response
from vectapilot_api.guards.types import DecidedBy, RouteSlug
from vectapilot_api.handoffs.at_rest import mask_at_rest
from vectapilot_api.handoffs.store import (
    HandoffStore,
    NewHandoff,
    Origin,
    PostgresHandoffStore,
    StoredHandoff,
    TurnKind,
    TurnRow,
)
from vectapilot_api.intents.policy import Priority
from vectapilot_api.kb.types import KbError
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import new_trace_id
from vectapilot_api.widget.config import WidgetBusiness, WidgetRegistry
from vectapilot_api.widget.models import (
    HandoffQueued,
    HandoffStateResponse,
    MessageRequest,
    MessageResponse,
)
from vectapilot_api.widget.router import get_limiter, get_widgets, widget_not_found

logger = logging.getLogger("vectapilot_api.widget")

router = APIRouter(prefix="/v1/widget", tags=["widget"], route_class=SessionRoute)

STORE_FAILED: Final = "widget_store_failed"
"""The warning a failed or hung store logs, with `reason` (the error's class name) and the trace
id only."""
TURN_BOUND: Final = "widget_turn_bound"
"""The warning D15's bound logs when it ends a turn, with the trace id only."""
HANDOFF_NOT_FOUND: Final = "there is no hand-off with this id for this session"
LIMITS_UNAVAILABLE: Final = "hand-off status cannot be checked now; try again later"
STORE_UNAVAILABLE: Final = "hand-offs cannot be read now; try again later"
NOT_QUEUED: Final = HandoffQueued(id=None, queued=False, follow_up=False)

Turn = ReplyResponse | ConfirmationResponse | HandoffResponse | AnswerKindResponse
"""One of `/v1/agent`'s four kinds (`agent.responses.AgentResponse`, unannotated)."""

_TYPED: Final = "a typed error: `{detail, error}`, `Cache-Control: no-store`"
_TRACE: Final = {
    TRACE_HEADER: {
        "description": "the turn's trace id, as in its records and its widget turn row",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
}
MESSAGE_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    200: {
        "description": "`/v1/agent`'s response and, for a hand-off, whether it was queued",
        "headers": _TRACE,
    },
    **{code: {"description": _TYPED} for code in (400, 401, 404, 422, 503)},
    429: {
        "description": "a full rate window (M7 G9, as `/v1/agent`): `{detail, error,"
        " retry_after_seconds, guidance}`, with `Retry-After`; never for a red-flag message",
        "headers": _TRACE,
    },
}
HANDOFF_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    code: {"description": _TYPED} for code in (400, 401, 404, 422, 503)
}
STATE_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    **HANDOFF_RESPONSES,
    429: {
        "description": "the session's status window is full: `{detail, error,"
        " retry_after_seconds, guidance}` with `Retry-After`"
    },
}


async def request_started() -> float:
    """The clock the turn row's latency starts from (module docstring): declared before the
    session, so FastAPI resolves it first. Async, so it never waits for the thread pool."""
    return time.perf_counter()


def queued_as(stored: StoredHandoff) -> HandoffQueued:
    """The session's open hand-off as stored: queued, and D11's `follow_up` from its row."""
    return HandoffQueued(id=stored.id, queued=True, follow_up=stored.follow_up)


def get_handoff_store(request: Request) -> HandoffStore:
    """The hand-off store over the app's database (a dependency, so a test can replace it)."""
    return PostgresHandoffStore(request.app.state.database)


def _error(status_code: int, code: str, message: str) -> KbError:
    return KbError(status_code, code, message, headers=NO_STORE)


def enabled_widget(widgets: WidgetRegistry, session: CustomerSession) -> WidgetBusiness:
    """The session's business's widget, while it is enabled (module docstring)."""
    widget = widgets.enabled(session.business_id)
    if widget is None:
        raise widget_not_found()
    return widget


def widget_handoff(
    widget: WidgetBusiness, trace_id: str, reason: HandoffReason, priority: Priority
) -> HandoffResponse:
    """A hand-off the route makes itself, guidance as `AgentGuard._handoff` shows it: for a
    health business or an urgent hand-off."""
    shown = widget.health or priority is Priority.URGENT
    return HandoffResponse(
        trace_id=trace_id,
        reason=reason,
        priority=priority,
        guidance=widget.guidance if shown else (),
    )


def guards_not_configured(widget: WidgetBusiness, trace_id: str) -> HandoffResponse:
    """S6: `guard_routes = "off"` has no unguarded branch here (module docstring)."""
    priority = Priority.URGENT if widget.health else Priority.HIGH
    return widget_handoff(widget, trace_id, HandoffReason.GUARDS_NOT_CONFIGURED, priority)


def turn_timeout(widget: WidgetBusiness, trace_id: str) -> HandoffResponse:
    """D15 (a): the outer bound ended the turn (module docstring)."""
    priority = Priority.URGENT if widget.health else Priority.NORMAL
    return widget_handoff(widget, trace_id, HandoffReason.TURN_TIMEOUT, priority)


async def bounded_turn(
    guard: AgentGuard,
    session: CustomerSession,
    request: AgentRequest,
    *,
    trace_id: str,
    service: AgentService,
    read_only: AgentService | None,
    bound_s: float,
) -> GuardedResponse | None:
    """The guard's response within `bound_s`, or None when the bound ended it (D15 (a)). The guard
    itself never raises but a cancel; a `TimeoutError` that is not this bound's propagates."""
    scope = asyncio.timeout(bound_s)
    try:
        async with scope:
            return await guard.respond(
                session.business_id,
                request,
                trace_id=trace_id,
                service=service,
                read_only=read_only,
            )
    except TimeoutError:
        if not scope.expired():
            raise
    logger.warning(TURN_BOUND, extra={"trace_id": trace_id})
    return None


def turn_row(session: CustomerSession, turn: Turn, latency_ms: int) -> TurnRow:
    """The turn's metadata row (D6 (a)): its kind, a hand-off's reason, priority and intent, and an
    answer's outcome, under the trace id the customer was sent in the body (the one a rating
    names, PR 2b; the guard echoes the route's, so it is also the `X-Trace-Id` header's)."""
    handoff = turn if isinstance(turn, HandoffResponse) else None
    answer = turn if isinstance(turn, AnswerKindResponse) else None
    return TurnRow(
        business_id=session.business_id,
        session_id=session.session_id,
        trace_id=turn.trace_id,
        kind=TurnKind(turn.kind),
        reason=None if handoff is None else handoff.reason,
        priority=None if handoff is None else handoff.priority,
        intent=None if handoff is None else handoff.intent,
        answer_outcome=None if answer is None else answer.answer.outcome,
        ui_language=session.ui_language,
        server_latency_ms=latency_ms,
    )


def turn_handoff(
    session: CustomerSession, widget: WidgetBusiness, turn: HandoffResponse, message: str
) -> NewHandoff:
    """The session's hand-off for a turn that handed off: S11's masked message, the guidance
    lines exactly as sent (S8), the reason, priority and intent of the response."""
    return NewHandoff(
        business_id=session.business_id,
        session_id=session.session_id,
        trace_id=turn.trace_id,
        origin=Origin.TURN,
        reason=turn.reason,
        priority=turn.priority,
        intent=turn.intent,
        ui_language=session.ui_language,
        guidance_shown=turn.guidance,
        message_masked=mask_at_rest(message),
        retention_days=widget.file.handoff_retention_days,
    )


def store_failed(error: Exception, trace_id: str) -> None:
    logger.warning(STORE_FAILED, extra={"reason": type(error).__name__, "trace_id": trace_id})


@router.post("/messages", response_model=MessageResponse, responses=MESSAGE_RESPONSES)
async def post_message(
    started: Annotated[float, Depends(request_started)],  # first: before the session lookup
    session: Annotated[CustomerSession, Depends(get_customer_session)],
    body: MessageRequest,
    request: Request,
    response: Response,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    read_only: Annotated[AgentService | None, Depends(get_read_only_agent_service)],
    guard: Annotated[AgentGuard | None, Depends(get_agent_guard)],
    store: Annotated[HandoffStore, Depends(get_handoff_store)],
) -> MessageResponse | JSONResponse:
    """One customer message, guarded exactly as `/v1/agent`'s (module docstring)."""
    widget = enabled_widget(widgets, session)
    settings: Settings = request.app.state.settings
    trace_id = new_trace_id()
    turn: GuardedResponse | None
    if guard is None:  # guard_routes = "off": a hand-off, never the service (S6)
        turn = guards_not_configured(widget, trace_id)
    else:
        agent_request = AgentRequest(message=body.message, customer_ref=session.customer_ref)
        turn = await bounded_turn(
            guard,
            session,
            agent_request,
            trace_id=trace_id,
            service=service,
            read_only=read_only,
            bound_s=settings.widget_turn_bound_seconds,
        )
        if turn is None:
            turn = turn_timeout(widget, trace_id)
    if isinstance(turn, Response):  # the guard's 429, returned as built (S6)
        turn.headers[TRACE_HEADER] = trace_id
        return turn
    latency_ms = round((time.perf_counter() - started) * 1000)
    handoff = (
        turn_handoff(session, widget, turn, body.message)
        if isinstance(turn, HandoffResponse)
        else None
    )
    stored: StoredHandoff | None = None
    try:
        async with asyncio.timeout(settings.handoff_store_timeout_seconds):
            stored = await store.record_turn(turn_row(session, turn, latency_ms), handoff)
    except Exception as error:  # S8, S14: never a 5xx; a cancel is not an Exception
        store_failed(error, trace_id)
    response.headers[TRACE_HEADER] = trace_id
    queued = None
    if handoff is not None:
        queued = NOT_QUEUED if stored is None else queued_as(stored)
    return MessageResponse(turn=turn, handoff=queued)


@router.post("/handoffs", response_model=HandoffQueued, responses=HANDOFF_RESPONSES)
async def talk_to_a_person(
    session: Annotated[CustomerSession, Depends(get_customer_session)],
    request: Request,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    store: Annotated[HandoffStore, Depends(get_handoff_store)],
) -> HandoffQueued:
    """ "Talk to a person": the session's open hand-off, made now if there is none (S17)."""
    widget = enabled_widget(widgets, session)
    settings: Settings = request.app.state.settings
    trace_id = new_trace_id()
    pressed = NewHandoff(
        business_id=session.business_id,
        session_id=session.session_id,
        trace_id=trace_id,
        origin=Origin.BUTTON,
        reason=HandoffReason.CUSTOMER_ASKED,
        priority=Priority.NORMAL,
        intent=None,
        ui_language=session.ui_language,
        guidance_shown=(),
        message_masked=None,
        retention_days=widget.file.handoff_retention_days,
    )
    try:
        async with asyncio.timeout(settings.handoff_store_timeout_seconds):
            stored = await store.press_button(pressed)
    except Exception as error:  # S14: never a 5xx; a cancel is not an Exception
        store_failed(error, trace_id)
        return NOT_QUEUED
    return queued_as(stored)


@router.get(
    "/handoffs/{handoff_id}", response_model=HandoffStateResponse, responses=STATE_RESPONSES
)
async def handoff_state(
    handoff_id: UUID,
    session: Annotated[CustomerSession, Depends(get_customer_session)],
    request: Request,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    limiter: Annotated[Limiter, Depends(get_limiter)],
    store: Annotated[HandoffStore, Depends(get_handoff_store)],
) -> HandoffStateResponse | JSONResponse:
    """The session's own hand-off's status and reply (module docstring)."""
    widget = enabled_widget(widgets, session)
    try:
        await limiter.admit(session.business_id, RouteSlug.HANDOFF_STATUS, session.customer_ref)
    except GuardRefused as refused:
        if refused.kind is RefusalKind.LIMIT:
            return limited_response(refused, widget.guidance if widget.health else ())
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            DecidedBy.LIMITS_UNAVAILABLE.value,
            LIMITS_UNAVAILABLE,
        ) from None
    settings: Settings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.handoff_store_timeout_seconds):
            found = await store.state(session.business_id, session.session_id, handoff_id)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "handoff_store_unavailable", STORE_UNAVAILABLE
        )
        logger.warning(STORE_FAILED, extra={"reason": type(error).__name__})
    else:
        if found is None:
            raise _error(status.HTTP_404_NOT_FOUND, "handoff_not_found", HANDOFF_NOT_FOUND)
        return HandoffStateResponse(
            id=found.id,
            status=found.status,
            reply=found.reply,
            replied_at=found.replied_at,
            follow_up=found.follow_up,
        )
    raise failure  # outside the `except`: nothing (and no message) is chained to it
