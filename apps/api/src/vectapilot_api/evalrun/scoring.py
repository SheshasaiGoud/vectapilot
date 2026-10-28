"""One question's retrieval, scored by content, and the record a run keeps of it (M2 PR 8).

Pure: values in, values out. The harness (`harness.py`) feeds it what one answer retrieved.

**A hit's business comes from its document row**, never from the question (ADR 0012, plan PR 8):
the harness reads each hit's document row (`store.document_rows`: business, status, audience) in
the search's own transaction, and `check_hit_rows` checks every hit of every question, scored or
not, **as the search returns it, before the answer step goes on**: a hit whose document belongs
to another business, or is not visible in this one, raises `SearchLeak` (business-context §6:
zero cross-business leaks is a hard gate); a hit whose document a customer may never see (not
`approved`, or an audience outside `VISIBLE_AUDIENCES["customer"]`) raises `AudienceLeak`
(business-context §3.4). Either aborts the run loudly, whatever the answer step would have done
next. `retrieved_chunks` then builds each `relevance.RetrievedChunk` with the row's business,
mapped back from the run's eval namespace to the dataset's id (`namespace.dataset_business`),
and checks it again (`CrossBusinessLeak`, a second layer). So each scored run is also a leak check.

**Three counts, nothing dropped.** A question without evidence groups is *not scored*
(`ranking.select_for_retrieval`). A question with a group whose every key no stored,
customer-visible chunk holds at this config is *unscorable at this config* (`unscorable_groups`):
a chunking artefact, not a retrieval miss, reported as its own count. Every other question is
*scored*, an empty hit list included (a miss). Unscorability is decided **per group** (any key
finds a group, ADR 0012); a single key no stored chunk holds, in a group another key still finds,
is kept too (`unheld_keys`, by index) and counted in the report.

**Hybrid runs (M3 PR 5).** The ranked list is the fused one, and `ComponentLists` keeps each
leg's list per question (chunk ids, ranks, matched groups; no text) with the harness's own time
inside each stage (`HarnessTime`). A leak line then names its list ("rank 2 of the text leg");
vector-mode lines stay exactly M2's.

**The metrics are H2's** (`evalset/metrics.py`, shesha's [HAND-WRITE]), passed in as a
`MetricFunctions` bundle (default `H2_METRICS`) so local tests can use stand-ins that return
prepared values; nothing here computes hit@k, a reciprocal rank or a mean itself. hit@k and MRR@k
are taken on the search's ranked list before the threshold (`AnswerRun.hits`); "evidence
complete" is `all_groups_at_k` on the hits the prompt held (`AnswerRun.evidence`), a separate,
threshold-dependent diagnostic, never called hit@k.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Annotated, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from vectapilot_api.evalrun.namespace import dataset_business
from vectapilot_api.evalset import metrics
from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.evalset.ranking import RankedRelevance, check_k, has_evidence, ranked_relevance
from vectapilot_api.evalset.relevance import RetrievedChunk, hit_matches_key, matched_groups
from vectapilot_api.retrieval.types import VISIBLE_AUDIENCES, SearchHit

CROSS_BUSINESS_LEAK: Final = "CROSS-BUSINESS LEAK"
AUDIENCE_LEAK: Final = "AUDIENCE LEAK"
CUSTOMER_AUDIENCES: Final = VISIBLE_AUDIENCES["customer"]
"""What a customer's search may return (`AnswerService` searches as `customer`)."""

# ── H2, as the harness calls it ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricFunctions:
    """The five H2 functions the harness calls (signatures as `evalset/metrics.py`)."""

    hit_at_k: Callable[[RankedRelevance, int], bool]
    all_groups_at_k: Callable[[RankedRelevance, int], bool]
    reciprocal_rank: Callable[[RankedRelevance, int], Fraction]
    mean_hit_at_k: Callable[[Sequence[RankedRelevance], int], Fraction]
    mean_reciprocal_rank: Callable[[Sequence[RankedRelevance], int], Fraction]


