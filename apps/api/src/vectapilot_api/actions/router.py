"""`POST /v1/agent/actions/{action_id}/confirm` (M5 PR 3): the customer's explicit confirmation.

The only route that writes a booking or a ticket, and only the stored, proposed action (W2): the
body is `{"customer_ref", "confirm_token"}` and nothing else (`extra="forbid"`: a body with tool
arguments is a 422). The token travels in the body, never the URL, so no access log, proxy or
browser history holds it; the action id in the path is not a credential.

**Order of checks:** a body that is not JSON at all is FastAPI's own 422 (it is parsed before any
dependency runs); then the business (`X-Business-Id`, 400; refused outside `HEADER_ENVIRONMENTS`
before the service runs, as `customer_ref` is client-asserted until M8) → the path and body
fields (422, echoing nothing) → the confirm service (`actions/confirm.py`, W6's order).

**Responses**, each with `X-Trace-Id` (the confirm's trace id, also in its audit row and its
`tool_call` record):
- 200, `ConfirmResponse` `executed`: written now, or earlier (a repeat returns the stored result);
- 409, `ConfirmResponse` `failed`: nothing was written, with a typed reason (`slot_unavailable`,
  `booking_changed`, ...); a repeat returns the same body;
- 404 `action_not_found`: every refusal, one body (W6);
- 503 `confirm_failed` (this attempt rolled back before its COMMIT and changed nothing; a
  parallel confirm of the same action may still have run it, so confirm again) or
  `outcome_unknown` (a timeout or a failure while committing: retry confirm, it returns the
  stored result or executes once, W9).

Anything else is a bug and propagates (500).
"""

from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.types import (
    ActionNotFound,
    ActionStatus,
    ConfirmFailed,
    ConfirmRequest,
    ConfirmResponse,
    OutcomeUnknown,
)
from vectapilot_api.business import get_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.retrieval.router import TRACE_HEADER, ErrorTable, typed_error
from vectapilot_api.retrieval.search import new_trace_id

router = APIRouter(prefix="/v1/agent", tags=["agent"])

ACTION_NOT_FOUND: Final = "there is no pending action to confirm for this id, customer and token"
CONFIRM_ERRORS: Final[ErrorTable] = {
    ActionNotFound: (status.HTTP_404_NOT_FOUND, "action_not_found", ACTION_NOT_FOUND),
    ConfirmFailed: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "confirm_failed",
        "this attempt changed nothing; confirm again: the action runs at most once",
    ),
    OutcomeUnknown: (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "outcome_unknown",
        "the confirmation's outcome is unknown; retry confirm: the action runs at most once",
    ),
}
"""Each failure → (status, code, fixed message). Never a value, an id or a database message."""

_TRACE: Final = {
    TRACE_HEADER: {
        "description": "the confirm's trace id, as in its audit row and `tool_call` record",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
}
CONFIRM_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    200: {"description": "executed, now or earlier", "headers": _TRACE},
    409: {
        "description": "failed: nothing was written",
        "model": ConfirmResponse,
        "headers": _TRACE,
    },
    400: {"description": "a typed error: `{detail, error}`"},
    **{
        code: {"description": "a typed error: `{detail, error}`", "headers": _TRACE}
        for code in (404, 503)
    },
}


def get_confirm_service(request: Request) -> ConfirmService:
    service: ConfirmService = request.app.state.confirm_service
    return service


@router.post("/actions/{action_id}/confirm", responses=CONFIRM_RESPONSES)
async def confirm(
    action_id: UUID,
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    body: ConfirmRequest,
    service: Annotated[ConfirmService, Depends(get_confirm_service)],
    response: Response,
) -> ConfirmResponse:
    """Carry out the proposed action `action_id` once, with its stored arguments, for the
    customer holding its one-time token; or return the result it already has."""
    trace_id = new_trace_id()  # before the service: a typed error carries it too
    try:
        result = await service.confirm(business_id, action_id, body, trace_id=trace_id)
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
