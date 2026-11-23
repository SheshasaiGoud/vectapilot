"""`POST /v1/extractions?kind=<kind>` (M6 PR 3; ADR 0017 decision 6; E3, E8, E9, E10, E11, E12).

The PDF is the raw request body (as `POST /v1/kb/documents`: no multipart package, and the size is
enforced while the body streams in). The business comes from `X-Business-Id` (ADR 0009), so the
route exists only in local, test and ci (`business.HEADER_ENVIRONMENTS`) until M8: production
gets the header's 400 before anything is read. Uploads are by staff and operators, synthetic
documents only until Q4c and M7 (ADR 0017 decision 6).

**Order of checks:** the business (400) → the query (422, echoing nothing) → the kind (404: an
unknown kind and one the business does not enable are the same answer, E8; with no extraction
folder configured, every kind is unknown) → the format (415: `Content-Type` must be
`application/pdf`) → with `guard_routes = "on"` (the default), M7's guard (`guards.document`):
no guard folder is a typed 503 `document_scan_unavailable`, then the limits (the business's
window, before the body is read, so a limited caller never costs a body read or a PDF parse; a 429
with `Retry-After`, or a typed 503 `limits_unavailable` when Redis is down, nothing read,
extracted or stored) → the size (413, while the body streams in) → extract (PR 2's service: E12's
page and text limits are 413s, a body that is no PDF a 415, an encrypted or unreadable one a 422,
each before any model call and with nothing stored) → with the guard on, the document screen
(M7 PR 7, G13: after the extraction, before the store; the bytes read again in the app's reading
thread, every page's text through the red-flag and injection screens, hidden text as a signal) →
store in one transaction → 201. With `guard_routes = "off"` the route is M6's, byte for byte.

**The model is always the fake provider in M6 (E11).** `create_app` builds the extraction's
gateway on a `FakeProvider` whatever Settings say, and refuses any other provider; unscripted, the
fake fails, so an extraction through the route is stored all-review (fail closed, E3). Paid
extraction runs only through PR 4's `extracteval --live`. Routing is H1's `route_fields`, which
raises until shesha writes it: until then every extraction is all-review (`internal_error`).

**Responses**, each from the service step on with `X-Trace-Id` (the trace id of the `extraction`
record and the stored row):
- 201 with the stored id, status, failure, pages, expiry and every field (status, level, reasons,
  value, span, page). Values and spans are untrusted data (E7): escape them in any UI. With the
  guard on, also `document_flags` (codes and page numbers, never stored; absent while off).
- 413, 415 or 422 for a refused document (`REFUSALS`), nothing stored or sent.
- 503 `database_unavailable` when storing fails (`SQLAlchemyError`, `OSError`): nothing is stored,
  only the exception's type is logged (`extraction_not_stored`), and the error is raised outside
  `except`, so nothing (no row values a database message may hold) is chained to it (E9).
- 503 `document_scan_unavailable` when the document screen cannot run (G3: H1's stub, a guard
  that raises, hostile bytes, a cap): nothing is stored, `extraction_not_stored` says so (error
  `DocumentScanFailed`), and the `guard` record holds the cause's class name.
- 503 `extraction_timeout` past `REQUEST_TIMEOUT_S`: send the document again (each upload is its
  own extraction; one that was cut off while committing may still be stored, and expires as usual).
  A PDF still being read keeps its reading thread until pypdf returns (`REQUEST_TIMEOUT_S`).
Anything else is a bug and propagates (500).
"""

import asyncio
import hashlib
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import Executor
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.business import DATABASE_UNAVAILABLE, get_business_id
from vectapilot_api.extraction.config import KindConfig, Snake
from vectapilot_api.extraction.files import ExtractionConfig
from vectapilot_api.extraction.routing import DocumentStatus, FieldStatus, Level, Reason
from vectapilot_api.extraction.service import ExtractionOutcome, ExtractionService
from vectapilot_api.extraction.store import ExtractionStore, StoredExtraction, ordered
from vectapilot_api.extraction.text import MAX_BYTES, DocumentRefused, RefusalReason
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.document import (
    SCAN_UNAVAILABLE,
    SCAN_UNAVAILABLE_DETAIL,
    DocumentFlag,
    DocumentScanFailed,
    DocumentScreen,
    ExtractionGuard,
)
from vectapilot_api.kb.router import read_body
from vectapilot_api.kb.types import KbError
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import new_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["extraction"])

PDF: Final = "application/pdf"
REQUEST_TIMEOUT_S: Final = 40.0
"""How long the request waits for the extraction and its storage [proposal]: a bound, not a
measured latency. Above the model call's own deadline (`extractor.EXTRACT_DEADLINE_S`, 30 s), so a
slow model is stored as a `timeout` review (E3) before this limit is reached. The PDF is read in a
worker thread (`service.page_reader`), so a slow read gets this 503 on time, but the read itself
keeps its thread until pypdf returns: a known limit until a hard kill (a subprocess, M9)."""
NOT_STORED: Final = "extraction_not_stored"
"""The log record (and its `extra` key) when storing fails: metadata and the exception's type."""

