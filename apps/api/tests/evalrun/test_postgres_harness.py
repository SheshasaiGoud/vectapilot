"""The eval harness against real Postgres (integration, CI): ingest the synthetic sample corpus
through `KbService` into salted eval businesses, check the stored chunks, read each hit's document
row under RLS, abort a run on a hit from the other namespace (H1 replaced by a stand-in, so this
runs now), delete only the run's own namespaces, and (once H1 and H2 are written) answer, score
and summarise TEST-ONLY questions end to end.

Each test salts its namespaces (`namespace.eval_business_id(..., salt=...)`), so it never touches
a namespace a human dry run made, and removes them afterwards as the owner. The fake embedder and
the dry-run answer model only: nothing is sent anywhere.

**Waiting for H1 and H2.** The end-to-end run carries `pending_hand_writes("vector_search",
"retrieval_metrics")`: a strict xfail while either stub raises `NotImplementedError`, nothing once
both are written (`tests/canary/read_paths.py`, `PENDING_HAND_WRITE`).
"""

import io
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.read_paths import pending_hand_writes
from tests.conftest import integration
from tests.evalrun.helpers import (
    CORPUS_DIR,
    PAWS,
    PAWS_STORE_HOURS,
    SUNRISE,
    SUNRISE_HOURS,
    corpus,
    hit,
    question,
    run_info,
)
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.evalrun.budget import CostCap, record_for
from vectapilot_api.evalrun.harness import RunConfig, RunOutcome, Target, execute, stored_visible
from vectapilot_api.evalrun.ingest import StaleChunks
from vectapilot_api.evalrun.namespace import namespaces
from vectapilot_api.evalrun.report import RESULTS_FILE, RUN_FILE, ConfigInfo, summarize
from vectapilot_api.evalrun.scoring import AnswerLine, DocumentRow, QuestionResult
from vectapilot_api.evalrun.store import PostgresEvalStore, document_rows
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.llm_cli import LogCapture
from vectapilot_api.retrieval.types import SearchHit, VectorQuery

pytestmark = [integration, pytest.mark.anyio]

DEFAULT: Final = ChunkingConfig()
SMALL: Final = ChunkingConfig(max_chars=400, overlap_chars=50)
CORPUS: Final = corpus()
FAKE_MODEL: Final = load_embedding_catalog().resolve("fake", None, 1024).model
DOCUMENT_IDS: Final = text("SELECT id FROM documents WHERE business_id = :b ORDER BY source_id")
UPROOT: Final = (
    text("DELETE FROM documents WHERE business_id = :b"),
    text("DELETE FROM businesses WHERE business_id = :b"),
)


CONFIG_INFO: Final = ConfigInfo(
    max_chars=DEFAULT.max_chars,
    overlap_chars=DEFAULT.overlap_chars,
    search_k=5,
    metric_k=5,
    rag_min_score=0.3,
    weak_evidence_outcome="dont_know",
    llm_max_retries=2,
    llm_max_repairs=1,
)
"""`config()` as run.json records it."""


def config(chunking: ChunkingConfig = DEFAULT) -> RunConfig:
    return RunConfig(
        chunking=chunking,
        search_k=5,
        metric_k=5,
        rag_min_score=0.3,
        weak_evidence_outcome="dont_know",
        llm_max_retries=2,
        llm_max_repairs=1,
    )


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
async def salts(owner_engine: AsyncEngine) -> AsyncIterator[list[str]]:
    """Salts handed out to a test; every namespace they make is removed afterwards."""
    made: list[str] = []
    yield made
    for salt in made:
        for chunking in (DEFAULT, SMALL):
            for business in namespaces(
                [SUNRISE, PAWS], embedding_model=FAKE_MODEL, chunking=chunking, salt=salt
            ).values():
                async with business_transaction(owner_engine, business) as connection:
                    for statement in UPROOT:
                        await connection.execute(statement, {"b": business})


def salted(salts: list[str], chunking: ChunkingConfig = DEFAULT) -> dict[str, str]:
    if not salts:
        salts.append(f"test-only-{secrets.token_hex(4)}")
    return namespaces([SUNRISE, PAWS], embedding_model=FAKE_MODEL, chunking=chunking, salt=salts[0])


