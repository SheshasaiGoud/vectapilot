"""The cited answer (M2 PR 6): retrieve, decide, prompt, call the gateway, verify the citations.

One answer, in this order (every step for one business, scoped by its id):

1. **Retrieve** (`SearchService`): embed the question, refuse mixed models, H1's search as
   reader `customer`, `context_k` hits. In hybrid mode (M3 PR 4, `rag_retrieval_mode`): both
   legs and the fusion in one snapshot, `candidate_k` fused hits, of which the top `context_k`
   go on (ADR 0014 decision 5); the whole fused list stays in `AnswerRun.hits` for the harness.
2. **Abstain early.** The evidence is the hits (in hybrid mode: of those top `context_k`)
   scoring at least `min_score` (provisional, unmeasured: Settings). The score is always the
   cosine, never an RRF score (D7): a keyword-stuffed chunk that fusion ranks first is still no
   evidence if its cosine is low. No hits, or none at the threshold, means **no model call at
   all**: the answer is the weak-evidence outcome (`dont_know` by default, or `handoff`), with
   no text.
3. **Prompt** (`prompt.py`, versioned): the evidence only, each chunk in an unforgeable block.
4. **The gateway** (`complete_structured`, alias `large`, operation `rag-answer`, the request's
   trace id): the reply must match `ModelAnswer`; a schema repair is the gateway's job.
5. **Verify, fail closed. No source, no claim.** The answer is shown only if the model said
   `answered`, wrote a text, cited at least one id, and **every** cited id is a chunk this prompt
   held (so: retrieved for this business in this request). Anything else becomes the weak-evidence
   outcome with no text: one unretrieved id drops the whole answer, because the text cannot be
   split into the claims that id was meant to support.
6. **One `rag_answer` record**, metadata only, also on error. In hybrid mode it carries a
   `retrieval` block (the k's, counts per leg, FTS-only hits and evidence, stage durations); in
   vector mode it is exactly M2's record.

Errors: the search's own (embedding, `ReindexRequired`, database) propagate unchanged for the
route to map; the model's become `ModelUnavailable` (transient, deadline) or `AnswerFailed`
(anything else), raised outside the `except` block so nothing is chained.
"""

import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Self

from vectapilot_api.answer.prompt import PROMPT_VERSION, AnswerPrompt, build_prompt
from vectapilot_api.answer.types import (
    AbstainReason,
    AnswerFailed,
    AnswerOutcome,
    AnswerResponse,
    AnswerRun,
    Confidence,
    ModelAnswer,
    ModelUnavailable,
    RagAnswerRecord,
    RetrievalRecord,
    WeakEvidenceOutcome,
)
from vectapilot_api.config import Settings
from vectapilot_api.gateway.errors import DeadlineExceeded, GatewayError, Transient
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.types import LLMRequest, Message
from vectapilot_api.retrieval.search import (
    Citation,
    SearchService,
    SearchStats,
    fts_only,
    top_score,
)
from vectapilot_api.retrieval.types import Reader, SearchHit

ANSWER_ALIAS: Final = "large"
"""ADR 0006: `large` is the alias for a grounded support answer with citations (models.toml)."""
ANSWER_OPERATION: Final = "rag-answer"
"""The operation of this answer's `embedding_call` and `llm_call` records."""
CUSTOMER: Final[Reader] = "customer"
RAG_ANSWER: Final = "rag_answer"
"""The log message of the one record per answer, and the key that holds the record."""

logger = logging.getLogger("vectapilot_api.answer")
# Pinned at INFO like the gateway's: a quieter `log_level` must never drop the records.
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class RagConfig:
    """Platform defaults until per-business configuration exists (rule 9); see Settings."""

    min_score: float
    """Provisional and unmeasured (Settings.rag_min_score): tuned on the dev split in PR 8."""
    context_k: int
    weak_evidence_outcome: WeakEvidenceOutcome

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            min_score=settings.rag_min_score,
            context_k=settings.rag_context_k,
            weak_evidence_outcome=settings.rag_weak_evidence_outcome,
        )


@dataclass
class _Tally:
    """What one answer left behind, for its record."""

    hits: int = 0
    evidence: int = 0
    cited: int = 0
    top_score: float | None = None
    model_called: bool = False
    outcome: AnswerOutcome | None = None
    reason: AbstainReason | None = None
    search: SearchStats | None = None
    """Hybrid mode only: what the search did (filled in by `SearchService` as it runs)."""
    fts_only_evidence: int | None = None


def _cited(citations: Sequence[str]) -> list[uuid.UUID | None]:
    """One entry per distinct citation, in order: its UUID, or None for a citation that is not a
    UUID at all (so never a retrieved chunk). Repeats are removed on a normalised key, before
    mapping: a UUID's canonical form (two spellings of one UUID count once), else the stripped
    text (two different non-UUIDs count twice: the record's `cited` counts what the model cited).
    """
    distinct: dict[str, uuid.UUID | None] = {}
    for citation in citations:
        stripped = citation.strip()
        parsed = _as_uuid(stripped)
        distinct.setdefault(stripped if parsed is None else str(parsed), parsed)
    return list(distinct.values())


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


