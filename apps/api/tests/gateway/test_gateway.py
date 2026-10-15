"""The gateway (`gateway/gateway.py`): whole logical calls on the scripted fake provider.

Nothing here reaches the network or waits long. Backoff waits go to `FakeTime` (an injected clock
and sleep: a wait moves the clock, nothing sleeps). Only the attempt timeout and the deadline are
real `asyncio.timeout`s, of a few hundredths of a second, which end the fake's `HANG`.

Most tests read the `llm_call` records back from the log (`read_records`), which also checks that
the gateway logged nothing else.
"""

import asyncio
import hashlib
import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Final

import pytest
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator
from pydantic.errors import PydanticInvalidForJsonSchema
from pydantic_core import PydanticCustomError

from tests.conftest import make_settings
from vectapilot_api.gateway import gateway as gateway_module
from vectapilot_api.gateway.cache import CachedResponse, ResponseCache, cache_key
from vectapilot_api.gateway.catalog import Catalog, load_catalog, parse_catalog
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
from vectapilot_api.gateway.gateway import (
    LLM_CALL,
    MAX_PROBLEMS,
    LLMGateway,
    build_gateway,
    build_provider,
)
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import (
    HANG,
    OTHER_MODEL,
    THINKING_TEXT,
    FakeProvider,
    FakeReply,
    FakeStep,
)
from vectapilot_api.gateway.types import (
    CallOutcome,
    CallRecord,
    LLMRequest,
    Message,
    RedactedThinkingBlock,
    RetryPolicy,
    StopReason,
    TextBlock,
    ThinkingBlock,
    Usage,
)
from vectapilot_api.log import JsonFormatter, configure_logging

CANARY_PROMPT: Final = "canary-prompt-7f3a"
CANARY_COMPLETION: Final = "canary-completion-7f3a"
CANARY_KEY: Final = "canary-key-7f3a"  # fake on purpose, and not shaped like a real key
TRACE_ID: Final = "4bf92f3577b34da6a3ce929d0e0e4736"
ATTEMPT_TIMEOUT_S: Final = 0.02

CATALOG: Final[Catalog] = parse_catalog(
    """
catalog_version = "test-2026-10-15"

[models.model-a]
source_url = "https://example.com/pricing"
checked = 2026-10-15
standard = { input = 3, output = 15, cache_read = 0.30, cache_write_5m = 3.75, cache_write_1h = 6 }

[models.model-a-snapshot]
source_url = "https://example.com/pricing"
checked = 2026-10-15
standard = { input = 5, output = 25, cache_read = 0.50, cache_write_5m = 6.25, cache_write_1h = 10 }

[models.model-b]
source_url = "https://example.com/pricing"
checked = 2026-10-15
standard = { input = 1, output = 2, cache_read = 0.10, cache_write_5m = 1.25, cache_write_1h = 2 }

[aliases.small]
model = "model-a"
max_output_tokens = 100
effort = "low"

[aliases.large]
model = "model-b"
max_output_tokens = 200
effort = "medium"
"""
)
"""Two aliases with different models, efforts and caps; `model-a-snapshot` is priced but no alias
uses it (it answers in place of `model-a`). `OTHER_MODEL` is not priced at all."""

# FakeReply's default usage is 10 input and 5 output tokens; prices are per million tokens.
COST_MODEL_A: Final = Decimal("0.000105")  # 10 x 3 + 5 x 15 = 105
COST_SNAPSHOT: Final = Decimal("0.000175")  # 10 x 5 + 5 x 25 = 175
DEFAULT_USAGE: Final = Usage(input_tokens=10, output_tokens=5)


class FakeTime:
    """An injected monotonic clock and sleep: a backoff wait moves the clock, nothing waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class SignallingFake(FakeProvider):
    """The fake, plus an event set when an attempt reaches it."""

    def __init__(self, script: list[FakeStep]) -> None:
        super().__init__(script)
        self.reached = asyncio.Event()

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.reached.set()
        return await super().send(request)


class TimeoutRaisingFake(SignallingFake):
    """A provider bug: `send` raises a `TimeoutError` of its own, not the gateway's time limit."""

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        raise TimeoutError


