import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vectapilot_api.config import Settings
from vectapilot_api.main import create_app

# Port 1 is unreachable on purpose: unit tests must never depend on a running database.
UNREACHABLE_SETTINGS = Settings(
    environment="test",
    database_url="postgresql+asyncpg://vectapilot:not-a-real-secret@127.0.0.1:1/vectapilot",
    redis_url="redis://:not-a-real-secret@127.0.0.1:1/0",
    readiness_timeout_seconds=0.5,
)

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
