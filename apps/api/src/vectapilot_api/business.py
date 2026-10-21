"""The business a request is for: the `X-Business-Id` header, until authentication (M8).

ADR 0009: the header names a business; it does not authenticate. So it is accepted only in the
environments listed in `HEADER_ENVIRONMENTS` (an allowlist: `production`, and any environment added
later, get a 400), and business routes cannot be served there before M8 replaces it. A missing,
repeated, malformed or unregistered id gets one and the same error, so the response does not tell
which ids exist.

Use it as a dependency: `business_id: Annotated[BusinessId, Depends(get_business_id)]`, then do
the route's work in `request.app.state.database.business_transaction(business_id)`.
"""

from typing import Annotated, Final

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.config import Settings
from vectapilot_api.db import Database, InvalidBusinessId, parse_business_id
from vectapilot_api.gateway.types import BusinessId

BUSINESS_HEADER: Final = "X-Business-Id"
UNKNOWN_BUSINESS: Final = "X-Business-Id must name one registered business"
HEADER_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
"""Where the header is accepted. A new environment is refused until it is listed here on purpose."""
REFUSED_ENVIRONMENT: Final = (
    "X-Business-Id is accepted only in local, test and ci: business routes need authentication (M8)"
)
DATABASE_UNAVAILABLE: Final = "the database is unavailable; try again later"


async def get_business_id(
    request: Request,
    # Declared for the OpenAPI docs; the value is read below, from every copy of the header.
    x_business_id: Annotated[str | None, Header(alias=BUSINESS_HEADER)] = None,
) -> BusinessId:
    settings: Settings = request.app.state.settings
    if settings.environment not in HEADER_ENVIRONMENTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, REFUSED_ENVIRONMENT)
    # Exactly one copy: with two, a proxy and the app could each read a different one.
    values = request.headers.getlist(BUSINESS_HEADER)
    if len(values) != 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, UNKNOWN_BUSINESS)
    try:
        business_id = parse_business_id(values[0])
    except InvalidBusinessId:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, UNKNOWN_BUSINESS) from None
    database: Database = request.app.state.database
    try:
        exists = await database.business_exists(business_id)
    except (OSError, SQLAlchemyError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, DATABASE_UNAVAILABLE) from None
    if not exists:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, UNKNOWN_BUSINESS)
    return business_id
