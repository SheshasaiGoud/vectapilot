"""M8 PR 1's settings (ADR 0019; P1): defaults, bounds that equal migration 0008's CHECKs, the
rules per environment, and `create_app`'s production rule. Every refusal names fields, never a
value."""

from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.conftest import make_settings
from tests.guards.test_settings import PRODUCTION
from vectapilot_api.auth.store import MAX_LINK_LIFETIME_S, MAX_SESSION_LIFETIME_S
from vectapilot_api.config import (
    CONFIRM_WRITE_BACKSTOP_SECONDS,
    PUBLIC_ORIGIN_REFUSED,
    WIDGET_CLIENT_MARGIN_SECONDS,
    Settings,
)
from vectapilot_api.main import create_app
from vectapilot_api.tools.spec import WRITE_TIMEOUT_S

DEFAULTS: Final[dict[str, Any]] = {
    "public_origin": None,
    "widget_config_dir": None,
    "ui_dir": None,
    "customer_session_lifetime_seconds": 12 * 60 * 60,  # P1
    "staff_session_lifetime_seconds": 8 * 60 * 60,  # P1
    "staff_link_lifetime_seconds": 15 * 60,  # P1
    "session_limit_business_per_minute": 60,
    "session_lookup_timeout_seconds": 2.0,
    "cookie_insecure_dev": False,
    "cors_dev_origins": (),
    "widget_turn_bound_seconds": 60.0,  # P7, D15 (a): router 15 + turn 35 + margin 10
    "widget_confirm_timeout_seconds": 15.0,  # P7
}
CANARY: Final = "https://canary-origin.example/path"
"""An invalid value that must never come back in an error's text."""


def problems(**overrides: Any) -> str:
    with pytest.raises(ValidationError) as caught:
        make_settings(**overrides)
    return str(caught.value)


def test_the_defaults_are_the_plans(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DEFAULTS:
        monkeypatch.delenv(f"VECTAPILOT_{name.upper()}", raising=False)
    settings = Settings(
        _env_file=None, database_url="postgresql://u:p@h/d", redis_url="redis://h:6379/0"
    )

    assert {name: getattr(settings, name) for name in DEFAULTS} == DEFAULTS


def test_staff_login_has_no_window_setting() -> None:
    # Step B3 (zara's review): a window keyed by a caller-written token prefix only locks staff
    # out; the link's 256 bits and single use are the control until M10's edge limits.
    assert "staff_login_limit_business_per_minute" not in Settings.model_fields


def test_the_lifetime_bounds_are_the_migrations_checks() -> None:
    fields = Settings.model_fields
    for name in ("customer_session_lifetime_seconds", "staff_session_lifetime_seconds"):
        assert make_settings(**{name: MAX_SESSION_LIFETIME_S}).model_dump()[name] == 86_400
        assert name in problems(**{name: MAX_SESSION_LIFETIME_S + 1})
        assert name in problems(**{name: 0})
    assert make_settings(staff_link_lifetime_seconds=MAX_LINK_LIFETIME_S)
    assert "staff_link_lifetime_seconds" in problems(staff_link_lifetime_seconds=3_601)
    assert "staff_link_lifetime_seconds" in problems(staff_link_lifetime_seconds=0)
    assert fields["customer_session_lifetime_seconds"].default == 43_200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_limit_business_per_minute", 0),
        ("session_lookup_timeout_seconds", 0),
        ("session_lookup_timeout_seconds", 10.5),
        ("session_lookup_timeout_seconds", float("nan")),
        ("widget_turn_bound_seconds", 0),
        ("widget_turn_bound_seconds", 121),
        ("widget_turn_bound_seconds", float("inf")),
        ("widget_confirm_timeout_seconds", 0),
        ("widget_confirm_timeout_seconds", 61),
    ],
)
def test_out_of_range_values_are_refused(field: str, value: object) -> None:
    assert field in problems(**{field: value})


