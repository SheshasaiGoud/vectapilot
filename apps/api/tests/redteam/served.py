"""The red-team suite's database environment (M7 PR 2, reworked by PR 6): the real app, as the app
role, with the guards on, serving the sample businesses and the fixture business `rt-shop`, once
per module (CI time).

**The folders** come from `redteam.businesses.write_app_folders`: the sample businesses' tool
files and corpus, `rt-shop`'s from the dataset's `fixtures/`, and since PR 6 the guard folder
(`data/safety/` plus `fixtures/rt-shop/safety.json`, leo's PR 2 review), with the committed
extraction folder (`data/extraction/`, F's extraction route). Nothing is ever written under
`data/`.

**The app** is `create_app` with `guard_routes = "on"` and one seam: an allowing in-memory limiter
(`guards.fake_limiter.FakeLimiter`), so the suite never writes a key on the stack's own Redis
database [proposal, PR 6]: the cases that test Redis run on the isolated logical database 15
(`redteam.limiter`, k-002, k-004; `redteam.agent`, k-003), and J arms its outages on its own
in-memory limiter. Each agent case's guarded services are built by the agent runner exactly as
`create_app` builds them and answer the route for that one request (`redteam.agent.overriding`):
the route, the business lookup, the database, the tools and the pending-action store are the
app's own.

**Rows, as the owner role.** Every served business's `businesses` row is inserted when missing
(the sample businesses have no seed migration), and `rt-shop`'s sandbox is reseeded from its
`seed.json` through the real reset (leo's PR 2 review). k-001's arrangement (PR 5's decision:
planting and owner-role cleanup belong here) plants, for each sample business, one approved,
public document titled with that business's own canary, whose one chunk is k-001's question
under the configured (fake) embedding model with the fake embedder's vector of it: a search that
leaked across businesses would return the other one at score 1.0. Afterwards, in a `finally`,
exactly the planted documents go (their chunks with them), and a business inserted here is
uprooted; a business that existed before keeps everything it had: no row is ever left for
`sunrise-clinic` or `paws-and-co`.

**The answer path's end-to-end cases** (M7 PR 8: b-001, h-009, `ANSWER_CASES`) plant their own
rows the same way: each `fixture.chunks` entry becomes one approved document of its audience with
one chunk, its filled text under the fake embedder's vector of the question as the guarded
answer route embeds it (cleaned and masked, `embedded_question`), so the real search returns
the case's chunks at score 1.0. The suite hands their ids over as `answer_chunks` (case id to
chunk slug to chunk id; `redteam.answer.planted_ids`), and removes exactly those documents in
the same `finally`. The app keeps `guard_answer` off: each answer case's guarded pipeline answers
the route's two guard dependencies for its one request (`redteam.answer.answering`), so
`/v1/kb/search` stays k-001's plain search.
"""

import asyncio
import secrets
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.guards.helpers import SAFETY_DIR, SAMPLE_EXTRACTION_DIR
from tests.redteam.suite import CASES, DATASET, DATASET_DIR, END_TO_END, GENERATOR
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from tests.tools.sandbox_db import fresh_engine, owner_url, seed_business, uproot
from vectapilot_api.answer.types import AnswerRequest
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.locales import locales
from vectapilot_api.guards.pii import mask_pii
from vectapilot_api.guards.screens import prepare
from vectapilot_api.ingestion.language_map import text_search_config
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.main import create_app
from vectapilot_api.redteam.businesses import (
    AppFolders,
    customer_ref_for,
    fixture_business_tools,
    write_app_folders,
)
from vectapilot_api.redteam.cases import Case, ChunkFixture
from vectapilot_api.redteam.plan import CasePath, environment_of, path_of
from vectapilot_api.tools.files import load_tools_dir
from vectapilot_api.tools.sandbox import SandboxSeed

