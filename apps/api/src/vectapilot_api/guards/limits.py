"""Rate limits and daily budgets in Redis (M7 PR 5; G9, G12, G15; D3 (a), D4 (a)).

**Keys** follow one closed grammar (`KEY_GRAMMAR`), business first (G12):
`vp:{business_id}:rl:{route_slug}:{all|h}` and `vp:{business_id}:budget:{yyyymmdd}`. The route
is a fixed slug (`RouteSlug`), never a path. `h` is the first 16 hex characters of SHA-256 over
`business_id ":" customer_ref`: **pseudonymised**, and reversible by enumeration (a customer
reference is a short slug), never called anonymous. The business in the hash gives `cust-001`
two different hashes in two businesses. From M8 (S4) a widget session's reference is `s-` plus
its uuid's hex, so its segment is not enumerable; the hash and the grammar stay.

**Limits** (`admit`): a per-business window (`all`, the real control) and, when the request names
a customer, a per-customer window (advisory: client-asserted on the header routes; on the widget,
one window per session, which a client renews by opening a new session; the per-business windows
are the control). M8 PR 1 adds one route with its own per-business window and no customer
(`LimitPolicy.routes`): creating a customer session. Staff login has none (step B3).
One Lua script per request checks every window and increments all of them or none (`RATE_SCRIPT`),
loaded with `register_script`: redis-py sends EVALSHA and reloads the script on NOSCRIPT after a
Redis restart. A window is fixed: it starts at its first request and its key expires with it. A
refusal raises `GuardRefused(LIMIT)` with the longest full window's remaining seconds as
`Retry-After`.

**Budgets** (`reserve`, `settle`, `remaining`): per business per UTC day, in integer micro-USD
rounded up (`micro_usd`). One conditional script reserves (`RESERVE_SCRIPT`: GET, compare with the
cap passed as ARGV, INCRBY, set the TTL if none), never INCRBY-then-check-then-DECRBY, so the day
never goes over its cap, even under concurrent requests. A settle adjusts the key captured at
reservation (`Reserved.key`), so a call that crosses UTC midnight settles the day it reserved on;
a refund never takes the day below zero (`SETTLE_SCRIPT`).
The key lives `BUDGET_TTL_SECONDS` [proposal: two days, so such a settle still finds it].

**Fail closed** (D3 (a), G3). Every Redis call runs inside `asyncio.timeout`
(`guard_redis_timeout_seconds`): without it redis-py's 5 s defaults would hold each guarded request
on a blackholed Redis. A timeout, any Redis error or an unexpected reply raises
`GuardRefused(UNAVAILABLE)` with the class name only, raised outside `except` (`log.py` writes a
chained exception's text). `asyncio.CancelledError` always propagates. The red-flag screen never
calls this module (G1): an emergency never waits on Redis.

**`GuardRefused`** is a plain `Exception`: neither a `GatewayError` (the agent loop would turn it
into `model_unavailable`) nor a `TimeoutError` (`AgentService` catches that first as the turn's
own timeout). PR 6 passes it in `AgentService`'s `propagate` and maps it with `refusal_decision`
and `limited_response`: a 429 with `Retry-After` for a limit (its body carries the guidance for a
health business), a `budget` hand-off for the budget, and a `guard_error` hand-off (or a typed
503 on a route without a hand-off) when Redis fails.

Every number here is a placeholder from Settings [proposal; unmeasured], tuned only on measured
traffic. A Redis recreate zeroes every counter and the day's budget (compose has no volume; ADR
0018, Consequences).
"""

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, Self

from fastapi.responses import JSONResponse

from vectapilot_api.guards.types import DecidedBy, GuardAction, GuardDecision, RouteSlug
from vectapilot_api.intents.policy import Priority
from vectapilot_api.tools.config import CUSTOMER_REF_LENGTH, CUSTOMER_REF_PATTERN

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript

    from vectapilot_api.config import Settings

