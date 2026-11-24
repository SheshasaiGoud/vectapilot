"""What a session route depends on (M8 PR 1; S1, S3, S5, S14, ADR 0019 decisions 2-3, 6).

- **`get_customer_session`** (the widget's routes, D3 (a)): one `Authorization: Bearer <token>`
  header, a customer session in that token's business. Replaces `business.get_business_id` on
  every widget route: the business, the session and the customer (`customer_ref`, S4) come from the
  session row only.
- **`require_staff(role)`** (every `/v1/staff/*` route, D4 (a)): the staff cookie
  (`cookies.read_staff_cookie`), a staff session of an enabled member whose role may use `role`
  (P2), read on every request. A route depends on exactly one `StaffAccess`, whose `role` the
  route inventory reads.
- **`require_same_origin`** (S5's CSRF dependency, on every non-GET staff route, login included):
  `Origin` equal to `public_origin` (absent is refused), `Sec-Fetch-Site: same-origin` when sent,
  and a body only as `application/json`. Origin is the control on a bodiless request (claim,
  logout); SameSite=Strict on the cookie is the other.

**Order and answers** (each a typed `{detail, error}` body that echoes nothing, with
`Cache-Control: no-store`, S9). Before any of these, FastAPI refuses a JSON body it cannot decode
(400 or 422, `auth.http.SessionRoute`): it reads the body before any dependency runs, and the
refusal writes nothing. Then:
1. `X-Business-Id` present, even empty → 400 `business_header_refused` (S1 [proposal]: the session
   names the business; a client that sends both is confused or probing). Before any lookup.
2. Any authentication failure → **one** 401 `unauthorized`, byte for byte (S3): no header, two
   headers, another scheme, a malformed token, an unknown, expired or revoked one, the wrong kind
   (a customer token on a staff route, a staff token as a bearer token on a widget route, S1), a
   disabled member. The customer's 401 adds `WWW-Authenticate: Bearer` (RFC 9110 §11.6.1); the
   staff 401, from a cookie, does not.
3. A lookup that cannot run (Postgres down or slower than `session_lookup_timeout_seconds`) → 503
   `session_store_unavailable` (S14), never a 500 and never a pass.
4. A staff member whose role may not use the route → 403 `role_refused`.
CSRF answers 403 `cross_site_refused` (Origin, Sec-Fetch-Site) and 415 `json_only` (a body that is
not JSON).

**Never logged** (S3): a token, its hash, a cookie or an `Authorization` value is never put in a
log record or an exception message here. A 503 logs `session_lookup_unavailable` with the error's
class name only.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.auth.cookies import read_staff_cookie
from vectapilot_api.auth.store import (
    CustomerSession,
    StaffRole,
    StaffSession,
    find_customer_session,
    find_staff_session,
)
from vectapilot_api.auth.tokens import InvalidToken, ParsedToken, parse_token
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.kb.types import KbError

logger = logging.getLogger("vectapilot_api.auth")

NO_STORE: Final = {"Cache-Control": "no-store"}
"""S9: no auth answer sits in a disk cache."""
BEARER: Final = "bearer"
"""RFC 9110 §11.1: an authentication scheme's name is case-insensitive."""

UNAUTHORIZED: Final = "authentication required"
BUSINESS_HEADER_REFUSED: Final = (
    "X-Business-Id is not accepted on a session route: the session names the business"
)
SESSION_STORE_UNAVAILABLE: Final = "sessions cannot be checked now; try again later"
ROLE_REFUSED: Final = "this staff role may not use this route"
CROSS_SITE_REFUSED: Final = "this request must come from the API's own origin"
JSON_ONLY: Final = "a request body must be application/json"
JSON_MEDIA_TYPE: Final = "application/json"
LOOKUP_UNAVAILABLE: Final = "session_lookup_unavailable"
"""The warning a 503 logs, with `reason` (the error's class name) only."""


def unauthorized(*, bearer: bool) -> KbError:
    """The one 401 (S3). `bearer`: the widget's, which names its scheme (RFC 9110 §11.6.1)."""
    headers = {**NO_STORE, "WWW-Authenticate": "Bearer"} if bearer else NO_STORE
    return KbError(status.HTTP_401_UNAUTHORIZED, "unauthorized", UNAUTHORIZED, headers=headers)


def _refused(status_code: int, code: str, message: str) -> KbError:
    return KbError(status_code, code, message, headers=NO_STORE)


def refuse_business_header(request: Request) -> None:
    """S1: a session route never takes the business from a header."""
    if request.headers.getlist(BUSINESS_HEADER):
        raise _refused(
            status.HTTP_400_BAD_REQUEST, "business_header_refused", BUSINESS_HEADER_REFUSED
        )


