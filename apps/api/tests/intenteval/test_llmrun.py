"""LLM mode, the replay fallback and the hybrid rows (M4 PR 4). Fakes only: a scripted fallback,
the real gateway on a `FakeProvider`, made-up rows (`TEST-ONLY` texts). H1 is shesha's: the
hybrid's plumbing is tested with `ask_always` (deliberately not H1), and with H1 itself as a strict
xfail."""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import pytest
from pydantic import TypeAdapter

import vectapilot_api.embeddings.base  # noqa: F401 (registers its GatewayError subclasses)
from tests.intenteval.harness_helpers import ask_always
from tests.intenteval.helpers import eval_row
from tests.intents.fakes import (
    ScriptedFallback,
    gateway_for,
    intent_reply,
    package_gateway_error_classes,
)
from vectapilot_api.evalrun.budget import CapReached, CostCap
from vectapilot_api.gateway.errors import AuthenticationFailed, ModelRefused, Transient
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply, FakeStep
from vectapilot_api.gateway.types import TraceId
from vectapilot_api.intenteval.llmrun import (
    DryRunRouterProvider,
    LlmRun,
    ReplayError,
    ReplayFallback,
    base_fields,
    hybrid_rows,
    replay_error,
    row_trace_id,
    run_llm_rows,
)
from vectapilot_api.intenteval.rows import EvalRow
from vectapilot_api.intenteval.runinfo import ResultLine
from vectapilot_api.intenteval.runs import FAIL_CLOSED_ON_TEST, status_on_split
from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds, route_message
from vectapilot_api.intents.fallback import IntentFallback, LLMIntentFallback
from vectapilot_api.intents.policy import Priority, RouteKind, RoutePolicy, policy_for
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

E, Q, C = Intent.EMERGENCY, Intent.QUESTION, Intent.COMPLAINT
POLICIES: Final = {
    "sunrise-clinic": policy_for("human_healthcare"),
    "paws-and-co": policy_for("veterinary"),
}
ROWS: Final = (
    eval_row(id="example-r1", intent=Q),
    eval_row(id="example-r2", intent=E),
    eval_row(id="example-r3", intent=E, business_id="paws-and-co"),
    eval_row(id="example-r4", intent=C),
    eval_row(id="example-r5", intent=Q),
)


class Collect:
    def __init__(self) -> None:
        self.lines: list[ResultLine] = []

    def __call__(self, line: ResultLine) -> None:
        self.lines.append(line)


def ticking() -> object:
    t = [0.0]

    def clock() -> float:
        t[0] += 0.25
        return t[0]

    return clock


def test_a_rows_trace_id_is_a_w3c_trace_id_fixed_by_its_row() -> None:
    ids = [row_trace_id(r.id) for r in ROWS]
    assert ids == [row_trace_id(r.id) for r in ROWS]
    assert len(set(ids)) == len(ids)
    assert [TypeAdapter[str](TraceId).validate_python(i) for i in ids] == ids


async def run(
    rows: Sequence[EvalRow], script: list[Intent | BaseException], cap: CostCap | None = None
) -> tuple[list[ResultLine], str, str | None, ScriptedFallback]:
    fallback = ScriptedFallback(list(script))
    sink = Collect()
    outcome = await run_llm_rows(
        rows,
        fallback,
        cap or CostCap(cap=None),
        policies=POLICIES,
        records=lambda trace: None,
        clock=ticking(),  # type: ignore[arg-type]
        sink=sink,
    )
    return sink.lines, outcome.status, outcome.stop, fallback


@pytest.mark.anyio
async def test_llm_mode_routes_by_the_answer_and_fails_closed_at_high() -> None:
    lines, status, stop, fallback = await run(ROWS[:3], [Q, Transient(), C])

    assert (status, stop) == ("complete", None)
    assert [(x.decided_by, x.final_intent, x.llm_intent, x.error) for x in lines] == [
        ("llm", Q, Q, None),
        ("fail_closed", None, None, "Transient"),
        ("llm", C, C, None),
    ]
    assert [(x.route, x.priority) for x in lines] == [
        (RouteKind.RAG, None),
        (RouteKind.HANDOFF, Priority.HIGH),  # no classifier: high
        (RouteKind.HANDOFF, Priority.HIGH),
    ]
    assert [x.safety for x in lines] == [False, True, True]
    assert all(x.latency_ms == 250.0 and x.cost_usd is None for x in lines)
    assert not any(x.cost_complete for x in lines)  # no record: never a complete cost
    # Each call carries the row's own business and trace id; the text is only passed through.
    assert [(b, t) for _, b, t in fallback.calls] == [
        (r.business_id, row_trace_id(r.id)) for r in ROWS[:3]
    ]


