"""Test doubles for PR 6's search and answer and M3 PR 4's hybrid search, without Postgres and
without any SQL.

- `StandInSearch` stands in for H1's `vector_search` (same signature): it returns hits prepared
  per business and records every call. It runs no query: H1's real search is shesha's
  [HAND-WRITE] (`retrieval/vector.py`), tested against Postgres by H1's own tests.
- `StandInTextSearch` does the same for M3 H1's `text_search`, and `StandInFuse` stands in for M3
  H2's `rrf_fuse`. Both are shesha's [HAND-WRITE]s, not written yet: these are deliberately not
  them (no full-text matching, no RRF arithmetic), so the hybrid wiring can be tested before H1
  and H2 exist, and nothing here can be copied into them.
- `FakeDatabase` stands in for `db.Database.business_transaction`: it records which business each
  transaction was opened for (and whether as a snapshot) and yields a marker object as the
  connection.
- `StoredModels` stands in for `kb.store.stored_models_in`.
- `hit(...)` builds a `SearchHit`.
"""

import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from tests.embeddings.scripted import POLICY, FakeClock, ScriptedEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.search import RetrievalConfig, SearchService
from vectapilot_api.retrieval.types import FusedHit, SearchHit, TextQuery, VectorQuery


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
    """`business_transaction` that records the business (and, in `snapshots`, whether it was
    opened as a snapshot, M3 PR 4) and yields a `Connection` marker; `fail` makes opening one
    raise it (a database outage). With `clock` set, a transaction that ends normally reads it
    once on the way out, as the commit (so a duration that must include the commit is
    testable)."""

    opened: list[str] = field(default_factory=list)
    fail: Exception | None = None
    snapshots: list[bool] = field(default_factory=list)
    clock: Callable[[], float] | None = None

    @asynccontextmanager
    async def business_transaction(
        self, business_id: str, *, snapshot: bool = False
    ) -> AsyncIterator[AsyncConnection]:
        if self.fail is not None:
            raise self.fail
        self.opened.append(business_id)
        self.snapshots.append(snapshot)
        yield cast(AsyncConnection, Connection(business_id))
        # Reached on a normal exit only (an exception in the block is raised at the `yield`):
        # the commit, which takes one reading.
        if self.clock is not None:
            self.clock()


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
class StandInTextSearch:
    """Stands in for M3 H1's `text_search`, as `StandInSearch` does for the vector leg: no SQL, no
    matching, prepared hits per business (at most `k`, in the given order), every call
    recorded. `raises` makes it raise instead (H1's stub raises `NotImplementedError`)."""

    hits: Mapping[str, Sequence[SearchHit]] = field(default_factory=dict)
    raises: BaseException | None = None
    calls: list[tuple[Any, TextQuery]] = field(default_factory=list)

    async def __call__(self, connection: AsyncConnection, query: TextQuery) -> list[SearchHit]:
        self.calls.append((connection, query))
        if self.raises is not None:
            raise self.raises
        return list(self.hits.get(query.business_id, ()))[: query.k]


@dataclass
class StandInFuse:
    """Stands in for M3 H2's `rrf_fuse` (same signature), and is on purpose not Reciprocal Rank
    Fusion: no 1/(k + rank) arithmetic, no tie rules, no duplicate checks, and `k` is ignored.

    It keeps each distinct chunk where it is first met, list by list (so the vector list first),
    up to `limit`; `rrf_score` is a placeholder, 1/position in its output; `ranks` say where each
    hit stands in each list, as `FusedHit` requires. With `fused` set it returns that prepared list
    whatever it is given (a test that needs particular RRF scores). `raises` makes it raise
    instead (H2's stub raises `NotImplementedError`). Every call is recorded."""

    fused: Sequence[FusedHit] | None = None
    raises: BaseException | None = None
    calls: list[tuple[list[list[SearchHit]], int, int]] = field(default_factory=list)

    def __call__(
        self, ranked_lists: Sequence[Sequence[SearchHit]], *, k: int, limit: int
    ) -> list[FusedHit]:
        lists = [list(ranked) for ranked in ranked_lists]
        self.calls.append((lists, k, limit))
        if self.raises is not None:
            raise self.raises
        if self.fused is not None:
            return list(self.fused)
        first: dict[uuid.UUID, SearchHit] = {}
        for ranked in lists:
            for item in ranked:
                first.setdefault(item.chunk_id, item)
        return [
            FusedHit(
                hit=item,
                rrf_score=Fraction(1, position),
                ranks=tuple(_rank_in(ranked, item.chunk_id) for ranked in lists),
            )
            for position, item in enumerate(list(first.values())[:limit], start=1)
        ]


def _rank_in(ranked: Sequence[SearchHit], chunk_id: uuid.UUID) -> int | None:
    return next((n for n, item in enumerate(ranked, start=1) if item.chunk_id == chunk_id), None)


def fused(item: SearchHit, rrf_score: Fraction, ranks: tuple[int | None, ...]) -> FusedHit:
    """A prepared fused hit, for `StandInFuse(fused=[...])`."""
    return FusedHit(hit=item, rrf_score=rrf_score, ranks=ranks)


class TickingClock:
    """A monotonic clock for stage durations: each reading is `step` seconds after the last."""

    def __init__(self, step: float = 0.001) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


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


@dataclass
class HybridSearch(Search):
    """A hybrid `SearchService` over stand-ins for both legs and the fusion."""

    text: StandInTextSearch
    fuse: StandInFuse


HYBRID: RetrievalConfig = RetrievalConfig(mode="hybrid", candidate_k=20, rrf_k=60)
"""The hybrid mode at the platform defaults (a test pins them against Settings)."""


def build_hybrid_search(
    vector_hits: Mapping[str, Sequence[SearchHit]] | None = None,
    text_hits: Mapping[str, Sequence[SearchHit]] | None = None,
    *,
    fuse: StandInFuse | None = None,
    embedder: ScriptedEmbedder | None = None,
    stored: Mapping[str, frozenset[str]] | None = None,
    retrieval: RetrievalConfig = HYBRID,
    clock: Callable[[], float] | None = None,
) -> HybridSearch:
    """As `build_search`, in hybrid mode. `stored` defaults to: every business with hits in either
    leg has the embedder's model only."""
    embedder = embedder or ScriptedEmbedder()
    vector_hits, text_hits = vector_hits or {}, text_hits or {}
    runner_clock = FakeClock()
    runner = EmbeddingRunner(
        embedder,
        embedder._fake._spec,
        POLICY,
        deadline_s=100.0,
        clock=runner_clock,
        sleep=runner_clock.sleep,
        rand=lambda: 0.5,
    )
    businesses = {*vector_hits, *text_hits}
    models = StoredModels(
        stored if stored is not None else {b: frozenset({embedder.model}) for b in businesses}
    )
    standin, text = StandInSearch(vector_hits), StandInTextSearch(text_hits)
    fusion = fuse or StandInFuse()
    ticking = clock or TickingClock()
    database = FakeDatabase(clock=ticking)  # its commit reads the service's clock
    service = SearchService(
        database,
        runner,
        search=standin,
        stored_models=models,
        text=text,
        fuse=fusion,
        retrieval=retrieval,
        clock=ticking,
    )
    return HybridSearch(service, embedder, database, models, standin, text, fusion)
