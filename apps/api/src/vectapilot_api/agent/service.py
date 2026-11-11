"""The agent service: one customer message → one `/v1/agent` response (ADR 0016 decisions 2, 5, 7;
M5 PR 4). Team code around H1's loop; it never runs a tool itself and never writes but through
the pending-action store.

**Order, per message** (every step for one business, scoped by its id):
1. **No configuration, no agent.** A business without a tool configuration gets a hand-off
   (`not_configured`): no loop, no model call.
2. **W11, the safety step before any tool.** For a health business (`AgentProfile.health`: either
   `tools.json` or the corpus manifest says so):
   - `agent_router = off`: fail closed. A normal hand-off (`safety_floor_off`) with the business's
     emergency guidance, before any model call or tool.
   - `agent_router = on`: the router first (`routing.ClassifierRouter`, M4's `route_message`).
     The service does not trust the decision's route: it recomputes the route from the
     business's own policy (`policy.route(intent)`), and a decision that disagrees fails closed
     (`router_failed`: urgent with the guidance for a safety intent or an urgent route, else
     high). Then, in this order: a mandatory intent (`policy.mandatory`: the safety intents and
     §3.5's hand-offs) hands off at the policy's priority before any tool; `action_request`
     reaches the loop if the business enables tools; a `rag` route goes to `AnswerService`
     (`answer`). A fail-closed decision or a router exception hands off (`router_failed`) with
     the guidance (for a health business, or whenever the hand-off is urgent).
   A business of another type runs the loop directly under `off`, and is routed the same way
   under `on`.
3. **The loop** (`run_turn`, H1) inside `asyncio.timeout(agent_turn_timeout_seconds)`; its own
   expiry is `turn_timeout` (told apart from a raised `TimeoutError` by `.expired()`), and **any
   exception** from it is a hand-off (`agent_error`), never a 500 and never a reply.
4. **The outcome.** A reply is returned as the model wrote it. A loop `handoff(safety)` becomes
   `Priority.URGENT` with the guidance from configuration, never model text, for every business
   (§3.5: an emergency is urgent everywhere). Other loop hand-offs are `normal`. A proposal is
   stored as a pending action (`ProposalStore.create`: the arguments validated again, the token's
   hash, the database's expiry, its `stored` audit row); its token goes into this response only.
   A failure to store it is `proposal_not_stored`: the customer has no token, so nothing can be
   confirmed, and an orphan row simply expires. Anything else from the loop (not one of the
   three outcomes: a loop bug) is `agent_error`, with the guidance for a health business.
5. **The last resort.** Any other exception while deciding (building a response included) is an
   `agent_error` hand-off, with the guidance for a health business: never a 500.

**Routing for the eval harness.** `respond` follows the configured `agent_router` (`off` or `on`).
`respond_unrouted` runs the loop with no router and no W11 step (`router=none`): only the tool-call
harness (PR 6) calls it, its report says "safety floor not exercised", no setting can select it
(Settings allows `off` and `on` only) and no route calls it (tests/agent/test_route.py).

**What propagates.** `asyncio.CancelledError` always, and the exception types in `propagate`
(the harness passes `CapReached`, so a spend cap stops a run instead of reading as a hand-off).

**One `agent_turn` record per message**, metadata only: business, trace id, prompt version,
routing, the router model's artifact sha256 (under `on`), kind, reason, priority, intent and
how it was decided, the proposal's tool and action id, steps, the exception's class name,
latency. Never the message, a reply, an observation, the
arguments, the summary or the token.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Final, Literal, Protocol

from vectapilot_api.actions.types import PendingAction
from vectapilot_api.agent.loop import run_turn
from vectapilot_api.agent.profiles import AgentProfile
from vectapilot_api.agent.prompt import AGENT_PROMPT_VERSION
from vectapilot_api.agent.responses import (
    AgentRequest,
    AgentResponse,
    AnswerKindResponse,
    ConfirmationResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
    handoff_reason,
)
from vectapilot_api.agent.routing import IntentRouter
from vectapilot_api.agent.step_model import GatewayStepModel
from vectapilot_api.agent.tools import BoundTools
from vectapilot_api.agent.types import (
    ConfirmationRequired,
    LoopLimits,
    ModelHandoffReason,
    TurnHandoff,
    TurnLoop,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.answer.types import AnswerRun
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.intents.decide import RouteDecision
from vectapilot_api.intents.policy import Priority, RouteKind
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.tools.types import ToolContext, WriteProposal

AGENT_TURN: Final = "agent_turn"
"""The log message of the one record per message, and the key that holds it."""

AgentRouting = Literal["off", "on"]
"""`Settings.agent_router`: what a route can select."""
Routing = AgentRouting | Literal["none"]
"""`none` is the eval harness's alone (`respond_unrouted`)."""

