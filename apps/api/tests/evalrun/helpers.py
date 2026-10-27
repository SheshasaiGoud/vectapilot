"""Test doubles for the eval harness (M2 PR 8): no Postgres, no network, no paid call.

- `question(...)` builds an obviously fake (TEST-ONLY) `EvalQuestion`; the anchors are real quotes
  from the synthetic corpus (`data/sample`), so the corpus-based tests can resolve them.
- `ScriptedSearch` stands in for H1's `vector_search`: the n-th call returns the n-th prepared
  hit list. It runs no SQL and ranks nothing (H1 is shesha's [HAND-WRITE]).
- `ScriptedTextSearch` does the same for M3 H1's `text_search` (no matching), and hybrid runs fuse
  with `tests.retrieval.standins.StandInFuse`, which is deliberately not RRF (M3 H2 is shesha's
  [HAND-WRITE]): both on the team's side of the line, as in M2.
- `PreparedLookup` stands in for `store.document_rows`: prepared {document id: business, status,
  audience}.
- `PreparedMetrics` stands in for H2 (shesha's [HAND-WRITE]): every value is prepared and looked
  up, nothing is computed. Per-question values are keyed by question id; the two means are
  hand-written literals keyed by the set of question ids they are asked for (a set it does not
  know raises `KeyError`). A bootstrap resample (ids renamed `q~3`) gets a stand-in statistic
  that is deliberately not a mean: `position_weighted` of the resample's prepared reciprocal
  ranks (weights 1/2, 1/4, ... by position). It depends on every record and its place, so the
  seed, the resampling unit and the resample count each change the interval (the mutation audit's
  M7b-d); equal values give that value exactly. So it is no implementation of hit@k, MRR or their
  means.
- `harness_parts(...)` builds the real `AnswerService` over those stand-ins, with the capped
  embedder (the fake) and the capped gateway, and captures the gateway's records; with
  `retrieval` in hybrid mode, the search runs both legs and the fusion through the recorder.
"""

import io
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection

from tests.embeddings.scripted import FAKE_SPEC, POLICY, FakeClock, ScriptedEmbedder
from tests.retrieval.standins import FakeDatabase, StandInFuse, StoredModels
from vectapilot_api.answer.service import AnswerService, RagConfig
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.evalrun.budget import CappedEmbedder, CappedGateway, CostCap, record_for
from vectapilot_api.evalrun.dryrun import DryRunAnswerProvider
from vectapilot_api.evalrun.harness import RecordingSearch
from vectapilot_api.evalrun.report import (
    ConfigInfo,
    CostInfo,
    RetrievalInfo,
    RunInfo,
    language_map_sha256,
)
from vectapilot_api.evalrun.scoring import DocumentRow, MetricFunctions
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.evalset.files import read_corpus
from vectapilot_api.evalset.questions import EvalQuestion, SourceKey
from vectapilot_api.evalset.ranking import RankedRelevance
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.llm_cli import LogCapture
from vectapilot_api.retrieval.search import RetrievalConfig, SearchService
from vectapilot_api.retrieval.types import SearchHit, TextQuery, VectorQuery

REPO: Final = Path(__file__).resolve().parents[4]
CORPUS_DIR: Final = REPO / "data" / "sample"
SUNRISE: Final = "sunrise-clinic"
PAWS: Final = "paws-and-co"

SUNRISE_HOURS: Final = SourceKey(
    source_id="faq", version="2026-10", anchor="Saturday: 9:00 to 14:00. Sunday: closed."
)
SUNRISE_PARKING: Final = SourceKey(
    source_id="faq", version="2026-10", anchor="free parking for 12 cars behind the building"
)
PAWS_STORE_HOURS: Final = SourceKey(
    source_id="faq", version="2026-09", anchor="Pet store and grooming: every day, 10:00 to 21:00."
)

SENTINEL_QUESTION: Final = "TEST-ONLY sentinel question text zq7"
"""A question text that must never reach a log line, a progress line or a record."""


def question(
    question_id: str,
    business_id: str = SUNRISE,
    *,
    evidence: Sequence[Sequence[SourceKey]] = ((SUNRISE_HOURS,),),
    category: str = "faq",
    expected: str = "answered",
    language: str = "en",
    text: str | None = None,
    split: str | None = None,
    split_group: str | None = None,
) -> EvalQuestion:
    """A TEST-ONLY question (never part of a dataset in `evals/datasets`)."""
    return EvalQuestion.model_validate(
        {
            "id": question_id,
            "split_group": split_group,
            "business_id": business_id,
            "question": text or f"TEST-ONLY question {question_id}",
            "language": language,
            "category": category,
            "author_type": "team",
            "saw_documents": True,
            "expected_outcome": expected,
            "expected_evidence": [list(group) for group in evidence],
            "split": split,
            "created": date(2026, 10, 22),
            "synthetic": True,
        }
    )