class BlockingSleep:
    """A backoff wait that never ends by itself: only a cancel (the deadline's or the caller's)
    gets the call out of it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.entered.set()
        await asyncio.Event().wait()  # never set


def retry_policy(max_retries: int = 2) -> RetryPolicy:
    return RetryPolicy(max_retries=max_retries, base_delay_s=0.5, max_delay_s=8.0)


class Harness:
    """A gateway on a scripted fake, with fake time. `rand=1.0` makes each backoff its bound
    (0.5 s, then 1.0 s), so waits and `latency_ms` are exact; `rand=0.0` makes every wait 0 s.
    `sleep` replaces the fake time's sleep (to block or fail inside a backoff wait). `cache` is the
    response cache (none by default, as in production)."""

    def __init__(
        self,
        script: list[FakeStep],
        *,
        attempt_timeout_s: float = ATTEMPT_TIMEOUT_S,
        deadline_s: float = 5.0,
        max_retries: int = 2,
        max_repairs: int = 1,
        cache: ResponseCache | None = None,
        provider: SignallingFake | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rand: float = 1.0,
    ) -> None:
        self.provider = SignallingFake(script) if provider is None else provider
        self.time = FakeTime()
        self.gateway = LLMGateway(
            self.provider,
            CATALOG,
            attempt_timeout_s=attempt_timeout_s,
            deadline_s=deadline_s,
            retry_policy=retry_policy(max_retries),
            max_repairs=max_repairs,
            cache=cache,
            clock=self.time.clock,
            sleep=self.time.sleep if sleep is None else sleep,
            rand=lambda: rand,
        )


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "business_id": "sunrise-clinic",
        "alias": "small",
        "system": CANARY_PROMPT,
        "messages": [{"role": "user", "content": CANARY_PROMPT}],
    }
    return LLMRequest.model_validate(fields | overrides)


ReadRecords = Callable[[], list[CallRecord]]


@pytest.fixture
def read_records(caplog: pytest.LogCaptureFixture) -> ReadRecords:
    """Every `llm_call` record logged so far, parsed back into `CallRecord` (the contract)."""
    caplog.set_level(logging.DEBUG)

    def read() -> list[CallRecord]:
        ours = [r for r in caplog.records if r.name.startswith("vectapilot_api")]
        assert [r.getMessage() for r in ours] == [LLM_CALL] * len(ours)  # nothing else is logged
        return [CallRecord.model_validate(getattr(r, LLM_CALL)) for r in ours]

    return read


def the_record(read_records: ReadRecords) -> CallRecord:
    [record] = read_records()  # exactly one per logical call
    return record


def assert_not_chained(error: BaseException) -> None:
    assert error.__context__ is None
    assert error.__cause__ is None


# ── Success, retries and permanent errors ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_success_returns_the_result_and_writes_one_record(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(text=CANARY_COMPLETION)])

    result = await h.gateway.complete(request(trace_id=TRACE_ID, operation="rag-answer"))

    assert (result.text, result.stop_reason, result.model_answered) == (
        CANARY_COMPLETION,
        "end_turn",
        "model-a",
    )
    assert (result.usage, result.cost_usd, result.cost_complete) == (
        DEFAULT_USAGE,
        COST_MODEL_A,
        True,
    )
    assert the_record(read_records).model_dump() == {
        "record_version": 1,
        "business_id": "sunrise-clinic",
        "alias": "small",
        "model_requested": "model-a",
        "model_answered": "model-a",
        "catalog_version": "test-2026-10-15",
        "inference_geo": "global",
        "outcome": CallOutcome.OK,
        "stop_reason": "end_turn",
        "attempts": 1,
        "retries": 0,
        "repairs": 0,
        "attempt_timeouts": 0,
        "first_try_valid": None,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": COST_MODEL_A,
        "cost_complete": True,
        "cache_hit": False,
        "latency_ms": 0.0,
        "request_ids": ("fake-req-1",),
        "trace_id": TRACE_ID,
        "operation": "rag-answer",
    }


@pytest.mark.anyio
async def test_a_transient_error_is_retried_then_ok(read_records: ReadRecords) -> None:
    h = Harness([Transient(status_code=529, request_id="req_t1"), FakeReply()])

    result = await h.gateway.complete(request())

    assert (result.cost_usd, result.cost_complete) == (COST_MODEL_A, True)  # the 529 is not billed
    assert h.time.sleeps == [0.5]
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (CallOutcome.OK, 1, 0)
    assert record.request_ids == ("req_t1", "fake-req-2")
    assert record.latency_ms == 500.0  # the whole call: the backoff wait included


@pytest.mark.anyio
async def test_retries_exhausted_raise_the_last_transient(read_records: ReadRecords) -> None:
    last = Transient(status_code=503)
    h = Harness([Transient(status_code=529), Transient(status_code=429), last])

    with pytest.raises(Transient) as caught:
        await h.gateway.complete(request())

    assert caught.value is last
    assert len(h.provider.calls) == 3
    assert h.time.sleeps == [0.5, 1.0]
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.latency_ms) == (
        CallOutcome.TRANSIENT_EXHAUSTED,
        2,
        1500.0,
    )
    assert (record.model_answered, record.stop_reason, record.inference_geo) == (None, None, None)
    assert (record.cost_usd, record.cost_complete) == (Decimal(0), True)  # errors are not billed


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (SpendLimitReached(status_code=429, request_id="req_p1"), CallOutcome.SPEND_LIMIT),
        (BillingError(status_code=402, request_id="req_p1"), CallOutcome.BILLING),
        (AuthenticationFailed(status_code=401, request_id="req_p1"), CallOutcome.AUTH_FAILED),
        (InvalidRequest(status_code=400, request_id="req_p1"), CallOutcome.INVALID_REQUEST),
        (ProviderError(status_code=418, request_id="req_p1"), CallOutcome.PROVIDER_ERROR),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, GatewayError) else None,
)
async def test_each_permanent_error_ends_the_call_after_one_attempt(
    error: GatewayError, outcome: CallOutcome, read_records: ReadRecords
) -> None:
    h = Harness([error])  # a second attempt would fail the test: the script has one step

    with pytest.raises(GatewayError) as caught:
        await h.gateway.complete(request())

    assert caught.value is error
    assert len(h.provider.calls) == 1
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.request_ids) == (outcome, 0, ("req_p1",))
    assert (record.cost_usd, record.cost_complete) == (Decimal(0), True)  # a status: not billed


# ── Time limits and cancellation ─────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_an_attempt_timeout_is_transient_and_retried(read_records: ReadRecords) -> None:
    h = Harness([HANG, FakeReply()])

    result = await h.gateway.complete(request())

    # The timed-out attempt may have been sent and billed: the cost is a lower bound.
    assert (result.cost_usd, result.cost_complete) == (COST_MODEL_A, False)
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (CallOutcome.OK, 1, 1)
    assert record.request_ids == ("fake-req-2",)  # the hung attempt got none


@pytest.mark.anyio
async def test_attempt_timeouts_can_exhaust_the_retries(read_records: ReadRecords) -> None:
    h = Harness([HANG, HANG, HANG])

    with pytest.raises(Transient) as caught:
        await h.gateway.complete(request())

    assert caught.value.status_code is None
    assert_not_chained(caught.value)  # no TimeoutError or CancelledError behind it
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (
        CallOutcome.TRANSIENT_EXHAUSTED,
        2,
        3,
    )
    assert record.cost_complete is False


@pytest.mark.anyio
async def test_a_deadline_that_fires_mid_attempt_is_deadline_exceeded_not_cancelled(
    read_records: ReadRecords,
) -> None:
    # Equal limits (Settings allows it). The deadline's timer falls due first (it was armed first)
    # or in the same loop tick as the attempt's. Either way the attempt's scope does not claim the
    # cancel: it was not its own timer, or two cancels are pending, so it lets `CancelledError`
    # through, and the deadline's scope turns it into its `TimeoutError`.
    h = Harness([HANG], attempt_timeout_s=0.05, deadline_s=0.05)

    with pytest.raises(DeadlineExceeded) as caught:
        await h.gateway.complete(request())

    assert caught.value.deadline_s == 0.05
    assert_not_chained(caught.value)
    record = the_record(read_records)
    assert record.outcome is CallOutcome.DEADLINE_EXCEEDED
    assert (record.retries, record.attempt_timeouts) == (0, 0)  # the deadline, not the attempt
    assert record.cost_complete is False  # cut off after sending: maybe billed


@pytest.mark.anyio
async def test_an_external_cancel_propagates_and_still_writes_one_record(
    read_records: ReadRecords,
) -> None:
    h = Harness([HANG], attempt_timeout_s=5.0, deadline_s=5.0)
    task = asyncio.create_task(h.gateway.complete(request()))
    await h.provider.reached.wait()  # the attempt now hangs in the provider

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (
        CallOutcome.CANCELLED,
        0,
        0,
    )
    assert record.cost_complete is False


@pytest.mark.anyio
async def test_a_deadline_after_an_attempt_timeout_records_both(read_records: ReadRecords) -> None:
    # The default settings' common case (15 s attempts, 30 s deadline, a hung provider): attempt 1
    # hits its timeout, then the deadline cuts attempt 2. Real clock and sleep (no jitter, so the
    # retry starts at once); 50 ms between the timers keeps it stable under load.
    gateway = LLMGateway(
        SignallingFake([HANG, HANG]),
        CATALOG,
        attempt_timeout_s=0.1,
        deadline_s=0.15,
        retry_policy=retry_policy(),
        max_repairs=1,
        clock=time.monotonic,
        sleep=asyncio.sleep,
        rand=lambda: 0.0,
    )

    with pytest.raises(DeadlineExceeded):
        await gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (
        CallOutcome.DEADLINE_EXCEEDED,
        1,
        1,
    )
    assert record.cost_complete is False
    assert record.latency_ms >= 149  # the whole call, up to the deadline


@pytest.mark.anyio
async def test_a_deadline_during_a_backoff_wait_keeps_a_known_cost_complete(
    read_records: ReadRecords,
) -> None:
    sleep = BlockingSleep()
    h = Harness([Transient(status_code=529)], deadline_s=0.02, sleep=sleep, rand=0.0)

    with pytest.raises(DeadlineExceeded):
        await h.gateway.complete(request())

    assert sleep.entered.is_set()
    record = the_record(read_records)
    assert (record.outcome, record.retries, record.attempt_timeouts) == (
        CallOutcome.DEADLINE_EXCEEDED,
        0,
        0,
    )
    assert record.cost_complete is True  # nothing was in flight: the 529 is not billed


@pytest.mark.anyio
async def test_an_external_cancel_during_a_backoff_wait_keeps_a_known_cost_complete(
    read_records: ReadRecords,
) -> None:
    sleep = BlockingSleep()
    h = Harness([Transient(status_code=529)], sleep=sleep, rand=0.0)
    task = asyncio.create_task(h.gateway.complete(request()))
    await sleep.entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = the_record(read_records)
    assert (record.outcome, record.retries, record.cost_complete) == (
        CallOutcome.CANCELLED,
        0,
        True,
    )


@pytest.mark.anyio
async def test_a_cancel_in_the_same_tick_as_the_deadline_is_the_callers_and_propagates(
    read_records: ReadRecords,
) -> None:
    # asyncio's cancel count, not `.expired()`, decides: with the caller's cancel also pending, the
    # deadline's scope lets `CancelledError` through, so callers' own timeouts and task groups
    # keep working. Catching `CancelledError` and asking `.expired()` would swallow it.
    h = Harness([HANG], attempt_timeout_s=5.0, deadline_s=0.01)
    task = asyncio.create_task(h.gateway.complete(request()))
    await h.provider.reached.wait()

    time.sleep(0.02)  # noqa: ASYNC251 -- blocks the loop on purpose: the deadline is now overdue
    asyncio.get_running_loop().call_later(0, task.cancel)  # due in the same tick as the deadline
    with pytest.raises(asyncio.CancelledError):
        await task

    assert the_record(read_records).outcome is CallOutcome.CANCELLED


@pytest.mark.anyio
async def test_a_timeout_error_from_the_provider_is_a_bug_not_an_attempt_timeout(
    read_records: ReadRecords,
) -> None:
    h = Harness([], provider=TimeoutRaisingFake([]))

    with pytest.raises(TimeoutError):
        await h.gateway.complete(request())

    assert len(h.provider.calls) == 1  # never retried
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.retries, record.attempt_timeouts) == (
        CallOutcome.INTERNAL_ERROR,
        1,
        0,
        0,
    )
    assert record.cost_complete is False  # it failed mid-attempt: maybe billed


@pytest.mark.anyio
async def test_a_timeout_error_that_is_not_the_deadline_is_not_recorded_as_one(
    read_records: ReadRecords,
) -> None:
    async def broken_sleep(seconds: float) -> None:
        raise TimeoutError

    h = Harness([Transient(status_code=529)], max_retries=1, sleep=broken_sleep)

    with pytest.raises(TimeoutError):
        await h.gateway.complete(request())

    assert the_record(read_records).outcome is CallOutcome.INTERNAL_ERROR  # a bug, not a deadline


@pytest.mark.anyio
async def test_a_response_without_a_request_id_adds_none(
    monkeypatch: pytest.MonkeyPatch, read_records: ReadRecords
) -> None:
    # The adapter returns None when the `request-id` header is missing; the fake always sets one.
    h = Harness([FakeReply()])
    send = h.provider.send

    async def without_request_id(request: ProviderRequest) -> ProviderResponse:
        return (await send(request)).model_copy(update={"request_id": None})

    monkeypatch.setattr(h.provider, "send", without_request_id)

    await h.gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.request_ids) == (CallOutcome.OK, ())


# ── Cost ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_cost_is_priced_by_the_answering_model(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(model="model-a-snapshot")])

    result = await h.gateway.complete(request())

    assert (result.model_answered, result.cost_usd, result.cost_complete) == (
        "model-a-snapshot",
        COST_SNAPSHOT,
        True,
    )
    record = the_record(read_records)
    assert (record.model_requested, record.model_answered) == ("model-a", "model-a-snapshot")
    assert record.cost_usd == COST_SNAPSHOT


def test_tokens_and_cost_sum_over_attempts_each_priced_by_its_answering_model() -> None:
    # The bookkeeping on its own; through the gateway, a repair reaches the second term
    # (test_a_repair_bills_two_attempts_each_priced_by_its_answering_model).
    def answer(model: str, usage: Usage, request_id: str, geo: str) -> ProviderResponse:
        return ProviderResponse(
            content=(),
            stop_reason="end_turn",
            usage=usage,
            model=model,
            request_id=request_id,
            inference_geo=geo,
        )

    tally = gateway_module._Tally()
    tally.answered(answer("model-a", DEFAULT_USAGE, "req_1", "global"), CATALOG)
    tally.answered(
        answer("model-a-snapshot", Usage(input_tokens=20, output_tokens=10), "req_2", "us"), CATALOG
    )

    assert tally.usage == Usage(input_tokens=30, output_tokens=15)
    # (10 x 3 + 5 x 15) + (20 x 5 + 10 x 25) = 105 + 350 = 455 per million tokens
    assert tally.cost_usd == Decimal("0.000455")
    assert tally.cost_complete is False  # the second answer came from `us`
    assert tally.request_ids == ["req_1", "req_2"]


@pytest.mark.anyio
async def test_an_unpriced_answering_model_makes_the_cost_incomplete(
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(model=OTHER_MODEL)])

    result = await h.gateway.complete(request())

    assert (result.cost_usd, result.cost_complete) == (Decimal(0), False)  # a lower bound
    assert the_record(read_records).model_answered == OTHER_MODEL


@pytest.mark.anyio
@pytest.mark.parametrize("geo", [None, "us"], ids=["unknown", "us"])
async def test_an_inference_geo_other_than_global_makes_the_cost_incomplete(
    geo: str | None, read_records: ReadRecords
) -> None:
    h = Harness([FakeReply(inference_geo=geo)])

    result = await h.gateway.complete(request())

    assert (result.cost_usd, result.cost_complete) == (COST_MODEL_A, False)
    record = the_record(read_records)
    assert (record.inference_geo, record.cost_complete) == (geo, False)


@pytest.mark.anyio
async def test_a_status_less_provider_error_makes_the_cost_incomplete(
    read_records: ReadRecords,
) -> None:
    # From the adapter: a 2xx it could not read, or a status outside 100-599. Maybe billed.
    h = Harness([ProviderError()])

    with pytest.raises(ProviderError):
        await h.gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.cost_complete) == (CallOutcome.PROVIDER_ERROR, False)


@pytest.mark.anyio
async def test_a_status_less_transient_makes_the_cost_incomplete(read_records: ReadRecords) -> None:
    # From the adapter: the connection was lost, possibly after the request was sent.
    h = Harness([Transient(), FakeReply()])

    result = await h.gateway.complete(request())

    assert (result.cost_usd, result.cost_complete) == (COST_MODEL_A, False)
    assert the_record(read_records).cost_complete is False


# ── Stop reasons ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stop_reason", "error_type", "outcome"),
    [
        ("refusal", ModelRefused, CallOutcome.REFUSAL),
        ("max_tokens", OutputTruncated, CallOutcome.TRUNCATED),
        ("model_context_window_exceeded", OutputTruncated, CallOutcome.TRUNCATED),
        ("tool_use", ProviderError, CallOutcome.PROVIDER_ERROR),
        ("pause_turn", ProviderError, CallOutcome.PROVIDER_ERROR),
        ("stop_sequence", ProviderError, CallOutcome.PROVIDER_ERROR),
        ("other", ProviderError, CallOutcome.PROVIDER_ERROR),
    ],
)
async def test_every_stop_reason_but_end_turn_is_a_typed_error_with_a_complete_cost(
    stop_reason: StopReason,
    error_type: type[GatewayError],
    outcome: CallOutcome,
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(stop_reason=stop_reason, text=CANARY_COMPLETION)])

    with pytest.raises(error_type) as caught:
        await h.gateway.complete(request())

    error = caught.value
    assert (error.status_code, error.request_id, error.retryable) == (None, "fake-req-1", False)
    if isinstance(error, OutputTruncated):
        assert error.stop_reason == stop_reason
    assert_not_chained(error)
    assert len(h.provider.calls) == 1  # never retried
    record = the_record(read_records)
    assert (record.outcome, record.stop_reason) == (outcome, stop_reason)
    # The gateway raised it about a response it read: that usage is known, so the cost is
    # complete, unlike a status-less ProviderError from the provider (test above).
    assert (record.cost_usd, record.cost_complete) == (COST_MODEL_A, True)
    assert (record.input_tokens, record.output_tokens) == (10, 5)


@pytest.mark.anyio
async def test_an_odd_stop_reason_and_model_id_cannot_mask_the_outcome(
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(stop_reason="other", model="odd.model:id_9-x")])

    with pytest.raises(ProviderError):
        await h.gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.stop_reason) == (CallOutcome.PROVIDER_ERROR, "other")
    assert (record.model_answered, record.cost_complete) == ("odd.model:id_9-x", False)


@pytest.mark.anyio
async def test_an_unexpected_provider_exception_propagates_as_an_internal_error(
    read_records: ReadRecords,
) -> None:
    # The fake cannot build a response with a model id that is not id-shaped; the adapter would
    # turn that into a ProviderError, the fake lets the ValidationError out: a bug, so it propagates
    # unchanged and is recorded as `internal_error`, never as `provider_error` (decided 2026-10-15).
    h = Harness([FakeReply(model=f"odd model {CANARY_COMPLETION}")])

    with pytest.raises(ValidationError):
        await h.gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.model_answered) == (CallOutcome.INTERNAL_ERROR, None)
    assert record.attempts == 1
    assert record.cost_complete is False  # it failed mid-attempt: maybe billed


@pytest.mark.anyio
async def test_a_failing_record_cannot_mask_the_result_or_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the log handler broke")

    monkeypatch.setattr(gateway_module.logger, "info", broken)
    h = Harness([FakeReply(), FakeReply(stop_reason="refusal")])

    assert (await h.gateway.complete(request())).cost_complete is True
    with pytest.raises(ModelRefused):
        await h.gateway.complete(request())


# ── Before any call ──────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_max_tokens_above_the_alias_maximum_is_rejected_before_any_call(
    read_records: ReadRecords,
) -> None:
    h = Harness([])

    with pytest.raises(InvalidRequest) as caught:
        await h.gateway.complete(request(max_tokens=101))

    assert (caught.value.status_code, caught.value.request_id) == (None, None)
    assert h.provider.calls == []
    # Decided 2026-10-15: one record, with zeros, no model answered and a complete $0.
    assert the_record(read_records).model_dump(exclude={"latency_ms"}) == {
        "record_version": 1,
        "business_id": "sunrise-clinic",
        "alias": "small",
        "model_requested": "model-a",
        "model_answered": None,
        "catalog_version": "test-2026-10-15",
        "inference_geo": None,
        "outcome": CallOutcome.INVALID_REQUEST,
        "stop_reason": None,
        "attempts": 0,
        "retries": 0,
        "repairs": 0,
        "attempt_timeouts": 0,
        "first_try_valid": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": Decimal(0),
        "cost_complete": True,
        "cache_hit": False,
        "request_ids": (),
        "trace_id": None,
        "operation": None,
    }


@pytest.mark.anyio
async def test_an_unknown_alias_is_rejected_before_any_call_with_one_record(
    read_records: ReadRecords,
) -> None:
    # Decision 3 (every logical call, one record); no model requested (decided 2026-10-15).
    h = Harness([])

    with pytest.raises(InvalidRequest) as caught:
        await h.gateway.complete(request(alias="medium"))

    assert (caught.value.status_code, caught.value.request_id) == (None, None)
    assert_not_chained(caught.value)
    assert h.provider.calls == []
    record = the_record(read_records)
    assert (record.alias, record.model_requested, record.outcome) == (
        "medium",
        None,
        CallOutcome.INVALID_REQUEST,
    )
    assert (
        record.attempts,
        record.retries,
        record.attempt_timeouts,
        record.input_tokens,
        record.cost_usd,
    ) == (0, 0, 0, 0, Decimal(0))
    assert (record.model_answered, record.cost_complete) == (None, True)


@pytest.mark.anyio
async def test_calls_show_the_model_effort_and_max_tokens_per_alias() -> None:
    h = Harness([FakeReply(), FakeReply(), FakeReply()])

    await h.gateway.complete(request(alias="small"))
    await h.gateway.complete(request(alias="large"))
    await h.gateway.complete(request(alias="small", max_tokens=100))  # = the maximum: allowed

    assert [(c.model, c.effort, c.max_tokens) for c in h.provider.calls] == [
        ("model-a", "low", 100),
        ("model-b", "medium", 200),
        ("model-a", "low", 100),
    ]
    sent = h.provider.calls[0]
    assert (sent.system, sent.messages) == (request().system, request().messages)


# ── What is logged (ADR 0007) ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_llm_call_line_holds_no_prompt_or_completion(
    caplog: pytest.LogCaptureFixture, read_records: ReadRecords
) -> None:
    h = Harness(
        [
            FakeReply(text=CANARY_COMPLETION, thinking=True),
            FakeReply(text=CANARY_COMPLETION, stop_reason="refusal"),
            FakeReply(text=CANARY_COMPLETION, stop_reason="max_tokens"),
            FakeReply(text=CANARY_COMPLETION, stop_reason="tool_use"),
        ]
    )
    errors: list[GatewayError] = []

    for _ in range(4):
        try:
            await h.gateway.complete(request())
        except GatewayError as error:
            errors.append(error)

    assert len(read_records()) == 4  # and nothing else from our loggers
    lines = [JsonFormatter().format(r) for r in caplog.records]  # every logger, as JSON lines
    error_texts = ["".join(traceback.format_exception(e)) + repr(e) for e in errors]
    for text in lines + error_texts:
        assert CANARY_PROMPT not in text
        assert CANARY_COMPLETION not in text
    ours = [json.loads(line) for line in lines if '"logger": "vectapilot_api' in line]
    assert [line["message"] for line in ours] == [LLM_CALL] * 4
    assert all(isinstance(line[LLM_CALL], dict) for line in ours)  # the record, under one key


@pytest.mark.anyio
async def test_a_quieter_log_level_never_drops_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Settings allows log_level WARNING and ERROR; the gateway's logger is pinned at INFO.
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    try:
        configure_logging("WARNING")  # as the app does at startup: JSON lines to stdout
        await Harness([FakeReply()]).gateway.complete(request())
    finally:
        root.setLevel(saved_level)
        root.handlers[:] = saved_handlers

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["message"] for line in lines] == [LLM_CALL]
    assert CallRecord.model_validate(lines[0][LLM_CALL]).outcome is CallOutcome.OK


# ── build_provider and build_gateway ─────────────────────────────────────────────────────────


def test_build_provider_gives_the_fake_for_the_fake_setting() -> None:
    assert isinstance(build_provider(make_settings()), FakeProvider)


class SpyAnthropicProvider:
    """Stands in for the adapter: records how the factory built it, and makes no client."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def test_build_provider_gives_the_adapter_the_secret_and_the_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_module, "AnthropicProvider", SpyAnthropicProvider)
    settings = make_settings(
        environment="local",
        llm_provider="anthropic",
        anthropic_api_key=CANARY_KEY,
        llm_attempt_timeout_seconds=12.0,
    )

    provider = build_provider(settings)

    assert isinstance(provider, SpyAnthropicProvider)
    # Still a SecretStr: the adapter unwraps it in its constructor (and adds the 1 s SDK margin).
    assert provider.kwargs == {"api_key": SecretStr(CANARY_KEY), "attempt_timeout_s": 12.0}
    assert isinstance(provider.kwargs["api_key"], SecretStr)