KIND_NOT_FOUND: Final = (
    status.HTTP_404_NOT_FOUND,
    "kind_not_found",
    "this business has no extraction of this kind",
)
UNSUPPORTED_MEDIA_TYPE: Final = (
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "unsupported_media_type",
    f"Content-Type must be {PDF}",
)
STORE_FAILED: Final = (
    status.HTTP_503_SERVICE_UNAVAILABLE,
    "database_unavailable",
    DATABASE_UNAVAILABLE,
)
SCAN_FAILED: Final = (
    status.HTTP_503_SERVICE_UNAVAILABLE,
    SCAN_UNAVAILABLE,
    SCAN_UNAVAILABLE_DETAIL,
)
"""The document screen could not run (M7 PR 7, G3): nothing stored."""
TIMED_OUT: Final = (
    status.HTTP_503_SERVICE_UNAVAILABLE,
    "extraction_timeout",
    "the extraction did not finish in time; send the document again",
)
REFUSALS: Final[Mapping[RefusalReason, tuple[int, str, str]]] = {
    RefusalReason.TOO_LARGE: (413, "file_too_large", "the file is larger than the upload limit"),
    RefusalReason.TOO_MANY_PAGES: (413, "too_many_pages", "the PDF has more pages than the limit"),
    RefusalReason.TOO_MUCH_TEXT: (
        413,
        "text_too_long",
        "the document's text is longer than the limit",
    ),
    RefusalReason.NOT_PDF: (415, "not_pdf", "the body is not a PDF"),
    RefusalReason.ENCRYPTED: (422, "encrypted_document", "encrypted PDFs are not supported"),
    RefusalReason.UNREADABLE: (422, "unreadable_document", "the PDF cannot be read"),
}
"""Each refusal before the model call (E12, `text.read_pages`) → (status, code, fixed message)."""


class ExtractionParams(BaseModel):
    """The query string: the document's kind, one of the business's enabled kinds."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    kind: Snake


class ExtractedField(BaseModel):
    """One field as stored (D6 (a)). `value` and `span` are untrusted document text (E7)."""

    field: str
    status: FieldStatus
    level: Level | None
    """An ordinal level from checks in code (D4 (a)), never a probability; only with a value."""
    reasons: list[Reason]
    value: str | None
    span: str | None
    """The quote's label-to-value text (at most 40 characters), never the whole quote."""
    page: int | None


class ExtractionResponse(BaseModel):
    id: UUID
    kind: str
    status: DocumentStatus
    """`complete` only when no field is in review (D5 (a))."""
    failure: Reason | None
    """E3's failure, if the extraction failed closed into review."""
    pages: int
    image_pages: list[int]
    expires_at: datetime
    """Hidden from then on, and deleted by the next purge (E10)."""
    fields: list[ExtractedField]
    document_flags: list[DocumentFlag] | None = None
    """M7 PR 7 (G7): what the document screen flagged, as codes and page numbers (a red flag is
    `urgent`), never stored. Set only while the route's guard is on (an empty list: screened,
    nothing found); while it is off the key is absent (the route's `response_model_exclude_unset`),
    so M6's response keeps its bytes."""

    @classmethod
    def of(
        cls,
        outcome: ExtractionOutcome,
        stored: StoredExtraction,
        document_flags: Sequence[DocumentFlag] | None = None,
    ) -> "ExtractionResponse":
        response = cls(
            id=stored.id,
            kind=outcome.kind,
            status=outcome.status,
            failure=outcome.failure,
            pages=outcome.pages,
            image_pages=list(outcome.image_pages),
            expires_at=stored.expires_at,
            fields=[
                ExtractedField(
                    field=result.field,
                    status=result.status,
                    level=result.level,
                    reasons=[Reason(reason) for reason in ordered(result.reasons)],
                    value=result.value,
                    span=result.span,
                    page=result.page,
                )
                for result in outcome.fields
            ],
        )
        if document_flags is not None:  # set (so serialised) only when the screen ran
            response.document_flags = list(document_flags)
        return response


def get_extraction_service(request: Request) -> ExtractionService:
    service: ExtractionService = request.app.state.extraction_service
    return service


def get_extraction_store(request: Request) -> ExtractionStore:
    store: ExtractionStore = request.app.state.extraction_store
    return store


def get_extraction_guard(request: Request) -> ExtractionGuard | None:
    """The route's guard (M7 PR 6-7, G13: an object the route calls, never a screening
    dependency): its limits and its document screen; None while `guard_routes` is off."""
    guard: ExtractionGuard | None = request.app.state.extraction_guard
    return guard


def find_kind(request: Request, business_id: str, kind: str) -> KindConfig:
    """The business's configuration for `kind`, or the one 404 (E8)."""
    config: ExtractionConfig | None = request.app.state.extraction
    found = None if config is None else config.kind_config(business_id, kind)
    if found is None:
        raise KbError(*KIND_NOT_FOUND)
    return found


def require_pdf(content_type: str | None) -> None:
    if (content_type or "").split(";", 1)[0].strip().lower() != PDF:
        raise KbError(*UNSUPPORTED_MEDIA_TYPE)


