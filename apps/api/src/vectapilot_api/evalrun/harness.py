"""The eval run itself (M2 PR 8, M3 PR 5): ingest, check the stored chunks, answer and score each
question.

One question at a time, in dataset order, through the product's own `AnswerService` (ADR 0011):
one embedding call for the question, the search as reader `customer`, the threshold, the prompt,
the answer model (`large`) and the citation check. The run's retrieval mode is its own
(`RunConfig.retrieval`, from `--retrieval-mode`, default Settings' `rag_retrieval_mode`; M3 PR 5):
- **vector** (M2's path): H1's `vector_search` with k = the run's search k;
- **hybrid** (ADR 0014): both legs at the candidate k, M3 H2's `rrf_fuse` limited to it, in one
  snapshot; the whole fused list is the answer's `AnswerRun.hits`, and its top search-k hits reach
  the evidence gate.
The harness adds three things around it, and changes nothing inside it:

1. **`RecordingSearch`** wraps every list: H1's `vector_search` (the `search` seam
   `SearchService` already has), and in hybrid mode M3 H1's `text_search` and M3 H2's
   `rrf_fuse` too (its `text` and `fuse` seams). On the same connection, inside the same business
   transaction, each leg's wrapper reads each hit's document row (`store.document_rows`:
   business, status, audience), records the hits, and **checks them before returning**
   (`scoring.check_hit_rows`); the fusion's wrapper checks the fused list against the rows its
   legs read (a hit whose document no leg returned has no row, so it fails closed), then at
   chunk level (`FusedListMismatch`): every fused hit must be, unchanged (`==`), a hit with the
   same chunk id in one of the lists the fusion was given, and no chunk id may repeat, so a
   sibling chunk no leg returned, or a leg's hit with a changed `score`, stops the run before the
   answer step (review of PR 5, ananya A2). A hit of another business,
   or one a customer may never see, raises inside the search, so the run aborts whatever the
   answer step would have done next (a cap refusal, Ctrl-C, an error): a leak that fusion drops
   is still a leak. `_score` checks the business again (`scoring.retrieved_chunks`, on every
   list), and retrieval is scored even when the answer model fails. The recorder times its own
   work inside each stage on the search's monotonic clock (`scoring.HarnessTime`), so the
   summary can subtract it from the `rag_answer` record's stage durations.
2. **The cost cap** (`budget.py`): the embedder and the gateway reserve each paid request's worst
   case before it is sent; a request that would cross `--max-usd` is not sent (`CapReached`),
   and the run stops, partial.
3. **H2's metrics** (`scoring.score_retrieval`) on the answer's ranked list: the vector list, or
   in hybrid mode the fused list (each leg's list is kept per question, `ComponentLists`, and
   scored in the summary).

Each question is first validated as `POST /v1/answer` validates it (`retrieval.search.Question`:
stripped, not only whitespace, valid UTF-8), so the run embeds and prompts exactly what the
product would; a question that fails is refused by id before anything is sent.

What stops a run: the cap, a stop rule after a paid call (`CostCap.stop`; during ingest it is
checked before each document), a failure before the answer step finished (the search, an
embedding or database error: every later question would likely fail the same way; only the class
name is kept, except a `FusedListMismatch`, whose own message names a rank and a chunk id),
Ctrl-C (also while registering, deleting, ingesting or checking the stored chunks: the run is then
partial and its folder is still written), and a hit no customer may see (aborted: a
cross-business leak is a hard gate). An answer model failure (`ModelUnavailable`,
`AnswerFailed`) is an outcome, recorded as `error`, and the run goes on. A `NotImplementedError`
(H1 or H2 not written yet) propagates: it is never an outcome. Questions after a stop are kept as
`not_run`, never dropped, and the run is `partial`; a stop rule tripped by the last question
leaves none unrun, so the run is `complete` with its stop recorded, never for the note
(`final_status`, decided 2026-10-22).

`execute` refuses (`HarnessRefused`) a dry run whose Settings name a paid provider, a live run
without a cap, and a `--fresh` delete of any id that is not a derived eval id of its business, or
in production. (M3 PR 4's refusal of a hybrid `rag_retrieval_mode` is gone: PR 5 measures the
mode `RunConfig.retrieval` names, whatever Settings say.)

Nothing here logs or prints question, chunk or answer text: progress lines carry ids, labels,
H2's values and dollar amounts.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.answer.service import AnswerService, RagConfig
from vectapilot_api.answer.types import (
    AnswerFailed,
    AnswerRun,
    ModelUnavailable,
    WeakEvidenceOutcome,
)
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.factory import build_embedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.evalrun.budget import CappedEmbedder, CappedGateway, CapReached, CostCap, usd
from vectapilot_api.evalrun.dryrun import DryRunAnswerProvider
from vectapilot_api.evalrun.ingest import IngestCount, StaleChunks, ingest_corpus, verify_stored
from vectapilot_api.evalrun.namespace import (
    EVAL_MARKER,
    dataset_business,
    is_eval_business_id,
)
from vectapilot_api.evalrun.report import SEARCH_K_ABOVE_CANDIDATE_K, RunStatus
from vectapilot_api.evalrun.scoring import (
    H2_METRICS,
    AnswerLine,
    ComponentLists,
    CrossBusinessLeak,
    DocumentRow,
    HarnessTime,
    HitPolicyViolation,
    MetricFunctions,
    QuestionResult,
    SourceRef,
    base_fields,
    check_hit_rows,
    check_metric_k,
    component_hits,
    first_relevant_rank,
    hit_refs,
    not_run,
    retrieved_chunks,
    score_retrieval,
    unheld_keys,
    unscorable_groups,
)
from vectapilot_api.evalrun.store import (
    EvalStore,
    PostgresEvalStore,
    document_rows,
    register_businesses,
)
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.evalset.ranking import has_evidence
from vectapilot_api.evalset.relevance import RetrievedChunk
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import build_provider
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.retrieval.fusion import rrf_fuse
from vectapilot_api.retrieval.search import (
    Fuse,
    Question,
    RetrievalConfig,
    SearchService,
    TextSearch,
    VectorSearch,
    new_trace_id,
)
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import MAX_K, FusedHit, SearchHit, TextQuery, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

PROGRESS_WIDTH: Final = 3
INTERRUPTED_BEFORE_QUESTIONS: Final = "interrupted before the questions"

_API_QUESTION: Final = TypeAdapter(Question)


class HarnessRefused(Exception):
    """The run cannot start as configured. The message names settings and ids, never text."""


class FusedListMismatch(RuntimeError):
    """A fused hit that is not, unchanged, a hit one of the legs returned: a chunk no leg
    returned, a leg's hit with a field changed (its `score`, say), or a chunk id twice.
    `rrf_fuse`'s contract (`FusedHit.hit`: "the hit as its leg returned it, unchanged") is
    broken, so it is a bug, never an outcome: the run stops before the answer step. The message
    names the rank and the chunk id, never text (review of PR 5, ananya A2)."""


def api_questions(questions: Sequence[EvalQuestion]) -> dict[str, str]:
    """{question id: its text exactly as `POST /v1/answer` would pass it on} (the route's
    `Question` validation: stripped, not only whitespace, valid UTF-8, at most 2,000 characters).
    Raises `HarnessRefused` naming the first question that fails, by id only (the validation
    error would show the text, so it is never chained or printed)."""
    texts: dict[str, str] = {}
    invalid: str | None = None
    for question in questions:
        try:
            texts[question.id] = _API_QUESTION.validate_python(question.question)
        except ValidationError:
            invalid = question.id
            break
    if invalid is not None:  # outside the `except`: nothing chained
        raise HarnessRefused(
            f"question {invalid}: its text is not a question POST /v1/answer would accept (empty"
            " after stripping, or not valid Unicode)"
        )
    return texts


@dataclass(frozen=True)
class RunConfig:
    """What a run measures with: everything here goes into run.json and the config fingerprint."""

    chunking: ChunkingConfig
    search_k: int
    """The k passed to the search (`RagConfig.context_k`): how many hits one answer retrieves."""
    metric_k: int
    """hit@k and MRR@k's k (decided: 5). Never above `search_k` (checked when built)."""
    rag_min_score: float
    weak_evidence_outcome: WeakEvidenceOutcome
    llm_max_retries: int
    llm_max_repairs: int
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    """The run's retrieval mode, and in hybrid mode its candidate k and RRF k (M3 PR 5):
    `--retrieval-mode`, else Settings. Vector by default: M2's path."""

    def __post_init__(self) -> None:
        check_metric_k(self.metric_k, self.search_k)
        if self.search_k > MAX_K:
            raise ValueError(f"search k must be at most {MAX_K} (retrieval.types.MAX_K)")
        if not 0.0 <= self.rag_min_score <= 1.0:
            raise ValueError("rag_min_score must be between 0 and 1")
        if self.hybrid and self.search_k > self.retrieval.candidate_k:
            raise ValueError(SEARCH_K_ABOVE_CANDIDATE_K)

    @property
    def hybrid(self) -> bool:
        return self.retrieval.mode == "hybrid"

    @property
    def search_call_k(self) -> int:
        """The k the answer asks the search for: the search k in vector mode; in hybrid mode
        `max(candidate_k, search_k)`, each leg's k and the fused list's length
        (`AnswerService`)."""
        if self.hybrid:
            return max(self.retrieval.candidate_k, self.search_k)
        return self.search_k

    def rag(self) -> RagConfig:
        return RagConfig(
            min_score=self.rag_min_score,
            context_k=self.search_k,
            weak_evidence_outcome=self.weak_evidence_outcome,
        )


