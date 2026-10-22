"""The real app for PR 6's route tests, with the search and answer services built on stand-ins.

The real `X-Business-Id` dependency runs; only the registry lookup is replaced (as in
tests/kb/test_routes.py). Search runs over `tests/retrieval/standins.py` (no SQL), the embedder
is the scripted fake, and the answer model is a fake provider behind the real `LLMGateway` (so
the `llm_call` record is the real one).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.embeddings.scripted import FakeClock
from tests.retrieval.standins import Search, build_search
from vectapilot_api.answer.router import get_answer_service
from vectapilot_api.answer.service import AnswerService, RagConfig
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import get_search_service

A: Final = "clinic-a"
B: Final = "vet-b"
CONFIG: Final = RagConfig(min_score=0.3, context_k=5, weak_evidence_outcome="dont_know")
"""The test threshold: an explicit value here, so no test depends on the Settings default."""


def gateway_for(provider: Provider, clock: FakeClock | None = None) -> LLMGateway:
    """The real gateway and packaged catalog on `provider`; backoff waits move a fake clock."""
    clock = clock or FakeClock()
    return LLMGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )


def answer_service(
    search: Search, provider: Provider | None = None, config: RagConfig = CONFIG
) -> AnswerService:
    return AnswerService(search.service, gateway_for(provider or FakeProvider()), config)


@contextmanager
def serve(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    search: Search | None = None,
    answer: AnswerService | None = None,
    *,
    registered: bool = True,
    caplog: pytest.LogCaptureFixture | None = None,
) -> Iterator[TestClient]:
    """The app as a client. Pass `caplog` to read its records: `create_app` replaces the root
    logger's handlers (`configure_logging`), pytest's capture handler included, so it is added
    back here."""

    async def business_exists(self: Database, business_id: str) -> bool:
        return registered

    monkeypatch.setattr(Database, "business_exists", business_exists)
    search = search or build_search()
    app = create_app(settings)
    app.dependency_overrides[get_search_service] = lambda: search.service
    app.dependency_overrides[get_answer_service] = lambda: answer or answer_service(search)
    root = logging.getLogger()
    if caplog is not None:
        root.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if caplog is not None:
            root.removeHandler(caplog.handler)


def headers(business: str | None) -> dict[str, str]:
    return {} if business is None else {BUSINESS_HEADER: business}


def get_search(client: TestClient, business: str | None = A, **params: Any) -> httpx2.Response:
    return client.get("/v1/kb/search", params=params, headers=headers(business))


def post_answer(client: TestClient, body: Any, business: str | None = A) -> httpx2.Response:
    return client.post("/v1/answer", json=body, headers=headers(business))