H2_METRICS: Final = MetricFunctions(
    hit_at_k=metrics.hit_at_k,
    all_groups_at_k=metrics.all_groups_at_k,
    reciprocal_rank=metrics.reciprocal_rank,
    mean_hit_at_k=metrics.mean_hit_at_k,
    mean_reciprocal_rank=metrics.mean_reciprocal_rank,
)
"""shesha's H2. Until it is written each function raises `NotImplementedError`, which the harness
never catches: a run then fails, and its tests are strict xfails (`tests/canary/read_paths.py`,
`PENDING_HAND_WRITE`, "retrieval_metrics")."""


def check_metric_k(metric_k: int, search_k: int) -> None:
    """Every metric k is at most the k the search ran with (decided 2026-10-22, D6): hit@20 on a
    5-hit list would report a shallow search under a deeper label. Asserted for every run."""
    check_k(metric_k)
    check_k(search_k)
    if metric_k > search_k:
        raise ValueError(
            f"metric k ({metric_k}) must be at most the search's k ({search_k}): a deeper label"
            " than the search would report a shallow search as a deep one"
        )


# ── The leak check and the business of a hit ─────────────────────────────────────────────────


class CrossBusinessLeak(Exception):
    """A hit whose document is not the question's business: the run aborts. Ids only."""

    def __init__(
        self, question_id: str, rank: int, document_id: uuid.UUID, row_business: str | None
    ) -> None:
        found = "is not visible in it" if row_business is None else f"belongs to {row_business}"
        super().__init__(
            f"{CROSS_BUSINESS_LEAK}: question {question_id}, rank {rank}: document {document_id}"
            f" {found}, not the question's business"
        )
        self.question_id = question_id
        self.rank = rank


@dataclass(frozen=True)
class DocumentRow:
    """What the harness reads of a hit's document row (`store.document_rows`): no text."""

    business_id: str
    status: str
    audience: str


class HitPolicyViolation(Exception):
    """A hit a customer's search must never return, seen as the search returned it. Raised inside
    the search (`harness.RecordingSearch`), so nothing after the search runs; the question loop
    turns it into the run's abort line with `report`. Ids and labels only, never text."""

    def __init__(
        self, kind: str, rank: int, document_id: uuid.UUID, detail: str, where: str = ""
    ) -> None:
        super().__init__(f"{kind} at rank {rank}{where}: document {document_id} {detail}")
        self.kind = kind
        self.rank = rank
        self.document_id = document_id
        self.detail = detail
        self.where = where
        """Which list, in a hybrid run (" of the text leg"); empty in vector mode (M2's line)."""

    def report(self, question_id: str) -> str:
        """The run's abort line for `question_id`."""
        return (
            f"{self.kind}: question {question_id}, rank {self.rank}{self.where}: document"
            f" {self.document_id} {self.detail}"
        )


class SearchLeak(HitPolicyViolation):
    """A hit whose document row is another business's, or not visible in the searched one. Its
    `report` reads exactly as `CrossBusinessLeak`'s message."""

    def __init__(
        self, rank: int, document_id: uuid.UUID, row_business: str | None, where: str = ""
    ) -> None:
        found = "is not visible in it" if row_business is None else f"belongs to {row_business}"
        super().__init__(
            CROSS_BUSINESS_LEAK, rank, document_id, f"{found}, not the question's business", where
        )
        self.row_business = row_business


class AudienceLeak(HitPolicyViolation):
    """A hit of the searched business whose document a customer may never see: not approved, or
    an audience outside `CUSTOMER_AUDIENCES` (business-context §3.4)."""

    def __init__(
        self, rank: int, document_id: uuid.UUID, status: str, audience: str, where: str = ""
    ) -> None:
        super().__init__(
            AUDIENCE_LEAK,
            rank,
            document_id,
            f"is status {status}, audience {audience}; a customer's search must return only"
            " approved documents of a customer audience",
            where,
        )
        self.status = status
        self.audience = audience


def check_hit_rows(
    eval_business_id: str,
    hits: Sequence[SearchHit],
    rows: Mapping[uuid.UUID, DocumentRow],
    where: str = "",
) -> None:
    """Raise for the first hit, best first, whose document row is missing (not visible in the
    searched business) or another business's (`SearchLeak`), or that a customer may never see
    (`AudienceLeak`). The business is checked first: a leak is the harder gate. `where` names
    the list in a hybrid run (" of the text leg"), after the rank."""
    for rank, hit in enumerate(hits, start=1):
        row = rows.get(hit.document_id)
        if row is None or row.business_id != eval_business_id:
            raise SearchLeak(rank, hit.document_id, None if row is None else row.business_id, where)
        if row.status != "approved" or row.audience not in CUSTOMER_AUDIENCES:
            raise AudienceLeak(rank, hit.document_id, row.status, row.audience, where)


