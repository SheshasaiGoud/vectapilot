"""H1 tests: `route_message`, the router's decision (guide: docs/learning/m4-router.md).

`route_message` is [HAND-WRITE]: shesha writes it (D4). Until then it raises NotImplementedError,
so the whole module is marked `xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong answer    → a normal failure (only NotImplementedError counts as expected);
- implemented, right answer    → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, delete the `pytestmark` statement below (and the two
marks in tests/intents/test_fallback.py) and run again: every test must then simply pass. Do not
edit a test to make it pass; change the code.

How to read a case: probabilities are dyadic (1/2, 1/4, 3/8, 3/32, ...), exact as floats, so a
sum or a comparison at a threshold is exact. An intent not named has probability 0. Each
expected decision is built *before* the call: `RouteDecision` checks itself when built, so an
impossible expectation fails here even while the stub stands. Every message and probability is
made up; no number is a result.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Final

import pytest

from tests.intents.fakes import (
    BUSINESS,
    MESSAGE,
    TRACE,
    FallbackStep,
    ScriptedFallback,
    every_gateway_error,
    names,
    scores,
)
from vectapilot_api.evalrun.budget import CapReached
from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds, route_message
from vectapilot_api.intents.policy import (
    HEALTH_SAFETY,
    PLATFORM_SAFETY,
    RAG,
    BusinessType,
    Priority,
    Route,
    RoutePolicy,
    handoff,
    policy_for,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H1: shesha writes route_message — see docs/learning/m4-router.md",
)

E, PJ, C, R = (
    Intent.EMERGENCY,
    Intent.PROFESSIONAL_JUDGEMENT,
    Intent.COMPLAINT,
    Intent.REFUND_OR_PAYMENT,
)
HR, AR, Q, OOS = (
    Intent.HUMAN_REQUEST,
    Intent.ACTION_REQUEST,
    Intent.QUESTION,
    Intent.OUT_OF_SCOPE,
)

HEALTH: Final = policy_for("human_healthcare")
"""Safety intents: emergency and professional_judgement."""
VET: Final = policy_for("veterinary")
DEFAULT: Final = policy_for("ecommerce")
"""Safety intents: emergency only."""

URGENT, HIGH, NORMAL = handoff(Priority.URGENT), handoff(Priority.HIGH), handoff(Priority.NORMAL)
THRESHOLDS: Final = RouterThresholds(tau=0.75, sigma=0.25)

PJ_URGENT: Final = RoutePolicy(
    name="test-pj-urgent",
    business_types=frozenset({BusinessType.HUMAN_HEALTHCARE}),
    routes={**HEALTH.routes, PJ: URGENT},
    safety_intents=HEALTH_SAFETY,
)
"""A valid template that tightens professional_judgement to an urgent hand-off (§4 allows a
business to tighten): emergency is then not the only urgent intent."""


async def decide(
    probabilities: Mapping[Intent, float],
    fallback: ScriptedFallback,
    *,
    policy: RoutePolicy = HEALTH,
    thresholds: RouterThresholds = THRESHOLDS,
) -> RouteDecision:
    return await route_message(
        scores(probabilities),
        MESSAGE,
        policy,
        fallback,
        thresholds=thresholds,
        business_id=BUSINESS,
        trace_id=TRACE,
    )


def floor(intent: Intent, policy: RoutePolicy = HEALTH) -> RouteDecision:
    return RouteDecision(intent=intent, route=policy.route(intent), decided_by="safety_floor")


def by_classifier(intent: Intent, policy: RoutePolicy = HEALTH) -> RouteDecision:
    return RouteDecision(intent=intent, route=policy.route(intent), decided_by="classifier")


def by_llm(intent: Intent, policy: RoutePolicy = HEALTH) -> RouteDecision:
    return RouteDecision(
        intent=intent, route=policy.route(intent), decided_by="llm", llm_intent=intent
    )


def kept(intent: Intent, llm: Intent, policy: RoutePolicy = HEALTH) -> RouteDecision:
    return RouteDecision(
        intent=intent, route=policy.route(intent), decided_by="classifier_kept", llm_intent=llm
    )


# ── 1. The safety floor ─────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("probabilities", "intent"),
    [
        # Mass 3/16 + 1/16 = 1/4 = sigma exactly: at the floor. Emergency is the more probable.
        ({E: 0.1875, PJ: 0.0625, Q: 0.5, OOS: 0.25}, E),
        # The same mass, professional_judgement the more probable: a high hand-off.
        ({PJ: 0.1875, E: 0.0625, Q: 0.5, OOS: 0.25}, PJ),
    ],
    ids=["emergency", "professional_judgement"],
)
async def test_safety_mass_at_sigma_hands_off_to_the_most_probable_safety_intent(
    probabilities: Mapping[Intent, float], intent: Intent
) -> None:
    fallback = ScriptedFallback()
    expected = floor(intent)

    assert await decide(probabilities, fallback) == expected
    assert fallback.calls == []  # the floor never calls the LLM


@pytest.mark.anyio
async def test_the_floor_is_checked_before_tau() -> None:
    # Question at 3/4 reaches tau (3/4), but emergency's 1/4 reaches sigma (1/4): a confident
    # wrong argmax must not hide a sizeable safety probability. Urgent, no LLM call.
    fallback = ScriptedFallback()
    expected = floor(E)

    assert await decide({Q: 0.75, E: 0.25}, fallback) == expected
    assert fallback.calls == []


@pytest.mark.anyio
async def test_the_safety_mass_is_a_sum_not_the_largest_safety_probability() -> None:
    # 3/32 + 5/32 = 1/4 = sigma, though neither alone reaches it. The more probable: PJ.
    fallback = ScriptedFallback()
    expected = floor(PJ)

    assert await decide({E: 0.09375, PJ: 0.15625, Q: 0.75}, fallback) == expected
    assert fallback.calls == []


@pytest.mark.anyio
async def test_a_tie_between_safety_intents_goes_to_the_first_in_decision_order() -> None:
    expected = floor(E)

    assert await decide({PJ: 0.25, E: 0.25, Q: 0.5}, ScriptedFallback()) == expected


@pytest.mark.anyio
async def test_safety_mass_just_below_sigma_is_not_the_floor() -> None:
    # 1/8 + 1/16 = 3/16 < 1/4; question 13/16 >= tau: the classifier decides question (rag).
    fallback = ScriptedFallback()
    expected = by_classifier(Q)

    assert await decide({E: 0.125, PJ: 0.0625, Q: 0.8125}, fallback) == expected
    assert expected.route == RAG
    assert fallback.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("policy", "script", "expected", "llm_calls"),
    [
        # Health: professional_judgement is a safety intent, its 1/4 reaches sigma.
        (HEALTH, [], floor(PJ, HEALTH), 0),
        (VET, [], floor(PJ, VET), 0),
        # Default: only emergency is (0 here); question's 1/2 < tau, so the LLM is asked.
        (DEFAULT, [Q], kept(PJ, Q, DEFAULT), 1),
    ],
    ids=["human_healthcare", "veterinary", "default"],
)
async def test_the_floor_sums_only_the_business_types_safety_intents(
    policy: RoutePolicy, script: list[FallbackStep], expected: RouteDecision, llm_calls: int
) -> None:
    # PJ 1/2, question 1/4, out_of_scope 1/4; sigma 1/4, tau 3/4. Under a health type PJ's 1/2
    # is safety mass: the floor. Under default the safety mass (emergency) is 0, PJ is the top-1
    # below tau, and the LLM's `question` cannot loosen it: PJ is kept (a high hand-off).
    fallback = ScriptedFallback(list(script))

    decision = await decide({PJ: 0.5, Q: 0.25, OOS: 0.25}, fallback, policy=policy)

    assert decision == expected
    assert len(fallback.calls) == llm_calls


# ── 2. The threshold ────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("thresholds", "probabilities", "intent"),
    [
        (RouterThresholds(tau=0.75, sigma=0.25), {C: 0.75, Q: 0.25}, C),
        (RouterThresholds(tau=0.5, sigma=0.25), {AR: 0.5, Q: 0.25, OOS: 0.25}, AR),
        (RouterThresholds(tau=1.0, sigma=0.25), {OOS: 1.0}, OOS),
    ],
    ids=["complaint at 3/4", "action_request at 1/2", "out_of_scope at tau 1"],
)
async def test_a_top_probability_at_tau_is_the_classifiers_decision_without_an_llm_call(
    thresholds: RouterThresholds, probabilities: Mapping[Intent, float], intent: Intent
) -> None:
    fallback = ScriptedFallback()
    expected = by_classifier(intent)

    assert await decide(probabilities, fallback, thresholds=thresholds) == expected
    assert fallback.calls == []


@pytest.mark.anyio
async def test_a_top_probability_just_below_tau_asks_the_llm_once() -> None:
    # Complaint 23/32 < 3/4: the band. The LLM agrees.
    fallback = ScriptedFallback([C])
    expected = by_llm(C)

    assert await decide({C: 0.71875, Q: 0.28125}, fallback) == expected
    assert len(fallback.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("first", "second"),
    [(C, R), (HR, AR), (Q, OOS), (R, Q)],
    ids=lambda i: i.value if isinstance(i, Intent) else None,
)
async def test_a_tie_at_the_top_goes_to_the_first_intent_in_decision_order(
    first: Intent, second: Intent
) -> None:
    # Two intents at 1/2 each, tau 1/2: the classifier decides, the earlier one wins. (Scores
    # iterate in decision order however they were built; the tie rule is still yours to apply.)
    expected = by_classifier(first)

    decision = await decide(
        {second: 0.5, first: 0.5},
        ScriptedFallback(),
        thresholds=RouterThresholds(tau=0.5, sigma=0.25),
    )

    assert decision == expected


@pytest.mark.anyio
async def test_the_route_comes_from_the_policy_never_from_a_table_of_your_own() -> None:
    # A valid template that tightens question to a normal hand-off (allowed: tighten, never
    # loosen; still monotone in the decision order).
    tightened = RoutePolicy(
        name="test-tightened",
        business_types=frozenset({BusinessType.SERVICE}),
        routes={**DEFAULT.routes, Q: NORMAL},
        safety_intents=PLATFORM_SAFETY,
    )
    expected = by_classifier(Q, tightened)

    assert await decide({Q: 1.0}, ScriptedFallback(), policy=tightened) == expected
    assert expected.route == NORMAL


# ── 3. The fallback: once, below tau, tighten only ──────────────────────────────────────────

BAND: Final[Mapping[Intent, float]] = {Q: 0.5, AR: 0.25, OOS: 0.25}
"""Top-1 question at 1/2 < tau, no safety mass: the LLM band."""


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("llm", "route"),
    [(E, URGENT), (PJ, HIGH), (C, HIGH), (AR, NORMAL)],
    ids=lambda v: v.value if isinstance(v, Intent) else None,
)
async def test_below_tau_the_llm_may_add_an_escalation(llm: Intent, route: Route) -> None:
    fallback = ScriptedFallback([llm])
    expected = by_llm(llm)

    decision = await decide(BAND, fallback)

    assert decision == expected
    assert decision.route == route
    assert len(fallback.calls) == 1


@pytest.mark.anyio
async def test_an_llm_answer_earlier_than_out_of_scope_wins() -> None:
    # Top-1 out_of_scope (1/2); the LLM says question, which comes earlier: the LLM's.
    expected = by_llm(Q)

    assert await decide({OOS: 0.5, Q: 0.25, AR: 0.25}, ScriptedFallback([Q])) == expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("top", "llm", "policy"),
    [
        (C, Q, HEALTH),
        (R, OOS, HEALTH),
        (HR, AR, HEALTH),
        (AR, Q, VET),
        # Not a safety intent under default, but still kept: tighten-only is about order.
        (PJ, Q, DEFAULT),
    ],
    ids=["complaint", "refund", "human_request", "action_request", "pj under default"],
)
async def test_below_tau_the_llm_never_removes_an_escalation(
    top: Intent, llm: Intent, policy: RoutePolicy
) -> None:
    fallback = ScriptedFallback([llm])
    expected = kept(top, llm, policy)
    others = [i for i in (Q, OOS, AR) if i not in (top, llm)]

    decision = await decide({top: 0.5, llm: 0.25, others[0]: 0.25}, fallback, policy=policy)

    assert decision == expected
    assert len(fallback.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("policy", [HEALTH, VET, DEFAULT], ids=lambda p: p.name)
async def test_an_emergency_top_1_below_tau_and_sigma_stays_urgent_whatever_the_llm_says(
    policy: RoutePolicy,
) -> None:
    # Emergency 3/8 is the top-1 but below tau; the mass 3/8 is below sigma 1/2. The LLM says
    # question (steered, or wrong): the hand-off stays urgent.
    expected = kept(E, Q, policy)

    decision = await decide(
        {E: 0.375, Q: 0.25, C: 0.1875, OOS: 0.1875},
        ScriptedFallback([Q]),
        policy=policy,
        thresholds=RouterThresholds(tau=0.5, sigma=0.5),
    )

    assert decision == expected
    assert decision.route == URGENT


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("probabilities", "llm", "top"),
    [
        ({Q: 0.5, OOS: 0.5}, OOS, Q),  # a tied top-1 is question, so the LLM's oos is kept out
        ({C: 0.375, R: 0.375, Q: 0.25}, R, C),
    ],
    ids=["question-oos", "complaint-refund"],
)
async def test_the_top_1_in_the_band_breaks_ties_in_decision_order(
    probabilities: Mapping[Intent, float], llm: Intent, top: Intent
) -> None:
    expected = kept(top, llm)

    assert await decide(probabilities, ScriptedFallback([llm])) == expected


@pytest.mark.anyio
async def test_the_fallback_gets_the_message_business_and_trace_id_unchanged() -> None:
    fallback = ScriptedFallback([Q])

    await decide(BAND, fallback)

    assert fallback.calls == [(MESSAGE, BUSINESS, TRACE)]


@pytest.mark.anyio
async def test_the_llm_is_asked_exactly_once() -> None:
    # A second call would answer emergency and change the decision: there must be none.
    fallback = ScriptedFallback([Q, E])
    expected = by_llm(Q)

    assert await decide(BAND, fallback) == expected
    assert len(fallback.calls) == 1


# ── 4. Failing closed ───────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("error", every_gateway_error(), ids=names(every_gateway_error()))
@pytest.mark.parametrize(
    ("probabilities", "route"),
    [({E: 0.375, Q: 0.25, C: 0.1875, OOS: 0.1875}, URGENT), (BAND, HIGH)],
    ids=["emergency top-1", "question top-1"],
)
async def test_any_gateway_error_hands_off_with_no_intent(
    probabilities: Mapping[Intent, float], route: Route, error: GatewayError
) -> None:
    # Every kind keeps the top-1's urgency: a provider refusal (ModelRefused) is most likely on
    # exactly the messages the floor exists for. Top-1 emergency at 3/8 (safety mass 3/8 < sigma
    # 1/2, below tau 3/4): urgent. Top-1 question (rag) at 1/2: high. `error` is the class name,
    # never its text.
    fallback = ScriptedFallback([error])
    expected = RouteDecision(
        intent=None, route=route, decided_by="fail_closed", error=type(error).__name__
    )

    decision = await decide(
        probabilities, fallback, thresholds=RouterThresholds(tau=0.75, sigma=0.5)
    )

    assert decision == expected
    assert len(fallback.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("top", "route"),
    [
        (E, URGENT),
        (PJ, HIGH),
        (C, HIGH),
        (R, HIGH),
        (HR, HIGH),  # a normal hand-off becomes high
        (AR, HIGH),
        (Q, HIGH),  # rag becomes high
        (OOS, HIGH),
    ],
    ids=[i.value for i in DECISION_ORDER],
)
async def test_fail_closed_is_the_top_1s_priority_or_high_whichever_is_more_urgent(
    top: Intent, route: Route
) -> None:
    # The top-1 at 1/2 (below tau), the rest spread evenly (1/14 each); sigma 1, so the safety
    # mass (at most 1/2 + 1/14) never reaches the floor.
    probabilities = {i: (0.5 if i is top else 0.5 / 7) for i in DECISION_ORDER}
    expected = RouteDecision(intent=None, route=route, decided_by="fail_closed", error="Transient")
    transient = next(e for e in every_gateway_error() if type(e).__name__ == "Transient")

    decision = await decide(
        probabilities,
        ScriptedFallback([transient]),
        thresholds=RouterThresholds(tau=0.75, sigma=1.0),
    )

    assert decision == expected


SPREAD: Final[Mapping[Intent, float]] = {i: (0.5 if i is PJ else 0.5 / 7) for i in DECISION_ORDER}
"""Top-1 professional_judgement at 1/2, the rest 1/14 each; under sigma 1 never the floor."""


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("probabilities", "script", "expected"),
    [
        (
            SPREAD,
            [Transient(status_code=529)],
            RouteDecision(intent=None, route=URGENT, decided_by="fail_closed", error="Transient"),
        ),
        (BAND, [PJ], by_llm(PJ, PJ_URGENT)),
        (SPREAD, [Q], kept(PJ, Q, PJ_URGENT)),
    ],
    ids=["fail_closed", "llm", "classifier_kept"],
)
async def test_a_tightened_template_keeps_its_urgency_on_every_step(
    probabilities: Mapping[Intent, float],
    script: list[FallbackStep],
    expected: RouteDecision,
) -> None:
    # PJ_URGENT makes professional_judgement urgent. Every priority, the fail-closed one
    # included, comes from `policy.route(...)`, never from the intent's name.
    decision = await decide(
        probabilities,
        ScriptedFallback(list(script)),
        policy=PJ_URGENT,
        thresholds=RouterThresholds(tau=0.75, sigma=1.0),
    )

    assert decision == expected
    assert decision.route == URGENT


# ── 5. Everything else propagates ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_cancel_propagates_never_a_hand_off() -> None:
    with pytest.raises(asyncio.CancelledError):
        await decide(BAND, ScriptedFallback([asyncio.CancelledError()]))


@pytest.mark.anyio
async def test_the_eval_caps_cap_reached_propagates_never_a_hand_off() -> None:
    # CapReached stops an eval run; it is not a GatewayError, so it is never a fail-closed row.
    cap = CapReached("cap $1.00 reached before the router call")

    with pytest.raises(CapReached) as raised:
        await decide(BAND, ScriptedFallback([cap]))

    assert raised.value is cap


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [ValueError("bug"), KeyError("bug"), ZeroDivisionError("bug"), TimeoutError()],
    ids=["ValueError", "KeyError", "ZeroDivisionError", "TimeoutError"],
)
async def test_any_other_exception_propagates_unchanged(error: Exception) -> None:
    # No RuntimeError case: the stub's NotImplementedError is one, so it would be caught here.
    with pytest.raises(type(error)) as raised:
        await decide(BAND, ScriptedFallback([error]))

    assert raised.value is error


# ── Quiet ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_route_message_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    transient = next(e for e in every_gateway_error() if type(e).__name__ == "Transient")

    await decide({E: 0.25, Q: 0.75}, ScriptedFallback())  # the floor
    await decide({Q: 0.75, OOS: 0.25}, ScriptedFallback())  # the classifier
    await decide(BAND, ScriptedFallback([E]))  # the LLM
    await decide(BAND, ScriptedFallback([transient]))  # fail closed

    assert caplog.records == []
