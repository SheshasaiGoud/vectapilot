"""Liveness and readiness endpoints.

- `/healthz` — liveness: "can this process answer at all?" It never checks dependencies: if the
  database is down, restarting the API would not fix it, so liveness must stay green.
- `/readyz` — readiness: "should traffic be sent here right now?" It checks Postgres and Redis.

The readiness logic is [HAND-WRITE]: implemented by the human lead.
Guide: docs/learning/m0-readiness.md · Tests: tests/test_readyz.py
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from vectapilot_api.config import Settings

router = APIRouter(tags=["health"])


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one readiness check. `detail` is shown publicly — never put secrets in it."""

    name: str
    ok: bool
    detail: str = "ok"


ReadinessCheck = Callable[[], Awaitable[CheckResult]]


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ── [HAND-WRITE] the checks ──────────────────────────────────────────────────────────────────


async def check_postgres(engine: AsyncEngine) -> CheckResult:
    """Return `CheckResult("postgres", ok=True)` if Postgres answers `SELECT 1`.

    Never raises: any failure becomes `ok=False` with the exception's class name as `detail`,
    so the connection URL or password can never leak.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as error:
        return CheckResult("postgres", ok=False, detail=type(error).__name__)
    return CheckResult("postgres", ok=True)


async def check_redis(client: Redis) -> CheckResult:
    """Return `CheckResult("redis", ok=True)` if Redis answers `PING`. Same rules as Postgres."""
    try:
        await client.ping()
    except Exception as error:
        return CheckResult("redis", ok=False, detail=type(error).__name__)
    return CheckResult("redis", ok=True)


# ── Wiring (provided): FastAPI dependencies, so tests can swap in fakes ─────────────────────


def get_readiness_checks(request: Request) -> dict[str, ReadinessCheck]:
    """The checks `/readyz` runs, by name, bound to the clients created at startup (main.py).

    Keyed by name so `readyz` can report a check that raised or timed out — such a check never
    returns its own `CheckResult`.
    """
    state = request.app.state
    return {
        "postgres": lambda: check_postgres(state.db_engine),
        "redis": lambda: check_redis(state.redis),
    }


def get_timeout_seconds(request: Request) -> float:
    settings: Settings = request.app.state.settings
    return settings.readiness_timeout_seconds


# ── [HAND-WRITE] the endpoint ────────────────────────────────────────────────────────────────


async def run_check(name: str, check: ReadinessCheck, timeout_seconds: float) -> CheckResult:
    """Run one check, give up after `timeout_seconds`, and never raise."""
    try:
        async with asyncio.timeout(timeout_seconds):
            return await check()
    except TimeoutError:
        return CheckResult(name, ok=False, detail="timeout")
    except Exception as error:
        return CheckResult(name, ok=False, detail=type(error).__name__)


@router.get("/readyz")
async def readyz(
    checks: Annotated[dict[str, ReadinessCheck], Depends(get_readiness_checks)],
    timeout_seconds: Annotated[float, Depends(get_timeout_seconds)],
) -> JSONResponse:
    """Run every readiness check concurrently and report the result.

    `checks` maps each check's name (e.g. "postgres") to the function that runs it. Responds 200
    `{"status": "ready", ...}` when every check is ok, otherwise 503 `{"status": "not_ready", ...}`,
    with one `"<name>": {"ok": bool, "detail": str}` entry per check.
    """
    results = await asyncio.gather(
        *(run_check(name, check, timeout_seconds) for name, check in checks.items())
    )
    all_ok = all(result.ok for result in results)
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "not_ready",
            "checks": {
                name: {"ok": result.ok, "detail": result.detail}
                for name, result in zip(checks, results, strict=True)
            },
        },
    )
