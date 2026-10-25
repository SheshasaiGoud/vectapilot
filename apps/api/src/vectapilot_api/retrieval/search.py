"""Search a business's knowledge for a question (M2 PR 6, M3 PR 4): embed, check the model, search.

Two modes (`RetrievalConfig.mode`, from `Settings.rag_retrieval_mode`; default `vector`):

**Vector (M2's path, unchanged).**

1. **Embed the question** with `input_type="query"` through the `EmbeddingRunner`, so it gets the
   retries and exactly one metadata-only `embedding_call` record (carrying the request's trace id).
2. **One business transaction** (`Database.business_transaction`: the role check, then RLS for
   this business) holds both remaining steps on one connection:
   - **one model** (ADR 0008): the business's distinct `chunks.embedding_model` values must all be
     the question's model, else `ReindexRequired`, and nothing is compared;
   - **H1's `vector_search`**, as the given reader (the routes pass `customer`: approved public
     chunks only). Its `WHERE` clause names the business; RLS is the second line (business-context
     §5).
3. The hits come back as H1 returns them: best first, with their scores. No threshold here: what
   counts as evidence is the answer's decision (`answer/service.py`).

**Isolation level (vector).** The transaction runs at Postgres' default, READ COMMITTED, so the
model check and the search each see their own snapshot. A reindex that commits between them is
still safe: H1's SQL compares only chunks of the question's model, so the search finds fewer chunks
(or none, which the answer turns into "I don't know"), never mixed vectors. REPEATABLE READ would
give one snapshot, which correctness does not need: ADR 0011 (accepted 2026-10-22, option 5a) keeps
READ COMMITTED.

**Hybrid (M3 PR 4, ADR 0014 decisions 5-7).** The question is embedded once, as above. Then one
business transaction whose first statement is `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ
ONLY` (`db.SNAPSHOT`; D8), then `set_config` as always, holds:
   - the same model check;
   - `fused_search`: M2 H1's `vector_search` and M3 H1's `text_search`, each at the candidate
     count (`max(k, candidate_k)`), then M3 H2's `rrf_fuse([vector hits, text hits])` limited to
     `k`.
One snapshot: both legs read the same committed data, so an approval that commits between them
cannot put a retired and a new version of one source into one fused list (ADR 0011 decision 5,
amended for hybrid mode only). READ ONLY: nothing in it can write.
The result's `hits` are the fused hits in fused order, as plain `SearchHit`s (each `score` is
still the cosine, whichever leg found it), and `fused` holds the same list with each hit's RRF
score and ranks. Each leg is asked for at least `candidate_k` hits however small `k` is, so the
fusion always weighs the same candidates; `k` only cuts the fused list. The answer asks for
`candidate_k` and keeps the whole fused list for the eval harness, but only its top
`rag_context_k` reach the prompt (`answer/service.py`). `SearchStats` collects counts and
durations (monotonic clock) for the answer's `rag_answer` record: metadata only.

**Testing without Postgres.** `search`, `text`, `fuse` and `stored_models` are parameters
(defaults: M2 H1's `vector_search`, M3 H1's `text_search`, M3 H2's `rrf_fuse` and
`kb.store.stored_models_in`, the one copy of the stored-models query that reindex uses too), so
local tests inject stand-ins that return prepared hits without any SQL. The real paths run against
Postgres in CI (`tests/answer/test_postgres_search_and_answer.py`, the canary suite, and the
snapshot tests in `tests/retrieval/test_hybrid_snapshot.py`); the hybrid ones wait for H1 and H2
(`tests/canary/read_paths.py`, `PENDING_HAND_WRITE`).
"""

import secrets
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.config import Settings
from vectapilot_api.embeddings.guard import require_one_model
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.kb.store import stored_models_in
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.fusion import RRF_K, rrf_fuse
from vectapilot_api.retrieval.text import text_search

# `Question`, its check and its limit live in retrieval/types.py since M3 PR 3 (`TextQuery` holds a
# `Question`); re-exported here, where the routes, the harness and the tests import them from.
from vectapilot_api.retrieval.types import (
    CANDIDATE_K,
    MAX_K,
    FusedHit,
    Reader,
    RetrievalMode,
    SearchHit,
    TextQuery,
    VectorQuery,
)
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS as MAX_QUESTION_CHARS
from vectapilot_api.retrieval.types import Question as Question
from vectapilot_api.retrieval.types import check_question as check_question
from vectapilot_api.retrieval.vector import vector_search

