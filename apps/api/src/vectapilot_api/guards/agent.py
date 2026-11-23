"""The `/v1/agent` route's guards (M7 PR 6; ADR 0018; G1-G3, G7-G10, G13, G14).

`AgentGuard.respond` is what the route calls when `guard_routes = "on"` (built in `create_app`,
handed to the route as an object: no middleware, no screening dependency, G13). In G1's order,
every step scoped by the business:

1. **No guard folder** (local, test and ci only, G4): a `guards_not_configured` hand-off.
2. **Clean and fold** the message (G5); hidden characters are counted on the raw text first.
3. **The red-flag screen**, pure and in-process: no Redis, no budget, no model. A hit ends the
   request with a `safety` hand-off at M4's route policy's priority (`emergency` urgent,
   `professional_judgement` high; the most urgent hit wins) and the business's guidance, before
   the service: before W11, the router and the loop. So W11's normal `safety_floor_off` becomes
   urgent for a red flag, and a business of any type gets this deterministic step. **Known gap**
   [kenji's PR 6 review, option (a), proposal]: under `agent_router = on` a hand-off made here at
   high (a `professional_judgement`-only hit, or step 4's uncovered script) caps the priority
   even where M4's router would have said urgent (a paraphrased emergency beside a dose question).
   Nothing changes while the router is off everywhere; a test pins it, and the PR that turns the
   router on decides whether such messages run on to the router instead.
4. **An uncovered language** (D3 (i)): a health business's message holding a character of a
   `not_covered` language's script is a high `not_covered_language` hand-off with the guidance.
   For any other business it is an injection signal instead [proposal]: the injection screen
   cannot read that script either, so the turn runs read-only (step 6), and the message never
   counts as cleared (G3: a later guard failure is high, not normal).
5. **Limits, then the budget pre-check** (G9): `Limiter.admit` for the business and the customer,
   then `require_budget` with `turn_precheck_micro_usd` (decided 2026-11-21, shesha; G9
   amended). A full window is a 429 with `Retry-After` (its body carries the guidance for a
   health business); an exhausted budget a `budget` hand-off; Redis failing a `guard_error`
   hand-off (`limits.refusal_decision`).
6. **Injection signals** in the message (G7): a phrase or the hidden-character rule selects the
   read-only service (every constructor argument the same but its profiles, G13).
7. **Masking** of the clean message (H2's `mask_pii`), re-validated as an `AgentRequest`, so
   `Question`'s rules run again (G13: never `model_copy`).
8. **The service**, inside one budget tally (`budget.track_budget`) and one screen tally
   (`screens.track_screens`). Its seams (the chunk-screening search, the tool-result executor,
   the ticket store) raise what the route decides: `GuardRefused` (a call the day's budget
   refused), `GuardWithheld` (a ticket's text, G7: `guard_output`, the proposal stored already
   expired and named in the record) and `GuardFailed` (a guard inside a seam raised:
   `guard_error`), each through `AgentService.propagate`. A seam's failure is also read from the
   tally after the service, whatever the service returned: the loop turns an exception from its
   tools into its own `tool_failure` hand-off (M5 H1 rule 7).
9. **Output checks** (H3, G8) on a `reply`'s text and an `answer` kind's answer text (its cited
   chunks' clean texts as grounding). A finding replaces the whole response with a `guard_output`
   hand-off [proposal: high for a health business, normal for any other, as G3's after-the-screen
   rule]. Hand-offs and `confirmation_required` pass byte for byte (G2; a ticket's text was
   screened before its response was built, `guards.store`).

**Fail closed** (G3). Any exception in a guard (a pending stub's `NotImplementedError`, a hit H1
was not given, Redis, a bug) becomes a `guard_error` hand-off, never a 500 and never the message
passed on unchecked. Before the red-flag screen has cleared the message it is urgent for a health
business and high for any other; after, high for a health business and normal for any other. The
guidance is shown for a health business or whenever the hand-off is urgent. `CancelledError`
always propagates.

**One metadata-only `guard` record per request** (G10): the checks that ran, `decided_by` (the
first rule in G1's order that changed the response; every other rule a request trips is a
finding), finding counts, the exception's class name, the budget tally, a withheld proposal's
action id and durations. Never the message, a term or a term id. The `agent_turn` record stays
the loop's outcome; both share the trace id.

**Which rule decided** (PR 6's proposal, standing as built, decided 2026-11-21, shesha; the
scoring rules' reading for c-007 and a-005): a rule before the service decides first (the red
flag, the language, limits and budget, the message's injection signal, which chose the
read-only service). After the service, a **whole-response
replacement** (the ticket text's screen or H3, `guard_output`) decides over a tightening inside
the service (a dropped chunk, a withheld tool-result field), which then is a finding: that
replacement, not the tightening, decided what the customer got. Only a response that passed
through is decided by such a tightening, a dropped chunk before a withheld field (G1's order).
"""

