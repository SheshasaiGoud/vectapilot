import os

# Before any API import, as apps/api/tests/conftest.py: tests run as `test` with the fake
# providers, and no key or SDK variable from the developer's shell reaches them. The MCP server
# never builds a model or an embedder; Settings still validates these.
os.environ["VECTAPILOT_ENVIRONMENT"] = "test"
os.environ["VECTAPILOT_LLM_PROVIDER"] = "fake"
os.environ["VECTAPILOT_EMBEDDINGS_PROVIDER"] = "fake"
os.environ.pop("VECTAPILOT_ANTHROPIC_API_KEY", None)
os.environ.pop("VECTAPILOT_VOYAGE_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ.pop("ANTHROPIC_BASE_URL", None)
os.environ.pop("ANTHROPIC_LOG", None)
os.environ.pop("ANTHROPIC_CUSTOM_HEADERS", None)
os.environ.pop("SSL_CERT_FILE", None)
os.environ.pop("SSL_CERT_DIR", None)
os.environ.pop("SSLKEYLOGFILE", None)

from collections.abc import Mapping
from typing import Final

import pytest

CI_WITHOUT_INTEGRATION: Final = (
    "GITHUB_ACTIONS=true but RUN_INTEGRATION is not 1: CI would pass with every database test"
    " skipped. Set RUN_INTEGRATION=1 in the workflow."
)


def runs_integration(environ: Mapping[str, str]) -> bool:
    return environ.get("RUN_INTEGRATION") == "1"


def integration_skipped_in_ci(environ: Mapping[str, str]) -> bool:
    """On GitHub Actions the integration tests must run: a skip there would look like a pass."""
    return environ.get("GITHUB_ACTIONS") == "true" and not runs_integration(environ)


def pytest_sessionstart(session: pytest.Session) -> None:
    """Stop the whole run, before collection, when CI would skip the integration tests (the
    `mcp` job's gate, scripts/integration_gate.py, also fails on zero integration tests)."""
    if integration_skipped_in_ci(os.environ):
        pytest.exit(CI_WITHOUT_INTEGRATION, returncode=pytest.ExitCode.USAGE_ERROR)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