def retrieved_chunks(
    question: EvalQuestion,
    eval_business_id: str,
    hits: Sequence[SearchHit],
    document_businesses: Mapping[uuid.UUID, str],
    mapping: Mapping[str, str],
) -> tuple[RetrievedChunk, ...]:
    """The hits as `RetrievedChunk`s, each with its document row's business (mapped to the
    dataset's id). Raises `CrossBusinessLeak` for the first hit whose row is missing (not visible
    in the question's business) or names any business but the question's eval business."""
    chunks: list[RetrievedChunk] = []
    for rank, hit in enumerate(hits, start=1):
        row_business = document_businesses.get(hit.document_id)
        if row_business != eval_business_id:
            raise CrossBusinessLeak(question.id, rank, hit.document_id, row_business)
        business = dataset_business(row_business, mapping)
        if business != question.business_id:  # the mapping itself disagrees: never trust it
            raise CrossBusinessLeak(question.id, rank, hit.document_id, row_business)
        chunks.append(
            RetrievedChunk(
                business_id=business,
                source_id=hit.source_id,
                version=hit.version,
                content=hit.content,
            )
        )
    return tuple(chunks)


def unscorable_groups(question: EvalQuestion, stored: Sequence[RetrievedChunk]) -> tuple[int, ...]:
    """The indexes (from 0) of the question's evidence groups that no key of which any stored,
    customer-visible chunk holds (`relevance.hit_matches_key`, the scoring rule itself). Empty
    when every group can be found; a question with any is unscorable at this config."""
    return tuple(
        index
        for index, group in enumerate(question.expected_evidence)
        if not any(
            hit_matches_key(chunk, question.business_id, key) for key in group for chunk in stored
        )
    )


def unheld_keys(
    question: EvalQuestion, stored: Sequence[RetrievedChunk]
) -> tuple[tuple[int, int], ...]:
    """Every evidence key, as (group index, key index) from 0, that no stored, customer-visible
    chunk holds at this config (`relevance.hit_matches_key`). A group all of whose keys are here
    is unscorable (`unscorable_groups`); a key here whose group another key still finds is not,
    but the report counts it, so no key disappears silently."""
    return tuple(
        (group_index, key_index)
        for group_index, group in enumerate(question.expected_evidence)
        for key_index, key in enumerate(group)
        if not any(hit_matches_key(chunk, question.business_id, key) for chunk in stored)
    )


# ── What a run keeps per question (results.jsonl) ────────────────────────────────────────────


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class HitRef(_Closed):
    """One retrieved chunk, without its text: where it is, its score, the groups it finds."""

    rank: int = Field(ge=1)
    chunk_id: str
    document_id: str
    source_id: str
    version: str
    chunk_index: int = Field(ge=0)
    page: int | None
    score: float
    groups: tuple[int, ...]
    """`relevance.matched_groups` for this hit (empty: not relevant, or a question with none)."""


class SourceRef(_Closed):
    """One cited source of an answer, without its text."""

    chunk_id: str
    source_id: str
    version: str
    chunk_index: int
    page: int | None
    score: float


RetrievalStatus = Literal["scored", "not_scored", "unscorable", "not_run"]
AnswerStatus = Literal["answered", "dont_know", "handoff", "error", "not_run"]
ComponentName = Literal["vector", "text"]
Milliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ComponentHit(_Closed):
    """One hit of one leg's list in a hybrid run (M3 PR 5): its rank, chunk id and the groups it
    finds. No text, and no score: the fused list (`QuestionResult.hits`) carries each hit's
    cosine."""

    rank: int = Field(ge=1)
    chunk_id: str
    groups: tuple[int, ...]


class HarnessTime(_Closed):
    """The recorder's own time inside one hybrid search's stages, in milliseconds on the search's
    monotonic clock (noah N4): in each leg, its document lookup (one query) and the leak check; in
    the fusion, the check of the fused list (no query). `summarize` subtracts it from the
    `rag_answer` record's stage durations."""

    vector_ms: Milliseconds
    text_ms: Milliseconds
    fuse_ms: Milliseconds

    @property
    def total_ms(self) -> float:
        return self.vector_ms + self.text_ms + self.fuse_ms