def test_build_provider_refuses_the_adapter_without_a_key() -> None:
    settings = make_settings(
        environment="local", llm_provider="anthropic", anthropic_api_key=CANARY_KEY
    )
    unchecked = settings.model_copy(update={"anthropic_api_key": None})  # skips validation

    with pytest.raises(ValueError, match="anthropic_api_key is required"):
        build_provider(unchecked)


@pytest.mark.anyio
async def test_build_gateway_uses_settings_and_the_catalog_then_closes_the_provider(
    monkeypatch: pytest.MonkeyPatch, read_records: ReadRecords
) -> None:
    fake = FakeProvider([FakeReply(), Transient(status_code=529)])
    limits: list[dict[str, Any]] = []

    def scripted(settings: object) -> Provider:
        return fake

    class RecordingGateway(LLMGateway):
        def __init__(self, provider: Provider, catalog: Catalog, **kwargs: Any) -> None:
            limits.append(kwargs)
            super().__init__(provider, catalog, **kwargs)

    monkeypatch.setattr(gateway_module, "build_provider", scripted)
    monkeypatch.setattr(gateway_module, "LLMGateway", RecordingGateway)
    catalog = load_catalog()
    settings = make_settings(
        llm_attempt_timeout_seconds=7.0,
        llm_deadline_seconds=11.0,
        llm_max_retries=0,
        llm_backoff_base_seconds=0.25,
        llm_backoff_cap_seconds=4.0,
        llm_max_repairs=0,
    )

    async with build_gateway(settings) as gateway:
        await gateway.complete(request(alias="small"))
        with pytest.raises(Transient):  # llm_max_retries=0: not retried
            await gateway.complete(request(alias="large"))
        assert fake.closed is False

    assert fake.closed is True
    # Each limit reaches its own parameter (swapped, no retry could ever fit in the deadline);
    # no cache (off until M9); clock, sleep and rand are left to their production defaults.
    assert limits == [
        {
            "attempt_timeout_s": 7.0,
            "deadline_s": 11.0,
            "retry_policy": RetryPolicy(max_retries=0, base_delay_s=0.25, max_delay_s=4.0),
            "max_repairs": 0,
        }
    ]
    small, large = catalog.aliases["small"], catalog.aliases["large"]
    assert [(c.model, c.effort, c.max_tokens) for c in fake.calls] == [
        (small.model, small.effort, small.max_output_tokens),
        (large.model, large.effort, large.max_output_tokens),
    ]
    assert {r.catalog_version for r in read_records()} == {catalog.catalog_version}