def bearer_token(request: Request) -> str | None:
    """The credentials of exactly one `Authorization: Bearer <token>` header, else None."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, separator, credentials = values[0].partition(" ")
    if not separator or scheme.lower() != BEARER or not credentials or " " in credentials:
        return None
    return credentials


def _parsed(token: str | None) -> ParsedToken | None:
    if token is None:
        return None
    try:
        return parse_token(token)
    except InvalidToken:
        return None


def store_unavailable(error: Exception) -> KbError:
    """S14's typed 503 for a session statement that could not run (`error`'s class name is
    logged, never its text). The caller raises it outside its `except`, so nothing is chained."""
    logger.warning(LOOKUP_UNAVAILABLE, extra={"reason": type(error).__name__})
    return _refused(
        status.HTTP_503_SERVICE_UNAVAILABLE, "session_store_unavailable", SESSION_STORE_UNAVAILABLE
    )


@asynccontextmanager
async def session_transaction(request: Request, business_id: str) -> AsyncIterator[AsyncConnection]:
    """`business_id`'s RLS transaction for a session statement, the whole of it bounded by
    `session_lookup_timeout_seconds` (S14). A timeout is a `TimeoutError` (an `OSError`): with a
    database error (`SQLAlchemyError`) the caller answers `store_unavailable`."""
    settings: Settings = request.app.state.settings
    database: Database = request.app.state.database
    async with asyncio.timeout(settings.session_lookup_timeout_seconds):
        async with database.business_transaction(business_id) as connection:
            yield connection


async def get_customer_session(request: Request) -> CustomerSession:
    """The request's customer session (module docstring): 400, the one 401 or 503 otherwise."""
    refuse_business_header(request)
    token = _parsed(bearer_token(request))
    if token is None:
        raise unauthorized(bearer=True)
    try:
        async with session_transaction(request, token.business_id) as connection:
            session = await find_customer_session(connection, token)
    except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
        failure = store_unavailable(error)
    else:
        if session is None:
            raise unauthorized(bearer=True)
        return session
    raise failure  # outside the `except`: nothing (and no message) is chained to it


class StaffAccess:
    """A dependency: the request's staff session, whose member's role may use `role` (P2). Made
    by `require_staff`, one per role, so a route's role is `dependency.role`."""

    def __init__(self, role: StaffRole) -> None:
        self.role = role

    def __repr__(self) -> str:
        return f"require_staff({self.role.value!r})"

    async def __call__(self, request: Request) -> StaffSession:
        refuse_business_header(request)
        settings: Settings = request.app.state.settings
        token = _parsed(read_staff_cookie(request, settings=settings))
        if token is None:
            raise unauthorized(bearer=False)
        try:
            async with session_transaction(request, token.business_id) as connection:
                session = await find_staff_session(connection, token)
        except (OSError, SQLAlchemyError) as error:  # TimeoutError is an OSError
            failure = store_unavailable(error)
        else:
            if session is None:
                raise unauthorized(bearer=False)
            if not session.may(self.role):
                raise _refused(status.HTTP_403_FORBIDDEN, "role_refused", ROLE_REFUSED)
            return session
        raise failure  # outside the `except`: nothing (and no message) is chained to it


_STAFF_ACCESS: Final = {role: StaffAccess(role) for role in StaffRole}


def require_staff(role: StaffRole) -> StaffAccess:
    """The dependency for a staff route that requires `role`: `Depends(require_staff(...))`. The
    same object per role, so FastAPI resolves it once per request."""
    return _STAFF_ACCESS[role]


def _has_body(request: Request) -> bool:
    length = request.headers.get("content-length")
    return "transfer-encoding" in request.headers or (length is not None and length != "0")


def _is_json(request: Request) -> bool:
    values = request.headers.getlist("content-type")
    if len(values) != 1:
        return False
    media_type = values[0].partition(";")[0].strip().lower()
    return media_type == JSON_MEDIA_TYPE


async def require_same_origin(request: Request) -> None:
    """S5's CSRF check for a non-GET staff route (module docstring)."""
    settings: Settings = request.app.state.settings
    origins = request.headers.getlist("origin")
    if settings.public_origin is None or origins != [settings.public_origin]:
        raise _refused(status.HTTP_403_FORBIDDEN, "cross_site_refused", CROSS_SITE_REFUSED)
    fetch_site = request.headers.getlist("sec-fetch-site")
    if fetch_site and fetch_site != ["same-origin"]:
        raise _refused(status.HTTP_403_FORBIDDEN, "cross_site_refused", CROSS_SITE_REFUSED)
    if _has_body(request) and not _is_json(request):
        raise _refused(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "json_only", JSON_ONLY)
