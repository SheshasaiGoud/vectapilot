"""`AgentService` (M5 PR 4): W11, the router's decisions, the turn timeout, every exception a
hand-off, proposals stored with their token in the response only, and the `agent_turn` record.

The loop is a stand-in (`fakes.canned_loop`: one fixed outcome, never a step), so these tests
are the service's alone; H1's loop is tested in test_loop.py. The router is a fake too: M4's
`route_message` is shesha's open H1 and is never implemented here. No database, no model call:
every test also checks the fake provider was never called.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from uuid import UUID

import pytest

from tests.agent.fakes import (
    NO_DATABASE,
    PAWS,
    SUNRISE,
    TOOLS,
    canned_loop,
    one_step_stand_in,
    proposed,
)
from tests.agent.providers import BlockCheckingProvider, reply_json
from tests.answer.apps import gateway_for
from tests.intents.fakes import TRACE
from tests.tools.helpers import CORPUS_DIR
from vectapilot_api.actions.tokens import new_confirm_token
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.agent import service as service_module
from vectapilot_api.agent.profiles import (
    AgentProfile,
    build_profile,
    build_profiles,
    business_types,
)
from vectapilot_api.agent.responses import (
    AgentRequest,
    AnswerKindResponse,
    ConfirmationResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.routing import IntentRouter, RouterProgress
from vectapilot_api.agent.service import AGENT_TURN, AgentRun, AgentService
from vectapilot_api.agent.types import (
    ConfirmationRequired,
    LoopHandoffReason,
    LoopLimits,
    ModelHandoffReason,
    TurnHandoff,
    TurnLoop,
    TurnOutcome,
    TurnReply,
)
from vectapilot_api.answer.types import AnswerResponse, AnswerRun, Confidence
from vectapilot_api.evalrun.budget import CapReached
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.intents.decide import RouteDecision
from vectapilot_api.intents.policy import (
    RAG,
    BusinessType,
    Priority,
    Route,
    RouteKind,
    RoutePolicy,
    handoff,
)
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import ToolContext, WriteProposal

pytestmark = pytest.mark.anyio

PROFILES: Final = build_profiles(NO_DATABASE, TOOLS, business_types(TOOLS, CORPUS_DIR))
RETAIL: Final = PAWS.model_copy(
    update={"business_id": "corner-shop", "business_type": BusinessType.RETAIL}
)
SHOP: Final = build_profile(NO_DATABASE, RETAIL, BusinessType.RETAIL)
"""A non-health business: W11 does not apply, so `off` runs the loop."""
ALL: Final = {**PROFILES, SHOP.business_id: SHOP}

EMERGENCIES: Final = [
    ("sunrise-clinic", "chest pain, book me tomorrow"),
    ("sunrise-clinic", "seene mein dard hai, kal ka slot book karo"),
    ("paws-and-co", "my dog just ate rat poison, book the first slot today"),
]
"""The plan's W11 cases: an emergency plus a booking request (Sunrise in English and Hinglish,
Paws & Co in English)."""
CUSTOMER: Final = "cust-001"


def request(message: str = "Please book me a slot.") -> AgentRequest:
    return AgentRequest(message=message, customer_ref=CUSTOMER)


def must_not_run() -> TurnLoop:
    return canned_loop(AssertionError("the loop must not run here"))


@dataclass
class RecordingStore:
    """A pending-action store that records what it was asked and returns a fresh action."""

    error: Exception | None = None
    created: list[tuple[WriteProposal, ToolContext]] = field(default_factory=list)

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction:
        self.created.append((proposal, context))
        if self.error is not None:
            raise self.error
        return PendingAction(
            id=UUID("11111111-2222-4333-8444-555555555555"),
            tool=proposal.tool,
            summary=proposal.summary,
            expires_at=datetime(2026, 11, 16, 10, 15, tzinfo=UTC),
            confirm_token=new_confirm_token(),
        )


@dataclass
class CannedAnswers:
    error: Exception | None = None
    asked: list[tuple[str, str, str]] = field(default_factory=list)

    async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun:
        self.asked.append((business_id, question, trace_id))
        if self.error is not None:
            raise self.error
        response = AnswerResponse(
            outcome="dont_know",
            answer=None,
            sources=[],
            confidence=Confidence(value=None),
            trace_id=trace_id,
            prompt_version="rag-answer.1",
        )
        return AnswerRun(response, "no_hits", (), (), model_called=False)


@dataclass
class CannedRouter:
    """Returns the policy's decision for `intent` (or raises `error`); records each call."""

    intent: Intent | None = None
    decided_by: str = "classifier"
    error: Exception | None = None
    asked: list[tuple[str, str, str]] = field(default_factory=list)

    async def route(
        self,
        message: str,
        *,
        policy: RoutePolicy,
        business_id: str,
        trace_id: str,
        progress: RouterProgress,
    ) -> RouteDecision:
        self.asked.append((message, business_id, trace_id))
        if self.error is not None:
            raise self.error
        if self.decided_by == "fail_closed":
            return RouteDecision(
                intent=None,
                route=handoff(Priority.URGENT),
                decided_by="fail_closed",
                error="Transient",
            )
        assert self.intent is not None
        return RouteDecision(
            intent=self.intent,
            route=policy.route(self.intent),
            decided_by=self.decided_by,  # type: ignore[arg-type]
        )


@dataclass
class Built:
    service: AgentService
    provider: FakeProvider
    store: RecordingStore
    answers: CannedAnswers