SEARCH_OPERATION: Final = "kb-search"
"""The `embedding_call` operation of `GET /v1/kb/search`."""


class BusinessTransactions(Protocol):
    """What search needs from `db.Database`: one business transaction (role check, then RLS),
    as a read-only snapshot for a hybrid search (`snapshot=True`, M3 PR 4)."""

    def business_transaction(
        self, business_id: str, *, snapshot: bool = False
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...


VectorSearch = Callable[[AsyncConnection, VectorQuery], Awaitable[list[SearchHit]]]
"""M2 H1's `vector_search` signature: the seam a local test fills with a stand-in."""

TextSearch = Callable[[AsyncConnection, TextQuery], Awaitable[list[SearchHit]]]
"""M3 H1's `text_search` signature: the seam a local test fills with a stand-in."""


class Fuse(Protocol):
    """M3 H2's `rrf_fuse` signature: the seam a local test fills with a stand-in."""

    def __call__(
        self, ranked_lists: Sequence[Sequence[SearchHit]], *, k: int, limit: int
    ) -> list[FusedHit]: ...


StoredModels = Callable[[AsyncConnection, str], Awaitable[frozenset[str]]]
"""`kb.store.stored_models_in`'s signature: (the business transaction's connection, business id)."""


@dataclass(frozen=True)
class RetrievalConfig:
    """How `SearchService` retrieves (M3 PR 4): Settings' platform defaults until per-business
    configuration exists (rule 9). The default is vector mode, M2's path."""

    mode: RetrievalMode = "vector"
    candidate_k: int = CANDIDATE_K
    """Hybrid only: the least k each leg is asked for, and the answer's fused list length
    (`rag_candidate_k`)."""
    rrf_k: int = RRF_K
    """Hybrid only: `rrf_fuse`'s k (`rag_rrf_k`)."""

    def __post_init__(self) -> None:
        if self.mode not in ("vector", "hybrid"):
            raise ValueError("mode must be 'vector' or 'hybrid'")
        if not 1 <= self.candidate_k <= MAX_K:
            raise ValueError(f"candidate_k must be between 1 and {MAX_K} (MAX_K)")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be at least 1")

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            mode=settings.rag_retrieval_mode,
            candidate_k=settings.rag_candidate_k,
            rrf_k=settings.rag_rrf_k,
        )


class Citation(BaseModel):
    """One retrieved chunk as the API shows it: what a citation needs, its text and its score.

    `GET /v1/kb/search` returns these as `hits`; `POST /v1/answer` as `sources` (only the chunks
    the answer cites, each of them retrieved for the business in that request).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: str
    document_id: str
    source_id: str
    version: str
    title: str
    page: int | None
    chunk_index: int
    language: str
    content_kind: ContentKind
    content: str = Field(repr=False)
    score: float
    """Cosine similarity (H1): higher is closer. Uncalibrated: not a probability, and not
    comparable across embedding models. In hybrid mode too: never the RRF score (D7)."""

    @classmethod
    def of(cls, hit: SearchHit) -> "Citation":
        return cls(
            chunk_id=str(hit.chunk_id),
            document_id=str(hit.document_id),
            source_id=hit.source_id,
            version=hit.version,
            title=hit.title,
            page=hit.page,
            chunk_index=hit.chunk_index,
            language=hit.language,
            content_kind=hit.content_kind,
            content=hit.content,
            score=hit.score,
        )


@dataclass(frozen=True)
class SearchResult:
    """The hits for one question, best first, and the model id the question was embedded with."""

    hits: tuple[SearchHit, ...] = field(repr=False)
    embedding_model: str
    fused: tuple[FusedHit, ...] | None = field(default=None, repr=False)
    """Hybrid only: `hits` again, each with its RRF score and its rank in each leg's list (vector
    first); None in vector mode."""


@dataclass
class SearchStats:
    """What one hybrid search did, for the answer's `rag_answer` record (M3 PR 4): counts and
    durations only, never text. Filled in as the search runs, so a search that failed half-way
    still shows how far it got: a field stays None until its stage has finished. Durations are
    milliseconds on the service's monotonic clock."""

    candidate_k: int | None = None
    """The k each leg was asked for (`max(k, candidate_k)`)."""
    vector_hits: int | None = None
    text_hits: int | None = None
    fused_hits: int | None = None
    fts_only_hits: int | None = None
    """Fused hits that only the full-text leg found (`fts_only`)."""
    embed_ms: float | None = None
    transaction_ms: float | None = None
    """From opening the business transaction (the role check on a process's first search, the
    snapshot, `set_config`) to its commit: the model check, both legs and the fuse included."""
    vector_ms: float | None = None
    text_ms: float | None = None
    fuse_ms: float | None = None


