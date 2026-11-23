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
`sunrise-clinic` or `paws-and-co`. Since PR 7 the document path's end-to-end case (d-001) stores
an extraction through the real route (the app role has no DELETE on it, migration 0007): the
runner hands each stored id back (`stored_extractions`), and the `finally` deletes exactly those
rows as the owner (their fields with them).
"""

import asyncio
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.guards.helpers import SAFETY_DIR, SAMPLE_EXTRACTION_DIR
from tests.redteam.suite import CASES, DATASET, DATASET_DIR
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from tests.tools.sandbox_db import fresh_engine, owner_url, seed_business, uproot
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.ingestion.language_map import text_search_config
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.main import create_app
from vectapilot_api.redteam.businesses import (
    AppFolders,
    customer_ref_for,
    fixture_business_tools,
    write_app_folders,
)
from vectapilot_api.redteam.cases import Case
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
_REMOVE_EXTRACTION: Final = text(
    "DELETE FROM extractions WHERE business_id = :b AND id = CAST(:id AS uuid)"
)  # its fields go with it


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


async def _remove_extraction(business_id: str, extraction_id: str) -> None:
    """As the owner: one extraction a case stored (`redteam.document`), named by business and id."""
    owner = fresh_engine(owner_url())
    try:
        async with business_transaction(owner, business_id) as connection:
            await connection.execute(_REMOVE_EXTRACTION, {"b": business_id, "id": extraction_id})
    finally:
        await owner.dispose()


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
    stored_extractions: list[tuple[str, str]]
    """(business id, extraction id) of every extraction a case stored (`redteam.document`):
    deleted as the owner when the suite ends."""

    def customer_for(self, case: Case) -> str:
        return customer_ref_for(case, self.seeds[case.business_id])


def served_suite() -> Iterator[ServedSuite]:
    """The module's served app (module docstring); a generator for a module-scoped fixture."""
    inserted: list[str] = []
    planted: list[Planted] = []
    stored: list[tuple[str, str]] = []
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
            app = create_app(settings, limiter=FakeLimiter())
            with TestClient(app) as client:
                yield ServedSuite(
                    client=client,
                    app=app,
                    folders=folders,
                    seeds=seeds,
                    kb_canaries={p.business_id: p.canary for p in planted},
                    stored_extractions=stored,
                )
        finally:
            for business_id, extraction_id in stored:
                asyncio.run(_remove_extraction(business_id, extraction_id))
            for row in planted:
                asyncio.run(_unplant(row))
            for business_id in inserted:
                asyncio.run(_remove(business_id))
