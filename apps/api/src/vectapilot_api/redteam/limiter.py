"""The limiter path's runner (M7 PR 5; `plan.CasePath.LIMITER`): k-002 and k-004 against real Redis.

**Where.** The database environment's served app gives the Redis URL (`state.settings`) and the
businesses it serves (`state.tools`). The runner never touches the app's own logical database: it
connects to `ISOLATION_DB` (15) on the same server, refuses any client that is not on it before
its first command (`require_isolated`), deletes only the exact keys it writes before and after,
and never runs FLUSHDB. Its limits are tiny (`POLICY`), so exhausting a window takes a
few calls; they are a test arrangement, not Settings.

**k-002, `cross-business-limit`.** Every *other* served business's windows on the agent route are
exhausted until the limiter refuses them (the control, `reached`: each one was refused). Then the
case's own business must find no `vp:` key of its own (untouched) and be admitted, business and
customer windows alike. Anything else is a `leak` of another business's state.

**k-004, `redis-key-scan`.** Every served business gets a rate window and a reservation; then
every `vp:` key on the database must carry a business segment in the closed grammar
(`guards.limits.key_business`). `reached` is the scan having seen keys of every served business.

A Redis failure is not caught here: the test errors, which the gate counts as a failure. The
result holds codes only: no key, hash or customer reference is returned or logged.
"""

import asyncio
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI
from redis.asyncio import Redis

from vectapilot_api.guards.limits import (
    GuardRefused,
    LimitPolicy,
    RedisLimiter,
    RefusalKind,
    Window,
    budget_key,
    key_business,
    rate_key,
    utc_day,
)
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.redteam.scoring import CaseResult

if TYPE_CHECKING:
    from vectapilot_api.redteam.harness import FilledCase, RunEnv

ISOLATION_DB: Final = 15
"""The logical database every Redis test and this runner use (never the app's, never flushed by
this runner)."""
POLICY: Final = LimitPolicy(business=Window(2, 60), customer=Window(1, 60), budget_micro_usd=1_000)
CUSTOMER: Final = "cust-001"
"""A synthetic customer reference from the sample seeds' grammar."""
ROUTE: Final = RouteSlug.AGENT
TIMEOUT_S: Final = 2.0


TCP_SCHEMES: Final = frozenset({"redis", "rediss"})
NOT_ISOLATED: Final = "the Redis client is not on the isolated database"


def isolated_redis_url(url: str, db: int = ISOLATION_DB) -> str:
    """`url` with its logical database replaced by `db` (credentials kept, never printed).

    redis-py lets a `db` query parameter override the path, so every `db` parameter is dropped
    [diego's PR 5 review]. A `unix` URL is refused: its path is the socket, not the database.
    `require_isolated` still checks the client built from it."""
    parts = urlsplit(url)
    if parts.scheme not in TCP_SCHEMES:
        raise ValueError("the isolated Redis URL must be redis:// or rediss://")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "db"]
    )
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", query, parts.fragment))


def require_isolated(redis: Redis) -> None:
    """Refuse a client that is not on `ISOLATION_DB` (the runner's deletes and the tests' FLUSHDB
    never reach the app's own database). Reads the parsed pool settings: no network."""
    if redis.connection_pool.connection_kwargs.get("db") != ISOLATION_DB:
        raise RuntimeError(NOT_ISOLATED)


def _served(env: "RunEnv") -> tuple[str, tuple[str, ...]]:
    """The isolated Redis URL and the served businesses, from the served suite's app."""
    app = getattr(env.app, "app", None)
    if env.environment != "database" or not isinstance(app, FastAPI):
        raise ValueError("the limiter path runs in the database environment")
    url = str(app.state.settings.redis_url.get_secret_value())
    return isolated_redis_url(url), tuple(sorted(app.state.tools or ()))


def keys_of(businesses: Iterable[str]) -> list[str]:
    """Every key this runner may write for `businesses` (deleted before and after): the rate
    windows, and the budget keys of today and either neighbour (a run that crosses UTC
    midnight)."""
    now = datetime.now(UTC)
    days = {utc_day(now), utc_day(now - timedelta(days=1)), utc_day(now + timedelta(days=1))}
    keys: list[str] = []
    for business in businesses:
        keys += [rate_key(business, ROUTE), rate_key(business, ROUTE, CUSTOMER)]
        keys += [budget_key(business, day) for day in sorted(days)]
    return keys


async def _scan(redis: Redis, business: str | None = None) -> list[str]:
    pattern = "vp:*" if business is None else f"vp:{business}:*"
    return [key.decode() async for key in redis.scan_iter(match=pattern, count=500)]


async def _exhausted(limiter: RedisLimiter, business: str) -> bool:
    """Admit until refused (at most one call over the business window): True once refused."""
    for _ in range(POLICY.business.limit + 1):
        try:
            await limiter.admit(business, ROUTE)
        except GuardRefused as refused:
            return refused.kind is RefusalKind.LIMIT
    return False


async def _cross_business_limit(
    redis: Redis, case_id: str, business: str, others: Sequence[str]
) -> CaseResult:
    limiter = RedisLimiter(redis, POLICY, timeout_s=TIMEOUT_S)
    refused = [await _exhausted(limiter, other) for other in others]
    untouched = not await _scan(redis, business)
    try:
        await limiter.admit(business, ROUTE, CUSTOMER)
        admitted = True
    except GuardRefused as refusal:
        if refusal.kind is not RefusalKind.LIMIT:
            raise
        admitted = False
    outcome = "no_leak" if untouched and admitted else "leak"
    return CaseResult(case_id, outcome, reached=bool(others) and all(refused))


async def _key_scan(redis: Redis, case_id: str, businesses: Sequence[str]) -> CaseResult:
    limiter = RedisLimiter(redis, POLICY, timeout_s=TIMEOUT_S)
    for business in businesses:
        await limiter.admit(business, ROUTE, CUSTOMER)
        await limiter.reserve(business, 1)
    keys = await _scan(redis)
    segments = [key_business(key) for key in keys]
    seen = {segment for segment in segments if segment is not None}
    outcome = "no_leak" if all(segment is not None for segment in segments) else "leak"
    return CaseResult(case_id, outcome, reached=bool(businesses) and set(businesses) <= seen)


async def _run(
    case_id: str, family: str, business: str, url: str, served: Sequence[str]
) -> CaseResult:
    redis = Redis.from_url(url)
    try:
        require_isolated(redis)  # before any delete or write, the `finally` below's included
    except RuntimeError:
        await redis.aclose()
        raise
    owned = keys_of(served)
    try:
        await redis.delete(*owned)
        if family == "cross-business-limit":
            others = [other for other in served if other != business]
            return await _cross_business_limit(redis, case_id, business, others)
        if family == "redis-key-scan":
            return await _key_scan(redis, case_id, served)
        raise ValueError(f"case {case_id}: no limiter arrangement for its family")
    finally:
        try:
            await redis.delete(*owned)
        finally:
            await redis.aclose()


def run_limiter(filled: "FilledCase", env: "RunEnv") -> CaseResult:
    """The registered runner (module docstring)."""
    case = filled.case
    url, served = _served(env)
    return asyncio.run(_run(case.id, case.family, case.business_id, url, served))
