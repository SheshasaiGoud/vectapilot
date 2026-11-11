"""`ClassifierRouter` (D7 (a), M5 PR 4): how the API runs M4's router in front of the loop.

M4's `route_message` is shesha's H1 and still a stub. These tests never implement it: one
replaces it with a recorder that returns a fixed decision, to check what the router hands it (the
query vector's scores, the policy, the fallback, the thresholds, the business and trace ids);
another shows the real stub raising, which the service turns into a fail-closed hand-off
(tests/agent/test_service.py, `test_under_on_a_router_that_raises_fails_closed`).
"""

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest

from tests.embeddings.scripted import FAKE_SPEC, POLICY, FakeClock, ScriptedEmbedder
from tests.intenteval.harness_helpers import FakeRepo, Now, frozen_copy, real_model
from tests.intents.fakes import TRACE, ScriptedFallback
from vectapilot_api.agent import routing
from vectapilot_api.agent.routing import (
    ROUTE_OPERATION,
    ClassifierRouter,
    RouterModel,
    RouterProgress,
)
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.intenteval.model import read_model
from vectapilot_api.intents.classifier import ClassifierArtifact, IntentScores
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds
from vectapilot_api.intents.fallback import IntentFallback
from vectapilot_api.intents.policy import RoutePolicy, policy_for
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

pytestmark = pytest.mark.anyio

POLICY_HEALTH = policy_for("human_healthcare")
EMBEDDING_CALL = "embedding_call"


@dataclass
class RecordedRouteMessage:
    """Stands in for `route_message`: records its arguments, returns one fixed decision."""

    decision: RouteDecision
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        scores: IntentScores,
        message: str,
        policy: RoutePolicy,
        fallback: IntentFallback,
        *,
        thresholds: RouterThresholds,
        business_id: str,
        trace_id: str,
    ) -> RouteDecision:
        self.calls.append(
            {
                "scores": scores,
                "message": message,
                "policy": policy,
                "fallback": fallback,
                "thresholds": thresholds,
                "business_id": business_id,
                "trace_id": trace_id,
            }
        )
        return self.decision


@pytest.fixture
def model(tmp_path: Path) -> RouterModel:
    """A real M4 model folder (fake repository), its artifact relabelled for the fake embedder."""
    root = tmp_path.resolve()
    folder = real_model(FakeRepo(root), frozen_copy(root), Now())
    loaded = read_model(folder)
    artifact = loaded.artifact.model_copy(update={"embedding_model": FAKE_SPEC.model})
    assert artifact.dimension == FAKE_SPEC.dimension
    return RouterModel(
        artifact=artifact,
        artifact_sha256=loaded.artifact_sha256,
        thresholds=RouterThresholds(tau=loaded.thresholds.tau, sigma=loaded.thresholds.sigma),
    )


def runner() -> EmbeddingRunner:
    clock = FakeClock()
    embedder = ScriptedEmbedder()
    return EmbeddingRunner(
        embedder, FAKE_SPEC, POLICY, deadline_s=100.0, clock=clock, sleep=clock.sleep
    )


