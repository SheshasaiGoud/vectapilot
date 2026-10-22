"""LLM gateway settings (ADR 0006, Settings): one test per fail-fast rule.

Every rule test also checks that the refusal carries no value: the canary (in the key, a DSN
password, or the numbers that broke the rule) is absent from `str(error)`, `repr(error)` and
`errors(include_input=False)`, the forms that may be logged.
"""

import logging
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import TEST_SETTINGS_FIELDS, make_settings
from vectapilot_api import llm_cli
from vectapilot_api.config import ANTHROPIC_SDK_ENV_VARS, TLS_ENV_VARS, Settings
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.main import create_app

CANARY = "canary-key-7f3a"  # fake on purpose: never shaped like a real API key
CANARY_DSN = f"postgresql+asyncpg://u:{CANARY}-db@db.example:5432/app"
PAID: dict[str, Any] = {
    "environment": "local",
    "llm_provider": "anthropic",
    "anthropic_api_key": CANARY,
}
"""Valid Settings for the paid provider: every `_llm_rules` rule passes."""


def assert_hides(error: ValidationError, *canaries: str) -> str:
    """Check that no canary leaks into a loggable form of `error`; return its one message."""
    texts = [str(error), repr(error), str(error.errors(include_input=False))]
    for canary in (CANARY, *canaries):
        for text in texts:
            assert canary not in text
    [detail] = error.errors(include_input=False)
    return detail["msg"]


def refused(*canaries: str, **overrides: Any) -> str:
    """Build settings that break one rule (with a canary DSN); return the rule's message."""
    with pytest.raises(ValidationError) as caught:
        make_settings(database_url=CANARY_DSN, **overrides)
    return assert_hides(caught.value, *canaries)


# ── The test setup itself (conftest.py) ──────────────────────────────────────────────────────


def test_the_test_settings_set_every_field_explicitly() -> None:
    # So an exported VECTAPILOT_* variable or a value in apps/api/.env cannot change them.
    assert set(TEST_SETTINGS_FIELDS) == set(Settings.model_fields)


def test_conftest_pins_the_test_environment() -> None:
    assert os.environ["VECTAPILOT_ENVIRONMENT"] == "test"
    assert os.environ["VECTAPILOT_LLM_PROVIDER"] == "fake"
    for name in ("VECTAPILOT_ANTHROPIC_API_KEY", *ANTHROPIC_SDK_ENV_VARS, *TLS_ENV_VARS):
        assert name not in os.environ


# ── Defaults and the retry policy ────────────────────────────────────────────────────────────


def test_defaults_are_adr_0006_starting_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [k for k in os.environ if k.startswith("VECTAPILOT_")]:
        monkeypatch.delenv(name)

    settings = Settings(_env_file=None, database_url=CANARY_DSN, redis_url="redis://cache:6379/0")

    assert settings.llm_provider == "fake"
    assert (settings.llm_attempt_timeout_seconds, settings.llm_deadline_seconds) == (15.0, 30.0)
    assert (settings.llm_max_retries, settings.llm_max_repairs) == (2, 1)
    assert (settings.llm_backoff_base_seconds, settings.llm_backoff_cap_seconds) == (0.5, 8.0)
    assert settings.anthropic_api_key is None
    assert settings.api_allow_paid_llm is False  # decided 2026-10-22: opt-in, off by default


def test_the_retry_policy_comes_from_settings() -> None:
    settings = make_settings(
        llm_max_retries=3, llm_backoff_base_seconds=0.25, llm_backoff_cap_seconds=4.0
    )

    assert settings.llm_retry_policy() == RetryPolicy(
        max_retries=3, base_delay_s=0.25, max_delay_s=4.0
    )


# ── One test per rule ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("environment", ["test", "ci"])
def test_test_and_ci_allow_only_the_fake_provider(environment: str) -> None:
    message = refused(environment=environment, llm_provider="anthropic", anthropic_api_key=CANARY)

    assert message == "Value error, llm_provider must be 'fake' when environment is 'test' or 'ci'"


def test_production_refuses_the_fake_provider() -> None:
    message = refused(
        environment="production",
        llm_provider="fake",
        anthropic_api_key=CANARY,
        embeddings_provider="voyage",  # so only the LLM rule breaks (ADR 0008 has its own test)
        voyage_api_key=CANARY,
    )

    assert (
        message == "Value error, llm_provider must not be 'fake' when environment is 'production'"
    )


@pytest.mark.parametrize("key", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_the_anthropic_provider_requires_an_api_key(key: str | None) -> None:
    message = refused(environment="local", llm_provider="anthropic", anthropic_api_key=key)

    assert message == (
        "Value error, anthropic_api_key (VECTAPILOT_ANTHROPIC_API_KEY) is required when"
        " llm_provider is 'anthropic'"
    )


def test_the_refused_sdk_variables_are_adr_0006s_list() -> None:
    # Pinned, so dropping one is a visible change (the parametrized test below reads this tuple).
    assert ANTHROPIC_SDK_ENV_VARS == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_LOG",
        "ANTHROPIC_CUSTOM_HEADERS",
    )


