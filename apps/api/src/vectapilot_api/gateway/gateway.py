"""The LLM gateway: `LLMGateway.complete` is the one path to a model (ADR 0006, ADR 0007).

One logical call, in this order, and every call writes exactly one `llm_call` record:

1. The alias is resolved in the catalog. An unknown alias is a caller bug: `InvalidRequest` before
   any model call; its record has no `model_requested` (decided 2026-10-15).
2. A `max_tokens` above the alias's maximum is `InvalidRequest` before any model call, never
   clamped (decided 2026-10-15).
3. H2's `call_with_retries` runs inside `asyncio.timeout(deadline)`, each attempt inside
   `asyncio.timeout(attempt timeout)`; an attempt that times out is a `Transient` (counted in
   `attempt_timeouts`), so H2 may retry it. When a limit fires, its scope cancels the task and, on
   exit, turns that `CancelledError` into `TimeoutError` only if no other cancel is pending
   (asyncio's cancel count), so a caller's cancel always wins and propagates. `.expired()` then
   tells a scope's own `TimeoutError` from one some code raised itself (a bug, never retried),
   at both levels.
4. The stop reason decides (decided 2026-10-15): `end_turn` is an `LLMResult`; `refusal` is
   `ModelRefused`; `max_tokens` and `model_context_window_exceeded` are `OutputTruncated`;
   `tool_use`, `pause_turn`, `stop_sequence` and `other` (M1 never asks for them) are a
   non-retryable `ProviderError`. None of them is retried, repaired or cached.
5. In `finally`: the one `llm_call` log line, metadata only (`CallRecord`), also on error,
   deadline and cancel. Building or emitting it can never replace the call's result or exception.
   A caller's own timeout around `complete()` is recorded as `cancelled`; only the gateway's
   deadline is `deadline_exceeded`.

Cost (ADR 0006, Cost): H1's `cost_usd` for each attempt that got a response, priced by the
*answering* model's exact catalog entry, summed. `cost_complete` turns False (`cost_usd` is then a
lower bound) when the answering model is unpriced, a response's `inference_geo` is not `global`
(None included), an attempt timed out or was cancelled, or the provider raised a `Transient` or
`ProviderError` without an HTTP status (the connection was lost, or a 2xx could not be read: maybe
billed, tokens unknown). An error the gateway raises itself about a response it read (an odd stop
reason, a refusal, a truncation) does not by itself make the cost incomplete: that response's
usage is known (the unpriced-model and geo rules still apply).

The gateway logs nothing but the `llm_call` line, and never puts provider text into an error: every
error it raises is built from typed fields, outside any `except` block, so nothing is chained.
"""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from vectapilot_api.config import Settings
from vectapilot_api.gateway.catalog import Alias, Catalog, load_catalog
from vectapilot_api.gateway.errors import (
    DeadlineExceeded,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    Transient,
)
from vectapilot_api.gateway.pricing import cost_usd
from vectapilot_api.gateway.providers.anthropic import AnthropicProvider
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.retry import call_with_retries
from vectapilot_api.gateway.types import (
    CallOutcome,
    CallRecord,
    LLMRequest,
    LLMResult,
    RetryPolicy,
    Usage,
)

LLM_CALL: Final = "llm_call"
"""The log message of the one record per call, and the key that holds the record (ADR 0007)."""

logger = logging.getLogger("vectapilot_api.gateway")
# Pinned at INFO: a quieter `log_level` (WARNING, ERROR) must never drop the `llm_call` records
# (ADR 0007: every model call is visible). The root handler has no level of its own.
logger.setLevel(logging.INFO)


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(**{name: getattr(a, name) + getattr(b, name) for name in Usage.model_fields})