KEY_PREFIX: Final = "vp"
CUSTOMER_HASH_HEX: Final = 16
MICRO_USD_PER_USD: Final = 1_000_000
BUDGET_TTL_SECONDS: Final = 2 * 24 * 60 * 60
"""How long a day's budget key lives [proposal]: two days, so a call reserved just before UTC
midnight still settles on its own day's key."""
WINDOW_SECONDS: Final = 60
"""Every rate window is one minute (`guard_limit_*_per_minute`)."""

_BUSINESS_ID: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_ROUTES: Final = "|".join(re.escape(route.value) for route in RouteSlug)
KEY_GRAMMAR: Final = re.compile(
    rf"^{KEY_PREFIX}:(?P<business>{_BUSINESS_ID})"
    rf":(?:rl:(?:{_ROUTES}):(?:all|[0-9a-f]{{{CUSTOMER_HASH_HEX}}})|budget:[0-9]{{8}})$"
)
"""Every key this module writes (G9's closed grammar); the key-scan canary checks each `vp:` key
against it (G12)."""
_CUSTOMER_REF: Final = re.compile(CUSTOMER_REF_PATTERN)
_ERROR_TYPE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")

RATE_LIMITED: Final = "rate_limited"
RATE_LIMITED_DETAIL: Final = "too many requests: retry after the number of seconds given"


# ── Refusals ─────────────────────────────────────────────────────────────────────────────────


class RefusalKind(StrEnum):
    LIMIT = "limit"
    """A rate window is full: a 429 with `Retry-After`."""
    BUDGET = "budget"
    """The business's day's budget cannot cover the call: a `budget` hand-off."""
    UNAVAILABLE = "unavailable"
    """Redis failed, timed out or answered unexpectedly: fail closed (G3)."""


class GuardRefused(Exception):
    """A limit, the budget or a Redis failure refused the request (module docstring). Its message
    is a fixed code; it carries codes, seconds and a class name only, never text."""

    def __init__(
        self, kind: RefusalKind, *, retry_after_s: int | None = None, error_type: str | None = None
    ) -> None:
        if (kind is RefusalKind.LIMIT) != (retry_after_s is not None):
            raise ValueError("a limit, and only a limit, has a Retry-After")
        if retry_after_s is not None and retry_after_s < 1:
            raise ValueError("Retry-After is at least one second")
        if error_type is not None and not _ERROR_TYPE.fullmatch(error_type):
            raise ValueError("error_type is a class name")
        super().__init__(f"guard refused: {kind.value}")
        self.kind = kind
        self.retry_after_s = retry_after_s
        self.error_type = error_type


def refusal_decision(refused: GuardRefused, *, health: bool, handoff: bool = True) -> GuardDecision:
    """What a route does with a refusal (G3, G9, G15). Limits and budgets run after the red-flag
    screen has cleared the message (G1), so a hand-off is high for a health business and normal
    for any other; every refusal for a health business carries the guidance. A route without a
    hand-off (`handoff=False`: search, confirm, extractions) answers a limit with a 429 and
    anything else with a typed 503."""
    if refused.kind is RefusalKind.LIMIT:
        return GuardDecision(GuardAction.LIMIT, DecidedBy.LIMIT, guidance=health)
    budget = refused.kind is RefusalKind.BUDGET
    if not handoff:
        code = DecidedBy.BUDGET if budget else DecidedBy.LIMITS_UNAVAILABLE
        return GuardDecision(GuardAction.UNAVAILABLE, code)
    decided_by = DecidedBy.BUDGET if budget else DecidedBy.GUARD_ERROR
    priority = Priority.HIGH if health else Priority.NORMAL
    return GuardDecision(GuardAction.HANDOFF, decided_by, priority=priority, guidance=health)