class AnswerService:
    """Cited answers for one process: a `SearchService`, the LLM gateway and `RagConfig`."""

    def __init__(
        self,
        search: SearchService,
        gateway: LLMGateway,
        config: RagConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._search = search
        self._gateway = gateway
        self.config = config
        self._clock = clock

    async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun:
        """Answer `question` for `business_id` from its approved public chunks, or abstain."""
        started = self._clock()
        tally = _Tally()
        error: BaseException | None = None
        try:
            return await self._answer(business_id, question, trace_id, tally)
        except BaseException as failure:  # recorded only, then re-raised unchanged
            error = failure
            raise
        finally:
            # A failure here must never replace the answer or the exception.
            with contextlib.suppress(Exception):
                self._emit(business_id, trace_id, tally, error, started)

    async def _answer(
        self, business_id: str, question: str, trace_id: str, tally: _Tally
    ) -> AnswerRun:
        retrieval = self._search.retrieval
        hybrid = retrieval.mode == "hybrid"
        k = self.config.context_k  # vector: M2's k
        if hybrid:  # the whole fused list, for the harness; only its top context_k go on
            k = max(retrieval.candidate_k, self.config.context_k)
            tally.search = SearchStats()
        result = await self._search.search(
            business_id,
            question,
            k=k,
            reader=CUSTOMER,
            trace_id=trace_id,
            operation=ANSWER_OPERATION,
            stats=tally.search,
        )
        hits = result.hits
        # Hybrid: the top context_k fused hits go on (ADR 0014 decision 5). Vector: M2's hits, as
        # they came (the search was asked for context_k).
        context = hits[: self.config.context_k] if hybrid else hits
        # D7: `hit.score` is the cosine in both modes. A FusedHit's RRF score never gets here.
        evidence = tuple(hit for hit in context if hit.score >= self.config.min_score)
        tally.hits, tally.evidence, tally.top_score = len(hits), len(evidence), top_score(context)
        if hybrid:
            rescued = fts_only(result.fused or ())
            tally.fts_only_evidence = sum(hit.chunk_id in rescued for hit in evidence)
        confidence = Confidence(value=tally.top_score)

        def abstain(reason: AbstainReason) -> AnswerRun:
            tally.outcome, tally.reason = self.config.weak_evidence_outcome, reason
            response = AnswerResponse(
                outcome=self.config.weak_evidence_outcome,
                answer=None,
                sources=[],
                confidence=confidence,
                trace_id=trace_id,
                prompt_version=PROMPT_VERSION,
            )
            return AnswerRun(response, reason, hits, evidence, tally.model_called)

        if not evidence:
            return abstain("below_threshold" if hits else "no_hits")  # no model call

        prompt = build_prompt(question, evidence)
        tally.model_called = True
        reply = await self._ask(business_id, trace_id, prompt)
        cited = _cited(reply.citations)
        tally.cited = len(cited)
        if reply.outcome == "dont_know":
            return abstain("model_dont_know")
        text = reply.answer.strip()
        if not text:
            return abstain("empty_answer")
        if not cited:
            return abstain("no_citation")  # no source, no claim
        held = {hit.chunk_id: hit for hit in evidence}  # exactly the chunks this prompt held
        sources: list[SearchHit] = []
        for chunk_id in cited:
            hit = None if chunk_id is None else held.get(chunk_id)
            if hit is None:
                return abstain("citation_unverified")  # never shown: the whole answer is dropped
            sources.append(hit)
        tally.outcome = "answered"
        response = AnswerResponse(
            outcome="answered",
            answer=text,
            sources=[Citation.of(hit) for hit in sources],
            confidence=confidence,
            trace_id=trace_id,
            prompt_version=PROMPT_VERSION,
        )
        return AnswerRun(response, None, hits, evidence, model_called=True)

    async def _ask(self, business_id: str, trace_id: str, prompt: AnswerPrompt) -> ModelAnswer:
        request = LLMRequest(
            business_id=business_id,
            alias=ANSWER_ALIAS,
            system=prompt.system,
            messages=(Message(role="user", content=prompt.user),),
            trace_id=trace_id,
            operation=ANSWER_OPERATION,
        )
        failure: Exception
        try:
            structured = await self._gateway.complete_structured(request, ModelAnswer)
        except (Transient, DeadlineExceeded):
            failure = ModelUnavailable()
        except GatewayError:
            failure = AnswerFailed()
        else:
            return structured.value
        raise failure  # outside the `except`: nothing is chained to it

    def _emit(
        self,
        business_id: str,
        trace_id: str,
        tally: _Tally,
        error: BaseException | None,
        started: float,
    ) -> None:
        record = RagAnswerRecord(
            business_id=business_id,
            trace_id=trace_id,
            prompt_version=PROMPT_VERSION,
            outcome="error" if error is not None or tally.outcome is None else tally.outcome,
            reason=tally.reason,
            error=None if error is None else type(error).__name__,
            hits=tally.hits,
            evidence=tally.evidence,
            cited=tally.cited,
            top_score=tally.top_score,
            min_score=self.config.min_score,
            model_called=tally.model_called,
            latency_ms=max(0.0, (self._clock() - started) * 1000),
            retrieval=None if tally.search is None else self._retrieval_record(tally),
        )
        logger.info(RAG_ANSWER, extra={RAG_ANSWER: record.model_dump(mode="json")})

    def _retrieval_record(self, tally: _Tally) -> RetrievalRecord:
        """The hybrid record's `retrieval` block: counts and durations, never text or ids."""
        stats = tally.search or SearchStats()
        retrieval = self._search.retrieval
        return RetrievalRecord(
            mode="hybrid",
            candidate_k=stats.candidate_k or max(retrieval.candidate_k, self.config.context_k),
            context_k=self.config.context_k,
            rrf_k=retrieval.rrf_k,
            vector_hits=stats.vector_hits,
            text_hits=stats.text_hits,
            fused_hits=stats.fused_hits,
            fts_only_hits=stats.fts_only_hits,
            fts_only_evidence=tally.fts_only_evidence,
            embed_ms=stats.embed_ms,
            transaction_ms=stats.transaction_ms,
            vector_ms=stats.vector_ms,
            text_ms=stats.text_ms,
            fuse_ms=stats.fuse_ms,
        )
