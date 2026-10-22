"""Test doubles for PR 6's search and answer, without Postgres and without any SQL.

- `StandInSearch` stands in for H1's `vector_search` (same signature): it returns hits prepared
  per business and records every call. It runs no query: H1's real search is shesha's
  [HAND-WRITE] (`retrieval/vector.py`), tested against Postgres by H1's own tests.
- `FakeDatabase` stands in for `db.Database.business_transaction`: it records which business each
  transaction was opened for and yields a marker object as the connection.
- `StoredModels` stands in for `kb.store.stored_models_in`.
- `hit(...)` builds a `SearchHit`.
"""

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from tests.embeddings.scripted import POLICY, FakeClock, ScriptedEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.search import SearchService
from vectapilot_api.retrieval.types import SearchHit, VectorQuery


def hit(
    content: str,
    score: float,
    *,
    source_id: str = "faq",
    version: str = "1",
    title: str = "Opening hours",
    chunk_index: int = 0,
    page: int | None = None,
    language: str = "en",
    content_kind: ContentKind = ContentKind.FAQ,
    embedding_model: str = "fake-embedding-1024",
    chunk_id: uuid.UUID | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id or uuid.uuid4(),
        document_id=uuid.uuid4(),
        source_id=source_id,
        version=version,
        title=title,
        language=language,
        content_kind=content_kind,
        page=page,
        chunk_index=chunk_index,
        content=content,
        score=score,
        embedding_model=embedding_model,
    )


class Connection:
    """The marker a `FakeDatabase` transaction yields: it knows its business, nothing else."""

    def __init__(self, business_id: str) -> None:
        self.business_id = business_id


@dataclass
class FakeDatabase:
    """`business_transaction` that records the business and yields a `Connection` marker;
    `fail` makes opening one raise it (a database outage)."""

    opened: list[str] = field(default_factory=list)
    fail: Exception | None = None

    @asynccontextmanager
    async def business_transaction(self, business_id: str) -> AsyncIterator[AsyncConnection]:
        if self.fail is not None:
            raise self.fail
        self.opened.append(business_id)
        yield cast(AsyncConnection, Connection(business_id))


@dataclass
class StoredModels:
    """The distinct model ids each business's chunks carry (default: none, an empty KB)."""

    models: Mapping[str, frozenset[str]] = field(default_factory=dict)
    calls: list[tuple[Any, str]] = field(default_factory=list)

    async def __call__(self, connection: AsyncConnection, business_id: str) -> frozenset[str]:
        self.calls.append((connection, business_id))
        return self.models.get(business_id, frozenset())


@dataclass
class StandInSearch:
    """Stands in for H1's `vector_search`: no SQL, prepared hits per business (at most `k`, in
    the given order), every call recorded. `raises` makes it raise instead (H1's stub raises
    `NotImplementedError`)."""

    hits: Mapping[str, Sequence[SearchHit]] = field(default_factory=dict)
    raises: BaseException | None = None
    calls: list[tuple[Any, VectorQuery]] = field(default_factory=list)

    async def __call__(self, connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        self.calls.append((connection, query))
        if self.raises is not None:
            raise self.raises
        return list(self.hits.get(query.business_id, ()))[: query.k]


@dataclass
class Search:
    """A `SearchService` over stand-ins, and the stand-ins themselves."""

    service: SearchService
    embedder: ScriptedEmbedder
    database: FakeDatabase
    stored: StoredModels
    standin: StandInSearch


def build_search(
    hits: Mapping[str, Sequence[SearchHit]] | None = None,
    *,
    embedder: ScriptedEmbedder | None = None,
    stored: Mapping[str, frozenset[str]] | None = None,
    raises: BaseException | None = None,
) -> Search:
    """`stored` defaults to: every business with hits has the embedder's model only."""
    embedder = embedder or ScriptedEmbedder()
    hits = hits or {}
    clock = FakeClock()
    runner = EmbeddingRunner(
        embedder,
        embedder._fake._spec,
        POLICY,
        deadline_s=100.0,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )
    models = StoredModels(
        stored if stored is not None else {b: frozenset({embedder.model}) for b in hits}
    )
    standin = StandInSearch(hits, raises)
    database = FakeDatabase()
    service = SearchService(database, runner, search=standin, stored_models=models)
    return Search(service, embedder, database, models, standin)
