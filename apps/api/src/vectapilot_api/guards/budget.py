"""`BudgetedGateway`: every paid call a guarded route makes, reserved on its business's day's budget
first (M7 PR 5; G9, D4 (a)). `gateway/` and `agent/llm.py` are unchanged.

**A subclass, as `CappedGateway` is** (`evalrun/budget.py`): the services take a concrete
`LLMGateway`, so the seam overrides its two entry points, `complete` and `complete_structured`
(G13). Before the call it reserves the worst case of this exact request, M1's formula
(`measure.budget.request_reservation`) with this gateway's retry and repair limits, keyed by the
request's `business_id` (`Limiter.reserve`). A refusal raises `GuardRefused` and the provider is
never called; a refused call writes no `llm_call` record, so the `guard` record counts it
(`BudgetTally.refused`).

**The settle** (`Limiter.settle`, on the key captured at reservation):
- a result with a complete cost counts its `cost_usd`, rounded up to micro-USD;
- a result with an incomplete cost counts `max(reservation, cost)` (its cost is a lower bound);
- any exception, a cancel included, keeps the whole reservation (a `GatewayError` carries no
  cost), with no Redis call;
- a settle that fails is swallowed: the reservation then stays counted, and a settle never
  replaces the call's own result or exception.

An alias missing from the catalog has no reservation: the call goes to `LLMGateway`, which
rejects it before any provider call (and writes its pre-call `llm_call` record).

**Per request**, the route opens `track_budget()` (PR 6): every `BudgetedGateway` reached inside
it adds to the same `BudgetTally` (a context variable, copied into the tasks the request starts),
so a turn's reservations sum across the `small` step gateway and the `large` answer gateway, and
the `guard` record carries `reserved_micro_usd`, `settled_micro_usd` and `budget_refused`.

**Builders.** `build_budgeted_agent_gateway` mirrors `agent.llm.build_agent_gateway` argument for
argument (a test pins it), and `build_budgeted_gateway` mirrors `gateway.build_gateway` (the
agent's own `AnswerService`, `large`), each plus the limiter. Nothing here imports `evalrun`:
`prompt_bytes`' three lines are copied, and a test pins equal output.
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic import BaseModel

from vectapilot_api.agent.prompt import AGENT_ALIAS
from vectapilot_api.config import Settings
from vectapilot_api.gateway.catalog import Catalog, load_catalog
from vectapilot_api.gateway.gateway import LLMGateway, StructuredResult, build_provider
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import LLMRequest, LLMResult, RetryPolicy
from vectapilot_api.guards.limits import GuardRefused, Limiter, Reserved, micro_usd
from vectapilot_api.measure.budget import request_reservation


@dataclass
class BudgetTally:
    """One request's budget, as the `guard` record states it (G10)."""

    reserved_micro_usd: int = 0
    settled_micro_usd: int = 0
    refused: int = 0


_TALLY: ContextVar[BudgetTally | None] = ContextVar("vectapilot_budget_tally", default=None)


@contextmanager
def track_budget() -> Iterator[BudgetTally]:
    """Count every budgeted call made inside this block into one fresh tally (module docstring)."""
    tally = BudgetTally()
    token = _TALLY.set(tally)
    try:
        yield tally
    finally:
        _TALLY.reset(token)


def _tally() -> BudgetTally:
    return _TALLY.get() or BudgetTally()  # outside a tracked request: counted nowhere


def prompt_bytes(request: LLMRequest) -> int:
    """The UTF-8 bytes of a request's own text: the system prompt and every message. Copied from
    `evalrun.budget.prompt_bytes` (G9: no import from `evalrun`); a test pins equal output."""
    system = len(request.system.encode("utf-8")) if request.system is not None else 0
    return system + sum(len(message.content.encode("utf-8")) for message in request.messages)


