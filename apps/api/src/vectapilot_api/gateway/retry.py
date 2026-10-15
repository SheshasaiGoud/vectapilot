"""H2 — retry with capped, full-jitter backoff. [HAND-WRITE] piece, written by Claude at shesha's
request (2026-10-15); shesha's explain-back is still part of its acceptance.

Guide: docs/learning/m1-gateway.md · Tests: tests/gateway/test_retry.py.

The gateway (PR 6) is the only retry layer: the SDK's own retries are off (ADR 0006). It passes
`time.monotonic`, `asyncio.sleep` and `random.random` in; tests pass fakes, so nothing waits.
Per-attempt accounting (usage, request ids, attempt timeouts, the retry count) happens inside the
`call` the gateway passes in, not here, so these signatures stay as they are once H2 is merged.
"""

from collections.abc import Awaitable, Callable

from vectapilot_api.gateway.errors import GatewayError, Transient
from vectapilot_api.gateway.types import RetryPolicy


def backoff_delay(attempt: int, policy: RetryPolicy, rand: float) -> float:
    """Return how long to wait, in seconds, before retry number `attempt`.

    Contract (full jitter, capped):

    - `attempt` counts the retries already made: 0 before the first retry, 1 before the second,
      and so on. Any `attempt >= 0` works, however large: the cap holds and nothing overflows.
    - The bound: for attempt 0 it is `policy.base_delay_s`; it doubles with each further retry, and
      it never exceeds `policy.max_delay_s`.
    - `rand` is a number in [0.0, 1.0] (in production, `random.random()`). The result lies between
      0.0 and the bound: 0.0 when `rand` is 0.0, the bound itself when `rand` is 1.0, and larger
      for a larger `rand` (full jitter, guide §2.6).
    - The result is a `float`.

    The bounds the tests assume, for base 0.5 s and cap 8 s: attempt 0 → 0.5, 1 → 1.0, 2 → 2.0,
    3 → 4.0, 4 and above → 8.0. For base 1 s and cap 3 s: attempt 2 → 3.0.
    """
    # Double the bound once per earlier retry, but stop as soon as it reaches the cap: computing
    # `base * 2**attempt` directly would overflow a float for a huge `attempt`.
    bound = policy.base_delay_s
    for _ in range(attempt):
        if bound >= policy.max_delay_s:
            break
        bound *= 2
    return float(min(bound, policy.max_delay_s) * rand)  # full jitter: anywhere in [0, bound]


async def call_with_retries[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    deadline: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
    rand: Callable[[], float],
) -> T:
    """Await `call()` and retry it on retryable gateway errors; return the first success.

    Contract:

    1. The first attempt is always made, even if the deadline has already passed: the gateway's
       outer `asyncio.timeout` owns the deadline for running work. The first successful result is
       returned at once.
    2. Only a `GatewayError` whose `retryable` is True is retried. Every other exception — the
       non-retryable gateway errors (`SpendLimitReached`, `BillingError`, `AuthenticationFailed`,
       `InvalidRequest`, a default `ProviderError`, …) and any non-gateway exception — propagates
       unchanged after that one call.
    3. At most `policy.max_retries` retries, so at most `1 + policy.max_retries` calls. When no
       retry is left, the last error is raised: the same object, unchanged. Nothing is logged or
       printed, and no error is wrapped or chained to another: the gateway records the outcome.
    4. The wait before retry number k (k = 0, 1, …; every retry counts, whichever rule chose its
       wait):
       - a `Transient` with `retry_after` set: exactly `retry_after` seconds if it is at most
         `policy.max_delay_s`; above the cap there is no retry, and the error is raised;
       - otherwise: the full-jitter backoff for retry k (`backoff_delay`).
    5. `deadline` is an absolute time on `clock()`'s scale (in production, `time.monotonic()` at
       the start plus the deadline in seconds). After a failed attempt, read `clock()`: a wait is
       slept only if it ends strictly before the deadline. Otherwise nothing is slept and the last
       error is raised.
    6. Time is read only through `clock()`, waiting happens only through `await sleep(seconds)`,
       randomness comes only from `rand()`.
    7. `asyncio.CancelledError` is never caught, retried or swallowed — whether it comes from
       `call()` or from `sleep()` — so cancellation and the gateway's `asyncio.timeout` keep
       working.
    """
    retries = 0
    while True:
        try:
            return await call()
        except GatewayError as error:  # CancelledError is a BaseException: never caught here
            if not error.retryable or retries == policy.max_retries:
                raise  # the same object, unchanged
            failed = error
        # Outside the `except` block on purpose: nothing below can chain onto `failed`.
        wait = _wait_before_retry(failed, retries, policy, rand)
        if wait is None or clock() + wait >= deadline:  # the wait must end strictly before it
            raise failed
        await sleep(wait)
        retries += 1


def _wait_before_retry(
    error: GatewayError, retries: int, policy: RetryPolicy, rand: Callable[[], float]
) -> float | None:
    """Seconds to wait before retry number `retries`, or None if this error must not be retried."""
    if isinstance(error, Transient) and error.retry_after is not None:
        # The server said when to come back. Longer than our cap means give up (fail fast).
        return error.retry_after if error.retry_after <= policy.max_delay_s else None
    return backoff_delay(retries, policy, rand())
