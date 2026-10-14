"""H2 tests: `backoff_delay` and `call_with_retries` (guide: docs/learning/m1-gateway.md, H2).

Both are [HAND-WRITE]: shesha writes them. Until then they raise NotImplementedError, so the
whole module is marked `xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong behaviour → a normal failure (only NotImplementedError counts as expected);
- implemented, right behaviour → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, delete the `pytestmark` line below and run again: every
test must then simply pass. Do not edit the tests to make them pass; change the code.

Nothing here really waits: `FakeTime.sleep` records the wait and moves a fake clock forward.
"""

import asyncio

import pytest

from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    SpendLimitReached,
    Transient,
)
from vectapilot_api.gateway.retry import backoff_delay, call_with_retries
from vectapilot_api.gateway.types import RetryPolicy

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H2: shesha implements the retry — see docs/learning/m1-gateway.md, H2",
)

# 2 retries; backoff upper bounds 0.5 s, 1 s, 2 s, 4 s, then capped at 8 s.
POLICY = RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0)
START = 100.0  # the fake clock's start time; deadlines below are START + something


class FakeTime:
    """A clock and a sleep that never wait: sleeping records the wait and moves the clock."""

    def __init__(self) -> None:
        self.now = START
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class CancelledWhileSleeping(FakeTime):
    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        raise asyncio.CancelledError


class Script:
    """A fake model call that plays its outcomes in order: exceptions are raised, text returned.

    Each call takes `takes_s` seconds on the fake clock. Calling it more often than scripted fails
    the test.
    """

    def __init__(self, time: FakeTime, *outcomes: str | BaseException, takes_s: float = 0.0):
        self.time = time
        self.outcomes = outcomes
        self.takes_s = takes_s
        self.calls = 0

    async def __call__(self) -> str:
        if self.calls >= len(self.outcomes):
            raise AssertionError("called more often than the test scripted")
        outcome = self.outcomes[self.calls]
        self.calls += 1
        self.time.now += self.takes_s
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def run(
    script: Script, time: FakeTime, *, deadline: float = START + 60.0, rand: float = 1.0
) -> str:
    # rand=1.0 makes every backoff wait equal to its upper bound, so the waits are predictable.
    return await call_with_retries(
        script, POLICY, deadline=deadline, clock=time.clock, sleep=time.sleep, rand=lambda: rand
    )


# ── backoff_delay ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("attempt", "upper_bound"),
    [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 8.0), (10, 8.0)],
)
def test_backoff_with_rand_1_is_the_capped_exponential(attempt: int, upper_bound: float) -> None:
    assert backoff_delay(attempt, POLICY, 1.0) == upper_bound


def test_backoff_cap_applies_between_powers_of_two() -> None:
    # base 1 s, cap 3 s: attempt 2 would be 4 s uncapped → 3 s.
    policy = RetryPolicy(max_retries=3, base_delay_s=1.0, max_delay_s=3.0)

    assert backoff_delay(2, policy, 1.0) == 3.0


@pytest.mark.parametrize("attempt", [0, 3, 10])
def test_backoff_with_rand_0_is_zero(attempt: int) -> None:
    assert backoff_delay(attempt, POLICY, 0.0) == 0.0


def test_backoff_grows_with_rand() -> None:
    assert backoff_delay(3, POLICY, 0.25) < backoff_delay(3, POLICY, 0.75)


def test_backoff_for_a_huge_attempt_is_capped_not_an_overflow() -> None:
    assert backoff_delay(5000, POLICY, 1.0) == 8.0


def test_backoff_returns_a_float() -> None:
    assert type(backoff_delay(0, POLICY, 1.0)) is float


# ── call_with_retries: success and retries ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_success_on_the_first_try_calls_once_and_never_sleeps() -> None:
    time = FakeTime()
    script = Script(time, "answer")

    assert await run(script, time) == "answer"
    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_a_transient_error_then_success_retries_once() -> None:
    time = FakeTime()
    script = Script(time, Transient(status_code=529), "answer")

    assert await run(script, time) == "answer"
    assert script.calls == 2
    assert time.slept == [0.5]  # the bound for retry 0, as rand is 1.0


