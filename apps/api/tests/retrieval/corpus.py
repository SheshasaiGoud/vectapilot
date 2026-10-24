"""Planting a hand-checkable corpus in real Postgres, for the search tests (M2 H1, M3 H1).

Moved here unchanged from tests/retrieval/test_vector_search.py (M3 PR 3), so the vector and
full-text tests plant the same way; `Row.body` is the one addition (a chunk's own text, which
full-text search reads). The engines and the two test businesses are fixtures in
tests/retrieval/conftest.py.

Every planting is valid with and without migration 0004 (PR 5b, one approved version per source,
decided 2026-10-22): no business ever gets two approved versions of one source id. `plant` checks
it, so a new test cannot break the rule by accident. Each chunk's `ts_config` is its document's
language through the platform map (migration 0005), as ingestion sets it.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vectapilot_api.db import business_transaction
from vectapilot_api.ingestion.language_map import text_search_config
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.retrieval.types import SearchHit

MODEL: Final = "test-model-a"
OTHER_MODEL: Final = "test-model-b"

REGISTER: Final = text("INSERT INTO businesses (business_id) VALUES (:b)")
UPROOT: Final = (
    text("DELETE FROM documents WHERE business_id = :b"),  # chunks go with their document
    text("DELETE FROM businesses WHERE business_id = :b"),
)
INSERT_DOCUMENT: Final = text(
    "INSERT INTO documents (id, business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256)"
    " VALUES (:id, :b, :source_id, :version, :title, 'markdown', :content_kind, :language, :status,"
    " :audience, repeat('a', 64))"
    " RETURNING id"
)
INSERT_CHUNK: Final = text(
    "INSERT INTO chunks (id, business_id, document_id, chunk_index, content, page, embedding,"
    " embedding_model, embedding_dim, ts_config)"
    " VALUES (:id, :b, :document_id, :chunk_index, :content, :page, CAST(:embedding AS vector),"
    " :model, 1024, CAST(:ts_config AS regconfig))"
    " RETURNING id"
)
CHUNKS_OF: Final = text("SELECT count(*) FROM chunks WHERE business_id = :b")
BUSINESS_SETTING: Final = text("SELECT current_setting('app.business_id', true)")
SORTS_FIRST: Final = text("SELECT CAST(:first AS text) < CAST(:second AS text)")
LINGUISTIC: Final = (
    "premise: the database's default collation is linguistic, or this test cannot see a missing"
    ' COLLATE "C"'
)


@dataclass(frozen=True)
class Row:
    """One chunk to plant, with the fields of its document (one per source id and version)."""

    source_id: str
    chunk_index: int
    embedding: tuple[float, ...] = field(repr=False)
    version: str = "1"
    status: str = "approved"
    audience: str = "public"
    model: str = MODEL
    page: int | None = None
    language: str = "en"
    content_kind: str = "faq"
    body: str | None = None
    """The chunk's text; None: `Chunk <index> of <source id> v<version>` (M2's vector tests)."""

    @property
    def key(self) -> tuple[str, str, int]:
        """How the tests name a hit: (source id, version, chunk index)."""
        return (self.source_id, self.version, self.chunk_index)

    @property
    def title(self) -> str:
        return f"Title of {self.source_id} v{self.version}"

    @property
    def content(self) -> str:
        if self.body is not None:
            return self.body
        return f"Chunk {self.chunk_index} of {self.source_id} v{self.version}"


@dataclass(frozen=True)
class Ids:
    documents: dict[tuple[str, str], uuid.UUID]
    chunks: dict[tuple[str, str, int], uuid.UUID]


def ranked_id(rank: int) -> uuid.UUID:
    """A fresh version-4 UUID whose first byte is `rank` (0-255). Postgres compares uuids byte by
    byte, so ids of different ranks sort by rank; the random rest keeps every id unique."""
    return uuid.UUID(bytes=bytes([rank]) + uuid.uuid4().bytes[1:])


async def plant(
    engine: AsyncEngine,
    business_id: str,
    rows: Sequence[Row],
    *,
    document_ids: Mapping[tuple[str, str], uuid.UUID] | None = None,
    chunk_ids: Mapping[tuple[str, str, int], uuid.UUID] | None = None,
) -> Ids:
    """Write `rows` for `business_id`, in the order given, as one business transaction.

    `document_ids` (by source id and version) and `chunk_ids` (by `Row.key`) fix ids; every other
    id is random, as `gen_random_uuid()` would make it.
    """
    document_ids = document_ids or {}
    chunk_ids = chunk_ids or {}
    documents: dict[tuple[str, str], uuid.UUID] = {}
    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    approved: dict[str, str] = {}
    chunks: dict[tuple[str, str, int], uuid.UUID] = {}
    async with business_transaction(engine, business_id) as connection:
        for row in rows:
            document = (row.source_id, row.version)
            fields = (row.status, row.audience, row.language, row.content_kind)
            # One document per source id and version: its rows must agree on its fields.
            assert seen.setdefault(document, fields) == fields, (
                f"test bug: {document} planted with two statuses, audiences, languages or kinds"
            )
            # At most one approved version per source: valid with and without migration 0004.
            if row.status == "approved":
                assert approved.setdefault(row.source_id, row.version) == row.version, (
                    f"test bug: two approved versions of {row.source_id!r} (migration 0004)"
                )
            if document not in documents:
                documents[document] = (
                    await connection.execute(
                        INSERT_DOCUMENT,
                        {
                            "id": document_ids.get(document) or uuid.uuid4(),
                            "b": business_id,
                            "source_id": row.source_id,
                            "version": row.version,
                            "title": row.title,
                            "content_kind": row.content_kind,
                            "language": row.language,
                            "status": row.status,
                            "audience": row.audience,
                        },
                    )
                ).scalar_one()
            chunks[row.key] = (
                await connection.execute(
                    INSERT_CHUNK,
                    {
                        "id": chunk_ids.get(row.key) or uuid.uuid4(),
                        "b": business_id,
                        "document_id": documents[document],
                        "chunk_index": row.chunk_index,
                        "content": row.content,
                        "page": row.page,
                        "embedding": vector_literal(row.embedding),
                        "model": row.model,
                        "ts_config": text_search_config(row.language),  # migration 0005
                    },
                )
            ).scalar_one()
    assert set(document_ids) <= set(documents), "test bug: an id for a document not planted"
    assert set(chunk_ids) <= set(chunks), "test bug: an id for a chunk not planted"
    return Ids(documents, chunks)


async def sorts_first_by_default(engine: AsyncEngine, first: str, second: str) -> bool:
    """Whether the database's default collation puts `first` before `second`."""
    async with engine.connect() as connection:
        return bool(await connection.scalar(SORTS_FIRST, {"first": first, "second": second}))


def keys(hits: Sequence[SearchHit]) -> list[tuple[str, str, int]]:
    return [(hit.source_id, hit.version, hit.chunk_index) for hit in hits]


def scores(hits: Sequence[SearchHit]) -> list[float]:
    return [hit.score for hit in hits]
