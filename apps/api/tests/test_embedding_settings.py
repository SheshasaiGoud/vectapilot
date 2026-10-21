"""Embedding settings (ADR 0008): one test per fail-fast rule, and the Voyage key's handling.

Every refusal is checked for the canary (in the key or a DSN password): absent from `str(error)`,
`repr(error)` and `errors(include_input=False)`, the forms that may be logged.
"""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.conftest import TEST_SETTINGS_FIELDS
from tests.test_llm_settings import CANARY, assert_hides, refused
from vectapilot_api.config import PROCESS_ENV_ONLY, TLS_ENV_VARS, Settings


def test_conftest_pins_the_fake_embedder_and_no_voyage_key() -> None:
    assert os.environ["VECTAPILOT_EMBEDDINGS_PROVIDER"] == "fake"
    assert "VECTAPILOT_VOYAGE_API_KEY" not in os.environ


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [k for k in os.environ if k.startswith("VECTAPILOT_")]:
        monkeypatch.delenv(name)

    settings = Settings(_env_file=None, database_url="postgresql://u@h/d", redis_url="redis://h")

    assert (settings.embeddings_provider, settings.embeddings_model) == ("fake", None)
    assert (settings.embeddings_dimension, settings.embeddings_timeout_seconds) == (1024, 30.0)
    assert settings.voyage_api_key is None


@pytest.mark.parametrize("environment", ["test", "ci"])
def test_test_and_ci_allow_only_the_fake_embedder(environment: str) -> None:
    message = refused(environment=environment, embeddings_provider="voyage", voyage_api_key=CANARY)

    assert message == (
        "Value error, embeddings_provider must be 'fake' when environment is 'test' or 'ci'"
    )


def test_production_refuses_the_fake_embedder() -> None:
    message = refused(environment="production", llm_provider="anthropic", anthropic_api_key=CANARY)

    assert message == (
        "Value error, embeddings_provider must not be 'fake' when environment is 'production'"
    )


@pytest.mark.parametrize("key", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_the_voyage_provider_requires_a_key(key: str | None) -> None:
    message = refused(environment="local", embeddings_provider="voyage", voyage_api_key=key)

    assert message == (
        "Value error, voyage_api_key (VECTAPILOT_VOYAGE_API_KEY) is required when"
        " embeddings_provider is 'voyage'"
    )


@pytest.mark.parametrize("name", TLS_ENV_VARS)
def test_the_voyage_provider_refuses_tls_variables(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, f"/tmp/{CANARY}-tls")  # noqa: S108 - a value only, nothing is written

    message = refused(
        f"{CANARY}-tls", environment="local", embeddings_provider="voyage", voyage_api_key=CANARY
    )

    assert message == (
        f"Value error, {name} must not be set when embeddings_provider is 'voyage'"
        " (they replace the OS trust store or write TLS session keys): unset them"
    )


def test_the_keys_read_only_from_the_process_environment() -> None:
    assert PROCESS_ENV_ONLY == {
        "anthropic_api_key": "VECTAPILOT_ANTHROPIC_API_KEY",
        "voyage_api_key": "VECTAPILOT_VOYAGE_API_KEY",
    }


@pytest.mark.parametrize(
    "line",
    [f"VECTAPILOT_VOYAGE_API_KEY={CANARY}", f"vectapilot_voyage_api_key='{CANARY}'"],
    ids=["upper-case", "lower-case-quoted"],
)
def test_a_voyage_key_in_a_env_file_stops_startup(line: str, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"VECTAPILOT_EMBEDDINGS_PROVIDER=voyage\n{line}\n")
    fields = {k: v for k, v in TEST_SETTINGS_FIELDS.items() if k != "embeddings_provider"}

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=env_file, **fields)

    message = assert_hides(caught.value)
    assert message == (
        "VECTAPILOT_VOYAGE_API_KEY must come from the process environment only, never a .env"
        " file: remove it from the file"
    )
    [detail] = caught.value.errors()
    assert (detail["loc"], detail["type"], detail["input"]) == (
        ("voyage_api_key",),
        "key_in_env_file",
        None,
    )


def test_both_keys_in_a_env_file_are_both_reported(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"VECTAPILOT_ANTHROPIC_API_KEY={CANARY}\nVECTAPILOT_VOYAGE_API_KEY={CANARY}\n"
    )

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=env_file, **TEST_SETTINGS_FIELDS)

    assert [d["loc"] for d in caught.value.errors()] == [
        ("anthropic_api_key",),
        ("voyage_api_key",),
    ]
    for text in (str(caught.value), repr(caught.value)):
        assert CANARY not in text


def test_a_voyage_key_from_the_process_environment_is_accepted_and_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTAPILOT_VOYAGE_API_KEY", CANARY)
    fields = {k: v for k, v in TEST_SETTINGS_FIELDS.items() if k != "voyage_api_key"}

    settings = Settings(
        _env_file=None, **(fields | {"environment": "local", "embeddings_provider": "voyage"})
    )

    assert settings.voyage_api_key is not None
    assert settings.voyage_api_key.get_secret_value() == CANARY
    for text in (repr(settings), str(settings), settings.model_dump_json()):
        assert CANARY not in text
