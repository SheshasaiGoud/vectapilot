"""The `POST /v1/answer` and `GET /v1/kb/search` guards (M7 PR 8; ADR 0018; G1-G3, G6-G11, G13).

Both routes call `AnswerGuard` only when `guard_answer = "on"`: `create_app` builds it, with a
separate `AnswerService` over the chunk-screening search and its own `BudgetedGateway`, and hands
it to the routes as an object (G13: no middleware, no screening dependency). While `guard_answer`
is off (the default for M3's measurement window, G11) neither route reads it, M3's
`app.state.answer_service` serves, and both routes answer byte for byte as on the base commit
(tests/answer/test_answer_goldens.py).

**`POST /v1/answer`** (`AnswerGuard.answer`), in G1's order, every step scoped by the business:

1. **No guard folder** (local, test and ci only, G4): a `guards_not_configured` hand-off.
2. **Clean and fold** the question (G5); hidden characters are counted on the raw text first.
3. **The red-flag screen**, pure and in-process: no Redis, no budget, no model. A hit returns
   `AnswerSafetyHandoff` (reason `safety`, M4's route policy's priority for the most urgent hit,
   M4's intent and the business's guidance) before anything else runs (G11).
4. **An uncovered language** (D3 (i)): a health business's question holding a character of a
   `not_covered` language's script is a high `not_covered_language` hand-off with the guidance.
   For any other business it is a finding, and the question never counts as cleared (G3).
5. **Limits** (G9): the business's `answer` window (the body names no customer). A full window is
   a 429 with `Retry-After` (its body carries the guidance for a health business); Redis failing
   a `guard_error` hand-off. **No budget pre-check** [proposal, PR 8]: G9's pre-check models loop
   turns; an answer makes at most one `large` call, whose prompt is known only after retrieval,
   so that call's own reservation (`BudgetedGateway`) is the control, and its refusal a `budget`
   hand-off.
6. **Injection signals** in the question (G7) are counted, never acted on [proposal, PR 8]: the
   answer path has no write tool and no read-only variant to choose; its prompt boundaries, the
   citation check and H3 stay the line.
7. **Masking** of the clean question (H2's `mask_pii`), re-validated as an `AnswerRequest`
   (G13: `Question`'s rules run again), so the embedder and the model see only it (G6).
8. **The service**: the separate `AnswerService`, inside one budget tally and one screen tally. Its
   search drops a chunk with an injection signal before the prompt and hands every kept chunk on
   as its clean text (`guards.search`, G7). A seam's failure is `guard_error`; a budget refusal a
   `budget` hand-off. The service's own errors (M3's typed table: embedding, re-index, database,
   model) pass to the route unchanged, which maps them as before [proposal, PR 8]: they are not
   a guard's decision. Its `rag_answer` record counts `hits` and `evidence` after the screen,
   while a hybrid record's `retrieval` block (filled by the search before it) counts every chunk
   retrieved: the two differ by the chunks dropped (ananya's PR 8 review; `answer/service.py`
   and `retrieval/` are M3's, so the record stays as it is). A dropped chunk is marked by the
   `guard` record's `drop` / `injection:chunk` when the response passed; the record does not
   count dropped chunks.
9. **Output checks** (H3, G8) on an `answered` response: its answer text against the cited chunks'
   clean texts (digit grounding; PII exempt when a cited chunk of the same business holds it, or
   the message, or the guidance). The text is first checked against `Surface.REPLY`'s cap, as
   H3's own rule 1 reads an answer (G3: over it fails closed). A finding replaces the whole
   response with a `guard_output` hand-off (high for a health business, normal otherwise, as on
   `/v1/agent`). Abstentions have no text and pass unchanged.

**`GET /v1/kb/search`** (`AnswerGuard.search`) returns no model text, so it has no hand-off and its
guards run before the service: clean and fold, the red-flag screen and the uncovered-language
check (**recorded only**, the response unchanged, G11), the business's `search` window (a 429, or
the typed 503 `limits_unavailable`), the injection signals (counted), then masking: the search
embeds the clean, masked question [proposal, PR 8: G6's "before every third party"]. Its hits are
approved documents, not a prompt, so they are not screened [proposal, PR 8]. Without a guard
folder, or when a guard raises, it is a typed 503 (G3).

**Fail closed** (G3). Any exception in a guard (a pending stub's `NotImplementedError`, a hit H1
was not given, an H3 result that is not its codes, a bug) becomes a `guard_error` hand-off on
`/v1/answer` and a typed 503 on search, never a 500 and never the question passed on unchecked.
Before the red-flag screen has cleared the question it is urgent for a health business and high
for any other; after, high and normal. The guidance is shown for a health business or whenever
the hand-off is urgent. `CancelledError` always propagates.

**One metadata-only `guard` record per guarded request** (G10), as on `/v1/agent`: never the
question, a term or a term id.
"""

