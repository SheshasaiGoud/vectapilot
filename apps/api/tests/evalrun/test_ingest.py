"""Ingesting the eval corpus through `KbService` and checking the stored chunks against it
(`evalrun/ingest.py`, `harness.stored_visible`). The in-memory `KbStore` stands in for Postgres
(`tests/kb/fakes.py`, same contract); the corpus is the real synthetic `data/sample`."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

import pytest

from tests.embeddings.scripted import FAKE_SPEC, POLICY, FakeClock, ScriptedEmbedder
from tests.evalrun.helpers import CORPUS_DIR, PAWS, SUNRISE, corpus
from tests.kb.fakes import InMemoryKbStore
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.evalrun.harness import stored_visible
from vectapilot_api.evalrun.ingest import (
    IngestRefused,
    StaleChunks,
    ingest_corpus,
    verify_stored,
)
from vectapilot_api.evalrun.namespace import namespaces
from vectapilot_api.evalrun.store import StoredChunk
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.kb.service import KbService

DEFAULT: Final = ChunkingConfig()
SMALL: Final = ChunkingConfig(max_chars=400, overlap_chars=50)
CORPUS: Final = corpus()
MAPPING: Final = namespaces([SUNRISE, PAWS], embedding_model=FAKE_SPEC.model, chunking=DEFAULT)


def read_file(business_id: str, name: str) -> bytes:
    return (CORPUS_DIR / business_id / name).read_bytes()


@dataclass
class Kb:
    service: KbService
    store: InMemoryKbStore
    embedder: ScriptedEmbedder


def kb(config: ChunkingConfig = DEFAULT) -> Kb:
    clock = FakeClock()
    embedder = ScriptedEmbedder()
    runner = EmbeddingRunner(
        embedder, FAKE_SPEC, POLICY, deadline_s=100.0, clock=clock, sleep=clock.sleep
    )
    store = InMemoryKbStore()
    return Kb(KbService(store, runner, chunking=config), store, embedder)


def stored_of(store: InMemoryKbStore, business_id: str) -> tuple[StoredChunk, ...]:
    """The in-memory store's rows as `store.StoredChunk`s (what the Postgres query returns)."""
    documents = {
        document_id: document
        for (business, _, _), (document_id, document) in store.documents.items()
        if business == business_id
    }
    return tuple(
        StoredChunk(
            business_id=row.business_id,
            source_id=documents[row.document_id].source_id,
            version=documents[row.document_id].version,
            status=documents[row.document_id].status,
            audience=documents[row.document_id].audience,
            chunk_index=row.chunk.chunk_index,
            content=row.chunk.content,
            embedding_model=row.model,
        )
        for row in store.chunks_of(business_id)
    )


def chunk_count(corpus: Corpus, business_id: str, config: ChunkingConfig) -> int:
    return sum(len(corpus.chunks(d, config)) for d in corpus.documents(business_id))


@pytest.mark.anyio
async def test_every_listed_document_goes_through_kbservice_into_its_eval_business() -> None:
    target = kb()

    counts = await ingest_corpus(target.service, CORPUS, MAPPING, read_file)

    assert [(c.business_id, c.eval_business_id) for c in counts] == list(MAPPING.items())
    for count in counts:
        assert (count.documents, count.created) == (9, 9)
        assert count.chunks_written == chunk_count(CORPUS, count.business_id, DEFAULT)
        assert count.retired_versions == 0  # Sunrise's old fees version is uploaded as retired
    assert {key[0] for key in target.store.documents} == set(MAPPING.values())
    assert target.store.statuses(MAPPING[SUNRISE], "fees") == {
        "2026-04": "approved",
        "2025-10": "retired",
    }


@pytest.mark.anyio
async def test_a_second_ingest_embeds_nothing_and_writes_nothing() -> None:
    target = kb()
    await ingest_corpus(target.service, CORPUS, MAPPING, read_file)
    calls = len(target.embedder.batches)

    again = await ingest_corpus(target.service, CORPUS, MAPPING, read_file)

    assert [(c.created, c.chunks_written) for c in again] == [(0, 0), (0, 0)]
    assert len(target.embedder.batches) == calls


