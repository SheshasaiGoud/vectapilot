import os

# Before any app import (ADR 0006): tests run as `test` with the fake provider, and no key or SDK
# variable from the developer's shell reaches them. `Settings` refuses anything but the fake
# provider in `test`, so a test can never make a paid call. (Ruff allows `os.environ` changes
# above imports.)
os.environ["VECTAPILOT_ENVIRONMENT"] = "test"
os.environ["VECTAPILOT_LLM_PROVIDER"] = "fake"
os.environ["VECTAPILOT_EMBEDDINGS_PROVIDER"] = "fake"  # ADR 0008: tests never embed for real
os.environ.pop("VECTAPILOT_ANTHROPIC_API_KEY", None)
os.environ.pop("VECTAPILOT_VOYAGE_API_KEY", None)
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

from collections.abc import Iterator, Mapping
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
    "api_allow_paid_llm": False,
    "embeddings_provider": "fake",
    "embeddings_model": None,
    "embeddings_dimension": 1024,
    "embeddings_timeout_seconds": 30.0,
    "voyage_api_key": None,
    "rag_min_score": 0.3,
    "rag_context_k": 5,
    "rag_weak_evidence_outcome": "dont_know",
    "rag_retrieval_mode": "vector",
    "rag_candidate_k": 20,
    "rag_rrf_k": 60,
    "tools_config_dir": None,
    "action_lifetime_seconds": 900,
    "corpus_dir": None,
    "extraction_config_dir": None,
    "agent_llm_attempt_timeout_seconds": 6.0,
    "agent_llm_deadline_seconds": 8.0,
    "agent_max_steps": 4,
    "agent_turn_timeout_seconds": 35.0,
    "agent_router": "off",
    "agent_router_model_dir": None,
    "agent_router_timeout_seconds": 15.0,
    "guard_routes": "on",
    "guard_answer": "off",
    "guard_config_dir": None,
    # Far above test volume (M7 PR 5's plan; diego's review): the canary posts one BAIT_CUSTOMER
    # throughout, and CI's integration job shares one Redis database across tests. The plan's
    # placeholders stay the Settings defaults (`test_limit_settings.py` pins them); a test that
    # needs a small limit passes it.
    "guard_limit_business_per_minute": 100_000,
    "guard_limit_customer_per_minute": 100_000,
    "guard_budget_usd_per_business_per_day": "100",
    "guard_redis_timeout_seconds": 0.25,
    # M8 PR 1 (ADR 0019). No origin: every Origin-checked route refuses unless a test sets one
    # (TestClient's own is `http://testserver`, which `auth.origins` refuses; tests use https).
    "public_origin": None,
    "widget_config_dir": None,
    "ui_dir": None,
    "customer_session_lifetime_seconds": 43_200,
    "staff_session_lifetime_seconds": 28_800,
    "staff_link_lifetime_seconds": 900,
    # Far above test volume, as the guard limits above; `test_session_settings.py` pins the
    # defaults.
    "session_limit_business_per_minute": 100_000,
    "session_lookup_timeout_seconds": 2.0,
    "cookie_insecure_dev": False,
    "cors_dev_origins": (),
    "widget_turn_bound_seconds": 60.0,
    "widget_confirm_timeout_seconds": 15.0,
}
"""Every Settings field, explicitly (a test checks none is missing): none comes from the shell."""


def make_settings(**overrides: Any) -> Settings:
    """Settings from `TEST_SETTINGS_FIELDS` plus `overrides`, never from a `.env` file."""
    return Settings(_env_file=None, **(TEST_SETTINGS_FIELDS | overrides))


UNREACHABLE_SETTINGS = make_settings()


def runs_integration(environ: Mapping[str, str]) -> bool:
    return environ.get("RUN_INTEGRATION") == "1"


# Integration tests talk to real Postgres/Redis: CI provides them; locally run `make up` first.
integration = pytest.mark.skipif(
    not runs_integration(os.environ),
    reason="needs real Postgres + Redis; set RUN_INTEGRATION=1",
)

CI_WITHOUT_INTEGRATION: Final = (
    "GITHUB_ACTIONS=true but RUN_INTEGRATION is not 1: CI would pass with every database test"
    " skipped. Set RUN_INTEGRATION=1 in the workflow."
)


def integration_skipped_in_ci(environ: Mapping[str, str]) -> bool:
    """On GitHub Actions the integration tests must run: a skip there would look like a pass."""
    return environ.get("GITHUB_ACTIONS") == "true" and not runs_integration(environ)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Stop the whole run, before collection, when CI would skip the integration tests."""
    if integration_skipped_in_ci(os.environ):
        pytest.exit(CI_WITHOUT_INTEGRATION, returncode=pytest.ExitCode.USAGE_ERROR)


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