@pytest.mark.anyio
async def test_build_gateway_closes_the_provider_when_the_body_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeProvider()
    monkeypatch.setattr(gateway_module, "build_provider", lambda settings: fake)

    with pytest.raises(RuntimeError, match="the caller failed"):
        async with build_gateway(make_settings()):
            raise RuntimeError("the caller failed")

    assert fake.closed is True


@pytest.mark.anyio
async def test_build_gateway_builds_the_provider_last(monkeypatch: pytest.MonkeyPatch) -> None:
    # A retry policy that fails must not leave an HTTP client behind that nobody closes.
    built: list[FakeProvider] = []

    def spy(settings: object) -> FakeProvider:
        built.append(FakeProvider())
        return built[-1]

    monkeypatch.setattr(gateway_module, "build_provider", spy)
    unchecked = make_settings().model_copy(update={"llm_max_retries": 11})  # skips validation

    with pytest.raises(ValidationError):
        async with build_gateway(unchecked):
            pass  # never reached

    assert built == []


# ── Structured output (complete_structured) ──────────────────────────────────────────────────


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class Answer(BaseModel):
    """A support answer with its sources."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(max_length=40)
    citations: list[Citation] = []
    scores: dict[str, int] = {}
    note: str | None = None


class Brief(BaseModel):
    """A second schema, for "two schemas, two keys"."""

    answer: str


VALID: Final = '{"answer": "We open at 9 on Sundays", "citations": [{"url": "https://e.com/h"}]}'
VALID_ANSWER: Final = Answer(
    answer="We open at 9 on Sundays", citations=[Citation(url="https://e.com/h")]
)
SCHEMA_TEXT: Final = json.dumps(Answer.model_json_schema(), sort_keys=True, separators=(",", ":"))
INSTRUCTION: Final = (
    "Answer with only a JSON object that matches this JSON Schema, and no other text:\n"
    + SCHEMA_TEXT
)
JSON_KEY: Final = "canary-jsonkey-7f3a"
JSON_VALUE: Final = "canary-jsonvalue-7f3a"
CARD_NUMBER: Final = "4111111111111111"  # a numeric key: Pydantic puts it into `loc` as a string

HOSTILE_OUTPUTS: Final = [
    pytest.param(
        json.dumps({"answer": JSON_VALUE * 3, JSON_KEY: JSON_VALUE}),
        (("(root)", "extra_forbidden"), ("answer", "string_too_long")),
        id="unexpected-top-level-key-and-a-long-value",
    ),
    pytest.param(
        json.dumps({"answer": "ok", "citations": [{"url": 7, JSON_KEY: JSON_VALUE}]}),
        (("citations", "extra_forbidden"), ("citations.url", "string_type")),
        id="unexpected-key-in-a-list-item",
    ),
    pytest.param(
        json.dumps({"answer": "ok", "scores": {JSON_KEY: JSON_VALUE}}),
        (("scores", "int_parsing"),),
        id="dict-key",
    ),
    pytest.param(
        json.dumps({"answer": "ok", "scores": {CARD_NUMBER: JSON_VALUE}}),
        (("scores", "int_parsing"),),
        id="numeric-dict-key",
    ),
    pytest.param(
        json.dumps({"answer": {JSON_KEY: JSON_VALUE}}),
        (("answer", "string_type"),),
        id="object-instead-of-a-string",
    ),
    pytest.param(
        f'{{"{JSON_KEY}": "{JSON_VALUE}", "answer": ',
        (("(root)", "json_invalid"),),
        id="not-json",
    ),
    pytest.param(
        f"```json\n{json.dumps({'answer': JSON_VALUE})}\n```",
        (("(root)", "json_invalid"),),
        id="code-fenced-json-is-invalid",  # never stripped: the first-try number stays honest
    ),
    # Unexpected keys that equal a schema field name elsewhere: reported at their parent object.
    pytest.param(
        json.dumps({"answer": "ok", "url": JSON_VALUE}),
        (("(root)", "extra_forbidden"),),
        id="look-alike-key-at-the-top",
    ),
    pytest.param(
        json.dumps({"answer": "ok", "citations": [{"url": "x", "answer": JSON_VALUE}]}),
        (("citations", "extra_forbidden"),),
        id="look-alike-key-in-a-list-item",
    ),
    # A dict key equal to a schema field name stays in the location: schema vocabulary only.
    pytest.param(
        json.dumps({"answer": "ok", "scores": {"note": JSON_VALUE}}),
        (("scores.note", "int_parsing"),),
        id="look-alike-dict-key",
    ),
]


def feedback(*problems: tuple[str, str]) -> str:
    lines = "".join(f"\n- {location}: {kind}" for location, kind in problems)
    return (
        "A previous answer to this request did not match the JSON Schema. Problems (location:"
        f" error type):{lines}\nAnswer again with only a JSON object that matches the schema."
    )


@pytest.mark.anyio
async def test_a_valid_first_answer_is_returned_with_one_record(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(text=VALID)])

    answer = await h.gateway.complete_structured(request(), Answer)

    assert answer.value == VALID_ANSWER
    assert (answer.result.text, answer.result.cost_usd) == (VALID, COST_MODEL_A)
    # One instruction after the caller's system prompt; the messages are the caller's own.
    [sent] = h.provider.calls
    assert sent.system == f"{CANARY_PROMPT}\n\n{INSTRUCTION}"
    assert sent.messages == request().messages
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.retries, record.repairs) == (
        CallOutcome.OK,
        1,
        0,
        0,
    )
    assert record.first_try_valid is True


@pytest.mark.anyio
async def test_without_a_system_prompt_the_instruction_is_the_system_prompt() -> None:
    h = Harness([FakeReply(text=VALID)])

    await h.gateway.complete_structured(request(system=None), Answer)

    assert h.provider.calls[0].system == INSTRUCTION


@pytest.mark.anyio
async def test_an_invalid_answer_is_repaired_once_by_a_fresh_request(
    read_records: ReadRecords,
) -> None:
    hostile = json.dumps({"answer": JSON_VALUE * 3, JSON_KEY: JSON_VALUE})
    h = Harness([FakeReply(text=hostile, thinking=True), FakeReply(text=VALID)])

    answer = await h.gateway.complete_structured(request(), Answer)

    assert answer.value == VALID_ANSWER
    first, repair = h.provider.calls
    # The original messages plus one feedback message; no assistant turn, no thinking block.
    assert repair.messages == (
        *request().messages,
        Message(
            role="user",
            content=feedback(("(root)", "extra_forbidden"), ("answer", "string_too_long")),
        ),
    )
    assert [m.role for m in repair.messages] == ["user", "user"]
    assert (repair.model, repair.effort, repair.max_tokens, repair.system) == (
        first.model,
        first.effort,
        first.max_tokens,
        first.system,
    )
    sent = repair.model_dump_json()
    for text in (THINKING_TEXT, "fake-signature", "fake-redacted-thinking", JSON_KEY, JSON_VALUE):
        assert text not in sent
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.retries, record.repairs) == (
        CallOutcome.OK,
        2,
        0,
        1,
    )
    assert record.first_try_valid is False
    assert record.request_ids == ("fake-req-1", "fake-req-2")


@pytest.mark.anyio
async def test_a_failed_repair_is_validation_failed(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(text="{}"), FakeReply(text='{"answer": 1}')])

    with pytest.raises(StructuredOutputInvalid) as caught:
        await h.gateway.complete_structured(request(), Answer)

    error = caught.value
    assert error.problems == (("answer", "string_type"),)  # the repair's problems
    assert (error.request_id, error.retryable) == ("fake-req-2", False)
    assert_not_chained(error)
    assert h.provider.calls[1].messages[-1].content == feedback(("answer", "missing"))
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.retries, record.repairs) == (
        CallOutcome.VALIDATION_FAILED,
        2,
        0,
        1,
    )
    assert record.first_try_valid is False
    # Both answers were billed and are counted; their usage is known, so the cost is complete.
    assert (record.input_tokens, record.output_tokens) == (20, 10)
    assert (record.cost_usd, record.cost_complete) == (2 * COST_MODEL_A, True)


@pytest.mark.anyio
async def test_with_repairs_off_an_invalid_answer_fails_after_one_attempt(
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(text="{}")], max_repairs=0)

    with pytest.raises(StructuredOutputInvalid) as caught:
        await h.gateway.complete_structured(request(), Answer)

    assert caught.value.problems == (("answer", "missing"),)
    assert len(h.provider.calls) == 1
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.repairs, record.first_try_valid) == (
        CallOutcome.VALIDATION_FAILED,
        1,
        0,
        False,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stop_reason", "error_type"),
    [
        ("refusal", ModelRefused),
        ("max_tokens", OutputTruncated),
        ("model_context_window_exceeded", OutputTruncated),
    ],
)
async def test_a_refusal_or_truncation_is_never_repaired(
    stop_reason: StopReason, error_type: type[GatewayError], read_records: ReadRecords
) -> None:
    h = Harness([FakeReply(text="{", stop_reason=stop_reason)])  # one step: a repair would fail

    with pytest.raises(error_type):
        await h.gateway.complete_structured(request(), Answer)

    assert len(h.provider.calls) == 1
    record = the_record(read_records)
    assert (record.attempts, record.repairs, record.first_try_valid) == (1, 0, None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stop_reason", "error_type", "outcome"),
    [
        ("refusal", ModelRefused, CallOutcome.REFUSAL),
        ("max_tokens", OutputTruncated, CallOutcome.TRUNCATED),
    ],
)
async def test_a_refusal_or_truncation_of_the_repair_is_its_typed_error(
    stop_reason: StopReason,
    error_type: type[GatewayError],
    outcome: CallOutcome,
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(text="{}"), FakeReply(text=VALID, stop_reason=stop_reason)])

    with pytest.raises(error_type):
        await h.gateway.complete_structured(request(), Answer)

    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.repairs, record.first_try_valid) == (
        outcome,
        2,
        1,
        False,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(("output", "problems"), HOSTILE_OUTPUTS)
async def test_nothing_the_model_wrote_reaches_the_feedback_logs_or_error(
    output: str,
    problems: tuple[tuple[str, str], ...],
    caplog: pytest.LogCaptureFixture,
    read_records: ReadRecords,
) -> None:
    h = Harness([FakeReply(text=output), FakeReply(text=output)])

    with pytest.raises(StructuredOutputInvalid) as caught:
        await h.gateway.complete_structured(request(), Answer)

    error = caught.value
    assert error.problems == problems  # schema locations and error types only
    feedback_message = h.provider.calls[1].messages[-1].content
    assert feedback_message == feedback(*problems)
    texts = [
        feedback_message,
        *(call.model_dump_json() for call in h.provider.calls),  # what the fake received
        *(JsonFormatter().format(r) for r in caplog.records),  # every log line
        "".join(traceback.format_exception(error)) + repr(error),
    ]
    for text in texts:
        for canary in (JSON_KEY, JSON_VALUE, CARD_NUMBER):
            assert canary not in text
    assert the_record(read_records).outcome is CallOutcome.VALIDATION_FAILED


class Seven(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: int
    b: int
    c: int
    d: int
    e: int
    f: int
    g: int


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param(
            "{}", tuple((name, "missing") for name in "abcde"), id="7-missing-fields-5-items"
        ),
        pytest.param(
            json.dumps({**dict.fromkeys("abcdef", 1), **{f"{JSON_KEY}-{i}": i for i in range(50)}}),
            (("(root)", "extra_forbidden"), ("g", "missing")),
            id="50-unexpected-keys-1-item",
        ),
    ],
)
async def test_feedback_is_deterministic_deduplicated_and_at_most_five_items(
    output: str, expected: tuple[tuple[str, str], ...]
) -> None:
    h = Harness([FakeReply(text=output)] * 4, max_repairs=1)

    for _ in range(2):
        with pytest.raises(StructuredOutputInvalid) as caught:
            await h.gateway.complete_structured(request(), Seven)

    assert MAX_PROBLEMS == 5
    assert caught.value.problems == expected
    feedbacks = [call.messages[-1].content for call in h.provider.calls[1::2]]
    assert feedbacks == [feedback(*expected)] * 2  # the same output, the same feedback


class EchoesItsInput(BaseModel):
    """A schema whose validator (wrongly) builds its error type from the model's output."""

    answer: str

    @field_validator("answer")
    @classmethod
    def _echo(cls, value: str) -> str:
        raise PydanticCustomError(value, "an error type made of the output")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "output",
    [
        pytest.param("canary_jsonvalue_7f3a", id="shaped-like-an-error-code"),  # zara's probe
        pytest.param(f"Custom {JSON_VALUE}", id="free-text"),
    ],
)
async def test_an_error_type_pydantic_does_not_define_is_reported_as_invalid(
    output: str, caplog: pytest.LogCaptureFixture, read_records: ReadRecords
) -> None:
    h = Harness([FakeReply(text=json.dumps({"answer": output}))] * 2)

    with pytest.raises(StructuredOutputInvalid) as caught:
        await h.gateway.complete_structured(request(), EchoesItsInput)

    assert caught.value.problems == (("answer", "invalid"),)
    texts = [
        *(call.model_dump_json() for call in h.provider.calls),  # the feedback included
        *(JsonFormatter().format(r) for r in caplog.records),
        "".join(traceback.format_exception(caught.value)) + repr(caught.value),
    ]
    assert all(output not in text for text in texts)
    assert the_record(read_records).outcome is CallOutcome.VALIDATION_FAILED


