"""`GET /v1/kb/search` and `POST /v1/answer` end to end against real Postgres (integration, CI).

The real app as the app role, the fake embedder, and a scripted model (`CitingProvider`). Rows are
planted as the app role (under RLS) for two fictional businesses registered by the owner. Every
planting keeps at most one approved version per business and source id (migration 0004).

The chunks carry the fake embedder's own vectors (`fake_vector` of their text, model id
`fake-embedding-1024`), so a question that repeats a chunk's text scores 1 against it and about 0
against any other text.

**Waiting for H1.** Every test that reaches `vector_search` carries `H1_PENDING`, the strict xfail
of `tests/canary/read_paths.pending_hand_write("vector_search")`: one switch with H1's canary
path. When shesha removes "vector_search" from `PENDING_HAND_WRITE` (H1's step 3), these turn into
plain tests at once. The `reindex_required` tests stop before the search, so they run now.
"""

import secrets
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.answer.providers import CitingProvider
from tests.canary.read_paths import pending_hand_write
from tests.conftest import integration
from tests.embeddings.scripted import FAKE_SPEC
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.types import MAX_K

pytestmark = [integration, pytest.mark.anyio]

H1_PENDING: Final = pending_hand_write("vector_search")

QUESTION: Final = "When do you open on Sundays?"
"""Planted as the text of the chunks that must be found: their score is 1."""
UNRELATED: Final = "A visit costs 500 rupees, paid at the desk."
OTHER_MODEL: Final = "old-embedding-model"

REGISTER: Final = text("INSERT INTO businesses (business_id) VALUES (:b)")
UPROOT: Final = (
    text("DELETE FROM documents WHERE business_id = :b"),  # chunks go with their document
    text("DELETE FROM businesses WHERE business_id = :b"),
)
INSERT_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256)"
    " VALUES (:b, :source_id, '1', :title, 'markdown', 'faq', 'en', :status, :audience,"
    " repeat('a', 64))"
    " RETURNING id"
)
INSERT_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
    " embedding_model, embedding_dim)"
    " VALUES (:b, :document_id, 0, :content, CAST(:embedding AS vector), :model, 1024)"
    " RETURNING id"
)


def h1_pending(test: Any) -> Any:
    """Apply `H1_PENDING`'s marks (none once H1 is written)."""
    for mark in H1_PENDING:
        test = mark(test)
    return test


@dataclass(frozen=True)
class Row:
    """One document with one chunk whose vector is the fake embedder's for `content`."""

    source_id: str
    content: str
    status: str = "approved"
    audience: str = "public"
    model: str = FAKE_SPEC.model


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        str(MigrationSettings().migration_database_url.get_secret_value()), poolclass=NullPool
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        str(Settings().database_url.get_secret_value()), poolclass=NullPool
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def businesses(owner_engine: AsyncEngine) -> AsyncIterator[tuple[str, str]]:
    ids = (f"pr6-a-{secrets.token_hex(4)}", f"pr6-b-{secrets.token_hex(4)}")
    for business in ids:
        async with business_transaction(owner_engine, business) as connection:
            await connection.execute(REGISTER, {"b": business})
    try:
        yield ids
    finally:
        for business in ids:
            async with business_transaction(owner_engine, business) as connection:
                for statement in UPROOT:
                    await connection.execute(statement, {"b": business})


async def plant(engine: AsyncEngine, business: str, *rows: Row) -> dict[str, str]:
    """Plant `rows` for `business` as the app role; {source id: chunk id}."""
    chunk_ids: dict[str, str] = {}
    async with business_transaction(engine, business) as connection:
        for row in rows:
            document_id = (
                await connection.execute(
                    INSERT_DOCUMENT,
                    {
                        "b": business,
                        "source_id": row.source_id,
                        "title": f"Title of {row.source_id}",
                        "status": row.status,
                        "audience": row.audience,
                    },
                )
            ).scalar_one()
            chunk_id = (
                await connection.execute(
                    INSERT_CHUNK,
                    {
                        "b": business,
                        "document_id": document_id,
                        "content": row.content,
                        "embedding": vector_literal(fake_vector(row.content, 1024)),
                        "model": row.model,
                    },
                )
            ).scalar_one()
            chunk_ids[row.source_id] = str(chunk_id)
    return chunk_ids


