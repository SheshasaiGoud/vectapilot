"""The LLM gateway: `LLMGateway.complete` and `complete_structured` are the one path to a model
(ADR 0006, ADR 0007).

One logical call, in this order, and every call writes exactly one `llm_call` record:

1. The alias is resolved in the catalog. An unknown alias is a caller bug: `InvalidRequest` before
   any model call; its record has no `model_requested` (decided 2026-10-15).
2. A `max_tokens` above the alias's maximum is `InvalidRequest` before any model call, never
   clamped (decided 2026-10-15).
3. Everything that follows until the answer is ready runs inside `asyncio.timeout(deadline)`: the
   cache read, every attempt, every backoff wait and the repair.
4. With a response cache (off by default; `cache.py`), a valid entry of this business is returned
   without a model call: $0, `cache_hit`, re-validated against the schema. A cache error or an
   invalid entry is a miss.
5. H2's `call_with_retries` runs the attempts, each inside `asyncio.timeout(attempt timeout)`; an
   attempt that times out is a `Transient` (counted in `attempt_timeouts`), so H2 may retry it.
   When a limit fires, its scope cancels the task and, on exit, turns that `CancelledError` into
   `TimeoutError` only if no other cancel is pending (asyncio's cancel count), so a caller's cancel
   always wins and propagates. `.expired()` then tells a scope's own `TimeoutError` from one some
   code raised itself (a bug, never retried), at both levels.
6. The stop reason decides (decided 2026-10-15): `end_turn` is an `LLMResult`; `refusal` is
   `ModelRefused`; `max_tokens` and `model_context_window_exceeded` are `OutputTruncated`;
   `tool_use`, `pause_turn`, `stop_sequence` and `other` (M1 never asks for them) are a
   non-retryable `ProviderError`. None of them is retried, repaired or cached.
7. Structured calls only: the answer's text is validated against the caller's schema. If it fails,
   at most one repair (`max_repairs`), inside the same deadline and with the same retries: a fresh
   request, the original messages plus one feedback message that names only schema field locations
   and error types (`_problems`), never the output; no assistant or thinking block is replayed.
   Still invalid: `StructuredOutputInvalid` (`validation_failed`).
8. A success is written to the cache, cut off at the deadline and dropped on any error.
9. In `finally`: the one `llm_call` log line, metadata only (`CallRecord`), also on error,
   deadline and cancel. Building or emitting it can never replace the call's result or exception.
   A caller's own timeout around a call is recorded as `cancelled`; only the gateway's deadline is
   `deadline_exceeded`. An exception that is not a `GatewayError` (a bug) propagates unchanged and
   is recorded as `internal_error`.

Cost (ADR 0006, Cost): H1's `cost_usd` for each attempt that got a response, the repair's
included, priced by the *answering* model's exact catalog entry, summed. `cost_complete` turns False
(`cost_usd` is then a lower bound) when an answering model is unpriced, a response's
`inference_geo` is not `global` (None included), an attempt timed out or was cancelled, or the
provider raised a `Transient` or `ProviderError` without an HTTP status (the connection was lost, or
a 2xx could not be read: maybe billed, tokens unknown). An error the gateway raises itself about a
response it read (an odd stop reason, a refusal, a truncation, invalid structured output) does not
by itself make the cost incomplete: that response's usage is known.

The gateway logs nothing but the `llm_call` line, and never puts provider text into an error: every
error it raises is built from typed fields, outside any `except` block, so nothing is chained.
"""

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, cast, get_args

from pydantic import BaseModel, ValidationError
from pydantic_core.core_schema import ErrorType

from vectapilot_api.config import Settings
from vectapilot_api.gateway.cache import CachedResponse, ResponseCache, cache_key
from vectapilot_api.gateway.catalog import Alias, Catalog, load_catalog
from vectapilot_api.gateway.errors import (
    DeadlineExceeded,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    StructuredOutputInvalid,
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
    Message,
    RetryPolicy,
    StopReason,
    Usage,
)