@pytest.mark.anyio
async def test_a_repair_bills_two_attempts_each_priced_by_its_answering_model(
    read_records: ReadRecords,
) -> None:
    # The step 6 review's test: the sum over attempts, first reached through the gateway here.
    # The repair has its own usage, so reusing the first answer's usage would be caught too.
    repair_usage = Usage(input_tokens=12, output_tokens=7)
    h = Harness(
        [FakeReply(text="{}"), FakeReply(text=VALID, model="model-a-snapshot", usage=repair_usage)]
    )

    answer = await h.gateway.complete_structured(request(), Answer)

    # model-a: 10 x 3 + 5 x 15 = 105; model-a-snapshot: 12 x 5 + 7 x 25 = 235 (per million
    # tokens), so 340 per million: exactly $0.000340.
    result = answer.result
    assert (result.cost_usd, result.cost_complete) == (Decimal("0.000340"), True)
    assert (result.usage, result.model_answered) == (
        Usage(input_tokens=22, output_tokens=12),
        "model-a-snapshot",
    )
    record = the_record(read_records)
    assert (record.cost_usd, record.cost_complete) == (Decimal("0.000340"), True)
    assert (record.input_tokens, record.output_tokens) == (22, 12)
    assert (record.model_requested, record.model_answered) == ("model-a", "model-a-snapshot")


