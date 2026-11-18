"""The isolated Redis database for M7's limiter and budget tests (PR 5, G9, G12).

Real Redis Lua semantics are tested only on logical database `ISOLATION_DB` (15) of the server
the settings' `redis_url` names (`VECTAPILOT_REDIS_URL`, from the environment or `apps/api/.env`,
as the canary suite reads its settings), never on the app's own database (the dev stack's 0):
`isolated_redis` checks the client's database before it flushes, flushes before and after each
test, and tests use unique business and customer ids (`unique_business`).
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from vectapilot_api.config import Settings
from vectapilot_api.redteam.limiter import isolated_redis_url, require_isolated


def unique_business(tag: str = "b") -> str:
    """A fresh business slug, so no two tests (or runs) share a key."""
    return f"t{uuid.uuid4().hex[:12]}-{tag}"


@asynccontextmanager
async def isolated_redis() -> AsyncIterator[Redis]:
    """A client on the isolated database, flushed before and after (module docstring)."""
    url = str(Settings().redis_url.get_secret_value())  # as tests/canary reads its settings
    redis = Redis.from_url(isolated_redis_url(url))
    try:
        require_isolated(redis)  # never FLUSHDB anything but the isolated database
        await redis.flushdb()
        yield redis
        await redis.flushdb()
    finally:
        await redis.aclose()
