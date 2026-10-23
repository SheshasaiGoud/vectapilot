"""Ingest and reindex (M2 PR 5): load → chunk → embed → store, synchronous (workers are M9).

**Idempotent ingestion.** A business's (source id, version) is stored once (the database's unique
key). Re-sending the same bytes under the same version, with the same status and language, returns
the stored document and embeds nothing (no cost); different bytes under a stored version are a
`VersionConflict` (a change needs a new version), the same bytes with another status a
`StatusConflict` (an upload never changes a stored version's status, for example re-approving a
retired one: that is a new version), and the same bytes and status with another language a
`LanguageConflict` (the language decides the chunks' `ts_config` and `tsv`, migration 0005; the
tag is compared exactly as stored, so `en-IN` against a stored `en` is a conflict too). They are
checked in that order. The check runs before loading, and again inside the write (two concurrent
uploads).

**One approved version per source (ADR 0010).** Uploading a new version as approved retires the
source's previously approved version in the store's same transaction (`IngestResult.
retired_version` names it); a draft or retired upload retires nothing. A concurrent approval of
another version of the source is an `ApprovalConflict`.

**Reindex.** Chunks carry the model id they were embedded with (ADR 0008). After a model change,
`reindex` re-embeds every chunk whose model is not the configured one and switches them all in one
transaction, so search never sees a half-switched business.
"""

import hashlib
from collections.abc import Sequence
from typing import Final

from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.ingestion.chunking import DEFAULT_CHUNKING, chunk_document
from vectapilot_api.ingestion.loaders import DEFAULT_LIMITS, load_document
from vectapilot_api.ingestion.types import Chunk, ChunkingConfig, LoadLimits, SourceType
from vectapilot_api.kb.store import KbStore
from vectapilot_api.kb.types import (
    ChunkEmbedding,
    IngestParams,
    IngestResult,
    LanguageConflict,
    NewChunk,
    NewDocument,
    ReindexResult,
    StatusConflict,
    StoredDocument,
    VersionConflict,
)

INGEST_OPERATION: Final = "kb-ingest"
REINDEX_OPERATION: Final = "kb-reindex"


class KbService:
    """The knowledge-base use cases for one process. Limits and chunking are the platform defaults
    until business configs supply them (PR 3's starting values, tuned in PR 8)."""

    def __init__(
        self,
        store: KbStore,
        runner: EmbeddingRunner,
        *,
        limits: LoadLimits = DEFAULT_LIMITS,
        chunking: ChunkingConfig = DEFAULT_CHUNKING,
    ) -> None:
        self._store = store
        self._runner = runner
        self.limits = limits
        self._chunking = chunking

    async def ingest(
        self, business_id: str, data: bytes, source_type: SourceType, params: IngestParams
    ) -> IngestResult:
        sha256 = hashlib.sha256(data).hexdigest()
        existing = await self._store.find_document(business_id, params.source_id, params.version)
        if existing is not None:
            return _already_stored(existing, sha256, params)
        loaded = load_document(data, source_type, params.source_meta(), self.limits)
        chunks = chunk_document(loaded, self._chunking)
        embedded = await self._runner.embed(
            [chunk.content for chunk in chunks],
            input_type="document",
            business_id=business_id,
            operation=INGEST_OPERATION,
        )
        document = NewDocument(
            source_id=loaded.source_id,
            version=loaded.version,
            title=loaded.title,
            source_type=loaded.source_type,
            content_kind=params.content_kind,
            language=loaded.language,
            status=params.status,
            audience=params.audience,
            content_sha256=loaded.content_sha256,
            review_by=params.review_by,
            metadata={} if loaded.page_count is None else {"page_count": loaded.page_count},
        )
        stored = await self._store.save_document(
            business_id,
            document,
            _new_chunks(chunks, embedded.vectors),
            model=embedded.model,
            dimension=embedded.dimension,
        )
        if not stored.created:
            return _already_stored(stored, sha256, params)
        return IngestResult(
            document_id=stored.document_id,
            source_id=params.source_id,
            version=params.version,
            created=True,
            chunks_written=len(chunks),
            embedding_model=embedded.model,
            retired_version=stored.retired_version,
        )

    async def reindex(self, business_id: str) -> ReindexResult:
        stale = await self._store.stale_chunks(business_id, self._runner.model)
        if not stale:
            return ReindexResult(
                embedding_model=self._runner.model,
                dimension=self._runner.dimension,
                chunks_reindexed=0,
            )
        embedded = await self._runner.embed(
            [chunk.content for chunk in stale],
            input_type="document",
            business_id=business_id,
            operation=REINDEX_OPERATION,
        )
        updates = [
            ChunkEmbedding(
                chunk_id=chunk.chunk_id, previous_model=chunk.embedding_model, embedding=vector
            )
            for chunk, vector in zip(stale, embedded.vectors, strict=True)
        ]
        changed = await self._store.replace_embeddings(
            business_id, updates, model=embedded.model, dimension=embedded.dimension
        )
        return ReindexResult(
            embedding_model=embedded.model, dimension=embedded.dimension, chunks_reindexed=changed
        )


def _already_stored(stored: StoredDocument, sha256: str, params: IngestParams) -> IngestResult:
    if stored.content_sha256 != sha256:
        raise VersionConflict()
    if stored.status != params.status:
        raise StatusConflict()
    if stored.language != params.language:  # as stored, not through the language map
        raise LanguageConflict()
    return IngestResult(
        document_id=stored.document_id,
        source_id=params.source_id,
        version=params.version,
        created=False,
        chunks_written=0,
        embedding_model=None,
        retired_version=None,
    )


def _new_chunks(chunks: Sequence[Chunk], vectors: Sequence[tuple[float, ...]]) -> list[NewChunk]:
    return [
        NewChunk(
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            page=chunk.page,
            metadata={
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "heading_path": list(chunk.heading_path),
                **({} if chunk.row is None else {"row": chunk.row}),
            },
            embedding=vector,
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
