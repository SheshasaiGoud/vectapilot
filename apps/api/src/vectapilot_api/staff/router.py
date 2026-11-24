"""Staff login, logout and who-am-I (M8 PR 1; S1, S3, S5, S9, S14, ADR 0019 decision 3, D4 (a)).

- **`POST /v1/staff/login {token}`**: the one-time link's token (the page reads it from the URL
  fragment, D4 (a)). In order: a body FastAPI cannot decode is refused before any check runs (400
  or 422, `auth.http.SessionRoute`); then `X-Business-Id` refused (S1); S5's CSRF check, login
  included (login CSRF: a victim logged into a session the attacker chose); the body model
  (closed); the token's form, else the one 401; then **one transaction**: S5's atomic link use
  (`auth.store.use_login_link`) and, for an enabled member, a new staff session. Either both happen
  or neither: a session that cannot be stored leaves the link unused. Answers 204 with the
  `__Host-vp_staff` cookie (`auth.cookies`), and no body.
  - **A new session on every login** (no fixation): the token is made here, never taken from the
    request, and a cookie the browser already sends is ignored.
  - **A disabled member's link is used up and opens nothing** (B1's proposal, tightened by zara's
    review): the link use reads the member in the same statement, so the login commits the use,
    stores no session and answers the one 401 with no cookie. No session exists for a member who
    was disabled when the link was used, so a hand re-enable brings none back.
  - **No login window** (zara's review, step B3): a window keyed by the token's business prefix,
    which the caller writes, would run before any lookup and only let anyone lock a business's
    staff out (and make login depend on Redis). The link's 256 random bits and single use are the
    control; per-IP limits are M10's edge.
- **`POST /v1/staff/logout`**: `X-Business-Id` refused (S1), S5's Origin check (the control on a
  bodiless request), the staff session (any role), then it revokes that session and clears the
  cookie. 204.
- **`GET /v1/staff/me`**: the session's business, member, role and display name (any role). The
  dashboard calls it after login and on load.

The role map (`ROUTE_ROLES`): every `/v1/staff/*` route but login depends on exactly one
`require_staff(role)`; the route inventory test reads it from the dependencies. Every response
carries `Cache-Control: no-store` and every 422 echoes nothing (`auth.http.SessionRoute`).
"""

from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.auth.cookies import clear_staff_cookie, set_staff_cookie
from vectapilot_api.auth.dependencies import (
    refuse_business_header,
    require_same_origin,
    require_staff,
    session_transaction,
    store_unavailable,
    unauthorized,
)
from vectapilot_api.auth.http import SessionRoute
from vectapilot_api.auth.store import (
    SessionKind,
    StaffRole,
    StaffSession,
    insert_session,
    revoke_session,
    use_login_link,
)
from vectapilot_api.auth.tokens import TOKEN_MAX, InvalidToken, make_token, parse_token
from vectapilot_api.config import Settings
from vectapilot_api.gateway.types import BusinessId

router = APIRouter(prefix="/v1/staff", tags=["staff"], route_class=SessionRoute)

LOGIN: Final = "/v1/staff/login"
"""The one staff route without a staff session (it creates one)."""

_ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    code: {"description": "a typed error: `{detail, error}`, `Cache-Control: no-store`"}
    for code in (400, 401, 403, 415, 422, 503)
}


class LoginRequest(BaseModel):
    """`POST /v1/staff/login`'s body: the link's token only. Its prefix names the business."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    token: str = Field(min_length=1, max_length=TOKEN_MAX, repr=False)


class StaffMe(BaseModel):
    """200 of `GET /v1/staff/me`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    business_id: BusinessId
    staff_id: UUID
    role: StaffRole
    display_name: str


@router.post(
    "/login",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(refuse_business_header), Depends(require_same_origin)],
    responses=_ERROR_RESPONSES,
)
async def login(body: LoginRequest, request: Request) -> Response:
    """Use a one-time link and open a new staff session (module docstring)."""
    try:
        link = parse_token(body.token)
    except InvalidToken:
        raise unauthorized(bearer=False) from None
    settings: Settings = request.app.state.settings
    token = make_token(link.business_id)  # never the link's, never the request's cookie
    created = None
    try:
        async with session_transaction(request, link.business_id) as connection:
            used = await use_login_link(connection, link)
            # A disabled member's link stays used (the transaction commits) and opens nothing.
            if used is not None and used.enabled:
                created = await insert_session(
                    connection,
                    parse_token(token),
                    kind=SessionKind.STAFF,
                    lifetime_s=settings.staff_session_lifetime_seconds,
                    staff_id=used.staff_id,
                )
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = store_unavailable(error)
    else:
        if created is None:
            raise unauthorized(bearer=False)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        set_staff_cookie(
            response, token, max_age_s=settings.staff_session_lifetime_seconds, settings=settings
        )
        return response
    raise failure  # outside the `except`: nothing (and no message) is chained to it


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    # The header first, as on every session route (S1), then S5's Origin check, then the session.
    dependencies=[Depends(refuse_business_header), Depends(require_same_origin)],
    responses=_ERROR_RESPONSES,
)
async def logout(
    session: Annotated[StaffSession, Depends(require_staff(StaffRole.REVIEWER))],
    request: Request,
) -> Response:
    """Revoke this staff session and clear its cookie."""
    settings: Settings = request.app.state.settings
    try:
        async with session_transaction(request, session.business_id) as connection:
            await revoke_session(connection, session.business_id, session.session_id)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = store_unavailable(error)
    else:
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        clear_staff_cookie(response, settings=settings)
        return response
    raise failure


@router.get("/me", response_model=StaffMe, responses=_ERROR_RESPONSES)
async def me(
    session: Annotated[StaffSession, Depends(require_staff(StaffRole.REVIEWER))],
) -> StaffMe:
    """Who this staff session belongs to."""
    return StaffMe(
        business_id=session.business_id,
        staff_id=session.staff_id,
        role=session.role,
        display_name=session.display_name,
    )


ROUTE_ROLES: Final[dict[tuple[str, str], StaffRole | None]] = {
    ("POST", LOGIN): None,
    ("POST", "/v1/staff/logout"): StaffRole.REVIEWER,
    ("GET", "/v1/staff/me"): StaffRole.REVIEWER,
}
"""Every staff route and the role it requires (None: login, which has no session yet). The route
inventory test requires this map to equal what each route's dependencies say (S5)."""