@pytest.mark.anyio
async def test_two_transient_errors_then_success_uses_both_retries() -> None:
    time = FakeTime()
    script = Script(time, Transient(status_code=503), Transient(status_code=529), "answer")

    assert await run(script, time) == "answer"
    assert script.calls == 3
    assert time.slept == [0.5, 1.0]  # retry 0, then retry 1


@pytest.mark.anyio
async def test_when_retries_run_out_the_last_transient_error_is_raised() -> None:
    time = FakeTime()
    errors = [Transient(status_code=529, request_id=f"req_{n}") for n in range(3)]
    script = Script(time, *errors, "never reached")

    with pytest.raises(Transient) as caught:
        await run(script, time)

    assert caught.value is errors[-1]  # the same object, not a copy or the first error
    assert script.calls == 3  # 1 + max_retries
    assert time.slept == [0.5, 1.0]


@pytest.mark.anyio
async def test_a_provider_error_marked_retryable_is_retried() -> None:
    time = FakeTime()  # x-should-retry: true turns a ProviderError retryable
    script = Script(time, ProviderError(status_code=500, retryable=True), "answer")

    assert await run(script, time) == "answer"
    assert script.calls == 2
    assert time.slept == [0.5]  # no Retry-After on a ProviderError: the backoff for retry 0


# ── call_with_retries: errors that are never retried ─────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(SpendLimitReached(status_code=429), id="spend-limit"),
        pytest.param(BillingError(status_code=402), id="billing"),
        pytest.param(AuthenticationFailed(status_code=401), id="auth"),
        pytest.param(InvalidRequest(status_code=400), id="invalid-request"),
        pytest.param(ProviderError(status_code=418), id="provider-error"),
        pytest.param(ModelRefused(), id="refusal"),
        pytest.param(OutputTruncated(stop_reason="max_tokens"), id="truncated"),
        pytest.param(Transient(status_code=503, retryable=False), id="x-should-retry-false"),
    ],
)
async def test_a_non_retryable_error_is_raised_after_exactly_one_call(error: GatewayError) -> None:
    time = FakeTime()
    script = Script(time, error, "answer")  # a retry would wrongly return "answer"

    with pytest.raises(GatewayError) as caught:
        await run(script, time)

    assert caught.value is error
    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_an_exception_that_is_not_a_gateway_error_propagates_after_one_call() -> None:
    time = FakeTime()
    # Not RuntimeError: NotImplementedError is a RuntimeError, so the stub would satisfy the test.
    bug = ValueError("a bug, not a network blip")
    script = Script(time, bug, "answer")

    with pytest.raises(ValueError, match="a bug") as caught:
        await run(script, time)

    assert caught.value is bug
    assert script.calls == 1


# ── call_with_retries: Retry-After ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_retry_after_is_waited_for_instead_of_the_backoff() -> None:
    time = FakeTime()
    script = Script(time, Transient(status_code=429, retry_after=3.0), "answer")

    assert await run(script, time) == "answer"
    assert time.slept == [3.0]  # exactly Retry-After, not the 0.5 s backoff


@pytest.mark.anyio
async def test_a_retry_after_wait_still_counts_as_a_retry_for_the_backoff() -> None:
    # Retry 0 waits the server's 3 s; the next wait is the backoff for retry 1 (1.0 s), not 0.
    time = FakeTime()
    script = Script(
        time, Transient(status_code=429, retry_after=3.0), Transient(status_code=529), "answer"
    )

    assert await run(script, time) == "answer"
    assert script.calls == 3
    assert time.slept == [3.0, 1.0]