# ── The search, recorded ─────────────────────────────────────────────────────────────────────

DocumentLookup = Callable[
    [AsyncConnection, Sequence[uuid.UUID]], Awaitable[Mapping[uuid.UUID, DocumentRow]]
]


@dataclass(frozen=True)
class SearchCall:
    """One leg's search as the harness saw it: the hits (best first) and their documents' rows."""

    business_id: str
    k: int
    hits: tuple[SearchHit, ...] = field(repr=False)
    documents: Mapping[uuid.UUID, DocumentRow]
    harness_ms: float = 0.0
    """The recorder's own time inside this leg: the document lookup and the check."""

    @property
    def document_businesses(self) -> dict[uuid.UUID, str]:
        return {document_id: row.business_id for document_id, row in self.documents.items()}


@dataclass(frozen=True)
class FuseCall:
    """One fusion as the harness saw it: the lists it was given and the fused list it returned."""

    lists: tuple[tuple[SearchHit, ...], ...] = field(repr=False)
    rrf_k: int
    limit: int
    fused: tuple[FusedHit, ...] = field(repr=False)
    harness_ms: float = 0.0
    """The recorder's own time inside the fusion: the check of the fused list."""


def _ms(seconds: float) -> float:
    return max(0.0, seconds * 1000)


def check_fused_from_legs(
    ranked_lists: Sequence[Sequence[SearchHit]], fused: Sequence[FusedHit]
) -> None:
    """Raise `FusedListMismatch` for the first fused hit, best first, whose chunk id is in none
    of `ranked_lists`, that equals (`==`, every field) none of the hits with its chunk id there,
    or whose chunk id came earlier in the fused list."""
    given: dict[uuid.UUID, list[SearchHit]] = {}
    for ranked in ranked_lists:
        for hit in ranked:
            given.setdefault(hit.chunk_id, []).append(hit)
    seen: set[uuid.UUID] = set()
    for rank, item in enumerate(fused, start=1):
        chunk_id = item.hit.chunk_id
        where = f"rank {rank} of the fused list: chunk {chunk_id}"
        if chunk_id in seen:
            raise FusedListMismatch(f"{where} is in the fused list twice")
        seen.add(chunk_id)
        returned = given.get(chunk_id)
        if returned is None:
            raise FusedListMismatch(f"{where} is in neither leg's list")
        if not any(item.hit == hit for hit in returned):
            raise FusedListMismatch(
                f"{where} is not the hit its leg returned (a field such as its score changed;"
                " rrf_fuse returns each hit unchanged)"
            )