SEARCH_CASE: Final = "k-001"
PLANT_LANGUAGE: Final = "en"
_INSERT: Final = text(
    "INSERT INTO businesses (business_id) VALUES (:b) ON CONFLICT DO NOTHING RETURNING business_id"
)
_PLANT_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type,"
    " content_kind, language, status, audience, content_sha256)"
    " VALUES (:b, :source, '1', :canary, 'markdown', 'faq', :language, 'approved', 'public',"
    " repeat('e', 64))"
)
_PLANT_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
    " embedding_model, embedding_dim, ts_config)"
    " SELECT :b, id, 0, :content, CAST(:embedding AS vector), :model,"
    " CAST(:dimension AS integer), CAST(:ts_config AS regconfig)"
    " FROM documents WHERE business_id = :b AND source_id = :source"
)
_REMOVE_PLANTED: Final = text(
    "DELETE FROM documents WHERE business_id = :b AND source_id = :source"
)  # its chunks go with it
ANSWER_CASES: Final = tuple(
    sorted(
        case.id
        for case in CASES.values()
        if path_of(case) is CasePath.ANSWER and environment_of(case, END_TO_END) == "database"
    )
)
"""The answer path's end-to-end cases, whose chunks are planted rows (module docstring)."""
ANSWER_TITLE: Final = "TEST-ONLY red-team chunk"
_PLANT_ANSWER_DOCUMENT: Final = text(
    "INSERT INTO documents (business_id, source_id, version, title, source_type,"
    " content_kind, language, status, audience, content_sha256)"
    " VALUES (:b, :source, '1', :title, 'markdown', 'faq', :language, 'approved', :audience,"
    " repeat('e', 64))"
)
_PLANT_ANSWER_CHUNK: Final = text(
    "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
    " embedding_model, embedding_dim, ts_config)"
    " SELECT :b, id, 0, :content, CAST(:embedding AS vector), :model,"
    " CAST(:dimension AS integer), CAST(:ts_config AS regconfig)"
    " FROM documents WHERE business_id = :b AND source_id = :source"
    " RETURNING id"
)


async def _insert_business(business_id: str) -> bool:
    """As the owner: the business's row when missing. True when this call inserted it."""
    owner = fresh_engine(owner_url())
    try:
        async with owner.begin() as connection:
            return (await connection.execute(_INSERT, {"b": business_id})).first() is not None
    finally:
        await owner.dispose()


async def _seed(seed: SandboxSeed) -> None:
    owner = fresh_engine(owner_url())
    try:
        await seed_business(owner, seed)
    finally:
        await owner.dispose()


async def _remove(business_id: str) -> None:
    owner = fresh_engine(owner_url())
    try:
        await uproot(owner, business_id)
    finally:
        await owner.dispose()


@dataclass(frozen=True)
class Planted:
    business_id: str
    source_id: str
    canary: str


async def _plant(business_id: str, question: str, settings: Settings) -> Planted:
    """k-001's planted document for one business (module docstring), as the owner."""
    spec = load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    planted = Planted(
        business_id, f"redteam-k001-{secrets.token_hex(4)}", f"canary-{secrets.token_hex(8)}"
    )
    params = {
        "b": business_id,
        "source": planted.source_id,
        "canary": planted.canary,
        "language": PLANT_LANGUAGE,
        "content": question,
        "embedding": vector_literal(fake_vector(question, spec.dimension)),
        "model": spec.model,
        "dimension": spec.dimension,
        "ts_config": text_search_config(PLANT_LANGUAGE),
    }
    owner = fresh_engine(owner_url())
    try:
        async with business_transaction(owner, business_id) as connection:
            await connection.execute(_PLANT_DOCUMENT, params)
            await connection.execute(_PLANT_CHUNK, params)
    finally:
        await owner.dispose()
    return planted


def embedded_question(text: str) -> str:
    """The question as the guarded answer route embeds it: cleaned (G5), masked by H2 (G6) and
    validated as an `AnswerRequest` again (`guards.answer`, steps 2 and 7)."""
    masked = mask_pii(prepare(text).clean, locales()).text
    return AnswerRequest.model_validate({"question": masked}).question