@dataclass
class _Tally:
    """What the attempts of one call left behind. Only the `attempt` closure in `complete` writes
    it (per-attempt accounting, so H2's signature never has to change)."""

    attempts: int = 0
    attempt_timeouts: int = 0
    usage: Usage = field(default_factory=Usage)
    """Summed over every attempt that got a response."""
    cost_usd: Decimal = field(default_factory=Decimal)
    cost_complete: bool = True
    request_ids: list[str] = field(default_factory=list)
    last: ProviderResponse | None = None
    """The last response (model answered, geo, stop reason); None if no attempt got one."""

    def answered(self, response: ProviderResponse, catalog: Catalog) -> None:
        self.last = response
        self.usage = _add(self.usage, response.usage)
        if response.request_id is not None:
            self.request_ids.append(response.request_id)
        pricing = catalog.models.get(response.model)  # the answering model, never the alias's
        if pricing is None:
            self.cost_complete = False  # billed at a price we do not know: never a silent $0
        else:
            self.cost_usd += cost_usd(response.usage, pricing)
        if response.inference_geo != "global":
            self.cost_complete = False  # prices assume global routing; None is unknown

    def failed(self, error: GatewayError) -> None:
        if error.request_id is not None:
            self.request_ids.append(error.request_id)
        if error.status_code is None and isinstance(error, Transient | ProviderError):
            self.cost_complete = False  # maybe billed (lost connection, unreadable 2xx)


