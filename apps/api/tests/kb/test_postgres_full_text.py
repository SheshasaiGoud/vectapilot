"""The full-text columns (migration 0005, ADR 0014) via `PostgresKbStore`, against real Postgres.

Integration only (CI; locally `make test-integration`). Every row is written and read as the app
role, under RLS; the owner only registers and removes the test business. `ts_config` is always
read as `ts_config::text`, never raw (asyncpg's `regconfig` codec is not relied on).
"""

import uuid
from collections.abc import AsyncIterator
from typing import Final, NamedTuple

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import integration
from tests.kb.test_postgres_store import counts, registered_business
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, business_transaction
from vectapilot_api.ingestion.language_map import (
    FALLBACK_CONFIG,
    TEXT_SEARCH_CONFIGS,
    text_search_config,
)
from vectapilot_api.ingestion.types import SourceType
from vectapilot_api.kb import store as kb_store
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.kb.types import ContentKind, NewChunk, NewDocument

pytestmark = [integration, pytest.mark.anyio]

EMBEDDING: Final = (0.0,) * 1023 + (1.0,)
GENERATED_ALWAYS: Final = "428C9"
"""Postgres' SQLSTATE for a write that sets a GENERATED ALWAYS column."""
UNDEFINED_OBJECT: Final = "42704"
"""Postgres' SQLSTATE for a name that does not exist (here: no such text-search config)."""