async def _plant_answer_chunk(
    case: Case, chunk: ChunkFixture, settings: Settings
) -> tuple[Planted, uuid.UUID]:
    """One answer case's chunk as an approved document with one chunk, as the owner (module
    docstring); its id."""
    spec = load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    question = embedded_question(GENERATOR.fill(case.id, case.input))
    planted = Planted(
        case.business_id,
        f"redteam-{case.id}-{chunk.id}-{secrets.token_hex(4)}",
        ANSWER_TITLE,  # no canary: these rows are found by their ids
    )
    params = {
        "b": case.business_id,
        "source": planted.source_id,
        "title": ANSWER_TITLE,
        "language": case.language,
        "audience": chunk.audience,
        "content": GENERATOR.fill(case.id, chunk.text),
        "embedding": vector_literal(fake_vector(question, spec.dimension)),
        "model": spec.model,
        "dimension": spec.dimension,
        "ts_config": text_search_config(case.language),
    }
    owner = fresh_engine(owner_url())
    try:
        async with business_transaction(owner, case.business_id) as connection:
            await connection.execute(_PLANT_ANSWER_DOCUMENT, params)
            chunk_id = (await connection.execute(_PLANT_ANSWER_CHUNK, params)).scalar_one()
    finally:
        await owner.dispose()
    return planted, uuid.UUID(str(chunk_id))


async def _unplant(planted: Planted) -> None:
    owner = fresh_engine(owner_url())
    try:
        async with business_transaction(owner, planted.business_id) as connection:
            await connection.execute(
                _REMOVE_PLANTED, {"b": planted.business_id, "source": planted.source_id}
            )
    finally:
        await owner.dispose()


@dataclass(frozen=True)
class ServedSuite:
    client: TestClient
    app: FastAPI
    folders: AppFolders
    seeds: Mapping[str, SandboxSeed]
    """Every served business's sandbox seed, by business id."""
    kb_canaries: Mapping[str, str]
    """k-001's planted canary per sample business (`redteam.search`)."""
    answer_chunks: Mapping[str, Mapping[str, uuid.UUID]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    """The answer path's end-to-end cases' planted chunk ids, by case and chunk slug (M7 PR 8)."""

    def customer_for(self, case: Case) -> str:
        return customer_ref_for(case, self.seeds[case.business_id])


def served_suite() -> Iterator[ServedSuite]:
    """The module's served app (module docstring); a generator for a module-scoped fixture."""
    inserted: list[str] = []
    planted: list[Planted] = []
    answer_planted: list[Planted] = []
    fixtures = {
        business_id: fixture_business_tools(DATASET_DIR, business_id).seed
        for business_id in DATASET.manifest.fixture_businesses
    }
    with tempfile.TemporaryDirectory() as directory:
        folders = write_app_folders(
            Path(directory),
            dataset_dir=DATASET_DIR,
            sandbox_dir=SANDBOX_DIR,
            corpus_dir=CORPUS_DIR,
            fixture_businesses=DATASET.manifest.fixture_businesses,
            safety_dir=SAFETY_DIR,
        )
        seeds = {
            business_id: t.seed for business_id, t in load_tools_dir(folders.tools_dir).items()
        }
        # From the environment, as the canary's served apps: the integration database.
        settings = Settings(
            tools_config_dir=folders.tools_dir,
            corpus_dir=folders.corpus_dir,
            guard_config_dir=folders.guard_dir,
            extraction_config_dir=SAMPLE_EXTRACTION_DIR,
            guard_routes="on",
            guard_answer="off",  # whatever the environment says (module docstring)
        )
        try:
            for business_id in sorted(seeds):
                if asyncio.run(_insert_business(business_id)):
                    inserted.append(business_id)
            for seed in fixtures.values():
                asyncio.run(_seed(seed))
            question = CASES[SEARCH_CASE].input
            for business_id in sorted(set(seeds) - set(fixtures)):
                planted.append(asyncio.run(_plant(business_id, question, settings)))
            answer_chunks: dict[str, Mapping[str, uuid.UUID]] = {}
            for case_id in ANSWER_CASES:
                ids: dict[str, uuid.UUID] = {}
                for chunk in CASES[case_id].fixture.chunks:
                    row, ids[chunk.id] = asyncio.run(
                        _plant_answer_chunk(CASES[case_id], chunk, settings)
                    )
                    answer_planted.append(row)
                answer_chunks[case_id] = MappingProxyType(ids)
            app = create_app(settings, limiter=FakeLimiter())
            with TestClient(app) as client:
                yield ServedSuite(
                    client=client,
                    app=app,
                    folders=folders,
                    seeds=seeds,
                    kb_canaries={p.business_id: p.canary for p in planted},
                    answer_chunks=MappingProxyType(answer_chunks),
                )
        finally:
            for row in (*planted, *answer_planted):
                asyncio.run(_unplant(row))
            for business_id in inserted:
                asyncio.run(_remove(business_id))