def limited_response(refused: GuardRefused, guidance: Sequence[str] = ()) -> JSONResponse:
    """A limit's 429 (G9): `Retry-After` in whole seconds, and a body that never echoes input. Pass
    the business's guidance lines for a health business (G3), none for any other."""
    if refused.retry_after_s is None:
        raise ValueError("only a limit is a 429")
    return JSONResponse(
        {
            "detail": RATE_LIMITED_DETAIL,
            "error": RATE_LIMITED,
            "retry_after_seconds": refused.retry_after_s,
            "guidance": list(guidance),
        },
        status_code=429,
        headers={"Retry-After": str(refused.retry_after_s)},
    )


# ── Keys ─────────────────────────────────────────────────────────────────────────────────────


def _business(business_id: str) -> str:
    if not 3 <= len(business_id) <= 40 or not re.fullmatch(_BUSINESS_ID, business_id):
        raise ValueError("business_id is not a business slug")
    return business_id


def customer_hash(business_id: str, customer_ref: str) -> str:
    """The pseudonymous customer segment (module docstring): 16 hex of SHA-256 over
    `business_id:customer_ref`."""
    low, high = CUSTOMER_REF_LENGTH
    if not low <= len(customer_ref) <= high or not _CUSTOMER_REF.fullmatch(customer_ref):
        raise ValueError("customer_ref is not a customer slug")
    joined = f"{_business(business_id)}:{customer_ref}".encode()
    return hashlib.sha256(joined).hexdigest()[:CUSTOMER_HASH_HEX]


def rate_key(business_id: str, route: RouteSlug, customer_ref: str | None = None) -> str:
    """`vp:{business_id}:rl:{route}:all`, or the customer's window when `customer_ref` is given."""
    scope = "all" if customer_ref is None else customer_hash(business_id, customer_ref)
    return f"{KEY_PREFIX}:{_business(business_id)}:rl:{RouteSlug(route).value}:{scope}"


def budget_key(business_id: str, day: date) -> str:
    """`vp:{business_id}:budget:{yyyymmdd}`, the UTC day's budget."""
    return f"{KEY_PREFIX}:{_business(business_id)}:budget:{day:%Y%m%d}"


def key_business(key: str) -> str | None:
    """The business segment of a key in the grammar, or None for any other key (G12's scan)."""
    match = KEY_GRAMMAR.fullmatch(key)
    return None if match is None else match["business"]


def utc_day(now: datetime) -> date:
    """The UTC day of an aware time (a naive one is refused: it has no day)."""
    if now.tzinfo is None:
        raise ValueError("the clock must be timezone-aware")
    return now.astimezone(UTC).date()


def micro_usd(usd: Decimal) -> int:
    """Dollars as integer micro-USD, rounded up (`ROUND_CEILING`): a budget never under-counts."""
    if not usd.is_finite() or usd < 0:
        raise ValueError("a cost is a finite amount of at least zero")
    return int((usd * MICRO_USD_PER_USD).to_integral_value(rounding=ROUND_CEILING))


# ── Policy and the limiter's interface ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Window:
    limit: int
    period_s: int

    def __post_init__(self) -> None:
        if self.limit < 1 or self.period_s < 1:
            raise ValueError("a window allows at least one request for at least one second")


@dataclass(frozen=True)
class LimitPolicy:
    """The windows and the day's cap (Settings' placeholders, module docstring).

    `routes` (M8 PR 1): a route's own per-business window, used instead of `business` for that
    route only. Creating a customer session has its own placeholder
    (`session_limit_business_per_minute`, S14); every other route keeps `business`. Its keys
    follow the same grammar (`RouteSlug`)."""

    business: Window
    customer: Window
    budget_micro_usd: int
    routes: Mapping[RouteSlug, Window] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.budget_micro_usd < 1:
            raise ValueError("a day's budget is at least one micro-USD")
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))

    def business_window(self, route: RouteSlug) -> Window:
        """The per-business window of `route`: its own when it has one, else `business`."""
        return self.routes.get(route, self.business)

    @classmethod
    def from_settings(cls, settings: "Settings") -> Self:
        return cls(
            business=Window(settings.guard_limit_business_per_minute, WINDOW_SECONDS),
            customer=Window(settings.guard_limit_customer_per_minute, WINDOW_SECONDS),
            budget_micro_usd=micro_usd(settings.guard_budget_usd_per_business_per_day),
            routes={
                RouteSlug.SESSION: Window(
                    settings.session_limit_business_per_minute, WINDOW_SECONDS
                ),
            },
        )


