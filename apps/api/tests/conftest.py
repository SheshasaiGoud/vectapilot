import os

# Before any app import (ADR 0006): tests run as `test` with the fake provider, and no key or SDK
# variable from the developer's shell reaches them. `Settings` refuses anything but the fake
# provider in `test`, so a test can never make a paid call. (Ruff allows `os.environ` changes
# above imports.)
os.environ["VECTAPILOT_ENVIRONMENT"] = "test"
os.environ["VECTAPILOT_LLM_PROVIDER"] = "fake"
os.environ.pop("VECTAPILOT_ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ.pop("ANTHROPIC_LOG", None)
os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
# Settings refuses these with the anthropic provider (config.TLS_ENV_VARS); a developer's shell may
# set them, which would break the tests that build anthropic settings.
os.environ.pop("SSL_CERT_FILE", None)
os.environ.pop("SSL_CERT_DIR", None)
os.environ.pop("SSLKEYLOGFILE", None)

from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vectapilot_api.config import Settings
from vectapilot_api.main import create_app

TEST_SETTINGS_FIELDS: Final[dict[str, Any]] = {
    "environment": "test",
    # Port 1 is unreachable on purpose: unit tests must never depend on a running database.
    "database_url": "postgresql+asyncpg://vectapilot:not-a-real-secret@127.0.0.1:1/vectapilot",
    "redis_url": "redis://:not-a-real-secret@127.0.0.1:1/0",
    "readiness_timeout_seconds": 0.5,
    "log_level": "INFO",
    "llm_provider": "fake",
    "llm_attempt_timeout_seconds": 15.0,
    "llm_deadline_seconds": 30.0,
    "llm_max_retries": 2,
    "llm_backoff_base_seconds": 0.5,
    "llm_backoff_cap_seconds": 8.0,
    "llm_max_repairs": 1,
    "anthropic_api_key": None,
}
"""Every Settings field, explicitly (a test checks none is missing): none comes from the shell."""


def make_settings(**overrides: Any) -> Settings:
    """Settings from `TEST_SETTINGS_FIELDS` plus `overrides`, never from a `.env` file."""
    return Settings(_env_file=None, **(TEST_SETTINGS_FIELDS | overrides))


UNREACHABLE_SETTINGS = make_settings()

# Integration tests talk to real Postgres/Redis: CI provides them; locally run `make up` first.
integration = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="needs real Postgres + Redis; set RUN_INTEGRATION=1",
)


@pytest.fixture
def settings() -> Settings:
    return UNREACHABLE_SETTINGS.model_copy()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # `with` runs the app's lifespan (startup and shutdown), like a real server.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
