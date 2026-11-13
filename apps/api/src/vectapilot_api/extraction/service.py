"""One document's extraction, end to end (ADR 0017 decisions 3 to 5; E1, E3, E4, E9, E12; M6 PR 2).

    bytes → text (`text.read_pages`, E12) → one model call (`extractor.FieldExtractor`)
          → signals (`signals.compute_signals`) → routing (H1, `routing.route_fields`)
          → the invariant check (`invariants.enforce`) → `ExtractionOutcome`

**Refused before any model call.** A document over E12's limits or unreadable raises
`text.DocumentRefused` unchanged (PR 3's 413 or 415): nothing is sent, nothing is stored, nothing
is cut. A caller bug (another business's configuration, a malformed trace id) raises `ValueError`
before anything is read.

**Fail closed into review, never lose an upload (E3).** Every other `Exception` from reading the
pages (an OCR engine), the model call, the signals, the routing, the check or building the outcome
becomes a stored `needs_review` extraction with every field in review and no value, built here by
team code, never by H1: a `GatewayError` by kind (`failure_of`: `truncated`, `invalid_output`,
`timeout`, `model_unavailable`), anything else `internal_error`. Only the exception's type name is
kept. `BaseException` (cancellation) always propagates. Storage errors are PR 3's 503.

**No quote, no value (E2; decided 2026-11-13 by shesha, M6 PR 3 review).** A field's value,
level, page and span are stored and returned only when E2 verified the value (`verified`): its
quote is in one page's text, the value is the quote's value, and the quote runs to its end (no
`value_cut`, E2's check for a value cut short with its quote). Otherwise the field keeps its status
and reasons and has none of them (`drop_unverified`, counted in the record as `unverified`): a
reviewer reads the value from the document, never from a guess no check confirmed. Team code
after the invariant check, so it holds whatever routing decides. An accepted field is always
verified (`invariants.may_accept` needs the same facts); if one were not, `FieldDecision` would
refuse it without its level and the extraction fails closed (E3).

**Every outcome fits its columns (D6 (a), migration 0007; M6 PR 3 review).** A model's value can
grow under NFKC (`signals.plain`: U+FDFA is 18 characters), so after the invariant check a value
longer than its own field's bound (`FieldSpec.value_bound`) is withheld: the field stays in
review with `too_long` and no value, level, page or span (`withheld`, counted in the record).
Then `storable` checks every value and span against the columns (length, no NUL, valid UTF-8);
an outcome that still does not fit is a bug or an input no check foresaw, and fails closed into
E3's all-review extraction (`internal_error`, `UnstorableValue`), so a hostile value can never
turn an upload into a 503.

**Pages are read off the event loop.** pypdf is synchronous pure Python: `read_pages` runs in a
worker thread (the app's own `page_reader`, `READ_WORKERS` at a time, or the loop's default
executor), so a slow or hostile PDF never blocks other requests and the route's time limit can
answer. A read that is cut off keeps its thread until pypdf returns (a hard kill needs a
subprocess: M9).

**Routing is injected.** Production uses `routing.route_fields` (H1, shesha's `[HAND-WRITE]`):
until it is written it raises `NotImplementedError`, so every extraction is all-review
(`internal_error`), fail closed. Tests pass a stand-in that is deliberately not H1
(tests/extraction/standins.py). Whatever routing returns, `invariants.enforce` runs after it.

**One `extraction` record per call (E9),** metadata only: business, trace id, kind, template,
prompt and rules versions, status, failure, the exception's type, pages and image pages, counts by
level, status and reason, invariant corrections, withheld and unverified values, signal errors (a
count and the distinct exception type names, so a broken check is told from a routing slip) and
duration. Never a value, a span, a quote or document text; the gateway writes its own
metadata-only `llm_call` record.
"""

import asyncio
import contextlib
import dataclasses
import functools
import logging
import re
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final, Protocol