class RecordingSearch:
    """Every list one search makes, each checked before it goes on (module docstring): H1's
    `vector_search` (the instance itself is the vector seam), M3 H1's `text_search`
    (`text_leg`) and M3 H2's `rrf_fuse` (`fuse`), or a test's stand-ins, plus the document lookup
    on the search's connection. `clock` must be the search's own (monotonic seconds), so the
    recorder's time is measured as the search measures its stages."""

    def __init__(
        self,
        search: VectorSearch | None = None,
        lookup: DocumentLookup | None = None,
        *,
        text: TextSearch | None = None,
        fuse: Fuse | None = None,
        clock: Callable[[], float] = time.monotonic,
        name_lists: bool = False,
    ) -> None:
        self._search = vector_search if search is None else search
        self._lookup = document_rows if lookup is None else lookup
        self._text = text_search if text is None else text
        self._fuse: Fuse = rrf_fuse if fuse is None else fuse
        self._clock = clock
        self._name_lists = name_lists
        """A hybrid run's leak lines name the list (" of the text leg"); vector lines are M2's."""
        self.calls: list[SearchCall] = []
        """The vector leg's searches."""
        self.text_calls: list[SearchCall] = []
        self.fuse_calls: list[FuseCall] = []

    def clear(self) -> None:
        """Forget the previous question's lists."""
        self.calls.clear()
        self.text_calls.clear()
        self.fuse_calls.clear()

    async def __call__(self, connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        hits = await self._search(connection, query)
        await self._record(connection, query.business_id, query.k, hits, self.calls, "vector leg")
        return hits

    async def text_leg(self, connection: AsyncConnection, query: TextQuery) -> list[SearchHit]:
        hits = await self._text(connection, query)
        await self._record(
            connection, query.business_id, query.k, hits, self.text_calls, "text leg"
        )
        return hits

    def fuse(
        self, ranked_lists: Sequence[Sequence[SearchHit]], *, k: int, limit: int
    ) -> list[FusedHit]:
        fused = self._fuse(ranked_lists, k=k, limit=limit)
        started = self._clock()
        legs = [*self.calls, *self.text_calls]
        rows: dict[uuid.UUID, DocumentRow] = {}
        for call in legs:
            rows.update(call.documents)
        # The question's business, from its legs; with no leg recorded, "" matches no row, so
        # every fused hit fails the check (closed).
        business = legs[0].business_id if legs else ""
        try:
            # The business, status and audience first (a leak is the harder gate, exit 4), then
            # the chunk-level check (a bug: the run stops, partial).
            check_hit_rows(business, [item.hit for item in fused], rows, self._where("fused list"))
            check_fused_from_legs(ranked_lists, fused)
        finally:
            self.fuse_calls.append(
                FuseCall(
                    lists=tuple(tuple(ranked) for ranked in ranked_lists),
                    rrf_k=k,
                    limit=limit,
                    fused=tuple(fused),
                    harness_ms=_ms(self._clock() - started),
                )
            )
        return fused

    def _where(self, name: str) -> str:
        return f" of the {name}" if self._name_lists else ""

    async def _record(
        self,
        connection: AsyncConnection,
        business_id: str,
        k: int,
        hits: Sequence[SearchHit],
        into: list[SearchCall],
        name: str,
    ) -> None:
        """Read the hits' rows on the search's connection, record them, then check them: a
        `HitPolicyViolation` (ids only) raises inside the search, the call recorded all the same."""
        started = self._clock()
        found: Mapping[uuid.UUID, DocumentRow] = {}
        try:
            found = await self._lookup(connection, [hit.document_id for hit in hits])
            check_hit_rows(business_id, hits, found, self._where(name))
        finally:
            into.append(
                SearchCall(business_id, k, tuple(hits), dict(found), _ms(self._clock() - started))
            )


# ── The stored corpus ────────────────────────────────────────────────────────────────────────


async def stored_visible(
    store: EvalStore,
    corpus: Corpus,
    mapping: Mapping[str, str],
    config: ChunkingConfig,
    embedding_model: str,
) -> dict[str, tuple[RetrievedChunk, ...]]:
    """Check every eval business's stored chunks against the corpus at `config` (`StaleChunks`
    if any differs), then return, per dataset business, its customer-visible stored chunks as
    `RetrievedChunk`s (business from the row), for the unscorable check."""
    problems: list[str] = []
    visible: dict[str, tuple[RetrievedChunk, ...]] = {}
    for business_id, eval_id in mapping.items():
        stored = await store.stored_chunks(eval_id)
        problems += verify_stored(corpus, business_id, config, stored, embedding_model)
        visible[business_id] = tuple(
            RetrievedChunk(
                business_id=dataset_business(chunk.business_id, mapping),
                source_id=chunk.source_id,
                version=chunk.version,
                content=chunk.content,
            )
            for chunk in stored
            if chunk.answers_customers
        )
    if problems:
        raise StaleChunks(problems)
    return visible


# ── The questions ────────────────────────────────────────────────────────────────────────────

ResultSink = Callable[[QuestionResult, AnswerLine | None], None]
"""Called once per question, in order, as soon as its result exists (the CLI writes it out)."""


@dataclass
class QuestionsOutcome:
    results: list[QuestionResult] = field(default_factory=list)
    stop: str | None = None
    leak: str | None = None


def _sources(run: AnswerRun) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(
            chunk_id=source.chunk_id,
            source_id=source.source_id,
            version=source.version,
            chunk_index=source.chunk_index,
            page=source.page,
            score=source.score,
        )
        for source in run.response.sources
    )