@pytest.mark.anyio
async def test_a_document_changed_under_a_stored_version_is_refused_by_name() -> None:
    target = kb()
    await ingest_corpus(target.service, CORPUS, MAPPING, read_file)

    def edited(business_id: str, name: str) -> bytes:
        data = read_file(business_id, name)
        return data + b"\nTEST-ONLY edit\n" if name == "faq.md" else data

    with pytest.raises(IngestRefused) as refused:
        await ingest_corpus(target.service, CORPUS, MAPPING, edited)
    assert "sunrise-clinic/faq.md (faq 2026-10)" in str(refused.value)
    assert "VersionConflict" in str(refused.value) and "--fresh" in str(refused.value)
    assert "TEST-ONLY" not in str(refused.value)


@pytest.mark.anyio
async def test_a_language_changed_under_a_stored_version_is_refused_by_name() -> None:
    target = kb()
    await ingest_corpus(target.service, CORPUS, MAPPING, read_file)
    sunrise = CORPUS.manifests[SUNRISE]
    relabelled = sunrise.model_copy(
        update={
            "sources": tuple(
                source.model_copy(update={"language": "en-IN"})
                if source.file == "faq.md"
                else source
                for source in sunrise.sources
            )
        }
    )
    changed = Corpus(
        [relabelled, *(m for b, m in CORPUS.manifests.items() if b != SUNRISE)],
        [document for b in sorted(CORPUS.business_ids) for document in CORPUS.documents(b)],
    )

    with pytest.raises(IngestRefused) as refused:
        await ingest_corpus(target.service, changed, MAPPING, read_file)
    assert "sunrise-clinic/faq.md (faq 2026-10)" in str(refused.value)
    assert "LanguageConflict" in str(refused.value) and "--fresh" in str(refused.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("file", "audience", "named"),
    [
        ("faq.md", "staff_only", "(faq 2026-10)"),
        ("staff-handbook.md", "public", "(staff-handbook 2026-10)"),
    ],
    ids=["public to staff_only", "staff_only to public"],
)
async def test_an_audience_changed_under_a_stored_version_is_refused_by_name(
    file: str, audience: str, named: str
) -> None:
    target = kb()
    await ingest_corpus(target.service, CORPUS, MAPPING, read_file)
    documents, chunks = dict(target.store.documents), dict(target.store.chunks)
    batches = len(target.embedder.batches)
    sunrise = CORPUS.manifests[SUNRISE]
    relabelled = sunrise.model_copy(
        update={
            "sources": tuple(
                source.model_copy(update={"audience": audience}) if source.file == file else source
                for source in sunrise.sources
            )
        }
    )
    changed = Corpus(
        [relabelled, *(m for b, m in CORPUS.manifests.items() if b != SUNRISE)],
        [document for b in sorted(CORPUS.business_ids) for document in CORPUS.documents(b)],
    )

    with pytest.raises(IngestRefused) as refused:
        await ingest_corpus(target.service, changed, MAPPING, read_file)
    assert f"sunrise-clinic/{file} {named}" in str(refused.value)
    assert "AudienceConflict" in str(refused.value) and "--fresh" in str(refused.value)
    assert target.store.documents == documents  # the stored audience is unchanged
    assert target.store.chunks == chunks
    # Refused before loading: no batch embedded (no cost); the first ingest's batches show that
    # the count sees this embedder.
    after = len(target.embedder.batches)  # a count: a failure prints no corpus text
    assert batches and after == batches


@pytest.fixture
async def ingested() -> InMemoryKbStore:
    target = kb()
    await ingest_corpus(target.service, CORPUS, MAPPING, read_file)
    return target.store


@pytest.mark.anyio
async def test_the_stored_chunks_are_the_corpus_at_the_run_config(
    ingested: InMemoryKbStore,
) -> None:
    for business, eval_id in MAPPING.items():
        stored = stored_of(ingested, eval_id)
        assert verify_stored(CORPUS, business, DEFAULT, stored, FAKE_SPEC.model) == []


@pytest.mark.anyio
async def test_chunks_stored_at_another_size_are_named_document_by_document(
    ingested: InMemoryKbStore,
) -> None:
    # Measured in PR 7: at 400/50 six sections split, so those documents chunk differently.
    problems = verify_stored(
        CORPUS, SUNRISE, SMALL, stored_of(ingested, MAPPING[SUNRISE]), FAKE_SPEC.model
    )

    assert problems
    expected = "stored chunks differ from the corpus chunked at max_chars 400"
    assert all(expected in problem for problem in problems)


