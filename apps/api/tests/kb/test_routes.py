"""`POST /v1/kb/documents` and `POST /v1/kb/reindex` without a database (M2 PR 5).

The real app and the real `X-Business-Id` dependency; the registry lookup is replaced (as in
test_business_header.py), the store is in memory (`fakes.py`) and the embedder is the fake. The same
routes against real Postgres: test_postgres_store.py and the canary suite's write paths.
"""

import importlib.util
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from tests.embeddings.scripted import POLICY, FakeClock, ScriptedEmbedder, small_spec
from tests.ingestion.pdf_builder import build_pdf
from tests.kb.fakes import InMemoryKbStore
from vectapilot_api.business import UNKNOWN_BUSINESS
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.embeddings.runner import EMBEDDING_CALL, EmbeddingRunner
from vectapilot_api.gateway.errors import AuthenticationFailed, Transient
from vectapilot_api.ingestion.types import ChunkingConfig, IngestionError, LoadLimits
from vectapilot_api.kb.router import as_kb_error, get_kb_service
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.main import create_app

A: Final = "clinic-a"
B: Final = "vet-b"
CANARY: Final = "canary-text-91c2"
"""Planted in documents and query values; must never appear in an error body or a log line."""
MARKDOWN: Final = (
    f"# Opening hours\n\nWe are open 9 to 5. {CANARY}\n\n## Fees\n\nA visit costs 500.\n"
).encode()
PARAMS: Final = {
    "source_id": "faq-main",
    "version": "2026-10-21",
    "language": "en",
    "content_kind": "faq",
    "status": "approved",
    "audience": "public",
}
MIGRATION_0003: Final = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "0003_document_content_kind.py"
)


class Kb:
    """The app under test, its store and its embedder."""

    def __init__(
        self,
        client: TestClient,
        store: InMemoryKbStore,
        embedder: ScriptedEmbedder,
        clock: FakeClock,
    ) -> None:
        self.client = client
        self.store = store
        self.embedder = embedder
        self.clock = clock

    def post(
        self,
        body: Any = MARKDOWN,
        *,
        params: dict[str, str] | None = None,
        business: str | None = A,
        content_type: str | None = "text/markdown",
    ) -> httpx2.Response:
        headers = {} if business is None else {"X-Business-Id": business}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return self.client.post(
            "/v1/kb/documents", content=body, params=PARAMS | (params or {}), headers=headers
        )

    def reindex(self, business: str | None = A) -> httpx2.Response:
        headers = {} if business is None else {"X-Business-Id": business}
        return self.client.post("/v1/kb/reindex", headers=headers)


def build_kb(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    embedder: ScriptedEmbedder | None = None,
    limits: LoadLimits | None = None,
    registered: bool = True,
) -> tuple[FastAPI, InMemoryKbStore, ScriptedEmbedder, FakeClock]:
    async def business_exists(self: Database, business_id: str) -> bool:
        return registered

    monkeypatch.setattr(Database, "business_exists", business_exists)
    embedder = embedder or ScriptedEmbedder()
    store, clock = InMemoryKbStore(), FakeClock()
    runner = EmbeddingRunner(
        embedder,
        embedder._fake._spec,
        POLICY,
        deadline_s=100.0,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )
    service = KbService(
        store,
        runner,
        limits=limits or LoadLimits(),
        chunking=ChunkingConfig(max_chars=200, overlap_chars=20),
    )
    app = create_app(settings)
    app.dependency_overrides[get_kb_service] = lambda: service
    return app, store, embedder, clock