logger = logging.getLogger("vectapilot_api.agent")
# Pinned at INFO like the gateway's: a quieter `log_level` must never drop an `agent_turn` record.
logger.setLevel(logging.INFO)


class ProposalStore(Protocol):
    """Stores a proposal as a pending action (`actions.pending.PendingActionStore`)."""

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction: ...


class AnswerSource(Protocol):
    """The cited answer for a `rag` route (`answer.service.AnswerService`)."""

    async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun: ...


@dataclass(frozen=True)
class AgentRun:
    """One message and how it came about: the route returns `response`; the eval harness (PR 6)
    reads the rest."""

    response: AgentResponse
    outcome: TurnOutcome | None = None
    """The loop's outcome, when it ran and returned."""
    decision: RouteDecision | None = None
    """The router's decision, under `agent_router = on`."""
    action: PendingAction | None = field(default=None, repr=False)
    """The stored pending action of a proposal (its token is masked in every repr)."""
    steps: int = 0
    error: str | None = None
    """The class name of the exception that became a hand-off (never its message)."""


@dataclass
class _Tally:
    routing: Routing = "off"
    steps: int = 0
    error: str | None = None
    decision: RouteDecision | None = None


class AgentService:
    """`/v1/agent` for one process: the businesses' profiles, the agent's gateway, the pending
    action store, the answer service and the configured routing."""

    def __init__(
        self,
        profiles: Mapping[str, AgentProfile],
        *,
        gateway: LLMGateway,
        store: ProposalStore,
        answers: AnswerSource,
        limits: LoopLimits,
        turn_timeout_s: float,
        routing: AgentRouting = "off",
        router: IntentRouter | None = None,
        router_artifact: str | None = None,
        loop: TurnLoop = run_turn,
        propagate: tuple[type[Exception], ...] = (),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if routing == "on" and router is None:
            raise ValueError("agent_router = on needs a router")
        if not turn_timeout_s > 0:
            raise ValueError("the turn timeout must be positive")
        self._profiles = profiles
        self._gateway = gateway
        self._store = store
        self._answers = answers
        self._limits = limits
        self._turn_timeout_s = turn_timeout_s
        self._routing: AgentRouting = routing
        self._router = router
        self._router_artifact = router_artifact
        self._loop = loop
        self._propagate = propagate
        self._clock = clock

    @property
    def routing(self) -> AgentRouting:
        return self._routing

    async def respond(self, business_id: str, request: AgentRequest, *, trace_id: str) -> AgentRun:
        """The response to one message under the configured routing (`off` or `on`)."""
        return await self._respond(business_id, request, trace_id, self._routing)

    async def respond_unrouted(
        self, business_id: str, request: AgentRequest, *, trace_id: str
    ) -> AgentRun:
        """The loop with no router and no W11 step (`router=none`): the eval harness only."""
        return await self._respond(business_id, request, trace_id, "none")

    # ── One message ──────────────────────────────────────────────────────────────────────────

    async def _respond(
        self, business_id: str, request: AgentRequest, trace_id: str, routing: Routing
    ) -> AgentRun:
        started = self._clock()
        tally = _Tally(routing=routing)
        run: AgentRun | None = None
        error: BaseException | None = None
        try:
            try:
                decided = await self._decide(business_id, request, trace_id, tally)
            except self._propagate:
                raise
            except Exception as failure:
                # The last resort (module docstring, 5): every path below already maps its own
                # failures; this catches a bug between them, so a customer still gets a next step.
                tally.error = type(failure).__name__
                profile = self._profiles.get(business_id)
                decided = self._handoff(
                    trace_id,
                    HandoffReason.AGENT_ERROR,
                    Priority.NORMAL,
                    profile.guidance if profile is not None and profile.health else (),
                    decision=tally.decision,
                )
            # One place fills what every path shares: the steps, the error, the decision.
            run = replace(decided, steps=tally.steps, error=tally.error, decision=tally.decision)
            return run
        except BaseException as failure:  # recorded only, then re-raised unchanged
            error = failure
            raise
        finally:
            # A failure here must never replace the response or the exception.
            with contextlib.suppress(Exception):
                self._emit(business_id, trace_id, tally, run, error, started)

    async def _decide(
        self, business_id: str, request: AgentRequest, trace_id: str, tally: _Tally
    ) -> AgentRun:
        profile = self._profiles.get(business_id)
        if profile is None:
            return self._handoff(trace_id, HandoffReason.NOT_CONFIGURED, Priority.NORMAL)
        if tally.routing == "off" and profile.health:
            # W11: no deterministic safety step without the router, so no tool runs.
            return self._handoff(
                trace_id, HandoffReason.SAFETY_FLOOR_OFF, Priority.NORMAL, profile.guidance
            )
        if tally.routing == "on":
            routed = await self._route(profile, request, trace_id, tally)
            if routed is not None:
                return routed
        return await self._turn(profile, request, trace_id, tally)

    async def _route(
        self, profile: AgentProfile, request: AgentRequest, trace_id: str, tally: _Tally
    ) -> AgentRun | None:
        """The router's decision as a response, or None when the message goes to the loop."""
        policy = profile.policy
        guidance = profile.guidance if profile.health else ()
        if self._router is None:  # pragma: no cover (refused in __init__)
            raise RuntimeError("agent_router = on needs a router")
        try:
            decision = await self._router.route(
                request.message,
                policy=policy,
                business_id=profile.business_id,
                trace_id=trace_id,
            )
        except self._propagate:
            raise
        except Exception as failure:
            # The safety step did not run: fail closed, with the guidance for a health business.
            tally.error = type(failure).__name__
            return self._handoff(trace_id, HandoffReason.ROUTER_FAILED, Priority.HIGH, guidance)
        tally.decision = decision
        intent = decision.intent
        if decision.decided_by == "fail_closed" or intent is None:
            # The safety step could not decide. An urgent hand-off shows the guidance for every
            # business (§3.5: an emergency is urgent everywhere), as the loop's handoff(safety).
            priority = decision.route.priority or Priority.HIGH
            shown = profile.guidance if profile.health or priority is Priority.URGENT else ()
            return self._handoff(
                trace_id, HandoffReason.ROUTER_FAILED, priority, shown, decision=decision
            )
        # Defence in depth (W11): the route comes from this business's own policy, never from
        # the decision. A decision whose route disagrees is a router bug, so fail closed, at least
        # as urgent as the policy would have been.
        expected = policy.route(intent)
        if decision.route != expected:
            urgent = intent in policy.safety_intents or expected.priority is Priority.URGENT
            priority = Priority.URGENT if urgent else Priority.HIGH
            shown = profile.guidance if profile.health or urgent else ()
            return self._handoff(
                trace_id, HandoffReason.ROUTER_FAILED, priority, shown, decision=decision
            )
        if intent in policy.mandatory:  # before the loop and the answer, whatever the route says
            priority = expected.priority or Priority.HIGH
            return self._handoff(
                trace_id,
                HandoffReason.SAFETY if intent in policy.safety_intents else HandoffReason.ROUTED,
                priority,
                profile.guidance if priority is Priority.URGENT else (),
                decision=decision,
            )
        if intent is Intent.ACTION_REQUEST and profile.tools_enabled:
            return None  # the policy's normal hand-off "until M5's tools" becomes the loop
        if expected.kind is RouteKind.RAG:
            return await self._answer(profile, request, trace_id, tally)
        return self._handoff(
            trace_id,
            HandoffReason.ROUTED,
            expected.priority or Priority.NORMAL,
            profile.guidance if expected.priority is Priority.URGENT else (),
            decision=decision,
        )

    async def _answer(
        self, profile: AgentProfile, request: AgentRequest, trace_id: str, tally: _Tally
    ) -> AgentRun:
        try:
            answered = await self._answers.answer(
                profile.business_id, request.message, trace_id=trace_id
            )
        except self._propagate:
            raise
        except Exception as failure:
            tally.error = type(failure).__name__
            return self._handoff(
                trace_id, HandoffReason.AGENT_ERROR, Priority.NORMAL, decision=tally.decision
            )
        return AgentRun(AnswerKindResponse(trace_id=trace_id, answer=answered.response))

    async def _turn(
        self, profile: AgentProfile, request: AgentRequest, trace_id: str, tally: _Tally
    ) -> AgentRun:
        context = ToolContext(
            business_id=profile.business_id, customer_ref=request.customer_ref, trace_id=trace_id
        )
        step_model = GatewayStepModel(
            self._gateway,
            profile.step_schema,
            business_id=profile.business_id,
            trace_id=trace_id,
            today=profile.config.sandbox_date,
            timezone=profile.config.timezone,
        )
        tools = BoundTools(profile.executor, context)
        scope = asyncio.timeout(self._turn_timeout_s)
        reason: HandoffReason | None = None
        outcome: TurnOutcome | None = None
        try:
            async with scope:
                outcome = await self._loop(
                    request.message, step_model=step_model, tools=tools, limits=self._limits
                )
        except TimeoutError as failure:
            if scope.expired():
                reason = HandoffReason.TURN_TIMEOUT
            else:  # raised inside the turn, not the turn's own limit: a failure like any other
                tally.error, reason = type(failure).__name__, HandoffReason.AGENT_ERROR
        except self._propagate:
            raise
        except Exception as failure:
            tally.error, reason = type(failure).__name__, HandoffReason.AGENT_ERROR
        finally:
            tally.steps = step_model.steps
        if outcome is None:
            return self._handoff(trace_id, reason or HandoffReason.AGENT_ERROR, Priority.NORMAL)
        return await self._outcome(profile, outcome, context, tally)

    async def _outcome(
        self, profile: AgentProfile, outcome: TurnOutcome, context: ToolContext, tally: _Tally
    ) -> AgentRun:
        trace_id = context.trace_id or ""
        if isinstance(outcome, TurnReply):
            return AgentRun(ReplyResponse(trace_id=trace_id, text=outcome.text), outcome)
        if isinstance(outcome, TurnHandoff):
            if outcome.reason == ModelHandoffReason.SAFETY:  # by value, not identity
                # W11: urgent, with the configured guidance; never the model's words.
                return self._handoff(
                    trace_id,
                    HandoffReason.SAFETY,
                    Priority.URGENT,
                    profile.guidance,
                    decision=tally.decision,
                    outcome=outcome,
                )
            return self._handoff(
                trace_id,
                handoff_reason(outcome.reason),
                Priority.NORMAL,
                decision=tally.decision,
                outcome=outcome,
            )
        if isinstance(outcome, ConfirmationRequired):
            return await self._store_proposal(outcome, context, tally)
        # Not an outcome at all: a loop bug. Never a proposal, never a reply (module docstring, 4).
        tally.error = type(outcome).__name__
        return self._handoff(
            trace_id,
            HandoffReason.AGENT_ERROR,
            Priority.NORMAL,
            profile.guidance if profile.health else (),
            decision=tally.decision,
        )

    async def _store_proposal(
        self, outcome: ConfirmationRequired, context: ToolContext, tally: _Tally
    ) -> AgentRun:
        trace_id = context.trace_id or ""
        try:
            action = await self._store.create(outcome.proposal, context)
        except self._propagate:
            raise
        except Exception as failure:
            tally.error = type(failure).__name__
            return self._handoff(
                trace_id,
                HandoffReason.PROPOSAL_NOT_STORED,
                Priority.NORMAL,
                decision=tally.decision,
                outcome=outcome,
            )
        response = ConfirmationResponse(
            trace_id=trace_id,
            action_id=action.id,
            tool=action.tool,
            summary=action.summary,
            expires_at=action.expires_at,
            confirm_token=action.confirm_token,
        )
        return AgentRun(response, outcome, action=action)

    @staticmethod
    def _handoff(
        trace_id: str,
        reason: HandoffReason,
        priority: Priority,
        guidance: tuple[str, ...] = (),
        *,
        decision: RouteDecision | None = None,
        outcome: TurnOutcome | None = None,
    ) -> AgentRun:
        response = HandoffResponse(
            trace_id=trace_id,
            reason=reason,
            priority=priority,
            intent=None if decision is None else decision.intent,
            guidance=guidance,
        )
        return AgentRun(response, outcome)

    # ── The record ───────────────────────────────────────────────────────────────────────────

    def _emit(
        self,
        business_id: str,
        trace_id: str,
        tally: _Tally,
        run: AgentRun | None,
        error: BaseException | None,
        started: float,
    ) -> None:
        response = None if run is None else run.response
        decision = tally.decision
        record: dict[str, object] = {
            "business_id": business_id,
            "trace_id": trace_id,
            "prompt_version": AGENT_PROMPT_VERSION,
            "routing": tally.routing,
            "kind": "error" if response is None else response.kind,
            "reason": response.reason.value if isinstance(response, HandoffResponse) else None,
            "priority": response.priority.value if isinstance(response, HandoffResponse) else None,
            "intent": None
            if decision is None or decision.intent is None
            else decision.intent.value,
            "decided_by": None if decision is None else decision.decided_by,
            "router_artifact": self._router_artifact if tally.routing == "on" else None,
            "tool": response.tool.value if isinstance(response, ConfirmationResponse) else None,
            "action_id": (
                str(response.action_id) if isinstance(response, ConfirmationResponse) else None
            ),
            "steps": tally.steps,
            "error": type(error).__name__ if error is not None else tally.error,
            "latency_ms": max(0.0, (self._clock() - started) * 1000),
        }
        logger.info(AGENT_TURN, extra={AGENT_TURN: record})