class ComponentLists(_Closed):
    """A hybrid run's per-question record of each leg's list as the fusion got it (M3 PR 5; ADR
    0013 amendment 2026-10-23): ids, ranks and matched groups, no text. The fused list is
    `QuestionResult.hits`."""

    vector: tuple[ComponentHit, ...]
    text: tuple[ComponentHit, ...]
    harness: HarnessTime


class QuestionResult(_Closed):
    """One line of results.jsonl: everything the summary is computed from, for one question.

    No question, chunk or answer text: ids, labels, ranks, scores and H2's values. The per-question
    metric values are H2's outputs at `metric_k`, kept for traceability; `report.summarize`
    recomputes them from `hits[].groups` and refuses a file where they disagree.
    """

    question_id: str
    business_id: str
    eval_business_id: str
    category: str
    subtype: str | None
    language_group: str
    author_type: str
    split_group: str | None
    expected_outcome: str
    n_groups: int = Field(ge=0)
    retrieval: RetrievalStatus
    unscorable_groups: tuple[int, ...] = ()
    unheld_keys: tuple[tuple[int, int], ...] = ()
    """`scoring.unheld_keys`: (group, key) indexes no stored chunk holds at this config."""
    hits: tuple[HitRef, ...] = ()
    """The search's ranked list (best first), before the score threshold."""
    evidence_chunk_ids: tuple[str, ...] | None = None
    """The hits the prompt held (`AnswerRun.evidence`); None when the answer step did not finish."""
    hit_at_k: bool | None = None
    all_groups_at_k: bool | None = None
    reciprocal_rank: str | None = None
    """H2's `reciprocal_rank` as exact text (`"1/3"`, `"0"`)."""
    evidence_complete: bool | None = None
    outcome: AnswerStatus
    reason: str | None = None
    """`AnswerRun.reason` (why an answer was not shown), or None."""
    error: str | None = None
    """The exception's class name when the answer step failed (`AnswerFailed`, …)."""
    model_called: bool | None = None
    sources: tuple[SourceRef, ...] = ()
    confidence: float | None = None
    trace_id: str | None = None
    prompt_version: str | None = None
    components: ComponentLists | None = None
    """Hybrid only (M3 PR 5): each leg's list, and the harness's own time; `hits` is then the
    fused list. Absent from the line in vector mode, so a vector line is exactly M2's."""

    @model_serializer(mode="wrap")
    def _without_absent_components(self, handler: SerializerFunctionWrapHandler) -> object:
        data = handler(self)
        if self.components is None and isinstance(data, dict):
            data.pop("components", None)
        return data

    def ranked(self) -> RankedRelevance:
        """The H2 record of the search's ranked list (scored questions only): in a hybrid run,
        the fused list."""
        return RankedRelevance(
            question_id=self.question_id,
            n_groups=self.n_groups,
            groups_by_rank=tuple(hit.groups for hit in self.hits),
        )

    def ranked_component(self, name: ComponentName) -> RankedRelevance:
        """The H2 record of one leg's list (a hybrid run's scored questions only)."""
        if self.components is None:
            raise ValueError(f"question {self.question_id} has no component lists")
        hits = self.components.vector if name == "vector" else self.components.text
        return RankedRelevance(
            question_id=self.question_id,
            n_groups=self.n_groups,
            groups_by_rank=tuple(hit.groups for hit in hits),
        )

    def ranked_evidence(self) -> RankedRelevance | None:
        """The H2 record of the prompt's evidence, in rank order; None without an answer."""
        if self.evidence_chunk_ids is None:
            return None
        held = set(self.evidence_chunk_ids)
        return RankedRelevance(
            question_id=self.question_id,
            n_groups=self.n_groups,
            groups_by_rank=tuple(hit.groups for hit in self.hits if hit.chunk_id in held),
        )