async def ingest(
    mapping: dict[str, str], chunking: ChunkingConfig = DEFAULT, **kwargs: Any
) -> RunOutcome:
    return await execute(
        Target(corpus=CORPUS, corpus_dir=CORPUS_DIR, questions=(), mapping=mapping),
        Settings(),
        config(chunking),
        CostCap(cap=None),
        dry_run=True,
        owner_url=str(MigrationSettings().migration_database_url.get_secret_value()),
        records=lambda trace_id: None,
        sink=lambda result, answer: None,
        progress=lambda line: None,
        ingest_only=True,
        **kwargs,
    )


async def test_ingest_registers_ingests_and_checks_the_sample_corpus(salts: list[str]) -> None:
    mapping = salted(salts)

    first = await ingest(mapping)
    again = await ingest(mapping)

    assert (first.status, first.stop, first.problems) == ("complete", None, ())
    assert set(first.registered) == set(mapping.values())
    for count in first.ingest:
        assert (count.documents, count.created) == (9, 9)
        assert count.chunks_written == sum(
            len(CORPUS.chunks(d, DEFAULT)) for d in CORPUS.documents(count.business_id)
        )
    assert again.status == "complete" and again.registered == ()
    assert [(c.created, c.chunks_written) for c in again.ingest] == [(0, 0), (0, 0)]


async def test_another_chunk_size_is_a_fresh_namespace(salts: list[str]) -> None:
    default, small = salted(salts), salted(salts, SMALL)

    await ingest(default)
    outcome = await ingest(small, SMALL)

    assert set(default.values()).isdisjoint(small.values())
    assert outcome.status == "complete"
    assert [c.created for c in outcome.ingest] == [9, 9]


async def test_stored_chunks_at_another_size_are_refused_before_any_question(
    salts: list[str], app_engine: AsyncEngine
) -> None:
    mapping = salted(salts)
    await ingest(mapping)
    store = PostgresEvalStore(Database(app_engine))

    visible = await stored_visible(store, CORPUS, mapping, DEFAULT, FAKE_MODEL)
    with pytest.raises(StaleChunks):
        await stored_visible(store, CORPUS, mapping, SMALL, FAKE_MODEL)

    assert {c.business_id for c in visible[SUNRISE]} == {SUNRISE}
    assert {c.business_id for c in visible[PAWS]} == {PAWS}


async def document_ids(app_engine: AsyncEngine, mapping: dict[str, str]) -> dict[str, list[Any]]:
    ids: dict[str, list[Any]] = {}
    for business, eval_id in mapping.items():
        async with business_transaction(app_engine, eval_id) as connection:
            ids[business] = list((await connection.execute(DOCUMENT_IDS, {"b": eval_id})).scalars())
    return ids


async def test_a_hits_business_is_read_from_its_row_and_rls_hides_the_other_one(
    salts: list[str], app_engine: AsyncEngine
) -> None:
    mapping = salted(salts)
    await ingest(mapping)
    ids = await document_ids(app_engine, mapping)

    async with business_transaction(app_engine, mapping[SUNRISE]) as connection:
        found = await document_rows(connection, [ids[SUNRISE][0], ids[PAWS][0]])

    [(document_id, row)] = found.items()  # the other business's row: not visible
    assert document_id == ids[SUNRISE][0] and row.business_id == mapping[SUNRISE]
    assert isinstance(row, DocumentRow) and row.status and row.audience


