"""The Redis-isolation canaries (M7 PR 5; G12, ADR 0018 part 8): a hard gate, as the database's.

Zero cross-business leaks covers Redis state too: one business exhausting its limit or its day's
budget must leave every other business untouched, and every key in the `vp:` namespace must carry
a business segment (the closed grammar, `guards.limits.KEY_GRAMMAR`). The canaries call the
limiter and the budget directly, so they never wait on H1; red-team category K mirrors them by id
(k-002 the limit, k-003 the budget, k-004 the key scan). Each check proves itself on purpose: the
`test_red_proof_*` tests break one protection and assert the same check turns red.

Integration only, on the isolated logical database with unique business ids
(`tests/redis_isolation.py`): never the app's own database. `scripts/canary_gate.py` counts these
with the rest of `tests/canary/`.
"""

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from tests.conftest import integration
from tests.redis_isolation import isolated_redis, unique_business
from vectapilot_api.guards import limits
from vectapilot_api.guards.limits import (
    GuardRefused,
    LimitPolicy,
    RedisLimiter,
    RefusalKind,
    Window,
    key_business,
)
from vectapilot_api.guards.types import RouteSlug

pytestmark = [pytest.mark.anyio, integration]

POLICY = LimitPolicy(business=Window(3, 60), customer=Window(2, 60), budget_micro_usd=1_000)
CUSTOMER = "cust-001"
"""The same reference in both businesses: its hash must still differ (G9)."""


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    async with isolated_redis() as client:
        yield client


def limiter(redis: Redis) -> RedisLimiter:
    return RedisLimiter(redis, POLICY, timeout_s=2.0)


async def keys_of(redis: Redis, business: str) -> list[str]:
    return [key.decode() async for key in redis.scan_iter(match=f"vp:{business}:*", count=500)]


async def refused(call: object) -> RefusalKind | None:
    try:
        await call  # type: ignore[misc]
    except GuardRefused as refusal:
        return refusal.kind
    return None


async def exhaust_limits(built: RedisLimiter, business: str) -> bool:
    """Fill A's business window and its customer's window on every route; True once each is
    refused (the positive control: A really is exhausted)."""
    exhausted = True
    for route in RouteSlug:
        for _ in range(POLICY.customer.limit):
            await built.admit(business, route, CUSTOMER)
        for _ in range(POLICY.business.limit - POLICY.customer.limit):
            await built.admit(business, route)
        exhausted &= await refused(built.admit(business, route)) is RefusalKind.LIMIT
        exhausted &= await refused(built.admit(business, route, CUSTOMER)) is RefusalKind.LIMIT
    return exhausted


async def b_is_untouched_by_limits(built: RedisLimiter, redis: Redis, b: str) -> bool:
    if await keys_of(redis, b):
        return False
    for route in RouteSlug:
        for _ in range(POLICY.customer.limit):
            if await refused(built.admit(b, route, CUSTOMER)) is not None:
                return False
    return True


async def test_k_002_a_businesss_exhausted_limit_leaves_another_business_untouched(
    redis: Redis,
) -> None:
    a, b = unique_business("a"), unique_business("b")
    built = limiter(redis)

    assert await exhaust_limits(built, a)
    assert await b_is_untouched_by_limits(built, redis, b)
    assert await keys_of(redis, a)  # A's own keys exist: the arrangement is real


async def test_red_proof_a_limit_key_without_its_business_leaks_between_businesses(
    redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = limits.rate_key
    monkeypatch.setattr(
        limits, "rate_key", lambda business, route, ref=None: real("shared", route, ref)
    )
    a, b = unique_business("a"), unique_business("b")
    built = limiter(redis)

    assert await exhaust_limits(built, a)
    assert not await b_is_untouched_by_limits(built, redis, b)


async def exhaust_budget(built: RedisLimiter, business: str) -> bool:
    await built.reserve(business, POLICY.budget_micro_usd)
    return await refused(built.reserve(business, 1)) is RefusalKind.BUDGET


async def b_is_untouched_by_budget(built: RedisLimiter, redis: Redis, b: str) -> bool:
    if await keys_of(redis, b) or await built.remaining(b) != POLICY.budget_micro_usd:
        return False
    return await refused(built.reserve(b, POLICY.budget_micro_usd)) is None


async def test_k_003_a_businesss_exhausted_budget_leaves_another_business_untouched(
    redis: Redis,
) -> None:
    a, b = unique_business("a"), unique_business("b")
    built = limiter(redis)

    assert await exhaust_budget(built, a)
    assert await built.remaining(a) == 0
    assert await b_is_untouched_by_budget(built, redis, b)


async def test_red_proof_a_budget_key_without_its_business_leaks_between_businesses(
    redis: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = limits.budget_key
    monkeypatch.setattr(limits, "budget_key", lambda business, day: real("shared", day))
    a, b = unique_business("a"), unique_business("b")
    built = limiter(redis)

    assert await exhaust_budget(built, a)
    assert not await b_is_untouched_by_budget(built, redis, b)


async def vp_keys(redis: Redis) -> list[str]:
    return [key.decode() async for key in redis.scan_iter(match="vp:*", count=500)]


def keys_without_a_business(keys: list[str]) -> list[str]:
    return [key for key in keys if key_business(key) is None]


async def arrange_every_kind_of_key(built: RedisLimiter, businesses: list[str]) -> None:
    for business in businesses:
        for route in RouteSlug:
            await built.admit(business, route, CUSTOMER)
        await built.reserve(business, 1)


async def customer_segments(redis: Redis, business: str) -> set[str]:
    keys = await keys_of(redis, business)
    return {key.rsplit(":", 1)[1] for key in keys if ":rl:" in key and not key.endswith(":all")}


async def test_k_004_every_vp_key_has_a_business_segment(redis: Redis) -> None:
    businesses = [unique_business("a"), unique_business("b")]
    await arrange_every_kind_of_key(limiter(redis), businesses)

    keys = await vp_keys(redis)
    # Positive control: every kind of key exists for both: 5 routes x 2 windows + 1 budget.
    assert {key_business(key) for key in keys} == set(businesses)
    assert len(keys) == 2 * (2 * len(RouteSlug) + 1)
    assert keys_without_a_business(keys) == []
    # The same reference in two businesses never shares a customer segment (G9's hash).
    a_hashes = await customer_segments(redis, businesses[0])
    b_hashes = await customer_segments(redis, businesses[1])
    assert len(a_hashes) == len(b_hashes) == 1
    assert a_hashes.isdisjoint(b_hashes)


async def test_red_proof_the_scan_sees_a_key_without_a_business(redis: Redis) -> None:
    businesses = [unique_business("a")]
    await arrange_every_kind_of_key(limiter(redis), businesses)
    await redis.set("vp:rl:agent:all", 1)  # a key some later code wrote without a business

    assert keys_without_a_business(await vp_keys(redis)) == ["vp:rl:agent:all"]


async def test_the_scan_reads_only_the_vp_namespace(
    redis: Redis,
) -> None:
    # G12: the scan checks `vp:` only (a later job queue may share Redis).
    await redis.set("queue:jobs", 1)
    assert await vp_keys(redis) == []
