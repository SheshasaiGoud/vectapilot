"""Where knowledge-base rows live: the `KbStore` protocol and its Postgres implementation.

Every method is one `Database.business_transaction` (ADR 0009): row-level security limits it to the
business, and each statement also names `business_id` (defence in depth). A document and its chunks
are written in ONE transaction, so a failure leaves neither. Tests use an in-memory store
(`tests/kb/fakes.py`); this one runs against real Postgres in CI (`tests/kb/test_postgres_store.py`
and the canary suite's write paths).
"""

import json
import uuid
from collections.abc import Sequence
from typing import Final, Protocol

from sqlalchemy import text

from vectapilot_api.db import Database
from vectapilot_api.kb.types import (
    ChunkEmbedding,
    NewChunk,
    NewDocument,
    StaleChunk,
    StoredDocument,
)


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
        """Insert the document and its chunks in one transaction. If the source id and version
        already exist (a concurrent upload), insert nothing and return the existing one."""
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


_FIND_DOCUMENT: Final = text(
    "SELECT id, content_sha256 FROM documents"
    " WHERE business_id = :business_id AND source_id = :source_id AND version = :version"
)
_INSERT_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256, review_by, metadata)"
    " VALUES (:business_id, :source_id, :version, :title, :source_type, :content_kind, :language,"
    " :status, :audience, :content_sha256, :review_by, CAST(:metadata AS jsonb))"
    " ON CONFLICT (business_id, source_id, version) DO NOTHING"
    " RETURNING id"
)
_INSERT_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, page, metadata,"
    " embedding, embedding_model, embedding_dim)"
    " VALUES (:business_id, :document_id, :chunk_index, :content, :page,"
    " CAST(:metadata AS jsonb), CAST(:embedding AS vector), :model, :dimension)"
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
        if row is None:
            return None
        return StoredDocument(document_id=row.id, content_sha256=row.content_sha256, created=False)

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
        async with self._database.business_transaction(business_id) as connection:
            document_id: uuid.UUID | None = (
                await connection.execute(_INSERT_DOCUMENT, params)
            ).scalar_one_or_none()
            if document_id is None:  # the same version arrived first through another request
                key = {name: params[name] for name in ("business_id", "source_id", "version")}
                row = (await connection.execute(_FIND_DOCUMENT, key)).one()
                return StoredDocument(
                    document_id=row.id, content_sha256=row.content_sha256, created=False
                )
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
                    }
                    for chunk in chunks
                ],
            )
        return StoredDocument(
            document_id=document_id, content_sha256=document.content_sha256, created=True
        )

    async def stored_models(self, business_id: str) -> frozenset[str]:
        async with self._database.business_transaction(business_id) as connection:
            result = await connection.execute(_STORED_MODELS, {"business_id": business_id})
            return frozenset(result.scalars().all())

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