def corpus() -> Corpus:
    """The real synthetic sample corpus, both businesses."""
    return read_corpus(CORPUS_DIR, [SUNRISE, PAWS])


def hit(
    content: str,
    score: float = 0.9,
    *,
    source_id: str = "faq",
    version: str = "2026-10",
    chunk_index: int = 0,
    document_id: uuid.UUID | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4() if document_id is None else document_id,
        source_id=source_id,
        version=version,
        title="TEST-ONLY title",
        language="en",
        content_kind="faq",
        page=None,
        chunk_index=chunk_index,
        content=content,
        score=score,
        embedding_model=FAKE_SPEC.model,
    )


@dataclass
class ScriptedSearch:
    """H1's stand-in: call n returns `script[n]` (at most k hits, unless `truncate` is off to
    fake an H1 that ignores k). No SQL, no ranking."""

    script: list[list[SearchHit]]
    raises: BaseException | None = None
    calls: list[VectorQuery] = field(default_factory=list)
    truncate: bool = True

    async def __call__(self, connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        self.calls.append(query)
        if self.raises is not None:
            raise self.raises
        hits = list(self.script.pop(0))
        return hits[: query.k] if self.truncate else hits


@dataclass
class ScriptedTextSearch:
    """M3 H1's stand-in, as `ScriptedSearch` is M2 H1's: call n returns `script[n]` (at most k).
    No SQL, no matching, no ranking."""

    script: list[list[SearchHit]]
    calls: list[TextQuery] = field(default_factory=list)

    async def __call__(self, connection: AsyncConnection, query: TextQuery) -> list[SearchHit]:
        self.calls.append(query)
        return list(self.script.pop(0))[: query.k]


@dataclass
class PreparedLookup:
    """`store.document_rows`' stand-in: prepared rows; a missing id is not visible."""

    rows: dict[uuid.UUID, DocumentRow] = field(default_factory=dict)
    ticks: int = 0
    """Readings of `clock` per call: the lookup "takes" that many steps of a ticking clock."""
    clock: Callable[[], float] | None = None

    def own(
        self,
        business_id: str,
        *hits: SearchHit,
        status: str = "approved",
        audience: str = "public",
    ) -> list[SearchHit]:
        """Give each hit's document a row of `business_id`; return the hits."""
        for item in hits:
            self.rows[item.document_id] = DocumentRow(business_id, status, audience)
        return list(hits)

    async def __call__(
        self, connection: AsyncConnection, document_ids: Sequence[uuid.UUID]
    ) -> Mapping[uuid.UUID, DocumentRow]:
        if self.clock is not None:
            for _ in range(self.ticks):
                self.clock()
        return {i: self.rows[i] for i in document_ids if i in self.rows}


def _base(question_id: str) -> str:
    """A bootstrap resample renames `q` to `q~3`; prepared values are keyed by `q`."""
    return question_id.split("~", 1)[0]


def position_weighted(values: Sequence[Fraction]) -> Fraction:
    """The stand-in's resample statistic, deliberately NOT a mean (and not MRR): the values
    weighted 1/2, 1/4, ..., 1/2^(n-1) by position, the last weight doubled so they sum to 1.
    Order-dependent on purpose: swapping two different values changes it."""
    n = len(values)
    weights = [Fraction(1, 2 ** (i + 1)) for i in range(n - 1)] + [Fraction(1, 2 ** (n - 1))]
    return sum((w * v for w, v in zip(weights, values, strict=True)), Fraction(0))


@dataclass
class PreparedMetrics:
    """H2's stand-in: prepared values only (module docstring)."""

    hits: dict[str, bool] = field(default_factory=dict)
    all_groups: dict[str, bool] = field(default_factory=dict)
    rr: dict[str, Fraction] = field(default_factory=dict)
    evidence_all: dict[str, bool] = field(default_factory=dict)
    mean_hits: dict[frozenset[str], Fraction] = field(default_factory=dict)
    """Hand-written `mean_hit_at_k` per set of question ids."""
    mean_rrs: dict[frozenset[str], Fraction] = field(default_factory=dict)
    """Hand-written `mean_reciprocal_rank` per set of question ids."""
    calls: list[tuple[str, str, int]] = field(default_factory=list)
    given: list[tuple[str, RankedRelevance]] = field(default_factory=list)
    """(function, record) for every per-question call: what the harness handed H2."""
    mean_hit_offset: Fraction = Fraction(0)
    """Added to `mean_hit_at_k` to fake an H2 whose mean disagrees with its per-question values."""
    mean_rr_offset: Fraction = Fraction(0)
    """Added to `mean_reciprocal_rank` (not on resamples), for the same purpose."""
    metric_k: int = 5
    """The run's metric k: `all_groups_at_k` at any other k is the evidence-complete check."""

    def prepare(
        self, question_id: str, *, hit: bool, rr: Fraction, all_groups: bool | None = None
    ) -> None:
        self.hits[question_id] = hit
        self.rr[question_id] = rr
        self.all_groups[question_id] = hit if all_groups is None else all_groups

    def prepare_mean(self, ids: Iterable[str], *, hit: Fraction, mrr: Fraction) -> None:
        """The two means for exactly this set of question ids, written out by hand."""
        key = frozenset(ids)
        self.mean_hits[key] = hit
        self.mean_rrs[key] = mrr

    def functions(self) -> MetricFunctions:
        def hit_at_k(record: RankedRelevance, k: int) -> bool:
            self.calls.append(("hit_at_k", record.question_id, k))
            self.given.append(("hit_at_k", record))
            return self.hits[_base(record.question_id)]

        def all_groups_at_k(record: RankedRelevance, k: int) -> bool:
            self.calls.append(("all_groups_at_k", record.question_id, k))
            self.given.append(("all_groups_at_k", record))
            if record.question_id in self.evidence_all and k != self.metric_k:
                return self.evidence_all[record.question_id]
            return self.all_groups[_base(record.question_id)]

        def reciprocal_rank(record: RankedRelevance, k: int) -> Fraction:
            self.calls.append(("reciprocal_rank", record.question_id, k))
            self.given.append(("reciprocal_rank", record))
            return self.rr[_base(record.question_id)]

        def mean_hit_at_k(records: Sequence[RankedRelevance], k: int) -> Fraction:
            self.calls.append(("mean_hit_at_k", f"{len(records)} records", k))
            return self.mean_hits[frozenset(r.question_id for r in records)] + self.mean_hit_offset

        def mean_reciprocal_rank(records: Sequence[RankedRelevance], k: int) -> Fraction:
            self.calls.append(("mean_reciprocal_rank", f"{len(records)} records", k))
            if any(r.question_id != _base(r.question_id) for r in records):
                # A bootstrap resample: a stand-in statistic, deliberately not a mean.
                return position_weighted([self.rr[_base(r.question_id)] for r in records])
            return self.mean_rrs[frozenset(r.question_id for r in records)] + self.mean_rr_offset

        return MetricFunctions(
            hit_at_k=hit_at_k,
            all_groups_at_k=all_groups_at_k,
            reciprocal_rank=reciprocal_rank,
            mean_hit_at_k=mean_hit_at_k,
            mean_reciprocal_rank=mean_reciprocal_rank,
        )


@dataclass
class Parts:
    answer: AnswerService
    recorder: RecordingSearch
    search: ScriptedSearch
    lookup: PreparedLookup
    cap: CostCap
    capture: LogCapture
    embedder: ScriptedEmbedder
    text: ScriptedTextSearch
    fuse: StandInFuse


@contextmanager
def harness_parts(
    script: list[list[SearchHit]],
    eval_ids: Sequence[str],
    *,
    provider: Provider | None = None,
    cap: CostCap | None = None,
    embedding_price: Decimal = Decimal(0),
    retry_policy: RetryPolicy | None = None,
    context_k: int = 5,
    retrieval: RetrievalConfig | None = None,
    text_script: list[list[SearchHit]] | None = None,
    fuse: StandInFuse | None = None,
    lookup: PreparedLookup | None = None,
    clock: Callable[[], float] | None = None,
) -> Iterator[Parts]:
    """The real `AnswerService`, over `ScriptedSearch` + `PreparedLookup`, the capped fake
    embedder and the capped gateway (dry-run answers unless `provider`); records captured. In
    hybrid mode (`retrieval`) the text leg is `ScriptedTextSearch(text_script)` and the fusion
    `fuse` (default `StandInFuse()`), both through the recorder. `clock`, if given, is the one
    monotonic clock of the answer, the search, the recorder and the transaction's commit."""
    cap = cap or CostCap(cap=None)
    fake = FakeClock()  # the runner's and the gateway's own clock (deadlines, sleeps)
    embedder = ScriptedEmbedder()
    runner = EmbeddingRunner(
        CappedEmbedder(embedder, embedding_price, cap),
        FAKE_SPEC,
        POLICY,
        deadline_s=100.0,
        clock=fake,
        sleep=fake.sleep,
        rand=lambda: 0.5,
    )
    search = ScriptedSearch(script)
    lookup = PreparedLookup() if lookup is None else lookup
    text = ScriptedTextSearch([] if text_script is None else text_script)
    fuse = StandInFuse() if fuse is None else fuse
    timed: dict[str, Any] = {} if clock is None else {"clock": clock}
    hybrid = retrieval is not None and retrieval.mode == "hybrid"
    recorder = RecordingSearch(search, lookup, text=text, fuse=fuse, name_lists=hybrid, **timed)
    service = SearchService(
        FakeDatabase(clock=clock),
        runner,
        search=recorder,
        stored_models=StoredModels({b: frozenset({FAKE_SPEC.model}) for b in eval_ids}),
        text=recorder.text_leg,
        fuse=recorder.fuse,
        retrieval=retrieval,
        **timed,
    )
    capture = LogCapture(io.StringIO())
    gateway_logger.addHandler(capture)
    try:
        gateway = CappedGateway(
            provider or DryRunAnswerProvider(),
            load_catalog(),
            attempt_timeout_s=5.0,
            deadline_s=10.0,
            retry_policy=retry_policy
            or RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0),
            max_repairs=1,
            cap=cap,
            records=lambda trace_id: record_for(capture.lines, trace_id),
            clock=fake,
            sleep=fake.sleep,
            rand=lambda: 0.5,
        )
        answer = AnswerService(
            service,
            gateway,
            RagConfig(min_score=0.3, context_k=context_k, weak_evidence_outcome="dont_know"),
            **timed,
        )
        yield Parts(answer, recorder, search, lookup, cap, capture, embedder, text, fuse)
    finally:
        gateway_logger.removeHandler(capture)


