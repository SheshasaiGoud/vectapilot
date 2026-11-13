"""`POST /v1/extractions?kind=<kind>` (M6 PR 3; ADR 0017 decision 6; E3, E8, E9, E10, E11, E12).

The PDF is the raw request body (as `POST /v1/kb/documents`: no multipart package, and the size is
enforced while the body streams in). The business comes from `X-Business-Id` (ADR 0009), so the
route exists only in local, test and ci (`business.HEADER_ENVIRONMENTS`) until M8: production
gets the header's 400 before anything is read. Uploads are by staff and operators, synthetic
documents only until Q4c and M7 (ADR 0017 decision 6).

**Order of checks:** the business (400) → the query (422, echoing nothing) → the kind (404: an
unknown kind and one the business does not enable are the same answer, E8; with no extraction
folder configured, every kind is unknown) → the format (415: `Content-Type` must be
`application/pdf`) → the size (413, while the body streams in) → extract (PR 2's service: E12's
page and text limits are 413s, a body that is no PDF a 415, an encrypted or unreadable one a 422,
each before any model call and with nothing stored) → store in one transaction → 201.

**The model is always the fake provider in M6 (E11).** `create_app` builds the extraction's
gateway on a `FakeProvider` whatever Settings say, and refuses any other provider; unscripted, the
fake fails, so an extraction through the route is stored all-review (fail closed, E3). Paid
extraction runs only through PR 4's `extracteval --live`. Routing is H1's `route_fields`, which
raises until shesha writes it: until then every extraction is all-review (`internal_error`).

**Responses**, each from the service step on with `X-Trace-Id` (the trace id of the `extraction`
record and the stored row):
- 201 with the stored id, status, failure, pages, expiry and every field (status, level, reasons,
  value, span, page). Values and spans are untrusted data (E7): escape them in any UI.
- 413, 415 or 422 for a refused document (`REFUSALS`), nothing stored or sent.
- 503 `database_unavailable` when storing fails (`SQLAlchemyError`, `OSError`): nothing is stored,
  only the exception's type is logged (`extraction_not_stored`), and the error is raised outside
  `except`, so nothing (no row values a database message may hold) is chained to it (E9).
- 503 `extraction_timeout` past `REQUEST_TIMEOUT_S`: send the document again (each upload is its
  own extraction; one that was cut off while committing may still be stored, and expires as usual).
  A PDF still being read keeps its reading thread until pypdf returns (`REQUEST_TIMEOUT_S`).
Anything else is a bug and propagates (500).
"""

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
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

    @classmethod
    def of(cls, outcome: ExtractionOutcome, stored: StoredExtraction) -> "ExtractionResponse":
        return cls(
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


def get_extraction_service(request: Request) -> ExtractionService:
    service: ExtractionService = request.app.state.extraction_service
    return service


def get_extraction_store(request: Request) -> ExtractionStore:
    store: ExtractionStore = request.app.state.extraction_store
    return store


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
}


@router.post("/extractions", status_code=status.HTTP_201_CREATED, responses=EXTRACTION_RESPONSES)
async def create_extraction(
    request: Request,
    response: Response,
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    params: Annotated[ExtractionParams, Query()],
    service: Annotated[ExtractionService, Depends(get_extraction_service)],
    store: Annotated[ExtractionStore, Depends(get_extraction_store)],
) -> ExtractionResponse:
    """Extract one PDF (the raw body) as the business's `kind`, store the result and return it:
    every field accepted, in review or absent, with its level and reasons (module docstring)."""
    kind = find_kind(request, business_id, params.kind)
    require_pdf(request.headers.get("content-type"))
    data = await read_body(request, MAX_BYTES)
    trace_id = new_trace_id()  # before the service: a typed error carries it too
    file_sha256 = hashlib.sha256(data).hexdigest()
    outcome: ExtractionOutcome | None = None
    stored: StoredExtraction | None = None
    failure: tuple[int, str, str] | None = None
    error = ""
    limit = asyncio.timeout(REQUEST_TIMEOUT_S)
    try:
        async with limit:
            outcome = await service.extract(kind, data, business_id=business_id, trace_id=trace_id)
            stored = await store.save(
                outcome,
                business_id=business_id,
                file_sha256=file_sha256,
                trace_id=trace_id,
                retention_days=kind.retention_days,
            )
    except DocumentRefused as refused:  # E12 and unreadable bodies: nothing stored or sent
        failure = REFUSALS[refused.reason]
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
    return ExtractionResponse.of(outcome, stored)