@pytest.mark.anyio
async def test_retry_after_equal_to_the_cap_is_still_waited_for() -> None:
    time = FakeTime()
    script = Script(time, Transient(status_code=429, retry_after=8.0), "answer")

    assert await run(script, time) == "answer"
    assert time.slept == [8.0]


@pytest.mark.anyio
async def test_retry_after_above_the_cap_is_not_retried() -> None:
    time = FakeTime()
    error = Transient(status_code=429, retry_after=8.5)
    script = Script(time, error, "answer")

    with pytest.raises(Transient) as caught:
        await run(script, time)

    assert caught.value is error
    assert script.calls == 1
    assert time.slept == []


# ── call_with_retries: the deadline ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_first_attempt_is_made_even_when_the_deadline_has_passed() -> None:
    # The deadline decides only whether a wait for a retry may start; the gateway's outer timeout
    # owns running work (contract point 1).
    time = FakeTime()
    script = Script(time, "answer")

    assert await run(script, time, deadline=START - 1.0) == "answer"
    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_a_backoff_that_would_end_past_the_deadline_is_not_slept() -> None:
    time = FakeTime()
    error = Transient(status_code=529)
    script = Script(time, error, "answer")

    with pytest.raises(Transient) as caught:
        await run(script, time, deadline=START + 0.3)  # the 0.5 s wait would end at START + 0.5

    assert caught.value is error
    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_a_retry_after_that_would_end_past_the_deadline_is_not_slept() -> None:
    time = FakeTime()
    error = Transient(status_code=429, retry_after=3.0)
    script = Script(time, error, "answer")

    with pytest.raises(Transient) as caught:
        await run(script, time, deadline=START + 2.0)

    assert caught.value is error
    assert time.slept == []


@pytest.mark.anyio
async def test_a_wait_ending_exactly_at_the_deadline_is_not_slept() -> None:
    # The next attempt would start with no time left, so the wait is pointless.
    time = FakeTime()
    script = Script(time, Transient(status_code=529), "answer")

    with pytest.raises(Transient):
        await run(script, time, deadline=START + 0.5)  # 0.5 s wait ends at START + 0.5

    assert time.slept == []


@pytest.mark.anyio
async def test_the_clock_is_read_after_the_failed_attempt() -> None:
    # The attempt itself takes 0.4 s: it fails at START + 0.4, and a 0.5 s wait would end at
    # START + 0.9, past the deadline. Reading the clock only once, at the start, would miss that.
    time = FakeTime()
    script = Script(time, Transient(status_code=529), "answer", takes_s=0.4)

    with pytest.raises(Transient):
        await run(script, time, deadline=START + 0.8)

    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_the_deadline_can_stop_a_later_retry() -> None:
    # Retry 0 waits 0.5 s (ends at START + 0.5, before START + 1.0). Retry 1 would wait 1.0 s
    # (ends at START + 1.5, past it), so the second error is raised.
    time = FakeTime()
    first, second = Transient(request_id="req_1"), Transient(request_id="req_2")
    script = Script(time, first, second, "answer")

    with pytest.raises(Transient) as caught:
        await run(script, time, deadline=START + 1.0)

    assert caught.value is second
    assert script.calls == 2
    assert time.slept == [0.5]


# ── call_with_retries: cancellation always propagates ────────────────────────────────────────


@pytest.mark.anyio
async def test_cancelled_error_from_the_call_propagates() -> None:
    time = FakeTime()
    script = Script(time, asyncio.CancelledError(), "answer")

    with pytest.raises(asyncio.CancelledError):
        await run(script, time)

    assert script.calls == 1
    assert time.slept == []


@pytest.mark.anyio
async def test_cancelled_error_during_the_wait_propagates() -> None:
    time = CancelledWhileSleeping()
    script = Script(time, Transient(status_code=529), "answer")

    with pytest.raises(asyncio.CancelledError):
        await run(script, time)

    assert script.calls == 1  # no attempt after the cancel
    assert time.slept == [0.5]