def build(
    loop: TurnLoop | None = None,
    *,
    router: IntentRouter | None = None,
    store: RecordingStore | None = None,
    answers: CannedAnswers | None = None,
    turn_timeout_s: float = 35.0,
    router_timeout_s: float = 15.0,
    propagate: tuple[type[Exception], ...] = (),
    profiles: dict[str, AgentProfile] | None = None,
    router_artifact: str | None = None,
) -> Built:
    provider = FakeProvider()
    store = store or RecordingStore()
    answers = answers or CannedAnswers()
    service = AgentService(
        ALL if profiles is None else profiles,
        gateway=gateway_for(provider),
        store=store,
        answers=answers,
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=turn_timeout_s,
        routing="on" if router is not None else "off",
        router=router,
        router_timeout_s=router_timeout_s,
        router_artifact=router_artifact,
        loop=must_not_run() if loop is None else loop,
        propagate=propagate,
    )
    return Built(service, provider, store, answers)


def handoff_of(run: AgentRun) -> HandoffResponse:
    assert isinstance(run.response, HandoffResponse), run.response
    return run.response


# ── W11 while agent_router is off ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("business", "message"), EMERGENCIES)
async def test_under_off_a_health_business_fails_closed_with_its_guidance_before_any_tool(
    business: str, message: str
) -> None:
    built = build()  # the loop would fail the test if it ran

    run = await built.service.respond(business, request(message), trace_id=TRACE)

    response = handoff_of(run)
    assert response.reason is HandoffReason.SAFETY_FLOOR_OFF
    assert response.priority is Priority.NORMAL
    assert response.guidance == PROFILES[business].config.emergency_guidance  # separate lines
    assert built.provider.calls == [] and built.store.created == []


def test_both_fictional_businesses_are_health_types_so_off_is_closed_for_both() -> None:
    assert {business for business, profile in PROFILES.items() if profile.health} == set(TOOLS)


async def test_under_off_another_business_type_runs_the_loop() -> None:
    built = build(canned_loop(TurnReply("Monday at 10:20 is free.")))

    run = await built.service.respond(SHOP.business_id, request(), trace_id=TRACE)

    assert run.response == ReplyResponse(trace_id=TRACE, text="Monday at 10:20 is free.")


async def test_a_business_without_tools_is_handed_off_without_a_loop() -> None:
    built = build()

    run = await built.service.respond("no-such-config", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority, response.guidance) == (
        HandoffReason.NOT_CONFIGURED,
        Priority.NORMAL,
        (),
    )


# ── The loop's outcomes (router=none, the eval harness's path) ───────────────────────────────


@pytest.mark.parametrize(("business", "message"), EMERGENCIES)
async def test_a_loop_safety_handoff_becomes_urgent_with_the_configured_guidance(
    business: str, message: str
) -> None:
    # With the real loop and a fake provider: tests/agent/test_fake_turns.py (waits for H1).
    built = build(canned_loop(TurnHandoff(ModelHandoffReason.SAFETY)))

    run = await built.service.respond_unrouted(business, request(message), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.SAFETY, Priority.URGENT)
    assert response.guidance == PROFILES[business].config.emergency_guidance
    assert built.store.created == [] and run.action is None


@pytest.mark.parametrize(
    "reason",
    [ModelHandoffReason.CUSTOMER_ASKED, ModelHandoffReason.OTHER, *LoopHandoffReason],
    ids=lambda r: r.value,
)
async def test_every_other_loop_handoff_is_normal_with_its_own_reason(reason: Any) -> None:
    built = build(canned_loop(TurnHandoff(reason)))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason.value, response.priority, response.guidance) == (
        reason.value,
        Priority.NORMAL,
        (),
    )


def test_the_api_names_every_reason_the_model_and_the_loop_can_give() -> None:
    values = {reason.value for reason in HandoffReason}
    assert {r.value for r in ModelHandoffReason} | {r.value for r in LoopHandoffReason} <= values


async def test_a_proposal_is_stored_for_the_requests_customer_and_its_token_returned() -> None:
    envelope = proposed()
    assert envelope.proposal is not None
    built = build(canned_loop(ConfirmationRequired(envelope.proposal)))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    [(proposal, context)] = built.store.created
    assert proposal == envelope.proposal
    assert context == ToolContext(business_id="paws-and-co", customer_ref=CUSTOMER, trace_id=TRACE)
    assert isinstance(run.response, ConfirmationResponse)
    assert run.action is not None
    token = run.action.confirm_token.get_secret_value()
    assert run.response.summary == envelope.proposal.summary
    assert run.response.summary_language == "en"  # English-only until M8 (decided 2026-11-10)
    assert json.loads(run.response.model_dump_json())["confirm_token"] == token
    assert token not in repr(run) and token not in str(run.response.model_dump())


async def test_a_proposal_that_cannot_be_stored_hands_off_with_no_token() -> None:
    envelope = proposed()
    assert envelope.proposal is not None
    store = RecordingStore(error=OSError("connection lost"))
    built = build(canned_loop(ConfirmationRequired(envelope.proposal)), store=store)

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.PROPOSAL_NOT_STORED
    assert run.action is None and run.error == "OSError"
    assert "confirm_token" not in run.response.model_dump_json()


# ── The turn's limits and failures ───────────────────────────────────────────────────────────


async def test_a_turn_past_its_timeout_hands_off_as_turn_timeout() -> None:
    built = build(canned_loop(None), turn_timeout_s=0.01)  # the loop never returns

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.TURN_TIMEOUT
    assert run.error is None


async def test_a_timeout_error_raised_inside_the_turn_is_an_error_not_the_turns_timeout() -> None:
    built = build(canned_loop(TimeoutError()))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.AGENT_ERROR
    assert run.error == "TimeoutError"