@pytest.mark.anyio
async def test_a_repair_is_retried_and_its_retries_are_counted(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(text="{}"), Transient(status_code=529), FakeReply(text=VALID)])

    await h.gateway.complete_structured(request(), Answer)

    assert h.time.sleeps == [0.5]
    record = the_record(read_records)
    # attempts = 1 + repairs + retries
    assert (record.attempts, record.repairs, record.retries) == (3, 1, 1)
    assert record.request_ids == ("fake-req-1", "fake-req-3")  # the scripted 529 had none


@pytest.mark.anyio
async def test_the_repair_runs_inside_the_same_deadline(read_records: ReadRecords) -> None:
    h = Harness([FakeReply(text="{}"), HANG], attempt_timeout_s=5.0, deadline_s=0.05)

    with pytest.raises(DeadlineExceeded):
        await h.gateway.complete_structured(request(), Answer)

    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.repairs, record.retries) == (
        CallOutcome.DEADLINE_EXCEEDED,
        2,
        1,
        0,
    )
    assert (record.first_try_valid, record.cost_complete) == (False, False)


@pytest.mark.anyio
async def test_a_thinking_block_round_trips_unchanged_in_the_result() -> None:
    h = Harness(
        [FakeReply(text=CANARY_COMPLETION, thinking=True), FakeReply(text=VALID, thinking=True)]
    )

    result = await h.gateway.complete(request())
    structured = await h.gateway.complete_structured(request(), Answer)

    assert result.content == (
        ThinkingBlock(thinking=THINKING_TEXT, signature="fake-signature"),
        RedactedThinkingBlock(data="fake-redacted-thinking"),
        TextBlock(text=CANARY_COMPLETION),
    )
    assert result.text == CANARY_COMPLETION  # thinking is never part of the text
    assert structured.result.content[:2] == result.content[:2]
    assert structured.value == VALID_ANSWER  # validated from the text block only


