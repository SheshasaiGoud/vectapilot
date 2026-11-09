"""H1 (M4) — the router's decision: safety floor, threshold, LLM fallback. [HAND-WRITE], written by
shesha (D4); time-box 2 h.

Guide: docs/learning/m4-router.md · Tests: tests/intents/test_decide.py (strict xfails until the
body is written) and tests/intents/test_fallback.py's end-to-end case. ADR 0015 decision 6.

Built by the team and tested (tests/intents/test_decide_types.py): `RouterThresholds`,
`DecidedBy` and `RouteDecision`. The body of `route_message` is shesha's.
"""

import math
from dataclasses import dataclass
from typing import Literal

from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.fallback import IntentFallback
from vectapilot_api.intents.policy import Priority, Route, RouteKind, RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

DecidedBy = Literal["safety_floor", "classifier", "llm", "classifier_kept", "fail_closed"]
"""Which step decided (the docstring of `route_message`)."""


@dataclass(frozen=True)
class RouterThresholds:
    """tau and sigma, chosen on dev by D8's rule (PR 4's `thresholds`), each in (0, 1].

    `tau`: the confidence threshold; a top probability at or above it is decided by the
    classifier. `sigma`: the safety floor; a safety mass at or above it is a hand-off to the
    most probable safety intent, checked before tau."""

    tau: float
    sigma: float

    def __post_init__(self) -> None:
        for name in ("tau", "sigma"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, float | int):
                raise ValueError(f"{name} must be a number")
            if not (math.isfinite(value) and 0 < value <= 1):
                raise ValueError(f"{name} must lie in (0, 1]")


@dataclass(frozen=True)
class RouteDecision:
    """Where one message goes, and why. Checked when built, so an impossible decision cannot
    exist (a fail-closed decision with an intent, a kept classifier intent the LLM did not
    loosen, ...).

    - `intent`: the final intent; None only when `decided_by` is `fail_closed`.
    - `route`: from the business type's policy (`policy.route(intent)`), or for `fail_closed` a
      hand-off at `urgent` or `high`.
    - `llm_intent`: the LLM's own answer when it was called and answered, else None.
    - `error`: the `GatewayError` class name when `fail_closed` (never its text), else None.
    """

    intent: Intent | None
    route: Route
    decided_by: DecidedBy
    llm_intent: Intent | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        problem = self._problem()
        if problem:
            raise ValueError(f"impossible route decision: {problem}")

    def _problem(self) -> str | None:
        by = self.decided_by
        if by == "fail_closed":
            if self.intent is not None or self.llm_intent is not None:
                return "a fail-closed decision has no intent and no LLM answer"
            if not self.error:
                return "a fail-closed decision names its GatewayError class"
            if self.route.kind is not RouteKind.HANDOFF or self.route.priority not in (
                Priority.URGENT,
                Priority.HIGH,
            ):
                return "a fail-closed decision is a hand-off at urgent or high"
            return None
        if self.intent is None:
            return f"decided_by {by} needs an intent"
        if self.error is not None:
            return "only a fail-closed decision names an error"
        if by in ("safety_floor", "classifier"):
            if self.llm_intent is not None:
                return f"decided_by {by} means no LLM call"
            return None
        if self.llm_intent is None:
            return f"decided_by {by} needs the LLM's answer"
        position = DECISION_ORDER.index
        if by == "llm" and self.intent != self.llm_intent:
            return "decided_by llm means the final intent is the LLM's"
        if by == "classifier_kept" and position(self.llm_intent) <= position(self.intent):
            return "classifier_kept means the LLM's intent comes later in the decision order"
        return None

    @property
    def llm_called(self) -> bool:
        return self.decided_by in ("llm", "classifier_kept", "fail_closed")


async def route_message(
    scores: IntentScores,
    message: str,
    policy: RoutePolicy,
    fallback: IntentFallback,
    *,
    thresholds: RouterThresholds,
    business_id: str,
    trace_id: str,
) -> RouteDecision:
    """Decide where `message` goes from the classifier's `scores`, calling `fallback` at most once.

    Contract (ADR 0015 decision 6; the plan's "H1"):

    1. **Safety floor.** The safety mass is the sum of the probabilities of `policy`'s safety
       intents (`policy.safety_intents`: emergency and professional judgement for a health type,
       emergency only for the default template). If it is at least `thresholds.sigma`, return the
       most probable safety intent (ties: the first in `DECISION_ORDER`) with
       `decided_by="safety_floor"`. No fallback call. Checked before τ, always.
    2. **Threshold.** Otherwise the classifier's top-1 is the most probable intent, ties broken
       by `DECISION_ORDER` (the earlier wins). If its probability is at least `thresholds.tau`,
       return it with `decided_by="classifier"`. No fallback call.
    3. **Fallback.** Otherwise await `fallback(message, business_id=business_id,
       trace_id=trace_id)` exactly once, with the arguments unchanged. The final intent is
       whichever of the LLM's intent and the classifier's top-1 comes first in `DECISION_ORDER`:
       the LLM may add an escalation, never remove one (tighten, never loosen). `decided_by` is
       `"classifier_kept"` when the LLM's intent comes strictly later than the top-1 (the LLM
       would have loosened the route), else `"llm"` (it agreed or tightened). `llm_intent` is
       the LLM's answer either way.
    4. **Fail closed.** If the fallback raises any `GatewayError` (any subclass), return a
       hand-off with no intent (`decided_by="fail_closed"`, `error` = the exception's class
       name), at the top-1's route priority or `high`, whichever is more urgent: `urgent` if the
       top-1's route is an urgent hand-off, else `high` (a `rag` route or a `normal` hand-off
       becomes `high`).
    5. **Everything else propagates unchanged:** `asyncio.CancelledError`, the eval cap's
       `CapReached` (not a `GatewayError`) and any other exception (a bug) are never caught.

    The route of a decided intent is `policy.route(intent)`, never a table of your own. No
    logging (the message is customer text, and the gateway already records the call's
    metadata), no clock, no randomness; `scores` is never changed.
    """
    raise NotImplementedError("H1 (M4): write route_message (guide: docs/learning/m4-router.md)")