async def run_questions(
    questions: Sequence[EvalQuestion],
    *,
    answer_service: AnswerService,
    recorder: RecordingSearch,
    mapping: Mapping[str, str],
    visible: Mapping[str, Sequence[RetrievedChunk]],
    config: RunConfig,
    cap: CostCap,
    sink: ResultSink,
    progress: Callable[[str], None],
    functions: MetricFunctions = H2_METRICS,
    trace_ids: Callable[[], str] = new_trace_id,
    dry_run: bool = False,
) -> QuestionsOutcome:
    """Answer and score every question in order (the module docstring has the rules). Every
    question's text is validated first (`api_questions`): one that fails refuses the run."""
    asked = api_questions(questions)
    outcome = QuestionsOutcome()
    total = len(questions)
    for number, question in enumerate(questions, start=1):
        eval_id = mapping[question.business_id]
        if outcome.stop is not None:
            skipped = not_run(question, eval_id)
            outcome.results.append(skipped)
            sink(skipped, None)
            continue
        stored = visible[question.business_id]
        evidence = has_evidence(question)
        unscorable = unscorable_groups(question, stored) if evidence else ()
        unheld = unheld_keys(question, stored) if evidence else ()
        recorder.clear()
        trace_id = trace_ids()
        run: AnswerRun | None = None
        error: str | None = None
        try:
            run = await answer_service.answer(eval_id, asked[question.id], trace_id=trace_id)
        except NotImplementedError:
            raise  # H1 or H2 not written yet: never an outcome
        except HitPolicyViolation as violation:  # raised inside the search: the run aborts
            outcome.leak = outcome.stop = violation.report(question.id)
        except (ModelUnavailable, AnswerFailed) as failure:
            error = type(failure).__name__
        except CapReached as reached:
            outcome.stop = f"question {question.id}: {reached}"
        except asyncio.CancelledError:
            _uncancel()  # Ctrl-C: stop here and keep what was made (as M1's CLI)
            outcome.stop = f"interrupted during question {question.id}"
        except FusedListMismatch as broken:  # our own message: ranks and chunk ids, never text
            outcome.stop = f"question {question.id}: {broken}"
        except Exception as failure:  # the search, an embedding or database error, a bug
            outcome.stop = (
                f"question {question.id}: {type(failure).__name__} before its answer step finished"
            )
        if run is None and error is None:  # stopped during this question: it did not run
            skipped = not_run(question, eval_id)
            outcome.results.append(skipped)
            sink(skipped, None)
            continue
        scored = _score(
            question,
            eval_id,
            trace_id,
            (run, error),
            recorder,
            mapping,
            (unscorable, unheld),
            config,
            functions,
        )
        if isinstance(scored, CrossBusinessLeak):  # the second layer: the run aborts
            outcome.leak = outcome.stop = str(scored)
            skipped = not_run(question, eval_id)
            outcome.results.append(skipped)
            sink(skipped, None)
            continue
        result, answer = scored
        outcome.results.append(result)
        sink(result, answer)
        progress(_progress(number, total, result, config, cap, dry_run=dry_run))
        if cap.stop is not None:
            outcome.stop = f"after question {question.id}: {cap.stop}"
    return outcome


