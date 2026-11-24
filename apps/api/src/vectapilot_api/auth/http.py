"""What every `/v1/widget/*` and `/v1/staff/*` response carries (M8 PR 1; S1, S9).

`SessionRoute` is the route class of the widget and staff routers (`APIRouter(route_class=...)`),
so these rules hold on those routes only and never on an existing one (S13: `/v1/answer`'s and
`/v1/kb/search`'s goldens record every header in order):

- **`Cache-Control: no-store`** on every response the route makes: a success, a typed error
  (`KbError`, raised by the route or a dependency), a 422, and FastAPI's own refusal of a body it
  cannot decode (S9: masked messages, replies and tokens never sit in a disk cache).
- **A 422 that echoes nothing** (S1). The app's handler (`kb.errors.validation_error_handler`)
  already leaves out every value, but names each problem's location, and for an unknown key that
  location is the key itself: text the client wrote, which could be anything (`customer_ref`, a
  token, a message). Here an unknown key is reported as `(unknown key)`, as the guard files'
  loader does (`guards.config.UNKNOWN_KEY`); every other location is a name of ours.
- **A body FastAPI cannot decode is refused first** (zara's review). FastAPI reads a JSON body
  before any dependency runs: malformed JSON is its 422 (`json_invalid`, closed as above) and a
  body that is not UTF-8 its `HTTPException` 400. Both come before the header, Origin and session
  checks, and write nothing. The 400 becomes the typed `{detail, error}` body here, with
  `no-store`, instead of FastAPI's untyped `{"detail": ...}`.

A path under those prefixes that matches no route (a 404 or 405 from the router itself) carries
no data, and is not covered [proposal]: a middleware would also touch every existing route's
response (`tests/guards/test_redact.py` pins the middleware stack to `RequestScope`).
"""

from collections.abc import Callable, Coroutine
from typing import Any, Final

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException

from vectapilot_api.kb.errors import INVALID_REQUEST
from vectapilot_api.kb.types import KbError

NO_STORE: Final = "no-store"
CACHE_CONTROL: Final = "Cache-Control"
UNKNOWN_KEY: Final = "(unknown key)"
"""What a closed body's 422 says instead of an unknown key's name (as `guards.config`)."""
UNREADABLE_REQUEST: Final = "the request could not be read"
"""FastAPI's own refusal before any check runs (a body that is not UTF-8), as fixed text."""


def closed_problems(error: RequestValidationError) -> list[dict[str, object]]:
    """Each problem's location and type, with an unknown key's name replaced (module docstring)."""
    problems: list[dict[str, object]] = []
    for item in error.errors():
        location = [str(part) for part in item.get("loc", ())]
        kind = str(item.get("type"))
        if kind == "extra_forbidden" and location:
            location[-1] = UNKNOWN_KEY
        problems.append({"loc": location, "type": kind})
    return problems


def closed_validation_response(error: RequestValidationError) -> JSONResponse:
    """The 422 of a session route: the app's body, closed, and `no-store`."""
    return JSONResponse(
        {"detail": INVALID_REQUEST, "error": "invalid_request", "problems": closed_problems(error)},
        status_code=422,
        headers={CACHE_CONTROL: NO_STORE},
    )


def unreadable_response(error: HTTPException) -> JSONResponse:
    """FastAPI's own refusal on a session route (module docstring): its status, a typed body of
    fixed text (never its detail or headers) and `no-store`."""
    return JSONResponse(
        {"detail": UNREADABLE_REQUEST, "error": "invalid_request"},
        status_code=error.status_code,
        headers={CACHE_CONTROL: NO_STORE},
    )


class SessionRoute(APIRoute):
    """The widget's and the staff routers' route class (module docstring)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def no_store(request: Request) -> Response:
            try:
                response = await handler(request)
            except RequestValidationError as error:
                return closed_validation_response(error)
            except KbError as error:
                error.headers[CACHE_CONTROL] = NO_STORE
                raise
            except HTTPException as error:  # FastAPI's (a subclass of Starlette's) included
                return unreadable_response(error)
            response.headers[CACHE_CONTROL] = NO_STORE
            return response

        return no_store
