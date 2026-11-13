"""Serving the real app for M6 PR 3's route tests: an extraction folder, the scripted fake model
and, where a test needs values to come back, a routing stand-in that is deliberately not H1.

`route_fields` is shesha's [HAND-WRITE] (decided D7 (b)) and raises NotImplementedError until it
is written, so the app's own service stores every extraction all-review (`internal_error`, fail
closed). A test that needs accepted values swaps in `standins.accept_everything` (the most
dangerous routing there is; the invariant check still runs after it), on a gateway of its own with
the extraction's limits and no retries (`standins.SETTINGS`), exactly as PR 2's service tests run.
The one test that runs H1 through the route waits for its switch (`pending_hand_write`).

`RecordingStore` replaces the database for the tests without one: it records every save and
answers with a fixed id and times, or raises the error it was given.
"""

import asyncio
import logging
import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.extraction.helpers import EXTRACTION_DIR, read_json, write_json
from tests.extraction.standins import SETTINGS
from tests.tools.helpers import write_corpus
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.extraction.extractor import (
    EXTRACT_ATTEMPT_TIMEOUT_S,
    EXTRACT_DEADLINE_S,
    FieldExtractor,
)
from vectapilot_api.extraction.files import BUSINESSES, TEMPLATES
from vectapilot_api.extraction.service import ExtractionOutcome, ExtractionService, Route
from vectapilot_api.extraction.store import ExtractionStore, StoredExtraction
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.main import create_app

PATH: Final = "/v1/extractions"
PDF: Final = "application/pdf"
STORED_ID: Final = UUID("0b6f5a52-6c2e-4c8e-9a51-6f2b7f1d2c3e")
CREATED: Final = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
HANG_S: Final = 5.0
"""How long a hanging `RecordingStore` waits: far above any test's request limit, and bounded, so
a broken limit fails the test (a 500) instead of hanging the run."""


@dataclass
class Saved:
    outcome: ExtractionOutcome
    business_id: str
    file_sha256: str
    trace_id: str
    retention_days: int


@dataclass
class RecordingStore:
    """A stand-in `ExtractionStore`: records each save; raises `error` if given, or with `hang`
    waits `HANG_S` (a store that does not answer in time), then raises: the request limit should
    have cancelled it long before."""

    error: BaseException | None = None
    hang: bool = False
    saved: list[Saved] = field(default_factory=list)

    async def save(
        self,
        outcome: ExtractionOutcome,
        *,
        business_id: str,
        file_sha256: str,
        trace_id: str,
        retention_days: int,
    ) -> StoredExtraction:
        self.saved.append(Saved(outcome, business_id, file_sha256, trace_id, retention_days))
        if self.hang:
            await asyncio.sleep(HANG_S)
            raise AssertionError("the request limit did not fire")
        if self.error is not None:
            raise self.error
        return StoredExtraction(STORED_ID, CREATED, CREATED + timedelta(days=retention_days))


def gateway_on(provider: FakeProvider) -> LLMGateway:
    """The extraction's gateway on `provider`: its limits, no retries, no cache (as
    `build_extraction_gateway` with `standins.SETTINGS`)."""
    return LLMGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=EXTRACT_ATTEMPT_TIMEOUT_S,
        deadline_s=EXTRACT_DEADLINE_S,
        retry_policy=SETTINGS.llm_retry_policy(),
        max_repairs=SETTINGS.llm_max_repairs,
        cache=None,
    )


def extraction_folder(destination: Path, businesses: Mapping[str, str]) -> tuple[Path, Path]:
    """An extraction folder with the committed templates and one business file per id in
    `businesses` (new id → the committed business whose file it copies), and a corpus folder of
    matching manifests: `(extraction dir, corpus dir)`."""
    folder = destination / "extraction"
    shutil.copytree(EXTRACTION_DIR / TEMPLATES, folder / TEMPLATES)
    (folder / BUSINESSES).mkdir()
    types: dict[str, str] = {}
    for business_id, source in businesses.items():
        data = read_json(EXTRACTION_DIR / BUSINESSES / f"{source}.json") | {
            "business_id": business_id
        }
        write_json(folder / BUSINESSES / f"{business_id}.json", data)
        types[business_id] = str(data["business_type"])
    return folder, write_corpus(destination / "corpus", types)


@dataclass
class Served:
    client: TestClient
    provider: FakeProvider
    app: FastAPI

    def upload(
        self,
        business_id: str | None,
        data: bytes,
        *,
        kind: str = "referral_letter",
        content_type: str | None = PDF,
        params: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        headers = {} if business_id is None else {BUSINESS_HEADER: business_id}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return self.client.post(
            PATH,
            content=data,
            params=dict(params) if params is not None else {"kind": kind},
            headers=headers,
        )


@contextmanager
def served(
    settings: Settings,
    provider: FakeProvider | None = None,
    *,
    route: Route | None = None,
    store: ExtractionStore | None = None,
    caplog: pytest.LogCaptureFixture | None = None,
) -> Iterator[Served]:
    """The real app. `route` (a stand-in, never H1) replaces the service's routing on a gateway
    of its own over `provider`; without it, the app's own service (H1's `route_fields`) runs on
    `provider` as the route's fake. `store` replaces the database store. Pass `caplog` to read
    the records: `create_app` replaces the root logger's handlers, pytest's included, so it is
    added back here (as tests/answer/apps.py)."""
    fake = provider or FakeProvider()
    app = create_app(settings, extraction_provider=None if route is not None else fake)
    root = logging.getLogger()
    if caplog is not None:
        root.addHandler(caplog.handler)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            if route is not None:
                app.state.extraction_service = ExtractionService(
                    FieldExtractor(gateway_on(fake)), route=route
                )
            if store is not None:
                app.state.extraction_store = store
            yield Served(client, fake, app)
    finally:
        if caplog is not None:
            root.removeHandler(caplog.handler)