@pytest.mark.parametrize(
    "error",
    [NotImplementedError("H1"), RuntimeError("a bug"), CapReached("cap"), OSError("db")],
    ids=lambda e: type(e).__name__,
)
async def test_any_exception_from_the_loop_is_a_handoff_never_a_reply(error: Exception) -> None:
    built = build(canned_loop(error))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.AGENT_ERROR
    assert run.error == type(error).__name__


async def test_the_real_loop_stub_is_handed_off_too() -> None:
    # While H1 is pending the stub raises NotImplementedError; once written, an unscripted fake
    # provider fails the first step. Either way the customer gets a hand-off, never a 500.
    provider = FakeProvider()
    service = AgentService(
        ALL,
        gateway=gateway_for(provider),
        store=RecordingStore(),
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
    )

    run = await service.respond(SHOP.business_id, request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.AGENT_ERROR


async def test_the_eval_cap_propagates_when_the_harness_asks() -> None:
    built = build(canned_loop(CapReached("cap")), propagate=(CapReached,))

    with pytest.raises(CapReached):
        await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)


async def test_a_cancel_always_propagates() -> None:
    built = build(canned_loop(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)


# ── agent_router = on (a fake router: M4's H1 is never implemented here) ────────────────────


@pytest.mark.parametrize(("business", "message"), EMERGENCIES)
async def test_under_on_an_emergency_is_an_urgent_handoff_with_guidance_before_any_tool(
    business: str, message: str
) -> None:
    router = CannedRouter(Intent.EMERGENCY, decided_by="safety_floor")
    built = build(router=router)

    run = await built.service.respond(business, request(message), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority, response.intent) == (
        HandoffReason.SAFETY,
        Priority.URGENT,
        Intent.EMERGENCY,
    )
    assert response.guidance == PROFILES[business].config.emergency_guidance
    assert router.asked == [(message, business, TRACE)]
    assert built.provider.calls == [] and built.store.created == []


async def test_under_on_professional_judgement_hands_off_before_any_tool() -> None:
    built = build(router=CannedRouter(Intent.PROFESSIONAL_JUDGEMENT))

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.SAFETY, Priority.HIGH)


@pytest.mark.parametrize(
    "intent", [Intent.COMPLAINT, Intent.REFUND_OR_PAYMENT, Intent.HUMAN_REQUEST]
)
async def test_under_on_other_handoff_intents_hand_off_at_the_policys_priority(
    intent: Intent,
) -> None:
    built = build(router=CannedRouter(intent))

    run = await built.service.respond("paws-and-co", request(), trace_id=TRACE)

    response = handoff_of(run)
    expected = PROFILES["paws-and-co"].policy.route(intent).priority
    assert (response.reason, response.priority, response.intent, response.guidance) == (
        HandoffReason.ROUTED,
        expected,
        intent,
        (),
    )


async def test_under_on_an_action_request_reaches_the_loop() -> None:
    built = build(
        canned_loop(TurnReply("Monday is free.")), router=CannedRouter(Intent.ACTION_REQUEST)
    )

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    assert isinstance(run.response, ReplyResponse)
    assert run.decision is not None and run.decision.intent is Intent.ACTION_REQUEST


async def test_under_on_an_action_request_without_tools_keeps_the_policys_handoff() -> None:
    no_tools = PAWS.model_copy(update={"enabled_tools": (), "ticket_categories": ()})
    profiles = {"paws-and-co": build_profile(NO_DATABASE, no_tools, BusinessType.VETERINARY)}
    built = build(router=CannedRouter(Intent.ACTION_REQUEST), profiles=profiles)

    run = await built.service.respond("paws-and-co", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTED, Priority.NORMAL)


@pytest.mark.parametrize("intent", [Intent.QUESTION, Intent.OUT_OF_SCOPE])
async def test_under_on_a_rag_route_is_the_answer_services_response_unchanged(
    intent: Intent,
) -> None:
    built = build(router=CannedRouter(intent))

    run = await built.service.respond(
        "sunrise-clinic", request("When do you open?"), trace_id=TRACE
    )

    assert isinstance(run.response, AnswerKindResponse)
    assert built.answers.asked == [("sunrise-clinic", "When do you open?", TRACE)]
    assert run.response.answer.prompt_version == "rag-answer.1"


async def test_under_on_a_failing_answer_is_a_handoff() -> None:
    built = build(router=CannedRouter(Intent.QUESTION), answers=CannedAnswers(error=OSError()))

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    assert handoff_of(run).reason is HandoffReason.AGENT_ERROR


async def test_under_on_a_fail_closed_decision_keeps_its_priority_and_shows_the_guidance() -> None:
    built = build(router=CannedRouter(decided_by="fail_closed"))

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority, response.intent) == (
        HandoffReason.ROUTER_FAILED,
        Priority.URGENT,
        None,
    )
    assert response.guidance == SUNRISE.emergency_guidance


BEFORE_SCORING: Final = [
    # [decided 2026-11-11, shesha] Nothing cleared the message: urgent with the guidance for a
    # health business (by its business type), the router-failure mapping (high) for any other.
    pytest.param("sunrise-clinic", Priority.URGENT, True, id="human_healthcare"),
    pytest.param("paws-and-co", Priority.URGENT, True, id="veterinary"),
    pytest.param(SHOP.business_id, Priority.HIGH, False, id="retail"),
]


