"""One document's extraction, end to end (ADR 0017 decisions 3 to 5; E1, E3, E4, E9, E12; M6 PR 2).

    bytes → text (`text.read_pages`, E12) → one model call (`extractor.FieldExtractor`)
          → signals (`signals.compute_signals`) → routing (H1, `routing.route_fields`)
          → the invariant check (`invariants.enforce`) → `ExtractionOutcome`

**Refused before any model call.** A document over E12's limits or unreadable raises
`text.DocumentRefused` unchanged (PR 3's 413 or 415): nothing is sent, nothing is stored, nothing
is cut. A caller bug (another business's configuration, a malformed trace id) raises `ValueError`
before anything is read.

**Fail closed into review, never lose an upload (E3).** Every other `Exception` from reading the
pages (an OCR engine), the model call, the signals, the routing or the check becomes a stored
`needs_review` extraction with every field in review and no value, built here by team code, never
by H1: a `GatewayError` by kind (`failure_of`: `truncated`, `invalid_output`, `timeout`,
`model_unavailable`), anything else `internal_error`. Only the exception's type name is kept.
`BaseException` (cancellation) always propagates. Storage errors are PR 3's 503.

**Routing is injected.** Production uses `routing.route_fields` (H1, shesha's `[HAND-WRITE]`):
until it is written it raises `NotImplementedError`, so every extraction is all-review
(`internal_error`), fail closed. Tests pass a stand-in that is deliberately not H1
(tests/extraction/standins.py). Whatever routing returns, `invariants.enforce` runs after it.

**One `extraction` record per call (E9),** metadata only: business, trace id, kind, template,
prompt and rules versions, status, failure, the exception's type, pages and image pages, counts by
level, status and reason, invariant corrections, signal errors (a count and the distinct exception
type names, so a broken check is told from a routing slip) and duration. Never a value, a span,
a quote or document text; the gateway writes its own metadata-only `llm_call` record.
"""

import contextlib
import dataclasses
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from vectapilot_api.extraction.config import KindConfig
from vectapilot_api.extraction.extractor import Proposal
from vectapilot_api.extraction.invariants import CheckedDecision, enforce
from vectapilot_api.extraction.prompt import EXTRACT_PROMPT_VERSION
from vectapilot_api.extraction.routing import (
    DocumentSignals,
    DocumentStatus,
    ExtractionDecision,
    FieldDecision,
    FieldStatus,
    Level,
    Reason,
    ReviewPolicy,
    route_fields,
)
from vectapilot_api.extraction.signals import (
    EXTRACTION_RULES_VERSION,
    FieldEvidence,
    SignalReport,
    compute_signals,
)
from vectapilot_api.extraction.text import (
    DEFAULT_LIMITS,
    NO_OCR,
    DocumentRefused,
    DocumentText,
    OcrEngine,
    TextLimits,
    read_pages,
)
from vectapilot_api.gateway.errors import (
    DeadlineExceeded,
    GatewayError,
    OutputTruncated,
    StructuredOutputInvalid,
)
from vectapilot_api.gateway.types import LLMResult

logger = logging.getLogger(__name__)

EXTRACTION: Final = "extraction"
"""The log record's message and its `extra` key (E9)."""
_TRACE_ID: Final = re.compile(r"[0-9a-f]{32}")


class Extract(Protocol):
    """The model call (`extractor.FieldExtractor`)."""

    async def __call__(
        self, kind: KindConfig, document: DocumentText, *, business_id: str, trace_id: str
    ) -> Proposal: ...


Route = Callable[[DocumentSignals, ReviewPolicy], ExtractionDecision]
"""`routing.route_fields`'s signature."""


@dataclass(frozen=True)
class FieldResult:
    """One field as stored and returned (D6 (a)): value and span are untrusted data, never
    logged."""

    field: str
    status: FieldStatus
    level: Level | None
    reasons: frozenset[Reason]
    value: str | None = dataclasses.field(default=None, repr=False)
    span: str | None = dataclasses.field(default=None, repr=False)
    page: int | None = None
    quote_hits: int = 0


