"""LLM-only routing, the replay fallback, and the hybrid rows (M4 PR 4).

**LLM mode** (`run_llm_rows`): every row goes to the router's fallback (`LLMIntentFallback`, alias
`small`, through the run's `CappedGateway`); its answer is the final intent and the route comes
from the business type's policy. A `GatewayError` is a fail-closed hand-off at `high` (no
classifier, so no top-1 to be more urgent than: "LLM-only mode follows the same rule"). The cap's
`CapReached` is never a hand-off: it stops the run, and the rows not reached are `not_run`.

**The replay fallback** (`ReplayFallback`): an `IntentFallback` that answers from a finished LLM
run's results: the recorded intent, or the recorded `GatewayError` class raised again
(`replay_error`). Calls are matched by trace id (`row_trace_id`, one per row), so the message text
is never needed to find the answer, and a call for a row the LLM run never reached raises
`ReplayError`, which is not a `GatewayError`: it propagates through H1 and `summarize` refuses.

**The hybrid** (`hybrid_rows`): the classifier run's calibrated probabilities and each row's text go
through H1's `route_message` itself with the replay fallback, so the measured hybrid *is* H1's
code, and costs $0 (plan: "the hybrid row replays the LLM-only answers", labelled "replayed").
Its latency is composed: the classifier's embedding and scoring, plus the replayed call's own
latency when the LLM was asked.

**The dry provider** (`DryRunRouterProvider`): a fresh `FakeProvider` per request, answering an
intent picked by a hash of the request: no network, no key, no cost, and a spread of answers so the
plumbing (tighten-only, discordant rows) is exercised. Its answers mean nothing.

Nothing here logs or stores a message's text; the gateway writes its metadata-only `llm_call`
records.
"""

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from vectapilot_api.embeddings.base import InputTooLarge, RateLimited
from vectapilot_api.evalrun.budget import CapReached, CostCap
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    DeadlineExceeded,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    SpendLimitReached,
    StructuredOutputInvalid,
    Transient,
)
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.intenteval.rows import EvalRow
from vectapilot_api.intenteval.runinfo import ResultLine, RunStatus
from vectapilot_api.intenteval.split import unit_key
from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds
from vectapilot_api.intents.fallback import IntentFallback
from vectapilot_api.intents.policy import Priority, RoutePolicy, handoff
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

FAIL_CLOSED_LLM_ONLY: Final = handoff(Priority.HIGH)
"""LLM-only mode's fail-closed route: a hand-off at high (no classifier top-1 to compare)."""


def row_trace_id(row_id: str) -> str:
    """The W3C trace id of a row's routing call: 32 hex characters from the row id, the same in the
    LLM run and in its replay (never all zeros: a SHA-256 prefix of 128 zero bits is not a
    practical case, and the gateway would refuse it before any call)."""
    return hashlib.sha256(f"intents-route:{row_id}".encode()).hexdigest()[:32]


def unit_of(row: EvalRow) -> str:
    return ":".join(unit_key(row))


def base_fields(row: EvalRow, policy: RoutePolicy) -> dict[str, Any]:
    """The fields every mode's result line shares (ids and labels, no text)."""
    return {
        "id": row.id,
        "business_id": row.business_id,
        "language_group": row.language_group,
        "unit": unit_of(row),
        "gold": row.intent,
        "safety": row.intent in policy.safety_intents,
    }


def not_run(row: EvalRow, policy: RoutePolicy) -> ResultLine:
    return ResultLine.model_validate(
        base_fields(row, policy)
        | {"decided_by": "not_run", "final_intent": None, "route": None, "priority": None}
    )


# ── LLM mode ─────────────────────────────────────────────────────────────────────────────────


@dataclass
class LlmRun:
    status: RunStatus
    """`complete`, `partial` when the cap or a stop rule ended it, `failed` on an error."""
    stop: str | None


async def run_llm_rows(
    rows: Sequence[EvalRow],
    fallback: IntentFallback,
    cap: CostCap,
    *,
    policies: Mapping[str, RoutePolicy],
    records: Callable[[str], CallRecord | None],
    clock: Callable[[], float],
    sink: Callable[[ResultLine], None],
    progress: Callable[[str], None] = lambda line: None,
) -> LlmRun:
    """Route every row by the LLM alone (module docstring), one line to `sink` per row, the rows
    a stop left unreached as `not_run`."""
    stop: str | None = None
    for index, row in enumerate(rows):
        policy = policies[row.business_id]
        trace = row_trace_id(row.id)
        started = clock()
        fields = base_fields(row, policy)
        try:
            intent = await fallback(row.text, business_id=row.business_id, trace_id=trace)
        except GatewayError as error:
            route = FAIL_CLOSED_LLM_ONLY
            fields |= {"decided_by": "fail_closed", "final_intent": None}
            fields |= {"error": type(error).__name__}
        except CapReached as reached:
            stop = str(reached)
            for left in rows[index:]:
                sink(not_run(left, policies[left.business_id]))
            break
        else:
            route = policy.route(intent)
            fields |= {"decided_by": "llm", "final_intent": intent, "llm_intent": intent}
        record = records(trace)
        sink(
            ResultLine.model_validate(
                fields
                | {
                    "route": route.kind,
                    "priority": route.priority,
                    "latency_ms": max(0.0, (clock() - started) * 1000),
                    "cost_usd": None if record is None else record.cost_usd,
                    "cost_complete": record is not None and record.cost_complete,
                }
            )
        )
        if (index + 1) % 20 == 0 or index + 1 == len(rows):
            progress(f"llm: {index + 1} of {len(rows)} rows")
        if cap.stop is not None:
            stop = cap.stop
            for left in rows[index + 1 :]:
                sink(not_run(left, policies[left.business_id]))
            break
    return LlmRun(status="complete" if stop is None else "partial", stop=stop)