_TRACE: Final = {
    TRACE_HEADER: {
        "description": "the extraction's trace id, as in its `extraction` record and stored row",
        "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    }
}
_TYPED_ERROR: Final = "a typed error: `{detail, error}`"
EXTRACTION_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    201: {"description": "stored", "headers": _TRACE},
    **{code: {"description": _TYPED_ERROR} for code in (400, 404, 422)},
    **{code: {"description": _TYPED_ERROR, "headers": _TRACE} for code in (413, 415, 503)},
    429: {
        "description": "a full rate window (M7): `{detail, error, retry_after_seconds,"
        " guidance}`, with `Retry-After`; nothing was read or stored",
        "headers": _TRACE,
    },
}


@router.post(
    "/extractions",
    status_code=status.HTTP_201_CREATED,
    response_model=ExtractionResponse,
    response_model_exclude_unset=True,  # `document_flags` only when set (ExtractionResponse)
    responses=EXTRACTION_RESPONSES,
)
async def create_extraction(
    request: Request,
    response: Response,
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    params: Annotated[ExtractionParams, Query()],
    service: Annotated[ExtractionService, Depends(get_extraction_service)],
    store: Annotated[ExtractionStore, Depends(get_extraction_store)],
    guard: Annotated[ExtractionGuard | None, Depends(get_extraction_guard)],
) -> ExtractionResponse | JSONResponse:
    """Extract one PDF (the raw body) as the business's `kind`, store the result and return it:
    every field accepted, in review or absent, with its level and reasons (module docstring)."""
    kind = find_kind(request, business_id, params.kind)
    require_pdf(request.headers.get("content-type"))
    trace_id: str | None = None
    screen: DocumentScreen | None = None
    if guard is not None:  # M7 PR 6-7: before the body is read (module docstring)
        trace_id = new_trace_id()  # the guard record's, then the extraction's
        admitted = await guard.admit(business_id, trace_id=trace_id)
        if isinstance(admitted, JSONResponse):
            admitted.headers[TRACE_HEADER] = trace_id
            return admitted
        screen = admitted
    try:
        return await extract_and_store(
            request, response, business_id, kind, service, store, trace_id, screen
        )
    finally:
        if screen is not None:  # the request's one `guard` record, whatever happened
            screen.finish()


async def extract_and_store(
    request: Request,
    response: Response,
    business_id: BusinessId,
    kind: KindConfig,
    service: ExtractionService,
    store: ExtractionStore,
    trace_id: str | None,
    screen: DocumentScreen | None,
) -> ExtractionResponse:
    """The body, the extraction, the document screen (M7 PR 7, when `screen` is given) and the
    store, in the module docstring's order. Raises the typed errors."""
    data = await read_body(request, MAX_BYTES)
    if trace_id is None:
        trace_id = new_trace_id()  # before the service: a typed error carries it too
    file_sha256 = hashlib.sha256(data).hexdigest()
    outcome: ExtractionOutcome | None = None
    flags: tuple[DocumentFlag, ...] | None = None
    stored: StoredExtraction | None = None
    failure: tuple[int, str, str] | None = None
    error = ""
    limit = asyncio.timeout(REQUEST_TIMEOUT_S)
    try:
        async with limit:
            outcome = await service.extract(kind, data, business_id=business_id, trace_id=trace_id)
            if screen is not None:  # M7 PR 7 (G13): after the extraction, before the store
                reader: Executor | None = request.app.state.extraction_reader
                flags = await screen.scan(data, reader=reader)
            stored = await store.save(
                outcome,
                business_id=business_id,
                file_sha256=file_sha256,
                trace_id=trace_id,
                retention_days=kind.retention_days,
            )
    except DocumentRefused as refused:  # E12 and unreadable bodies: nothing stored or sent
        failure = REFUSALS[refused.reason]
    except DocumentScanFailed:  # G3: nothing stored; the guard record holds the cause's class
        failure, error = SCAN_FAILED, DocumentScanFailed.__name__
    except TimeoutError as caught:  # before OSError: TimeoutError is one
        if not limit.expired():
            failure, error = STORE_FAILED, type(caught).__name__  # the database's own timeout
        else:
            failure, error = TIMED_OUT, type(caught).__name__
    except (SQLAlchemyError, OSError) as caught:  # E3: a database failure stores nothing
        failure, error = STORE_FAILED, type(caught).__name__
    if failure is not None:
        if error:
            logger.warning(
                NOT_STORED,
                extra={
                    NOT_STORED: {
                        "business_id": business_id,
                        "trace_id": trace_id,
                        "kind": kind.kind,
                        "code": failure[1],
                        "error": error,
                    }
                },
            )
        raise KbError(*failure, headers={TRACE_HEADER: trace_id})  # outside `except`: unchained
    if outcome is None or stored is None:  # pragma: no cover (both are set without a failure)
        raise RuntimeError("an extraction finished without an outcome")
    response.headers[TRACE_HEADER] = trace_id
    return ExtractionResponse.of(outcome, stored, flags)