import contextlib
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from vectapilot_api.answer.service import AnswerService
from vectapilot_api.answer.types import AnswerRequest, AnswerResponse, AnswerRun
from vectapilot_api.gateway.types import TraceId
from vectapilot_api.guards.agent import red_flag_priority
from vectapilot_api.guards.budget import BudgetTally, track_budget
from vectapilot_api.guards.caps import Surface, within_cap
from vectapilot_api.guards.config import GUIDANCE_LINES_MAX, BusinessGuards, GuardConfig
from vectapilot_api.guards.limits import (
    GuardRefused,
    Limiter,
    RefusalKind,
    limited_response,
    refusal_decision,
)
from vectapilot_api.guards.locales import locales
from vectapilot_api.guards.normalize import uncovered_languages
from vectapilot_api.guards.output import (
    Matcher,
    OutputCheck,
    OutputContext,
    OutputKind,
    check_reply,
)
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.screens import ANSWER_BOUNDARY_LINES as ANSWER_BOUNDARY_LINES
from vectapilot_api.guards.screens import (
    RED_FLAG_DECIDED_BY,
    GuardFailed,
    Masker,
    Prepared,
    ScreenTally,
    injection_findings,
    output_withheld,
    prepare,
    red_flag_category,
    track_screens,
)
from vectapilot_api.guards.types import (
    UNCHANGED,
    Check,
    Clean,
    DecidedBy,
    Finding,
    GuardAction,
    GuardDecision,
    GuardRecord,
    RouteSlug,
    emit_guard_record,
    red_flag_intent,
)
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType, Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.retrieval.search import Question

ERROR_TYPE_OVER_CAP: Final = "OverCap"
GUARD_UNAVAILABLE: Final = "guard_unavailable"
GUARD_UNAVAILABLE_DETAIL: Final = (
    "the request could not be checked right now; nothing was done: try again shortly"
)
GUARDS_NOT_CONFIGURED: Final = "guards_not_configured"
GUARDS_NOT_CONFIGURED_DETAIL: Final = (
    "the request cannot be checked: this server has no guard configuration"
)
LIMITS_UNAVAILABLE: Final = "limits_unavailable"
LIMITS_UNAVAILABLE_DETAIL: Final = (
    "the request's limits cannot be checked right now; nothing was done: try again shortly"
)
"""As `guards.routes`' 503 (the same code and text): Redis failing on search (G3)."""

_QUESTION: Final[TypeAdapter[str]] = TypeAdapter(Question)


# ── The response shape (G11) ─────────────────────────────────────────────────────────────────

AnswerHandoffReason = Literal[
    "safety",
    "not_covered_language",
    "budget",
    "guard_error",
    "guard_output",
    "guards_not_configured",
]
"""Why a guard handed a question off; each is a `/v1/agent` `HandoffReason` value too (a test
checks), so staff read one vocabulary. A red flag is `safety`."""


class AnswerSafetyHandoff(BaseModel):
    """`POST /v1/answer`'s hand-off when a guard decided it (G11; `guard_answer = "on"` only).

    `AnswerResponse`'s own `handoff` outcome means weak evidence and carries no priority or
    guidance, so a guard's hand-off is this separate shape in the route's response union;
    `AnswerResponse` itself is unchanged. A client tells them apart by `kind`, which
    `AnswerResponse` never has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["handoff"] = "handoff"
    reason: AnswerHandoffReason
    priority: Priority
    intent: Intent | None = None
    """M4's intent of a red-flag hit (`emergency`, `professional_judgement`); None otherwise."""
    guidance: tuple[str, ...] = Field(default=(), max_length=GUIDANCE_LINES_MAX)
    """The business's emergency guidance lines, for a health business or an urgent hand-off."""
    trace_id: TraceId
    """As every answer's: the `guard` and `rag_answer` records of this request carry it."""


@dataclass(frozen=True)
class GuardedAnswer:
    """What the answer route sends: `response`, or the service's own `failure` (M3's typed table,
    or a bug) for the route to map exactly as before."""

    response: AnswerResponse | AnswerSafetyHandoff | JSONResponse | None = None
    failure: Exception | None = None