@pytest.mark.anyio
async def test_cap_reached_stops_the_run_and_is_never_a_hand_off() -> None:
    lines, status, stop, _ = await run(ROWS, [Q, CapReached("TEST-ONLY: not sent")])

    assert status == "partial" and stop == "TEST-ONLY: not sent"
    assert [(x.id, x.decided_by) for x in lines] == [
        ("example-r1", "llm"),
        ("example-r2", "not_run"),
        ("example-r3", "not_run"),
        ("example-r4", "not_run"),
        ("example-r5", "not_run"),
    ]
    assert all(x.route is None and x.final_intent is None for x in lines[1:])


@pytest.mark.anyio
async def test_a_cap_stop_rule_ends_the_run_after_the_current_row() -> None:
    cap = CostCap(cap=None)
    cap.stop_after("TEST-ONLY stop rule")
    lines, status, stop, _ = await run(ROWS[:3], [Q], cap)

    assert (status, stop) == ("partial", "TEST-ONLY stop rule")
    assert [x.decided_by for x in lines] == ["llm", "not_run", "not_run"]


def test_every_gateway_error_class_can_be_replayed() -> None:
    for cls in package_gateway_error_classes():
        assert type(replay_error(cls.__name__)) is cls
    with pytest.raises(ReplayError, match="cannot raise: KeyError"):
        replay_error("KeyError")


def llm_line(row: EvalRow, intent: Intent | None, error: str | None = None) -> ResultLine:
    by = "not_run" if intent is None and error is None else ("fail_closed" if error else "llm")
    route = None if by == "not_run" else (RouteKind.HANDOFF if error else RouteKind.RAG)
    return ResultLine.model_validate(
        base_fields(row, POLICIES[row.business_id])
        | {
            "decided_by": by,
            "final_intent": intent,
            "llm_intent": intent,
            "error": error,
            "route": route,
            "priority": Priority.HIGH if error else None,
        }
    )


@pytest.mark.anyio
async def test_the_replay_answers_from_the_recorded_run() -> None:
    r1, r2, r3 = ROWS[:3]
    recorded = [llm_line(r1, Q), llm_line(r2, None, "ModelRefused"), llm_line(r3, None)]
    replay = ReplayFallback(recorded)

    assert await replay("TEST-ONLY", business_id=r1.business_id, trace_id=row_trace_id(r1.id)) is Q
    with pytest.raises(ModelRefused):
        await replay("TEST-ONLY", business_id=r2.business_id, trace_id=row_trace_id(r2.id))
    with pytest.raises(ReplayError, match="did not answer row example-r3"):
        await replay("TEST-ONLY", business_id=r3.business_id, trace_id=row_trace_id(r3.id))
    with pytest.raises(ReplayError, match="another business"):
        await replay("TEST-ONLY", business_id="paws-and-co", trace_id=row_trace_id(r1.id))
    with pytest.raises(ReplayError, match="no row for this trace id"):
        await replay("TEST-ONLY", business_id=r1.business_id, trace_id=row_trace_id("other"))
    assert replay.calls == ["example-r1", "example-r2", "example-r3", "example-r1"]


def classifier_line(row: EvalRow, top: Intent) -> ResultLine:
    probabilities = tuple(0.5 if i is top else 1 / 14 for i in DECISION_ORDER)
    route = POLICIES[row.business_id].route(top)
    return ResultLine.model_validate(
        base_fields(row, POLICIES[row.business_id])
        | {
            "decided_by": "classifier",
            "final_intent": top,
            "classifier_intent": top,
            "route": route.kind,
            "priority": route.priority,
            "top_probability": 0.5,
            "top_probability_raw": 0.4,
            "probabilities": probabilities,
            "latency_ms": 7.0,
            "scoring_ms": 1.0,
            "embedding_ms": 6.0,
        }
    )


SCRIPT: Final[list[FakeStep]] = [
    FakeReply(text=intent_reply("question")),
    FakeReply(text=intent_reply("emergency")),
    AuthenticationFailed(status_code=401),
    FakeReply(text=intent_reply("complaint")),
    FakeReply(text=intent_reply("out_of_scope")),
]
THRESHOLDS: Final = RouterThresholds(tau=0.9, sigma=0.5)


@pytest.mark.anyio
async def test_replay_and_a_fake_provider_run_decide_identically() -> None:
    # The LLM-only run through the real gateway on a scripted provider...
    sink = Collect()
    await run_llm_rows(
        ROWS,
        LLMIntentFallback(gateway_for(FakeProvider(SCRIPT))),
        CostCap(cap=None),
        policies=POLICIES,
        records=lambda trace: None,
        clock=ticking(),  # type: ignore[arg-type]
        sink=sink,
    )
    classifier = [classifier_line(r, Q) for r in ROWS]
    rows = {r.id: r for r in ROWS}
    # ...replayed through the router, against the same router calling the same provider live.
    replayed = await hybrid_rows(classifier, sink.lines, rows, POLICIES, THRESHOLDS, ask_always)
    live = LLMIntentFallback(gateway_for(FakeProvider(SCRIPT)))
    direct = []
    for line in classifier:
        row = rows[line.id]
        scores = IntentScores(dict(zip(DECISION_ORDER, line.probabilities or (), strict=True)))
        direct.append(
            await ask_always(
                scores,
                row.text,
                POLICIES[row.business_id],
                live,
                thresholds=THRESHOLDS,
                business_id=row.business_id,
                trace_id=row_trace_id(row.id),
            )
        )

    assert [(x.final_intent, x.decided_by, x.llm_intent, x.error) for x in replayed] == [
        (d.intent, d.decided_by, d.llm_intent, d.error) for d in direct
    ]
    assert [(x.route, x.priority) for x in replayed] == [
        (d.route.kind, d.route.priority) for d in direct
    ]
    assert replayed[2].error == "AuthenticationFailed"
    # Composed latency: the classifier's 7 ms plus the replayed call's 250 ms.
    assert {x.latency_ms for x in replayed} == {257.0}