LLM_CALL: Final = "llm_call"
"""The log message of the one record per call, and the key that holds the record (ADR 0007)."""

MAX_PROBLEMS: Final = 5
"""At most this many `(location, error type)` pairs in a repair's feedback and in the error."""

ROOT: Final = "(root)"
"""The location of a problem with the JSON object as a whole (invalid JSON, an unexpected key)."""

_KNOWN_ERROR_TYPES: Final[frozenset[str]] = frozenset(get_args(ErrorType))
"""Pydantic's own error types, a closed list (pydantic-core's `ErrorType`). Any other type (a
schema's custom one, which could be built from the output) is reported as `invalid`."""

logger = logging.getLogger("vectapilot_api.gateway")
# Pinned at INFO: a quieter `log_level` (WARNING, ERROR) must never drop the `llm_call` records
# (ADR 0007: every model call is visible). The root handler has no level of its own.
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    """A structured call's answer: `value`, validated against the caller's schema, and `result`
    (usage, cost, the model that answered; its `text` is the JSON `value` came from)."""

    value: T = field(repr=False)
    result: LLMResult


# ── Structured output: the instruction, validation and the repair's feedback ────────────────────


@dataclass(frozen=True)
class _Schema[T: BaseModel]:
    """A caller's schema, prepared once per call."""

    model: type[T]
    text: str
    """Its JSON Schema as canonical JSON (sorted keys, no spaces): the instruction quotes it."""
    names: frozenset[str]
    """Every property name in the JSON Schema: the only text a problem's location may contain."""

    @property
    def identity(self) -> str:
        """What the cache key hashes for this schema: its qualified name and its JSON Schema."""
        return f"{self.model.__module__}.{self.model.__qualname__}:{self.text}"

    def instruct(self, system: str | None) -> str:
        """The caller's system prompt plus the output instruction (ADR 0006: no native format)."""
        instruction = (
            "Answer with only a JSON object that matches this JSON Schema, and no other text:\n"
            + self.text
        )
        return instruction if system is None else f"{system}\n\n{instruction}"

    def validate(self, text: str) -> tuple[T | None, tuple[tuple[str, str], ...]]:
        """The validated value and no problems, or None and the problems (never the text).

        Only a `ValidationError` is caught. A schema validator that raises anything but
        `ValueError`, `AssertionError` or `PydanticCustomError` (which Pydantic turns into one)
        propagates unchanged (`internal_error`), with whatever its message holds.
        """
        try:
            value = self.model.model_validate_json(text)
        except ValidationError as error:  # never logged, chained or turned into a string
            return None, _problems(error, self.names)
        return value, ()


def _prepare[T: BaseModel](model: type[T]) -> _Schema[T]:
    json_schema = model.model_json_schema()
    text = json.dumps(json_schema, sort_keys=True, separators=(",", ":"))
    return _Schema(model, text, frozenset(_property_names(json_schema)))


def _property_names(node: object) -> Iterator[str]:
    """Every key of every `properties` object in a JSON Schema, nested models (`$defs`) included."""
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            yield from properties
        for child in node.values():
            yield from _property_names(child)
    elif isinstance(node, list):
        for child in node:
            yield from _property_names(child)


def _problems(error: ValidationError, names: frozenset[str]) -> tuple[tuple[str, str], ...]:
    """At most `MAX_PROBLEMS` distinct `(location, error type)` pairs, in Pydantic's order.

    Only schema vocabulary gets through: each error's input, message and context are never read,
    and an error type outside Pydantic's own list is `invalid`. Pydantic's `loc` mixes the schema's
    field names with text from the output: dict keys and unexpected keys (a numeric dict key arrives
    as a string, too) and union member labels. So an unexpected key is reported at its parent
    object (`(root)` at the top level), list positions are skipped, and the first part that is not a
    schema field name ends the location. A dict key equal to some schema field name stays in it
    (`scores.note`): text the system prompt's schema already holds, nothing else.
    """
    found: list[tuple[str, str]] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        kind = item["type"] if item["type"] in _KNOWN_ERROR_TYPES else "invalid"
        problem = (_location(item["loc"], kind, names), kind)
        if problem not in found:
            found.append(problem)
    return tuple(found[:MAX_PROBLEMS])


