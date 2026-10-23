"""Where knowledge-base rows live: the `KbStore` protocol and its Postgres implementation.

Every method is one `Database.business_transaction` (ADR 0009): row-level security limits it to the
business, and each statement also names `business_id` (defence in depth). A document and its chunks
are written in ONE transaction, so a failure leaves neither. Tests use an in-memory store
(`tests/kb/fakes.py`); this one runs against real Postgres in CI (`tests/kb/test_postgres_store.py`
and the canary suite's write paths).

**One approved version per source (ADR 0010).** Saving an approved document first retires the
business's approved version of the same source, then inserts the new one, in that same
transaction: migration 0004's partial unique index is checked row by row, so the old row must
leave it before the new one enters. Any failure afterwards (a chunk, the commit) rolls the retire
back with everything else, so the old version stays approved. When a concurrent request approves
another version first, the index refuses this insert: `ApprovalConflict`, never a raw database
error.

**Full-text config (M3 PR 2, ADR 0014).** Each chunk's `ts_config` is its document's language
through the platform map (`ingestion/language_map.py`), bound as `CAST(:ts_config AS regconfig)`;
Postgres computes `tsv` from it (migration 0005). Nothing here selects `ts_config` raw: a read
casts it to text first (asyncpg's `regconfig` codec is not something this code relies on).
"""

import json
import uuid
from collections.abc import Sequence
from typing import Final, Protocol

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.db import Database
from vectapilot_api.ingestion.language_map import text_search_config
from vectapilot_api.kb.types import (
    ApprovalConflict,
    ChunkEmbedding,
    NewChunk,
    NewDocument,
    StaleChunk,
    StoredDocument,
)

ONE_APPROVED_INDEX: Final = "documents_one_approved_version"
"""Migration 0004's partial unique index on (business_id, source_id) WHERE status = 'approved'
(a test checks the migration uses the same name)."""
UNIQUE_VIOLATION: Final = "23505"
"""Postgres' SQLSTATE for a unique index or constraint violation."""


class KbStore(Protocol):
    async def find_document(
        self, business_id: str, source_id: str, version: str
    ) -> StoredDocument | None:
        """The business's document with this source id and version, if any (`created` False)."""
        ...

    async def save_document(
        self,
        business_id: str,
        document: NewDocument,
        chunks: Sequence[NewChunk],
        *,
        model: str,
        dimension: int,
    ) -> StoredDocument:
        """Insert the document and its chunks in one transaction. An approved document retires
        the source's previously approved version in that same transaction (`retired_version`).
        If the source id and version already exist (a concurrent upload), change nothing and
        return the existing one. `ApprovalConflict` if a concurrent request approved another
        version of the source first."""
        ...

    async def stored_models(self, business_id: str) -> frozenset[str]:
        """The distinct `chunks.embedding_model` values of the business."""
        ...

    async def stale_chunks(self, business_id: str, model: str) -> list[StaleChunk]:
        """The business's chunks embedded with any model other than `model`."""
        ...

    async def replace_embeddings(
        self, business_id: str, updates: Sequence[ChunkEmbedding], *, model: str, dimension: int
    ) -> int:
        """Set each chunk's vector and model in one transaction, only where it still carries
        `previous_model`. Returns how many chunks changed."""
        ...


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form, `[x,y,...]`; `repr` keeps every digit of a float."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def is_one_approved_violation(error: IntegrityError) -> bool:
    """Whether `error` is migration 0004's index refusing a second approved version of a source.

    SQLAlchemy 2.1's asyncpg adapter keeps the SQLSTATE on `error.orig` and asyncpg's own error,
    which names the violated index, on `error.orig.orig`. Read with `getattr`: an unknown shape is
    False (the error stays a database error, 503), never a wrong 409.
    """
    adapted = error.orig
    driver = getattr(adapted, "orig", None)
    return (
        getattr(adapted, "sqlstate", None) == UNIQUE_VIOLATION
        and getattr(driver, "constraint_name", None) == ONE_APPROVED_INDEX
    )


class _StoredMeanwhile(Exception):
    """Raised inside a save's transaction to roll it back (the retire included) when the version
    turned out to be stored already; carries that stored document out."""

    def __init__(self, stored: StoredDocument) -> None:
        super().__init__("stored meanwhile")
        self.stored = stored


_FIND_DOCUMENT: Final = text(
    "SELECT id, content_sha256, status, language FROM documents"
    " WHERE business_id = :business_id AND source_id = :source_id AND version = :version"
)
# Never the version being written: only "the previously approved version" leaves the index.
_RETIRE_APPROVED: Final = text(
    "UPDATE documents SET status = 'retired'"
    " WHERE business_id = :business_id AND source_id = :source_id AND status = 'approved'"
    " AND version <> :version"
    " RETURNING version"
)
_INSERT_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256, review_by, metadata)"
    " VALUES (:business_id, :source_id, :version, :title, :source_type, :content_kind, :language,"
    " :status, :audience, :content_sha256, :review_by, CAST(:metadata AS jsonb))"
    " ON CONFLICT (business_id, source_id, version) DO NOTHING"
    " RETURNING id"
)
# `tsv` is generated by Postgres from `content` and `ts_config` (migration 0005): never named here.
_INSERT_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, page, metadata,"
    " embedding, embedding_model, embedding_dim, ts_config)"
    " VALUES (:business_id, :document_id, :chunk_index, :content, :page,"
    " CAST(:metadata AS jsonb), CAST(:embedding AS vector), :model, :dimension,"
    " CAST(:ts_config AS regconfig))"
)
_STORED_MODELS: Final = text(
    "SELECT DISTINCT embedding_model FROM chunks WHERE business_id = :business_id"
)
_STALE_CHUNKS: Final = text(
    "SELECT id, content, embedding_model FROM chunks"
    " WHERE business_id = :business_id AND embedding_model <> :model"
    " ORDER BY document_id, chunk_index"
)
_REPLACE_EMBEDDING: Final = text(
    "UPDATE chunks SET embedding = CAST(:embedding AS vector), embedding_model = :model,"
    " embedding_dim = :dimension"
    " WHERE business_id = :business_id AND id = :chunk_id AND embedding_model = :previous_model"
    " RETURNING id"
)


