"""Error bodies that never echo input: `{"detail": <fixed text>, "error": <code>}`.

FastAPI's default 422 body copies each invalid value back (`input`) and a validator's message;
`validation_error_handler` replaces it, app-wide, with each problem's location and error type
only (as the gateway's `StructuredOutputInvalid`). A location is a parameter or field name.
"""

from typing import Final, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from vectapilot_api.kb.types import KbError

INVALID_REQUEST: Final = "the request is invalid: see problems (location, type)"


async def kb_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(KbError, exc)  # registered for KbError only
    return JSONResponse(
        {"detail": error.message, "error": error.code}, status_code=error.status_code
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    errors = cast(RequestValidationError, exc).errors()  # registered for it only
    problems = [
        {"loc": [str(part) for part in error.get("loc", ())], "type": str(error.get("type"))}
        for error in errors
    ]
    return JSONResponse(
        {"detail": INVALID_REQUEST, "error": "invalid_request", "problems": problems},
        status_code=422,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(KbError, kb_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
