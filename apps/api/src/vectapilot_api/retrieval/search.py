"""Search a business's knowledge for a question (M2 PR 6): embed, check the model, run H1.

One search, in this order:

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

**Isolation level.** The transaction runs at Postgres' default, READ COMMITTED, so the model check
and the search each see their own snapshot. A reindex that commits between them is still safe:
H1's SQL compares only chunks of the question's model, so the search finds fewer chunks (or none,
which the answer turns into "I don't know"), never mixed vectors. REPEATABLE READ would give one
snapshot, which correctness does not need: ADR 0011 (accepted 2026-10-22, option 5a) keeps READ
COMMITTED.

**Testing without Postgres.** `search` and `stored_models` are parameters (defaults: H1's
`vector_search` and `kb.store.stored_models_in`, the one copy of the stored-models query that
reindex uses too), so local tests inject a stand-in that returns prepared hits without any SQL.
The real path runs against Postgres in CI (`tests/answer/test_postgres_search_and_answer.py` and
the canary suite).
"""

import secrets
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Annotated, Final, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.embeddings.guard import require_one_model
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.kb.store import stored_models_in
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.types import Reader, SearchHit, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

MAX_QUESTION_CHARS: Final = 2000
"""The longest question accepted, in characters, counted before stripping. A platform default, not
measured: generous for a customer's question, far below the embedding model's context (32,000
tokens, ADR 0008), and it bounds what one request can put into a prompt."""

SEARCH_OPERATION: Final = "kb-search"
"""The `embedding_call` operation of `GET /v1/kb/search`."""


def check_question(value: str) -> str:
    """A question with text in it, stripped; encodable as UTF-8 (no lone surrogates, which the
    embedding and model APIs would refuse). Messages never contain the value."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must contain text, not only whitespace")
    try:
        stripped.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None
    return stripped


Question = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARS),
    AfterValidator(check_question),
]
"""A customer's question as the API accepts it (`GET /v1/kb/search?q=`, `POST /v1/answer`)."""


class BusinessTransactions(Protocol):
    """What search needs from `db.Database`: one business transaction (role check, then RLS)."""

    def business_transaction(
        self, business_id: str
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...


VectorSearch = Callable[[AsyncConnection, VectorQuery], Awaitable[list[SearchHit]]]
"""H1's `vector_search` signature: the seam a local test fills with a stand-in."""

StoredModels = Callable[[AsyncConnection, str], Awaitable[frozenset[str]]]
"""`kb.store.stored_models_in`'s signature: (the business transaction's connection, business id)."""


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
    comparable across embedding models."""

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


class SearchService:
    """Embeds a question and searches one business's knowledge. One instance per process."""

    def __init__(
        self,
        database: BusinessTransactions,
        runner: EmbeddingRunner,
        *,
        search: VectorSearch | None = None,
        stored_models: StoredModels | None = None,
    ) -> None:
        """`search` and `stored_models` default to H1's `vector_search` and `stored_models_in`,
        looked up when the service is built (so a test can replace them for a whole app)."""
        self._database = database
        self._runner = runner
        self._search = vector_search if search is None else search
        self._stored_models = stored_models_in if stored_models is None else stored_models

    @property
    def vector_search(self) -> VectorSearch:
        """The search function in use (H1's `vector_search` unless a test injected a stand-in)."""
        return self._search

    async def search(
        self,
        business_id: str,
        question: str,
        *,
        k: int,
        reader: Reader,
        trace_id: str,
        operation: str = SEARCH_OPERATION,
    ) -> SearchResult:
        """The top `k` hits of `business_id` for `question`, as `reader` may see them.

        Raises the runner's typed embedding errors, `ReindexRequired` when the business's chunks
        carry another model id than the question's, and database errors (`OSError`,
        `SQLAlchemyError`). `pydantic.ValidationError` cannot escape for a valid `k`: `VectorQuery`
        is built from checked values only.
        """
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


def new_trace_id() -> str:
    """A fresh W3C trace id for one request: 32 lowercase hex characters, never all zeros."""
    while (trace_id := secrets.token_hex(16)) == "0" * 32:  # 1 in 2**128; the rule is absolute
        continue
    return trace_id


def top_score(hits: Sequence[SearchHit]) -> float | None:
    """The best score among `hits` (None for none). `max`, not `hits[0]`: it does not depend on
    the order H1 returns."""
    return max((hit.score for hit in hits), default=None)