@dataclass(frozen=True)
class ExtractionOutcome:
    """One extraction: what PR 3 stores and returns, and what PR 4's eval scores."""

    business_id: str
    kind: str
    template_version: int
    status: DocumentStatus
    fields: tuple[FieldResult, ...]
    sha256: str | None
    """The file's SHA-256 (E10); None only if its pages could not be read (E3)."""
    pages: int
    image_pages: tuple[int, ...]
    failure: Reason | None = None
    error: str | None = None
    """The type name of the exception behind `failure`, if any (never its text)."""
    corrected: tuple[str, ...] = ()
    """Fields the invariant check moved to review (`routing_invariant`)."""
    signal_errors: tuple[str, ...] = ()
    """The type names of the checks that failed unexpectedly (`SignalReport.errors`, never their
    text): their fields went to review without signals (`missing_signal`). A count above zero is
    a bug or a hostile value on an untested path, never a routing slip."""
    signals: SignalReport | None = dataclasses.field(default=None, repr=False)
    """The facts behind the decisions (PR 4's results); None for a failure."""
    result: LLMResult | None = dataclasses.field(default=None, repr=False)
    """The model call's usage and cost (PR 4); None if no answer came back."""
    prompt_version: str = EXTRACT_PROMPT_VERSION
    rules_version: str = EXTRACTION_RULES_VERSION

    def field_of(self, name: str) -> FieldResult:
        return next(result for result in self.fields if result.field == name)


def failure_of(error: Exception) -> Reason:
    """E3's failure for an exception (module docstring)."""
    if isinstance(error, OutputTruncated):
        return Reason.TRUNCATED
    if isinstance(error, StructuredOutputInvalid):
        return Reason.INVALID_OUTPUT
    if isinstance(error, DeadlineExceeded | TimeoutError):
        return Reason.TIMEOUT
    if isinstance(error, GatewayError):
        return Reason.MODEL_UNAVAILABLE
    return Reason.INTERNAL_ERROR


def all_review(policy: ReviewPolicy, failure: Reason) -> ExtractionDecision:
    """Every field in review with `failure` and no level (E3), built by team code."""
    return ExtractionDecision.of(
        [
            FieldDecision(name, FieldStatus.REVIEW, None, frozenset({failure}))
            for name in policy.fields
        ]
    )


def check_context(kind: KindConfig, business_id: str, trace_id: str) -> None:
    """A caller bug raises `ValueError` before anything is read (E8: context is injected)."""
    if kind.business_id != business_id:
        raise ValueError("the kind's configuration belongs to another business")
    if _TRACE_ID.fullmatch(trace_id) is None or trace_id == "0" * 32:
        raise ValueError("trace_id must be a W3C trace id (32 lowercase hex, not all zeros)")


@dataclass
class _Tally:
    outcome: ExtractionOutcome | None = None
    refused: str | None = None


