"""What the eval harness reads and writes in Postgres beyond the product's own paths (M2 PR 8).

Ingestion and search go through the product (`KbService`, `SearchService`, `AnswerService`); this
module holds only what an eval needs on top, every statement in one business transaction as the
app role (ADR 0009), except registering a business, which only the owner may do.

- **`document_rows`**: a hit's document row: its business, status and audience. `SearchHit`
  carries none of them (H1's contract stays as decided), and the question's business must never
  be assumed for a hit, so the harness looks the documents up **on the search's own connection,
  inside the same business transaction** (`harness.RecordingSearch`). The statement deliberately
  names no business in its `WHERE`: its point is to read each row's own `business_id`. RLS still
  limits it to the transaction's business, so a document of another business comes back as
  missing, which the harness treats as a leak too. Status and audience show a hit no customer may
  see (a staff-only, draft or retired document: `scoring.check_hit_rows`).
- **`stored_chunks`**: every chunk of a business with its document's identity, status and
  audience. The harness compares them with the corpus chunked at the run's config (a stale chunk
  size or chunker is refused) and finds evidence keys that no stored chunk holds (unscorable).
- **`register_businesses`** (owner): the eval businesses (`namespace.py`) in the registry.
- **`delete_documents`**: `--fresh` only (`run` or `ingest`). It refuses any id that does not
  have the derived form `<business>-ev-<10 hex>` (`namespace.is_eval_business_id`), so a dataset
  or production business id is never deleted, whoever calls it; `harness.execute` also refuses
  `--fresh` in production.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from vectapilot_api.db import Database, business_transaction
from vectapilot_api.evalrun.namespace import is_eval_business_id
from vectapilot_api.evalrun.scoring import CUSTOMER_AUDIENCES, DocumentRow

_DOCUMENT_ROWS: Final = text(
    # No business filter on purpose (module docstring): the row's own business_id is the answer.
    "SELECT id, business_id, status, audience FROM documents WHERE id = ANY(:ids)"
).bindparams(bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))

_STORED_CHUNKS: Final = text(
    "SELECT c.business_id, d.source_id, d.version, d.status, d.audience, c.chunk_index,"
    " c.content, c.embedding_model"
    " FROM chunks c JOIN documents d ON d.business_id = c.business_id AND d.id = c.document_id"
    " WHERE c.business_id = :business_id"
    ' ORDER BY d.source_id COLLATE "C", d.version COLLATE "C", c.chunk_index'
)

_REGISTER: Final = text(
    "INSERT INTO businesses (business_id) VALUES (:business_id) ON CONFLICT DO NOTHING"
    " RETURNING business_id"
)

_DELETE_DOCUMENTS: Final = text(
    # Chunks go with their document (ON DELETE CASCADE, migration 0002).
    "DELETE FROM documents WHERE business_id = :business_id RETURNING id"
)


async def document_rows(
    connection: AsyncConnection, document_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, DocumentRow]:
    """{document id: its row's business, status and audience} for the documents this transaction
    can see (one statement, on the caller's connection)."""
    if not document_ids:
        return {}
    rows = await connection.execute(_DOCUMENT_ROWS, {"ids": list(document_ids)})
    return {
        row.id: DocumentRow(business_id=row.business_id, status=row.status, audience=row.audience)
        for row in rows
    }


@dataclass(frozen=True)
class StoredChunk:
    """One stored chunk and its document's identity (no vector)."""

    business_id: str
    source_id: str
    version: str
    status: str
    audience: str
    chunk_index: int
    content: str = field(repr=False)
    embedding_model: str

    @property
    def answers_customers(self) -> bool:
        """What a customer's search may return (H1's filter): approved, and an audience in
        `VISIBLE_AUDIENCES["customer"]`."""
        return self.status == "approved" and self.audience in CUSTOMER_AUDIENCES


class EvalStore(Protocol):
    """What the harness reads and writes as the app role (tests use an in-memory one)."""

    async def stored_chunks(self, business_id: str) -> tuple[StoredChunk, ...]: ...

    async def business_exists(self, business_id: str) -> bool: ...

    async def delete_documents(self, business_id: str) -> int: ...


class PostgresEvalStore:
    """`EvalStore` over the API's `Database` (the app role, under RLS)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def stored_chunks(self, business_id: str) -> tuple[StoredChunk, ...]:
        async with self._database.business_transaction(business_id) as connection:
            rows = await connection.execute(_STORED_CHUNKS, {"business_id": business_id})
            return tuple(
                StoredChunk(
                    business_id=row.business_id,
                    source_id=row.source_id,
                    version=row.version,
                    status=row.status,
                    audience=row.audience,
                    chunk_index=row.chunk_index,
                    content=row.content,
                    embedding_model=row.embedding_model,
                )
                for row in rows
            )

    async def business_exists(self, business_id: str) -> bool:
        return await self._database.business_exists(business_id)

    async def delete_documents(self, business_id: str) -> int:
        """Delete every document of the eval business `business_id`; refuse (`ValueError`, before
        any query) an id that is not of the derived eval form."""
        if not is_eval_business_id(business_id):
            raise ValueError(
                f"refusing to delete the documents of {business_id}: --fresh deletes only derived"
                " eval business ids (<business>-ev-<10 hex>)"
            )
        async with self._database.business_transaction(business_id) as connection:
            result = await connection.execute(_DELETE_DOCUMENTS, {"business_id": business_id})
            return len(result.all())


async def register_businesses(owner_engine: AsyncEngine, business_ids: Sequence[str]) -> list[str]:
    """Add the businesses missing from the registry, as the owner (the app role can only read
    it); return the ones added. Idempotent."""
    added: list[str] = []
    for business_id in business_ids:
        async with business_transaction(owner_engine, business_id) as connection:
            result = await connection.execute(_REGISTER, {"business_id": business_id})
            if result.first() is not None:
                added.append(business_id)
    return added