import contextlib
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final
from uuid import UUID

from fastapi.responses import JSONResponse

from vectapilot_api.agent.profiles import AgentProfile
from vectapilot_api.agent.prompt import build_agent_prompt, build_step_request
from vectapilot_api.agent.responses import (
    AgentRequest,
    AgentResponse,
    AnswerKindResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.service import AgentRun, AgentService
from vectapilot_api.agent.types import Transcript
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.budget import BudgetedGateway, BudgetTally, track_budget
from vectapilot_api.guards.caps import Surface, within_cap
from vectapilot_api.guards.config import BusinessGuards, GuardConfig
from vectapilot_api.guards.limits import (
    GuardRefused,
    Limiter,
    RefusalKind,
    limited_response,
    refusal_decision,
    require_budget,
)
from vectapilot_api.guards.locales import locales
from vectapilot_api.guards.normalize import uncovered_languages
from vectapilot_api.guards.output import Matcher, OutputCheck, OutputKind, check_reply
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.screens import (
    RED_FLAG_DECIDED_BY,
    GuardFailed,
    GuardWithheld,
    Masker,
    ScreenTally,
    injection_findings,
    output_context,
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
    TermCategory,
    emit_guard_record,
    red_flag_intent,
)
from vectapilot_api.intents.policy import TEMPLATES, BusinessType, Priority, Route, policy_for
from vectapilot_api.tools.config import READ_TOOLS, ToolsConfig
from vectapilot_api.tools.files import BusinessTools

GuardedResponse = AgentResponse | JSONResponse
"""What the route returns: one of `/v1/agent`'s four kinds, or a limit's 429."""

ERROR_TYPE_OVER_CAP: Final = "OverCap"


# ── The read-only service's profiles (G13) ──────────────────────────────────────────────────


def read_only_tools(
    tools: Mapping[BusinessId, BusinessTools],
) -> Mapping[BusinessId, BusinessTools]:
    """Each business's tool files with `enabled_tools` limited to `READ_TOOLS`, re-validated as a
    `ToolsConfig` (never `dataclasses.replace`), for `build_profiles` with the same checked types:
    the read-only service's step schema then holds no write tool, and a health business keeps its
    type, so it still gets W11's `safety_floor_off`."""
    limited: dict[BusinessId, BusinessTools] = {}
    for business_id, files in tools.items():
        data = files.config.model_dump(mode="json")
        data["enabled_tools"] = [name for name in data["enabled_tools"] if name in READ_TOOLS]
        limited[business_id] = BusinessTools(
            config=ToolsConfig.model_validate(data), seed=files.seed
        )
    return MappingProxyType(limited)


# ── The budget pre-check (G9) ────────────────────────────────────────────────────────────────


def turn_precheck_micro_usd(
    gateway: BudgetedGateway,
    profile: AgentProfile,
    message: str,
    *,
    trace_id: str,
    max_steps: int,
) -> int:
    """The day's remainder a turn needs before it starts (decided 2026-11-21, shesha; G9 amended
    to match): `max_steps` times the worst case of the turn's first step, computed exactly as
    `BudgetedGateway` will reserve it (the step's own request for this message, the business's
    step schema, the gateway's retry and repair limits). Every later step's prompt holds this one
    plus its observations, so each later reservation is at least as large: this is a **lower
    bound** of a loop turn's worst case (leo's PR 6 review; G9 now says so), so it never refuses
    a turn that could have finished. A remainder below it cannot finish a loop turn that uses its
    steps; above it, each call's own reservation stays the control (a step refused mid-turn is a
    `budget` hand-off, and the loop never writes). It models the loop's step gateway only: under
    `agent_router = on` a RAG turn spends a `small` fallback and a `large` answer instead
    (kenji's PR 6 review), and a refusal in the router's fallback fails closed through M4
    (`guards.budget.RefusalAsRouterFailure`)."""
    prompt = build_agent_prompt(
        Transcript(message),
        today=profile.config.sandbox_date,
        timezone=profile.config.timezone,
    )
    request = build_step_request(prompt, business_id=profile.business_id, trace_id=trace_id)
    return max_steps * gateway.reservation_micro_usd(request, profile.step_schema)


# ── One request ──────────────────────────────────────────────────────────────────────────────


def red_flag_priority(category: TermCategory, types: frozenset[BusinessType]) -> Priority:
    """M4's route policy's priority for a red-flag category (G1), read, never edited: the most
    urgent over the business's types, or over every template when its type is unknown (G4: fail
    towards safety). A red flag always hands off, so a policy without a hand-off for it is high."""
    intent = red_flag_intent(category)
    policies = [policy_for(t.value) for t in sorted(types)] if types else list(TEMPLATES)
    routes: list[Route] = [policy.route(intent) for policy in policies]
    best = max(routes, key=lambda route: route.urgency)
    return best.priority or Priority.HIGH


@dataclass(frozen=True)
class _Business:
    """What the route knows about one business."""

    guards: BusinessGuards | None
    profile: AgentProfile | None
    health: bool
    guidance: tuple[str, ...]
    types: frozenset[BusinessType]


@dataclass
class _Turn:
    """One request's record, filled in as G1's steps run."""

    business: _Business
    checks: list[Check] = field(default_factory=list)
    findings: Counter[Finding] = field(default_factory=Counter)
    durations: dict[Check, float] = field(default_factory=dict)
    invisible: int = 0
    cleared: bool = False
    """The red-flag screen ran and found nothing: G3's priorities drop one step."""
    first: DecidedBy | None = None
    """The first rule in G1's order that changed the service or its response."""
    error_type: str | None = None
    budget: BudgetTally | None = None
    withheld_action_id: UUID | None = None
    """The pending action a withheld ticket was stored as, already expired (G7)."""

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
    response: GuardedResponse
    decision: GuardDecision = UNCHANGED


class AgentGuard:
    """G1's pipeline for `/v1/agent` (module docstring). One per app, built in `create_app`."""

    def __init__(
        self,
        *,
        config: GuardConfig | None,
        limiter: Limiter,
        matcher: Matcher,
        profiles: Mapping[str, AgentProfile],
        gateway: BudgetedGateway,
        max_steps: int,
        check: OutputCheck = check_reply,
        mask: Masker = mask_pii,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._config = config
        self._limiter = limiter
        self._matcher = matcher
        self._profiles = profiles
        self._gateway = gateway
        self._max_steps = max_steps
        self._check = check
        self._mask = mask
        self._clock = clock

    async def respond(
        self,
        business_id: str,
        request: AgentRequest,
        *,
        trace_id: str,
        service: AgentService,
        read_only: AgentService | None,
    ) -> GuardedResponse:
        """The response to one message (module docstring). Never raises but a cancel."""
        turn = _Turn(self._business(business_id))
        outcome: _Outcome | None = None
        try:
            with track_budget() as budget, track_screens() as screens:
                turn.budget = budget
                try:
                    outcome = await self._respond(
                        business_id, request, trace_id, service, read_only, turn, screens
                    )
                except Exception as failure:  # G3: fail closed; a cancel is not an Exception
                    turn.error_type = (
                        failure.error_type
                        if isinstance(failure, GuardFailed)
                        else type(failure).__name__
                    )
                    outcome = self._failed(trace_id, turn)
            return outcome.response
        finally:
            if outcome is not None:  # a cancelled request has no response to record
                with contextlib.suppress(Exception):  # never replaces the response
                    self._emit(business_id, trace_id, turn, outcome.decision)

    def _business(self, business_id: str) -> _Business:
        profile = self._profiles.get(business_id)
        guards = None if self._config is None else self._config.for_business(business_id)
        health = bool(guards is not None and guards.health)
        health = health or bool(profile is not None and profile.health)
        guidance = () if guards is None else guards.guidance
        if not guidance and profile is not None:
            guidance = profile.guidance  # tools.json's lines, equal where both exist (PR 4)
        types: set[BusinessType] = set()
        if profile is not None:
            types.add(profile.config.business_type)
        if guards is not None and guards.business_type is not None:
            types.add(guards.business_type)
        return _Business(guards, profile, health, guidance, frozenset(types))

    async def _respond(
        self,
        business_id: str,
        request: AgentRequest,
        trace_id: str,
        service: AgentService,
        read_only: AgentService | None,
        turn: _Turn,
        screens: ScreenTally,
    ) -> _Outcome:
        business = turn.business
        guards = business.guards
        if self._config is None or guards is None:
            priority = Priority.URGENT if business.health else Priority.HIGH
            return self._handoff(
                trace_id,
                HandoffReason.GUARDS_NOT_CONFIGURED,
                priority,
                DecidedBy.GUARDS_NOT_CONFIGURED,
                turn,
            )
        started = self._clock()
        raw = request.message
        if not within_cap(Surface.MESSAGE, raw):  # validation bounds it first (G1): defence only
            turn.findings[Finding.OVER_CAP] += 1
            turn.error_type = ERROR_TYPE_OVER_CAP
            return self._failed(trace_id, turn, DecidedBy.INPUT_CAP)
        prepared = prepare(raw)
        turn.invisible = prepared.invisible.invisible
        turn.ran(Check.CLEAN, started, self._clock())

        # 3. The red-flag screen: no Redis, no budget, no model (G1).
        started = self._clock()
        category = red_flag_category(
            prepared.folded, guards.red_flags, self._matcher, turn.findings
        )
        turn.ran(Check.RED_FLAG, started, self._clock())
        if category is not None:
            return self._handoff(
                trace_id,
                HandoffReason.SAFETY,
                red_flag_priority(category, business.types),
                RED_FLAG_DECIDED_BY[category],
                turn,
                show=True,
            )

        # 4. An uncovered language (D3 (i)).
        started = self._clock()
        uncovered = uncovered_languages(prepared.clean, guards.not_covered, self._config.scripts)
        turn.ran(Check.NOT_COVERED_LANGUAGE, started, self._clock())
        if uncovered:
            turn.findings[Finding.NOT_COVERED_LANGUAGE] += 1
            if business.health:
                return self._handoff(
                    trace_id,
                    HandoffReason.NOT_COVERED_LANGUAGE,
                    Priority.HIGH,
                    DecidedBy.NOT_COVERED_LANGUAGE,
                    turn,
                    show=True,
                )
        # G3: a message in a script the red-flag screen cannot read was never cleared by it, so a
        # later failure keeps the before-the-screen priority (kenji's PR 6 review).
        turn.cleared = not uncovered

        # 5. Limits, then the budget's pre-check (G9).
        limited = await self._limits(business_id, request, prepared.clean, trace_id, turn)
        if limited is not None:
            return limited

        # 6. Injection signals: a hit selects the read-only service (G7).
        started = self._clock()
        found = injection_findings(prepared, guards.injection, self._matcher)
        turn.ran(Check.INJECTION, started, self._clock())
        turn.findings.update(found)
        if found or uncovered:
            turn.decide(DecidedBy.INJECTION_MESSAGE)

        # 7. Masking, then `Question`'s rules again (G6, G13).
        started = self._clock()
        masked = self._mask(prepared.clean, locales())
        masked_request = AgentRequest.model_validate(
            {"message": masked.text, "customer_ref": request.customer_ref}
        )
        turn.ran(Check.MASK, started, self._clock())

        # 8. The service.
        screens.business, screens.message, screens.health = guards, prepared.clean, business.health
        chosen = read_only if turn.first is DecidedBy.INJECTION_MESSAGE else service
        if chosen is None:  # never the full service instead: fail closed (G3)
            raise RuntimeError("the read-only service is missing")
        try:
            run = await self._service(chosen, business_id, masked_request, trace_id)
        finally:
            turn.findings.update(screens.findings)
            for check in screens.checks:  # run inside the service's seams: no duration of their own
                turn.note(check)
        if screens.failed is not None:  # a seam's guard raised, whatever the service made of it
            raise GuardFailed(screens.failed)
        if isinstance(run, GuardRefused):
            return self._refused(run, trace_id, turn)
        if isinstance(run, GuardWithheld):  # a ticket's text (guards.store): stored expired
            turn.findings.update(run.findings)
            turn.withheld_action_id = run.action_id
            turn.decide(run.decided_by)
            return self._withheld(trace_id, turn)

        # 9. Output checks on model text (H3, G8).
        response = run.response
        started = self._clock()
        withheld = self._output(response, screens)
        if withheld is not None:
            turn.ran(Check.OUTPUT, started, self._clock())
            turn.findings.update(withheld.findings)
            turn.decide(withheld.decided_by)
            return self._withheld(trace_id, turn)
        if isinstance(response, ReplyResponse) or (
            isinstance(response, AnswerKindResponse) and response.answer.answer is not None
        ):
            turn.ran(Check.OUTPUT, started, self._clock())
        # The response passed: a tightening inside the service decided it (module docstring).
        if screens.dropped_chunks:
            turn.decide(DecidedBy.INJECTION_CHUNK)
        if screens.withheld_fields:
            turn.decide(DecidedBy.INJECTION_TOOL_RESULT)
        return _Outcome(response, self._passed(turn))

    @staticmethod
    async def _service(
        chosen: AgentService, business_id: str, request: AgentRequest, trace_id: str
    ) -> AgentRun | GuardRefused | GuardWithheld:
        """The service's run, or the refusal or withholding one of its seams raised (step 8).
        Any other exception propagates (G3: `guard_error`)."""
        try:
            return await chosen.respond(business_id, request, trace_id=trace_id)
        except (GuardRefused, GuardWithheld) as stopped:
            return stopped

    async def _limits(
        self, business_id: str, request: AgentRequest, message: Clean, trace_id: str, turn: _Turn
    ) -> _Outcome | None:
        started = self._clock()
        try:
            try:
                await self._limiter.admit(business_id, RouteSlug.AGENT, request.customer_ref)
            finally:
                turn.ran(Check.LIMITS, started, self._clock())
            profile = turn.business.profile
            if profile is None:  # not configured: the service makes no model call
                return None
            started = self._clock()
            try:
                needed = turn_precheck_micro_usd(
                    self._gateway,
                    profile,
                    message if message.strip() else request.message,
                    trace_id=trace_id,
                    max_steps=self._max_steps,
                )
                await require_budget(self._limiter, business_id, needed)
            finally:
                turn.ran(Check.BUDGET, started, self._clock())
        except GuardRefused as refusal:
            return self._refused(refusal, trace_id, turn)
        return None

    def _output(self, response: AgentResponse, screens: ScreenTally) -> GuardWithheld | None:
        """H3 on a reply's or an answer's text; None for every other kind (G8)."""
        if isinstance(response, ReplyResponse):
            context = output_context(screens, OutputKind.REPLY, self._matcher)
            return output_withheld(self._check(response.text, context))
        if isinstance(response, AnswerKindResponse) and response.answer.answer is not None:
            # Every cited chunk passed the screening search in this request; a citation of any
            # other chunk is a KeyError, which fails closed (G3).
            cited = tuple(screens.kept_chunks[s.chunk_id] for s in response.answer.sources)
            context = output_context(screens, OutputKind.ANSWER, self._matcher, cited)
            return output_withheld(self._check(response.answer.answer, context))
        return None

    # ── Responses ────────────────────────────────────────────────────────────────────────────

    def _handoff(
        self,
        trace_id: str,
        reason: HandoffReason,
        priority: Priority,
        decided_by: DecidedBy,
        turn: _Turn,
        *,
        show: bool | None = None,
    ) -> _Outcome:
        """A hand-off decided by `decided_by`. The guidance is shown for a health business or an
        urgent hand-off (G3), or whenever `show` says so (G1: a red flag always carries it);
        `no_guidance` is counted when there is none to show."""
        business = turn.business
        wanted = (business.health or priority is Priority.URGENT) if show is None else show
        if wanted and not business.guidance:
            turn.findings[Finding.NO_GUIDANCE] += 1
        shown = business.guidance if wanted else ()
        turn.decide(decided_by)
        response = HandoffResponse(
            trace_id=trace_id, reason=reason, priority=priority, guidance=shown
        )
        decision = GuardDecision(
            GuardAction.HANDOFF, decided_by, priority=priority, guidance=bool(shown)
        )
        return _Outcome(response, decision)

    def _failed(
        self, trace_id: str, turn: _Turn, decided_by: DecidedBy = DecidedBy.GUARD_ERROR
    ) -> _Outcome:
        """G3's `guard_error` hand-off, its priority by whether the red-flag screen cleared. A
        failure always decides as itself, never as an earlier rule: a stub or a broken guard can
        then never be scored as the rule it interrupted (PR 2's scoring rules)."""
        health = turn.business.health
        if turn.cleared:
            priority = Priority.HIGH if health else Priority.NORMAL
        else:
            priority = Priority.URGENT if health else Priority.HIGH
        return self._handoff(trace_id, HandoffReason.GUARD_ERROR, priority, decided_by, turn)

    def _withheld(self, trace_id: str, turn: _Turn) -> _Outcome:
        """A `guard_output` hand-off replacing the whole response (G2, G7, G8) [proposal],
        decided by the first rule in G1's order that changed the turn (the caller has recorded
        the withholding rule; an earlier read-only choice comes first)."""
        priority = Priority.HIGH if turn.business.health else Priority.NORMAL
        rule = turn.first or DecidedBy.GUARD_ERROR  # always set by the caller
        return self._handoff(trace_id, HandoffReason.GUARD_OUTPUT, priority, rule, turn)

    def _refused(self, refusal: GuardRefused, trace_id: str, turn: _Turn) -> _Outcome:
        """A limit, the budget or Redis failing (G9, PR 5's `refusal_decision`)."""
        business = turn.business
        decision = refusal_decision(refusal, health=business.health)
        if refusal.kind is RefusalKind.UNAVAILABLE:
            turn.error_type = refusal.error_type
        turn.decide(decision.decided_by)
        if decision.action is GuardAction.LIMIT:
            shown = business.guidance if decision.guidance else ()
            if decision.guidance and not shown:
                turn.findings[Finding.NO_GUIDANCE] += 1
            return _Outcome(limited_response(refusal, shown), decision)
        reason = (
            HandoffReason.BUDGET
            if refusal.kind is RefusalKind.BUDGET
            else HandoffReason.GUARD_ERROR
        )
        priority = decision.priority or Priority.HIGH
        return self._handoff(trace_id, reason, priority, decision.decided_by, turn)

    @staticmethod
    def _passed(turn: _Turn) -> GuardDecision:
        if turn.first is DecidedBy.INJECTION_MESSAGE:
            return GuardDecision(GuardAction.READ_ONLY, DecidedBy.INJECTION_MESSAGE)
        if turn.first is DecidedBy.INJECTION_CHUNK:
            return GuardDecision(GuardAction.DROP, DecidedBy.INJECTION_CHUNK)
        if turn.first is DecidedBy.INJECTION_TOOL_RESULT:
            return GuardDecision(GuardAction.WITHHOLD, DecidedBy.INJECTION_TOOL_RESULT)
        return UNCHANGED

    # ── The record ───────────────────────────────────────────────────────────────────────────

    def _emit(self, business_id: str, trace_id: str, turn: _Turn, decision: GuardDecision) -> None:
        budget = turn.budget
        spent = budget is not None and bool(
            budget.reserved_micro_usd or budget.settled_micro_usd or budget.refused
        )
        emit_guard_record(
            GuardRecord(
                business_id=business_id,
                trace_id=trace_id,
                route=RouteSlug.AGENT,
                rules_version=None if self._config is None else self._config.rules_version,
                decision=decision,
                checks=tuple(turn.checks),
                findings=dict(turn.findings),
                invisible=turn.invisible,
                error_type=turn.error_type,
                reserved_micro_usd=budget.reserved_micro_usd if spent and budget else None,
                settled_micro_usd=budget.settled_micro_usd if spent and budget else None,
                budget_refused=0 if budget is None else budget.refused,
                withheld_action_id=turn.withheld_action_id,
                durations_ms=turn.durations,
            )
        )