@pytest.mark.parametrize(("business", "priority", "guided"), BEFORE_SCORING)
async def test_under_on_a_router_that_raises_before_scoring_fails_closed(
    business: str, priority: Priority, guided: bool, caplog: pytest.LogCaptureFixture
) -> None:
    # CannedRouter raises before any score (no top-1 in RouterProgress), like a failed embedding.
    caplog.set_level(logging.INFO)
    built = build(router=CannedRouter(error=RuntimeError("embedding failed")))

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, priority)
    assert response.guidance == (ALL[business].guidance if guided else ())
    assert bool(response.guidance) is guided
    assert run.error == "RuntimeError"
    [record] = agent_turns(caplog)
    assert (record["router_timeout"], record["priority"]) == (False, priority.value)


def test_on_needs_a_router() -> None:
    with pytest.raises(ValueError, match="needs a router"):
        AgentService(
            ALL,
            gateway=gateway_for(FakeProvider()),
            store=RecordingStore(),
            answers=CannedAnswers(),
            limits=LoopLimits(max_steps=4),
            turn_timeout_s=35.0,
            routing="on",
        )


# ── The router's own limit (decided 2026-11-11, shesha, M5 PR 4 review) ─────────────────────


@dataclass
class SlowRouter:
    """Waits `delay_s`, then returns the policy's decision for `intent`. Stands in for M4's
    router (embedding plus fallback call); records whether its wait was cancelled, so a test can
    see the limit stopped it instead of leaving it running.

    `top1`: recorded in the service's `RouterProgress` before the wait, as `ClassifierRouter`
    does after scoring (so the wait stands for the fallback call). `swallow`: a misbehaving
    router that catches its limit's cancel and returns its decision anyway. `convert`: one that
    turns the cancel into this exception instead."""

    delay_s: float
    intent: Intent = Intent.ACTION_REQUEST
    top1: Intent | None = None
    swallow: bool = False
    convert: Exception | None = None
    started: int = 0
    cancelled: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def route(
        self,
        message: str,
        *,
        policy: RoutePolicy,
        business_id: str,
        trace_id: str,
        progress: RouterProgress,
    ) -> RouteDecision:
        self.started += 1
        self.entered.set()
        if self.top1 is not None:
            progress.top1 = self.top1
        try:
            await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled = True
            if self.convert is not None:
                raise self.convert from None
            if not self.swallow:
                raise
        return RouteDecision(
            intent=self.intent, route=policy.route(self.intent), decided_by="classifier"
        )


def sleeping_loop(delay_s: float, result: TurnOutcome) -> TurnLoop:
    """A stand-in for `run_turn` that waits `delay_s`, then returns `result` (not a loop)."""

    async def loop(message: str, **_: Any) -> TurnOutcome:
        await asyncio.sleep(delay_s)
        return result

    return cast(TurnLoop, loop)


def agent_turns(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]


@pytest.mark.parametrize(("business", "priority", "guided"), BEFORE_SCORING)
async def test_a_router_past_its_limit_before_scoring_fails_closed_as_router_failed(
    business: str, priority: Priority, guided: bool
) -> None:
    # Before any score (still embedding: no top-1): urgent with the guidance for a health
    # business, high for any other [decided 2026-11-11, shesha]: the same as a router exception
    # at that point, so timing never decides urgency. The loop (must_not_run) and the answer
    # never run: no tool, no model call, no proposal. The router would answer after 1 s, well
    # inside the turn's 35 s: only its own limit stops it.
    router = SlowRouter(delay_s=1.0)
    built = build(router=router, router_timeout_s=0.01)

    run = await built.service.respond(business, request(EMERGENCIES[0][1]), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority, response.intent) == (
        HandoffReason.ROUTER_FAILED,
        priority,
        None,
    )
    assert response.guidance == (ALL[business].guidance if guided else ())
    assert bool(response.guidance) is guided
    assert (run.error, run.decision, run.outcome, run.action) == (None, None, None, None)
    assert router.started == 1 and router.cancelled  # stopped, not left running
    assert built.provider.calls == [] and built.store.created == [] and built.answers.asked == []


URGENT_TOP1: Final = [
    pytest.param(business, id=business)
    for business in ("sunrise-clinic", "paws-and-co", SHOP.business_id)
]


@pytest.mark.parametrize("business", URGENT_TOP1)
async def test_a_limit_during_the_fallback_keeps_an_emergency_top1_urgent_everywhere(
    business: str,
) -> None:
    # [router-timeout review, zara] The classifier already scored an emergency (below sigma, so
    # route_message went on to its fallback call), then the router's limit fired. That must hand
    # off exactly as route_message's own fail-closed step would if the fallback's deadline had
    # fired first: urgent, with the guidance for every business (§3.5), the shop included.
    router = SlowRouter(delay_s=1.0, top1=Intent.EMERGENCY)
    built = build(router=router, router_timeout_s=0.01)

    run = await built.service.respond(
        business, request("my kid swallowed a button battery from your order"), trace_id=TRACE
    )

    response = handoff_of(run)
    assert (response.reason, response.priority, response.intent) == (
        HandoffReason.ROUTER_FAILED,
        Priority.URGENT,
        None,
    )
    assert response.guidance == ALL[business].guidance and response.guidance
    assert (run.error, run.decision) == (None, None) and router.cancelled
    assert built.provider.calls == [] and built.store.created == [] and built.answers.asked == []