async def test_the_router_embeds_a_query_scores_it_and_hands_route_message_everything(
    model: RouterModel, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    decision = RouteDecision(
        intent=Intent.EMERGENCY,
        route=POLICY_HEALTH.route(Intent.EMERGENCY),
        decided_by="safety_floor",
    )
    recorder = RecordedRouteMessage(decision)
    monkeypatch.setattr(routing, "route_message", recorder)
    fallback = ScriptedFallback([])
    embeddings = runner()

    got = await ClassifierRouter(embeddings, model, fallback).route(
        "chest pain, book me tomorrow",
        policy=POLICY_HEALTH,
        business_id="sunrise-clinic",
        trace_id=TRACE,
        progress=RouterProgress(),
    )

    assert got is decision
    [call] = recorder.calls
    vector = (
        await embeddings.embed(
            ["chest pain, book me tomorrow"],
            input_type="query",
            business_id="sunrise-clinic",
            operation="test",
        )
    ).vectors[0]
    expected = model.artifact.score(vector, model=FAKE_SPEC.model, input_type="query")
    assert call == {
        "scores": expected,
        "message": "chest pain, book me tomorrow",
        "policy": POLICY_HEALTH,
        "fallback": fallback,
        "thresholds": model.thresholds,
        "business_id": "sunrise-clinic",
        "trace_id": TRACE,
    }
    embedding_calls = [
        getattr(r, EMBEDDING_CALL) for r in caplog.records if r.getMessage() == EMBEDDING_CALL
    ]
    assert embedding_calls[0]["operation"] == ROUTE_OPERATION
    assert embedding_calls[0]["input_type"] == "query"
    assert embedding_calls[0]["trace_id"] == TRACE


async def test_with_m4s_stub_the_router_raises_and_never_decides(model: RouterModel) -> None:
    router = ClassifierRouter(runner(), model, ScriptedFallback([]))

    with pytest.raises(NotImplementedError, match="H1 \\(M4\\)"):
        await router.route(
            "When do you open?",
            policy=POLICY_HEALTH,
            business_id="sunrise-clinic",
            trace_id=TRACE,
            progress=RouterProgress(),
        )


async def test_a_model_without_a_confidence_band_reaches_route_message_as_tau_none(
    model: RouterModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Decided 2026-11-09: tau None is "no band", never 1.0. load_router_model keeps it None
    # (tests/agent/test_startup.py); the router must hand it to route_message unchanged.
    no_band = replace(model, thresholds=RouterThresholds(tau=None, sigma=model.thresholds.sigma))
    decision = RouteDecision(
        intent=Intent.QUESTION,
        route=POLICY_HEALTH.route(Intent.QUESTION),
        decided_by="llm",
        llm_intent=Intent.QUESTION,
    )
    recorder = RecordedRouteMessage(decision)
    monkeypatch.setattr(routing, "route_message", recorder)

    await ClassifierRouter(runner(), no_band, ScriptedFallback([])).route(
        "When do you open?",
        policy=POLICY_HEALTH,
        business_id="sunrise-clinic",
        trace_id=TRACE,
        progress=RouterProgress(),
    )

    [call] = recorder.calls
    assert call["thresholds"] == RouterThresholds(tau=None, sigma=model.thresholds.sigma)
    assert call["thresholds"].tau is None


# ── RouterProgress: what the service needs if the router's own limit fires [router-timeout] ──


async def test_the_router_records_the_top1_before_route_message_runs(
    model: RouterModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The service reads `progress.top1` when its limit fires during route_message's fallback
    # call, so it must already be set when route_message starts (not only after it returns).
    progress = RouterProgress()
    seen: list[Intent | None] = []
    scored: list[IntentScores] = []

    async def recorder(scores: IntentScores, *_: Any, **__: Any) -> RouteDecision:
        seen.append(progress.top1)
        scored.append(scores)
        raise NotImplementedError("H1 (M4)")  # as the stub: the top-1 stays recorded

    monkeypatch.setattr(routing, "route_message", recorder)

    with pytest.raises(NotImplementedError):
        await ClassifierRouter(runner(), model, ScriptedFallback([])).route(
            "chest pain, book me tomorrow",
            policy=POLICY_HEALTH,
            business_id="sunrise-clinic",
            trace_id=TRACE,
            progress=progress,
        )

    [probabilities] = [s.probabilities for s in scored]
    # The most probable intent, ties to the first in DECISION_ORDER (computed independently).
    expected = min(DECISION_ORDER, key=lambda i: (-probabilities[i], DECISION_ORDER.index(i)))
    assert seen == [expected] and progress.top1 is expected


@dataclass(frozen=True)
class FixedScores:
    """An artifact stand-in that scores every vector the same (only `score` is used)."""

    probabilities: dict[Intent, float]

    def score(self, vector: Any, *, model: str, input_type: str) -> IntentScores:
        return IntentScores(self.probabilities)


@pytest.mark.parametrize(
    ("probabilities", "top1"),
    [
        # A tie goes to the earlier intent in DECISION_ORDER, as route_message's step 2.
        ({Intent.EMERGENCY: 0.4, Intent.QUESTION: 0.4, Intent.COMPLAINT: 0.2}, Intent.EMERGENCY),
        ({Intent.QUESTION: 0.4, Intent.COMPLAINT: 0.4, Intent.EMERGENCY: 0.2}, Intent.COMPLAINT),
        # The largest wins even when it comes last in DECISION_ORDER.
        (
            {Intent.OUT_OF_SCOPE: 0.5, Intent.EMERGENCY: 0.3, Intent.QUESTION: 0.2},
            Intent.OUT_OF_SCOPE,
        ),
    ],
)
async def test_the_recorded_top1_is_the_most_probable_ties_to_the_earlier(
    model: RouterModel,
    monkeypatch: pytest.MonkeyPatch,
    probabilities: dict[Intent, float],
    top1: Intent,
) -> None:
    full = {intent: probabilities.get(intent, 0.0) for intent in Intent}
    fixed = replace(model, artifact=cast(ClassifierArtifact, FixedScores(full)))
    monkeypatch.setattr(routing, "route_message", RecordedRouteMessage(_question_decision()))
    progress = RouterProgress()

    await ClassifierRouter(runner(), fixed, ScriptedFallback([])).route(
        "anything",
        policy=POLICY_HEALTH,
        business_id="sunrise-clinic",
        trace_id=TRACE,
        progress=progress,
    )

    assert progress.top1 is top1


def _question_decision() -> RouteDecision:
    return RouteDecision(
        intent=Intent.QUESTION, route=POLICY_HEALTH.route(Intent.QUESTION), decided_by="classifier"
    )