Tamper = Callable[[tuple[StoredChunk, ...]], tuple[StoredChunk, ...]]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (
            lambda rows: (*rows, replace(rows[0], source_id="test-only-leftover")),
            "test-only-leftover 2026-10 is stored but not in the corpus manifest",
        ),
        (
            lambda rows: (
                tuple(replace(r, embedding_model="old-model") for r in rows[:1]) + rows[1:]
            ),
            "1 other embedding model id(s) are stored next to fake-embedding-1024",
        ),
        (
            lambda rows: tuple(r for r in rows if r.source_id != "services"),
            "services 2026-10 is not stored",
        ),
        (
            lambda rows: tuple(
                replace(r, status="draft") if r.source_id == "emergency" else r for r in rows
            ),
            "emergency 2026-10: stored status or audience differs from the manifest",
        ),
        (
            lambda rows: tuple(
                replace(r, content=r.content + " ") if r.source_id == "faq" else r for r in rows
            ),
            "faq 2026-10: stored chunks differ",
        ),
    ],
    ids=["leftover", "other-model", "missing", "status", "content"],
)
async def test_every_difference_is_a_problem_naming_ids_only(
    ingested: InMemoryKbStore, tamper: Tamper, message: str
) -> None:
    stored = tamper(stored_of(ingested, MAPPING[SUNRISE]))

    problems = verify_stored(CORPUS, SUNRISE, DEFAULT, stored, FAKE_SPEC.model)

    assert any(message in problem for problem in problems), problems


@dataclass
class MemoryEvalStore:
    """`store.EvalStore` over the in-memory KB store."""

    store: InMemoryKbStore

    async def stored_chunks(self, business_id: str) -> tuple[StoredChunk, ...]:
        return stored_of(self.store, business_id)

    async def business_exists(self, business_id: str) -> bool:
        return True

    async def delete_documents(self, business_id: str) -> int:
        return 0


@pytest.mark.anyio
async def test_only_approved_public_chunks_are_visible_with_their_rows_business(
    ingested: InMemoryKbStore,
) -> None:
    visible = await stored_visible(
        MemoryEvalStore(ingested), CORPUS, MAPPING, DEFAULT, FAKE_SPEC.model
    )

    sunrise = visible[SUNRISE]
    assert {c.business_id for c in sunrise} == {SUNRISE}
    assert ("staff-handbook", "2026-10") not in {(c.source_id, c.version) for c in sunrise}
    assert ("fees", "2025-10") not in {(c.source_id, c.version) for c in sunrise}
    expected = sum(
        len(CORPUS.chunks(d, DEFAULT))
        for d in CORPUS.documents(SUNRISE)
        if d.source.answers_customers
    )
    assert len(sunrise) == expected


@pytest.mark.anyio
async def test_stale_chunks_stop_the_run_before_any_question(
    ingested: InMemoryKbStore,
) -> None:
    with pytest.raises(StaleChunks) as stale:
        await stored_visible(MemoryEvalStore(ingested), CORPUS, MAPPING, SMALL, FAKE_SPEC.model)

    assert stale.value.problems and "--fresh" in str(stale.value)


@pytest.mark.anyio
async def test_a_stop_rule_set_by_a_paid_call_stops_the_ingest_before_the_next_document() -> None:
    target = kb()

    # The cap's stop rule as the harness passes it: here, set once anything was embedded.
    counts = await ingest_corpus(
        target.service,
        CORPUS,
        MAPPING,
        read_file,
        should_stop=lambda: bool(target.embedder.batches),
    )

    [count] = counts  # Sunrise only, after its first document; Paws never started
    assert (count.business_id, count.documents, count.created) == (SUNRISE, 1, 1)
    first = CORPUS.manifests[SUNRISE].sources[0]
    assert {key[1:] for key in target.store.documents} == {(first.source_id, first.version)}


@pytest.mark.anyio
async def test_a_stop_rule_set_by_a_businesss_last_document_stops_before_the_next_business() -> (
    None
):
    target = kb()
    listed = len(CORPUS.manifests[SUNRISE].sources)

    counts = await ingest_corpus(
        target.service,
        CORPUS,
        MAPPING,
        read_file,
        should_stop=lambda: len(target.store.documents) >= listed,
    )

    [count] = counts  # Sunrise complete; Paws never started, and no empty count for it
    assert (count.business_id, count.documents, count.created) == (SUNRISE, listed, listed)