def test_the_origin_settings_are_read_strictly_and_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTAPILOT_PUBLIC_ORIGIN", "https://api.vectapilot.example")
    monkeypatch.setenv("VECTAPILOT_CORS_DEV_ORIGINS", '["http://localhost:3000"]')
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "local")  # cors_dev_origins is local only
    settings = Settings(
        _env_file=None, database_url="postgresql://u:p@h/d", redis_url="redis://h:6379/0"
    )
    assert settings.public_origin == "https://api.vectapilot.example"
    assert settings.cors_dev_origins == ("http://localhost:3000",)

    for field, value in (("public_origin", CANARY), ("cors_dev_origins", (CANARY,))):
        message = problems(**{field: value})
        assert field in message and CANARY not in message


def test_production_needs_an_https_origin() -> None:
    message = problems(**{**PRODUCTION, "public_origin": "http://localhost:8000"})

    assert "public_origin must be https when environment is 'production'" in message
    assert "localhost" not in message
    assert make_settings(**PRODUCTION).public_origin == PRODUCTION["public_origin"]


def overrides_for(environment: str) -> dict[str, Any]:
    return {**(PRODUCTION if environment == "production" else {}), "environment": environment}


# The expected answers are written out here, never read from INSECURE_COOKIE_ENVIRONMENTS or
# CORS_DEV_ENVIRONMENTS: a test that reads the constant it checks passes whatever the constant
# says (the mutation review: allowing the insecure cookie in production passed every test).
@pytest.mark.parametrize(
    ("environment", "allowed"),
    [("local", True), ("test", True), ("ci", False), ("production", False)],
)
def test_the_insecure_dev_cookie_is_refused_in_ci_and_production(
    environment: str, allowed: bool
) -> None:
    overrides = overrides_for(environment)
    if allowed:
        assert make_settings(**overrides, cookie_insecure_dev=True).cookie_insecure_dev
    else:
        assert "cookie_insecure_dev may be true only when environment is local or test" in (
            problems(**overrides, cookie_insecure_dev=True)
        )


@pytest.mark.parametrize(
    ("environment", "allowed"),
    [("local", True), ("test", False), ("ci", False), ("production", False)],
)
def test_cors_dev_origins_are_refused_outside_local(environment: str, allowed: bool) -> None:
    overrides = overrides_for(environment)
    origins = ("http://localhost:3000",)
    if allowed:
        assert make_settings(**overrides, cors_dev_origins=origins).cors_dev_origins == origins
    else:
        assert "cors_dev_origins may be set only when environment is local" in problems(
            **overrides, cors_dev_origins=origins
        )
    assert make_settings(**overrides).cors_dev_origins == ()  # empty is fine everywhere


def test_production_without_a_public_origin_does_not_start() -> None:
    settings = make_settings(**{**PRODUCTION, "public_origin": None})  # Settings alone accepts it

    with pytest.raises(ValidationError) as caught:
        create_app(settings)

    [error] = caught.value.errors(include_input=False)
    assert (error["type"], error["loc"], error["msg"]) == (
        "public_origin_required",
        ("public_origin",),
        PUBLIC_ORIGIN_REFUSED,
    )


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_elsewhere_the_app_starts_without_one(environment: str) -> None:
    with TestClient(create_app(make_settings(environment=environment))) as client:
        assert client.get("/healthz").status_code == 200


def test_the_widgets_turn_bound_covers_the_router_and_the_turn() -> None:
    # D15 (a): the bound only catches an overrun, so it is never below what the turn may take.
    assert make_settings(widget_turn_bound_seconds=50.0).widget_turn_bound_seconds == 50.0
    message = problems(widget_turn_bound_seconds=49.9)

    assert "widget_turn_bound_seconds must be >= agent_router_timeout_seconds +" in message
    assert "49.9" not in message


def test_the_client_waits_the_bound_plus_a_margin() -> None:
    assert WIDGET_CLIENT_MARGIN_SECONDS == 5.0  # P7: 60 + 5 = 65 s (arithmetic)


def test_the_confirm_timeout_is_above_the_confirm_routes_write_backstop() -> None:
    assert CONFIRM_WRITE_BACKSTOP_SECONDS == WRITE_TIMEOUT_S  # written out, not imported
    message = problems(widget_confirm_timeout_seconds=CONFIRM_WRITE_BACKSTOP_SECONDS)

    assert "widget_confirm_timeout_seconds must be above the confirm route's write backstop" in (
        message
    )
    assert make_settings(widget_confirm_timeout_seconds=5.5).widget_confirm_timeout_seconds == 5.5