from vectapilot_api.extraction.config import TEXT_MAX, KindConfig
from vectapilot_api.extraction.extractor import Proposal
from vectapilot_api.extraction.invariants import CheckedDecision, enforce
from vectapilot_api.extraction.prompt import EXTRACT_PROMPT_VERSION
from vectapilot_api.extraction.routing import (
    DocumentSignals,
    DocumentStatus,
    ExtractionDecision,
    FieldDecision,
    FieldSignals,
    FieldStatus,
    Level,
    Reason,
    ReviewPolicy,
    route_fields,
)
from vectapilot_api.extraction.signals import (
    EXTRACTION_RULES_VERSION,
    SPAN_MAX,
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
STORED_VALUE_MAX: Final = TEXT_MAX
"""The longest value stored or returned: the longest `FieldSpec.value_bound` (a text field's
`max_length` is at most `TEXT_MAX`; identifiers, dates, enum ids and synonyms are shorter).
Migration 0007's `VALUE_MAX`; tests/test_migrations.py pins both to the config's bounds."""
READ_WORKERS: Final = 1
"""Threads that read PDFs for one app (`page_reader`); decided 2026-11-13 (shesha): 1 for M6,
revisited with M9's subprocess hard kill. A hostile PDF holds it until pypdf returns, and the next
upload waits for it (and may get the route's timeout) instead of taking another thread."""


class UnstorableValue(Exception):
    """The type name recorded when an outcome does not fit its columns (`storable`)."""


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
    withheld: tuple[str, ...] = ()
    """Fields whose value was longer than the field's bound, so it was withheld: in review with
    `too_long`, no value, level, page or span (module docstring)."""
    unverified: tuple[str, ...] = ()
    """Fields whose value E2 did not verify (`verified`), so it was dropped: the same status and
    reasons, no value, level, page or span (module docstring)."""
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


def withhold(decided: FieldDecision) -> FieldDecision:
    """`decided` once its value is withheld: review with `too_long`, and no level (only a value
    has one). Team code after the invariant check: it only ever moves a field towards review."""
    return FieldDecision(
        decided.field, FieldStatus.REVIEW, None, decided.reasons | {Reason.TOO_LONG}
    )


def verified(signals: FieldSignals | None) -> bool:
    """Whether E2 verified the field's value, so it may be stored and returned (module
    docstring): the quote was found, the value is in its quote, and the quote was not cut short
    with it. A field without signals is never verified."""
    return (
        signals is not None
        and signals.quote_found
        and signals.value_in_quote
        and Reason.VALUE_CUT not in signals.codes
    )


def drop_unverified(decided: FieldDecision) -> FieldDecision:
    """`decided` once its unverified value is dropped: the same status and reasons, and no level
    (only a value has one). `FieldDecision` refuses an accepted field without its level: never
    the case after the invariant check (module docstring), and a slip fails closed (E3)."""
    return FieldDecision(decided.field, decided.status, None, decided.reasons)


def fits(text: str | None, limit: int) -> bool:
    """Whether `text` fits a stored text column: None, or 1 to `limit` characters of valid UTF-8
    without NUL (Postgres refuses NUL in `text` and `jsonb`; a lone surrogate cannot be sent)."""
    if text is None:
        return True
    if not 1 <= len(text) <= limit or "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def storable(outcome: "ExtractionOutcome") -> bool:
    """Whether every value and span of `outcome` fits migration 0007's columns
    (`STORED_VALUE_MAX`, `signals.SPAN_MAX`). The service never returns one that does not; the
    store refuses one as a caller bug."""
    return all(
        fits(result.value, STORED_VALUE_MAX) and fits(result.span, SPAN_MAX)
        for result in outcome.fields
    )


@contextlib.asynccontextmanager
async def page_reader() -> AsyncIterator[Executor]:
    """The app's PDF reading threads (`READ_WORKERS`). At exit the pool is shut down without
    waiting: queued reads are cancelled, a running one finishes on its own."""
    pool = ThreadPoolExecutor(max_workers=READ_WORKERS, thread_name_prefix="extraction-read")
    try:
        yield pool
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


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
        reader: Executor | None = None,
    ) -> None:
        self._extractor = extractor
        self._route = route
        self._ocr = ocr
        self._limits = limits
        self._clock = clock
        self._reader = reader
        """Where `read_pages` runs (`page_reader`); None is the loop's default executor."""

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
            document = await self._read(data)
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

        outcome: ExtractionOutcome | None = None
        try:
            report = compute_signals(kind, document, proposal.answer)
            decision = self._route(report.document, policy)
            if not isinstance(decision, ExtractionDecision):
                raise TypeError("routing returned something other than an ExtractionDecision")
            checked: CheckedDecision = enforce(decision, report.document, policy)
            outcome = self._outcome(
                kind,
                document,
                checked.decision,
                report.evidence,
                corrected=checked.corrected,
                signals=report,
                result=proposal.result,
            )
        except Exception as caught:  # E3: H1, a check or the outcome failed; fail closed
            error = type(caught).__name__
        if outcome is None:
            return self._failed(kind, policy, document, Reason.INTERNAL_ERROR, error, proposal)
        if storable(outcome):
            return outcome
        # E3: a value no check foresaw would make the store refuse the upload (a 503); fail
        # closed into review instead, with no value (module docstring).
        unstorable = UnstorableValue.__name__
        return self._failed(kind, policy, document, Reason.INTERNAL_ERROR, unstorable, proposal)

    async def _read(self, data: bytes) -> DocumentText:
        """`read_pages` in a worker thread (module docstring): a cancel stops the wait at once,
        while the read keeps its thread until pypdf returns."""
        loop = asyncio.get_running_loop()
        read = functools.partial(read_pages, data, limits=self._limits, ocr=self._ocr)
        return await loop.run_in_executor(self._reader, read)

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
        bounds = {spec.name: spec.value_bound for spec in kind.template.fields}
        facts = {} if signals is None else {s.name: s for s in signals.document.fields}
        decisions: list[FieldDecision] = []
        fields: list[FieldResult] = []
        withheld: list[str] = []
        unverified: list[str] = []
        for decided in decision.fields:
            found = evidence.get(decided.field) or FieldEvidence(decided.field)
            if found.value is not None and len(found.value) > bounds.get(decided.field, 0):
                decided = withhold(decided)  # the value is longer than its field allows
                found = FieldEvidence(decided.field, quote_hits=found.quote_hits)
                withheld.append(decided.field)
            if found.value is not None and not verified(facts.get(decided.field)):
                decided = drop_unverified(decided)  # no quote, no value (E2)
                found = FieldEvidence(decided.field, quote_hits=found.quote_hits)
                unverified.append(decided.field)
            decisions.append(decided)
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
            status=ExtractionDecision.of(decisions).status,  # a withheld value is in review
            fields=tuple(fields),
            sha256=None if document is None else document.sha256,
            pages=0 if document is None else len(document.pages),
            image_pages=() if document is None else document.image_pages,
            failure=failure,
            error=error,
            corrected=corrected,
            withheld=tuple(withheld),
            unverified=tuple(unverified),
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
            "withheld": 0 if outcome is None else len(outcome.withheld),
            "unverified": 0 if outcome is None else len(outcome.unverified),
            "signal_errors": 0 if outcome is None else len(outcome.signal_errors),
            "signal_error_types": [] if outcome is None else sorted(set(outcome.signal_errors)),
            "duration_ms": max(0.0, (self._clock() - started) * 1000),
        }
        logger.info(EXTRACTION, extra={EXTRACTION: record})