def _score(
    question: EvalQuestion,
    eval_id: str,
    trace_id: str,
    answered: tuple[AnswerRun | None, str | None],
    recorder: RecordingSearch,
    mapping: Mapping[str, str],
    keys: tuple[tuple[int, ...], tuple[tuple[int, int], ...]],
    config: RunConfig,
    functions: MetricFunctions,
) -> tuple[QuestionResult, AnswerLine] | CrossBusinessLeak:
    """The question's result and answer line, or the leak its hits show. `answered` is the
    answer step's run and, when it failed after the search, the failure's class name; `keys` the
    unscorable groups and the unheld keys at this config."""
    run, error = answered
    unscorable, unheld = keys
    lists = _recorded_lists(question, eval_id, recorder, config)
    ranked = lists.ranked
    if run is not None and run.hits != ranked:
        raise RuntimeError(f"question {question.id}: the answer's hits are not the search's")
    try:
        chunks = retrieved_chunks(question, eval_id, ranked, lists.businesses, mapping)
        components = _components(question, eval_id, lists, mapping)
    except CrossBusinessLeak as leak:
        return leak
    evidence: list[RetrievedChunk] | None = None
    if run is not None:
        held = {hit.chunk_id for hit in run.evidence}
        evidence = [
            chunk for hit, chunk in zip(ranked, chunks, strict=True) if hit.chunk_id in held
        ]
    score = score_retrieval(
        question,
        chunks,
        evidence,
        unscorable,
        metric_k=config.metric_k,
        search_k=config.search_k,
        functions=functions,
    )
    sources = () if run is None else _sources(run)
    status = "error" if run is None else run.response.outcome
    result = QuestionResult.model_validate(
        {
            **base_fields(question, eval_id),
            "retrieval": score.status,
            "unscorable_groups": score.unscorable,
            "unheld_keys": unheld,
            "hits": hit_refs(ranked, chunks, question),
            "evidence_chunk_ids": (
                None if run is None else tuple(str(hit.chunk_id) for hit in run.evidence)
            ),
            "hit_at_k": score.hit_at_k,
            "all_groups_at_k": score.all_groups_at_k,
            "reciprocal_rank": (
                None if score.reciprocal_rank is None else str(score.reciprocal_rank)
            ),
            "evidence_complete": score.evidence_complete,
            "outcome": status,
            "reason": None if run is None else run.reason,
            "error": error,
            "model_called": None if run is None else run.model_called,
            "sources": sources,
            "confidence": None if run is None else run.response.confidence.value,
            "trace_id": trace_id,
            "prompt_version": None if run is None else run.response.prompt_version,
            "components": components,
        }
    )
    answer = AnswerLine(
        question_id=question.id,
        trace_id=trace_id,
        expected_outcome=question.expected_outcome,
        outcome=status,
        reason=result.reason,
        error=error,
        answer=None if run is None else run.response.answer,
        sources=sources,
    )
    return result, answer


@dataclass(frozen=True)
class _Lists:
    """One question's recorded lists, checked against the run's config (`_recorded_lists`)."""

    ranked: tuple[SearchHit, ...]
    """What the answer kept and the run scores: the vector list, or in hybrid mode the fused one."""
    businesses: dict[uuid.UUID, str]
    """Every leg's document rows: {document id: business}."""
    vector: SearchCall
    text: SearchCall | None = None
    fuse: FuseCall | None = None