@pytest.fixture
def provider() -> CitingProvider:
    return CitingProvider()


@pytest.fixture
def api(provider: CitingProvider) -> Iterator[TestClient]:
    with TestClient(create_app(Settings(), llm_provider=provider)) as client:
        yield client


def as_business(business: str) -> dict[str, str]:
    return {BUSINESS_HEADER: business}


# ── GET /v1/kb/search ─────────────────────────────────────────────────────────────────────────


@h1_pending
async def test_search_returns_only_the_businesss_approved_public_chunks_best_first(
    api: TestClient, app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, b = businesses
    mine = await plant(
        app_engine,
        a,
        Row("hours", QUESTION),
        Row("fees", UNRELATED),
        Row("hours-draft", QUESTION, status="draft"),
        Row("hours-old", QUESTION, status="retired"),
        Row("hours-staff", QUESTION, audience="staff_only"),
    )
    theirs = await plant(app_engine, b, Row("hours", QUESTION))

    response = api.get("/v1/kb/search", params={"q": QUESTION, "k": MAX_K}, headers=as_business(a))

    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    assert [h["chunk_id"] for h in hits] == [mine["hours"], mine["fees"]]
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-5)  # float32 storage
    assert hits[0]["content"] == QUESTION and hits[0]["source_id"] == "hours"
    assert theirs["hours"] not in response.text


async def test_search_refuses_a_business_with_chunks_of_two_models(
    api: TestClient, app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    await plant(app_engine, a, Row("hours", QUESTION), Row("fees", UNRELATED, model=OTHER_MODEL))

    response = api.get("/v1/kb/search", params={"q": QUESTION}, headers=as_business(a))

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "reindex_required"
    assert OTHER_MODEL not in response.text and QUESTION not in response.text


# ── POST /v1/answer ───────────────────────────────────────────────────────────────────────────


@h1_pending
async def test_an_answer_cites_only_chunks_retrieved_for_the_business(
    api: TestClient,
    provider: CitingProvider,
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
) -> None:
    a, b = businesses
    mine = await plant(app_engine, a, Row("hours", QUESTION), Row("fees", UNRELATED))
    theirs = await plant(app_engine, b, Row("hours", QUESTION))

    response = api.post("/v1/answer", json={"question": QUESTION}, headers=as_business(a))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "answered" and body["route"] == "rag"
    assert [s["chunk_id"] for s in body["sources"]] == [mine["hours"]]  # fees: below 0.3
    assert body["confidence"]["value"] == pytest.approx(1.0, abs=1e-5)
    assert body["confidence"]["calibrated"] is False
    [call] = provider.calls
    assert theirs["hours"] not in call.messages[-1].content
    assert mine["fees"] not in call.messages[-1].content


@h1_pending
async def test_an_answer_without_evidence_is_dont_know_and_calls_no_model(
    api: TestClient,
    provider: CitingProvider,
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
) -> None:
    a, b = businesses
    unrelated = sum(
        x * y
        for x, y in zip(fake_vector(QUESTION, 1024), fake_vector(UNRELATED, 1024), strict=True)
    )
    assert unrelated < Settings().rag_min_score, (
        "premise: the unrelated text scores below the threshold"
    )
    await plant(app_engine, a, Row("fees", UNRELATED))
    await plant(app_engine, b, Row("hours", QUESTION))  # the other business would answer it

    response = api.post("/v1/answer", json={"question": QUESTION}, headers=as_business(a))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["outcome"], body["answer"], body["sources"]) == ("dont_know", None, [])
    assert provider.calls == []


async def test_an_answer_for_a_business_with_chunks_of_two_models_is_reindex_required(
    api: TestClient,
    provider: CitingProvider,
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
) -> None:
    a, _ = businesses
    await plant(app_engine, a, Row("hours", QUESTION), Row("fees", UNRELATED, model=OTHER_MODEL))

    response = api.post("/v1/answer", json={"question": QUESTION}, headers=as_business(a))

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "reindex_required"
    assert provider.calls == []