async def never_asks(
    scores: IntentScores,
    message: str,
    policy: RoutePolicy,
    fallback: IntentFallback,
    *,
    thresholds: RouterThresholds,
    business_id: str,
    trace_id: str,
) -> RouteDecision:
    """A stand-in router (not H1) that never asks the fallback: the top-1 decides."""
    top = max(DECISION_ORDER, key=lambda i: scores.probabilities[i])
    return RouteDecision(intent=top, route=policy.route(top), decided_by="classifier")


@pytest.mark.anyio
async def test_the_hybrid_carries_the_replayed_calls_cost_and_nothing_else() -> None:
    classifier = [classifier_line(r, Q) for r in ROWS[:2]]
    costs = [(Decimal("0.0012"), True), (Decimal("0.0034"), False)]
    llm = [
        llm_line(r, Q).model_copy(update={"cost_usd": usd, "cost_complete": complete})
        for r, (usd, complete) in zip(ROWS[:2], costs, strict=True)
    ]
    rows = {r.id: r for r in ROWS}

    asked = await hybrid_rows(classifier, llm, rows, POLICIES, THRESHOLDS, ask_always)
    kept = await hybrid_rows(classifier, llm, rows, POLICIES, THRESHOLDS, never_asks)

    assert [(x.cost_usd, x.cost_complete) for x in asked] == costs
    assert [(x.cost_usd, x.cost_complete) for x in kept] == [(None, True), (None, True)]
    assert [x.latency_ms for x in kept] == [7.0, 7.0]  # the classifier's own latency only


@pytest.mark.anyio
async def test_a_hybrid_needing_an_unreached_row_is_refused() -> None:
    classifier = [classifier_line(r, Q) for r in ROWS[:2]]
    llm = [llm_line(ROWS[0], Q), llm_line(ROWS[1], None)]

    with pytest.raises(ReplayError, match="did not answer row example-r2"):
        await hybrid_rows(
            classifier, llm, {r.id: r for r in ROWS}, POLICIES, THRESHOLDS, ask_always
        )


@pytest.mark.anyio
@pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H1: shesha writes route_message — see docs/learning/m4-router.md",
)
async def test_the_hybrid_is_h1_itself() -> None:
    # Every row below tau with no safety mass above sigma: H1 asks the replay once per row, and
    # the hybrid's decisions are H1's (here the LLM never loosens: its answers equal the top-1).
    classifier = [classifier_line(r, Q) for r in ROWS]
    llm = [llm_line(r, Q) for r in ROWS]

    lines = await hybrid_rows(
        classifier, llm, {r.id: r for r in ROWS}, POLICIES, THRESHOLDS, route_message
    )

    assert [x.decided_by for x in lines] == ["llm"] * len(ROWS)


@pytest.mark.anyio
async def test_the_dry_provider_answers_an_intent_through_the_real_gateway() -> None:
    provider = DryRunRouterProvider()
    fallback = LLMIntentFallback(gateway_for(provider))

    answers = [
        await fallback(r.text, business_id=r.business_id, trace_id=row_trace_id(r.id)) for r in ROWS
    ]

    assert all(isinstance(a, Intent) for a in answers) and provider.requests == len(ROWS)
    again = LLMIntentFallback(gateway_for(DryRunRouterProvider()))
    assert answers[0] is await again(
        ROWS[0].text, business_id=ROWS[0].business_id, trace_id=row_trace_id(ROWS[0].id)
    )


def test_a_fail_closed_row_on_test_makes_the_run_partial_and_on_dev_does_not() -> None:
    lines = [llm_line(ROWS[0], Q), llm_line(ROWS[1], None, "Transient")]
    complete = LlmRun(status="complete", stop=None)

    assert status_on_split(complete, lines, "dev") == complete
    assert status_on_split(complete, lines, "test") == LlmRun("partial", f"1 {FAIL_CLOSED_ON_TEST}")
    assert status_on_split(complete, lines[:1], "test") == complete
    stopped = LlmRun(status="partial", stop="TEST-ONLY cap")
    assert status_on_split(stopped, lines, "test") == stopped  # the first reason stays
