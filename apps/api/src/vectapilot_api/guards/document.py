"""The document screen on `POST /v1/extractions` (M7 PR 7; ADR 0018; G1, G3, G5, G7, G10, G13).

`ExtractionGuard` is what the route calls while `guard_routes = "on"` (built in `create_app`,
handed to the route as an object: no middleware, no screening dependency, G13). Two steps:

1. **`admit`, before the body is read.** No guard folder (local, test and ci only, G4): a typed
   503 `document_scan_unavailable`, before the limiter and before anything is read (the screen
   could never run, so nothing may be stored). Then PR 6's limits (`RouteLimits.check`: a 429
   with `Retry-After`, or a typed 503 `limits_unavailable`). Otherwise a `DocumentScreen` for this
   upload.
2. **`DocumentScreen.scan`, after `service.extract` and before `store.save`** (G13: the measured
   service is never edited). `ExtractionOutcome` holds no page text, so the bytes are read again
   in the app's reading thread (`extraction.service.page_reader`, `READ_WORKERS` threads; the
   route passes it), inside the route's own request limit: `read_for_screen` takes every page's
   text from the extraction's own reader (`extraction.text.read_pages`: the same text the model
   was sent, E12's limits again) and counts its hidden runs (`guards.hidden`). Then, on the event
   loop, **every page's text, hidden or not**, goes through G5's clean and fold, the red-flag
   screen and the injection screen (`guards.screens`, the same path and matcher as `/v1/agent`;
   H1 through `create_app`'s seam), and the hidden-text signal is added. A page holding a script
   of a language its business lists as `not_covered` is flagged `not_covered_language` (D3 (i),
   decided 2026-11-23, shesha): the screens could not read it, so an empty list never claims it
   was screened.

**Flags, never decisions** (G7, until PR 11's forced review). Each hit becomes a `DocumentFlag`
on the response (a code and a page number, never text; every red-flag code is marked `urgent`,
`not_covered_language` is not) and a finding in the `guard` record, whose decision stays `none`:
the extraction is stored and returned as it would be without the guard. `document_flags` is never
stored (no migration).

**Fail closed** (G3, the design point of 2026-11-16 for PR 5 and PR 7). Any exception in the scan
(H1's stub `NotImplementedError`, a hit H1 was not given, pypdf on hostile bytes, a cap) raises
`DocumentScanFailed`, which the route answers with a typed 503 `document_scan_unavailable`,
`{detail, error}` with `X-Trace-Id`: never a stored extraction. While H1 is a stub, every guarded
extraction is that 503. A cap (`OverCap`: the page text's, or `guards.hidden.OPS_MAX`) is decided
by `input_cap`, any other failure by `guard_error`. `CancelledError` always propagates.

**One metadata-only `guard` record per request** (G10), written by `DocumentScreen.finish` from
the route's `finally` (or by `admit` for its own 429 and 503s): the limits, the read
(`document`), clean, red flag and injection with their durations, the findings by code, the
hidden characters `clean` dropped, the decision and the exception's class name. Never page text, a
term or a term id.

**Known gaps:** text an OCR engine would read (none in M7, `extraction.text.NoOcr`) is not
screened; a script no business lists (neither covered nor `not_covered`) is not flagged; and
`guards.hidden`'s own list.
"""

import asyncio
import contextlib
import io
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

from vectapilot_api.extraction.text import read_pages
from vectapilot_api.guards.caps import OverCap, Surface, require_within_cap
from vectapilot_api.guards.config import BusinessGuards, GuardConfig
from vectapilot_api.guards.hidden import hidden_runs
from vectapilot_api.guards.normalize import ScriptRanges, uncovered_languages
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.routes import LimitCheck, RouteLimits
from vectapilot_api.guards.screens import (
    GuardFailed,
    injection_findings,
    prepare,
    red_flag_category,
)
from vectapilot_api.guards.types import (
    UNCHANGED,
    Check,
    DecidedBy,
    Finding,
    GuardAction,
    GuardDecision,
    GuardRecord,
    RouteSlug,
    emit_guard_record,
)

SCAN_UNAVAILABLE: Final = "document_scan_unavailable"
SCAN_UNAVAILABLE_DETAIL: Final = (
    "the document's safety checks could not run; nothing was stored: send it again later"
)


