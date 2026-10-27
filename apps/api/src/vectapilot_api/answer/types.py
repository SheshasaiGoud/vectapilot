"""Typed values of the cited answer (M2 PR 6): the request, the model's schema, the response, the
outcome of one run, its metadata-only record and its errors."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    model_serializer,
)

from vectapilot_api.gateway.types import BusinessId, Count, GatewayModel, OpaqueId, TraceId
from vectapilot_api.retrieval.search import Citation, Question
from vectapilot_api.retrieval.types import MAX_K, SearchHit

AnswerOutcome = Literal["answered", "dont_know", "handoff"]
"""`answered`: a text whose every citation was retrieved for this business. `dont_know`: an
honest "I don't know", no text. `handoff`: the same, labelled for a human (nothing notifies staff
until M8)."""

WeakEvidenceOutcome = Literal["dont_know", "handoff"]

AbstainReason = Literal[
    "no_hits",
    "below_threshold",
    "model_dont_know",
    "empty_answer",
    "no_citation",
    "citation_unverified",
]
"""Why an answer was not shown. The first two never call the model."""

ROUTE: Final = "rag"
"""Every answer's route until M4's router chooses among routes."""


class AnswerRequest(BaseModel):
    """`POST /v1/answer`'s JSON body: the question stays out of the URL (and so out of logs)."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    question: Question


class ModelAnswer(BaseModel):
    """Your reply to the QUESTION block, built only from the DOCUMENT blocks."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    outcome: Literal["answered", "dont_know"] = Field(
        description='"answered" if the documents clearly answer the question, else "dont_know".'
    )
    answer: str = Field(
        default="",
        repr=False,
        description="The answer for the customer, in the question's language. Empty for dont_know.",
    )
    citations: list[str] = Field(
        default_factory=list,
        description="The id of every document the answer uses, copied exactly from its start"
        " marker line. Empty for dont_know.",
    )
    """Plain strings, not UUIDs: an id that is not a UUID is simply not a retrieved chunk (the
    service refuses the answer) rather than a schema error that costs a repair call."""


class Confidence(BaseModel):
    """v0: the top retrieval score (cosine similarity), labelled for what it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float | None
    """The best cosine among the hits the evidence gate saw: every retrieved hit in vector mode,
    the top `rag_context_k` fused hits in hybrid mode (ADR 0014). None when nothing was
    retrieved."""
    basis: Literal["top_retrieval_score"] = "top_retrieval_score"
    calibrated: Literal[False] = False
    """Not a probability of being right: calibrating it needs labelled eval data (PR 8, M3)."""


class AnswerResponse(BaseModel):
    outcome: AnswerOutcome
    answer: str | None
    """The text, only when `outcome` is `answered`; otherwise None (the client shows the
    business's own "I don't know" or hand-off message: none is hard-coded here)."""
    sources: list[Citation]
    """Exactly the cited chunks, in citation order: each was retrieved for this business in this
    request and put into this prompt. Empty unless `answered`."""
    confidence: Confidence
    route: Literal["rag"] = ROUTE
    trace_id: TraceId
    """New per request; this answer's `embedding_call`, `llm_call` and `rag_answer` records carry
    it too."""
    prompt_version: str


@dataclass(frozen=True)
class AnswerRun:
    """One answer and how it came about, for the route (which returns `response`) and for the eval
    harness (PR 8: the retrieved hits for hit@k, the reason for abstentions)."""

    response: AnswerResponse
    reason: AbstainReason | None
    """None when `answered`."""
    hits: tuple[SearchHit, ...] = field(repr=False)
    """Everything retrieved, best first. In hybrid mode (M3 PR 4) the whole fused list in fused
    order, as plain `SearchHit`s (each `score` the cosine): only its top `rag_context_k` reached
    the answer step, and the eval harness scores the whole list (M3 PR 5,
    `evalrun.harness`)."""
    evidence: tuple[SearchHit, ...] = field(repr=False)
    """The hits at or above the threshold: the chunks the prompt held (empty: no model call). In
    hybrid mode, those of the top `rag_context_k` fused hits whose cosine is at least
    `rag_min_score` (D7), in fused order."""
    model_called: bool


Milliseconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
"""A stage's duration on a monotonic clock, in milliseconds."""


class RetrievalRecord(GatewayModel):
    """The `retrieval` block of a hybrid answer's `rag_answer` record (M3 PR 4; ADR 0007 and ADR
    0011 amendments): how the search went, as counts and durations only, never text or ids.

    Omitted in vector mode, whose records stay exactly M2's. A count or duration is None when its
    stage did not finish (the search failed before or during it).
    """

    mode: Literal["hybrid"]
    candidate_k: int = Field(ge=1, le=MAX_K, strict=True)
    """The k each leg was asked for (`rag_candidate_k`, or the search's k if larger)."""
    context_k: int = Field(ge=1, le=MAX_K, strict=True)
    """How many of the top fused hits reached the evidence gate (`rag_context_k`; decided
    2026-10-25, shesha): with `candidate_k` it says how much of the fused list the answer saw."""
    rrf_k: int = Field(ge=1, strict=True)
    vector_hits: Count | None
    text_hits: Count | None
    fused_hits: Count | None
    """The fused list's length: `hits` again, once the search finished."""
    fts_only_hits: Count | None
    """Fused hits that only the full-text leg found (in the text list, not the vector list)."""
    fts_only_evidence: Count | None
    """Evidence hits that only the full-text leg found: what hybrid added to the prompt (D7's
    "FTS-only rescues"). None when the search did not finish."""
    embed_ms: Milliseconds | None
    transaction_ms: Milliseconds | None
    """Opening the business transaction to its commit: the model check, both legs, the fuse."""
    vector_ms: Milliseconds | None
    text_ms: Milliseconds | None
    fuse_ms: Milliseconds | None


class RagAnswerRecord(GatewayModel):
    """The `rag_answer` log record: one per answer, metadata only (never question, chunk or answer
    text), also on error. Joined to the `embedding_call` and `llm_call` records by `trace_id`.

    `record_version` stays 1 with M3 PR 4's optional `retrieval` block: ADR 0007's rule
    ("additive; a breaking change bumps it") governs ADR 0011's "a new `record_version`" (ADR
    0011 amendment; decided 2026-10-25, shesha). An M2 record, without the block, still
    validates, and a vector-mode record serializes without it, exactly as in M2.
    """

    record_version: Literal[1] = 1
    business_id: BusinessId
    trace_id: TraceId
    prompt_version: OpaqueId
    outcome: Literal["answered", "dont_know", "handoff", "error"]
    reason: AbstainReason | None
    error: OpaqueId | None
    """The failure's class name (ours or a library's: never a message) when `outcome` is error."""
    hits: Count
    """Hits retrieved: in hybrid mode the whole fused list (= `retrieval.fused_hits`), of which
    only the top `rag_context_k` reach the gate."""
    evidence: Count
    """Gated hits at or above `min_score`."""
    cited: Count
    """Distinct citations the model made, shown or not: one UUID in any spelling counts once, and
    each different text that is not a UUID counts on its own."""
    top_score: float | None = Field(allow_inf_nan=False)
    """The best cosine among the hits the evidence gate saw: every retrieved hit in vector mode,
    the top `rag_context_k` fused hits in hybrid mode (ADR 0014). None when nothing was
    retrieved."""
    min_score: float = Field(allow_inf_nan=False)
    model_called: StrictBool
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    retrieval: RetrievalRecord | None = None
    """Hybrid mode only (M3 PR 4); absent from the serialized record otherwise."""

    @model_serializer(mode="wrap")
    def _without_absent_retrieval(self, handler: SerializerFunctionWrapHandler) -> Any:
        # Every dump (the log line, `model_copy`, the harness's copy) leaves out an absent block,
        # so a vector-mode record has exactly M2's keys.
        data = handler(self)
        if self.retrieval is None and isinstance(data, dict):
            data.pop("retrieval", None)
        return data


class ModelUnavailable(Exception):
    """The answer model could not be reached in time (a transient failure or the deadline)."""


class AnswerFailed(Exception):
    """The answer model failed otherwise (refusal, truncation, invalid output, account or request
    errors). The `llm_call` record holds the gateway's own outcome."""