class LLMGateway:
    """Calls a model through `provider`, with the catalog's aliases and the time and retry limits
    from Settings (`build_gateway`). `clock`, `sleep` and `rand` are for tests; production uses
    `time.monotonic`, `asyncio.sleep` and `random.random`."""

    def __init__(
        self,
        provider: Provider,
        catalog: Catalog,
        *,
        attempt_timeout_s: float,
        deadline_s: float,
        retry_policy: RetryPolicy,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._provider = provider
        self._catalog = catalog
        self._attempt_timeout_s = attempt_timeout_s
        self._deadline_s = deadline_s
        self._retry_policy = retry_policy
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def aclose(self) -> None:
        """Close the provider this gateway was given."""
        await self._provider.aclose()

    async def complete(self, request: LLMRequest) -> LLMResult:
        """Make one logical call; return the result or raise a typed `GatewayError`."""
        started = self._clock()
        tally = _Tally()
        alias = self._catalog.aliases.get(request.alias)
        outcome = CallOutcome.PROVIDER_ERROR  # what an unexpected exception (a bug) is recorded as
        try:
            if alias is None:
                raise InvalidRequest()  # a caller bug, recorded like any other pre-call rejection
            if request.max_tokens is not None and request.max_tokens > alias.max_output_tokens:
                raise InvalidRequest()
            response = await self._call(request, alias, tally, started)
            result = self._result(response, tally)
            outcome = CallOutcome.OK
            return result
        except GatewayError as error:
            outcome = error.outcome
            raise
        except asyncio.CancelledError:  # caught only to record, then re-raised
            outcome = CallOutcome.CANCELLED
            raise
        finally:
            # A failure here must never replace the call's own result or exception.
            with contextlib.suppress(Exception):
                self._emit(request, alias, tally, outcome, started)

    async def _call(
        self, request: LLMRequest, alias: Alias, tally: _Tally, started: float
    ) -> ProviderResponse:
        max_tokens = alias.max_output_tokens if request.max_tokens is None else request.max_tokens
        provider_request = ProviderRequest(
            model=alias.model,
            effort=alias.effort,
            max_tokens=max_tokens,
            system=request.system,
            messages=request.messages,
        )

        async def attempt() -> ProviderResponse:
            tally.attempts += 1
            answer: ProviderResponse | None = None
            limit = asyncio.timeout(self._attempt_timeout_s)
            try:
                async with limit:
                    answer = await self._provider.send(provider_request)
            except TimeoutError:
                tally.cost_complete = False  # it may have been sent, and billed
                if not limit.expired():
                    raise  # not this attempt's limit: a bug, never retried (provider_error)
                tally.attempt_timeouts += 1
            except GatewayError as error:
                tally.failed(error)
                raise
            except BaseException:
                # Cancelled (the deadline or the caller) or a bug, mid-attempt: maybe billed.
                # Recorded only, then re-raised unchanged.
                tally.cost_complete = False
                raise
            if answer is None:
                raise Transient()  # outside the `except` block: nothing is chained
            tally.answered(answer, self._catalog)
            return answer

        deadline = asyncio.timeout(self._deadline_s)
        response: ProviderResponse | None = None
        try:
            async with deadline:
                response = await call_with_retries(
                    attempt,
                    self._retry_policy,
                    deadline=started + self._deadline_s,
                    clock=self._clock,
                    sleep=self._sleep,
                    rand=self._rand,
                )
        except TimeoutError:
            if not deadline.expired():
                raise  # not the deadline's: a bug, recorded as provider_error
        if response is None:
            raise DeadlineExceeded(deadline_s=self._deadline_s)
        return response

    @staticmethod
    def _result(response: ProviderResponse, tally: _Tally) -> LLMResult:
        match response.stop_reason:
            case "end_turn":
                return LLMResult(
                    content=response.content,
                    stop_reason=response.stop_reason,
                    usage=tally.usage,
                    model_answered=response.model,
                    cost_usd=tally.cost_usd,
                    cost_complete=tally.cost_complete,
                )
            case "refusal":
                raise ModelRefused(request_id=response.request_id)
            case "max_tokens" | "model_context_window_exceeded" as truncation:
                raise OutputTruncated(stop_reason=truncation, request_id=response.request_id)
            case _:  # tool_use, pause_turn, stop_sequence, other: never asked for in M1
                raise ProviderError(request_id=response.request_id)

    def _emit(
        self,
        request: LLMRequest,
        alias: Alias | None,
        tally: _Tally,
        outcome: CallOutcome,
        started: float,
    ) -> None:
        last = tally.last
        record = CallRecord(
            business_id=request.business_id,
            alias=request.alias,
            model_requested=None if alias is None else alias.model,
            model_answered=None if last is None else last.model,
            catalog_version=self._catalog.catalog_version,
            inference_geo=None if last is None else last.inference_geo,
            outcome=outcome,
            stop_reason=None if last is None else last.stop_reason,
            retries=max(tally.attempts - 1, 0),  # no repairs before PR 7
            repairs=0,
            attempt_timeouts=tally.attempt_timeouts,
            **tally.usage.model_dump(),
            cost_usd=tally.cost_usd,
            cost_complete=tally.cost_complete,
            cache_hit=False,
            latency_ms=max(0.0, (self._clock() - started) * 1000),
            request_ids=tuple(tally.request_ids),
            trace_id=request.trace_id,
            operation=request.operation,
        )
        logger.info(LLM_CALL, extra={LLM_CALL: record.model_dump(mode="json")})


def build_provider(settings: Settings) -> Provider:
    """The one factory that turns Settings into a provider (decided 2026-10-15).

    The key is unwrapped only inside `AnthropicProvider`'s constructor; its SDK timeout is the
    attempt timeout plus `SDK_TIMEOUT_MARGIN_S` (1 s), a backstop below the gateway's own limit.
    """
    if settings.llm_provider == "fake":
        return FakeProvider()
    key = settings.anthropic_api_key
    if key is None:  # Settings refuses this; checked again so it can never pass silently
        raise ValueError("anthropic_api_key is required when llm_provider is 'anthropic'")
    return AnthropicProvider(api_key=key, attempt_timeout_s=settings.llm_attempt_timeout_seconds)


@asynccontextmanager
async def build_gateway(settings: Settings) -> AsyncIterator[LLMGateway]:
    """Yield a gateway built from Settings and the packaged catalog; close its provider on exit."""
    # Everything that can fail comes first; the provider (an HTTP client) last, so it is always
    # closed once it exists.
    catalog = load_catalog()
    retry_policy = settings.llm_retry_policy()
    gateway = LLMGateway(
        build_provider(settings),
        catalog,
        attempt_timeout_s=settings.llm_attempt_timeout_seconds,
        deadline_s=settings.llm_deadline_seconds,
        retry_policy=retry_policy,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