@pytest.mark.parametrize("name", ANTHROPIC_SDK_ENV_VARS)
def test_the_anthropic_provider_refuses_sdk_variables(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, f"{CANARY}-sdk")

    message = refused(
        f"{CANARY}-sdk", environment="local", llm_provider="anthropic", anthropic_api_key=CANARY
    )

    assert message == (
        f"Value error, {name} must not be set when llm_provider is 'anthropic'"
        " (the SDK would read them): unset them"
    )
    # The rule is about the anthropic provider: the fake ignores these variables.
    assert make_settings().llm_provider == "fake"


def test_the_refused_tls_variables_are_the_decided_list() -> None:
    # Decided 2026-10-15 (ADR 0007). Pinned, so dropping one is a visible change.
    assert TLS_ENV_VARS == ("SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE")


@pytest.mark.parametrize("name", TLS_ENV_VARS)
def test_the_anthropic_provider_refuses_tls_variables(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, f"/tmp/{CANARY}-tls")  # noqa: S108 - a value only, nothing is written

    message = refused(
        f"{CANARY}-tls", environment="local", llm_provider="anthropic", anthropic_api_key=CANARY
    )

    assert message == (
        f"Value error, {name} must not be set when llm_provider is 'anthropic'"
        " (they replace the OS trust store or write TLS session keys): unset them"
    )
    # The fake makes no HTTP call, so it ignores them.
    assert make_settings().llm_provider == "fake"


def test_the_anthropic_provider_keeps_honouring_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decided 2026-10-15: proxies keep working (the SDK mounts them itself).
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example:3128")

    settings = make_settings(
        environment="local", llm_provider="anthropic", anthropic_api_key=CANARY
    )

    assert settings.llm_provider == "anthropic"


def test_a_deadline_below_the_attempt_timeout_is_refused() -> None:
    message = refused(
        "17.25", "13.75", llm_attempt_timeout_seconds=17.25, llm_deadline_seconds=13.75
    )

    assert message == "Value error, llm_deadline_seconds must be >= llm_attempt_timeout_seconds"
    # Equal is allowed: one attempt may use the whole deadline.
    assert make_settings(llm_attempt_timeout_seconds=20, llm_deadline_seconds=20)


def test_a_backoff_cap_below_the_base_is_refused() -> None:
    message = refused("3.75", "2.25", llm_backoff_base_seconds=3.75, llm_backoff_cap_seconds=2.25)

    assert message == "Value error, llm_backoff_cap_seconds must be >= llm_backoff_base_seconds"


@pytest.mark.parametrize(
    ("field", "too_high", "limit"),
    [
        ("llm_attempt_timeout_seconds", 120.25, 120),
        ("llm_deadline_seconds", 300.25, 300),
        ("llm_backoff_cap_seconds", 60.25, 60),
    ],
)
def test_a_time_setting_above_its_upper_limit_is_refused(
    field: str, too_high: float, limit: int
) -> None:
    message = refused(str(too_high), **{field: too_high})

    assert message == f"Input should be less than or equal to {limit}"


def test_the_upper_limits_themselves_are_allowed() -> None:
    settings = make_settings(
        llm_attempt_timeout_seconds=120, llm_deadline_seconds=300, llm_backoff_cap_seconds=60
    )

    assert settings.llm_deadline_seconds == 300


@pytest.mark.parametrize(
    "line",
    [
        f"VECTAPILOT_ANTHROPIC_API_KEY={CANARY}",
        f"vectapilot_anthropic_api_key='{CANARY}'",
        "VECTAPILOT_ANTHROPIC_API_KEY=",
    ],
    ids=["upper-case", "lower-case-quoted", "empty"],
)
def test_an_api_key_in_a_env_file_stops_startup(line: str, tmp_path: Path) -> None:
    # The plan's "tmp .env" test: the same file without the key resolves to test + fake (the
    # process environment that conftest pinned beats `.env`); with the key, startup stops.
    env_file = tmp_path / ".env"
    others = "VECTAPILOT_ENVIRONMENT=production\nVECTAPILOT_LLM_PROVIDER=anthropic\n"
    from_shell = {"environment", "llm_provider"}  # left to the environment conftest pinned
    fields = {k: v for k, v in TEST_SETTINGS_FIELDS.items() if k not in from_shell}

    env_file.write_text(others)
    settings = Settings(_env_file=env_file, **fields)
    assert (settings.environment, settings.llm_provider) == ("test", "fake")

    env_file.write_text(f"{others}{line}\n")
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=env_file, **fields)

    message = assert_hides(caught.value)
    assert message == (
        "VECTAPILOT_ANTHROPIC_API_KEY must come from the process environment only, never a .env"
        " file: remove it from the file"
    )
    [detail] = caught.value.errors()  # even with the input: none was ever passed in
    assert (detail["loc"], detail["type"], detail["input"]) == (
        ("anthropic_api_key",),
        "key_in_env_file",
        None,
    )


