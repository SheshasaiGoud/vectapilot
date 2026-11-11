"""Settings for the tests: explicit values, never a `.env` file."""

from typing import Any, Final

from tests.fakes import CORPUS_DIR, SANDBOX_DIR
from vectapilot_api.config import Settings

UNREACHABLE_DB: Final = (
    "postgresql+asyncpg://vectapilot_app:not-a-real-secret@127.0.0.1:1/vectapilot"
)
"""Port 1 is unreachable on purpose: a unit test must never depend on a running database."""


def make_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "environment": "test",
        "database_url": UNREACHABLE_DB,
        "redis_url": "redis://:not-a-real-secret@127.0.0.1:1/0",
        "llm_provider": "fake",
        "embeddings_provider": "fake",
        "tools_config_dir": SANDBOX_DIR,
        "corpus_dir": CORPUS_DIR,
    }
    return Settings(_env_file=None, **(fields | overrides))