def _recorded_lists(
    question: EvalQuestion, eval_id: str, recorder: RecordingSearch, config: RunConfig
) -> _Lists:
    """The question's lists as the recorder saw them, refused (`RuntimeError`: a bug, never an
    outcome) unless they are exactly what the run's config asks of the answer step: one search
    of this business at `config.search_call_k`; in vector mode no other list, in hybrid mode one
    text leg at the same k and one fusion of exactly [vector hits, text hits] at the run's RRF k,
    limited to that k (review of PR 4, noah N2)."""
    k = config.search_call_k
    counts = (len(recorder.calls), len(recorder.text_calls), len(recorder.fuse_calls))
    if counts != ((1, 1, 1) if config.hybrid else (1, 0, 0)):
        raise RuntimeError(
            f"question {question.id}: the answer step must search exactly once"
            f" ({'both legs and one fusion' if config.hybrid else 'the vector leg only'})"
        )
    legs = [recorder.calls[0], *recorder.text_calls]
    for leg in legs:
        if leg.business_id != eval_id or leg.k != k:
            raise RuntimeError(f"question {question.id}: the search ran with another business or k")
        if len(leg.hits) > leg.k:
            raise RuntimeError(
                f"question {question.id}: the search returned {len(leg.hits)} hits for k = {leg.k}"
            )
    businesses: dict[uuid.UUID, str] = {}
    for leg in legs:
        businesses.update(leg.document_businesses)
    if not config.hybrid:
        return _Lists(ranked=legs[0].hits, businesses=businesses, vector=legs[0])
    fuse = recorder.fuse_calls[0]
    if (
        fuse.lists != (legs[0].hits, legs[1].hits)
        or fuse.rrf_k != config.retrieval.rrf_k
        or fuse.limit != k
    ):
        raise RuntimeError(
            f"question {question.id}: the fusion did not get [vector hits, text hits] at the"
            " run's RRF k and candidate k"
        )
    if len(fuse.fused) > fuse.limit:
        raise RuntimeError(
            f"question {question.id}: the fusion returned {len(fuse.fused)} hits for limit"
            f" {fuse.limit}"
        )
    return _Lists(
        ranked=tuple(item.hit for item in fuse.fused),
        businesses=businesses,
        vector=legs[0],
        text=legs[1],
        fuse=fuse,
    )


def _components(
    question: EvalQuestion, eval_id: str, lists: _Lists, mapping: Mapping[str, str]
) -> ComponentLists | None:
    """Hybrid only: each leg's list without text, each checked again by its rows' business
    (`retrieved_chunks`, the second layer; a leak there is the run's abort), with the recorder's
    own time per stage."""
    if lists.text is None or lists.fuse is None:
        return None
    legs = {}
    for name, leg in (("vector", lists.vector), ("text", lists.text)):
        chunks = retrieved_chunks(question, eval_id, leg.hits, leg.document_businesses, mapping)
        legs[name] = component_hits(leg.hits, chunks, question)
    return ComponentLists(
        vector=legs["vector"],
        text=legs["text"],
        harness=HarnessTime(
            vector_ms=lists.vector.harness_ms,
            text_ms=lists.text.harness_ms,
            fuse_ms=lists.fuse.harness_ms,
        ),
    )


def _progress(
    number: int,
    total: int,
    result: QuestionResult,
    config: RunConfig,
    cap: CostCap,
    *,
    dry_run: bool,
) -> str:
    """One line per question: ids, labels, H2's values and dollars (none in a dry run, whose
    usage is fake); never text."""
    head = (
        f"[{number:>{PROGRESS_WIDTH}}/{total}] {result.question_id}"
        f" ({result.category}, {result.language_group}): retrieval {result.retrieval}"
    )
    if result.retrieval == "scored" and result.reciprocal_rank is not None:
        rank = first_relevant_rank(Fraction(result.reciprocal_rank))
        head += (
            f" · hit@{config.metric_k} {'yes' if result.hit_at_k else 'no'}"
            f" · first relevant rank {rank if rank is not None else 'none'}"
        )
    answer = result.outcome + (f" ({result.error})" if result.error else "")
    total_text = "$0 (dry run: fake usage)" if dry_run else usd(cap.total)
    return f"{head} · answer {answer} · run total {total_text}"


# ── Wiring the product for a run ─────────────────────────────────────────────────────────────


@dataclass
class Parts:
    """The product's services, built for one run, plus the harness's seams."""

    kb: KbService
    answer: AnswerService
    recorder: RecordingSearch
    embedding_model: str