@pytest.mark.anyio
async def test_a_schema_without_a_json_schema_is_an_internal_error_with_one_record(
    read_records: ReadRecords,
) -> None:
    class NoJsonSchema(BaseModel):
        handle: Callable[[], None] | None = None  # Pydantic has no JSON Schema for a callable

    h = Harness([])

    with pytest.raises(PydanticInvalidForJsonSchema):  # a caller bug: propagates unchanged
        await h.gateway.complete_structured(request(), NoJsonSchema)

    assert h.provider.calls == []
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.model_requested) == (
        CallOutcome.INTERNAL_ERROR,
        0,
        "model-a",
    )


# ── Response cache (cache.py) ─────────────────────────────────────────────────────────────────

CACHE_ERROR_TEXT: Final = "canary-cache-error-7f3a"


class DictCache:
    """The `ResponseCache` test double: a dict. It can raise or hang on get or set."""

    def __init__(
        self,
        *,
        fail_get: bool = False,
        fail_set: bool = False,
        hang_get: bool = False,
        hang_set: bool = False,
    ) -> None:
        self.data: dict[str, str] = {}
        self.gets: list[str] = []
        self.sets: list[str] = []
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.hang_get = hang_get
        self.hang_set = hang_set

    async def get(self, key: str) -> str | None:
        self.gets.append(key)
        if self.fail_get:
            raise ConnectionError(CACHE_ERROR_TEXT)
        if self.hang_get:
            await asyncio.Event().wait()  # never set
        return self.data.get(key)

    async def set(self, key: str, value: str) -> None:
        self.sets.append(key)
        if self.fail_set:
            raise ConnectionError(CACHE_ERROR_TEXT)
        if self.hang_set:
            await asyncio.Event().wait()  # never set
        self.data[key] = value


def is_a_key_of(business_id: str, key: str) -> bool:
    prefix, digest = key.split(":")
    return prefix == business_id and len(digest) == 64 and int(digest, 16) >= 0


@pytest.mark.anyio
async def test_the_cache_is_off_by_default() -> None:
    h = Harness([FakeReply(), FakeReply()])

    await h.gateway.complete(request())
    await h.gateway.complete(request())

    assert len(h.provider.calls) == 2


@pytest.mark.anyio
async def test_a_miss_calls_the_model_and_a_hit_does_not(read_records: ReadRecords) -> None:
    cache = DictCache()
    h = Harness([FakeReply(text=CANARY_COMPLETION, thinking=True)], cache=cache)

    first = await h.gateway.complete(request())
    # trace_id and operation do not change the answer, so they are not in the key.
    hit = await h.gateway.complete(request(trace_id=TRACE_ID, operation="rag-answer"))

    assert len(h.provider.calls) == 1
    [key] = cache.sets
    assert is_a_key_of("sunrise-clinic", key)
    assert cache.gets == [key, key]
    assert hit.content == first.content  # thinking blocks included, unchanged
    assert (hit.usage, hit.cost_usd, hit.cost_complete) == (Usage(), Decimal(0), True)
    assert (hit.model_answered, hit.stop_reason) == ("model-a", "end_turn")
    miss_record, hit_record = read_records()
    assert (miss_record.cache_hit, miss_record.attempts) == (False, 1)
    # The decided hit record (2026-10-15), exactly:
    assert hit_record.model_dump(exclude={"latency_ms"}) == {
        "record_version": 1,
        "business_id": "sunrise-clinic",
        "alias": "small",
        "model_requested": "model-a",
        "model_answered": "model-a",
        "catalog_version": "test-2026-10-15",
        "inference_geo": None,
        "outcome": CallOutcome.OK,
        "stop_reason": "end_turn",
        "attempts": 0,
        "retries": 0,
        "repairs": 0,
        "attempt_timeouts": 0,
        "first_try_valid": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": Decimal(0),
        "cost_complete": True,
        "cache_hit": True,
        "request_ids": (),
        "trace_id": TRACE_ID,
        "operation": "rag-answer",
    }


@pytest.mark.anyio
async def test_the_stored_value_carries_the_business_and_the_answer_only() -> None:
    cache = DictCache()
    h = Harness([FakeReply(text=CANARY_COMPLETION)], cache=cache)

    await h.gateway.complete(request())

    [value] = cache.data.values()
    assert CachedResponse.model_validate_json(value) == CachedResponse(
        business_id="sunrise-clinic",
        model_answered="model-a",
        stop_reason="end_turn",
        content=(TextBlock(text=CANARY_COMPLETION),),
    )


@pytest.mark.anyio
async def test_a_structured_hit_is_revalidated_and_returned_without_a_call(
    read_records: ReadRecords,
) -> None:
    cache = DictCache()
    h = Harness([FakeReply(text=VALID)], cache=cache)

    await h.gateway.complete_structured(request(), Answer)
    hit = await h.gateway.complete_structured(request(), Answer)

    assert len(h.provider.calls) == 1
    assert (hit.value, hit.result.cost_usd) == (VALID_ANSWER, Decimal(0))
    _, hit_record = read_records()
    assert (hit_record.cache_hit, hit_record.attempts, hit_record.first_try_valid) == (
        True,
        0,
        None,
    )


@pytest.mark.anyio
async def test_a_cached_answer_the_schema_no_longer_accepts_is_a_miss() -> None:
    cache = DictCache()
    h = Harness([FakeReply(text=VALID), FakeReply(text=VALID)], cache=cache)
    await h.gateway.complete_structured(request(), Answer)
    [key] = cache.sets
    stale = CachedResponse(
        business_id="sunrise-clinic",
        model_answered="model-a",
        stop_reason="end_turn",
        content=(TextBlock(text='{"answer": 1}'),),
    )
    cache.data[key] = stale.model_dump_json()

    answer = await h.gateway.complete_structured(request(), Answer)

    assert answer.value == VALID_ANSWER
    assert len(h.provider.calls) == 2
    assert CachedResponse.model_validate_json(cache.data[key]).content == (TextBlock(text=VALID),)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(f"not json {CANARY_COMPLETION}", id="not-json"),
        pytest.param('{"business_id": "sunrise-clinic"}', id="missing-fields"),
        pytest.param(
            json.dumps(
                {
                    "business_id": "sunrise-clinic",
                    "model_answered": "model-a",
                    "stop_reason": "refusal",
                    "content": [{"type": "text", "text": CANARY_COMPLETION}],
                }
            ),
            id="a-refusal",
        ),
        pytest.param(
            json.dumps(
                {
                    "business_id": "sunrise-clinic",
                    "model_answered": "model-a",
                    "stop_reason": "end_turn",
                    "content": [],
                    "prompt": CANARY_PROMPT,
                }
            ),
            id="an-unknown-field",
        ),
    ],
)
async def test_an_invalid_cached_value_is_a_miss(
    stored: str, caplog: pytest.LogCaptureFixture, read_records: ReadRecords
) -> None:
    cache = DictCache()
    h = Harness([FakeReply(), FakeReply(text="fresh")], cache=cache)
    await h.gateway.complete(request())
    [key] = cache.sets
    cache.data[key] = stored

    result = await h.gateway.complete(request())

    assert (result.text, len(h.provider.calls)) == ("fresh", 2)
    assert CachedResponse.model_validate_json(cache.data[key]).content == (TextBlock(text="fresh"),)
    assert [r.cache_hit for r in read_records()] == [False, False]
    lines = "".join(JsonFormatter().format(r) for r in caplog.records)
    assert CANARY_COMPLETION not in lines
    assert CANARY_PROMPT not in lines


@pytest.mark.anyio
async def test_a_stored_value_of_another_business_is_never_served() -> None:
    cache = DictCache()
    h = Harness([FakeReply(), FakeReply(text="for sunrise")], cache=cache)
    await h.gateway.complete(request())
    [key] = cache.sets
    planted = CachedResponse(
        business_id="paws-and-co",
        model_answered="model-a",
        stop_reason="end_turn",
        content=(TextBlock(text="for paws"),),
    )
    cache.data[key] = planted.model_dump_json()

    result = await h.gateway.complete(request())

    assert (result.text, len(h.provider.calls)) == ("for sunrise", 2)
    assert CachedResponse.model_validate_json(cache.data[key]).business_id == "sunrise-clinic"


