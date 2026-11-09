"""H1 scaffold: the types around `route_message`, built by the team (these pass now).

`RouterThresholds` and `RouteDecision` check themselves when built, so H1's tests can build every
expected decision before the call, and an impossible decision cannot leave `route_message`.
"""

import inspect
import math

import pytest

import vectapilot_api.embeddings.base  # noqa: F401 (registers its GatewayError subclasses)
from tests.intents.fakes import every_gateway_error, package_gateway_error_classes
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds, route_message
from vectapilot_api.intents.policy import RAG, Priority, handoff
from vectapilot_api.intents.taxonomy import Intent

URGENT, HIGH, NORMAL = handoff(Priority.URGENT), handoff(Priority.HIGH), handoff(Priority.NORMAL)


def test_thresholds_take_values_in_0_1() -> None:
    assert RouterThresholds(tau=1.0, sigma=0.01) == RouterThresholds(tau=1, sigma=0.01)


@pytest.mark.parametrize("value", [0.0, -0.1, 1.5, math.nan, math.inf, True])
@pytest.mark.parametrize("name", ["tau", "sigma"])
def test_thresholds_outside_0_1_are_refused(name: str, value: float) -> None:
    good = {"tau": 0.8, "sigma": 0.2}
    with pytest.raises(ValueError, match=name):
        RouterThresholds(**(good | {name: value}))


@pytest.mark.parametrize(
    "decision",
    [
        RouteDecision(intent=Intent.EMERGENCY, route=URGENT, decided_by="safety_floor"),
        RouteDecision(intent=Intent.QUESTION, route=RAG, decided_by="classifier"),
        RouteDecision(
            intent=Intent.EMERGENCY, route=URGENT, decided_by="llm", llm_intent=Intent.EMERGENCY
        ),
        RouteDecision(
            intent=Intent.COMPLAINT,
            route=HIGH,
            decided_by="classifier_kept",
            llm_intent=Intent.QUESTION,
        ),
        RouteDecision(intent=None, route=HIGH, decided_by="fail_closed", error="Transient"),
        RouteDecision(intent=None, route=URGENT, decided_by="fail_closed", error="ModelRefused"),
    ],
    ids=["floor", "classifier", "llm", "kept", "fail closed high", "fail closed urgent"],
)
def test_possible_decisions_build(decision: RouteDecision) -> None:
    assert decision.llm_called == (decision.decided_by in ("llm", "classifier_kept", "fail_closed"))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"intent": Intent.QUESTION, "route": HIGH, "decided_by": "fail_closed", "error": "X"},
            "no intent",
        ),
        ({"intent": None, "route": HIGH, "decided_by": "fail_closed"}, "names its GatewayError"),
        (
            {"intent": None, "route": NORMAL, "decided_by": "fail_closed", "error": "Transient"},
            "urgent or high",
        ),
        (
            {"intent": None, "route": RAG, "decided_by": "fail_closed", "error": "Transient"},
            "urgent or high",
        ),
        ({"intent": None, "route": RAG, "decided_by": "classifier"}, "needs an intent"),
        (
            {"intent": Intent.QUESTION, "route": RAG, "decided_by": "classifier", "error": "X"},
            "only a fail-closed",
        ),
        (
            {
                "intent": Intent.QUESTION,
                "route": RAG,
                "decided_by": "safety_floor",
                "llm_intent": Intent.QUESTION,
            },
            "no LLM call",
        ),
        ({"intent": Intent.QUESTION, "route": RAG, "decided_by": "llm"}, "LLM's answer"),
        (
            {
                "intent": Intent.QUESTION,
                "route": RAG,
                "decided_by": "llm",
                "llm_intent": Intent.EMERGENCY,
            },
            "the final intent is the LLM's",
        ),
        (
            {
                "intent": Intent.QUESTION,
                "route": RAG,
                "decided_by": "classifier_kept",
                "llm_intent": Intent.EMERGENCY,
            },
            "comes later",
        ),
        (
            {
                "intent": Intent.QUESTION,
                "route": RAG,
                "decided_by": "classifier_kept",
                "llm_intent": Intent.QUESTION,
            },
            "comes later",
        ),
    ],
    ids=[
        "fail closed with intent",
        "fail closed without error",
        "fail closed normal",
        "fail closed rag",
        "classifier without intent",
        "error without fail closed",
        "floor with llm",
        "llm without answer",
        "llm not final",
        "kept but llm tighter",
        "kept but agreeing",
    ],
)
def test_impossible_decisions_are_refused(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        RouteDecision(**fields)  # type: ignore[arg-type]


def test_h1s_tests_cover_every_gateway_error_class_in_the_package() -> None:
    # A new GatewayError kind must get its own fail-closed case (tests/intents/fakes.py).
    covered = {type(error) for error in every_gateway_error()}
    assert package_gateway_error_classes() <= covered


def test_route_message_has_the_signature_pr_4_calls() -> None:
    parameters = inspect.signature(route_message).parameters
    assert list(parameters) == [
        "scores",
        "message",
        "policy",
        "fallback",
        "thresholds",
        "business_id",
        "trace_id",
    ]
    assert [p.kind for p in parameters.values()][4:] == [inspect.Parameter.KEYWORD_ONLY] * 3
    assert inspect.iscoroutinefunction(route_message)