@pytest.mark.parametrize(
    ("business", "top1"),
    [
        # route_message's step 4: only an urgent hand-off top-1 raises the priority; a high
        # hand-off (professional judgement, complaint) and a rag route (question) stay high.
        ("sunrise-clinic", Intent.PROFESSIONAL_JUDGEMENT),
        ("paws-and-co", Intent.COMPLAINT),
        ("sunrise-clinic", Intent.QUESTION),
        (SHOP.business_id, Intent.COMPLAINT),
        (SHOP.business_id, Intent.QUESTION),
        (SHOP.business_id, Intent.HUMAN_REQUEST),
    ],
)
async def test_a_limit_during_the_fallback_with_another_top1_is_high(
    business: str, top1: Intent
) -> None:
    built = build(router=SlowRouter(delay_s=1.0, top1=top1), router_timeout_s=0.01)

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, Priority.HIGH)
    assert response.guidance == (ALL[business].guidance if ALL[business].health else ())


@pytest.mark.parametrize(
    ("business", "top1", "priority"),
    [
        ("sunrise-clinic", None, Priority.URGENT),
        ("sunrise-clinic", Intent.QUESTION, Priority.HIGH),
        (SHOP.business_id, None, Priority.HIGH),
        (SHOP.business_id, Intent.EMERGENCY, Priority.URGENT),
    ],
)
async def test_a_router_that_swallows_its_limit_is_never_followed(
    business: str, top1: Intent | None, priority: Priority, caplog: pytest.LogCaptureFixture
) -> None:
    # [router-timeout review, zara] The router catches its limit's cancel and returns an
    # action_request anyway. The limit still wins: no loop (must_not_run), no decision kept.
    caplog.set_level(logging.INFO)
    router = SlowRouter(delay_s=1.0, top1=top1, swallow=True)
    built = build(router=router, router_timeout_s=0.01)

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, priority)
    assert router.cancelled and (run.error, run.decision, run.outcome) == (None, None, None)
    assert built.provider.calls == [] and built.store.created == [] and built.answers.asked == []
    [record] = agent_turns(caplog)
    assert (record["router_timeout"], record["error"], record["intent"]) == (True, None, None)


@pytest.mark.parametrize(
    ("business", "top1", "priority"),
    [
        (SHOP.business_id, None, Priority.HIGH),
        (SHOP.business_id, Intent.EMERGENCY, Priority.URGENT),
        ("paws-and-co", None, Priority.URGENT),
        ("paws-and-co", Intent.COMPLAINT, Priority.HIGH),
    ],
)
async def test_a_router_that_turns_its_limit_into_another_error_is_still_its_limit(
    business: str, top1: Intent | None, priority: Priority, caplog: pytest.LogCaptureFixture
) -> None:
    # The class name is kept (it is the exception that became the hand-off), and the record says
    # the limit fired, with the same priority as a clean expiry.
    caplog.set_level(logging.INFO)
    router = SlowRouter(delay_s=1.0, top1=top1, convert=RuntimeError("cancelled"))
    built = build(router=router, router_timeout_s=0.01)

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, priority)
    assert run.error == "RuntimeError"
    [record] = agent_turns(caplog)
    assert (record["router_timeout"], record["error"]) == (True, "RuntimeError")


@dataclass
class ScoredThenFailed(CannedRouter):
    """Records `top1` as scored, then raises, as `ClassifierRouter` does today when M4's
    `route_message` stub raises `NotImplementedError` after scoring."""

    top1: Intent = Intent.EMERGENCY

    async def route(
        self,
        message: str,
        *,
        policy: RoutePolicy,
        business_id: str,
        trace_id: str,
        progress: RouterProgress,
    ) -> RouteDecision:
        progress.top1 = self.top1
        raise NotImplementedError("H1 (M4)")


@pytest.mark.parametrize(
    ("business", "top1", "priority"),
    [
        # [decided 2026-11-11, shesha] A router exception after scoring follows the top-1 rule,
        # as the limit does: urgent when the top-1's route is an urgent hand-off (an emergency,
        # for every business: §3.5), else high (a question's rag route).
        pytest.param("sunrise-clinic", Intent.EMERGENCY, Priority.URGENT, id="clinic-emergency"),
        pytest.param("sunrise-clinic", Intent.QUESTION, Priority.HIGH, id="clinic-question"),
        pytest.param("paws-and-co", Intent.EMERGENCY, Priority.URGENT, id="vet-emergency"),
        pytest.param("paws-and-co", Intent.QUESTION, Priority.HIGH, id="vet-question"),
        pytest.param(SHOP.business_id, Intent.EMERGENCY, Priority.URGENT, id="shop-emergency"),
        pytest.param(SHOP.business_id, Intent.QUESTION, Priority.HIGH, id="shop-question"),
    ],
)
async def test_a_router_error_after_scoring_follows_the_top1_rule(
    business: str, top1: Intent, priority: Priority, caplog: pytest.LogCaptureFixture
) -> None:
    # The guidance for a health business, or whenever the hand-off is urgent (the shop's
    # emergency included). The record still tells the exception from the limit. Under `on`
    # today, M4's route_message stub ends every message here.
    caplog.set_level(logging.INFO)
    built = build(router=ScoredThenFailed(top1=top1))

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    profile = ALL[business]
    assert (response.reason, response.priority, response.intent, run.error) == (
        HandoffReason.ROUTER_FAILED,
        priority,
        None,
        "NotImplementedError",
    )
    guided = profile.health or priority is Priority.URGENT
    assert response.guidance == (profile.guidance if guided else ())
    assert bool(response.guidance) is guided
    assert built.provider.calls == [] and built.store.created == [] and built.answers.asked == []
    [record] = agent_turns(caplog)
    assert (record["router_timeout"], record["error"]) == (False, "NotImplementedError")


