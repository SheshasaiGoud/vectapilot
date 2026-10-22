"""Typed values of the knowledge-base endpoints (M2 PR 5): request metadata, results, errors.

Every error carries a fixed message and a code: never document text, a header or a query value.
"""

import uuid
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vectapilot_api.ingestion.types import SourceMeta, SourceType


class ContentKind(StrEnum):
    """What a knowledge source is about (business-context §3.4, without "PDFs": a format).

    Its own field, separate from `SourceType` (the file format), decided 2026-10-21. Migration
    0003 holds the same list as a CHECK constraint (a test compares them).
    """

    FAQ = "faq"
    SERVICE_LIST = "service_list"
    PRODUCT_CATALOGUE = "product_catalogue"
    PRICING = "pricing"
    APPOINTMENT_POLICY = "appointment_policy"
    OTHER = "other"


DocumentStatus = Literal["draft", "approved", "retired"]
Audience = Literal["public", "staff_only"]


class IngestParams(BaseModel):
    """What the uploader declares about the file, as query parameters (the body is the file).

    No defaults for status and audience: approving a source or showing it to customers is always
    a decision the uploader states. The limits match `SourceMeta`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    source_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    language: str = Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content_kind: ContentKind
    status: DocumentStatus
    audience: Audience
    review_by: date | None = None

    def source_meta(self) -> SourceMeta:
        return SourceMeta(
            source_id=self.source_id, version=self.version, language=self.language, title=self.title
        )


class IngestResult(BaseModel):
    document_id: uuid.UUID
    source_id: str
    version: str
    created: bool
    """False when this business already had this source id and version with the same bytes and
    status."""
    chunks_written: int
    """0 for an existing document: nothing was loaded, embedded or written."""
    embedding_model: str | None
    """The model id the stored chunks carry; None for an existing document (nothing embedded)."""
    retired_version: str | None
    """The version of this source that was approved until this upload approved a new one, and is
    now retired (ADR 0010). None when nothing was retired: a draft or retired upload, the source's
    first approved version, or an existing document."""


class ReindexResult(BaseModel):
    embedding_model: str
    dimension: int
    chunks_reindexed: int


# ── What the store reads and writes ──────────────────────────────────────────────────────────


class NewDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    version: str
    title: str = Field(repr=False)
    source_type: SourceType
    content_kind: ContentKind
    language: str
    status: DocumentStatus
    audience: Audience
    content_sha256: str
    review_by: date | None
    metadata: dict[str, object]


class NewChunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_index: int
    content: str = Field(repr=False)
    page: int | None
    metadata: dict[str, object]
    embedding: tuple[float, ...] = Field(repr=False)


class StoredDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: uuid.UUID
    content_sha256: str
    status: DocumentStatus
    created: bool
    retired_version: str | None = None
    """Set only when this write approved a new version and retired the previous one."""


class StaleChunk(BaseModel):
    """A chunk embedded with another model than the configured one."""

    model_config = ConfigDict(frozen=True)

    chunk_id: uuid.UUID
    content: str = Field(repr=False)
    embedding_model: str


class ChunkEmbedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: uuid.UUID
    previous_model: str
    embedding: tuple[float, ...] = Field(repr=False)


# ── Errors ──────────────────────────────────────────────────────────────────────────────────


class KbError(Exception):
    """A typed API error: HTTP status, a stable code and a fixed message (never input)."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class VersionConflict(Exception):
    """This source id and version exist with different bytes: a change needs a new version."""


class StatusConflict(Exception):
    """This source id and version exist with the same bytes but another status (for example a
    retired version uploaded again as approved). An upload never changes a stored version's
    status: approving it again means a new version (PR 5b's default, ADR 0010)."""


class ApprovalConflict(Exception):
    """A concurrent request approved another version of this source, and migration 0004's index
    let only one of the two commit. Nothing of this upload was stored; sending it again retires
    the version approved meanwhile."""
