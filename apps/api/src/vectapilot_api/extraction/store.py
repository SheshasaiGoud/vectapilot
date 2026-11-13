"""Storing one extraction under row-level security (ADR 0017 decision 6, decided D6 (a); E3, E9,
E10; M6 PR 3).

**What is stored (D6 (a), migration 0007).** One `extractions` row (metadata: kind, template,
prompt and rules versions, status, E3's failure, the file's SHA-256, pages, image pages, trace id
and the retention that applied) and one `extraction_fields` row per template field (status,
level, reasons, and only the value, its page and the quote's label-to-value span). Never the file,
its text or a whole quote: the bytes are hashed by the caller and dropped (E10).

**One transaction, one business.** The caller names the business (the route: `X-Business-Id`)
and an outcome of any other is refused before anything is sent. Both inserts run in
`Database.business_transaction` for that business (RLS's `WITH CHECK` refuses any other), and each
names `:business_id` itself.
A failure rolls back both, so a 503 stores nothing (E3: "a database failure is a 503 and stores
nothing"); the caller maps `SQLAlchemyError` and `OSError` to that 503, never their text.

**The database decides the id and the times.** The app role may insert named columns only
(migration 0007): never an id, `created_at` or `expires_at`. The database draws the id and sets
`expires_at` from `retention_days` (the business file's, 1-90); `RETURNING` hands them back (the
app role's `SELECT` grant, and the expiry policy passes: a new row expires at least a day later).

**A caller bug raises `ValueError` before any statement:** an outcome of another business than
the one named, a file hash that is not one or differs from the one the text reader computed, a
malformed trace id, a retention outside 1-90, or a value or span that does not fit its column
(`service.storable`: the service never returns one, so the database never refuses a value as a
503). Values and spans are untrusted data (E7): stored as given, never logged or put in an
error.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from sqlalchemy import text

from vectapilot_api.db import Database
from vectapilot_api.extraction.config import RETENTION_DAYS
from vectapilot_api.extraction.routing import Reason
from vectapilot_api.extraction.service import ExtractionOutcome, FieldResult, storable

_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_TRACE_ID: Final = re.compile(r"[0-9a-f]{32}")
REASON_ORDER: Final[Mapping[Reason, int]] = {reason: i for i, reason in enumerate(Reason)}
"""Reasons are stored and returned in `Reason`'s order, so equal sets give equal rows."""

INSERT_EXTRACTION: Final = text(
    "INSERT INTO extractions (business_id, kind, template_version, prompt_version,"
    " rules_version, status, failure_reason, file_sha256, pages, image_pages, trace_id,"
    " retention_days)"
    " VALUES (:business_id, :kind, :template_version, :prompt_version, :rules_version, :status,"
    " :failure_reason, :file_sha256, :pages, CAST(:image_pages AS integer[]), :trace_id,"
    " :retention_days)"
    " RETURNING id, created_at, expires_at"
)
INSERT_FIELD: Final = text(
    "INSERT INTO extraction_fields (business_id, extraction_id, field, status, level, reasons,"
    " value, span, page)"
    " VALUES (:business_id, :extraction_id, :field, :status, :level, CAST(:reasons AS text[]),"
    " to_jsonb(CAST(:value AS text)), :span, :page)"
)
"""`to_jsonb(NULL)` is SQL NULL: a missing value is no value, never a JSON `null`."""
STATEMENTS: Final = (INSERT_EXTRACTION, INSERT_FIELD)
"""Every statement a save sends; each names `:business_id` (a test checks)."""


@dataclass(frozen=True)
class StoredExtraction:
    """What the database decided for a stored extraction."""

    id: UUID
    created_at: datetime
    expires_at: datetime


class ExtractionStore(Protocol):
    """Where the route stores an extraction (`PostgresExtractionStore`; tests pass stand-ins)."""

    async def save(
        self,
        outcome: ExtractionOutcome,
        *,
        business_id: str,
        file_sha256: str,
        trace_id: str,
        retention_days: int,
    ) -> StoredExtraction: ...


def ordered(reasons: frozenset[Reason]) -> list[str]:
    """`reasons` as stored and returned: their values, in `Reason`'s order."""
    return [reason.value for reason in sorted(reasons, key=REASON_ORDER.__getitem__)]


def check_save(
    outcome: ExtractionOutcome,
    *,
    business_id: str,
    file_sha256: str,
    trace_id: str,
    retention_days: int,
) -> None:
    """A caller bug raises `ValueError` before anything is sent (module docstring); its message
    is fixed, never a value."""
    if outcome.business_id != business_id:
        raise ValueError("the outcome belongs to another business")
    if _SHA256.fullmatch(file_sha256) is None:
        raise ValueError("file_sha256 is a lowercase hex SHA-256")
    if outcome.sha256 is not None and outcome.sha256 != file_sha256:
        raise ValueError("file_sha256 differs from the hash of the pages that were read")
    if _TRACE_ID.fullmatch(trace_id) is None or trace_id == "0" * 32:
        raise ValueError("trace_id must be a W3C trace id (32 lowercase hex, not all zeros)")
    low, high = RETENTION_DAYS
    if not low <= retention_days <= high:
        raise ValueError(f"retention_days is between {low} and {high}")
    if not storable(outcome):
        raise ValueError("a value or span does not fit its column")


def extraction_row(
    outcome: ExtractionOutcome, *, file_sha256: str, trace_id: str, retention_days: int
) -> dict[str, object]:
    return {
        "business_id": outcome.business_id,
        "kind": outcome.kind,
        "template_version": outcome.template_version,
        "prompt_version": outcome.prompt_version,
        "rules_version": outcome.rules_version,
        "status": outcome.status.value,
        "failure_reason": None if outcome.failure is None else outcome.failure.value,
        "file_sha256": file_sha256,
        "pages": outcome.pages,
        "image_pages": list(outcome.image_pages),
        "trace_id": trace_id,
        "retention_days": retention_days,
    }


def field_row(business_id: str, extraction_id: UUID, result: FieldResult) -> dict[str, object]:
    return {
        "business_id": business_id,
        "extraction_id": extraction_id,
        "field": result.field,
        "status": result.status.value,
        "level": None if result.level is None else result.level.value,
        "reasons": ordered(result.reasons),
        "value": result.value,
        "span": result.span,
        "page": result.page,
    }


class PostgresExtractionStore:
    """`ExtractionStore` on the API's database, as the app role (module docstring)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(
        self,
        outcome: ExtractionOutcome,
        *,
        business_id: str,
        file_sha256: str,
        trace_id: str,
        retention_days: int,
    ) -> StoredExtraction:
        """Store `outcome` and its fields for `business_id` in one transaction; the database's
        id and times."""
        check_save(
            outcome,
            business_id=business_id,
            file_sha256=file_sha256,
            trace_id=trace_id,
            retention_days=retention_days,
        )
        async with self._database.business_transaction(business_id) as connection:
            row = (
                await connection.execute(
                    INSERT_EXTRACTION,
                    extraction_row(
                        outcome,
                        file_sha256=file_sha256,
                        trace_id=trace_id,
                        retention_days=retention_days,
                    ),
                )
            ).one()
            stored = StoredExtraction(row.id, row.created_at, row.expires_at)
            rows = [field_row(business_id, stored.id, result) for result in outcome.fields]
            if rows:  # executemany refuses an empty list (a template has at least one field)
                await connection.execute(INSERT_FIELD, rows)
        return stored