def test_a_key_from_the_process_environment_is_accepted_and_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTAPILOT_ANTHROPIC_API_KEY", CANARY)
    fields = {k: v for k, v in TEST_SETTINGS_FIELDS.items() if k != "anthropic_api_key"}

    settings = Settings(
        _env_file=None, **(fields | {"environment": "local", "llm_provider": "anthropic"})
    )

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == CANARY
    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert CANARY not in text


def test_pydantic_settings_debug_output_never_reaches_the_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # PYDANTIC_SETTINGS_DEBUG dumps every raw source value before validation; with the root
    # logger at DEBUG (VECTAPILOT_LOG_LEVEL=DEBUG after configure_logging) only the pin in
    # config.py keeps the key and the DSN password out of the log.
    monkeypatch.setenv("PYDANTIC_SETTINGS_DEBUG", "1")
    monkeypatch.setenv("VECTAPILOT_ANTHROPIC_API_KEY", f"{CANARY}-env")
    caplog.set_level(logging.DEBUG)
    fields = {k: v for k, v in TEST_SETTINGS_FIELDS.items() if k != "anthropic_api_key"}
    fields |= {"environment": "local", "llm_provider": "anthropic", "database_url": CANARY_DSN}

    Settings(_env_file=None, **fields)

    assert CANARY not in caplog.text  # in the env key and the DSN password
    assert logging.getLogger("pydantic_settings").getEffectiveLevel() == logging.WARNING


# ── The API server's opt-in to a paid provider (decided 2026-10-22, ADR 0006 Exposure) ───────
# `create_app` is only called here, never started: the lifespan (clients, the gateway) never runs,
# so nothing is built and nothing is sent.


def test_the_api_refuses_a_paid_provider_without_the_opt_in() -> None:
    settings = make_settings(database_url=CANARY_DSN, **PAID)  # valid Settings, opt-in off

    with pytest.raises(ValidationError) as caught:
        create_app(settings)  # at startup, in the factory: before any request is served

    message = assert_hides(caught.value)  # neither the key nor the DSN password
    assert message == (
        "api_allow_paid_llm (VECTAPILOT_API_ALLOW_PAID_LLM) must be true for the API server to use"
        " a paid llm_provider (any but 'fake'): it has no budgets until M7"
    )
    [detail] = caught.value.errors()  # even with the input: none was ever passed in
    assert (detail["loc"], detail["type"], detail["input"]) == (
        ("api_allow_paid_llm",),
        "paid_llm_not_opted_in",
        None,
    )


def test_an_injected_provider_does_not_bypass_the_opt_in() -> None:
    # The rule reads Settings only: a paid provider in Settings is refused whatever is injected.
    with pytest.raises(ValidationError, match="VECTAPILOT_API_ALLOW_PAID_LLM"):
        create_app(make_settings(**PAID), llm_provider=FakeProvider())


def test_the_api_accepts_a_paid_provider_with_the_opt_in() -> None:
    app = create_app(make_settings(**PAID, api_allow_paid_llm=True))

    assert app.state.settings.llm_provider == "anthropic"


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
@pytest.mark.parametrize("opt_in", [False, True], ids=["opt-in-off", "opt-in-on"])
def test_the_api_always_accepts_the_fake_provider(environment: str, opt_in: bool) -> None:
    settings = make_settings(environment=environment, api_allow_paid_llm=opt_in)

    assert create_app(settings).state.settings.llm_provider == "fake"
    # A test's injected (scripted) fake as well:
    assert create_app(settings, llm_provider=FakeProvider()).state.settings is settings


def test_the_measurement_cli_does_not_need_the_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI's `--live` loader reads the same Settings as the API, but the opt-in is the API
    # server's rule alone: the CLI's live run keeps its own --max-usd cap (test_llm_cli.py runs it
    # with settings that leave the opt-in off).
    for name in [k for k in os.environ if k.startswith("VECTAPILOT_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "local")
    monkeypatch.setenv("VECTAPILOT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("VECTAPILOT_ANTHROPIC_API_KEY", CANARY)
    monkeypatch.setenv("VECTAPILOT_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("VECTAPILOT_REDIS_URL", "redis://127.0.0.1:1/0")

    live = llm_cli.load_settings(True)  # the real loader, reading that environment

    assert (live.llm_provider, live.api_allow_paid_llm) == ("anthropic", False)
    # The same Settings stop the API server:
    with pytest.raises(ValidationError, match="VECTAPILOT_API_ALLOW_PAID_LLM"):
        create_app(live)
