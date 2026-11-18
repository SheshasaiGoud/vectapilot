"""M7 PR 5 (G9): the limiter's and the budget's Lua scripts against real Redis. Integration only,
on the isolated logical database (`tests/redis_isolation.py`), with unique ids.

What the stub tests in `test_limits.py` cannot show: every window or none under concurrent
requests, expiry, a settle after UTC midnight on the reserved day's key, a reservation that never
goes over the cap, a turn's reservations summed across the `small` and `large` aliases, and the
script reloaded after a NOSCRIPT.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from tests.conftest import integration
from tests.redis_isolation import isolated_redis, unique_business
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import LLMRequest, RetryPolicy, Usage
from vectapilot_api.guards.budget import BudgetedGateway, track_budget
from vectapilot_api.guards.limits import (
    BUDGET_TTL_SECONDS,
    GuardRefused,
    LimitPolicy,
    RedisLimiter,
    RefusalKind,
    Window,
    budget_key,
    rate_key,
)
from vectapilot_api.guards.types import RouteSlug

pytestmark = [pytest.mark.anyio, integration]

ROUTE = RouteSlug.AGENT


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    async with isolated_redis() as client:
        yield client


def limiter(redis: Redis, policy: LimitPolicy, **kwargs: object) -> RedisLimiter:
    return RedisLimiter(redis, policy, timeout_s=2.0, **kwargs)  # type: ignore[arg-type]


async def outcome(call: object) -> str:
    try:
        await call  # type: ignore[misc]
    except GuardRefused as refused:
        return refused.kind.value
    return "allowed"


async def count(redis: Redis, key: str) -> int:
    value = await redis.get(key)
    return 0 if value is None else int(value)


async def test_concurrent_requests_count_in_every_window_or_none(redis: Redis) -> None:
    business = unique_business()
    policy = LimitPolicy(Window(10, 60), Window(3, 60), budget_micro_usd=1)
    built = limiter(redis, policy)

    # One customer, 20 at once: 3 pass; the 17 refused by the customer window never count in
    # the business window.
    first = await asyncio.gather(
        *(built.admit(business, ROUTE, "cust-001") for _ in range(20)), return_exceptions=True
    )
    assert sum(result is None for result in first) == 3
    assert await count(redis, rate_key(business, ROUTE)) == 3
    assert await count(redis, rate_key(business, ROUTE, "cust-001")) == 3

    # 20 different customers at once: the business window has 7 left, and only those count.
    customers = [f"cust-{n:03d}" for n in range(100, 120)]
    second = await asyncio.gather(
        *(built.admit(business, ROUTE, c) for c in customers), return_exceptions=True
    )
    assert sum(result is None for result in second) == 7
    assert await count(redis, rate_key(business, ROUTE)) == 10
    per_customer = [await count(redis, rate_key(business, ROUTE, c)) for c in customers]
    assert sum(per_customer) == 7 and set(per_customer) <= {0, 1}
    refused = [r for r in [*first, *second] if r is not None]
    assert all(isinstance(r, GuardRefused) and r.kind is RefusalKind.LIMIT for r in refused)


async def test_a_window_expires_and_retry_after_says_when(redis: Redis) -> None:
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(2, 1), Window(2, 1), budget_micro_usd=1))
    await built.admit(business, ROUTE)
    await built.admit(business, ROUTE)
    with pytest.raises(GuardRefused) as caught:
        await built.admit(business, ROUTE)
    assert caught.value.retry_after_s == 1
    ttl = await redis.pttl(rate_key(business, ROUTE))
    assert 0 < ttl <= 1000

    await asyncio.sleep(1.1)
    await built.admit(business, ROUTE)  # a new window
    assert await count(redis, rate_key(business, ROUTE)) == 1


async def test_a_window_without_a_ttl_gets_one_instead_of_refusing_forever(redis: Redis) -> None:
    business = unique_business()
    await redis.set(rate_key(business, ROUTE), 5)  # as if a TTL had been lost
    built = limiter(redis, LimitPolicy(Window(5, 60), Window(5, 60), budget_micro_usd=1))
    with pytest.raises(GuardRefused) as caught:
        await built.admit(business, ROUTE)
    assert caught.value.retry_after_s == 60
    assert 0 < await redis.pttl(rate_key(business, ROUTE)) <= 60_000


@pytest.mark.parametrize(("business_ms", "customer_ms"), [(5_000, 55_000), (55_000, 5_000)])
async def test_retry_after_waits_for_the_longest_full_window(
    redis: Redis, business_ms: int, customer_ms: int
) -> None:
    # [diego's PR 5 review] Both windows full: a Retry-After from the first one in KEYS' order
    # (5 s) would only earn a second 429 when the client came back.
    business = unique_business()
    await redis.set(rate_key(business, ROUTE), 10, px=business_ms)
    await redis.set(rate_key(business, ROUTE, "cust-001"), 3, px=customer_ms)
    built = limiter(redis, LimitPolicy(Window(10, 60), Window(3, 60), budget_micro_usd=1))
    with pytest.raises(GuardRefused) as caught:
        await built.admit(business, ROUTE, "cust-001")
    assert caught.value.retry_after_s == 55
    assert await count(redis, rate_key(business, ROUTE)) == 10  # a refusal counts nothing


async def test_concurrent_reservations_never_go_over_the_cap(redis: Redis) -> None:
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=1_000))
    results = await asyncio.gather(*(outcome(built.reserve(business, 30)) for _ in range(50)))

    assert results.count("allowed") == 33  # 33 x 30 = 990 <= 1,000 < 34 x 30
    assert set(results) == {"allowed", "budget"}
    key = budget_key(business, datetime.now(UTC).date())
    assert await count(redis, key) == 990
    assert 0 < await redis.ttl(key) <= BUDGET_TTL_SECONDS
    assert await built.remaining(business) == 10
    await built.reserve(business, 10)  # exactly the remainder fits
    assert await outcome(built.reserve(business, 1)) == "budget"


async def test_a_settle_after_utc_midnight_lands_on_the_reserved_day(redis: Redis) -> None:
    business = unique_business()
    now = [datetime(2026, 11, 18, 23, 59, 59, 900_000, tzinfo=UTC)]
    built = limiter(
        redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=1_000), clock=lambda: now[0]
    )
    reserved = await built.reserve(business, 400)
    now[0] += timedelta(seconds=1)  # the call ends on the next UTC day

    await built.settle(reserved, 150)

    assert await count(redis, "vp:" + business + ":budget:20261118") == 150
    assert await redis.exists("vp:" + business + ":budget:20261119") == 0
    assert await built.remaining(business) == 1_000  # the new day starts untouched


async def test_a_settle_never_recreates_a_vanished_key(redis: Redis) -> None:
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=1_000))
    reserved = await built.reserve(business, 400)
    await redis.delete(reserved.key)  # a Redis recreate zeroes the day (ADR 0018)
    await built.settle(reserved, 100)
    assert await redis.exists(reserved.key) == 0


async def test_a_refund_after_a_recreate_never_takes_the_day_below_zero(redis: Redis) -> None:
    # [diego's PR 5 review] The first reservation vanished with a recreate; its refund must not
    # eat the second one's and leave the day negative, which would raise the cap.
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=1_000))
    first = await built.reserve(business, 400)
    await redis.delete(first.key)  # a Redis recreate zeroes the day (ADR 0018)
    await built.reserve(business, 100)
    await built.settle(first, 150)  # a delta of -250 on a key that holds 100
    assert await count(redis, first.key) == 0
    assert 0 < await redis.ttl(first.key) <= BUDGET_TTL_SECONDS  # KEEPTTL: the day still expires
    assert await built.remaining(business) == 1_000  # never above the cap


async def test_the_scripts_reload_after_redis_forgets_them(redis: Redis) -> None:
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(5, 60), Window(5, 60), budget_micro_usd=1_000))
    await built.admit(business, ROUTE)
    await redis.script_flush()  # as after a Redis restart: the next EVALSHA is a NOSCRIPT
    await built.admit(business, ROUTE, "cust-001")
    await redis.script_flush()
    reserved = await built.reserve(business, 10)
    await redis.script_flush()
    await built.settle(reserved, 5)
    assert await count(redis, rate_key(business, ROUTE)) == 2
    assert await count(redis, reserved.key) == 5


def _gateway(provider: FakeProvider, built: RedisLimiter) -> BudgetedGateway:
    return BudgetedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay_s=0.01, max_delay_s=0.01),
        max_repairs=1,
        limiter=built,
    )


async def test_a_turns_reservations_sum_across_the_small_and_large_aliases(redis: Redis) -> None:
    business = unique_business()
    built = limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=1_000_000))
    usage = Usage(input_tokens=1_000, output_tokens=200)
    small = _gateway(FakeProvider([FakeReply(usage=usage)] * 2), built)
    large = _gateway(FakeProvider([FakeReply(usage=usage)]), built)
    step = LLMRequest.model_validate(
        {"business_id": business, "alias": "small", "messages": [{"role": "user", "content": "x"}]}
    )
    answer = step.model_copy(update={"alias": "large"})
    key = budget_key(business, datetime.now(UTC).date())

    with track_budget() as tally:
        results = [await small.complete(step), await large.complete(answer)]
        # Mid-turn, the day holds the settled step and the large call's settle: both aliases.
        results.append(await small.complete(step))

    assert tally.reserved_micro_usd == 2 * small.reservation_micro_usd(
        step, None
    ) + large.reservation_micro_usd(answer, None)
    assert tally.settled_micro_usd == await count(redis, key)
    assert 0 < await count(redis, key) < tally.reserved_micro_usd  # settles refund the excess
    assert all(result.cost_complete for result in results)
    assert isinstance(small, LLMGateway)


async def test_a_turn_that_meets_the_cap_mid_way_is_refused_before_its_next_call(
    redis: Redis,
) -> None:
    business = unique_business()
    probe = _gateway(FakeProvider(), limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), 1)))
    step = LLMRequest.model_validate(
        {"business_id": business, "alias": "small", "messages": [{"role": "user", "content": "x"}]}
    )
    one = probe.reservation_micro_usd(step, None)
    # Room for exactly one worst case: the second call is refused, never sent.
    built = limiter(redis, LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=one))
    provider = FakeProvider([FakeReply(usage=Usage(input_tokens=1, output_tokens=1))] * 2)
    gateway = _gateway(provider, built)

    with track_budget() as tally:
        await gateway.complete(step)  # settled far below its worst case
        with pytest.raises(GuardRefused):
            await gateway.complete(step.model_copy(update={"alias": "large"}))

    assert len(provider.calls) == 1
    assert tally.refused == 1
