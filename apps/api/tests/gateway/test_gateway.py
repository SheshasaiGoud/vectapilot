"""The gateway (`gateway/gateway.py`): whole logical calls on the scripted fake provider.

Nothing here reaches the network or waits long. Backoff waits go to `FakeTime` (an injected clock
and sleep: a wait moves the clock, nothing sleeps). Only the attempt timeout and the deadline are
real `asyncio.timeout`s, of a few hundredths of a second, which end the fake's `HANG`.

Most tests read the `llm_call` records back from the log (`read_records`), which also checks that
the gateway logged nothing else.
"""

import asyncio
import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any, Final

import pytest
from pydantic import SecretStr, ValidationError

from tests.conftest import make_settings
from vectapilot_api.gateway import gateway as gateway_module
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
    Transient,
)
from vectapilot_api.gateway.gateway import LLM_CALL, LLMGateway, build_gateway, build_provider
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import (
    HANG,
    OTHER_MODEL,
    FakeProvider,
    FakeReply,
    FakeStep,
)
from vectapilot_api.gateway.types import (
    CallOutcome,
    CallRecord,
    LLMRequest,
    RetryPolicy,
    StopReason,
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
    `sleep` replaces the fake time's sleep (to block or fail inside a backoff wait)."""

    def __init__(
        self,
        script: list[FakeStep],
        *,
        attempt_timeout_s: float = ATTEMPT_TIMEOUT_S,
        deadline_s: float = 5.0,
        max_retries: int = 2,
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
    assert (record.outcome, record.retries, record.attempt_timeouts) == (
        CallOutcome.PROVIDER_ERROR,
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

    assert the_record(read_records).outcome is CallOutcome.PROVIDER_ERROR  # a bug, not a deadline


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
    # In PR 6 any response ends the call, so only a direct test reaches a second term; PR 7's
    # repair is the first path through the gateway that does.
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
async def test_an_unexpected_provider_exception_propagates_and_is_recorded(
    read_records: ReadRecords,
) -> None:
    # The fake cannot build a response with a model id that is not id-shaped; the adapter would
    # turn that into a ProviderError, the fake lets the ValidationError out. Either way: one record.
    h = Harness([FakeReply(model=f"odd model {CANARY_COMPLETION}")])

    with pytest.raises(ValidationError):
        await h.gateway.complete(request())

    record = the_record(read_records)
    assert (record.outcome, record.model_answered) == (CallOutcome.PROVIDER_ERROR, None)
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
    # Decision 3 (every logical call, one record); no model requested (proposed, shesha confirms).
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
    assert (record.retries, record.attempt_timeouts, record.input_tokens, record.cost_usd) == (
        0,
        0,
        0,
        Decimal(0),
    )
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
    )

    async with build_gateway(settings) as gateway:
        await gateway.complete(request(alias="small"))
        with pytest.raises(Transient):  # llm_max_retries=0: not retried
            await gateway.complete(request(alias="large"))
        assert fake.closed is False

    assert fake.closed is True
    # Each limit reaches its own parameter (swapped, no retry could ever fit in the deadline);
    # clock, sleep and rand are left to their production defaults.
    assert limits == [
        {
            "attempt_timeout_s": 7.0,
            "deadline_s": 11.0,
            "retry_policy": RetryPolicy(max_retries=0, base_delay_s=0.25, max_delay_s=4.0),
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