class DryRunRouterProvider:
    """A `Provider` answering every routing request with a hashed intent, through a fresh fake."""

    def __init__(self) -> None:
        self.requests = 0

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        content = request.messages[-1].content if request.messages else ""
        pick = hashlib.sha256(content.encode("utf-8", "surrogatepass")).digest()[0]
        intent = DECISION_ORDER[pick % len(DECISION_ORDER)]
        fake = FakeProvider([FakeReply(text=json.dumps({"intent": intent.value}))])
        return await fake.send(request)

    async def aclose(self) -> None:
        """Nothing to release."""


# ── Replay ───────────────────────────────────────────────────────────────────────────────────


class ReplayError(Exception):
    """A replay cannot answer: the LLM run has no answer for this call. Not a `GatewayError`, so
    H1 never turns it into a hand-off. Ids only."""


_REPLAYABLE: Final[Mapping[str, Callable[[], GatewayError]]] = {
    "GatewayError": GatewayError,
    "SpendLimitReached": SpendLimitReached,
    "BillingError": BillingError,
    "Transient": Transient,
    "RateLimited": RateLimited,
    "AuthenticationFailed": AuthenticationFailed,
    "InvalidRequest": InvalidRequest,
    "InputTooLarge": InputTooLarge,
    "ProviderError": ProviderError,
    "DeadlineExceeded": lambda: DeadlineExceeded(deadline_s=1.0),
    "ModelRefused": ModelRefused,
    "OutputTruncated": lambda: OutputTruncated(stop_reason="max_tokens"),
    "StructuredOutputInvalid": lambda: StructuredOutputInvalid(problems=()),
}
"""Every `GatewayError` class in the package, by name (a test keeps this complete): a replay
raises the recorded class again; H1 records only the class name, never a value."""


def replay_error(name: str) -> GatewayError:
    factory = _REPLAYABLE.get(name)
    if factory is None:
        raise ReplayError(f"the LLM run recorded an error class this replay cannot raise: {name}")
    return factory()


class ReplayFallback:
    """An `IntentFallback` answering from an LLM run's results (module docstring)."""

    def __init__(self, lines: Sequence[ResultLine]) -> None:
        self._by_trace = {row_trace_id(line.id): line for line in lines}
        self.calls: list[str] = []
        """The row ids asked, in order."""

    async def __call__(self, message: str, *, business_id: str, trace_id: str) -> Intent:
        line = self._by_trace.get(trace_id)
        if line is None:
            raise ReplayError("the LLM run has no row for this trace id")
        self.calls.append(line.id)
        if line.business_id != business_id:
            raise ReplayError(f"row {line.id} belongs to another business in the LLM run")
        if line.decided_by == "fail_closed" and line.error is not None:
            raise replay_error(line.error)
        if line.llm_intent is None:
            raise ReplayError(f"the LLM run did not answer row {line.id} ({line.decided_by})")
        return line.llm_intent


# ── The hybrid ───────────────────────────────────────────────────────────────────────────────


class Router(Protocol):
    """`decide.route_message`'s signature (H1)."""

    def __call__(
        self,
        scores: IntentScores,
        message: str,
        policy: RoutePolicy,
        fallback: IntentFallback,
        *,
        thresholds: RouterThresholds,
        business_id: str,
        trace_id: str,
    ) -> Awaitable[RouteDecision]: ...


def _add(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


async def hybrid_rows(
    classifier: Sequence[ResultLine],
    llm: Sequence[ResultLine],
    rows: Mapping[str, EvalRow],
    policies: Mapping[str, RoutePolicy],
    thresholds: RouterThresholds,
    router: Router,
) -> list[ResultLine]:
    """Each classifier row through `router` with the replay of `llm` (module docstring). Raises
    `ReplayError` for a row the LLM run cannot answer, and whatever `router` raises
    (`NotImplementedError` while H1 is unwritten)."""
    replay = ReplayFallback(llm)
    by_id = {line.id: line for line in llm}
    lines: list[ResultLine] = []
    for line in classifier:
        if line.probabilities is None:
            raise ReplayError(f"classifier row {line.id} has no probabilities to replay")
        row = rows[line.id]
        policy = policies[row.business_id]
        scores = IntentScores(dict(zip(DECISION_ORDER, line.probabilities, strict=True)))
        decision = await router(
            scores,
            row.text,
            policy,
            replay,
            thresholds=thresholds,
            business_id=row.business_id,
            trace_id=row_trace_id(row.id),
        )
        asked = by_id[row.id] if decision.llm_called else None
        lines.append(
            ResultLine.model_validate(
                base_fields(row, policy)
                | {
                    "decided_by": decision.decided_by,
                    "final_intent": decision.intent,
                    "route": decision.route.kind,
                    "priority": decision.route.priority,
                    "llm_intent": decision.llm_intent,
                    "error": decision.error,
                    "top_probability": line.top_probability,
                    "top_probability_raw": line.top_probability_raw,
                    "classifier_intent": line.classifier_intent,
                    "probabilities": line.probabilities,
                    "latency_ms": line.latency_ms
                    if asked is None
                    else _add(line.latency_ms, asked.latency_ms),
                    "scoring_ms": line.scoring_ms,
                    "embedding_ms": line.embedding_ms,
                    "cost_usd": None if asked is None else asked.cost_usd,
                    "cost_complete": True if asked is None else asked.cost_complete,
                }
            )
        )
    return lines