class BudgetedGateway(LLMGateway):
    """The gateway, with each call's worst case reserved on its business's day's budget first
    (module docstring)."""

    def __init__(
        self,
        provider: Provider,
        catalog: Catalog,
        *,
        attempt_timeout_s: float,
        deadline_s: float,
        retry_policy: RetryPolicy,
        max_repairs: int,
        limiter: Limiter,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            provider,
            catalog,
            attempt_timeout_s=attempt_timeout_s,
            deadline_s=deadline_s,
            retry_policy=retry_policy,
            max_repairs=max_repairs,
            clock=clock,
            sleep=sleep,
            rand=rand,
        )
        self._limiter = limiter

    def reservation_micro_usd(self, request: LLMRequest, schema: type[BaseModel] | None) -> int:
        """The worst case of this exact request (M1's formula), in micro-USD rounded up. Raises
        `KeyError` for an alias the catalog lacks."""
        return micro_usd(
            request_reservation(
                case_id=request.trace_id or "untraced",
                alias_name=request.alias,
                prompt_bytes=prompt_bytes(request),
                schema=schema,
                catalog=self._catalog,
                max_retries=self._retry_policy.max_retries,
                max_repairs=self._max_repairs,
            ).usd
        )

    async def complete(self, request: LLMRequest) -> LLMResult:
        reserved = await self._reserve(request, None)
        if reserved is None:
            return await super().complete(request)
        try:
            result = await super().complete(request)
        except BaseException:
            _tally().settled_micro_usd += reserved.micro_usd  # it may have been billed: keep it
            raise
        await self._settle(reserved, result)
        return result

    async def complete_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredResult[T]:
        reserved = await self._reserve(request, schema)
        if reserved is None:
            return await super().complete_structured(request, schema)
        try:
            structured = await super().complete_structured(request, schema)
        except BaseException:
            _tally().settled_micro_usd += reserved.micro_usd
            raise
        await self._settle(reserved, structured.result)
        return structured

    async def _reserve(
        self, request: LLMRequest, schema: type[BaseModel] | None
    ) -> Reserved | None:
        """The reservation, or None for an alias the catalog lacks (the base rejects it before
        any provider call). Raises `GuardRefused`: the call is not made."""
        if request.alias not in self._catalog.aliases:
            return None
        amount = self.reservation_micro_usd(request, schema)
        tally = _tally()
        try:
            reserved = await self._limiter.reserve(request.business_id, amount)
        except GuardRefused:
            tally.refused += 1
            raise
        tally.reserved_micro_usd += reserved.micro_usd
        return reserved

    async def _settle(self, reserved: Reserved, result: LLMResult) -> None:
        """Count a finished call (module docstring). Never raises a guard's error: the result is
        already the customer's."""
        cost = micro_usd(result.cost_usd)
        spent = cost if result.cost_complete else max(reserved.micro_usd, cost)
        counted = spent
        try:
            await self._limiter.settle(reserved, spent)
        except Exception:
            counted = reserved.micro_usd  # the reservation stays on the day's key
        _tally().settled_micro_usd += counted


@asynccontextmanager
async def build_budgeted_agent_gateway(
    settings: Settings, *, provider: Provider | None = None, limiter: Limiter
) -> AsyncIterator[BudgetedGateway]:
    """`agent.llm.build_agent_gateway`, argument for argument, as a `BudgetedGateway` on
    `limiter`; closes its provider on exit."""
    catalog = load_catalog()
    if AGENT_ALIAS not in catalog.aliases:
        raise ValueError(f"the model catalog has no {AGENT_ALIAS!r} alias for agent steps")
    retry_policy = settings.llm_retry_policy()
    gateway = BudgetedGateway(
        build_provider(settings) if provider is None else provider,
        catalog,
        attempt_timeout_s=settings.agent_llm_attempt_timeout_seconds,
        deadline_s=settings.agent_llm_deadline_seconds,
        retry_policy=retry_policy,
        max_repairs=settings.llm_max_repairs,
        limiter=limiter,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()


@asynccontextmanager
async def build_budgeted_gateway(
    settings: Settings, *, provider: Provider | None = None, limiter: Limiter
) -> AsyncIterator[BudgetedGateway]:
    """`gateway.build_gateway`, argument for argument, as a `BudgetedGateway` on `limiter` (the
    agent's own `AnswerService`, PR 6); closes its provider on exit."""
    catalog = load_catalog()
    retry_policy = settings.llm_retry_policy()
    gateway = BudgetedGateway(
        build_provider(settings) if provider is None else provider,
        catalog,
        attempt_timeout_s=settings.llm_attempt_timeout_seconds,
        deadline_s=settings.llm_deadline_seconds,
        retry_policy=retry_policy,
        max_repairs=settings.llm_max_repairs,
        limiter=limiter,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
