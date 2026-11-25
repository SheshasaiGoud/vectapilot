"""`POST /v1/widget/feedback {trace_id, rating}` (M8 PR 2b; D6 (a), S1, S9; cut 4): a customer's
thumbs up or down on one of this session's own turns.

Feedback is bound to turns (D6 (a)): a rating names a turn by the trace id its body carried, the
one its `widget_turns` row holds (`widget.messages`), and only the rating session's own turn can
be rated, once. That gives §6's thumbs-up rate its denominator ("rated n of m turns") and keeps
any session from rating another's turns.

**Order of checks:** a body FastAPI cannot decode (before anything runs), the session (400 for
`X-Business-Id`, the one 401, S14's typed 503), the body (closed, `extra="forbid"`: a body naming
`customer_ref`, `session_id`, `business_id` or anything else is a 422 that echoes nothing), the
session's widget (the config route's one 404 while it is disabled [proposal, as PR 2a's routes]),
then the per-session window in Redis (`RouteSlug.FEEDBACK`, the default per-business and
per-customer windows under its own slug [proposal]), failing closed: a full window is a 429 (with
the guidance for a health business, as every 429), Redis down a typed 503 `limits_unavailable`.
Feedback is never the emergency path.

**Answers** (`handoffs.store.rate_turn`, one transaction bounded by
`handoff_store_timeout_seconds`):
- 201 `{trace_id, rating}`: the rating is stored [proposal: the body as sent];
- 404 `turn_not_found`, one answer for another session's turn (in the same business or not) and a
  made-up trace id;
- 409 `already_rated`: the turn has its one rating (D6 (a)); a second, the same or the other, is
  refused, never an update;
- 503 `feedback_store_unavailable`: the statement could not run (Postgres down or slower than its
  bound); logged as `widget_store_failed` with the error's class name only.

No text is read or stored, and nothing here logs a token, a header or a trace id the client sent.
"""

import asyncio
import logging
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.auth.dependencies import NO_STORE, get_customer_session
from vectapilot_api.auth.http import SessionRoute
from vectapilot_api.auth.store import CustomerSession
from vectapilot_api.config import Settings
from vectapilot_api.guards.limits import GuardRefused, Limiter, RefusalKind, limited_response
from vectapilot_api.guards.types import DecidedBy, RouteSlug
from vectapilot_api.handoffs.store import HandoffStore, Rated, TurnRating
from vectapilot_api.kb.types import KbError
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.messages import STORE_FAILED, enabled_widget, get_handoff_store
from vectapilot_api.widget.models import FeedbackRecorded, FeedbackRequest
from vectapilot_api.widget.router import get_limiter, get_widgets

logger = logging.getLogger("vectapilot_api.widget")

router = APIRouter(prefix="/v1/widget", tags=["widget"], route_class=SessionRoute)

TURN_NOT_FOUND: Final = "there is no turn with this trace id for this session"
ALREADY_RATED: Final = "this turn has its rating already"
LIMITS_UNAVAILABLE: Final = "feedback cannot be taken now; try again later"
STORE_UNAVAILABLE: Final = "feedback cannot be stored now; try again later"

_TYPED: Final = "a typed error: `{detail, error}`, `Cache-Control: no-store`"
FEEDBACK_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    **{code: {"description": _TYPED} for code in (400, 401, 404, 409, 422, 503)},
    429: {
        "description": "the session's feedback window is full: `{detail, error,"
        " retry_after_seconds, guidance}` with `Retry-After`"
    },
}


def _error(status_code: int, code: str, message: str) -> KbError:
    return KbError(status_code, code, message, headers=NO_STORE)


@router.post(
    "/feedback",
    status_code=status.HTTP_201_CREATED,
    response_model=FeedbackRecorded,
    responses=FEEDBACK_RESPONSES,
)
async def rate_turn(
    session: Annotated[CustomerSession, Depends(get_customer_session)],
    body: FeedbackRequest,
    request: Request,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    limiter: Annotated[Limiter, Depends(get_limiter)],
    store: Annotated[HandoffStore, Depends(get_handoff_store)],
) -> FeedbackRecorded | JSONResponse:
    """One rating of one of this session's own turns (module docstring)."""
    widget = enabled_widget(widgets, session)
    try:
        await limiter.admit(session.business_id, RouteSlug.FEEDBACK, session.customer_ref)
    except GuardRefused as refused:
        if refused.kind is RefusalKind.LIMIT:
            return limited_response(refused, widget.guidance if widget.health else ())
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            DecidedBy.LIMITS_UNAVAILABLE.value,
            LIMITS_UNAVAILABLE,
        ) from None
    settings: Settings = request.app.state.settings
    rating = TurnRating(
        business_id=session.business_id,
        session_id=session.session_id,
        trace_id=body.trace_id,
        rating=body.rating,
    )
    try:
        async with asyncio.timeout(settings.handoff_store_timeout_seconds):
            rated = await store.rate(rating)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "feedback_store_unavailable", STORE_UNAVAILABLE
        )
        logger.warning(STORE_FAILED, extra={"reason": type(error).__name__})
    else:
        if rated is Rated.NOT_FOUND:
            raise _error(status.HTTP_404_NOT_FOUND, "turn_not_found", TURN_NOT_FOUND)
        if rated is Rated.ALREADY_RATED:
            raise _error(status.HTTP_409_CONFLICT, "already_rated", ALREADY_RATED)
        return FeedbackRecorded(trace_id=rating.trace_id, rating=rating.rating)
    raise failure  # outside the `except`: nothing (and no message) is chained to it