@pytest.mark.parametrize("top1", list(Intent))
@pytest.mark.parametrize("business", ["sunrise-clinic", "paws-and-co", SHOP.business_id])
async def test_after_scoring_the_kind_of_failure_never_decides_urgency(
    business: str, top1: Intent
) -> None:
    # [decided 2026-11-11, shesha] For every scored top-1: the router's limit and a router
    # exception hand off at the same priority with the same guidance.
    expired = await build(
        router=SlowRouter(delay_s=1.0, top1=top1), router_timeout_s=0.01
    ).service.respond(business, request(), trace_id=TRACE)
    raised = await build(router=ScoredThenFailed(top1=top1)).service.respond(
        business, request(), trace_id=TRACE
    )

    by_limit, by_error = handoff_of(expired), handoff_of(raised)
    assert by_limit.reason is by_error.reason is HandoffReason.ROUTER_FAILED
    assert (by_limit.priority, by_limit.guidance) == (by_error.priority, by_error.guidance)
    assert (expired.error, raised.error) == (None, "NotImplementedError")


async def test_a_router_in_time_is_followed_as_before() -> None:
    router = SlowRouter(delay_s=0.01, intent=Intent.EMERGENCY)
    built = build(router=router, router_timeout_s=5.0)

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.SAFETY, Priority.URGENT)
    assert response.guidance == SUNRISE.emergency_guidance
    assert not router.cancelled and run.error is None


async def test_the_routers_limit_covers_the_router_only_never_the_turn() -> None:
    # A turn longer than the router's limit (but inside its own) still replies: the scope ends
    # when the router returns, and the turn keeps agent_turn_timeout_seconds.
    built = build(
        sleeping_loop(0.2, TurnReply("Monday is free.")),
        router=SlowRouter(delay_s=0.0),
        router_timeout_s=0.05,
    )

    run = await built.service.respond(SHOP.business_id, request(), trace_id=TRACE)

    assert run.response == ReplyResponse(trace_id=TRACE, text="Monday is free.")


async def test_the_routers_limit_does_not_cover_the_answer() -> None:
    @dataclass
    class SlowAnswers(CannedAnswers):
        async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun:
            await asyncio.sleep(0.2)
            return await super().answer(business_id, question, trace_id=trace_id)

    built = build(
        router=SlowRouter(delay_s=0.0, intent=Intent.QUESTION),
        answers=SlowAnswers(),
        router_timeout_s=0.05,
    )

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    assert isinstance(run.response, AnswerKindResponse)


async def test_a_timeout_error_raised_by_the_router_is_a_failure_not_its_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    # Raised before any score: for a health business that is urgent, like the limit itself.
    built = build(router=CannedRouter(error=TimeoutError()))

    run = await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, Priority.URGENT)
    assert response.guidance == SUNRISE.emergency_guidance
    assert run.error == "TimeoutError"
    [record] = agent_turns(caplog)
    assert (record["router_timeout"], record["error"]) == (False, "TimeoutError")


async def test_the_record_names_a_router_past_its_limit(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    built = build(router=SlowRouter(delay_s=60.0), router_timeout_s=0.01)

    await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)

    [record] = agent_turns(caplog)
    assert {k: record[k] for k in ("routing", "kind", "reason", "priority", "error")} == {
        "routing": "on",
        "kind": "handoff",
        "reason": "router_failed",
        "priority": "urgent",
        "error": None,
    }
    assert record["router_timeout"] is True and record["decided_by"] is None


async def test_a_cancel_while_routing_propagates_and_is_not_the_routers_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # CancelledError is never caught: the scope converts only its own cancel into TimeoutError.
    caplog.set_level(logging.INFO)
    router = SlowRouter(delay_s=60.0)
    built = build(router=router, router_timeout_s=30.0)
    task = asyncio.create_task(built.service.respond("sunrise-clinic", request(), trace_id=TRACE))
    await router.entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert router.cancelled
    [record] = agent_turns(caplog)
    assert (record["kind"], record["error"], record["router_timeout"]) == (
        "error",
        "CancelledError",
        False,
    )


def test_on_needs_a_router_timeout() -> None:
    with pytest.raises(ValueError, match="needs a router timeout"):
        AgentService(
            ALL,
            gateway=gateway_for(FakeProvider()),
            store=RecordingStore(),
            answers=CannedAnswers(),
            limits=LoopLimits(max_steps=4),
            turn_timeout_s=35.0,
            routing="on",
            router=CannedRouter(Intent.QUESTION),
        )


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_a_router_timeout_that_is_not_positive_and_finite_is_refused(timeout: float) -> None:
    # Settings refuses it too (`Seconds`: gt=0, finite); the service's own guard is checked here.
    with pytest.raises(ValueError, match="router timeout must be positive and finite"):
        build(router=CannedRouter(Intent.QUESTION), router_timeout_s=timeout)


# ── The record ───────────────────────────────────────────────────────────────────────────────


async def test_one_agent_turn_record_metadata_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    envelope = proposed()
    assert envelope.proposal is not None
    built = build(canned_loop(ConfirmationRequired(envelope.proposal)))
    private = "my private message"

    run = await built.service.respond_unrouted("paws-and-co", request(private), trace_id=TRACE)

    [record] = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    assert run.action is not None
    assert record == {
        "business_id": "paws-and-co",
        "trace_id": TRACE,
        "prompt_version": "agent-step.1",
        "routing": "none",
        "kind": "confirmation_required",
        "reason": None,
        "priority": None,
        "intent": None,
        "decided_by": None,
        "router_artifact": None,
        "router_timeout": False,
        "tool": "book",
        "action_id": str(run.action.id),
        "steps": 0,
        "error": None,
        "latency_ms": record["latency_ms"],
    }
    text = json.dumps(record) + " ".join(r.getMessage() for r in caplog.records)
    assert private not in text
    assert run.action.confirm_token.get_secret_value() not in text
    assert envelope.proposal.summary not in text