@pytest.fixture
def make_kb(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    clients: list[TestClient] = []

    def make(**kwargs: Any) -> Kb:
        app, store, embedder, clock = build_kb(settings, monkeypatch, **kwargs)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Kb(client, store, embedder, clock)

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def kb(make_kb: Any) -> Kb:
    result: Kb = make_kb()
    return result


def assert_error(response: httpx2.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"] == code
    assert CANARY not in response.text


# ── Ingest: stored once, per business ────────────────────────────────────────────────────────


def test_ingest_stores_the_document_and_its_chunks_for_the_business(kb: Kb) -> None:
    response = kb.post()

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is True and body["embedding_model"] == kb.embedder.model
    [(key, (document_id, document))] = kb.store.documents.items()
    assert key == (A, "faq-main", "2026-10-21") and str(document_id) == body["document_id"]
    assert (document.title, document.source_type, document.content_kind) == (
        "Opening hours",
        "markdown",
        ContentKind.FAQ,
    )
    assert (document.status, document.audience) == ("approved", "public")
    rows = kb.store.chunks_of(A)
    assert len(rows) == body["chunks_written"] == 2
    assert {row.model for row in rows} == {kb.embedder.model}
    assert [row.chunk.metadata["heading_path"] for row in rows] == [
        ["Opening hours"],
        ["Opening hours", "Fees"],
    ]


def test_reingesting_the_same_version_is_idempotent(kb: Kb) -> None:
    first = kb.post()
    second = kb.post()

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["document_id"] == first.json()["document_id"]
    assert second.json()["created"] is False and second.json()["chunks_written"] == 0
    assert len(kb.store.documents) == 1 and len(kb.store.chunks) == 2
    assert len(kb.embedder.batches) == 1  # nothing embedded again: no cost


def test_a_concurrent_upload_of_the_same_version_is_not_duplicated(kb: Kb) -> None:
    kb.store.hide_from_find = True  # the version is stored between the check and the write
    first = kb.post()
    second = kb.post()

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["document_id"] == first.json()["document_id"]
    assert len(kb.store.chunks) == 2


@pytest.mark.parametrize("race", [False, True], ids=["checked first", "found on write"])
def test_other_bytes_under_a_stored_version_are_a_conflict(kb: Kb, race: bool) -> None:
    kb.post()
    kb.store.hide_from_find = race
    response = kb.post(MARKDOWN + b"\nOne more line.\n")

    assert_error(response, 409, "version_conflict")
    assert len(kb.store.chunks) == 2


def test_the_same_source_for_another_business_is_its_own_document(kb: Kb) -> None:
    mine, theirs = kb.post(business=A), kb.post(business=B)

    assert (mine.status_code, theirs.status_code) == (201, 201)
    assert mine.json()["document_id"] != theirs.json()["document_id"]
    assert len(kb.store.chunks_of(A)) == len(kb.store.chunks_of(B)) == 2


@pytest.mark.parametrize(
    ("content_type", "body", "source_type"),
    [
        ("text/markdown; charset=utf-8", MARKDOWN, "markdown"),
        ("text/html", b"<h1>Hours</h1><p>Open 9 to 5.</p>", "html"),
        ("TEXT/CSV", b"service,price\nvisit,500\n", "csv"),
        ("application/pdf", build_pdf([["Open 9 to 5."]]), "pdf"),
    ],
)
def test_each_format_is_read_by_its_loader(
    kb: Kb, content_type: str, body: bytes, source_type: str
) -> None:
    response = kb.post(body, content_type=content_type)

    assert response.status_code == 201, response.text
    [(_, document)] = kb.store.documents.values()
    assert document.source_type == source_type
    if source_type == "pdf":
        assert document.metadata == {"page_count": 1}
        assert [row.chunk.page for row in kb.store.chunks_of(A)] == [1]


@pytest.mark.parametrize("content_type", [None, "application/json", "text/plain"])
def test_an_unsupported_or_missing_format_is_refused(kb: Kb, content_type: str | None) -> None:
    assert_error(kb.post(content_type=content_type), 415, "unsupported_media_type")
    assert kb.store.documents == {} and kb.embedder.batches == []


# ── Limits and errors: typed, echoing nothing ────────────────────────────────────────────────


def test_a_body_over_the_size_limit_is_refused_before_loading(make_kb: Any) -> None:
    kb: Kb = make_kb(limits=LoadLimits(max_bytes=len(MARKDOWN)))

    def chunked() -> Iterator[bytes]:  # no Content-Length: the limit applies while streaming
        yield MARKDOWN
        yield b"x"

    assert_error(kb.post(MARKDOWN + b"x"), 413, "file_too_large")
    assert_error(kb.post(chunked()), 413, "file_too_large")
    assert kb.store.documents == {} and kb.embedder.batches == []
    assert kb.post(MARKDOWN).status_code == 201  # exactly at the limit


@pytest.mark.parametrize(
    "bad",
    [
        {"language": f"EN-{CANARY}!"},
        {"content_kind": CANARY},
        {"status": CANARY},
        {"review_by": CANARY},
        {"source_id": ""},
        {"unexpected": CANARY},
    ],
)
def test_invalid_metadata_gets_a_422_that_echoes_nothing(kb: Kb, bad: dict[str, str]) -> None:
    response = kb.post(params=bad)

    assert_error(response, 422, "invalid_request")
    problems = response.json()["problems"]
    assert problems and all(set(problem) == {"loc", "type"} for problem in problems)
    assert kb.store.documents == {}


def test_missing_metadata_gets_a_422(kb: Kb) -> None:
    response = kb.client.post(
        "/v1/kb/documents",
        content=MARKDOWN,
        params={"source_id": "faq-main"},
        headers={"X-Business-Id": A, "Content-Type": "text/markdown"},
    )

    assert_error(response, 422, "invalid_request")
    missing = {tuple(p["loc"]) for p in response.json()["problems"] if p["type"] == "missing"}
    assert ("query", "status") in missing and ("query", "content_kind") in missing


@pytest.mark.parametrize("route", ["documents", "reindex"])
@pytest.mark.parametrize("registered", [True, False], ids=["no header", "unregistered"])
def test_no_business_is_refused_before_anything_else(
    make_kb: Any, route: str, registered: bool
) -> None:
    kb: Kb = make_kb(registered=registered)
    business = None if registered else "nobody-here"
    if route == "documents":  # invalid metadata and no format: the business is checked first
        response = kb.post(params={"language": "??"}, content_type=None, business=business)
    else:
        response = kb.reindex(business=business)

    assert response.status_code == 400 and response.json() == {"detail": UNKNOWN_BUSINESS}
    assert kb.store.documents == {} and kb.embedder.batches == []


@pytest.mark.parametrize(
    ("body", "content_type", "limits", "status", "code"),
    [
        (b"  \n ", "text/markdown", None, 422, "empty_document"),
        (b"\xff\xfe" + CANARY.encode(), "text/csv", None, 422, "not_utf8"),
        (b"%PDF-1.4 " + CANARY.encode(), "application/pdf", None, 422, "malformed_document"),
        (MARKDOWN, "text/markdown", LoadLimits(max_chars=20), 413, "text_too_long"),
        (
            build_pdf([["one"], ["two"], [CANARY]]),
            "application/pdf",
            LoadLimits(max_pages=2),
            413,
            "too_many_pages",
        ),
    ],
)
def test_document_problems_are_typed_errors_without_document_text(
    make_kb: Any,
    body: bytes,
    content_type: str,
    limits: LoadLimits | None,
    status: int,
    code: str,
) -> None:
    kb: Kb = make_kb(limits=limits)

    assert_error(kb.post(body, content_type=content_type), status, code)
    assert kb.store.documents == {} and kb.embedder.batches == []


def test_the_base_ingestion_error_has_its_own_code() -> None:
    error = as_kb_error(IngestionError())

    assert (error.status_code, error.code) == (422, "unreadable_document")


@pytest.mark.parametrize(
    ("embedder", "status", "code"),
    [
        (ScriptedEmbedder(script=[Transient()] * 3), 503, "embedding_unavailable"),
        (ScriptedEmbedder(script=[AuthenticationFailed(status_code=401)]), 502, "embedding_failed"),
        (
            ScriptedEmbedder(small_spec(context_tokens=20, max_tokens_per_request=20)),
            422,
            "chunk_too_long",
        ),
    ],
    ids=["retries used up", "not retryable", "a chunk over the model's context"],
)
def test_embedding_failures_are_typed_errors_and_store_nothing(
    make_kb: Any, embedder: ScriptedEmbedder, status: int, code: str
) -> None:
    kb: Kb = make_kb(embedder=embedder)

    assert_error(kb.post(), status, code)
    assert kb.store.documents == {} and kb.store.chunks == {}


@pytest.mark.parametrize("route", ["documents", "reindex"])
@pytest.mark.parametrize(
    "outage", [OSError(), OperationalError("SELECT 1", {}, Exception())], ids=["os", "sqlalchemy"]
)
def test_a_database_outage_is_a_typed_503(kb: Kb, route: str, outage: Exception) -> None:
    kb.store.fail = outage
    response = kb.post() if route == "documents" else kb.reindex()

    assert_error(response, 503, "database_unavailable")


def test_every_embedding_call_leaves_one_metadata_only_record(
    kb: Kb, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    kb.post()

    found = [getattr(r, EMBEDDING_CALL) for r in caplog.records if r.getMessage() == EMBEDDING_CALL]
    assert [(r["business_id"], r["operation"], r["inputs"]) for r in found] == [(A, "kb-ingest", 2)]
    assert CANARY not in caplog.text


# ── Reindex ──────────────────────────────────────────────────────────────────────────────────


def test_reindex_re_embeds_only_this_businesss_chunks_of_another_model(
    kb: Kb, caplog: pytest.LogCaptureFixture
) -> None:
    kb.post(business=A)
    kb.post(business=B)
    for row in kb.store.chunks.values():  # as if both were embedded before a model change
        row.model, row.embedding = "old-model", (0.0,)
    caplog.set_level(logging.INFO)
    caplog.clear()

    response = kb.reindex(business=A)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "embedding_model": kb.embedder.model,
        "dimension": 1024,
        "chunks_reindexed": 2,
    }
    assert {row.model for row in kb.store.chunks_of(A)} == {kb.embedder.model}
    assert all(len(row.embedding) == 1024 for row in kb.store.chunks_of(A))
    assert {row.model for row in kb.store.chunks_of(B)} == {"old-model"}
    operations = [
        getattr(r, EMBEDDING_CALL)["operation"]
        for r in caplog.records
        if r.getMessage() == EMBEDDING_CALL
    ]
    assert operations == ["kb-reindex"]
    assert kb.reindex(business=A).json()["chunks_reindexed"] == 0  # done: nothing left


def test_reindex_with_nothing_stale_embeds_nothing(kb: Kb) -> None:
    kb.post()
    calls = len(kb.embedder.batches)

    assert kb.reindex().json()["chunks_reindexed"] == 0
    assert len(kb.embedder.batches) == calls


def test_a_failed_reindex_changes_no_chunk(make_kb: Any) -> None:
    kb: Kb = make_kb(embedder=ScriptedEmbedder(script=[None, AuthenticationFailed()]))
    kb.post()
    for row in kb.store.chunks.values():
        row.model = "old-model"

    assert_error(kb.reindex(), 502, "embedding_failed")
    assert {row.model for row in kb.store.chunks.values()} == {"old-model"}


# ── Wiring ───────────────────────────────────────────────────────────────────────────────────


def test_content_kinds_match_migration_0003() -> None:
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION_0003)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert tuple(kind.value for kind in ContentKind) == module.CONTENT_KINDS


def test_the_app_builds_the_service_and_closes_the_embedder(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder = ScriptedEmbedder()
    monkeypatch.setattr("vectapilot_api.main.build_embedder", lambda settings: embedder)
    app = create_app(settings)
    with TestClient(app) as client:
        assert isinstance(app.state.kb_service, KbService)
        paths = client.get("/openapi.json").json()["paths"]
        assert {"/v1/kb/documents", "/v1/kb/reindex"} <= set(paths)
        assert not embedder.closed

    assert embedder.closed
