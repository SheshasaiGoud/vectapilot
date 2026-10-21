"""An in-memory `KbStore` for unit tests: the same contract as `PostgresKbStore`, per business."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from vectapilot_api.kb.types import (
    ChunkEmbedding,
    NewChunk,
    NewDocument,
    StaleChunk,
    StoredDocument,
)


@dataclass
class ChunkRow:
    business_id: str
    document_id: uuid.UUID
    chunk: NewChunk
    embedding: tuple[float, ...]
    model: str
    dimension: int


class InMemoryKbStore:
    """`fail` makes every method raise it (a database outage). `hide_from_find` makes
    `find_document` miss, as when a concurrent upload stores the version after the check."""

    def __init__(self) -> None:
        self.documents: dict[tuple[str, str, str], tuple[uuid.UUID, NewDocument]] = {}
        self.chunks: dict[uuid.UUID, ChunkRow] = {}
        self.fail: Exception | None = None
        self.hide_from_find = False

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def chunks_of(self, business_id: str) -> list[ChunkRow]:
        return [row for row in self.chunks.values() if row.business_id == business_id]

    async def find_document(
        self, business_id: str, source_id: str, version: str
    ) -> StoredDocument | None:
        self._check()
        found = self.documents.get((business_id, source_id, version))
        if found is None or self.hide_from_find:
            return None
        return StoredDocument(
            document_id=found[0], content_sha256=found[1].content_sha256, created=False
        )

    async def save_document(
        self,
        business_id: str,
        document: NewDocument,
        chunks: Sequence[NewChunk],
        *,
        model: str,
        dimension: int,
    ) -> StoredDocument:
        self._check()
        key = (business_id, document.source_id, document.version)
        if key in self.documents:
            document_id, stored = self.documents[key]
            return StoredDocument(
                document_id=document_id, content_sha256=stored.content_sha256, created=False
            )
        document_id = uuid.uuid4()
        self.documents[key] = (document_id, document)
        for chunk in chunks:
            self.chunks[uuid.uuid4()] = ChunkRow(
                business_id, document_id, chunk, chunk.embedding, model, dimension
            )
        return StoredDocument(
            document_id=document_id, content_sha256=document.content_sha256, created=True
        )

    async def stored_models(self, business_id: str) -> frozenset[str]:
        self._check()
        return frozenset(row.model for row in self.chunks_of(business_id))

    async def stale_chunks(self, business_id: str, model: str) -> list[StaleChunk]:
        self._check()
        return [
            StaleChunk(chunk_id=chunk_id, content=row.chunk.content, embedding_model=row.model)
            for chunk_id, row in self.chunks.items()
            if row.business_id == business_id and row.model != model
        ]

    async def replace_embeddings(
        self, business_id: str, updates: Sequence[ChunkEmbedding], *, model: str, dimension: int
    ) -> int:
        self._check()
        changed = 0
        for update in updates:
            row = self.chunks.get(update.chunk_id)
            if row and row.business_id == business_id and row.model == update.previous_model:
                row.embedding, row.model, row.dimension = update.embedding, model, dimension
                changed += 1
        return changed