async def test_a_record_is_written_for_a_cancelled_turn_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    built = build(canned_loop(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    [record] = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    assert (record["kind"], record["error"]) == ("error", "CancelledError")


def test_expires_at_is_aware() -> None:
    action = PendingAction(
        id=UUID(int=1),
        tool=ToolName.BOOK,
        summary="s",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        confirm_token=new_confirm_token(),
    )
    response = ConfirmationResponse(
        trace_id=TRACE,
        action_id=action.id,
        tool=action.tool,
        summary=action.summary,
        expires_at=action.expires_at,
        confirm_token=action.confirm_token,
    )
    assert response.expires_at.tzinfo is not None
    assert "**********" in repr(response)


async def test_the_outcome_types_cover_every_turn_outcome() -> None:
    outcomes: list[TurnOutcome] = [TurnReply("x"), TurnHandoff(ModelHandoffReason.OTHER)]
    for outcome in outcomes:
        run = await build(canned_loop(outcome)).service.respond_unrouted(
            "paws-and-co", request(), trace_id=TRACE
        )
        assert run.outcome == outcome


# ── PR 4 review: the outcome branch fails closed [zara] ─────────────────────────────────────


@pytest.mark.parametrize(("business", "guided"), [("paws-and-co", True), (SHOP.business_id, False)])
async def test_a_loop_returning_something_that_is_not_an_outcome_is_a_handoff(
    business: str, guided: bool
) -> None:
    # An H1 bug, e.g. the raw `Handoff` step returned instead of a `TurnHandoff`: never a
    # proposal at normal priority, never a 500.
    built = build(canned_loop(cast(TurnOutcome, object())))

    run = await built.service.respond_unrouted(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.AGENT_ERROR, Priority.NORMAL)
    assert response.guidance == (ALL[business].guidance if guided else ())
    assert built.store.created == [] and run.error == "object"


@pytest.mark.parametrize("text", ["", "x" * 1501], ids=["empty", "too long"])
@pytest.mark.parametrize(("business", "guided"), [("paws-and-co", True), (SHOP.business_id, False)])
async def test_a_reply_the_response_cannot_hold_is_a_handoff_never_a_500(
    text: str, business: str, guided: bool
) -> None:
    built = build(canned_loop(TurnReply(text)))

    run = await built.service.respond_unrouted(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.AGENT_ERROR, Priority.NORMAL)
    assert response.guidance == (ALL[business].guidance if guided else ())
    assert run.error == "ValidationError"


async def test_a_safety_handoff_is_recognised_by_value() -> None:
    # A reason equal to "safety" but not the enum member itself (a loop that built it from text)
    # must still be urgent with the guidance.
    built = build(canned_loop(TurnHandoff(cast(ModelHandoffReason, "safety"))))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.SAFETY, Priority.URGENT)
    assert response.guidance == PAWS.emergency_guidance


# ── PR 4 review: the service checks the router's route against the policy [kenji] ───────────


@dataclass
class MisroutingRouter:
    """Returns `intent` with `wrong`, a route that is not the policy's (a router bug, built on
    purpose; `RouteDecision` does not check the pair)."""

    intent: Intent
    wrong: Route
    asked: int = 0

    async def route(
        self,
        message: str,
        *,
        policy: RoutePolicy,
        business_id: str,
        trace_id: str,
        progress: RouterProgress,
    ) -> RouteDecision:
        self.asked += 1
        return RouteDecision(intent=self.intent, route=self.wrong, decided_by="classifier")


def misroutes() -> list[Any]:
    """Every mandatory intent of both business types with a looser route than its policy's: rag,
    and a hand-off below the policy's priority where one exists. Plus a non-mandatory pair."""
    cases: list[Any] = []
    for business in ("sunrise-clinic", SHOP.business_id):
        policy = ALL[business].policy
        for intent in sorted(policy.mandatory):
            expected = policy.route(intent)
            looser = [RAG] + [handoff(p) for p in Priority if handoff(p).urgency < expected.urgency]
            for route in looser:
                label = route.kind.value if route.priority is None else route.priority.value
                cases.append(
                    pytest.param(business, intent, route, id=f"{business}-{intent}-{label}")
                )
    cases.append(
        pytest.param("sunrise-clinic", Intent.ACTION_REQUEST, RAG, id="action_request-as-rag")
    )
    cases.append(
        pytest.param(
            SHOP.business_id, Intent.QUESTION, handoff(Priority.NORMAL), id="question-as-handoff"
        )
    )
    return cases


@pytest.mark.parametrize(("business", "intent", "route"), misroutes())
async def test_a_route_that_disagrees_with_the_policy_fails_closed(
    business: str, intent: Intent, route: Route
) -> None:
    profile = ALL[business]
    expected = profile.policy.route(intent)
    assert route != expected
    router = MisroutingRouter(intent, route)
    built = build(router=router)  # the loop would fail the test

    run = await built.service.respond(business, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert router.asked == 1 and run.error is None
    assert (response.reason, response.intent) == (HandoffReason.ROUTER_FAILED, intent)
    assert handoff(response.priority).urgency >= expected.urgency  # never looser than policy
    if intent in profile.policy.safety_intents or expected.priority is Priority.URGENT:
        assert response.priority is Priority.URGENT
        assert response.guidance == profile.guidance
    else:  # failing closed shows a health business its guidance, as a router failure does
        assert response.priority is Priority.HIGH
        assert response.guidance == (profile.guidance if profile.health else ())
    if intent is Intent.EMERGENCY:
        assert (response.priority, response.guidance) == (Priority.URGENT, profile.guidance)
    assert built.answers.asked == [] and built.provider.calls == [] and built.store.created == []


def test_the_misroute_cases_cover_every_mandatory_intent_of_both_types() -> None:
    covered = {(p.values[0], p.values[1]) for p in misroutes()}
    for business in ("sunrise-clinic", SHOP.business_id):
        assert {(business, i) for i in ALL[business].policy.mandatory} <= covered
    assert RouteKind.RAG in {p.values[2].kind for p in misroutes()}


async def test_under_on_a_fail_closed_urgent_decision_shows_guidance_for_any_business() -> None:
    # §3.5: an emergency is urgent everywhere, so a non-health business gets its guidance too.
    built = build(router=CannedRouter(decided_by="fail_closed"))

    run = await built.service.respond(SHOP.business_id, request(), trace_id=TRACE)

    response = handoff_of(run)
    assert (response.reason, response.priority) == (HandoffReason.ROUTER_FAILED, Priority.URGENT)
    assert response.guidance == RETAIL.emergency_guidance


# ── PR 4 review: mutation survivors [mutation] ───────────────────────────────────────────────


async def test_the_record_and_the_run_count_the_step_models_calls() -> None:
    # S24: a stand-in that asks the step model once (tests/agent/fakes.py, not a loop).
    provider = BlockCheckingProvider([reply_json("Hello.")])
    service = AgentService(
        ALL,
        gateway=gateway_for(provider),
        store=RecordingStore(),
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
        loop=one_step_stand_in(TurnReply("x")),
    )

    run = await service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert run.steps == 1 and len(provider.calls) == 1
    # S35: the step model got the business's sandbox date and timezone (W5's sandbox clock).
    system = provider.calls[0].system
    assert system is not None
    assert (
        f"Today is {PAWS.sandbox_date.isoformat()} in the business's timezone, {PAWS.timezone}."
        in system
    )


async def test_the_record_holds_the_step_count(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    provider = BlockCheckingProvider([reply_json("Hello.")])
    service = AgentService(
        ALL,
        gateway=gateway_for(provider),
        store=RecordingStore(),
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
        loop=one_step_stand_in(TurnReply("x")),
    )

    await service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    [record] = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    assert record["steps"] == 1


async def test_a_routed_record_names_the_decision_and_the_router_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # S36 and kenji's traceability finding: under `on` the record says which model decided.
    caplog.set_level(logging.INFO)
    artifact = "ab" * 32
    built = build(
        router=CannedRouter(Intent.EMERGENCY, decided_by="safety_floor"), router_artifact=artifact
    )
    private = "chest pain, book me tomorrow"

    await built.service.respond("sunrise-clinic", request(private), trace_id=TRACE)

    [record] = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    assert {k: record[k] for k in ("routing", "kind", "reason", "priority", "intent")} == {
        "routing": "on",
        "kind": "handoff",
        "reason": "safety",
        "priority": "urgent",
        "intent": "emergency",
    }
    assert (record["decided_by"], record["router_artifact"]) == ("safety_floor", artifact)
    assert private not in json.dumps(record)


async def test_without_routing_the_record_names_no_router_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    built = build(canned_loop(TurnReply("x")), router_artifact="ab" * 32)

    await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    [record] = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    assert (record["routing"], record["router_artifact"]) == ("none", None)


def _capped_router() -> Built:
    return build(router=CannedRouter(error=CapReached("cap")), propagate=(CapReached,))


def _capped_answer() -> Built:
    return build(
        router=CannedRouter(Intent.QUESTION),
        answers=CannedAnswers(error=CapReached("cap")),
        propagate=(CapReached,),
    )


@pytest.mark.parametrize("built_by", [_capped_router, _capped_answer], ids=["router", "answer"])
async def test_the_eval_cap_propagates_from_the_router_and_the_answer(
    built_by: Any,
) -> None:
    # S08, S19: a spend cap hit while routing or answering stops the run, never a hand-off.
    built: Built = built_by()

    with pytest.raises(CapReached):
        await built.service.respond("sunrise-clinic", request(), trace_id=TRACE)


async def test_the_eval_cap_propagates_from_the_store() -> None:
    # S30.
    envelope = proposed()
    assert envelope.proposal is not None
    built = build(
        canned_loop(ConfirmationRequired(envelope.proposal)),
        store=RecordingStore(error=CapReached("cap")),
        propagate=(CapReached,),
    )

    with pytest.raises(CapReached):
        await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)


async def test_a_record_that_cannot_be_written_never_replaces_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S33: "never a 5xx" includes a broken log handler.
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the log handler failed")

    monkeypatch.setattr(service_module.logger, "info", broken)
    built = build(canned_loop(TurnReply("x")))

    run = await built.service.respond_unrouted("paws-and-co", request(), trace_id=TRACE)

    assert run.response == ReplyResponse(trace_id=TRACE, text="x")


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_a_turn_timeout_that_is_not_positive_is_refused(timeout: float) -> None:
    # S02: Settings refuses it too; the service's own guard is checked here.
    with pytest.raises(ValueError, match="must be positive"):
        build(turn_timeout_s=timeout)


def test_the_request_never_shows_the_message_in_its_repr() -> None:
    # R06: rule 5, and the record's "never the message".
    assert "my private message" not in repr(
        AgentRequest(message="my private message", customer_ref=CUSTOMER)
    )