@dataclass(frozen=True)
class FusedLists:
    """One hybrid search's three lists, each best first: each leg's hits and the fusion."""

    vector: tuple[SearchHit, ...] = field(repr=False)
    text: tuple[SearchHit, ...] = field(repr=False)
    fused: tuple[FusedHit, ...] = field(repr=False)


def fts_only(fused: Iterable[FusedHit]) -> frozenset[uuid.UUID]:
    """The chunk ids of the fused hits only the full-text leg found: in the text list, not in the
    vector list (`ranks[0] is None`: `fused_search` gives the fusion the vector list first)."""
    return frozenset(item.hit.chunk_id for item in fused if item.ranks[0] is None)


def _ms(seconds: float) -> float:
    return max(0.0, seconds * 1000)


async def fused_search(
    connection: AsyncConnection,
    query: TextQuery,
    *,
    limit: int,
    rrf_k: int,
    vector: VectorSearch,
    text: TextSearch,
    fuse: Fuse,
    clock: Callable[[], float] = time.monotonic,
    stats: SearchStats | None = None,
) -> FusedLists:
    """Both legs at `query.k` on `connection`, one after the other, then `fuse` limited to `limit`.

    The two legs are two statements, so `connection` must be a snapshot transaction
    (`business_transaction(..., snapshot=True)`) for them to read the same data (D8). The vector
    leg gets `query`'s fields without the question, so both legs see the same candidates (the
    business, the model, the reader's audiences). The fusion gets `[vector hits, text hits]`, in
    that order (`fts_only` relies on it), with RRF's `k = rrf_k`. `SearchService` and the canary
    suite's `hybrid_search` read path both run this function, so the canary suite checks the
    product's own composition. `stats`, if given, is filled in as each stage finishes.
    """
    stats = SearchStats() if stats is None else stats
    vector_query = VectorQuery(
        business_id=query.business_id,
        embedding=query.embedding,
        embedding_model=query.embedding_model,
        k=query.k,
        reader=query.reader,
    )
    started = clock()
    vector_hits = await vector(connection, vector_query)
    stats.vector_ms, stats.vector_hits = _ms(clock() - started), len(vector_hits)
    started = clock()
    text_hits = await text(connection, query)
    stats.text_ms, stats.text_hits = _ms(clock() - started), len(text_hits)
    started = clock()
    fused = fuse([vector_hits, text_hits], k=rrf_k, limit=limit)
    stats.fuse_ms = _ms(clock() - started)
    stats.fused_hits, stats.fts_only_hits = len(fused), len(fts_only(fused))
    return FusedLists(tuple(vector_hits), tuple(text_hits), tuple(fused))


