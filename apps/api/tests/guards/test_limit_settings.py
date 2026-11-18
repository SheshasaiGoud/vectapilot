"""M7 PR 5 (G9): the limit and budget placeholders in Settings, the turn rule's Redis bound, the
reworded paid opt-in, and `create_app`'s `limiter` seam."""

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import make_settings
from vectapilot_api.config import Settings
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.limits import LimitPolicy, RedisLimiter
from vectapilot_api.main import create_app

PLACEHOLDERS = {
    "guard_limit_business_per_minute": 120,
    "guard_limit_customer_per_minute": 10,
    "guard_budget_usd_per_business_per_day": Decimal("0.50"),
    "guard_redis_timeout_seconds": 0.25,
}


def problems(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        make_settings(**overrides)
    return str(caught.value)


def test_the_defaults_are_the_plans_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in PLACEHOLDERS:
        monkeypatch.delenv(f"VECTAPILOT_{name.upper()}", raising=False)
    settings = Settings(
        _env_file=None, database_url="postgresql://u:p@h/d", redis_url="redis://h:6379/0"
    )
    assert {name: getattr(settings, name) for name in PLACEHOLDERS} == PLACEHOLDERS
    # Today's defaults hold the turn rule with the Redis bound: 8 x 4 + 2 x 4 x 0.25 = 34 <= 35.
    steps = settings.agent_max_steps
    assert (
        settings.agent_llm_deadline_seconds * steps
        + 2 * steps * settings.guard_redis_timeout_seconds
        == 34.0
        <= settings.agent_turn_timeout_seconds
    )


def test_the_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTAPILOT_GUARD_LIMIT_BUSINESS_PER_MINUTE", "30")
    monkeypatch.setenv("VECTAPILOT_GUARD_LIMIT_CUSTOMER_PER_MINUTE", "3")
    monkeypatch.setenv("VECTAPILOT_GUARD_BUDGET_USD_PER_BUSINESS_PER_DAY", "0.25")
    monkeypatch.setenv("VECTAPILOT_GUARD_REDIS_TIMEOUT_SECONDS", "0.1")
    settings = Settings(
        _env_file=None, database_url="postgresql://u:p@h/d", redis_url="redis://h:6379/0"
    )
    assert LimitPolicy.from_settings(settings).budget_micro_usd == 250_000
    assert (settings.guard_limit_business_per_minute, settings.guard_limit_customer_per_minute) == (
        30,
        3,
    )
    assert settings.guard_redis_timeout_seconds == 0.1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("guard_limit_business_per_minute", 0),
        ("guard_limit_customer_per_minute", 0),
        ("guard_budget_usd_per_business_per_day", "0"),
        ("guard_budget_usd_per_business_per_day", "-0.01"),
        ("guard_budget_usd_per_business_per_day", "100.01"),
        ("guard_budget_usd_per_business_per_day", "0.0000001"),
        ("guard_budget_usd_per_business_per_day", "NaN"),
        ("guard_redis_timeout_seconds", 0),
        ("guard_redis_timeout_seconds", float("inf")),
    ],
)
def test_out_of_range_values_are_refused(field: str, value: object) -> None:
    assert field in problems(**{field: value})


def test_a_customer_limit_above_the_business_limit_is_refused() -> None:
    message = "guard_limit_customer_per_minute must be <= guard_limit_business_per_minute"
    assert message in problems(guard_limit_business_per_minute=5, guard_limit_customer_per_minute=6)
    make_settings(guard_limit_business_per_minute=5, guard_limit_customer_per_minute=5)


def test_the_redis_timeout_is_at_most_the_readiness_timeout() -> None:
    message = "guard_redis_timeout_seconds must be <= readiness_timeout_seconds"
    assert message in problems(readiness_timeout_seconds=0.2)
    make_settings(readiness_timeout_seconds=0.25)


def test_a_redis_timeout_that_breaks_the_turn_is_refused() -> None:
    from vectapilot_api.config import TURN_TIMEOUT_RULE

    # 8 x 4 + 2 x 4 x 0.5 = 36 > 35 (the readiness bound is raised so only the turn refuses).
    text = problems(guard_redis_timeout_seconds=0.5, readiness_timeout_seconds=1.0)
    assert TURN_TIMEOUT_RULE in text
    assert "readiness_timeout_seconds" not in text


# ── The create_app seam ──────────────────────────────────────────────────────────────────────


def test_an_injected_limiter_is_the_apps_limiter() -> None:
    fake = FakeLimiter()
    with TestClient(create_app(make_settings(), limiter=fake)) as client:
        assert client.app.state.limiter is fake  # type: ignore[attr-defined]
        assert client.get("/healthz").status_code == 200
    assert fake.calls == []  # nothing reads it until PR 6 wires the routes


def test_without_one_the_app_builds_the_redis_limiter_and_starts_with_redis_down() -> None:
    # The test settings' Redis is port 1: building the limiter connects to nothing, so the app
    # starts and /healthz stays green (the CI image check's condition, G9).
    settings = make_settings(
        guard_limit_business_per_minute=50,
        guard_limit_customer_per_minute=5,
        guard_redis_timeout_seconds=0.1,
    )
    with TestClient(create_app(settings)) as client:
        limiter = client.app.state.limiter  # type: ignore[attr-defined]
        assert isinstance(limiter, RedisLimiter)
        assert limiter.policy == LimitPolicy.from_settings(settings)
        assert client.get("/healthz").status_code == 200


def test_the_app_limiter_uses_the_redis_timeout() -> None:
    # [mutation check] Not the readiness timeout (2 s): every guarded call on a blackholed Redis
    # would wait that long, and TURN_TIMEOUT_RULE's arithmetic would no longer hold.
    settings = make_settings(guard_redis_timeout_seconds=0.1)
    with TestClient(create_app(settings)) as client:
        limiter = client.app.state.limiter  # type: ignore[attr-defined]
        assert limiter._timeout_s == settings.guard_redis_timeout_seconds
        assert settings.guard_redis_timeout_seconds != settings.readiness_timeout_seconds


@pytest.mark.parametrize(
    "field", ["guard_limit_business_per_minute", "guard_limit_customer_per_minute"]
)
def test_the_limits_have_an_upper_bound(field: str) -> None:
    # [mutation check] le=100_000 on both fields, refused by the field itself (the customer rule
    # alone would also refuse a customer limit above the business's).
    text = problems(**{"guard_limit_customer_per_minute": 1, field: 100_001})
    assert f"{field}\n  Input should be less than or equal to 100000" in text


def test_the_paid_opt_in_names_what_stays_unbudgeted() -> None:
    from vectapilot_api.config import API_PAID_REFUSED

    assert "no budgets until M7" not in API_PAID_REFUSED
    assert "embeddings, /v1/answer and /v1/kb/search have no budget" in API_PAID_REFUSED