class AnswerLine(_Closed):
    """One line of answers.jsonl: what the system replied, for the human correctness label.

    The only file of a run that holds model text (`answer`, null unless answered): synthetic
    answers about fictional businesses, kept so a label can always be checked against the answer
    it judged. Never in a log line or a record (ADR 0007).
    """

    question_id: str
    trace_id: str
    expected_outcome: str
    outcome: AnswerStatus
    reason: str | None
    error: str | None
    answer: str | None = Field(repr=False)
    sources: tuple[SourceRef, ...]


def base_fields(question: EvalQuestion, eval_business_id: str) -> dict[str, object]:
    """What every result line says about its question (no text)."""
    return {
        "question_id": question.id,
        "business_id": question.business_id,
        "eval_business_id": eval_business_id,
        "category": question.category,
        "subtype": question.subtype,
        "language_group": question.language_group,
        "author_type": question.author_type,
        "split_group": question.split_group,
        "expected_outcome": question.expected_outcome,
        "n_groups": len(question.expected_evidence),
    }


def not_run(question: EvalQuestion, eval_business_id: str) -> QuestionResult:
    """A selected question the run never reached (it stopped before it)."""
    return QuestionResult.model_validate(
        {**base_fields(question, eval_business_id), "retrieval": "not_run", "outcome": "not_run"}
    )


def fraction_text(value: Fraction) -> str:
    return str(value)


def hit_refs(
    hits: Sequence[SearchHit], chunks: Sequence[RetrievedChunk], question: EvalQuestion
) -> tuple[HitRef, ...]:
    """The ranked list without text, each hit with the groups it finds."""
    return tuple(
        HitRef(
            rank=rank,
            chunk_id=str(hit.chunk_id),
            document_id=str(hit.document_id),
            source_id=hit.source_id,
            version=hit.version,
            chunk_index=hit.chunk_index,
            page=hit.page,
            score=hit.score,
            groups=matched_groups(chunk, question),
        )
        for rank, (hit, chunk) in enumerate(zip(hits, chunks, strict=True), start=1)
    )


def component_hits(
    hits: Sequence[SearchHit], chunks: Sequence[RetrievedChunk], question: EvalQuestion
) -> tuple[ComponentHit, ...]:
    """One leg's list without text or score: rank, chunk id, the groups each hit finds."""
    return tuple(
        ComponentHit(rank=rank, chunk_id=str(hit.chunk_id), groups=matched_groups(chunk, question))
        for rank, (hit, chunk) in enumerate(zip(hits, chunks, strict=True), start=1)
    )


@dataclass(frozen=True)
class RetrievalScore:
    status: RetrievalStatus
    unscorable: tuple[int, ...]
    hit_at_k: bool | None
    all_groups_at_k: bool | None
    reciprocal_rank: Fraction | None
    evidence_complete: bool | None


def score_retrieval(
    question: EvalQuestion,
    chunks: Sequence[RetrievedChunk],
    evidence: Sequence[RetrievedChunk] | None,
    unscorable: tuple[int, ...],
    *,
    metric_k: int,
    search_k: int,
    functions: MetricFunctions,
) -> RetrievalScore:
    """H2's values for one question (None where the question is not scored)."""
    if not has_evidence(question):
        return RetrievalScore("not_scored", (), None, None, None, None)
    if unscorable:
        return RetrievalScore("unscorable", unscorable, None, None, None, None)
    record = ranked_relevance(question, chunks)
    complete = None
    if evidence is not None:
        complete = functions.all_groups_at_k(ranked_relevance(question, evidence), search_k)
    return RetrievalScore(
        status="scored",
        unscorable=(),
        hit_at_k=functions.hit_at_k(record, metric_k),
        all_groups_at_k=functions.all_groups_at_k(record, metric_k),
        reciprocal_rank=functions.reciprocal_rank(record, metric_k),
        evidence_complete=complete,
    )


def first_relevant_rank(reciprocal_rank: Fraction) -> int | None:
    """The rank of the first relevant hit, read off H2's reciprocal rank (1/r → r); None for 0.
    Raises `ValueError` when the value is not 0 or 1/r (H2 would be wrong)."""
    if reciprocal_rank == 0:
        return None
    if reciprocal_rank.numerator != 1 or reciprocal_rank.denominator < 1:
        raise ValueError("a reciprocal rank must be 0 or 1/r for a whole rank r")
    return reciprocal_rank.denominator