# ── A run.json for report and CLI tests ──────────────────────────────────────────────────────

RUN_ID: Final = "20261022T000000Z-fake-dev"
CONFIG: Final = ConfigInfo(
    max_chars=1200,
    overlap_chars=150,
    search_k=6,
    metric_k=5,
    rag_min_score=0.3,
    weak_evidence_outcome="dont_know",
    llm_max_retries=2,
    llm_max_repairs=1,
)


HYBRID_RETRIEVAL: Final = RetrievalInfo(
    mode="hybrid", candidate_k=20, rrf_k=60, language_map_sha256=language_map_sha256()
)
HYBRID_CONFIG: Final = CONFIG.model_copy(update={"retrieval": HYBRID_RETRIEVAL})
"""`CONFIG` as a hybrid run records it (candidate k 20, RRF k 60, today's language map)."""


def run_info(**changes: Any) -> RunInfo:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "status": "complete",
        "stop": None,
        "dry_run": True,
        "environment": "test",
        "dataset": "support_qa",
        "dataset_version": "v0.1",
        "dataset_stage": "frozen",
        "dataset_synthetic": True,
        "corpus_synthetic": True,
        "freeze_sha256": "0" * 64,
        "dataset_committed": True,
        "corpus_review_pending": 0,
        "split": "dev",
        "questions_selected": 0,
        "commit": "a" * 40,
        "uncommitted_changes": False,
        "started_at": datetime(2026, 10, 22, 9, tzinfo=UTC),
        "ended_at": datetime(2026, 10, 22, 9, 5, tzinfo=UTC),
        "embeddings_provider": "fake",
        "embedding_model": "fake-embedding-1024",
        "embedding_models_answered": ("fake-embedding-1024",),
        "llm_provider": "fake",
        "answer_alias": "large",
        "answer_model": "claude-sonnet-5-5",
        "answer_models_answered": ("claude-sonnet-5-5",),
        "catalog_version": "2026-10-14.1",
        "prompt_version": "rag-answer.1",
        "config": CONFIG,
        "config_fingerprint": "f" * 64,
        "namespaces": {SUNRISE: "sunrise-clinic-ev-aaaaaaaaaa", PAWS: "paws-and-co-ev-bbbbbbbbbb"},
        "confidence": "19/20",
        "bootstrap_seed": 20261022,
        "bootstrap_resamples": 200,
        "rate_min_n": 10,
        "bootstrap_min_n": 10,
        "cost": CostInfo(
            cap_usd=None,
            embedding_usd=0,
            llm_usd="0.0001",
            total_usd="0.0001",
            llm_cost_complete=True,
            embedding_requests=12,
            llm_calls=10,
        ),
        "ingest": (),
    }
    return RunInfo.model_validate(values | changes)