@pytest.mark.anyio
async def test_two_businesses_get_two_keys_and_never_share_a_hit() -> None:
    cache = DictCache()
    h = Harness([FakeReply(text="for sunrise"), FakeReply(text="for paws")], cache=cache)
    sunrise, paws = request(), request(business_id="paws-and-co")

    assert (await h.gateway.complete(sunrise)).text == "for sunrise"
    assert (await h.gateway.complete(paws)).text == "for paws"  # the same request: still a miss
    assert (await h.gateway.complete(sunrise)).text == "for sunrise"  # hits, each its own
    assert (await h.gateway.complete(paws)).text == "for paws"

    assert len(h.provider.calls) == 2
    sunrise_key, paws_key = cache.sets
    assert is_a_key_of("sunrise-clinic", sunrise_key)
    assert is_a_key_of("paws-and-co", paws_key)
    assert sunrise_key.split(":")[1] != paws_key.split(":")[1]  # the hash differs too


@pytest.mark.anyio
async def test_two_schemas_and_no_schema_get_three_keys() -> None:
    cache = DictCache()
    h = Harness([FakeReply(text=VALID)] * 3, cache=cache)

    await h.gateway.complete_structured(request(), Answer)
    await h.gateway.complete_structured(request(), Brief)
    await h.gateway.complete(request())

    assert len(h.provider.calls) == 3
    assert len(set(cache.sets)) == 3


def provider_request(**overrides: Any) -> ProviderRequest:
    fields: dict[str, Any] = {
        "model": "model-a",
        "effort": "low",
        "max_tokens": 100,
        "system": "Be brief.",
        "messages": [{"role": "user", "content": "When do you open?"}],
    }
    return ProviderRequest.model_validate(fields | overrides)


def key(**overrides: Any) -> str:
    fields: dict[str, Any] = {
        "business_id": "sunrise-clinic",
        "catalog_version": "test-2026-10-15",
        "request": provider_request(),
        "schema": None,
    }
    return cache_key(**(fields | overrides))


def test_the_cache_key_is_the_business_and_a_sha256_of_canonical_json() -> None:
    payload = {
        "business_id": "sunrise-clinic",
        "catalog_version": "test-2026-10-15",
        "request": {
            "effort": "low",
            "max_tokens": 100,
            "messages": [{"content": "When do you open?", "role": "user"}],
            "model": "model-a",
            "system": "Be brief.",
        },
        "schema": None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    assert key() == f"sunrise-clinic:{hashlib.sha256(canonical.encode()).hexdigest()}"
    assert key() == key()  # deterministic


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"business_id": "paws-and-co"}, id="business"),
        pytest.param({"catalog_version": "test-2026-10-16"}, id="catalog-version"),
        pytest.param({"request": provider_request(model="model-b")}, id="model"),
        pytest.param({"request": provider_request(effort="medium")}, id="effort"),
        pytest.param({"request": provider_request(max_tokens=99)}, id="max-tokens"),
        pytest.param({"request": provider_request(system=None)}, id="system"),
        pytest.param(
            {"request": provider_request(messages=[{"role": "user", "content": "Open now?"}])},
            id="messages",
        ),
        pytest.param({"schema": "tests.Answer:{}"}, id="schema"),
    ],
)
def test_every_part_of_the_request_changes_the_key(change: dict[str, Any]) -> None:
    assert key(**change) != key()


@pytest.mark.anyio
async def test_a_cache_get_error_is_a_miss_that_writes_nothing(
    caplog: pytest.LogCaptureFixture, read_records: ReadRecords
) -> None:
    cache = DictCache(fail_get=True)
    h = Harness([FakeReply(text=CANARY_COMPLETION)], cache=cache)

    result = await h.gateway.complete(request())

    assert result.text == CANARY_COMPLETION  # the model was called; nothing was raised
    assert (len(cache.gets), cache.sets, cache.data) == (1, [], {})
    record = the_record(read_records)
    assert (record.outcome, record.cache_hit, record.attempts) == (CallOutcome.OK, False, 1)
    assert CACHE_ERROR_TEXT not in "".join(JsonFormatter().format(r) for r in caplog.records)


@pytest.mark.anyio
async def test_a_cache_set_error_never_reaches_the_caller(
    caplog: pytest.LogCaptureFixture, read_records: ReadRecords
) -> None:
    cache = DictCache(fail_set=True)
    h = Harness([FakeReply(text=CANARY_COMPLETION), FakeReply()], cache=cache)

    result = await h.gateway.complete(request())
    await h.gateway.complete(request())  # nothing was written: a miss again

    assert result.text == CANARY_COMPLETION
    assert (len(cache.sets), cache.data, len(h.provider.calls)) == (2, {}, 2)
    assert [r.outcome for r in read_records()] == [CallOutcome.OK, CallOutcome.OK]
    assert CACHE_ERROR_TEXT not in "".join(JsonFormatter().format(r) for r in caplog.records)


@pytest.mark.anyio
async def test_a_cache_set_that_hangs_is_cut_at_the_deadline_and_the_answer_kept(
    read_records: ReadRecords,
) -> None:
    cache = DictCache(hang_set=True)
    h = Harness([FakeReply(text=CANARY_COMPLETION)], cache=cache, deadline_s=0.05)

    result = await h.gateway.complete(request())

    assert result.text == CANARY_COMPLETION  # paid for, so never lost to the cache
    assert (len(cache.sets), cache.data) == (1, {})
    assert the_record(read_records).outcome is CallOutcome.OK


@pytest.mark.anyio
async def test_a_cache_get_that_hangs_uses_up_the_deadline(read_records: ReadRecords) -> None:
    # Documented in cache.py: M9's store must bound its own I/O, or this is what a slow store costs.
    cache = DictCache(hang_get=True)
    h = Harness([], cache=cache, deadline_s=0.05)

    with pytest.raises(DeadlineExceeded):
        await h.gateway.complete(request())

    assert (h.provider.calls, cache.sets, cache.data) == ([], [], {})
    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.cost_usd, record.cost_complete) == (
        CallOutcome.DEADLINE_EXCEEDED,
        0,
        Decimal(0),
        True,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "step",
    [
        pytest.param(FakeReply(stop_reason="refusal"), id="refusal"),
        pytest.param(FakeReply(stop_reason="max_tokens"), id="max-tokens"),
        pytest.param(FakeReply(stop_reason="model_context_window_exceeded"), id="context-window"),
        pytest.param(FakeReply(stop_reason="tool_use"), id="odd-stop-reason"),
        pytest.param(Transient(status_code=529, retryable=False), id="transient"),
        pytest.param(InvalidRequest(status_code=400), id="invalid-request"),
        pytest.param(HANG, id="deadline"),
    ],
)
async def test_refusals_truncations_and_errors_are_never_cached(step: FakeStep) -> None:
    cache = DictCache()
    h = Harness([step], cache=cache, attempt_timeout_s=5.0, deadline_s=0.02)

    with pytest.raises(GatewayError):
        await h.gateway.complete(request())

    assert (cache.sets, cache.data) == ([], {})


@pytest.mark.anyio
async def test_a_validation_failure_is_never_cached() -> None:
    cache = DictCache()
    h = Harness([FakeReply(text="{}"), FakeReply(text="{}")], cache=cache)

    with pytest.raises(StructuredOutputInvalid):
        await h.gateway.complete_structured(request(), Answer)

    assert (cache.sets, cache.data) == ([], {})


@pytest.mark.anyio
async def test_a_pre_call_rejection_never_touches_the_cache(read_records: ReadRecords) -> None:
    cache = DictCache()
    h = Harness([], cache=cache)

    with pytest.raises(InvalidRequest):
        await h.gateway.complete(request(max_tokens=101))

    assert (cache.gets, cache.sets) == ([], [])
    assert the_record(read_records).attempts == 0


@pytest.mark.anyio
async def test_a_cancel_during_the_cache_read_propagates_with_one_record(
    read_records: ReadRecords,
) -> None:
    entered = asyncio.Event()

    class HangingCache(DictCache):
        async def get(self, key: str) -> str | None:
            entered.set()
            await asyncio.Event().wait()  # never set
            return None

    h = Harness([], cache=HangingCache())
    task = asyncio.create_task(h.gateway.complete(request()))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    record = the_record(read_records)
    assert (record.outcome, record.attempts, record.cost_complete) == (
        CallOutcome.CANCELLED,
        0,
        True,
    )