@asynccontextmanager
async def wired(
    settings: Settings,
    database: Database,
    config: RunConfig,
    cap: CostCap,
    *,
    dry_run: bool,
    records: Callable[[str | None], CallRecord | None],
    search: VectorSearch | None = None,
    lookup: DocumentLookup | None = None,
    text: TextSearch | None = None,
    fuse: Fuse | None = None,
) -> AsyncIterator[Parts]:
    """`KbService` and `AnswerService` as `create_app` builds them, with the capped embedder and
    gateway, the run's chunking, `RagConfig` and retrieval mode (`RunConfig.retrieval`, never
    Settings'), and `RecordingSearch` around every list. A dry run's answer model is
    `DryRunAnswerProvider`; a live run's is the one Settings name."""
    catalog = load_embedding_catalog()
    spec = catalog.resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    embedder = CappedEmbedder(build_embedder(settings, catalog), catalog.price(spec.model), cap)
    try:
        policy = settings.llm_retry_policy()
        runner = EmbeddingRunner(
            embedder,
            spec,
            policy,
            deadline_s=batch_deadline_s(policy, settings.embeddings_timeout_seconds),
        )
        recorder = RecordingSearch(search, lookup, text=text, fuse=fuse, name_lists=config.hybrid)
        gateway = CappedGateway(
            DryRunAnswerProvider() if dry_run else build_provider(settings),
            load_catalog(),
            attempt_timeout_s=settings.llm_attempt_timeout_seconds,
            deadline_s=settings.llm_deadline_seconds,
            retry_policy=policy,
            max_repairs=settings.llm_max_repairs,
            cap=cap,
            records=records,
        )
        try:
            yield Parts(
                kb=KbService(PostgresKbStore(database), runner, chunking=config.chunking),
                answer=AnswerService(
                    SearchService(
                        database,
                        runner,
                        search=recorder,
                        text=recorder.text_leg,
                        fuse=recorder.fuse,
                        retrieval=config.retrieval,
                    ),
                    gateway,
                    config.rag(),
                ),
                recorder=recorder,
                embedding_model=spec.model,
            )
        finally:
            await gateway.aclose()
    finally:
        await embedder.aclose()


@dataclass
class RunOutcome:
    """What `execute` did, for run.json and the summary."""

    status: RunStatus
    stop: str | None
    results: list[QuestionResult]
    ingest: tuple[IngestCount, ...] = ()
    registered: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Target:
    """What one `execute` works on."""

    corpus: Corpus
    corpus_dir: Path
    questions: tuple[EvalQuestion, ...]
    mapping: Mapping[str, str]
    """{dataset business: eval business} for every business of the dataset."""


def check_run(
    target: Target, settings: Settings, cap: CostCap, *, dry_run: bool, fresh: bool
) -> None:
    """Every refusal `execute` makes before it touches the database (`HarnessRefused`): a dry run
    whose Settings name a paid provider, a live run without a cap, a `--fresh` delete of an id
    that is not its business's derived eval id or in production, a question the API would refuse.
    The CLI checks the same before it prints the plan; this holds for any caller. (A hybrid
    search k above the candidate k cannot reach here: `RunConfig` refuses it when built.)"""
    if dry_run and (settings.embeddings_provider != "fake" or settings.llm_provider != "fake"):
        raise HarnessRefused(
            "a dry run needs the fake embedder and the fake answer model in Settings"
            f" (embeddings_provider {settings.embeddings_provider}, llm_provider"
            f" {settings.llm_provider}): no paid provider without --live"
        )
    if not dry_run and cap.cap is None:
        raise HarnessRefused("a live run needs a cap (--max-usd): no paid request without one")
    if fresh:
        if settings.environment == "production":
            raise HarnessRefused("--fresh never runs with environment production")
        for business, eval_id in target.mapping.items():
            if (
                not is_eval_business_id(eval_id)
                or not eval_id.startswith(f"{business}{EVAL_MARKER}")
                or dataset_business(eval_id, target.mapping) != business
            ):
                raise HarnessRefused(
                    f"--fresh refused: {eval_id} is not the derived eval business id of"
                    f" {business} (<business>-ev-<10 hex>); it deletes nothing else"
                )
    api_questions(target.questions)


def _uncancel() -> None:
    """Ctrl-C: keep going just long enough to write what was made (as M1's CLI)."""
    task = asyncio.current_task()
    if task is not None:
        task.uncancel()


@dataclass
class _SoFar:
    """What the steps before the questions have done, kept when they are interrupted."""

    registered: tuple[str, ...] = ()
    counts: tuple[IngestCount, ...] = ()


async def _register(
    store: EvalStore, mapping: Mapping[str, str], owner_url: str | None
) -> tuple[str, ...]:
    missing = [b for b in mapping.values() if not await store.business_exists(b)]
    if not missing:
        return ()
    if owner_url is None:
        raise HarnessRefused(
            f"{len(missing)} eval business(es) are not registered, and registering needs"
            " the owner's URL (VECTAPILOT_MIGRATION_DATABASE_URL)"
        )
    owner = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    try:
        return tuple(await register_businesses(owner, missing))
    finally:
        await owner.dispose()


