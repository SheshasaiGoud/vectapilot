"""M7 PR 5 (G9, G12, G3): the limiter's keys, refusals and Redis handling, without Redis.

`RedisLimiter` runs here over a stub client whose scripts return scripted replies, raise or hang:
these tests pin the Python side (the key grammar, the arguments sent, how replies, errors and
timeouts become refusals). The Lua scripts' own semantics (all windows or none, never over the
cap, expiry) are pinned against real Redis in `test_limits_redis.py`.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, ClassVar, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from tests.conftest import make_settings
from vectapilot_api.guards.limits import (
    BUDGET_TTL_SECONDS,
    KEY_GRAMMAR,
    RATE_SCRIPT,
    RESERVE_SCRIPT,
    SETTLE_SCRIPT,
    GuardRefused,
    LimitPolicy,
    RedisLimiter,
    RefusalKind,
    Reserved,
    Window,
    budget_key,
    customer_hash,
    key_business,
    limited_response,
    micro_usd,
    rate_key,
    refusal_decision,
    require_budget,
    utc_day,
)
from vectapilot_api.guards.types import DecidedBy, GuardAction, RouteSlug
from vectapilot_api.intents.policy import Priority

pytestmark = pytest.mark.anyio

NOON: datetime = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
POLICY = LimitPolicy(business=Window(5, 60), customer=Window(2, 60), budget_micro_usd=1_000)
HANG = object()

# ── Keys ─────────────────────────────────────────────────────────────────────────────────────


def test_the_customer_segment_is_16_hex_of_sha256_over_business_and_reference() -> None:
    expected = hashlib.sha256(b"sunrise-clinic:cust-001").hexdigest()[:16]
    assert customer_hash("sunrise-clinic", "cust-001") == expected
    # The business is in the hash: one reference in two businesses gives two segments.
    assert customer_hash("paws-and-co", "cust-001") != expected


def test_rate_keys_follow_the_grammar_business_first() -> None:
    assert rate_key("sunrise-clinic", RouteSlug.AGENT) == "vp:sunrise-clinic:rl:agent:all"
    customer = rate_key("sunrise-clinic", RouteSlug.CONFIRM, "cust-001")
    assert customer == f"vp:sunrise-clinic:rl:confirm:{customer_hash('sunrise-clinic', 'cust-001')}"
    assert "cust-001" not in customer  # pseudonymised, never the reference itself
    for route in RouteSlug:
        assert key_business(rate_key("rt-shop", route)) == "rt-shop"
        assert key_business(rate_key("rt-shop", route, "cust-002")) == "rt-shop"


def test_budget_keys_name_the_utc_day() -> None:
    assert budget_key("paws-and-co", date(2026, 11, 8)) == "vp:paws-and-co:budget:20261108"
    assert key_business(budget_key("paws-and-co", date(2026, 11, 8))) == "paws-and-co"


@pytest.mark.parametrize(
    "key",
    [
        "vp::rl:agent:all",  # no business segment
        "vp:sunrise-clinic:rl:/v1/agent:all",  # a path, never a slug
        "vp:sunrise-clinic:rl:agent:cust-001",  # a raw reference
        "vp:sunrise-clinic:rl:agent:0123456789abcde",  # 15 hex
        "vp:sunrise-clinic:budget:2026-11-18",
        "vp:Sunrise:rl:agent:all",
        "queue:sunrise-clinic:rl:agent:all",
        "vp:sunrise-clinic:rl:agent:all:extra",
    ],
)
def test_the_grammar_refuses_every_other_shape(key: str) -> None:
    assert key_business(key) is None
    assert KEY_GRAMMAR.fullmatch(key) is None


@pytest.mark.parametrize("business", ["", "ab", "Sunrise", "a" * 41, "x:y", "vp:a", "-ab"])
def test_a_key_refuses_a_business_that_is_not_a_slug(business: str) -> None:
    with pytest.raises(ValueError, match="business slug"):
        rate_key(business, RouteSlug.AGENT)
    with pytest.raises(ValueError, match="business slug"):
        budget_key(business, NOON.date())


@pytest.mark.parametrize("ref", ["", "ab", "Cust-1", "c" * 41, "cust:1", "cust 1"])
def test_a_key_refuses_a_customer_that_is_not_a_slug(ref: str) -> None:
    with pytest.raises(ValueError, match="customer slug"):
        rate_key("rt-shop", RouteSlug.AGENT, ref)


def test_a_key_with_a_trailing_newline_is_outside_the_grammar() -> None:
    # [mutation check] `fullmatch`, not `search`: `$` also matches before a final newline.
    assert key_business(rate_key("rt-shop", RouteSlug.AGENT) + "\n") is None
    assert key_business(budget_key("rt-shop", date(2026, 11, 8)) + "\n") is None


def test_the_budget_key_outlives_a_call_that_crosses_utc_midnight() -> None:
    # [mutation check] The TTL is set at the day's first reservation; a call reserved at 23:59
    # must still find the key after midnight, so the TTL is more than one day.
    assert BUDGET_TTL_SECONDS > 24 * 60 * 60


def test_the_day_is_the_utc_day_of_an_aware_clock() -> None:
    late_in_india = datetime(2026, 11, 19, 1, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert utc_day(late_in_india) == date(2026, 11, 18)
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_day(datetime(2026, 11, 18, 12, 0))


@pytest.mark.parametrize(
    ("usd", "expected"),
    [
        ("0", 0),
        ("0.000001", 1),
        ("0.0000001", 1),  # rounded up: never under-counted
        ("0.0000010001", 2),
        ("0.50", 500_000),
        ("0.008856", 8_856),
    ],
)
def test_micro_usd_rounds_up(usd: str, expected: int) -> None:
    assert micro_usd(Decimal(usd)) == expected


@pytest.mark.parametrize("usd", ["-0.000001", "NaN", "Infinity"])
def test_micro_usd_refuses_a_negative_or_non_finite_cost(usd: str) -> None:
    with pytest.raises(ValueError, match="finite"):
        micro_usd(Decimal(usd))


# ── Policy and refusals ──────────────────────────────────────────────────────────────────────


def test_the_policy_reads_the_settings_placeholders() -> None:
    # The plan's placeholders, passed explicitly: the test settings keep limits far above test
    # volume (conftest), and the Settings defaults are pinned in `test_limit_settings.py`.
    settings = make_settings(
        guard_limit_business_per_minute=120,
        guard_limit_customer_per_minute=10,
        guard_budget_usd_per_business_per_day="0.50",
    )
    assert LimitPolicy.from_settings(settings) == LimitPolicy(
        business=Window(120, 60),
        customer=Window(10, 60),
        budget_micro_usd=500_000,
        # M8 PR 1: session creation has its own per-business window (the test settings' value).
        routes={RouteSlug.SESSION: Window(100_000, 60)},
    )


def test_m8s_session_route_reads_its_own_placeholder_and_the_rest_keep_the_business_window() -> (
    None
):
    settings = make_settings(
        guard_limit_business_per_minute=120,
        guard_limit_customer_per_minute=10,
        session_limit_business_per_minute=60,
    )

    policy = LimitPolicy.from_settings(settings)

    assert policy.business_window(RouteSlug.SESSION) == Window(60, 60)
    for route in set(RouteSlug) - {RouteSlug.SESSION}:
        assert policy.business_window(route) == Window(120, 60)
    # Step B3 (zara's review): staff login has no slug and no window of its own.
    assert "staff_login" not in {route.value for route in RouteSlug}
    with pytest.raises(TypeError):
        policy.routes[RouteSlug.AGENT] = Window(1, 1)  # type: ignore[index]  # read-only


def test_a_policy_without_route_windows_uses_the_business_window_everywhere() -> None:
    assert all(POLICY.business_window(route) == POLICY.business for route in RouteSlug)


def test_a_window_and_a_budget_are_positive() -> None:
    with pytest.raises(ValueError, match="at least one request"):
        Window(0, 60)
    with pytest.raises(ValueError, match="at least one request"):
        Window(1, 0)
    with pytest.raises(ValueError, match="at least one micro-USD"):
        LimitPolicy(Window(1, 1), Window(1, 1), budget_micro_usd=0)


def test_guard_refused_is_neither_a_gateway_error_nor_a_timeout() -> None:
    from vectapilot_api.gateway.errors import GatewayError

    assert not issubclass(GuardRefused, GatewayError | TimeoutError)
    assert issubclass(GuardRefused, Exception)
    refused = GuardRefused(RefusalKind.UNAVAILABLE, error_type="ConnectionError")
    assert str(refused) == "guard refused: unavailable"  # a code, never text


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"kind": RefusalKind.LIMIT}, "Retry-After"),
        ({"kind": RefusalKind.BUDGET, "retry_after_s": 3}, "Retry-After"),
        ({"kind": RefusalKind.LIMIT, "retry_after_s": 0}, "at least one second"),
        ({"kind": RefusalKind.UNAVAILABLE, "error_type": "boom: a message"}, "class name"),
    ],
)
def test_a_refusal_carries_only_what_its_kind_allows(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        GuardRefused(**kwargs)


LIMIT = GuardRefused(RefusalKind.LIMIT, retry_after_s=12)
BUDGET = GuardRefused(RefusalKind.BUDGET)
DOWN = GuardRefused(RefusalKind.UNAVAILABLE, error_type="TimeoutError")


@pytest.mark.parametrize(("health", "priority"), [(True, Priority.HIGH), (False, Priority.NORMAL)])
def test_refusals_on_a_route_with_a_handoff(health: bool, priority: Priority) -> None:
    # G3: after the red-flag screen cleared the message, high for health and normal otherwise;
    # every refusal for a health business carries the guidance.
    limit = refusal_decision(LIMIT, health=health)
    assert (limit.action, limit.decided_by, limit.guidance) == (
        GuardAction.LIMIT,
        DecidedBy.LIMIT,
        health,
    )
    budget = refusal_decision(BUDGET, health=health)
    assert (budget.action, budget.decided_by, budget.priority, budget.guidance) == (
        GuardAction.HANDOFF,
        DecidedBy.BUDGET,
        priority,
        health,
    )
    down = refusal_decision(DOWN, health=health)
    assert (down.action, down.decided_by, down.priority, down.guidance) == (
        GuardAction.HANDOFF,
        DecidedBy.GUARD_ERROR,
        priority,
        health,
    )


def test_refusals_on_a_route_without_a_handoff() -> None:
    # Search, confirm and extractions (G15, PR 5's design point): a 429 or a typed 503.
    assert refusal_decision(LIMIT, health=True, handoff=False).action is GuardAction.LIMIT
    down = refusal_decision(DOWN, health=True, handoff=False)
    assert (down.action, down.decided_by) == (
        GuardAction.UNAVAILABLE,
        DecidedBy.LIMITS_UNAVAILABLE,
    )
    budget = refusal_decision(BUDGET, health=False, handoff=False)
    assert (budget.action, budget.decided_by) == (GuardAction.UNAVAILABLE, DecidedBy.BUDGET)


def test_a_limit_is_a_429_with_retry_after_and_the_guidance_given() -> None:
    lines = ("Call the clinic's emergency line now.",)
    response = limited_response(LIMIT, lines)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "12"
    assert json.loads(bytes(response.body)) == {
        "detail": "too many requests: retry after the number of seconds given",
        "error": "rate_limited",
        "retry_after_seconds": 12,
        "guidance": list(lines),
    }
    assert json.loads(bytes(limited_response(LIMIT).body))["guidance"] == []
    with pytest.raises(ValueError, match="only a limit"):
        limited_response(BUDGET)


# ── RedisLimiter over a stub client ──────────────────────────────────────────────────────────


class StubScript:
    def __init__(self, owner: "StubRedis", name: str) -> None:
        self.owner = owner
        self.name = name

    async def __call__(
        self, keys: Sequence[str] | None = None, args: Sequence[object] | None = None
    ) -> object:
        self.owner.calls.append((self.name, list(keys or ()), list(args or ())))
        return await self.owner.answer(self.name)


class StubRedis:
    """`register_script` and `get`, with scripted replies: a value, an exception or `HANG`."""

    NAMES: ClassVar[dict[str, str]] = {
        RATE_SCRIPT: "rate",
        RESERVE_SCRIPT: "reserve",
        SETTLE_SCRIPT: "settle",
    }

    def __init__(self, **replies: object) -> None:
        self.replies = replies
        self.calls: list[tuple[str, list[str], list[object]]] = []
        self.registered: list[str] = []

    def register_script(self, script: str) -> StubScript:
        self.registered.append(self.NAMES[script])
        return StubScript(self, self.NAMES[script])

    async def get(self, key: str) -> object:
        self.calls.append(("get", [key], []))
        return await self.answer("get")

    async def answer(self, name: str) -> object:
        reply = self.replies[name]
        if reply is HANG:
            await asyncio.sleep(30)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def limiter(stub: StubRedis, *, timeout_s: float = 0.25) -> RedisLimiter:
    return RedisLimiter(cast(Redis, stub), POLICY, timeout_s=timeout_s, clock=lambda: NOON)


def test_building_the_limiter_registers_three_scripts_and_calls_nothing() -> None:
    stub = StubRedis()
    built = limiter(stub)
    assert stub.registered == ["rate", "reserve", "settle"]
    assert stub.calls == []
    assert built.policy is POLICY
    with pytest.raises(ValueError, match="positive"):
        limiter(stub, timeout_s=0)


async def test_one_script_call_checks_every_window_of_a_request() -> None:
    stub = StubRedis(rate=[1, 0])
    await limiter(stub).admit("rt-shop", RouteSlug.AGENT, "cust-001")
    hashed = customer_hash("rt-shop", "cust-001")
    assert stub.calls == [
        (
            "rate",
            ["vp:rt-shop:rl:agent:all", f"vp:rt-shop:rl:agent:{hashed}"],
            [5, 60_000, 2, 60_000],
        )
    ]


async def test_without_a_customer_only_the_business_window_counts() -> None:
    stub = StubRedis(rate=[1, 0])
    await limiter(stub).admit("rt-shop", RouteSlug.EXTRACTION)
    assert stub.calls == [("rate", ["vp:rt-shop:rl:extraction:all"], [5, 60_000])]


@pytest.mark.parametrize(
    ("route", "limit"), [(RouteSlug.SESSION, 3), (RouteSlug.CONFIRM, 1), (RouteSlug.AGENT, 5)]
)
async def test_a_route_with_its_own_window_sends_that_window(route: RouteSlug, limit: int) -> None:
    # M8 PR 1: the same script and key grammar, the route's own limit (any slug may have one; the
    # second here only shows two route windows never mix).
    policy = LimitPolicy(
        Window(5, 60),
        Window(2, 60),
        budget_micro_usd=1_000,
        routes={RouteSlug.SESSION: Window(3, 60), RouteSlug.CONFIRM: Window(1, 60)},
    )
    stub = StubRedis(rate=[1, 0])
    built = RedisLimiter(cast(Redis, stub), policy, timeout_s=0.25, clock=lambda: NOON)

    await built.admit("rt-shop", route)

    assert stub.calls == [("rate", [f"vp:rt-shop:rl:{route.value}:all"], [limit, 60_000])]


@pytest.mark.parametrize(("ttl_ms", "retry_after"), [(12_001, 13), (1, 1), (0, 1), (60_000, 60)])
async def test_a_full_window_is_a_limit_with_its_remaining_seconds(
    ttl_ms: int, retry_after: int
) -> None:
    stub = StubRedis(rate=[0, ttl_ms])
    with pytest.raises(GuardRefused) as caught:
        await limiter(stub).admit("rt-shop", RouteSlug.CONFIRM, "cust-001")
    assert (caught.value.kind, caught.value.retry_after_s) == (RefusalKind.LIMIT, retry_after)


async def test_a_reservation_sends_the_amount_cap_and_ttl_on_todays_key() -> None:
    stub = StubRedis(reserve=[1, 300])
    reserved = await limiter(stub).reserve("paws-and-co", 300)
    assert reserved == Reserved(key="vp:paws-and-co:budget:20261118", micro_usd=300)
    assert stub.calls == [
        ("reserve", ["vp:paws-and-co:budget:20261118"], [300, 1_000, BUDGET_TTL_SECONDS])
    ]


async def test_a_reservation_over_the_cap_is_a_budget_refusal() -> None:
    stub = StubRedis(reserve=[0, 900])
    with pytest.raises(GuardRefused) as caught:
        await limiter(stub).reserve("paws-and-co", 300)
    assert caught.value.kind is RefusalKind.BUDGET
    with pytest.raises(ValueError, match="at least zero"):
        await limiter(stub).reserve("paws-and-co", -1)


async def test_a_settle_sends_the_difference_on_the_captured_key() -> None:
    stub = StubRedis(settle=1)
    reserved = Reserved(key="vp:paws-and-co:budget:20261117", micro_usd=300)  # yesterday's
    await limiter(stub).settle(reserved, 120)
    await limiter(stub).settle(reserved, 300)  # nothing to adjust: no Redis call
    await limiter(stub).settle(reserved, 450)
    assert stub.calls == [
        ("settle", ["vp:paws-and-co:budget:20261117"], [-180]),
        ("settle", ["vp:paws-and-co:budget:20261117"], [150]),
    ]
    with pytest.raises(ValueError, match="at least zero"):
        await limiter(stub).settle(reserved, -1)


@pytest.mark.parametrize(
    ("spent", "remaining"),
    # A negative day (never written by the scripts since diego's PR 5 review) never raises the cap.
    [(None, 1_000), (b"250", 750), (b"1200", 0), (b"-150", 1_000)],
)
async def test_remaining_is_the_cap_less_todays_spend(spent: object, remaining: int) -> None:
    stub = StubRedis(get=spent)
    assert await limiter(stub).remaining("rt-shop") == remaining
    assert stub.calls == [("get", ["vp:rt-shop:budget:20261118"], [])]


async def test_remaining_reads_the_limiters_clock_day() -> None:
    # [mutation check] Not the wall clock: NOON was the real date when this was written, so only
    # another day tells the injected clock from `datetime.now`.
    stub = StubRedis(get=b"1")
    other_day = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)
    built = RedisLimiter(cast(Redis, stub), POLICY, timeout_s=0.25, clock=lambda: other_day)
    await built.remaining("rt-shop")
    assert stub.calls == [("get", ["vp:rt-shop:budget:20250102"], [])]


async def test_the_route_precheck_refuses_below_one_turns_worst_case() -> None:
    stub = StubRedis(get=b"700")
    await require_budget(limiter(stub), "rt-shop", 300)  # exactly the remainder fits
    with pytest.raises(GuardRefused) as caught:
        await require_budget(limiter(stub), "rt-shop", 301)
    assert caught.value.kind is RefusalKind.BUDGET


FAILURES = [
    pytest.param(RedisConnectionError("connection refused to the canary host"), id="down"),
    pytest.param(OSError("socket"), id="os-error"),
    pytest.param(TimeoutError("the client's own timeout"), id="client-timeout"),
    pytest.param(["not", "a", "pair"], id="bad-reply"),
    pytest.param("text", id="bad-type"),
]


async def call_every_operation(stub: StubRedis) -> list[GuardRefused]:
    caught: list[GuardRefused] = []
    built = limiter(stub, timeout_s=0.05)
    reserved = Reserved(key="vp:rt-shop:budget:20261118", micro_usd=10)
    for operation in (
        lambda: built.admit("rt-shop", RouteSlug.AGENT, "cust-001"),
        lambda: built.reserve("rt-shop", 10),
        lambda: built.settle(reserved, 5),
        lambda: built.remaining("rt-shop"),
    ):
        try:
            await operation()
        except GuardRefused as refused:
            caught.append(refused)
    return caught


@pytest.mark.parametrize("failure", FAILURES)
async def test_any_redis_failure_fails_closed_with_its_class_name_only(failure: object) -> None:
    stub = StubRedis(rate=failure, reserve=failure, settle=failure, get=failure)
    caught = await call_every_operation(stub)
    settle_counts = isinstance(failure, BaseException)  # a settle ignores its reply's shape
    assert len(caught) == (4 if settle_counts else 3)
    for refused in caught:
        assert refused.kind is RefusalKind.UNAVAILABLE
        assert refused.error_type is not None and refused.error_type.isidentifier()
        # Raised outside `except`: no chained exception whose text a log line would write.
        assert refused.__context__ is None and refused.__cause__ is None
        assert "canary" not in str(refused)


async def test_a_reply_with_extra_values_fails_closed() -> None:
    # [mutation check] G3: a three-int reply is unexpected, never "allowed".
    stub = StubRedis(rate=[1, 0, 7], reserve=[1, 5, 7])
    built = RedisLimiter(cast(Redis, stub), POLICY, timeout_s=0.25, clock=lambda: NOON)
    for call in (
        lambda: built.admit("rt-shop", RouteSlug.AGENT),
        lambda: built.reserve("rt-shop", 5),
    ):
        with pytest.raises(GuardRefused) as caught:
            await call()
        assert caught.value.kind is RefusalKind.UNAVAILABLE


async def test_a_hung_redis_is_cut_off_at_the_timeout_and_fails_closed() -> None:
    stub = StubRedis(rate=HANG, reserve=HANG, settle=HANG, get=HANG)
    loop = asyncio.get_running_loop()
    started = loop.time()
    caught = await call_every_operation(stub)
    assert [refused.error_type for refused in caught] == ["TimeoutError"] * 4
    assert loop.time() - started < 2.0  # four 0.05 s bounds, never redis-py's 5 s (a bound)


async def test_a_cancel_from_outside_propagates_unchanged() -> None:
    stub = StubRedis(rate=HANG)
    task = asyncio.create_task(limiter(stub, timeout_s=10).admit("rt-shop", RouteSlug.AGENT))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_real_client_on_a_dead_port_fails_closed_within_the_timeout() -> None:
    # The unit tests' Redis is port 1 (`tests/conftest.py`): connection refused, never a hang.
    dead = Redis.from_url("redis://127.0.0.1:1/15")
    try:
        built = RedisLimiter(dead, POLICY, timeout_s=0.5)
        with pytest.raises(GuardRefused) as caught:
            await built.admit("rt-shop", RouteSlug.AGENT)
        assert caught.value.kind is RefusalKind.UNAVAILABLE
        assert caught.value.error_type in ("ConnectionError", "TimeoutError")
    finally:
        await dead.aclose()