class SearchService:
    """Embeds a question and searches one business's knowledge. One instance per process."""

    def __init__(
        self,
        database: BusinessTransactions,
        runner: EmbeddingRunner,
        *,
        search: VectorSearch | None = None,
        stored_models: StoredModels | None = None,
        text: TextSearch | None = None,
        fuse: Fuse | None = None,
        retrieval: RetrievalConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`search`, `stored_models`, `text` and `fuse` default to M2 H1's `vector_search`,
        `stored_models_in`, M3 H1's `text_search` and M3 H2's `rrf_fuse`, looked up when the
        service is built (so a test can replace them for a whole app). `retrieval` defaults to
        vector mode (M2's path); `clock` (monotonic seconds) times a hybrid search's stages."""
        self._database = database
        self._runner = runner
        self._search = vector_search if search is None else search
        self._stored_models = stored_models_in if stored_models is None else stored_models
        self._text = text_search if text is None else text
        self._fuse: Fuse = rrf_fuse if fuse is None else fuse
        self._retrieval = RetrievalConfig() if retrieval is None else retrieval
        self._clock = clock

    @property
    def vector_search(self) -> VectorSearch:
        """The search function in use (H1's `vector_search` unless a test injected a stand-in)."""
        return self._search

    @property
    def text_search(self) -> TextSearch:
        """The full-text leg in use (M3 H1's `text_search` unless a test injected a stand-in)."""
        return self._text

    @property
    def fuse(self) -> Fuse:
        """The fusion in use (M3 H2's `rrf_fuse` unless a test injected a stand-in)."""
        return self._fuse

    @property
    def retrieval(self) -> RetrievalConfig:
        """The mode and, for hybrid, the candidate count and RRF's k."""
        return self._retrieval

    async def search(
        self,
        business_id: str,
        question: str,
        *,
        k: int,
        reader: Reader,
        trace_id: str,
        operation: str = SEARCH_OPERATION,
        stats: SearchStats | None = None,
    ) -> SearchResult:
        """The top `k` hits of `business_id` for `question`, as `reader` may see them.

        Raises the runner's typed embedding errors, `ReindexRequired` when the business's chunks
        carry another model id than the question's, and database errors (`OSError`,
        `SQLAlchemyError`). `pydantic.ValidationError` cannot escape for a valid `k` (and, in
        hybrid mode, a question `Question` accepts, as the routes and the harness check first):
        the queries are built from checked values only. In hybrid mode `stats`, if given, is
        filled in as the search runs; vector mode leaves it untouched.
        """
        if self._retrieval.mode == "hybrid":
            return await self._hybrid(
                business_id,
                question,
                k=k,
                reader=reader,
                trace_id=trace_id,
                operation=operation,
                stats=SearchStats() if stats is None else stats,
            )
        embedded = await self._runner.embed(
            [question],
            input_type="query",
            business_id=business_id,
            operation=operation,
            trace_id=trace_id,
        )
        query = VectorQuery(
            business_id=business_id,
            embedding=embedded.vectors[0],
            embedding_model=embedded.model,
            k=k,
            reader=reader,
        )
        async with self._database.business_transaction(business_id) as connection:
            stored = await self._stored_models(connection, business_id)
            require_one_model(stored_models=stored, query_model=embedded.model)
            hits = await self._search(connection, query)
        return SearchResult(hits=tuple(hits), embedding_model=embedded.model)

    async def _hybrid(
        self,
        business_id: str,
        question: str,
        *,
        k: int,
        reader: Reader,
        trace_id: str,
        operation: str,
        stats: SearchStats,
    ) -> SearchResult:
        """The hybrid search (module docstring): one embedding, then one snapshot transaction."""
        started = self._clock()
        embedded = await self._runner.embed(
            [question],
            input_type="query",
            business_id=business_id,
            operation=operation,
            trace_id=trace_id,
        )
        stats.embed_ms = _ms(self._clock() - started)
        query = TextQuery(
            business_id=business_id,
            question=question,
            embedding=embedded.vectors[0],
            embedding_model=embedded.model,
            k=max(k, self._retrieval.candidate_k),
            reader=reader,
        )
        stats.candidate_k = query.k
        opened = self._clock()
        async with self._database.business_transaction(business_id, snapshot=True) as connection:
            stored = await self._stored_models(connection, business_id)
            require_one_model(stored_models=stored, query_model=embedded.model)
            lists = await fused_search(
                connection,
                query,
                limit=k,
                rrf_k=self._retrieval.rrf_k,
                vector=self._search,
                text=self._text,
                fuse=self._fuse,
                clock=self._clock,
                stats=stats,
            )
        stats.transaction_ms = _ms(self._clock() - opened)
        return SearchResult(
            hits=tuple(item.hit for item in lists.fused),
            embedding_model=embedded.model,
            fused=lists.fused,
        )


def new_trace_id() -> str:
    """A fresh W3C trace id for one request: 32 lowercase hex characters, never all zeros."""
    while (trace_id := secrets.token_hex(16)) == "0" * 32:  # 1 in 2**128; the rule is absolute
        continue
    return trace_id


def top_score(hits: Sequence[SearchHit]) -> float | None:
    """The best score among `hits` (None for none). `max`, not `hits[0]`: it does not depend on
    the order H1 returns."""
    return max((hit.score for hit in hits), default=None)
