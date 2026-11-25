"""`POST /v1/widget/actions/{action_id}/confirm` (M8 PR 2b; S1, S4, S6, S9, ADR 0016 W2, W6,
ADR 0019 decision 2): the customer's explicit confirmation through the widget.

The same confirmation as `/v1/agent/actions/{action_id}/confirm` (`actions.router`), with the
customer taken from the session instead of the body:

- **Identity from the session** (S1, S4): `get_customer_session` gives the business and the
  customer (`s-<hex>`); the body is `{confirm_token}` and nothing else (`extra="forbid"`: a body
  naming `customer_ref`, tool arguments or any other field is a closed 422 that echoes nothing).
  The route builds `ConfirmRequest` itself with the session's `customer_ref`, so a session can
  confirm only an action proposed in a turn of its own (the confirm service reads the action by
  business, customer and id together, W6): ADR 0016's IDOR stays closed.
- **The same objects** (S6, M7 G13): the confirm service and the limiter pre-check come from the
  confirm route's own dependency functions (`get_confirm_service`, `get_confirm_limits`), so a
  test override wires both routes identically.
- **No unguarded branch** (S6): `actions/router.py`'s `if limits is not None` is not copied. With
  `guard_routes = "off"` (local, test and ci only) there are no limits, and this route answers the
  limiter's own typed 503 (`limits_unavailable`, `guards.routes.unavailable_response`, the body a
  Redis outage gives) and never calls the service.
- **The same answers** (leo's (4)): with the guards on, the limiter's 429 or 503 is returned as
  built, and the service's outcomes and errors map through the confirm route's own table
  (`CONFIRM_ERRORS`): 200 `executed` (now or earlier, a repeat returns the stored body), 409
  `failed`, the one 404 `action_not_found` for every refusal, 503 `confirm_failed` or
  `outcome_unknown`. Status, body and headers equal the confirm route's but for the trace id and
  `Cache-Control: no-store`, which every widget response carries (`auth.http.SessionRoute`, S9).
  Anything else is a bug and propagates (500).

**Order of checks:** a body FastAPI cannot decode (400 or 422, before anything runs), then the
session (400 for `X-Business-Id`, the one 401, S14's typed 503), the path and the body (422), the
session's widget (the config route's one 404 while it is disabled [proposal, as PR 2a's routes]),
the limits (counted before the call, G15: a 429 never follows a write), then the service.

The token travels in the body, never the URL, and is never logged, audited or stored in a widget
row (W2): it is a `Secret` from the body to `ConfirmRequest`, and only its hash is compared.
Every response carries `X-Trace-Id`, the confirm's trace id (its audit row and `tool_call` record).
"""

from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import JSONResponse

from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.router import (
    CONFIRM_ERRORS,
    CONFIRM_RESPONSES,
    get_confirm_limits,
    get_confirm_service,
)
from vectapilot_api.actions.types import ActionStatus, ConfirmRequest, ConfirmResponse
from vectapilot_api.auth.dependencies import get_customer_session
from vectapilot_api.auth.http import SessionRoute
from vectapilot_api.auth.store import CustomerSession
from vectapilot_api.guards.routes import RouteLimits, unavailable_response
from vectapilot_api.retrieval.router import TRACE_HEADER, typed_error
from vectapilot_api.retrieval.search import new_trace_id
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.messages import enabled_widget
from vectapilot_api.widget.models import WidgetConfirmRequest
from vectapilot_api.widget.router import get_widgets

router = APIRouter(prefix="/v1/widget", tags=["widget"], route_class=SessionRoute)

_TYPED: Final = "a typed error: `{detail, error}`, `Cache-Control: no-store`"
WIDGET_CONFIRM_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    **CONFIRM_RESPONSES,
    **{code: {"description": _TYPED} for code in (400, 401, 422)},
    # No `headers` here (leo's PR 2 review): `widget_not_found` and `session_store_unavailable`
    # are raised before the confirm's trace id exists, so `X-Trace-Id` is not on every 404 or 503.
    404: {
        "description": f"{_TYPED}: `action_not_found` (every refusal, W6; with `X-Trace-Id`), or"
        " `widget_not_found` while the session's widget is disabled (no `X-Trace-Id`)",
    },
    503: {
        "description": f"{_TYPED}: `confirm_failed`, `outcome_unknown` or `limits_unavailable`"
        " (Redis down, or the guards off: the service did not run), each with `X-Trace-Id`; or"
        " `session_store_unavailable` (the session lookup, before any trace id: none)",
    },
}


@router.post(
    "/actions/{action_id}/confirm",
    response_model=ConfirmResponse,
    responses=WIDGET_CONFIRM_RESPONSES,
)
async def confirm(
    action_id: UUID,
    session: Annotated[CustomerSession, Depends(get_customer_session)],
    body: WidgetConfirmRequest,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    service: Annotated[ConfirmService, Depends(get_confirm_service)],
    limits: Annotated[RouteLimits | None, Depends(get_confirm_limits)],
    response: Response,
) -> ConfirmResponse | JSONResponse:
    """Carry out this session's proposed action `action_id` once, with its stored arguments, for
    the holder of its one-time token; or return the result it already has (module docstring)."""
    enabled_widget(widgets, session)
    trace_id = new_trace_id()  # before the service: a typed error carries it too
    refused: JSONResponse | None
    if limits is None:  # guard_routes = "off": the limiter's own 503, never the service (S6)
        refused = unavailable_response()
    else:  # G15: counted before the call, outside it
        refused = await limits.admit(
            session.business_id, trace_id=trace_id, customer_ref=session.customer_ref
        )
    if refused is not None:
        refused.headers[TRACE_HEADER] = trace_id
        return refused
    request = ConfirmRequest(customer_ref=session.customer_ref, confirm_token=body.confirm_token)
    try:
        result = await service.confirm(session.business_id, action_id, request, trace_id=trace_id)
    except Exception as error:
        failure = typed_error(error, CONFIRM_ERRORS, trace_id=trace_id)
        if failure is None:
            raise  # a bug: never turned into a typed error
    else:
        response.headers[TRACE_HEADER] = trace_id
        if result.status is ActionStatus.FAILED:
            response.status_code = status.HTTP_409_CONFLICT
        return result
    raise failure  # outside the `except`: nothing (and no message) is chained to it