class FlagCode(StrEnum):
    """What a page was flagged for: the `Finding` codes a page can produce, by value."""

    RED_FLAG_EMERGENCY = Finding.RED_FLAG_EMERGENCY.value
    RED_FLAG_PROFESSIONAL_JUDGEMENT = Finding.RED_FLAG_PROFESSIONAL_JUDGEMENT.value
    NOT_COVERED_LANGUAGE = Finding.NOT_COVERED_LANGUAGE.value
    INJECTION_PHRASE = Finding.INJECTION_PHRASE.value
    INJECTION_INVISIBLE = Finding.INJECTION_INVISIBLE.value
    HIDDEN_TEXT = Finding.DOCUMENT_HIDDEN_TEXT.value


URGENT_CODES: Final = frozenset(
    {FlagCode.RED_FLAG_EMERGENCY, FlagCode.RED_FLAG_PROFESSIONAL_JUDGEMENT}
)
"""A red flag in an uploaded document is marked urgent for staff, whichever red-flag category it
is [proposal]: the route has no hand-off, and marking the high category urgent only tightens."""


class DocumentFlag(BaseModel):
    """One flag on one page (G7): a code and a page number, never the page's text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: int = Field(ge=1)
    code: FlagCode
    urgent: bool
    """True exactly for a red-flag code (`URGENT_CODES`)."""

    @model_validator(mode="after")
    def _urgent_is_the_codes(self) -> Self:
        if self.urgent != (self.code in URGENT_CODES):
            raise ValueError("a flag is urgent exactly when its code is a red flag")
        return self

    @classmethod
    def of(cls, page: int, code: FlagCode) -> "DocumentFlag":
        return cls(page=page, code=code, urgent=code in URGENT_CODES)


FLAG_ORDER: Final = MappingProxyType({code: index for index, code in enumerate(FlagCode)})


class DocumentScanFailed(Exception):
    """The document screen could not run (G3): the route's typed 503, never a stored extraction.
    Its message is fixed; it is raised outside `except`, so nothing is chained to it."""

    def __init__(self) -> None:
        super().__init__("the document screen failed")


def scan_unavailable_response() -> JSONResponse:
    """The typed 503 when the screen cannot run before the body is read (no guard folder)."""
    return JSONResponse(
        {"detail": SCAN_UNAVAILABLE_DETAIL, "error": SCAN_UNAVAILABLE}, status_code=503
    )


# ── Reading the document again (in the reading thread) ───────────────────────────────────────


@dataclass(frozen=True)
class ScannedPage:
    """One page as the screens read it: its number, its whole text and its hidden runs."""

    number: int
    text: str = field(repr=False)
    hidden: int


def read_for_screen(data: bytes) -> tuple[ScannedPage, ...]:
    """Every page's text, from the extraction's own reader (the text the model was sent, E12's
    limits again), and its hidden runs (`guards.hidden`). Blocking: the route runs it in the app's
    reading thread. Raises on anything it cannot read (the caller keeps only the type)."""
    document = read_pages(data)
    reader = PdfReader(io.BytesIO(data))
    pages = list(reader.pages)
    if len(pages) != len(document.pages):
        raise ValueError("the document's pages changed between two reads")
    return tuple(
        ScannedPage(text.number, text.text, hidden_runs(page, reader))
        for text, page in zip(document.pages, pages, strict=True)
    )


# ── One upload ────────────────────────────────────────────────────────────────────────────────


class DocumentScreen:
    """One upload's screen and its one `guard` record (module docstring)."""

    def __init__(
        self,
        *,
        business_id: str,
        trace_id: str,
        config: GuardConfig | None,
        matcher: Matcher,
        clock: Callable[[], float],
    ) -> None:
        self._business_id = business_id
        self._trace_id = trace_id
        self._config = config
        self._matcher = matcher
        self._clock = clock
        self._checks: list[Check] = []
        self._durations: dict[Check, float] = {}
        self._findings: Counter[Finding] = Counter()
        self._invisible = 0
        self._decision = UNCHANGED
        self._error_type: str | None = None
        self._finished = False

    def limited(self, checked: LimitCheck) -> None:
        """Fold PR 6's limits check into this request's record."""
        self._ran(Check.LIMITS, checked.elapsed_ms)
        self._findings.update(checked.findings)
        self._decision = checked.decision
        self._error_type = checked.error_type

    def unconfigured(self) -> None:
        self._decision = GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.GUARDS_NOT_CONFIGURED)

    async def scan(self, data: bytes, *, reader: Executor | None) -> tuple[DocumentFlag, ...]:
        """Every page's flags, in page order, then `FlagCode` order; `DocumentScanFailed` when
        the screen cannot run (module docstring). Never raises anything else but a cancel."""
        flags: tuple[DocumentFlag, ...] | None = None
        rule = DecidedBy.GUARD_ERROR
        try:
            flags = await self._scan(data, reader)
        except Exception as failure:  # G3: fail closed; a cancel is not an Exception
            if isinstance(failure, OverCap):
                self._findings[Finding.OVER_CAP] += 1
                rule = DecidedBy.INPUT_CAP
            self._error_type = (
                failure.error_type if isinstance(failure, GuardFailed) else type(failure).__name__
            )
        if flags is None:
            self._decision = GuardDecision(GuardAction.UNAVAILABLE, rule)
            raise DocumentScanFailed  # outside `except`: nothing chained
        return flags

    async def _scan(self, data: bytes, reader: Executor | None) -> tuple[DocumentFlag, ...]:
        if self._config is None:  # `ExtractionGuard.admit` answers first; defence in depth
            raise RuntimeError("the document screen runs only with a guard folder")
        guards = self._config.for_business(self._business_id)
        started = self._clock()
        try:
            loop = asyncio.get_running_loop()
            pages = await loop.run_in_executor(reader, read_for_screen, data)
        finally:
            self._ran(Check.DOCUMENT, self._since(started))
        require_within_cap(Surface.PAGE_TEXT, "".join(page.text for page in pages))
        flags: list[DocumentFlag] = []
        for page in pages:
            flags += self._screen_page(page, guards, self._config.scripts)
        return tuple(sorted(flags, key=lambda flag: (flag.page, FLAG_ORDER[flag.code])))

    def _screen_page(
        self, page: ScannedPage, guards: BusinessGuards, scripts: ScriptRanges
    ) -> list[DocumentFlag]:
        found: Counter[Finding] = Counter()
        started = self._clock()
        prepared = prepare(page.text)
        self._invisible += prepared.invisible.invisible
        self._ran(Check.CLEAN, self._since(started))
        started = self._clock()
        red_flag_category(prepared.folded, guards.red_flags, self._matcher, found)
        self._ran(Check.RED_FLAG, self._since(started))
        started = self._clock()
        if uncovered_languages(prepared.clean, guards.not_covered, scripts):
            found[Finding.NOT_COVERED_LANGUAGE] += 1  # pages: the screens could not read it
        self._ran(Check.NOT_COVERED_LANGUAGE, self._since(started))
        started = self._clock()
        found.update(injection_findings(prepared, guards.injection, self._matcher))
        self._ran(Check.INJECTION, self._since(started))
        if page.hidden:
            found[Finding.DOCUMENT_HIDDEN_TEXT] += 1  # pages, not runs
        self._findings.update(found)
        return [DocumentFlag.of(page.number, FlagCode(code.value)) for code in found]

    def _since(self, started: float) -> float:
        return max(0.0, (self._clock() - started) * 1000)

    def _ran(self, check: Check, elapsed_ms: float) -> None:
        if check not in self._checks:
            self._checks.append(check)
        self._durations[check] = self._durations.get(check, 0.0) + elapsed_ms

    def finish(self) -> None:
        """Write this request's one `guard` record (once; never raises)."""
        if self._finished:
            return
        self._finished = True
        with contextlib.suppress(Exception):  # the record never replaces the response
            emit_guard_record(
                GuardRecord(
                    business_id=self._business_id,
                    trace_id=self._trace_id,
                    route=RouteSlug.EXTRACTION,
                    rules_version=None if self._config is None else self._config.rules_version,
                    decision=self._decision,
                    checks=tuple(self._checks),
                    findings=dict(self._findings),
                    invisible=self._invisible,
                    error_type=self._error_type,
                    durations_ms=self._durations,
                )
            )


class ExtractionGuard:
    """`POST /v1/extractions`' guard (module docstring). One per app, built in `create_app`."""

    def __init__(
        self,
        *,
        limits: RouteLimits,
        config: GuardConfig | None,
        matcher: Matcher,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._limits = limits
        self._config = config
        self._matcher = matcher
        self._clock = clock

    async def admit(self, business_id: str, *, trace_id: str) -> JSONResponse | DocumentScreen:
        """The 429 or a typed 503 (its record written), or this upload's screen, whose record the
        route writes with `finish`. Never raises but a cancel."""
        screen = DocumentScreen(
            business_id=business_id,
            trace_id=trace_id,
            config=self._config,
            matcher=self._matcher,
            clock=self._clock,
        )
        if self._config is None:
            screen.unconfigured()
            screen.finish()
            return scan_unavailable_response()
        checked = await self._limits.check(business_id)
        screen.limited(checked)
        if checked.response is not None:
            screen.finish()
            return checked.response
        return screen