async def test_a_hit_from_the_other_namespace_aborts_the_run_through_execute(
    salts: list[str], app_engine: AsyncEngine
) -> None:
    # No H1 and no H2 needed: the search is a stand-in and the leak fires before any scoring.
    mapping = salted(salts)
    await ingest(mapping)
    paws_document = (await document_ids(app_engine, mapping))[PAWS][0]

    async def other_namespace(connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        # H1's stand-in: one hit whose document is a real Paws document (no SQL, no ranking).
        return [hit("TEST-ONLY leaked chunk text", document_id=paws_document)]

    outcome = await execute(
        Target(
            corpus=CORPUS,
            corpus_dir=CORPUS_DIR,
            questions=(question("test-only-pg-leak"),),
            mapping=mapping,
        ),
        Settings(),
        config(),
        CostCap(cap=None),
        dry_run=True,
        owner_url=str(MigrationSettings().migration_database_url.get_secret_value()),
        records=lambda trace_id: None,
        sink=lambda result, answer: None,
        progress=lambda line: None,
        search=other_namespace,
    )

    assert outcome.status == "aborted_leak" and outcome.stop is not None
    assert outcome.stop.startswith("CROSS-BUSINESS LEAK: question test-only-pg-leak, rank 1")
    assert f"document {paws_document} is not visible in it" in outcome.stop  # RLS hid its row
    assert [r.outcome for r in outcome.results] == ["not_run"]


async def test_fresh_deletes_only_this_runs_namespaces(
    salts: list[str], app_engine: AsyncEngine
) -> None:
    mine, other = salted(salts), salted(salts, SMALL)
    await ingest(mine)
    await ingest(other, SMALL)

    outcome = await ingest(mine, fresh=True)

    assert [c.created for c in outcome.ingest] == [9, 9]  # deleted, then ingested again
    store = PostgresEvalStore(Database(app_engine))
    assert len(await store.stored_chunks(other[SUNRISE])) == sum(
        len(CORPUS.chunks(d, SMALL)) for d in CORPUS.documents(SUNRISE)
    )


@pytest.fixture
def capture() -> Iterator[LogCapture]:
    handler = LogCapture(io.StringIO())
    gateway_logger.addHandler(handler)
    try:
        yield handler
    finally:
        gateway_logger.removeHandler(handler)


def _chunk_text(business: str, source_id: str, version: str, anchor: str) -> str:
    """The stored text of the chunk holding `anchor`: with the fake embedder, a question that
    repeats a chunk's text scores 1 against it, so the expected first rank is known."""
    document = CORPUS.document(business, source_id, version)
    assert document is not None
    return next(c.content for c in CORPUS.chunks(document, DEFAULT) if anchor in c.content)


@pytest.mark.parametrize(
    "marks",
    [pytest.param(None, marks=pending_hand_writes("vector_search", "retrieval_metrics"))],
    ids=["h1-and-h2"],
)
async def test_a_dry_run_answers_and_scores_test_only_questions_end_to_end(
    salts: list[str], capture: LogCapture, marks: None, tmp_path: Path
) -> None:
    mapping = salted(salts)
    questions = (
        # TEST-ONLY fixtures: a chunk's own text as the question (fake embedder: score 1).
        question(
            "test-only-pg-1", text=_chunk_text(SUNRISE, "faq", "2026-10", SUNRISE_HOURS.anchor)
        ),
        question(
            "test-only-pg-2",
            PAWS,
            evidence=((PAWS_STORE_HOURS,),),
            text=_chunk_text(PAWS, "faq", "2026-09", PAWS_STORE_HOURS.anchor),
        ),
        question("test-only-pg-3", evidence=(), expected="dont_know", category="out_of_scope"),
    )
    sunk: list[tuple[QuestionResult, AnswerLine | None]] = []

    outcome = await execute(
        Target(corpus=CORPUS, corpus_dir=CORPUS_DIR, questions=questions, mapping=mapping),
        Settings(),
        config(),
        CostCap(cap=None),
        dry_run=True,
        owner_url=str(MigrationSettings().migration_database_url.get_secret_value()),
        records=lambda trace_id: record_for(capture.lines, trace_id),
        sink=lambda result, answer: sunk.append((result, answer)),
        progress=lambda line: None,
    )

    assert (outcome.status, outcome.stop) == ("complete", None)
    first, second, third = outcome.results
    for result in (first, second):
        assert (result.retrieval, result.hit_at_k, result.reciprocal_rank) == ("scored", True, "1")
        assert result.hits[0].groups == (0,) and result.outcome == "answered"
    assert third.retrieval == "not_scored"
    assert [r for r, _ in sunk] == outcome.results
    # The summary of these results, through H2 itself: 2 of 2 hits at rank 1, MRR 1.
    folder = tmp_path / "test-only-run"
    folder.mkdir()
    info = run_info(questions_selected=len(questions), config=CONFIG_INFO)
    (folder / RUN_FILE).write_text(info.model_dump_json(), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(
        "".join(r.model_dump_json() + "\n" for r in outcome.results), encoding="utf-8"
    )
    everything = summarize(folder).retrieval[0]
    assert (everything.group, everything.hit.x, everything.hit.n) == ("all", 2, 2)
    assert (everything.mrr.value, everything.mrr.ranks["1"]) == ("1", 2)
