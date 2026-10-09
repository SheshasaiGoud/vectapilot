"""Liveness and readiness endpoints.

- `/healthz` — liveness: "can this process answer at all?" It never checks dependencies: if the
  database is down, restarting the API would not fix it, so liveness must stay green.
- `/readyz` — readiness: "should traffic be sent here right now?" It checks Postgres and Redis.

The readiness logic is [HAND-WRITE]: implemented by the human lead.
Guide: docs/learning/m0-readiness.md · Tests: tests/test_readyz.py
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
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

    TODO(shesha): implement. Never raise: any failure becomes `ok=False` with a short `detail`
    such as the exception's class name. Never put the connection URL or password in `detail`.
    """
    raise NotImplementedError("HAND-WRITE: see docs/learning/m0-readiness.md")


async def check_redis(client: Redis) -> CheckResult:
    """Return `CheckResult("redis", ok=True)` if Redis answers `PING`.

    TODO(shesha): implement. Same rules as `check_postgres`.
    """
    raise NotImplementedError("HAND-WRITE: see docs/learning/m0-readiness.md")


# ── Wiring (provided): FastAPI dependencies, so tests can swap in fakes ─────────────────────


def get_readiness_checks(request: Request) -> list[ReadinessCheck]:
    """The checks `/readyz` runs, bound to the clients created at startup (see main.py)."""
    state = request.app.state
    return [
        lambda: check_postgres(state.db_engine),
        lambda: check_redis(state.redis),
    ]


def get_timeout_seconds(request: Request) -> float:
    settings: Settings = request.app.state.settings
    return settings.readiness_timeout_seconds


# ── [HAND-WRITE] the endpoint ────────────────────────────────────────────────────────────────


@router.get("/readyz")
async def readyz(
    checks: Annotated[list[ReadinessCheck], Depends(get_readiness_checks)],
    timeout_seconds: Annotated[float, Depends(get_timeout_seconds)],
) -> JSONResponse:
    """Run every readiness check and report the result.

    TODO(shesha): implement. Requirements (tests/test_readyz.py checks each one):
    - Run the checks concurrently, each limited to `timeout_seconds`. A check that times out or
      raises counts as failed, with `detail` "timeout" or the exception's class name.
    - Respond 200 with `{"status": "ready", "checks": {...}}` when every check is ok, otherwise
      503 with `{"status": "not_ready", "checks": {...}}`. Each entry in `checks` looks like
      `"<name>": {"ok": bool, "detail": str}`.
    """
    raise NotImplementedError("HAND-WRITE: see docs/learning/m0-readiness.md")
