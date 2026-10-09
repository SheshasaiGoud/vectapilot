"""Tests for the readiness checks you hand-write (guide: docs/learning/m0-readiness.md).

Today they fail with NotImplementedError, so the whole file is marked xfail(strict=True).
When your implementation makes them pass, strict mode turns each "unexpected pass" into a
failure — that is your signal to delete the `pytestmark` line below. Do not edit the tests to
make them pass; change the code.
"""

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import integration
from vectapilot_api.config import Settings
from vectapilot_api.health import (
    CheckResult,
    ReadinessCheck,
    check_postgres,
    check_redis,
    get_readiness_checks,
)

pytestmark = pytest.mark.xfail(strict=True, reason="HAND-WRITE: readiness checks not implemented")


# ── Fake checks: the endpoint logic is tested without any real database ─────────────────────


def passing(name: str, delay: float = 0.0) -> ReadinessCheck:
    async def check() -> CheckResult:
        await asyncio.sleep(delay)
        return CheckResult(name, ok=True)

    return check


def failing(name: str) -> ReadinessCheck:
    async def check() -> CheckResult:
        return CheckResult(name, ok=False, detail="ConnectionRefusedError")

    return check


def raising(name: str) -> ReadinessCheck:
    async def check() -> CheckResult:
        raise RuntimeError(f"{name} exploded")

    return check


def use_checks(app: FastAPI, *checks: ReadinessCheck) -> None:
    app.dependency_overrides[get_readiness_checks] = lambda: list(checks)


# ── The /readyz endpoint ─────────────────────────────────────────────────────────────────────


def test_all_checks_passing_returns_200(app: FastAPI, client: TestClient) -> None:
    use_checks(app, passing("postgres"), passing("redis"))

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "postgres": {"ok": True, "detail": "ok"},
            "redis": {"ok": True, "detail": "ok"},
        },
    }


def test_any_failing_check_returns_503(app: FastAPI, client: TestClient) -> None:
    use_checks(app, passing("postgres"), failing("redis"))

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["redis"] == {"ok": False, "detail": "ConnectionRefusedError"}
    assert body["checks"]["postgres"]["ok"] is True


def test_a_check_that_raises_is_reported_not_crashed(app: FastAPI, client: TestClient) -> None:
    use_checks(app, raising("postgres"), passing("redis"))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == {"ok": False, "detail": "RuntimeError"}


def test_a_slow_check_times_out(app: FastAPI, client: TestClient) -> None:
    # The test settings use readiness_timeout_seconds=0.5.
    use_checks(app, passing("postgres", delay=5), passing("redis"))

    started = time.perf_counter()
    response = client.get("/readyz")
    elapsed = time.perf_counter() - started

    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == {"ok": False, "detail": "timeout"}
    assert elapsed < 2, "a stuck dependency must not hang the readiness probe"


def test_checks_run_concurrently(app: FastAPI, client: TestClient) -> None:
    use_checks(app, passing("postgres", delay=0.3), passing("redis", delay=0.3))

    started = time.perf_counter()
    response = client.get("/readyz")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.5, f"took {elapsed:.2f}s — the checks seem to run one after another"


# ── The check functions, against servers that are not there ─────────────────────────────────


@pytest.mark.anyio
async def test_check_postgres_reports_an_unreachable_database(settings: Settings) -> None:
    engine = create_async_engine(str(settings.database_url))
    try:
        result = await check_postgres(engine)
    finally:
        await engine.dispose()

    assert result.name == "postgres"
    assert result.ok is False
    assert "not-a-real-secret" not in result.detail, "never leak credentials in a health check"


@pytest.mark.anyio
async def test_check_redis_reports_an_unreachable_server(settings: Settings) -> None:
    client = Redis.from_url(str(settings.redis_url))
    try:
        result = await check_redis(client)
    finally:
        await client.aclose()

    assert result.name == "redis"
    assert result.ok is False
    assert "not-a-real-secret" not in result.detail, "never leak credentials in a health check"


# ── Against real services (CI provides them; locally: `make up`, then RUN_INTEGRATION=1) ────


@integration
@pytest.mark.anyio
async def test_check_postgres_passes_against_a_real_database() -> None:
    settings = Settings()
    engine = create_async_engine(str(settings.database_url))
    try:
        assert await check_postgres(engine) == CheckResult("postgres", ok=True)
    finally:
        await engine.dispose()


@integration
@pytest.mark.anyio
async def test_check_redis_passes_against_a_real_server() -> None:
    settings = Settings()
    client = Redis.from_url(str(settings.redis_url))
    try:
        assert await check_redis(client) == CheckResult("redis", ok=True)
    finally:
        await client.aclose()
