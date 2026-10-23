"""`POST /v1/kb/documents` and `POST /v1/kb/reindex` (M2 PR 5), scoped by `X-Business-Id`.

**Request format: the file is the raw request body.** `Content-Type` is its format
(`MEDIA_TYPES`), and what the uploader declares about it (source id, version, language, kind,
status, audience ...) are query parameters (`IngestParams`). Why: no new package (multipart
uploads need `python-multipart`, not approved), no base64 (a third larger, and decoded before it
can be measured), and the size limit is enforced while the body streams in, before anything is
parsed: a declared `Content-Length` over the limit is refused unread, and the stream stops once it
passes the limit.

**Order of checks:** the business (400, before anything else is read) → the query (422) → the
format (415) → the size (413) → load, chunk, embed, store.

**Errors** are `KbError`s: a status, a stable `error` code and a fixed `detail`. None of them
echoes document text, a header or a query value (`errors.py` also strips the input from 422s).
"""

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.business import DATABASE_UNAVAILABLE, get_business_id
from vectapilot_api.embeddings.base import InputTooLarge
from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.ingestion.types import (
    EmptyDocumentError,
    FileTooLargeError,
    IngestionError,
    MalformedDocumentError,
    NotUtf8Error,
    SourceType,
    TextTooLongError,
    TooManyPagesError,
)
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.types import (
    ApprovalConflict,
    AudienceConflict,
    IngestParams,
    IngestResult,
    KbError,
    LanguageConflict,
    ReindexResult,
    StatusConflict,
    VersionConflict,
)

router = APIRouter(prefix="/v1/kb", tags=["knowledge base"])

MEDIA_TYPES: Final = {
    "text/markdown": SourceType.MARKDOWN,
    "text/html": SourceType.HTML,
    "text/csv": SourceType.CSV,
    "application/pdf": SourceType.PDF,
}
"""`Content-Type` (without parameters such as `charset`) → the file format the loader reads."""

UNSUPPORTED_MEDIA_TYPE: Final = (
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    "unsupported_media_type",
    f"Content-Type must be one of: {', '.join(sorted(MEDIA_TYPES))}",
)
FILE_TOO_LARGE: Final = (
    status.HTTP_413_CONTENT_TOO_LARGE,
    "file_too_large",
    "the file is larger than the upload limit",
)

ERRORS: Final[dict[type[Exception], tuple[int, str, str]]] = {
    FileTooLargeError: FILE_TOO_LARGE,
    TooManyPagesError: (413, "too_many_pages", "the PDF has more pages than the limit"),
    TextTooLongError: (413, "text_too_long", "the document's text is longer than the limit"),
    EmptyDocumentError: (422, "empty_document", "the file has no text to index"),
    NotUtf8Error: (422, "not_utf8", "the file is not valid UTF-8"),
    MalformedDocumentError: (
        422,
        "malformed_document",
        "the file cannot be read as its declared format",
    ),
    IngestionError: (422, "unreadable_document", "the file cannot be ingested"),
    VersionConflict: (
        409,
        "version_conflict",
        "this source id and version are stored with other content: upload it as a new version",
    ),
    StatusConflict: (
        409,
        "status_conflict",
        "this source id and version are stored with another status: upload it as a new version",
    ),
    LanguageConflict: (
        409,
        "language_conflict",
        "this source id and version are stored with another language: upload it as a new version",
    ),
    AudienceConflict: (
        409,
        "audience_conflict",
        "this source id and version are stored with another audience: upload it as a new version",
    ),
    ApprovalConflict: (
        409,
        "approval_conflict",
        "another version of this source was approved at the same time: send the upload again",
    ),
    InputTooLarge: (422, "chunk_too_long", "a chunk is longer than the embedding model accepts"),
    Transient: (503, "embedding_unavailable", "the embedding service is unavailable; try later"),
    GatewayError: (502, "embedding_failed", "the embedding service failed"),
    OSError: (503, "database_unavailable", DATABASE_UNAVAILABLE),
    SQLAlchemyError: (503, "database_unavailable", DATABASE_UNAVAILABLE),
}
"""Each failure the service may raise → (status, code, fixed message); the nearest class wins."""

_MAPPED: Final = tuple(ERRORS)


def as_kb_error(error: Exception) -> KbError:
    """The typed error for one of `ERRORS`' classes (by the nearest class in its MRO)."""
    return KbError(*next(ERRORS[cls] for cls in type(error).__mro__ if cls in ERRORS))


def get_kb_service(request: Request) -> KbService:
    service: KbService = request.app.state.kb_service
    return service


def source_type_of(content_type: str | None) -> SourceType:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    source_type = MEDIA_TYPES.get(media_type)
    if source_type is None:
        raise KbError(*UNSUPPORTED_MEDIA_TYPE)
    return source_type


async def read_body(request: Request, max_bytes: int) -> bytes:
    """The body, refused (413) once it is longer than `max_bytes`: by the declared length before
    reading anything, else while it streams in (a chunked body declares none)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise KbError(*FILE_TOO_LARGE)
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > max_bytes:
            raise KbError(*FILE_TOO_LARGE)
    return bytes(body)


ERROR_RESPONSES: Final[dict[int | str, dict[str, object]]] = {
    code: {"description": "a typed error: `{detail, error}`"}
    for code in (400, 409, 413, 415, 422, 502, 503)
}


@router.post(
    "/documents",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "already stored (same bytes, status, language and audience)"},
        **ERROR_RESPONSES,
    },
)
async def ingest_document(
    request: Request,
    response: Response,
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    params: Annotated[IngestParams, Query()],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> IngestResult:
    """Ingest one file (the raw body) for the business: load, chunk, embed, store. 201 when it
    was stored now, 200 when this source id and version already hold the same bytes, status,
    language and audience; 409 when they hold other bytes, another status, another language or
    another audience, reported in that order (a change is a new version). An approved upload
    retires the source's previously approved version (ADR 0010)."""
    source_type = source_type_of(request.headers.get("content-type"))
    data = await read_body(request, service.limits.max_bytes)
    try:
        result = await service.ingest(business_id, data, source_type, params)
    except _MAPPED as error:
        failure = as_kb_error(error)
    else:
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result
    raise failure  # outside the `except`: no exception (or its message) is chained to it


@router.post("/reindex", responses=ERROR_RESPONSES)
async def reindex(
    business_id: Annotated[BusinessId, Depends(get_business_id)],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> ReindexResult:
    """Re-embed every chunk of the business whose model is not the configured one (after a model
    change), synchronously; switches them all in one transaction."""
    try:
        return await service.reindex(business_id)
    except _MAPPED as error:
        failure = as_kb_error(error)
    raise failure
