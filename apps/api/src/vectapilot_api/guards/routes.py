"""Limits on the confirm route and `POST /v1/extractions` (M7 PR 6; G3, G9, G10, G15).

Neither route has a hand-off, so its guard in PR 6 is the limiter alone: `RouteLimits.admit`,
built in `create_app` and called by the route as an object (G13: no middleware, no dependency
runs it), before the route's service and outside it.

- **The confirm route** (G15): business → body → this pre-check → `ConfirmService.confirm`. The
  business's window and the customer's (`customer_ref`, client-asserted until M8). It is counted
  before the call, so a 429 can never follow a write. Its `Retry-After` is capped below
  `action_lifetime_seconds` (`max_retry_after_s`, G15), so a 429 alone never outlasts a fresh
  action; an action near its expiry may still expire first (the pre-check never reads the row,
  W6), and a confirm that committed returns its stored result whatever its expiry (W5, W9).
  Nothing encloses the confirm call: no guard scope, timeout or exception mapping
  (a cancelled confirm is never audited, and confirm's own backstop sits inside its
  transaction), and no masking or H3 runs on its response: 200, 409, 404 and 503 bodies pass byte
  for byte.
- **`/v1/extractions`** (PR 6): the business's window only (an upload names no customer), checked
  before `read_body`, so a limited caller never costs a body read or a PDF parse. PR 7 adds the
  document screen after the extraction.

**Responses.** None: the route goes on. A full window: a 429 with `Retry-After` whose body carries
the business's guidance for a health business (G3, `limits.refusal_decision`). Redis down, a
timeout or any other failure of the check (G3: fail closed): a typed 503 `limits_unavailable`,
`{detail, error}`, distinct from confirm's `confirm_failed` and `outcome_unknown` and never a
stored extraction; no guidance (PR 5's `GuardDecision`: guidance goes only with a hand-off or a
429). The route adds `X-Trace-Id`.

**One metadata-only `guard` record per request** (G10), written here, before the route's service
runs: the route, `limits` with its duration, the decision, a `no_guidance` finding when a health
business's 429 has no lines to show, and the exception's class name for a 503.

**Health and guidance** follow `/v1/agent`'s rule (`guards.agent`): a health business by either
source (its guard file, or the type the route serves it as: `tools.json`, the extraction file),
an unresolved guard file counting as health (G4); the guard file's guidance lines, or
`tools.json`'s where the file has none.
"""

import contextlib
import time
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Final

from fastapi.responses import JSONResponse

from vectapilot_api.guards.config import GuardConfig
from vectapilot_api.guards.limits import (
    GuardRefused,
    Limiter,
    RefusalKind,
    limited_response,
    refusal_decision,
)
from vectapilot_api.guards.types import (
    UNCHANGED,
    Check,
    DecidedBy,
    Finding,
    GuardAction,
    GuardDecision,
    GuardRecord,
    RouteSlug,
    emit_guard_record,
)
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType

LIMITS_UNAVAILABLE: Final = "limits_unavailable"
LIMITS_UNAVAILABLE_DETAIL: Final = (
    "the request's limits cannot be checked right now; nothing was done: try again shortly"
)


def unavailable_response() -> JSONResponse:
    """The typed 503 (module docstring): a fixed body, as `kb.errors` writes a typed error."""
    return JSONResponse(
        {"detail": LIMITS_UNAVAILABLE_DETAIL, "error": LIMITS_UNAVAILABLE}, status_code=503
    )


class RouteLimits:
    """The limits-only guard of one route without a hand-off (module docstring)."""

    def __init__(
        self,
        *,
        route: RouteSlug,
        config: GuardConfig | None,
        limiter: Limiter,
        types: Mapping[str, BusinessType],
        guidance: Mapping[str, tuple[str, ...]],
        max_retry_after_s: int | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        """`types` is the type each business is served as (`tools.json`, the extraction file);
        `guidance` its `tools.json` lines. `max_retry_after_s` caps a 429's `Retry-After`."""
        if max_retry_after_s is not None and max_retry_after_s < 1:
            raise ValueError("a Retry-After cap is at least one second")
        self._route = route
        self._config = config
        self._limiter = limiter
        self._types = types
        self._guidance = guidance
        self._max_retry_after_s = max_retry_after_s
        self._clock = clock

    def health_and_guidance(self, business_id: str) -> tuple[bool, tuple[str, ...]]:
        """Whether the business is a health type, and its guidance lines (module docstring)."""
        guards = None if self._config is None else self._config.for_business(business_id)
        served = self._types.get(business_id)
        health = bool(guards is not None and guards.health) or served in HEALTH_TYPES
        guidance = () if guards is None else guards.guidance
        return health, guidance or self._guidance.get(business_id, ())

    async def admit(
        self, business_id: str, *, trace_id: str, customer_ref: str | None = None
    ) -> JSONResponse | None:
        """None when the request may go on; else the 429 or the typed 503 (module docstring).
        Never raises but a cancel."""
        started = self._clock()
        refused: GuardRefused | None = None
        error_type: str | None = None
        try:
            await self._limiter.admit(business_id, self._route, customer_ref)
        except GuardRefused as caught:
            refused = caught
        except Exception as error:  # G3: fail closed; a cancel is not an Exception
            error_type = type(error).__name__
        elapsed = max(0.0, (self._clock() - started) * 1000)
        findings: Counter[Finding] = Counter()
        response: JSONResponse | None = None
        decision = UNCHANGED
        if refused is not None and refused.kind is RefusalKind.LIMIT:
            health, lines = self.health_and_guidance(business_id)
            decision = refusal_decision(refused, health=health, handoff=False)
            shown = lines if decision.guidance else ()
            if decision.guidance and not shown:
                findings[Finding.NO_GUIDANCE] += 1
            response = limited_response(self._capped(refused), shown)
        elif refused is not None or error_type is not None:
            if refused is not None:
                decision = refusal_decision(refused, health=False, handoff=False)
                error_type = refused.error_type
            else:
                decision = GuardDecision(GuardAction.UNAVAILABLE, DecidedBy.GUARD_ERROR)
            response = unavailable_response()
        with contextlib.suppress(Exception):  # the record never replaces the response
            self._emit(business_id, trace_id, decision, findings, error_type, elapsed)
        return response

    def _capped(self, refused: GuardRefused) -> GuardRefused:
        """`refused` with its `Retry-After` at most the cap (G15)."""
        cap, retry = self._max_retry_after_s, refused.retry_after_s
        if cap is None or retry is None or retry <= cap:
            return refused
        return GuardRefused(RefusalKind.LIMIT, retry_after_s=cap)

    def _emit(
        self,
        business_id: str,
        trace_id: str,
        decision: GuardDecision,
        findings: Counter[Finding],
        error_type: str | None,
        elapsed_ms: float,
    ) -> None:
        emit_guard_record(
            GuardRecord(
                business_id=business_id,
                trace_id=trace_id,
                route=self._route,
                rules_version=None if self._config is None else self._config.rules_version,
                decision=decision,
                checks=(Check.LIMITS,),
                findings=dict(findings),
                error_type=error_type,
                durations_ms={Check.LIMITS: elapsed_ms},
            )
        )