@dataclass(frozen=True)
class Reserved:
    """A reservation: the key captured when it was made (G9) and its amount."""

    key: str
    micro_usd: int


class Limiter(Protocol):
    """What the routes and `BudgetedGateway` call (`RedisLimiter`; `FakeLimiter` in tests). Each
    method raises `GuardRefused` to refuse, never returns a refusal."""

    async def admit(
        self, business_id: str, route: RouteSlug, customer_ref: str | None = None
    ) -> None:
        """Count one request in every window, or raise `GuardRefused(LIMIT)` and count none."""
        ...

    async def reserve(self, business_id: str, amount_micro_usd: int) -> Reserved:
        """Reserve `amount` on today's budget, or raise `GuardRefused(BUDGET)`."""
        ...

    async def settle(self, reserved: Reserved, spent_micro_usd: int) -> None:
        """Replace the reservation with what the call is counted at, on the reserved day's key."""
        ...

    async def remaining(self, business_id: str) -> int:
        """Today's unreserved budget (the route's pre-check, G9)."""
        ...


async def require_budget(limiter: Limiter, business_id: str, needed_micro_usd: int) -> None:
    """The route's pre-check (G9): `GuardRefused(BUDGET)` when today's remainder is below one
    turn's worst case `needed_micro_usd`, before the service runs."""
    if await limiter.remaining(business_id) < needed_micro_usd:
        raise GuardRefused(RefusalKind.BUDGET)


# ── Redis ────────────────────────────────────────────────────────────────────────────────────

RATE_SCRIPT: Final = """
-- KEYS: one per window. ARGV: limit and period (ms) of each window, in KEYS' order.
-- Checks every window first; increments all of them or none (G9). A refusal waits for the
-- longest full window, so one Retry-After is enough [diego's PR 5 review].
local full = false
local wait = 0
for i = 1, #KEYS do
  local count = tonumber(redis.call('GET', KEYS[i]) or '0')
  if count + 1 > tonumber(ARGV[2 * i - 1]) then
    local ttl = redis.call('PTTL', KEYS[i])
    if ttl < 0 then
      redis.call('PEXPIRE', KEYS[i], ARGV[2 * i])
      ttl = tonumber(ARGV[2 * i])
    end
    full = true
    if ttl > wait then
      wait = ttl
    end
  end
end
if full then
  return {0, wait}
end
for i = 1, #KEYS do
  local count = redis.call('INCR', KEYS[i])
  if count == 1 or redis.call('PTTL', KEYS[i]) < 0 then
    redis.call('PEXPIRE', KEYS[i], ARGV[2 * i])
  end
end
return {1, 0}
"""

RESERVE_SCRIPT: Final = """
-- KEYS[1]: the day's budget. ARGV: amount, cap, TTL (s). Never over the cap (G9).
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
if spent + amount > tonumber(ARGV[2]) then
  return {0, spent}
end
local now = redis.call('INCRBY', KEYS[1], amount)
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[3])
end
return {1, now}
"""

SETTLE_SCRIPT: Final = """
-- KEYS[1]: the reserved day's budget. ARGV[1]: spent minus reserved (may be negative).
-- A key that is gone (a Redis recreate) is not recreated without its TTL. A refund never takes
-- the day below zero: after a recreate the key may no longer hold this reservation, and a
-- negative day would raise the cap [diego's PR 5 review].
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('INCRBY', KEYS[1], ARGV[1]) < 0 then
  redis.call('SET', KEYS[1], 0, 'KEEPTTL')
end
return 1
"""


def _pair(reply: object) -> tuple[int, int]:
    if (
        not isinstance(reply, list)
        or len(reply) != 2
        or not all(isinstance(value, int) for value in reply)
    ):
        raise ValueError("unexpected script reply")
    return reply[0], reply[1]