CHUNK_FACTS: Final = text(
    "SELECT ts_config::text AS ts_config,"
    " tsv = to_tsvector(CAST(:expected AS regconfig), content) AS tsv_in_expected,"
    " tsv = to_tsvector(CAST('simple' AS regconfig), content) AS tsv_as_simple,"
    " tsv @@ CAST(:lexeme AS tsquery) AS matches"
    " FROM chunks WHERE business_id = :b"
)
CONFIG_SCHEMA: Final = text(
    "SELECT cfgnamespace::regnamespace::text FROM pg_catalog.pg_ts_config"
    " WHERE oid = CAST(:config AS regconfig)"
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
    async with registered_business() as business_id:
        yield business_id


def document(language: str) -> NewDocument:
    return NewDocument(
        source_id=f"src-{uuid.uuid4().hex[:8]}",
        version="1",
        title="Full text",
        source_type=SourceType.MARKDOWN,
        content_kind=ContentKind.PRICING,
        language=language,
        status="draft",
        audience="public",
        content_sha256="a" * 64,
        review_by=None,
        metadata={},
    )


async def store_one_chunk(engine: AsyncEngine, business: str, language: str, content: str) -> None:
    store = PostgresKbStore(Database(engine))
    chunk = NewChunk(chunk_index=0, content=content, page=None, metadata={}, embedding=EMBEDDING)
    await store.save_document(business, document(language), [chunk], model="m", dimension=1024)


class ChunkFacts(NamedTuple):
    """What a test checks about the one chunk it stored (a failure shows the field names)."""

    ts_config: str
    tsv_is_expected_config: bool
    """`tsv` equals `to_tsvector(expected, content)`."""
    tsv_is_simple: bool
    """`tsv` equals `to_tsvector('simple', content)`."""
    matches_lexeme: bool
    """`tsv @@ lexeme`."""


async def chunk_facts(
    engine: AsyncEngine, business: str, *, expected: str, lexeme: str
) -> ChunkFacts:
    params = {"b": business, "expected": expected, "lexeme": lexeme}
    async with business_transaction(engine, business) as connection:
        row = (await connection.execute(CHUNK_FACTS, params)).one()
    return ChunkFacts(
        ts_config=row.ts_config,
        tsv_is_expected_config=row.tsv_in_expected,
        tsv_is_simple=row.tsv_as_simple,
        matches_lexeme=row.matches,
    )


async def test_every_mapped_config_and_the_fallback_resolve_to_postgres_own(
    app_engine: AsyncEngine,
) -> None:
    # Resolved as the app role resolves the store's CAST(:ts_config AS regconfig): each name is
    # a built-in config (pg_catalog), not a same-named one elsewhere on the search path.
    async with app_engine.connect() as connection:
        for config in (*TEXT_SEARCH_CONFIGS.values(), FALLBACK_CONFIG):
            assert await connection.scalar(CONFIG_SCHEMA, {"config": config}) == "pg_catalog"


async def test_postgres_still_has_no_telugu_config(app_engine: AsyncEngine) -> None:
    # Why `te` falls back to `simple` (ADR 0014). Red the day a Postgres upgrade adds one: then
    # decide whether the map gains `te` (with its own backfill migration).
    async with app_engine.connect() as connection:
        found = await connection.scalar(
            text("SELECT count(*) FROM pg_catalog.pg_ts_config WHERE cfgname = 'telugu'")
        )
    assert found == 0


async def test_an_en_chunk_with_fees_matches_the_lexeme_fee(
    app_engine: AsyncEngine, business: str
) -> None:
    await store_one_chunk(app_engine, business, "en", "Our fees are listed below.")

    facts = await chunk_facts(app_engine, business, expected="english", lexeme="fee")

    # english, stemmed: 'fees' became 'fee', which `simple` would have kept as 'fees'.
    assert facts == ChunkFacts(
        ts_config="english", tsv_is_expected_config=True, tsv_is_simple=False, matches_lexeme=True
    )


async def test_an_hi_chunk_gets_hindi(app_engine: AsyncEngine, business: str) -> None:
    # कितना ("how much") is stemmed to कित by `hindi`, and kept whole by `simple`.
    await store_one_chunk(app_engine, business, "hi", "परामर्श शुल्क कितना है")

    facts = await chunk_facts(app_engine, business, expected="hindi", lexeme="कित")

    assert facts == ChunkFacts(
        ts_config="hindi", tsv_is_expected_config=True, tsv_is_simple=False, matches_lexeme=True
    )


@pytest.mark.parametrize(
    ("language", "content", "lexeme"),
    [
        ("te", "క్లినిక్ ఫీజులు", "ఫీజులు"),  # no Telugu config: lower-cased, never stemmed
        ("fr", "Les tarifs", "tarifs"),
    ],
)
async def test_an_unknown_language_gets_simple(
    app_engine: AsyncEngine, business: str, language: str, content: str, lexeme: str
) -> None:
    await store_one_chunk(app_engine, business, language, content)

    facts = await chunk_facts(app_engine, business, expected="simple", lexeme=lexeme)

    assert facts == ChunkFacts(
        ts_config="simple", tsv_is_expected_config=True, tsv_is_simple=True, matches_lexeme=True
    )


@pytest.mark.parametrize("language", ["en-IN", "hi-Latn"])
async def test_a_tag_with_subtags_gets_its_languages_config(
    app_engine: AsyncEngine, business: str, language: str
) -> None:
    # Romanized Hinglish under `hindi`: its Latin words are stemmed as English ('fees' -> 'fee').
    await store_one_chunk(app_engine, business, language, "Appointment fees kitni hai")
    expected = text_search_config(language)

    facts = await chunk_facts(app_engine, business, expected=expected, lexeme="fee")

    assert facts == ChunkFacts(
        ts_config=expected, tsv_is_expected_config=True, tsv_is_simple=False, matches_lexeme=True
    )


WRITES_TO_TSV: Final = {
    "insert": text(
        "INSERT INTO chunks (business_id, document_id, chunk_index, content, embedding,"
        " embedding_model, embedding_dim, ts_config, tsv)"
        " SELECT :b, id, 1, 'Opening hours', CAST(:embedding AS vector), 'm', 1024,"
        " CAST('english' AS regconfig), to_tsvector(CAST('simple' AS regconfig), 'forged')"
        " FROM documents WHERE business_id = :b"
    ),
    "update": text(
        "UPDATE chunks SET tsv = to_tsvector(CAST('simple' AS regconfig), 'forged')"
        " WHERE business_id = :b"
    ),
}


@pytest.mark.parametrize("write", sorted(WRITES_TO_TSV))
async def test_a_write_that_sets_tsv_is_refused(
    app_engine: AsyncEngine, business: str, write: str
) -> None:
    await store_one_chunk(app_engine, business, "en", "Our fees are listed below.")
    embedding = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"

    with pytest.raises(DBAPIError) as refused:
        async with business_transaction(app_engine, business) as connection:
            await connection.execute(WRITES_TO_TSV[write], {"b": business, "embedding": embedding})

    assert getattr(refused.value.orig, "sqlstate", None) == GENERATED_ALWAYS
    # Nothing changed: the one stored chunk keeps the tsv Postgres generated.
    assert await chunk_facts(app_engine, business, expected="english", lexeme="fee") == ChunkFacts(
        ts_config="english", tsv_is_expected_config=True, tsv_is_simple=False, matches_lexeme=True
    )


async def test_the_config_name_is_bound_never_spliced_into_the_statement(
    app_engine: AsyncEngine, business: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name with a quote reaches Postgres as one bound value, so the only complaint is that no
    such config exists (42704). Spliced into the SQL text (an f-string, `.replace`), the quote
    would end the literal early: a syntax error (42601) instead. The map never returns such a
    name: this proves the binding, not the map (mutation gap M08)."""
    monkeypatch.setattr(kb_store, "text_search_config", lambda language: "no'such")

    with pytest.raises(DBAPIError) as refused:
        await store_one_chunk(app_engine, business, "en", "Our fees are listed below.")

    assert getattr(refused.value.orig, "sqlstate", None) == UNDEFINED_OBJECT
    assert await counts(app_engine, business) == (0, 0)  # one transaction: no document either