def _location(loc: tuple[int | str, ...], kind: str, names: frozenset[str]) -> str:
    if kind == "extra_forbidden":
        loc = loc[:-1]  # the last part is the unexpected key itself: always the model's text
    parts: list[str] = []
    for part in loc:
        if isinstance(part, int):
            continue  # a position in a list: not a field
        if part not in names:
            break  # a key the model wrote, or a union member label: never shown
        parts.append(part)
    return ".".join(parts) or ROOT


def _feedback(problems: tuple[tuple[str, str], ...]) -> str:
    """The repair's one extra user message: deterministic, schema locations and error types only."""
    lines = "".join(f"\n- {location}: {kind}" for location, kind in problems)
    return (
        "A previous answer to this request did not match the JSON Schema. Problems (location:"
        f" error type):{lines}\nAnswer again with only a JSON object that matches the schema."
    )


# ── One call's bookkeeping ───────────────────────────────────────────────────────────────────


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(**{name: getattr(a, name) + getattr(b, name) for name in Usage.model_fields})


@dataclass
class _Tally:
    """What one call left behind, for its record. Only the gateway writes it: per-attempt
    accounting happens in the `attempt` closure, so H2's signature never has to change."""

    attempts: int = 0
    attempt_timeouts: int = 0
    repairs: int = 0
    first_try_valid: bool | None = None
    usage: Usage = field(default_factory=Usage)
    """Summed over every attempt that got a response."""
    cost_usd: Decimal = field(default_factory=Decimal)
    cost_complete: bool = True
    request_ids: list[str] = field(default_factory=list)
    model_answered: str | None = None
    stop_reason: StopReason | None = None
    inference_geo: str | None = None
    """The last response's model, stop reason and geo (a cache hit's model and stop reason)."""
    cache_hit: bool = False
    cache_failed: bool = False
    """The cache raised on `get`: this call then writes nothing to it (fails open)."""

    def answered(self, response: ProviderResponse, catalog: Catalog) -> None:
        self.model_answered = response.model
        self.stop_reason = response.stop_reason
        self.inference_geo = response.inference_geo
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

    def served(self, entry: CachedResponse) -> None:
        self.cache_hit = True
        self.model_answered = entry.model_answered
        self.stop_reason = entry.stop_reason


# ── The gateway ──────────────────────────────────────────────────────────────────────────────