def _int(reply: object) -> int:
    if reply is None:
        return 0
    if isinstance(reply, bytes):
        reply = reply.decode("ascii")
    if isinstance(reply, int):
        return reply
    if isinstance(reply, str) and re.fullmatch(r"-?[0-9]{1,18}", reply):
        return int(reply)
    raise ValueError("unexpected reply")


def _now() -> datetime:
    return datetime.now(UTC)


class RedisLimiter:
    """The `Limiter` over Redis (module docstring). Building it makes no network call:
    `register_script` only hashes each script, and the client connects lazily (`main.py`), so the
    API starts with Redis down and `/healthz` stays green."""

    def __init__(
        self,
        redis: "Redis",
        policy: LimitPolicy,
        *,
        timeout_s: float,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if not timeout_s > 0:
            raise ValueError("the Redis timeout is positive")
        self._redis = redis
        self._policy = policy
        self._timeout_s = timeout_s
        self._clock = clock
        self._rate: AsyncScript = redis.register_script(RATE_SCRIPT)
        self._reserve: AsyncScript = redis.register_script(RESERVE_SCRIPT)
        self._settle: AsyncScript = redis.register_script(SETTLE_SCRIPT)

    @property
    def policy(self) -> LimitPolicy:
        return self._policy

    async def _bounded[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run one Redis operation inside the timeout; any failure is `UNAVAILABLE` (module
        docstring), raised outside `except`. A cancel from outside propagates unchanged."""
        failure = TimeoutError.__name__
        try:
            async with asyncio.timeout(self._timeout_s):
                return await operation()
        except TimeoutError:  # ours expired, or the client's own: either way Redis did not answer
            pass
        except Exception as error:
            failure = type(error).__name__
        raise GuardRefused(RefusalKind.UNAVAILABLE, error_type=failure)

    async def admit(
        self, business_id: str, route: RouteSlug, customer_ref: str | None = None
    ) -> None:
        windows = [(rate_key(business_id, route), self._policy.business_window(route))]
        if customer_ref is not None:
            windows.append((rate_key(business_id, route, customer_ref), self._policy.customer))
        keys = [key for key, _ in windows]
        args = [part for _, w in windows for part in (w.limit, w.period_s * 1000)]

        async def run() -> tuple[int, int]:
            return _pair(await self._rate(keys=keys, args=args))

        allowed, ttl_ms = await self._bounded(run)
        if allowed != 1:
            raise GuardRefused(RefusalKind.LIMIT, retry_after_s=max(1, math.ceil(ttl_ms / 1000)))

    async def reserve(self, business_id: str, amount_micro_usd: int) -> Reserved:
        if amount_micro_usd < 0:
            raise ValueError("a reservation is at least zero")
        key = budget_key(business_id, utc_day(self._clock()))
        args = [amount_micro_usd, self._policy.budget_micro_usd, BUDGET_TTL_SECONDS]

        async def run() -> tuple[int, int]:
            return _pair(await self._reserve(keys=[key], args=args))

        allowed, _ = await self._bounded(run)
        if allowed != 1:
            raise GuardRefused(RefusalKind.BUDGET)
        return Reserved(key=key, micro_usd=amount_micro_usd)

    async def settle(self, reserved: Reserved, spent_micro_usd: int) -> None:
        if spent_micro_usd < 0:
            raise ValueError("a settled amount is at least zero")
        delta = spent_micro_usd - reserved.micro_usd
        if delta == 0:
            return

        async def run() -> object:
            return await self._settle(keys=[reserved.key], args=[delta])

        await self._bounded(run)

    async def remaining(self, business_id: str) -> int:
        key = budget_key(business_id, utc_day(self._clock()))

        async def run() -> int:
            return _int(await self._redis.get(key))

        spent = max(0, await self._bounded(run))  # a negative day never raises the cap
        return max(0, self._policy.budget_micro_usd - spent)