async def stored_models_in(connection: AsyncConnection, business_id: str) -> frozenset[str]:
    """The business's distinct `chunks.embedding_model` values, on the caller's connection (inside
    its business transaction; the `WHERE` names the business on top of RLS). The one copy of this
    query: reindex (`stored_models` below) and search (`retrieval/search.py`) decide from it, so
    the one-model guard (ADR 0008) cannot see two answers."""
    result = await connection.execute(_STORED_MODELS, {"business_id": business_id})
    return frozenset(result.scalars().all())


class PostgresKbStore:
    """`KbStore` over the API's `Database` (the app role, under RLS)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_document(
        self, business_id: str, source_id: str, version: str
    ) -> StoredDocument | None:
        params = {"business_id": business_id, "source_id": source_id, "version": version}
        async with self._database.business_transaction(business_id) as connection:
            row = (await connection.execute(_FIND_DOCUMENT, params)).first()
        return None if row is None else _existing(row)

    async def save_document(
        self,
        business_id: str,
        document: NewDocument,
        chunks: Sequence[NewChunk],
        *,
        model: str,
        dimension: int,
    ) -> StoredDocument:
        values = document.model_dump(mode="json", exclude={"review_by", "metadata"})
        params = {
            **values,
            "business_id": business_id,
            "review_by": document.review_by,
            "metadata": json.dumps(document.metadata),
        }
        key = {name: params[name] for name in ("business_id", "source_id", "version")}
        ts_config = text_search_config(document.language)  # one document, one language
        conflict = False
        try:
            async with self._database.business_transaction(business_id) as connection:
                retired: str | None = None
                if document.status == "approved":  # out of the index before the new row enters
                    result = await connection.execute(_RETIRE_APPROVED, key)
                    retired = result.scalar_one_or_none()  # at most one, by the index
                document_id: uuid.UUID | None = (
                    await connection.execute(_INSERT_DOCUMENT, params)
                ).scalar_one_or_none()
                if document_id is None:  # the same version arrived first through another request
                    row = (await connection.execute(_FIND_DOCUMENT, key)).one()
                    raise _StoredMeanwhile(_existing(row))  # undoes the retire above
                await connection.execute(
                    _INSERT_CHUNK,
                    [
                        {
                            "business_id": business_id,
                            "document_id": document_id,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            "page": chunk.page,
                            "metadata": json.dumps(chunk.metadata),
                            "embedding": vector_literal(chunk.embedding),
                            "model": model,
                            "dimension": dimension,
                            "ts_config": ts_config,
                        }
                        for chunk in chunks
                    ],
                )
        except _StoredMeanwhile as meanwhile:
            return meanwhile.stored
        except IntegrityError as error:
            if not is_one_approved_violation(error):
                raise
            conflict = True
        if conflict:
            # Outside the `except`: nothing is chained (the database's text holds input).
            raise ApprovalConflict()
        return StoredDocument(
            document_id=document_id,
            content_sha256=document.content_sha256,
            status=document.status,
            language=document.language,
            created=True,
            retired_version=retired,
        )

    async def stored_models(self, business_id: str) -> frozenset[str]:
        async with self._database.business_transaction(business_id) as connection:
            return await stored_models_in(connection, business_id)

    async def stale_chunks(self, business_id: str, model: str) -> list[StaleChunk]:
        params = {"business_id": business_id, "model": model}
        async with self._database.business_transaction(business_id) as connection:
            rows = (await connection.execute(_STALE_CHUNKS, params)).all()
        return [
            StaleChunk(chunk_id=row.id, content=row.content, embedding_model=row.embedding_model)
            for row in rows
        ]

    async def replace_embeddings(
        self, business_id: str, updates: Sequence[ChunkEmbedding], *, model: str, dimension: int
    ) -> int:
        changed = 0
        async with self._database.business_transaction(business_id) as connection:
            for update in updates:  # one statement each: RETURNING tells exactly what changed
                params = {
                    "business_id": business_id,
                    "chunk_id": update.chunk_id,
                    "previous_model": update.previous_model,
                    "embedding": vector_literal(update.embedding),
                    "model": model,
                    "dimension": dimension,
                }
                if (await connection.execute(_REPLACE_EMBEDDING, params)).first() is not None:
                    changed += 1
        return changed


def _existing(row: Row[tuple[uuid.UUID, str, str, str]]) -> StoredDocument:
    """A stored document read by `_FIND_DOCUMENT` (id, content_sha256, status, language)."""
    return StoredDocument(
        document_id=row.id,
        content_sha256=row.content_sha256,
        status=row.status,
        language=row.language,
        created=False,
    )