class LLMGateway:
    """Calls a model through `provider`, with the catalog's aliases and the time, retry and repair
    limits from Settings (`build_gateway`). `cache` is the response-cache hook: None (the default)
    means no caching. `clock`, `sleep` and `rand` are for tests; production uses `time.monotonic`,
    `asyncio.sleep` and `random.random`."""

    def __init__(
        self,
        provider: Provider,
        catalog: Catalog,
        *,
        attempt_timeout_s: float,
        deadline_s: float,
        retry_policy: RetryPolicy,
        max_repairs: int,
        cache: ResponseCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._provider = provider
        self._catalog = catalog
        self._attempt_timeout_s = attempt_timeout_s
        self._deadline_s = deadline_s
        self._retry_policy = retry_policy
        self._max_repairs = max_repairs
        """0 turns the repair off; the gateway never makes more than one (ADR 0006)."""
        self._cache = cache
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    async def aclose(self) -> None:
        """Close the provider this gateway was given."""
        await self._provider.aclose()

    async def complete(self, request: LLMRequest) -> LLMResult:
        """Make one logical call; return the result or raise a typed `GatewayError`."""
        result, _ = await self._run(request, None)
        return result

    async def complete_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredResult[T]:
        """Make one logical call whose answer must be a JSON object valid for `schema`.

        The system prompt gains one instruction: answer with only a JSON object for the schema,
        which it quotes. An answer that does not validate gets at most one repair; if that fails
        too, `StructuredOutputInvalid` names the problems (schema locations and error types only).
        Other failures raise as in `complete`.

        For schema authors: the schema's docstring and field descriptions are part of the prompt
        (and of the cache key), so write them for the model. Validators raise only `ValueError`,
        `AssertionError` or `PydanticCustomError`; any other exception propagates unchanged
        (`internal_error`), so it must never carry the input in its message.
        """
        result, value = await self._run(request, schema)
        return StructuredResult(value=cast(T, value), result=result)  # never None with a schema

    async def _run[T: BaseModel](
        self, request: LLMRequest, schema: type[T] | None
    ) -> tuple[LLMResult, T | None]:
        started = self._clock()
        tally = _Tally()
        alias = self._catalog.aliases.get(request.alias)
        outcome = CallOutcome.INTERNAL_ERROR  # what an exception that is not a GatewayError records
        try:
            if alias is None:
                raise InvalidRequest()  # a caller bug, recorded like any other pre-call rejection
            if request.max_tokens is not None and request.max_tokens > alias.max_output_tokens:
                raise InvalidRequest()
            spec = None if schema is None else _prepare(schema)
            provider_request = ProviderRequest(
                model=alias.model,
                effort=alias.effort,
                max_tokens=alias.max_output_tokens
                if request.max_tokens is None
                else request.max_tokens,
                system=request.system if spec is None else spec.instruct(request.system),
                messages=request.messages,
            )
            slot = None
            if self._cache is not None:
                key = cache_key(
                    business_id=request.business_id,
                    catalog_version=self._catalog.catalog_version,
                    request=provider_request,
                    schema=None if spec is None else spec.identity,
                )
                slot = (self._cache, key)
            deadline = asyncio.timeout(self._deadline_s)
            answer: tuple[LLMResult, T | None] | None = None
            try:
                async with deadline:
                    answer = await self._answer(
                        request.business_id, provider_request, spec, slot, tally, started
                    )
            except TimeoutError:
                if not deadline.expired():
                    raise  # not the deadline's: a bug (internal_error)
            if answer is None:
                raise DeadlineExceeded(deadline_s=self._deadline_s)
            if slot is not None and not (tally.cache_hit or tally.cache_failed):
                await _store(slot, request.business_id, answer[0], deadline.when())
            outcome = CallOutcome.OK
            return answer
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

    async def _answer[T: BaseModel](
        self,
        business_id: str,
        provider_request: ProviderRequest,
        spec: _Schema[T] | None,
        slot: tuple[ResponseCache, str] | None,
        tally: _Tally,
        started: float,
    ) -> tuple[LLMResult, T | None]:
        if slot is not None:
            hit = await _cached(slot, business_id, spec, tally)
            if hit is not None:
                return hit
        response = await self._attempts(provider_request, tally, started)
        result = self._result(response, tally)
        if spec is None:
            return result, None
        value, problems = spec.validate(result.text)
        tally.first_try_valid = value is not None
        if value is None and self._max_repairs > 0:
            # A fresh request: the original messages plus the feedback. Nothing of the invalid
            # answer is replayed (no assistant turn, no thinking block, no key or value from it).
            feedback = Message(role="user", content=_feedback(problems))
            repair = provider_request.model_copy(
                update={"messages": (*provider_request.messages, feedback)}
            )
            # Directly before the attempt, nothing in between: the record requires attempts =
            # 1 + repairs + retries, so a repair counted without its attempt would be dropped.
            tally.repairs = 1
            response = await self._attempts(repair, tally, started)
            result = self._result(response, tally)  # a refusal or truncation here is raised too
            value, problems = spec.validate(result.text)
        if value is None:
            raise StructuredOutputInvalid(problems=problems, request_id=response.request_id)
        return result, value

    async def _attempts(
        self, provider_request: ProviderRequest, tally: _Tally, started: float
    ) -> ProviderResponse:
        """One request (the first, or the repair), retried by H2 within the call's deadline."""

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
                    raise  # not this attempt's limit: a bug, never retried (internal_error)
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

        return await call_with_retries(
            attempt,
            self._retry_policy,
            deadline=started + self._deadline_s,
            clock=self._clock,
            sleep=self._sleep,
            rand=self._rand,
        )

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
        record = CallRecord(
            business_id=request.business_id,
            alias=request.alias,
            model_requested=None if alias is None else alias.model,
            model_answered=tally.model_answered,
            catalog_version=self._catalog.catalog_version,
            inference_geo=tally.inference_geo,
            outcome=outcome,
            stop_reason=tally.stop_reason,
            attempts=tally.attempts,
            retries=max(tally.attempts - 1 - tally.repairs, 0),  # 0 when no attempt was made
            repairs=tally.repairs,
            attempt_timeouts=tally.attempt_timeouts,
            first_try_valid=tally.first_try_valid,
            **tally.usage.model_dump(),
            cost_usd=tally.cost_usd,
            cost_complete=tally.cost_complete,
            cache_hit=tally.cache_hit,
            latency_ms=max(0.0, (self._clock() - started) * 1000),
            request_ids=tuple(tally.request_ids),
            trace_id=request.trace_id,
            operation=request.operation,
        )
        logger.info(LLM_CALL, extra={LLM_CALL: record.model_dump(mode="json")})


# ── The response cache (cache.py): read and write, both failing open ─────────────────────────


async def _cached[T: BaseModel](
    slot: tuple[ResponseCache, str],
    business_id: str,
    spec: _Schema[T] | None,
    tally: _Tally,
) -> tuple[LLMResult, T | None] | None:
    """A valid entry of this business as the call's answer ($0, no usage), or None: a miss."""
    cache, key = slot
    try:
        raw = await cache.get(key)
    except Exception:  # fails open: a miss, never raised or logged; nothing is written either
        tally.cache_failed = True
        return None
    if raw is None:
        return None
    try:
        entry = CachedResponse.model_validate_json(raw)
    except ValidationError:  # untrusted data: an invalid entry is a miss (and gets overwritten)
        return None
    if entry.business_id != business_id:
        return None  # never another business's answer
    result = LLMResult(
        content=entry.content,
        stop_reason=entry.stop_reason,
        usage=Usage(),
        model_answered=entry.model_answered,
        cost_usd=Decimal(0),
        cost_complete=True,
    )
    value: T | None = None
    if spec is not None:
        value, _ = spec.validate(result.text)  # re-validated: the schema may have changed
        if value is None:
            return None
    tally.served(entry)
    return result, value


async def _store(
    slot: tuple[ResponseCache, str], business_id: str, result: LLMResult, deadline: float | None
) -> None:
    """Write a success. Fails open: cut off at the call's deadline, and any error is dropped, so a
    cache problem never costs the caller an answer that was already paid for."""
    cache, key = slot
    with contextlib.suppress(Exception):
        entry = CachedResponse(
            business_id=business_id,
            model_answered=result.model_answered,
            stop_reason="end_turn",  # the only stop reason `_result` returns as a result
            content=result.content,
        )
        async with asyncio.timeout_at(deadline):
            await cache.set(key, entry.model_dump_json())


# ── Building a gateway from Settings ─────────────────────────────────────────────────────────


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
    """Yield a gateway built from Settings and the packaged catalog; close its provider on exit.

    No response cache: it stays off until M9 brings a store (ADR 0006).
    """
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
        max_repairs=settings.llm_max_repairs,
    )
    try:
        yield gateway
    finally:
        await gateway.aclose()
