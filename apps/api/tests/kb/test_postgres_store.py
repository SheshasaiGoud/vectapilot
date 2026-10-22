"""The knowledge-base routes and `PostgresKbStore` against real Postgres (integration, CI).

The owner (VECTAPILOT_MIGRATION_DATABASE_URL) registers and removes the test businesses; it also
watches for a blocked backend in the concurrent-approval test and, in one test, runs the store
where RLS cannot help (to prove the store's own `business_id` filter). Everything else checked runs
as the app role, under RLS. Cross-business checks live in the canary suite
(`tests/canary/write_paths.py`).

One approved version per source (PR 5b, ADR 0010): every committed row here keeps at most one
approved version per business and source id, so all of it is valid for H1's tests too.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import integration
from tests.embeddings.scripted import FAKE_SPEC
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.kb.store import PostgresKbStore, is_one_approved_violation
from vectapilot_api.kb.types import (
    ApprovalConflict,
    ContentKind,
    DocumentStatus,
    NewChunk,
    NewDocument,
    StoredDocument,
)
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


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


@asynccontextmanager
async def registered_business() -> AsyncIterator[str]:
    """A fresh fictional business, removed with all its rows afterwards."""
    owner = create_async_engine(owner_url())
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
async def business() -> AsyncIterator[str]:
    async with registered_business() as business_id:
        yield business_id


@pytest.fixture
async def other_business() -> AsyncIterator[str]:
    async with registered_business() as business_id:
        yield business_id


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def api() -> Iterator[TestClient]:
    with TestClient(create_app(Settings())) as client:
        yield client


def ingest(api: TestClient, business: str, body: bytes = BODY, **params: str) -> dict[str, object]:
    response = api.post(
        "/v1/kb/documents",
        content=body,
        params=PARAMS | params,
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


# ── One approved version per source (PR 5b, ADR 0010) ───────────────────────────────────────

SOURCE: Final = "prices"
MODEL: Final = "m"
CHUNK: Final = NewChunk(
    chunk_index=0, content="text", page=None, metadata={}, embedding=(0.0,) * 1023 + (1.0,)
)
STATUSES: Final = text(
    "SELECT version, status FROM documents WHERE business_id = :b AND source_id = :s"
)
INSERT_VERSION: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type, content_kind,"
    " language, status, audience, content_sha256)"
    " VALUES (:b, :s, :version, 'A version', 'markdown', 'pricing', 'en', :status, 'public',"
    " repeat('a', 64))"
)
SET_STATUS: Final = text(
    "UPDATE documents SET status = :status"
    " WHERE business_id = :b AND source_id = :s AND version = :version"
)
BLOCKED_BACKENDS: Final = text(
    "SELECT count(*) FROM pg_catalog.pg_stat_activity"
    " WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0"
)
OWNER_IS_SUPERUSER: Final = text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")


def document(version: str, status: DocumentStatus, source: str = SOURCE) -> NewDocument:
    return NewDocument(
        source_id=source,
        version=version,
        title=f"Version {version}",
        source_type=SourceType.MARKDOWN,
        content_kind=ContentKind.PRICING,
        language="en",
        status=status,
        audience="public",
        content_sha256="a" * 64,
        review_by=None,
        metadata={},
    )


async def statuses(engine: AsyncEngine, business: str, source: str = SOURCE) -> dict[str, str]:
    """{version: status} of one source, read as that business."""
    async with business_transaction(engine, business) as connection:
        rows = (await connection.execute(STATUSES, {"b": business, "s": source})).all()
    return {row.version: row.status for row in rows}


async def test_approving_a_new_version_leaves_exactly_one_approved_version_the_new_one(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    first = ingest(api, business, source_id=SOURCE, version="1")
    second = ingest(api, business, BODY + b"\nNew prices.\n", source_id=SOURCE, version="2")
    draft = ingest(
        api, business, BODY + b"\nNext.\n", source_id=SOURCE, version="3", status="draft"
    )

    assert (first["status"], second["status"], draft["status"]) == (201, 201, 201)
    assert (first["retired_version"], second["retired_version"]) == (None, "1")
    assert draft["retired_version"] is None
    assert await statuses(app_engine, business) == {"1": "retired", "2": "approved", "3": "draft"}


async def test_approving_retires_only_the_approved_version_of_that_source(
    business: str, app_engine: AsyncEngine
) -> None:
    # Two other versions of the source (one retired, one a pending draft) and a second approved
    # source: the retire's `source_id` and `status` predicates each have something to spare.
    seed = ((SOURCE, "1", "retired"), (SOURCE, "2", "approved"), (SOURCE, "3", "draft"))
    async with business_transaction(app_engine, business) as connection:
        for source, version, status in (*seed, ("hours", "1", "approved")):
            values = {"b": business, "s": source, "version": version, "status": status}
            await connection.execute(INSERT_VERSION, values)
    store = PostgresKbStore(Database(app_engine))

    stored = await store.save_document(
        business, document("4", "approved"), [CHUNK], model=MODEL, dimension=1024
    )

    assert stored.retired_version == "2"
    assert await statuses(app_engine, business) == {
        "1": "retired",
        "2": "retired",
        "3": "draft",
        "4": "approved",
    }
    assert await statuses(app_engine, business, "hours") == {"1": "approved"}


async def test_a_write_that_fails_after_the_retire_leaves_the_old_version_approved(
    business: str, app_engine: AsyncEngine
) -> None:
    store = PostgresKbStore(Database(app_engine))
    await store.save_document(
        business, document("1", "approved"), [CHUNK], model=MODEL, dimension=1024
    )

    # Dimension 7 against a 1024-dim vector: the chunks' CHECK refuses the chunk insert, the last
    # statement of the transaction.
    with pytest.raises(IntegrityError) as failed:
        await store.save_document(
            business, document("2", "approved"), [CHUNK], model=MODEL, dimension=7
        )

    # A CHECK, not the approval index: version 2's row was inserted, which needs version 1 out of
    # the index first. So the retire ran in that transaction and was rolled back with it.
    assert getattr(failed.value.orig, "sqlstate", None) == "23514"
    assert not is_one_approved_violation(failed.value)
    assert await statuses(app_engine, business) == {"1": "approved"}
    assert await counts(app_engine, business) == (1, 1)


async def test_a_version_stored_meanwhile_rolls_the_retire_back(
    business: str, app_engine: AsyncEngine
) -> None:
    store = PostgresKbStore(Database(app_engine))
    await store.save_document(
        business, document("1", "approved"), [CHUNK], model=MODEL, dimension=1024
    )
    await store.save_document(
        business, document("2", "draft"), [CHUNK], model=MODEL, dimension=1024
    )

    # Called directly, as when the service's check missed a version stored a moment later.
    stored = await store.save_document(
        business, document("2", "approved"), [CHUNK], model=MODEL, dimension=1024
    )

    assert (stored.created, stored.status, stored.retired_version) == (False, "draft", None)
    assert await statuses(app_engine, business) == {"1": "approved", "2": "draft"}


async def wait_until_blocked(owner: AsyncEngine, task: asyncio.Task[Any]) -> None:
    """Until some backend waits for a lock (the task's UPDATE on the row the test holds)."""
    async with asyncio.timeout(10):
        while True:
            if task.done():
                task.result()  # re-raises what it raised
                pytest.fail("the save finished without waiting for the lock: proves nothing")
            async with owner.connect() as connection:
                if await connection.scalar(BLOCKED_BACKENDS):
                    return
            await asyncio.sleep(0.05)


async def test_a_concurrent_approval_of_another_version_is_an_approval_conflict(
    business: str, app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    store = PostgresKbStore(Database(app_engine))
    await store.save_document(
        business, document("1", "approved"), [CHUNK], model=MODEL, dimension=1024
    )
    params = {"b": business, "s": SOURCE}

    async with business_transaction(app_engine, business) as first:
        # Another request approves version 2, as the store does, and has not committed yet: it
        # holds version 1's row, so the save below waits on its retire.
        await first.execute(SET_STATUS, params | {"version": "1", "status": "retired"})
        await first.execute(INSERT_VERSION, params | {"version": "2", "status": "approved"})
        second: asyncio.Task[StoredDocument] = asyncio.create_task(
            store.save_document(
                business, document("3", "approved"), [CHUNK], model=MODEL, dimension=1024
            )
        )
        try:
            await wait_until_blocked(owner_engine, second)
        except BaseException:
            second.cancel()
            raise
    # The first committed. The second's retire finds version 1 retired and never saw version 2
    # (not committed when that UPDATE began), so its insert meets version 2 in the index.
    with pytest.raises(ApprovalConflict):
        await second

    assert await statuses(app_engine, business) == {"1": "retired", "2": "approved"}


async def test_re_approving_a_retired_version_is_a_status_conflict(
    api: TestClient, business: str, app_engine: AsyncEngine
) -> None:
    ingest(api, business, source_id=SOURCE, version="1")
    ingest(api, business, BODY + b"\nNew prices.\n", source_id=SOURCE, version="2")

    again = ingest(api, business, source_id=SOURCE, version="1")  # version 1's bytes, approved

    assert (again["status"], again["error"]) == (409, "status_conflict")
    assert await statuses(app_engine, business) == {"1": "retired", "2": "approved"}


async def test_the_index_allows_one_approved_version_per_source_and_any_number_of_others(
    business: str, other_business: str, app_engine: AsyncEngine
) -> None:
    rows = (("1", "retired"), ("2", "retired"), ("3", "draft"), ("4", "draft"), ("5", "approved"))
    for owner_of_rows in (business, other_business):  # each business its own approved version
        async with business_transaction(app_engine, owner_of_rows) as connection:
            for version, status in rows:
                values = {"b": owner_of_rows, "s": SOURCE, "version": version, "status": status}
                await connection.execute(INSERT_VERSION, values)

    second_approved = {
        "a new approved row": (INSERT_VERSION, "6"),
        "a draft approved in place": (SET_STATUS, "3"),
    }
    for label, (statement, version) in second_approved.items():
        with pytest.raises(IntegrityError) as refused:
            async with business_transaction(app_engine, business) as connection:
                values = {"b": business, "s": SOURCE, "version": version, "status": "approved"}
                await connection.execute(statement, values)
        assert is_one_approved_violation(refused.value), label

    for owner_of_rows in (business, other_business):
        assert await statuses(app_engine, owner_of_rows) == dict(rows)


class OwnerDatabase:
    """`Database.business_transaction` on the owner's engine, without the role check: a
    superuser skips RLS, so only the store's own `WHERE business_id = ...` keeps the other
    business out."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    def business_transaction(
        self, business_id: str
    ) -> AbstractAsyncContextManager[AsyncConnection]:
        return business_transaction(self.engine, business_id)


async def test_the_retire_names_the_business_even_where_rls_cannot_help(
    business: str, other_business: str, app_engine: AsyncEngine, owner_engine: AsyncEngine
) -> None:
    async with owner_engine.connect() as connection:
        assert await connection.scalar(OWNER_IS_SUPERUSER), "needs an owner that skips RLS"
    store = PostgresKbStore(cast(Database, OwnerDatabase(owner_engine)))
    for owner_of_rows in (business, other_business):  # the same source id in both
        await store.save_document(
            owner_of_rows, document("1", "approved"), [CHUNK], model=MODEL, dimension=1024
        )

    stored = await store.save_document(
        business, document("2", "approved"), [CHUNK], model=MODEL, dimension=1024
    )

    assert stored.retired_version == "1"
    assert await statuses(app_engine, business) == {"1": "retired", "2": "approved"}
    assert await statuses(app_engine, other_business) == {"1": "approved"}
