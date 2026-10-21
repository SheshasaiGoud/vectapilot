"""The knowledge-base routes and `PostgresKbStore` against real Postgres (integration, CI).

The owner (VECTAPILOT_MIGRATION_DATABASE_URL) only registers and removes the test business;
everything checked runs as the app role, under RLS. Cross-business checks live in the canary suite
(`tests/canary/write_paths.py`).
"""

import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import integration
from tests.embeddings.scripted import FAKE_SPEC
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.kb.types import ContentKind, NewChunk, NewDocument
from vectapilot_api.main import create_app

pytestmark = [integration, pytest.mark.anyio]

BODY: Final = b"# Hours\n\nOpen 9 to 5.\n\n## Fees\n\nA visit costs 500.\n"
PARAMS: Final = {
    "source_id": "faq-main",
    "version": "1",
    "language": "en",
    "content_kind": "pricing",
    "status": "approved",
    "audience": "public",
}
COUNTS: Final = text(
    "SELECT (SELECT count(*) FROM documents WHERE business_id = :b) AS documents,"
    " (SELECT count(*) FROM chunks WHERE business_id = :b) AS chunks"
)


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        str(Settings().database_url.get_secret_value()), poolclass=NullPool
    )
    yield engine
    await engine.dispose()


@pytest.fixture
async def business() -> AsyncIterator[str]:
    owner = create_async_engine(str(MigrationSettings().migration_database_url.get_secret_value()))
    business_id = f"kb-test-{secrets.token_hex(4)}"
    params = {"b": business_id}
    async with business_transaction(owner, business_id) as connection:
        await connection.execute(text("INSERT INTO businesses (business_id) VALUES (:b)"), params)
    try:
        yield business_id
    finally:
        async with business_transaction(owner, business_id) as connection:
            await connection.execute(text("DELETE FROM documents WHERE business_id = :b"), params)
            await connection.execute(text("DELETE FROM businesses WHERE business_id = :b"), params)
        await owner.dispose()


@pytest.fixture
def api() -> Iterator[TestClient]:
    with TestClient(create_app(Settings())) as client:
        yield client


def ingest(api: TestClient, business: str, body: bytes = BODY) -> dict[str, object]:
    response = api.post(
        "/v1/kb/documents",
        content=body,
        params=PARAMS,
        headers={"X-Business-Id": business, "Content-Type": "text/markdown"},
    )
    return {"status": response.status_code, **response.json()}


async def counts(engine: AsyncEngine, business: str) -> tuple[int, int]:
    async with business_transaction(engine, business) as connection:
        row = (await connection.execute(COUNTS, {"b": business})).one()
    return int(row.documents), int(row.chunks)


async def test_reingesting_the_same_version_stores_it_once(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    first = ingest(api, business)
    stored = await counts(app_engine, business)
    second = ingest(api, business)

    assert (first["status"], second["status"]) == (201, 200)
    assert second["document_id"] == first["document_id"]
    assert stored == (1, first["chunks_written"]) and stored[1] > 0
    assert await counts(app_engine, business) == stored


async def test_other_bytes_under_a_stored_version_change_nothing(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    ingest(api, business)
    stored = await counts(app_engine, business)

    assert ingest(api, business, BODY + b"\nchanged\n")["status"] == 409
    assert await counts(app_engine, business) == stored


async def test_rows_carry_the_kind_the_model_and_the_citation_metadata(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    ingest(api, business)
    async with business_transaction(app_engine, business) as connection:
        document = (
            await connection.execute(text("SELECT source_type, content_kind FROM documents"))
        ).one()
        chunks = (
            await connection.execute(
                text(
                    "SELECT embedding_model, embedding_dim, metadata FROM chunks"
                    " ORDER BY chunk_index"
                )
            )
        ).all()

    assert tuple(document) == ("markdown", "pricing")
    assert {(c.embedding_model, c.embedding_dim) for c in chunks} == {(FAKE_SPEC.model, 1024)}
    assert chunks[-1].metadata["heading_path"] == ["Hours", "Fees"]


async def test_a_concurrent_write_of_the_same_version_inserts_nothing(
    business: str, app_engine: AsyncEngine
) -> None:
    store = PostgresKbStore(Database(app_engine))
    document = NewDocument(
        source_id="race",
        version="1",
        title="Race",
        source_type=SourceType.MARKDOWN,
        content_kind=ContentKind.FAQ,
        language="en",
        status="draft",
        audience="public",
        content_sha256="a" * 64,
        review_by=None,
        metadata={},
    )
    chunk = NewChunk(
        chunk_index=0, content="text", page=None, metadata={}, embedding=(0.0,) * 1023 + (1.0,)
    )
    first = await store.save_document(business, document, [chunk], model="m", dimension=1024)
    second = await store.save_document(business, document, [chunk], model="m", dimension=1024)

    assert first.created and not second.created
    assert second.document_id == first.document_id
    assert await counts(app_engine, business) == (1, 1)


async def test_reindex_switches_every_chunk_to_the_configured_model(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    written = ingest(api, business)["chunks_written"]
    async with business_transaction(app_engine, business) as connection:  # a model change
        await connection.execute(text("UPDATE chunks SET embedding_model = 'old-model'"))

    response = api.post("/v1/kb/reindex", headers={"X-Business-Id": business})

    assert response.status_code == 200 and response.json()["chunks_reindexed"] == written
    async with business_transaction(app_engine, business) as connection:
        models = await connection.execute(text("SELECT DISTINCT embedding_model FROM chunks"))
        assert set(models.scalars()) == {FAKE_SPEC.model}
    assert (
        api.post("/v1/kb/reindex", headers={"X-Business-Id": business}).json()["chunks_reindexed"]
        == 0
    )


@pytest.mark.parametrize(
    ("column", "value"), [("content_kind, ", "'canary-kind', "), ("", "")], ids=["unknown", "none"]
)
async def test_the_database_refuses_a_document_without_a_known_kind(
    business: str, app_engine: AsyncEngine, column: str, value: str
) -> None:
    statement = text(
        f"INSERT INTO documents (business_id, source_id, version, title, source_type, {column}"  # noqa: S608
        f"language, status, audience, content_sha256) VALUES (:b, 'x', '1', 'x', 'markdown',"
        f" {value}'en', 'draft', 'public', repeat('a', 64))"
    )
    with pytest.raises(IntegrityError):
        async with business_transaction(app_engine, business) as connection:
            await connection.execute(statement, {"b": business})
