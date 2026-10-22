"""`GET /v1/kb/search?q=&k=` (M2 PR 6): a business's approved public chunks for a question.

The business comes from `X-Business-Id` (ADR 0009); the reader is always `customer` until M8's
authentication can tell staff apart, so only approved, public chunks come back (business-context
§3.4). Hits are returned best first with their scores; there is no threshold here (the answer
decides what counts as evidence).

**Order of checks:** the business (400, before anything else) → `q` and `k` (422, echoing
nothing) → embed (one `embedding_call` record) → one model (503 `reindex_required`) → H1's search.
The question travels in the query string as the plan asks: `log.DropQueryString` keeps it out of
uvicorn's access log, and nothing here logs it. `POST /v1/answer` keeps it out of the URL instead.

**Errors** are `KbError`s: a status, a stable code and a fixed message, never input or document
text (`kb/errors.py` also strips the input from 422s). An exception outside `ERRORS` is a bug and
propagates (500): H1's stub raising `NotImplementedError` included, which is what the canary suite
waits for (`tests/canary/read_paths.py`).

**`X-Trace-Id`** (ADR 0011, option 9b): the trace id is made before the service runs, and every
response from the service step carries it as a header, the typed errors included (and the 200
body as `trace_id`), so a failed request can be joined to its `embedding_call` record. A request
refused before that step (400, or a 422 for the request itself) has no trace id and no header:
nothing was recorded for it. A bug (500) has none either.
"""

from collections.abc import Mapping
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.business import DATABASE_UNAVAILABLE, get_business_id
from vectapilot_api.embeddings.base import InputTooLarge
from vectapilot_api.embeddings.guard import ReindexRequired
from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.gateway.types import BusinessId, TraceId
from vectapilot_api.kb.types import KbError
from vectapilot_api.retrieval.search import Citation, Question, SearchService, new_trace_id
from vectapilot_api.retrieval.types import MAX_K, Reader

router = APIRouter(prefix="/v1/kb", tags=["knowledge base"])

CUSTOMER: Final[Reader] = "customer"
"""The only reader before M8: anyone talking to the business sees approved public chunks only."""

DEFAULT_K: Final = 5

ErrorTable = Mapping[type[Exception], tuple[int, str, str]]

SEARCH_ERRORS: Final[ErrorTable] = {
    ReindexRequired: (
        503,
        "reindex_required",
        "the knowledge base is being re-indexed for a new embedding model; try again later",
    ),
    InputTooLarge: (422, "question_too_long", "the question is longer than the model accepts"),
    Transient: (503, "embedding_unavailable", "the embedding service is unavailable; try later"),
    GatewayError: (502, "embedding_failed", "the embedding service failed"),
    OSError: (503, "database_unavailable", DATABASE_UNAVAILABLE),
    SQLAlchemyError: (503, "database_unavailable", DATABASE_UNAVAILABLE),
}
"""Each failure search may raise → (status, code, fixed message); the nearest class wins."""


TRACE_HEADER: Final = "X-Trace-Id"
"""The response header holding the request's trace id: a random id the server made, never input."""


def typed_error(error: Exception, table: ErrorTable, *, trace_id: str) -> KbError | None:
    """The typed error for `error` by the nearest class in its MRO, carrying `trace_id` as the
    `X-Trace-Id` header; or None (a bug: re-raise)."""
    for cls in type(error).__mro__:
        if cls in table:
            return KbError(*table[cls], headers={TRACE_HEADER: trace_id})
    return None


class SearchParams(BaseModel):
    """The query string: the question and how many hits."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    q: Question
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K)


class SearchResponse(BaseModel):
    trace_id: TraceId
    """New per request; the `embedding_call` record of this search carries it too."""
    embedding_model: str
    hits: list[Citation]
    """Best first: approved, public chunks of this business only."""


def get_search_service(request: Request) -> SearchService:
    service: SearchService = request.app.state.search_service
    return service


_TRACE_HEADER_DOC: Final = {
    TRACE_HEADER: {
        "description": "the request's trace id, as in the `embedding_call`, `llm_call` and"
        " `rag_answer` records; absent when the request was refused before it had one",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
}

_TYPED_ERROR: Final = "a typed error: `{detail, error}`"

ROUTE_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    200: {"headers": _TRACE_HEADER_DOC},
    400: {"description": _TYPED_ERROR},  # the business is refused before there is a trace id
    **{
        code: {"description": _TYPED_ERROR, "headers": _TRACE_HEADER_DOC}
        for code in (422, 502, 503)
    },
}
"""The OpenAPI responses of `GET /v1/kb/search` and `POST /v1/answer`."""


@router.get("/search", responses=ROUTE_RESPONSES)
async def search(
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    params: Annotated[SearchParams, Query()],
    service: Annotated[SearchService, Depends(get_search_service)],
    response: Response,
) -> SearchResponse:
    """The business's approved public chunks closest to `q`, best first, at most `k`."""
    trace_id = new_trace_id()  # before the service: a typed error carries it too
    try:
        result = await service.search(
            business_id, params.q, k=params.k, reader=CUSTOMER, trace_id=trace_id
        )
    except Exception as error:
        failure = typed_error(error, SEARCH_ERRORS, trace_id=trace_id)
        if failure is None:
            raise  # a bug, or H1's stub: never turned into a typed error
    else:
        response.headers[TRACE_HEADER] = trace_id
        return SearchResponse(
            trace_id=trace_id,
            embedding_model=result.embedding_model,
            hits=[Citation.of(hit) for hit in result.hits],
        )
    raise failure  # outside the `except`: nothing (and no message) is chained to it
