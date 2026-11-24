"""The widget's session routes (M8 PR 1; S1, S3, S9, S14, S16, ADR 0019 decisions 2, 5, 7).

- **`GET /v1/widget/{business_id}/config`**: what the widget shows before any session, the
  guidance included (S16). Files only, read at startup (`app.state.widgets`): no database and no
  Redis, so it answers when both are down (S14). A business without an enabled widget is a 404.
- **`POST /v1/widget/sessions {business_id, ui_language}`**: a new customer session, created
  lazily by the widget on the first send or "talk to a person", never on page load (S14). In
  order: a body FastAPI cannot decode is refused before any check runs (400 or 422,
  `auth.http.SessionRoute`); `X-Business-Id` refused (S1); S5's same-origin check (`Origin` equal
  to `public_origin`; the widget page is served by the API, D2 (a)); the body model (closed); the
  business
  has an enabled widget and offers `ui_language`; the per-business creation window in Redis
  (`RouteSlug.SESSION`), failing closed (a 429 with the guidance of a health business, or a typed
  503); then one insert under the business's RLS with `expires_at` from the database's clock
  (P1). The token is made here (`auth.tokens.make_token`) and returned once; only its hash is
  stored. A business with a widget file but no registry row is the same 404.
- **`DELETE /v1/widget/session`**: "End chat" (S18): revokes the bearer's own session. 204 also
  when it was revoked a moment earlier (the lookup found it live); the next request is the one
  401.

Every response carries `Cache-Control: no-store` and every 422 echoes nothing
(`auth.http.SessionRoute`). Every failure of authentication is the one 401
(`auth.dependencies.unauthorized`), and a statement that cannot run is S14's typed 503. Nothing
here logs a token, a hash or a header.
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from vectapilot_api.auth.dependencies import (
    NO_STORE,
    get_customer_session,
    refuse_business_header,
    require_same_origin,
    session_transaction,
    store_unavailable,
)
from vectapilot_api.auth.http import SessionRoute
from vectapilot_api.auth.store import (
    CustomerSession,
    SessionKind,
    insert_session,
    revoke_session,
)
from vectapilot_api.auth.tokens import make_token, parse_token
from vectapilot_api.config import WIDGET_CLIENT_MARGIN_SECONDS, Settings
from vectapilot_api.guards.limits import GuardRefused, Limiter, RefusalKind, limited_response
from vectapilot_api.guards.types import DecidedBy, RouteSlug
from vectapilot_api.kb.types import KbError
from vectapilot_api.widget.config import WidgetBusiness, WidgetRegistry
from vectapilot_api.widget.models import SessionCreated, SessionRequest, WidgetConfigResponse

router = APIRouter(prefix="/v1/widget", tags=["widget"], route_class=SessionRoute)

FOREIGN_KEY_VIOLATION: Final = "23503"
"""`sessions.business_id` references the registry: a business that is not registered."""
WIDGET_NOT_FOUND: Final = "this business has no widget"
LANGUAGE_NOT_OFFERED: Final = "ui_language is not one of this widget's languages"
LIMITS_UNAVAILABLE: Final = "new sessions cannot be opened now; try again later"

_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    code: {"description": "a typed error: `{detail, error}`, `Cache-Control: no-store`"}
    for code in (400, 401, 403, 404, 415, 422, 503)
}


def get_widgets(request: Request) -> WidgetRegistry:
    widgets: WidgetRegistry = request.app.state.widgets
    return widgets


def get_limiter(request: Request) -> Limiter:
    limiter: Limiter = request.app.state.limiter
    return limiter


def _error(status_code: int, code: str, message: str) -> KbError:
    return KbError(status_code, code, message, headers=NO_STORE)


def widget_not_found() -> KbError:
    """One answer for an unknown id, a business without a file, a disabled widget and an
    unregistered business: it never says which."""
    return _error(status.HTTP_404_NOT_FOUND, "widget_not_found", WIDGET_NOT_FOUND)


def _enabled(widgets: WidgetRegistry, business_id: str) -> WidgetBusiness:
    widget = widgets.enabled(business_id)
    if widget is None:
        raise widget_not_found()
    return widget


def _unregistered(error: IntegrityError) -> bool:
    return getattr(getattr(error, "orig", None), "sqlstate", None) == FOREIGN_KEY_VIOLATION


@router.get(
    "/{business_id}/config",
    response_model=WidgetConfigResponse,
    dependencies=[Depends(refuse_business_header)],
    responses=_ERROR_RESPONSES,
)
async def widget_config(
    business_id: str, widgets: Annotated[WidgetRegistry, Depends(get_widgets)]
) -> WidgetConfigResponse:
    """The business's widget texts and guidance (files only)."""
    return WidgetConfigResponse.of(_enabled(widgets, business_id))


@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=SessionCreated,
    dependencies=[Depends(refuse_business_header), Depends(require_same_origin)],
    responses={
        **_ERROR_RESPONSES,
        429: {
            "description": "the business's creation window is full: `{detail, error,"
            " retry_after_seconds, guidance}` with `Retry-After`"
        },
    },
)
async def create_session(
    body: SessionRequest,
    request: Request,
    widgets: Annotated[WidgetRegistry, Depends(get_widgets)],
    limiter: Annotated[Limiter, Depends(get_limiter)],
) -> SessionCreated | JSONResponse:
    """A new customer session for `business_id`'s widget (module docstring)."""
    widget = _enabled(widgets, body.business_id)
    if body.ui_language not in widget.file.languages:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "language_not_offered", LANGUAGE_NOT_OFFERED
        )
    try:
        await limiter.admit(widget.business_id, RouteSlug.SESSION)
    except GuardRefused as refused:
        if refused.kind is RefusalKind.LIMIT:
            return limited_response(refused, widget.guidance if widget.health else ())
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            DecidedBy.LIMITS_UNAVAILABLE.value,
            LIMITS_UNAVAILABLE,
        ) from None
    settings: Settings = request.app.state.settings
    token = make_token(widget.business_id)
    lifetime_s = settings.customer_session_lifetime_seconds
    try:
        async with session_transaction(request, widget.business_id) as connection:
            created = await insert_session(
                connection,
                parse_token(token),
                kind=SessionKind.CUSTOMER,
                lifetime_s=lifetime_s,
                ui_language=body.ui_language,
            )
    except IntegrityError as error:
        failure = widget_not_found() if _unregistered(error) else store_unavailable(error)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = store_unavailable(error)
    else:
        return SessionCreated(
            token=token,  # pydantic wraps it in the Secret
            expires_at=created.expires_at,
            lifetime_seconds=lifetime_s,
            ui_language=body.ui_language,
            message_timeout_seconds=settings.widget_turn_bound_seconds
            + WIDGET_CLIENT_MARGIN_SECONDS,
            confirm_timeout_seconds=settings.widget_confirm_timeout_seconds,
        )
    raise failure  # outside the `except`: nothing (and no message) is chained to it


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT, responses=_ERROR_RESPONSES)
async def end_session(
    session: Annotated[CustomerSession, Depends(get_customer_session)], request: Request
) -> Response:
    """Revoke the bearer's own session ("End chat", S18)."""
    try:
        async with session_transaction(request, session.business_id) as connection:
            await revoke_session(connection, session.business_id, session.session_id)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = store_unavailable(error)
    else:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    raise failure