async def _before_questions(
    target: Target,
    config: RunConfig,
    cap: CostCap,
    database: Database,
    parts: Parts,
    *,
    owner_url: str | None,
    progress: Callable[[str], None],
    fresh: bool,
    so_far: _SoFar,
) -> dict[str, tuple[RetrievedChunk, ...]] | RunOutcome:
    """Register, delete (`fresh`), ingest and check the stored chunks: the customer-visible
    stored chunks per business, or the outcome that ends the run here."""
    await database.verify_role()
    store = PostgresEvalStore(database)
    so_far.registered = await _register(store, target.mapping, owner_url)
    if fresh:
        for eval_id in target.mapping.values():
            deleted = await store.delete_documents(eval_id)
            progress(f"--fresh: deleted {deleted} document(s) of eval business {eval_id}")
    try:
        so_far.counts = await ingest_corpus(
            parts.kb,
            target.corpus,
            target.mapping,
            lambda business, name: (target.corpus_dir / business / name).read_bytes(),
            should_stop=lambda: cap.stop is not None,
        )
    except CapReached as reached:
        return RunOutcome("partial", f"ingest: {reached}", [], registered=so_far.registered)
    for count in so_far.counts:
        progress(
            f"ingest {count.business_id} → {count.eval_business_id}: {count.documents}"
            f" documents, {count.created} new, {count.chunks_written} chunks embedded"
        )
    if cap.stop is not None:
        return RunOutcome("partial", f"ingest: {cap.stop}", [], so_far.counts, so_far.registered)
    try:
        return await stored_visible(
            store, target.corpus, target.mapping, config.chunking, parts.embedding_model
        )
    except StaleChunks as stale:
        return RunOutcome(
            "failed", str(stale), [], so_far.counts, so_far.registered, problems=stale.problems
        )


async def execute(
    target: Target,
    settings: Settings,
    config: RunConfig,
    cap: CostCap,
    *,
    dry_run: bool,
    owner_url: str | None,
    records: Callable[[str | None], CallRecord | None],
    sink: ResultSink,
    progress: Callable[[str], None],
    fresh: bool = False,
    ingest_only: bool = False,
    search: VectorSearch | None = None,
    lookup: DocumentLookup | None = None,
    functions: MetricFunctions = H2_METRICS,
    text: TextSearch | None = None,
    fuse: Fuse | None = None,
) -> RunOutcome:
    """Register the eval businesses if needed, optionally delete their documents (`fresh`),
    ingest, check the stored chunks, then (unless `ingest_only`) answer and score every question.
    Connects as the app role from Settings; only registering uses `owner_url`. Refuses first
    what `check_run` refuses. A Ctrl-C before the questions ends the run `partial` with what was
    done so far (the cap has counted every paid request), so its folder is still written."""
    check_run(target, settings, cap, dry_run=dry_run, fresh=fresh)
    engine = create_async_engine(
        str(settings.database_url.get_secret_value()), poolclass=NullPool, hide_parameters=True
    )
    database = Database(engine)
    so_far = _SoFar()
    try:
        async with wired(
            settings,
            database,
            config,
            cap,
            dry_run=dry_run,
            records=records,
            search=search,
            lookup=lookup,
            text=text,
            fuse=fuse,
        ) as parts:
            try:
                prepared = await _before_questions(
                    target,
                    config,
                    cap,
                    database,
                    parts,
                    owner_url=owner_url,
                    progress=progress,
                    fresh=fresh,
                    so_far=so_far,
                )
            except asyncio.CancelledError:
                _uncancel()
                return RunOutcome(
                    "partial", INTERRUPTED_BEFORE_QUESTIONS, [], so_far.counts, so_far.registered
                )
            if isinstance(prepared, RunOutcome):  # a cap stop during ingest is partial here
                return prepared
            if ingest_only:
                return RunOutcome("complete", None, [], so_far.counts, so_far.registered)
            done = await run_questions(
                target.questions,
                answer_service=parts.answer,
                recorder=parts.recorder,
                mapping=target.mapping,
                visible=prepared,
                config=config,
                cap=cap,
                sink=sink,
                progress=progress,
                functions=functions,
                dry_run=dry_run,
            )
    finally:
        await engine.dispose()
    if done.leak is not None:
        return RunOutcome("aborted_leak", done.leak, done.results, so_far.counts, so_far.registered)
    return RunOutcome(final_status(done), done.stop, done.results, so_far.counts, so_far.registered)


def final_status(done: QuestionsOutcome) -> RunStatus:
    """`partial` when a stop left questions unrun; otherwise `complete`, also when a stop rule
    tripped after the last question (decided by shesha 2026-10-22, as M1's 2026-10-15 rule: every
    question ran, so it is not partial, but the stop stays in run.json and keeps the run out of
    the note, `report.status_of`)."""
    unrun = any(result.outcome == "not_run" for result in done.results)
    return "partial" if done.stop is not None and unrun else "complete"
