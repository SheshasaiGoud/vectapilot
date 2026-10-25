"""M3 PR 4 against real Postgres: the hybrid search's one snapshot (D8, zara S8), and the hybrid
routes over the real legs. Integration: CI runs these; locally with RUN_INTEGRATION=1.

**The snapshot.** A hybrid search runs two statements, one per leg. Its business transaction
therefore starts with `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY` (`db.SNAPSHOT`),
then `set_config` as always: every statement reads the data as of the first query, and nothing in
it may write. Each test also runs without the snapshot (READ COMMITTED, M2's level), where the
same steps must show the problem the snapshot exists for: the red proof and the positive control.

**No working H1 or H2 here.** The approval test runs `SearchService`'s hybrid path with M2 H1's
real `vector_search` as the vector leg; the second leg is a team-side stand-in that is not a
full-text search (it runs `vector_search` again, so it reads the database at the moment the
second leg runs), and the fusion is `standins.StandInFuse`, which is not RRF. The last test runs
the real `text_search` and `rrf_fuse` and is a strict xfail until both are written
(`pending_hand_writes`, the same switches as the canary suite).
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.answer.providers import CitingProvider
from tests.canary.read_paths import pending_hand_writes
from tests.conftest import integration
from tests.embeddings.scripted import FAKE_SPEC, POLICY, FakeClock, ScriptedEmbedder
from tests.retrieval.corpus import BUSINESS_SETTING, CHUNKS_OF, Row, plant
from tests.retrieval.standins import HYBRID, StandInFuse
from tests.retrieval.vectors import toward
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import SNAPSHOT, Database, business_transaction
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.kb.types import ContentKind, NewChunk, NewDocument
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.search import SearchService, new_trace_id
from vectapilot_api.retrieval.types import SearchHit, TextQuery, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

pytestmark = [integration, pytest.mark.anyio]

MODEL: Final = FAKE_SPEC.model
"""The fake embedder's model id: the question's, so the model check passes."""
QUESTION: Final = "When do you open on Sundays?"
UNRELATED: Final = "A visit costs 500 rupees, paid at the desk."
SET_BUSINESS_PREFIX: Final = "SELECT set_config('app.business_id', "
READ_ONLY_TRANSACTION: Final = "25006"
"""SQLSTATE `read_only_sql_transaction`."""

SNAPSHOTS: Final = [
    pytest.param(True, id="snapshot (hybrid)"),
    pytest.param(False, id="READ COMMITTED (red proof)"),
]


def statements_of(engine: AsyncEngine) -> list[str]:
    """Every statement `engine` sends from now on, in order (BEGIN is the driver's, not listed)."""
    sent: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        sent.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    return sent


async def test_a_snapshot_transaction_starts_with_set_transaction_then_the_business(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    database = Database(app_engine)
    await database.verify_role()  # its own query, on its own connection, before any transaction
    sent = statements_of(app_engine)

    async with database.business_transaction(a, snapshot=True) as connection:
        level = await connection.scalar(text("SHOW transaction_isolation"))
        read_only = await connection.scalar(text("SHOW transaction_read_only"))
        business = await connection.scalar(BUSINESS_SETTING)
    in_snapshot, start = sent[:], len(sent)
    async with business_transaction(app_engine, a) as connection:
        default_level = await connection.scalar(text("SHOW transaction_isolation"))
        default_read_only = await connection.scalar(text("SHOW transaction_read_only"))
    in_default = sent[start:]

    # SNAPSHOT is the transaction's first statement, then set_config, then the reads.
    assert in_snapshot[0] == SNAPSHOT and in_snapshot[1].startswith(SET_BUSINESS_PREFIX)
    assert len(in_snapshot) == 5
    assert (level, read_only, business) == ("repeatable read", "on", a)
    # Every other transaction stays at M2's level (READ COMMITTED) and may write.
    assert in_default[0].startswith(SET_BUSINESS_PREFIX) and SNAPSHOT not in in_default
    assert (default_level, default_read_only) == ("read committed", "off")


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
async def test_a_commit_between_two_statements_is_invisible_to_the_second(
    app_engine: AsyncEngine, businesses: tuple[str, str], snapshot: bool
) -> None:
    a, _ = businesses
    await plant(app_engine, a, [Row("hours", 0, toward(1.0))])

    async with business_transaction(app_engine, a, snapshot=snapshot) as reader:
        before = await reader.scalar(CHUNKS_OF, {"b": a})
        # Another transaction (a new connection: NullPool) commits a chunk in between.
        await plant(app_engine, a, [Row("parking", 0, toward(0.5))])
        after = await reader.scalar(CHUNKS_OF, {"b": a})
    async with business_transaction(app_engine, a) as connection:
        committed = await connection.scalar(CHUNKS_OF, {"b": a})

    assert (before, committed) == (1, 2)  # the commit really happened
    # One snapshot: the second statement sees what the first saw. READ COMMITTED: it does not.
    assert after == (1 if snapshot else 2)


@pytest.mark.parametrize(
    "write",
    [
        "UPDATE documents SET title = 'changed' WHERE business_id = :b",
        "DELETE FROM chunks WHERE business_id = :b",
        "INSERT INTO businesses (business_id) VALUES ('snapshot-probe-x')",
    ],
    ids=["update", "delete", "insert"],
)
async def test_a_write_inside_a_snapshot_is_refused(
    app_engine: AsyncEngine, businesses: tuple[str, str], write: str
) -> None:
    a, _ = businesses
    await plant(app_engine, a, [Row("hours", 0, toward(1.0))])

    with pytest.raises(DBAPIError) as raised:
        async with Database(app_engine).business_transaction(a, snapshot=True) as connection:
            await connection.execute(text(write), {"b": a})

    assert getattr(raised.value.orig, "sqlstate", None) == READ_ONLY_TRANSACTION
    async with business_transaction(app_engine, a) as connection:
        assert await connection.scalar(CHUNKS_OF, {"b": a}) == 1
        title = await connection.scalar(
            text("SELECT title FROM documents WHERE business_id = :b"), {"b": a}
        )
    assert title == "Title of hours v1"


# ── An approval that commits between the legs (zara S8) ──────────────────────────────────────


class IgnoresSnapshot:
    """`Database` whose transactions stay READ COMMITTED whatever the search asks: hybrid search
    without D8 (the red proof)."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def business_transaction(
        self, business_id: str, *, snapshot: bool = False
    ) -> AbstractAsyncContextManager[AsyncConnection]:
        return self._database.business_transaction(business_id)


def version_2() -> tuple[NewDocument, list[NewChunk]]:
    document = NewDocument(
        source_id="hours",
        version="2",
        title="Title of hours v2",
        source_type=SourceType.MARKDOWN,
        content_kind=ContentKind.FAQ,
        language="en",
        status="approved",
        audience="public",
        content_sha256="b" * 64,
        review_by=None,
        metadata={},
    )
    chunk = NewChunk(
        chunk_index=0, content="Sundays: 11 to 4.", page=None, metadata={}, embedding=toward(0.75)
    )
    return document, [chunk]


@dataclass
class ApprovingLeg:
    """M2 H1's `vector_search`, then a hook: the given approval commits on its own connection
    before the second leg runs (the race D8 closes)."""

    approve: Callable[[], Any]
    approved: int = 0

    async def __call__(self, connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        hits = await vector_search(connection, query)
        await self.approve()
        self.approved += 1
        return hits


async def a_second_leg_that_reads_the_database_now(
    connection: AsyncConnection, query: TextQuery
) -> list[SearchHit]:
    """Stands in for `text_search` (shesha's H1, not written): no full-text matching at all. It
    runs M2 H1's `vector_search` again with the query's own fields, so it returns what this
    transaction can see when the second leg runs."""
    fields = query.model_dump(exclude={"question"})
    return await vector_search(connection, VectorQuery.model_validate(fields))


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
async def test_an_approval_between_the_legs_never_puts_two_versions_of_a_source_in_one_list(
    app_engine: AsyncEngine, businesses: tuple[str, str], snapshot: bool
) -> None:
    a, b = businesses
    await plant(app_engine, a, [Row("hours", 0, toward(0.75), model=MODEL)])
    await plant(app_engine, b, [Row("hours", 0, toward(0.75), model=MODEL)])  # never a's
    database = Database(app_engine)
    store = PostgresKbStore(database)
    retired: list[str | None] = []

    async def approve() -> None:
        document, chunks = version_2()
        stored = await store.save_document(a, document, chunks, model=MODEL, dimension=1024)
        retired.append(stored.retired_version)

    clock = FakeClock()
    embedder = ScriptedEmbedder()
    runner = EmbeddingRunner(
        embedder,
        FAKE_SPEC,
        POLICY,
        deadline_s=100.0,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )
    leg = ApprovingLeg(approve)
    service = SearchService(
        database if snapshot else IgnoresSnapshot(database),
        runner,
        search=leg,
        text=a_second_leg_that_reads_the_database_now,
        fuse=StandInFuse(),
        retrieval=HYBRID,
    )

    result = await service.search(a, QUESTION, k=5, reader="customer", trace_id=new_trace_id())

    # The positive control: the approval committed between the legs and retired version 1.
    assert (leg.approved, retired) == (1, ["1"])
    versions = sorted({(hit.source_id, hit.version) for hit in result.hits})
    if snapshot:
        # Both legs read the snapshot from before the approval: version 1 only, found by both.
        assert versions == [("hours", "1")]
    else:
        # READ COMMITTED: the second leg sees version 2 approved and version 1 retired, so the
        # fused list holds both versions of one source: what the snapshot prevents.
        assert versions == [("hours", "1"), ("hours", "2")]
    assert {hit.title for hit in result.hits} <= {"Title of hours v1", "Title of hours v2"}


# ── The hybrid routes over the real legs (wait for H1 and H2) ────────────────────────────────


def waits_for_h1_and_h2[Test: Callable[..., Awaitable[None]]](test: Test) -> Test:
    """The canary's switches for M3 H1 and H2: a strict xfail on NotImplementedError while either
    is pending, nothing once both are written (then the test must simply pass)."""
    for mark in pending_hand_writes("rrf_fuse", "text_search"):
        test = mark(test)
    return test


@waits_for_h1_and_h2
async def test_the_hybrid_routes_over_the_real_legs_find_only_the_businesss_chunks(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, b = businesses
    question_vector, unrelated_vector = fake_vector(QUESTION, 1024), fake_vector(UNRELATED, 1024)
    mine = await plant(
        app_engine,
        a,
        [
            Row("hours", 0, question_vector, model=MODEL, body=QUESTION),
            Row("fees", 0, unrelated_vector, model=MODEL, body=UNRELATED),
            Row(
                "hours-staff", 0, question_vector, model=MODEL, body=QUESTION, audience="staff_only"
            ),
        ],
    )
    theirs = await plant(
        app_engine, b, [Row("hours", 0, question_vector, model=MODEL, body=QUESTION)]
    )
    provider = CitingProvider()
    with TestClient(
        create_app(Settings(rag_retrieval_mode="hybrid"), llm_provider=provider)
    ) as api:
        found = api.get("/v1/kb/search", params={"q": QUESTION}, headers={BUSINESS_HEADER: a})
        answered = api.post("/v1/answer", json={"question": QUESTION}, headers={BUSINESS_HEADER: a})

    assert found.status_code == 200, found.text
    hits = found.json()["hits"]
    # Both legs put a's "hours" chunk first (cosine 1, both question lexemes, `open` and
    # `sunday`): first in the fused list. "fees" shares no lexeme with the question, so only the
    # vector leg has it. The staff-only chunk and b's never come back.
    assert [h["chunk_id"] for h in hits] == [
        str(mine.chunks[("hours", "1", 0)]),
        str(mine.chunks[("fees", "1", 0)]),
    ]
    assert hits[0]["score"] == pytest.approx(1.0, abs=1e-5)  # the cosine, float32 storage
    assert answered.status_code == 200, answered.text
    assert answered.json()["outcome"] == "answered"
    assert [s["chunk_id"] for s in answered.json()["sources"]] == [hits[0]["chunk_id"]]
    for other in (theirs.chunks[("hours", "1", 0)], mine.chunks[("hours-staff", "1", 0)]):
        assert str(other) not in found.text and str(other) not in answered.text