# ── One request ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Business:
    guards: BusinessGuards | None
    health: bool
    guidance: tuple[str, ...]
    types: frozenset[BusinessType]


@dataclass
class _Request:
    """One request's record, filled in as G1's steps run."""

    business: _Business
    checks: list[Check] = field(default_factory=list)
    findings: Counter[Finding] = field(default_factory=Counter)
    durations: dict[Check, float] = field(default_factory=dict)
    invisible: int = 0
    cleared: bool = False
    """The red-flag screen ran and found nothing: G3's priorities drop one step."""
    first: DecidedBy | None = None
    error_type: str | None = None
    budget: BudgetTally | None = None

    def ran(self, check: Check, started: float, now: float) -> None:
        self.note(check)
        self.durations[check] = self.durations.get(check, 0.0) + max(0.0, (now - started) * 1000)

    def note(self, check: Check) -> None:
        if check not in self.checks:
            self.checks.append(check)

    def decide(self, rule: DecidedBy) -> None:
        if self.first is None:
            self.first = rule


@dataclass
class _Outcome:
    answered: GuardedAnswer
    decision: GuardDecision = UNCHANGED


class AnswerGuard:
    """G1's pipeline for `/v1/answer` and `/v1/kb/search` (module docstring). One per app."""

    def __init__(
        self,
        *,
        config: GuardConfig | None,
        limiter: Limiter,
        matcher: Matcher,
        types: Mapping[str, BusinessType],
        guidance: Mapping[str, tuple[str, ...]],
        check: OutputCheck = check_reply,
        mask: Masker = mask_pii,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """`types` is the type a guarded route serves each business as (`tools.json`, the
        extraction file) and `guidance` its `tools.json` lines, as `guards.routes.RouteLimits`
        reads them: a business is health by either source."""
        self._config = config
        self._limiter = limiter
        self._matcher = matcher
        self._types = types
        self._guidance = guidance
        self._check = check
        self._mask = mask
        self._clock = clock

    def _business(self, business_id: str) -> _Business:
        guards = None if self._config is None else self._config.for_business(business_id)
        served = self._types.get(business_id)
        health = bool(guards is not None and guards.health) or served in HEALTH_TYPES
        guidance = () if guards is None else guards.guidance
        types: set[BusinessType] = set() if served is None else {served}
        if guards is not None and guards.business_type is not None:
            types.add(guards.business_type)
        lines = guidance or self._guidance.get(business_id, ())
        return _Business(guards, health, lines, frozenset(types))

    # ── POST /v1/answer ──────────────────────────────────────────────────────────────────────

    async def answer(
        self,
        business_id: str,
        request: AnswerRequest,
        *,
        trace_id: str,
        service: AnswerService | None,
    ) -> GuardedAnswer:
        """The answer route's response, or the service's own failure (module docstring). Never
        raises but a cancel."""
        state = _Request(self._business(business_id))
        outcome: _Outcome | None = None
        try:
            with track_budget() as budget, track_screens() as screens:
                state.budget = budget
                try:
                    outcome = await self._answer(
                        business_id, request, trace_id, service, state, screens
                    )
                except Exception as failure:  # G3: fail closed; a cancel is not an Exception
                    state.error_type = (
                        failure.error_type
                        if isinstance(failure, GuardFailed)
                        else type(failure).__name__
                    )
                    outcome = self._failed(trace_id, state)
            return outcome.answered
        finally:
            if outcome is not None:  # a cancelled request has no response to record
                with contextlib.suppress(Exception):  # never replaces the response
                    self._emit(business_id, trace_id, RouteSlug.ANSWER, state, outcome.decision)

    async def _answer(
        self,
        business_id: str,
        request: AnswerRequest,
        trace_id: str,
        service: AnswerService | None,
        state: _Request,
        screens: ScreenTally,
    ) -> _Outcome:
        business, config = state.business, self._config
        guards = business.guards
        if config is None or guards is None:
            priority = Priority.URGENT if business.health else Priority.HIGH
            return self._handoff(
                trace_id,
                "guards_not_configured",
                priority,
                DecidedBy.GUARDS_NOT_CONFIGURED,
                state,
            )
        prepared = self._prepare(request.question, state)
        if prepared is None:
            return self._failed(trace_id, state, DecidedBy.INPUT_CAP)

        # 3. The red-flag screen: no Redis, no budget, no model (G1).
        started = self._clock()
        category = red_flag_category(
            prepared.folded, guards.red_flags, self._matcher, state.findings
        )
        state.ran(Check.RED_FLAG, started, self._clock())
        if category is not None:
            return self._handoff(
                trace_id,
                "safety",
                red_flag_priority(category, business.types),
                RED_FLAG_DECIDED_BY[category],
                state,
                intent=red_flag_intent(category),
                show=True,
            )

        # 4. An uncovered language (D3 (i)).
        uncovered = self._uncovered(prepared, guards, config, state)
        if uncovered and business.health:
            return self._handoff(
                trace_id,
                "not_covered_language",
                Priority.HIGH,
                DecidedBy.NOT_COVERED_LANGUAGE,
                state,
                show=True,
            )
        state.cleared = not uncovered

        # 5. Limits (G9).
        refused = await self._admit(business_id, RouteSlug.ANSWER, state)
        if refused is not None:
            return self._refused(refused, trace_id, state)

        # 6. Injection signals: counted (module docstring).
        self._injection(prepared, guards, state)

        # 7. Masking, then `Question`'s rules again (G6, G13).
        started = self._clock()
        masked = AnswerRequest.model_validate(
            {"question": self._mask(prepared.clean, locales()).text}
        )
        state.ran(Check.MASK, started, self._clock())

        # 8. The service.
        screens.business, screens.message, screens.health = guards, prepared.clean, business.health
        if service is None:  # never M3's unguarded service instead: fail closed (G3)
            raise RuntimeError("the guarded answer service is missing")
        try:
            run = await self._service(service, business_id, masked.question, trace_id)
        finally:
            state.findings.update(screens.findings)
            for check in screens.checks:  # run inside the search's seam: no duration of its own
                state.note(check)
        if screens.failed is not None:  # the chunk screen raised, whatever the service made of it
            raise GuardFailed(screens.failed)
        if isinstance(run, GuardFailed):  # a seam that did not record it: still G3
            raise GuardFailed(run.error_type)
        if isinstance(run, GuardRefused):
            return self._refused(run, trace_id, state)
        if isinstance(run, Exception):  # the service's own failure: the route maps it
            return _Outcome(GuardedAnswer(failure=run))

        # 9. Output checks on an answered response (H3, G8).
        response = run.response
        if response.outcome == "answered" and response.answer is not None:
            started = self._clock()
            if not within_cap(Surface.REPLY, response.answer):
                state.findings[Finding.OVER_CAP] += 1
                state.error_type = ERROR_TYPE_OVER_CAP
                return self._failed(trace_id, state, DecidedBy.INPUT_CAP)
            context = self._context(run, guards, prepared.clean, business.health, screens)
            withheld = output_withheld(self._check(response.answer, context))
            state.ran(Check.OUTPUT, started, self._clock())
            if withheld is not None:
                state.findings.update(withheld.findings)
                state.decide(withheld.decided_by)
                priority = Priority.HIGH if business.health else Priority.NORMAL
                return self._handoff(trace_id, "guard_output", priority, withheld.decided_by, state)
        if screens.dropped_chunks:  # the response passed; a dropped chunk tightened it (G7)
            state.decide(DecidedBy.INJECTION_CHUNK)
            dropped = GuardDecision(GuardAction.DROP, DecidedBy.INJECTION_CHUNK)
            return _Outcome(GuardedAnswer(response), dropped)
        return _Outcome(GuardedAnswer(response))

    @staticmethod
    async def _service(
        service: AnswerService, business_id: str, question: str, trace_id: str
    ) -> AnswerRun | Exception:
        """The service's run, or what it raised (a cancel propagates): `_answer` tells a guard's
        exception (`GuardFailed`, `GuardRefused`) from the service's own."""
        try:
            return await service.answer(business_id, question, trace_id=trace_id)
        except Exception as error:
            return error

    def _context(
        self,
        run: AnswerRun,
        guards: BusinessGuards,
        message: Clean,
        health: bool,
        screens: ScreenTally,
    ) -> OutputContext:
        """H3's context for an answer (G8). Every cited chunk passed the screening search in this
        request; a citation of any other chunk is a KeyError, which fails closed (G3)."""
        cited = tuple(screens.kept_chunks[source.chunk_id] for source in run.response.sources)
        return OutputContext(
            business_type=guards.business_type,
            health=health,
            message=message,
            kind=OutputKind.ANSWER,
            cited=cited,
            guidance=guards.guidance,
            terms=guards.output,
            matcher=self._matcher,
            boundary_lines=ANSWER_BOUNDARY_LINES,
            locales=locales(),
        )

    # ── GET /v1/kb/search ────────────────────────────────────────────────────────────────────

    async def search(self, business_id: str, question: str, *, trace_id: str) -> str | JSONResponse:
        """The clean, masked question the search embeds, or a 429 or typed 503 (module
        docstring); the `guard` record is written before the service runs. Never raises but a
        cancel."""
        state = _Request(self._business(business_id))
        result: str | JSONResponse | None = None
        decision = UNCHANGED
        try:
            try:
                result, decision = await self._search(business_id, question, state)
            except Exception as failure:  # G3: fail closed; a cancel is not an Exception
                state.error_type = type(failure).__name__
                decision = GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.GUARD_ERROR)
                result = unavailable(GUARD_UNAVAILABLE, GUARD_UNAVAILABLE_DETAIL)
            return result
        finally:
            if result is not None:
                with contextlib.suppress(Exception):  # never replaces the response
                    self._emit(business_id, trace_id, RouteSlug.SEARCH, state, decision)

    async def _search(
        self, business_id: str, question: str, state: _Request
    ) -> tuple[str | JSONResponse, GuardDecision]:
        guards, config = state.business.guards, self._config
        if config is None or guards is None:
            decision = GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.GUARDS_NOT_CONFIGURED)
            return unavailable(GUARDS_NOT_CONFIGURED, GUARDS_NOT_CONFIGURED_DETAIL), decision
        prepared = self._prepare(question, state)
        if prepared is None:
            decision = GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.INPUT_CAP)
            return unavailable(GUARD_UNAVAILABLE, GUARD_UNAVAILABLE_DETAIL), decision
        # The red-flag screen and the language check: recorded only (G11).
        started = self._clock()
        red_flag_category(prepared.folded, guards.red_flags, self._matcher, state.findings)
        state.ran(Check.RED_FLAG, started, self._clock())
        self._uncovered(prepared, guards, config, state)
        refused = await self._admit(business_id, RouteSlug.SEARCH, state)
        if refused is not None:
            return self._search_refused(refused, state)
        self._injection(prepared, guards, state)
        started = self._clock()
        masked = _QUESTION.validate_python(self._mask(prepared.clean, locales()).text)
        state.ran(Check.MASK, started, self._clock())
        return masked, UNCHANGED

    def _search_refused(
        self, refused: GuardRefused, state: _Request
    ) -> tuple[JSONResponse, GuardDecision]:
        """A full window (a 429, with the guidance for a health business) or Redis failing (the
        typed 503, no guidance), as `guards.routes.RouteLimits` answers them."""
        decision = refusal_decision(refused, health=state.business.health, handoff=False)
        if refused.kind is RefusalKind.LIMIT:
            shown = state.business.guidance if decision.guidance else ()
            if decision.guidance and not shown:
                state.findings[Finding.NO_GUIDANCE] += 1
            return limited_response(refused, shown), decision
        state.error_type = refused.error_type
        return unavailable(LIMITS_UNAVAILABLE, LIMITS_UNAVAILABLE_DETAIL), decision

    # ── Shared steps ─────────────────────────────────────────────────────────────────────────

    def _prepare(self, raw: str, state: _Request) -> Prepared | None:
        """Clean and fold (G5), or None over the message's cap (validation bounds it first, G1:
        defence only)."""
        started = self._clock()
        if not within_cap(Surface.MESSAGE, raw):
            state.findings[Finding.OVER_CAP] += 1
            state.error_type = ERROR_TYPE_OVER_CAP
            return None
        prepared = prepare(raw)
        state.invisible = prepared.invisible.invisible
        state.ran(Check.CLEAN, started, self._clock())
        return prepared

    def _uncovered(
        self, prepared: Prepared, guards: BusinessGuards, config: GuardConfig, state: _Request
    ) -> bool:
        started = self._clock()
        found = uncovered_languages(prepared.clean, guards.not_covered, config.scripts)
        state.ran(Check.NOT_COVERED_LANGUAGE, started, self._clock())
        if found:
            state.findings[Finding.NOT_COVERED_LANGUAGE] += 1
        return bool(found)

    async def _admit(
        self, business_id: str, route: RouteSlug, state: _Request
    ) -> GuardRefused | None:
        started = self._clock()
        try:
            await self._limiter.admit(business_id, route)
        except GuardRefused as refused:
            return refused
        finally:
            state.ran(Check.LIMITS, started, self._clock())
        return None

    def _injection(self, prepared: Prepared, guards: BusinessGuards, state: _Request) -> None:
        started = self._clock()
        found = injection_findings(prepared, guards.injection, self._matcher)
        state.ran(Check.INJECTION, started, self._clock())
        state.findings.update(found)

    # ── Responses ────────────────────────────────────────────────────────────────────────────

    def _handoff(
        self,
        trace_id: str,
        reason: AnswerHandoffReason,
        priority: Priority,
        decided_by: DecidedBy,
        state: _Request,
        *,
        intent: Intent | None = None,
        show: bool | None = None,
    ) -> _Outcome:
        """A hand-off decided by `decided_by`. The guidance is shown for a health business or an
        urgent hand-off (G3), or whenever `show` says so (G1: a red flag always carries it);
        `no_guidance` is counted when there is none to show."""
        business = state.business
        wanted = (business.health or priority is Priority.URGENT) if show is None else show
        if wanted and not business.guidance:
            state.findings[Finding.NO_GUIDANCE] += 1
        shown = business.guidance if wanted else ()
        state.decide(decided_by)
        response = AnswerSafetyHandoff(
            reason=reason, priority=priority, intent=intent, guidance=shown, trace_id=trace_id
        )
        decision = GuardDecision(
            GuardAction.HANDOFF, decided_by, priority=priority, guidance=bool(shown)
        )
        return _Outcome(GuardedAnswer(response), decision)

    def _failed(
        self, trace_id: str, state: _Request, decided_by: DecidedBy = DecidedBy.GUARD_ERROR
    ) -> _Outcome:
        """G3's `guard_error` hand-off, its priority by whether the red-flag screen cleared. A
        failure always decides as itself, never as an earlier rule (PR 2's scoring rules)."""
        health = state.business.health
        if state.cleared:
            priority = Priority.HIGH if health else Priority.NORMAL
        else:
            priority = Priority.URGENT if health else Priority.HIGH
        return self._handoff(trace_id, "guard_error", priority, decided_by, state)

    def _refused(self, refusal: GuardRefused, trace_id: str, state: _Request) -> _Outcome:
        """A limit, the budget or Redis failing (G9, PR 5's `refusal_decision`). A Redis failure
        for a question the red-flag screen never cleared (an uncovered script) keeps G3's
        before-the-screen priority."""
        business = state.business
        decision = refusal_decision(refusal, health=business.health)
        state.decide(decision.decided_by)
        if decision.action is GuardAction.LIMIT:
            shown = business.guidance if decision.guidance else ()
            if decision.guidance and not shown:
                state.findings[Finding.NO_GUIDANCE] += 1
            return _Outcome(GuardedAnswer(limited_response(refusal, shown)), decision)
        if refusal.kind is RefusalKind.UNAVAILABLE:
            state.error_type = refusal.error_type
            if not state.cleared:
                return self._failed(trace_id, state, decision.decided_by)
        reason: AnswerHandoffReason = (
            "budget" if refusal.kind is RefusalKind.BUDGET else "guard_error"
        )
        priority = decision.priority or Priority.HIGH
        return self._handoff(trace_id, reason, priority, decision.decided_by, state)

    # ── The record ───────────────────────────────────────────────────────────────────────────

    def _emit(
        self,
        business_id: str,
        trace_id: str,
        route: RouteSlug,
        state: _Request,
        decision: GuardDecision,
    ) -> None:
        budget = state.budget
        spent = budget is not None and bool(
            budget.reserved_micro_usd or budget.settled_micro_usd or budget.refused
        )
        emit_guard_record(
            GuardRecord(
                business_id=business_id,
                trace_id=trace_id,
                route=route,
                rules_version=None if self._config is None else self._config.rules_version,
                decision=decision,
                checks=tuple(state.checks),
                findings=dict(state.findings),
                invisible=state.invisible,
                error_type=state.error_type,
                reserved_micro_usd=budget.reserved_micro_usd if spent and budget else None,
                settled_micro_usd=budget.settled_micro_usd if spent and budget else None,
                budget_refused=0 if budget is None else budget.refused,
                durations_ms=state.durations,
            )
        )


def unavailable(code: str, detail: str) -> JSONResponse:
    """A typed 503 on search (G3), `{detail, error}` with fixed text, as `kb.errors` writes one."""
    return JSONResponse({"detail": detail, "error": code}, status_code=503)