class ExtractionService:
    """Extract one document for one business's kind (module docstring)."""

    def __init__(
        self,
        extractor: Extract,
        *,
        route: Route = route_fields,
        ocr: OcrEngine = NO_OCR,
        limits: TextLimits = DEFAULT_LIMITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._extractor = extractor
        self._route = route
        self._ocr = ocr
        self._limits = limits
        self._clock = clock

    async def extract(
        self, kind: KindConfig, data: bytes, *, business_id: str, trace_id: str
    ) -> ExtractionOutcome:
        """The extraction of `data` under `business_id`'s `kind`, or `DocumentRefused`."""
        check_context(kind, business_id, trace_id)
        started = self._clock()
        tally = _Tally()
        error: BaseException | None = None
        try:
            tally.outcome = await self._extract(kind, data, business_id, trace_id, tally)
            return tally.outcome
        except BaseException as failure:  # recorded (type only), then re-raised unchanged
            error = failure
            raise
        finally:
            with contextlib.suppress(Exception):  # the record never replaces the result
                self._emit(kind, business_id, trace_id, tally, error, started)

    async def _extract(
        self, kind: KindConfig, data: bytes, business_id: str, trace_id: str, tally: _Tally
    ) -> ExtractionOutcome:
        policy = ReviewPolicy.for_kind(kind)
        document: DocumentText | None = None
        refused: DocumentRefused | None = None
        error = ""
        try:
            document = read_pages(data, limits=self._limits, ocr=self._ocr)
        except Exception as caught:  # type only (E9); a refusal is raised below, unchained
            if isinstance(caught, DocumentRefused):
                refused = caught
            error = type(caught).__name__
        if refused is not None:
            tally.refused = refused.reason.value
            raise refused
        if document is None:
            return self._failed(kind, policy, None, Reason.INTERNAL_ERROR, error)

        proposal: Proposal | None = None
        failure = Reason.INTERNAL_ERROR
        try:
            proposal = await self._extractor(
                kind, document, business_id=business_id, trace_id=trace_id
            )
        except Exception as caught:  # E3: fail closed into review, type only
            failure, error = failure_of(caught), type(caught).__name__
        if proposal is None:
            return self._failed(kind, policy, document, failure, error)

        checked: CheckedDecision | None = None
        report: SignalReport | None = None
        try:
            report = compute_signals(kind, document, proposal.answer)
            decision = self._route(report.document, policy)
            if not isinstance(decision, ExtractionDecision):
                raise TypeError("routing returned something other than an ExtractionDecision")
            checked = enforce(decision, report.document, policy)
        except Exception as caught:  # E3: H1 or a check failed; fail closed, type only
            error = type(caught).__name__
        if checked is None or report is None:
            return self._failed(kind, policy, document, Reason.INTERNAL_ERROR, error, proposal)
        return self._outcome(
            kind,
            document,
            checked.decision,
            report.evidence,
            corrected=checked.corrected,
            signals=report,
            result=proposal.result,
        )

    def _failed(
        self,
        kind: KindConfig,
        policy: ReviewPolicy,
        document: DocumentText | None,
        failure: Reason,
        error: str,
        proposal: Proposal | None = None,
    ) -> ExtractionOutcome:
        """E3's all-review extraction: no value, no level, the failure as the only reason."""
        return self._outcome(
            kind,
            document,
            all_review(policy, failure),
            {},
            failure=failure,
            error=error or None,
            result=None if proposal is None else proposal.result,
        )

    @staticmethod
    def _outcome(
        kind: KindConfig,
        document: DocumentText | None,
        decision: ExtractionDecision,
        evidence: Mapping[str, FieldEvidence],
        *,
        failure: Reason | None = None,
        error: str | None = None,
        corrected: tuple[str, ...] = (),
        signals: SignalReport | None = None,
        result: LLMResult | None = None,
    ) -> ExtractionOutcome:
        fields: list[FieldResult] = []
        for decided in decision.fields:
            found = evidence.get(decided.field) or FieldEvidence(decided.field)
            fields.append(
                FieldResult(
                    field=decided.field,
                    status=decided.status,
                    level=decided.level,
                    reasons=decided.reasons,
                    value=found.value,
                    span=found.span,
                    page=found.page,
                    quote_hits=found.quote_hits,
                )
            )
        return ExtractionOutcome(
            business_id=kind.business_id,
            kind=kind.kind,
            template_version=kind.template.version,
            status=decision.status,
            fields=tuple(fields),
            sha256=None if document is None else document.sha256,
            pages=0 if document is None else len(document.pages),
            image_pages=() if document is None else document.image_pages,
            failure=failure,
            error=error,
            corrected=corrected,
            signal_errors=() if signals is None else tuple(signals.errors),
            signals=signals,
            result=result,
        )

    # ── The record (E9) ──────────────────────────────────────────────────────────────────────

    def _emit(
        self,
        kind: KindConfig,
        business_id: str,
        trace_id: str,
        tally: _Tally,
        error: BaseException | None,
        started: float,
    ) -> None:
        outcome = tally.outcome
        fields = () if outcome is None else outcome.fields
        if outcome is not None:
            status = outcome.status.value
        elif tally.refused is not None:
            status = "refused"
        else:
            status = "error"
        record: dict[str, object] = {
            "business_id": business_id,
            "trace_id": trace_id,
            "kind": kind.kind,
            "template_version": kind.template.version,
            "prompt_version": EXTRACT_PROMPT_VERSION,
            "rules_version": EXTRACTION_RULES_VERSION,
            "status": status,
            "refused": tally.refused,
            "failure": None
            if outcome is None or outcome.failure is None
            else outcome.failure.value,
            "error": (
                type(error).__name__
                if error is not None and tally.refused is None
                else (None if outcome is None else outcome.error)
            ),
            "pages": None if outcome is None else outcome.pages,
            "image_pages": None if outcome is None else len(outcome.image_pages),
            "fields": len(fields),
            "by_status": dict(Counter(result.status.value for result in fields)),
            "by_level": dict(Counter("none" if r.level is None else r.level.value for r in fields)),
            "by_reason": dict(Counter(reason.value for r in fields for reason in r.reasons)),
            "routing_invariant": 0 if outcome is None else len(outcome.corrected),
            "signal_errors": 0 if outcome is None else len(outcome.signal_errors),
            "signal_error_types": [] if outcome is None else sorted(set(outcome.signal_errors)),
            "duration_ms": max(0.0, (self._clock() - started) * 1000),
        }
        logger.info(EXTRACTION, extra={EXTRACTION: record})
