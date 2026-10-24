"""H1 tests against real Postgres: `vector_search` (guide: docs/learning/m2-retrieval.md).

`vector_search` is [HAND-WRITE], written by Claude at shesha's request (2026-10-22). The module's
strict xfail was removed with it, so these are plain tests (`integration`: real Postgres, CI). Do
not edit a test to make it pass; change the code.

Integration (like tests/kb/test_postgres_store.py): these need real Postgres. CI runs them; locally
`make test-integration` once Docker runs. Without RUN_INTEGRATION=1 every test here is skipped.
The owner (VECTAPILOT_MIGRATION_DATABASE_URL) registers and removes the test businesses, and in one
test searches where RLS cannot help; every other row is written and searched as the app role. The
fixtures are in tests/retrieval/conftest.py and the planting helpers in tests/retrieval/corpus.py
(moved there unchanged in M3 PR 3, shared with the full-text tests).

The question is always `Q`, the first axis, and each chunk's vector has an exact cosine with it
(`tests/retrieval/vectors.py`), so every expected order and score below is worked out by hand.

Every planting is valid with and without migration 0004 (PR 5b, one approved version per source,
decided 2026-10-22): no business ever gets two approved versions of one source id. `plant` checks
it, so a new test cannot break the rule by accident.
"""

from typing import Final

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import integration
from tests.retrieval.corpus import (
    BUSINESS_SETTING,
    CHUNKS_OF,
    LINGUISTIC,
    MODEL,
    OTHER_MODEL,
    Row,
    keys,
    plant,
    ranked_id,
    scores,
    sorts_first_by_default,
)
from tests.retrieval.vectors import Q, toward
from vectapilot_api.db import business_transaction
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.types import MAX_K, Reader, SearchHit, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

pytestmark = [
    integration,
    pytest.mark.anyio,
]


def question(
    business_id: str, *, k: int = MAX_K, reader: Reader = "customer", model: str = MODEL
) -> VectorQuery:
    return VectorQuery(
        business_id=business_id, embedding=Q, embedding_model=model, k=k, reader=reader
    )


async def search(engine: AsyncEngine, query: VectorQuery) -> list[SearchHit]:
    """`vector_search` inside a business transaction for the query's own business, which must still
    be open, for that business, when it returns."""
    async with business_transaction(engine, query.business_id) as connection:
        hits = await vector_search(connection, query)
        assert await connection.scalar(BUSINESS_SETTING) == query.business_id, (
            "vector_search ended the caller's business transaction (a commit or rollback?)"
        )
        return hits


# ── Order, scores and k ─────────────────────────────────────────────────────────────────────

SIX: Final = (  # planted in this order: neither by score nor by name
    Row("faq-quarter", 0, toward(0.25)),
    Row("faq-same", 0, toward(1.0)),
    Row("faq-opposite", 0, toward(-1.0)),
    Row("faq-half", 0, toward(0.5)),
    Row("faq-right-angle", 0, toward(0.0)),
    Row("faq-three-quarters", 0, toward(0.75)),
)
SIX_BEST_FIRST: Final = [  # by score; alphabetical would start with faq-half
    (("faq-same", "1", 0), 1.0),
    (("faq-three-quarters", "1", 0), 0.75),
    (("faq-half", "1", 0), 0.5),
    (("faq-quarter", "1", 0), 0.25),
    (("faq-right-angle", "1", 0), 0.0),
    (("faq-opposite", "1", 0), -1.0),  # cosine distance 2: still a candidate, ranked last
]


@pytest.mark.parametrize(
    "k", [1, 4, 6, MAX_K], ids=["k=1", "k=4", "k=6-all", "k=max_k-more-than-there-are"]
)
async def test_the_k_nearest_come_back_best_first_with_their_cosine_scores(
    app_engine: AsyncEngine, businesses: tuple[str, str], k: int
) -> None:
    a, _ = businesses
    await plant(app_engine, a, SIX)

    hits = await search(app_engine, question(a, k=k))

    expected = SIX_BEST_FIRST[:k]  # k = MAX_K: all six, no padding, no error
    assert keys(hits) == [key for key, _ in expected]
    # Exact in float32 (see vectors.py); the tolerance only absorbs how a formula is written.
    assert scores(hits) == pytest.approx([score for _, score in expected], abs=1e-6)


async def test_ties_are_broken_by_source_id_byte_by_byte_then_chunk_index(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    assert await sorts_first_by_default(app_engine, "a-faq", "B-faq"), LINGUISTIC
    # Ids fixed out of order: a chunks.id or document_id tie-break, asc or desc, fails every run.
    await plant(
        app_engine,
        a,
        [  # five chunks with the same vector (score 0.5), and one nearer
            Row("a-faq", 10, toward(0.5)),
            Row("B-faq", 0, toward(0.5)),
            Row("c-faq", 1, toward(0.5)),
            Row("a-faq", 2, toward(0.5)),
            Row("z-faq", 5, toward(1.0)),
            Row("a-faq", 0, toward(0.5)),
        ],
        document_ids={  # expected order B, a, c: ranks 2, 1, 3
            ("B-faq", "1"): ranked_id(2),
            ("a-faq", "1"): ranked_id(1),
            ("c-faq", "1"): ranked_id(3),
            ("z-faq", "1"): ranked_id(4),
        },
        chunk_ids={  # expected order B0, a0, a2, a10, c1: ranks 4, 2, 1, 3, 5
            ("B-faq", "1", 0): ranked_id(4),
            ("a-faq", "1", 0): ranked_id(2),
            ("a-faq", "1", 2): ranked_id(1),
            ("a-faq", "1", 10): ranked_id(3),
            ("c-faq", "1", 1): ranked_id(5),
        },
    )

    hits = await search(app_engine, question(a))

    assert keys(hits) == [
        ("z-faq", "1", 5),  # the score decides first, whatever the name
        ("B-faq", "1", 0),  # byte order: "B" is 0x42, "a" is 0x61 (en_US would put a-faq first)
        ("a-faq", "1", 0),
        ("a-faq", "1", 2),  # chunk_index is an integer: 2 before 10
        ("a-faq", "1", 10),
        ("c-faq", "1", 1),  # the source id before chunk_index: c-faq's 1 after a-faq's 10
    ]
    assert scores(hits) == pytest.approx([1.0, 0.5, 0.5, 0.5, 0.5, 0.5], abs=1e-6)


async def test_only_the_approved_version_of_a_source_is_a_candidate(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # One approved version per source (decided 2026-10-22; migration 0004, PR 5b): approving V3
    # retired v2, and v4 is a draft. v2 is nearer than V3 and shares V3's unchanged chunk 0, v4 is
    # nearer too: neither may come back, so the status filter is the version filter. The three
    # approved sources tie at 0.5, so the source id decides, byte by byte.
    a, _ = businesses
    assert await sorts_first_by_default(app_engine, "fees", "Hours"), LINGUISTIC
    # Ids fixed out of order: a document_id or chunks.id tie-break, asc or desc, fails every run.
    ids = await plant(
        app_engine,
        a,
        [
            Row("prices", 0, toward(0.5), version="v2", status="retired"),  # same as V3's chunk 0
            Row("prices", 1, toward(1.0), version="v2", status="retired"),  # dropped from V3
            Row("prices", 0, toward(0.5), version="V3"),  # the approved version
            Row("fees", 0, toward(0.5), version="2026-10"),
            Row("prices", 0, toward(0.75), version="v4", status="draft"),  # not approved yet
            Row("Hours", 0, toward(0.5)),
        ],
        document_ids={  # expected order Hours, fees, prices: ranks 2, 1, 3
            ("Hours", "1"): ranked_id(2),
            ("fees", "2026-10"): ranked_id(1),
            ("prices", "V3"): ranked_id(3),
        },
        chunk_ids={  # expected order Hours, fees, prices: ranks 3, 1, 2
            ("Hours", "1", 0): ranked_id(3),
            ("fees", "2026-10", 0): ranked_id(1),
            ("prices", "V3", 0): ranked_id(2),
        },
    )

    hits = await search(app_engine, question(a))

    expected = [
        ("Hours", "1", 0),  # byte order: "H" is 0x48, "f" is 0x66 (en_US would put fees first)
        ("fees", "2026-10", 0),
        ("prices", "V3", 0),  # V3's own chunk, not retired v2's identical one
    ]
    assert keys(hits) == expected
    assert [hit.chunk_id for hit in hits] == [ids.chunks[key] for key in expected]
    assert scores(hits) == pytest.approx([0.5, 0.5, 0.5], abs=1e-6)


# ── One business only: in the SQL, not only through RLS ─────────────────────────────────────

MINE: Final = (Row("a-hours", 0, toward(0.5)),)
THEIRS: Final = (Row("b-hours", 0, toward(1.0)), Row("b-fees", 0, toward(0.75)))  # both nearer


async def test_another_businesss_nearer_chunks_are_never_returned(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, b = businesses
    await plant(app_engine, a, MINE)
    await plant(app_engine, b, THEIRS)

    as_a = await search(app_engine, question(a, reader="staff"))
    as_b = await search(app_engine, question(b, reader="staff"))

    assert keys(as_a) == [("a-hours", "1", 0)]
    assert keys(as_b) == [("b-hours", "1", 0), ("b-fees", "1", 0)]


@pytest.mark.parametrize("k", [1, MAX_K], ids=["k=1", "k=max_k"])
async def test_the_business_filter_is_in_the_sql_even_where_rls_cannot_help(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, businesses: tuple[str, str], k: int
) -> None:
    # The owner is the bootstrap superuser locally and in CI (ADR 0009, "Limit"): RLS never
    # applies to it, FORCE or not. On its connection only the WHERE clause can keep b's rows out.
    # With k = 1, b's two nearer chunks would fill a LIMIT taken before the business filter.
    a, b = businesses
    await plant(app_engine, a, MINE)
    await plant(app_engine, b, THEIRS)

    async with business_transaction(owner_engine, a) as connection:
        seen = await connection.scalar(CHUNKS_OF, {"b": b})
        assert seen == len(THEIRS), "premise: this connection must see b's chunks (RLS skipped)"
        hits = await vector_search(connection, question(a, k=k, reader="staff"))

    assert keys(hits) == [("a-hours", "1", 0)]


async def test_a_transaction_for_another_business_finds_nothing(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # RLS shows only b's rows here and the query asks for a's: both must hold, so nothing does.
    # Without the WHERE clause, b's chunks would come back labelled as a's search.
    a, b = businesses
    await plant(app_engine, a, MINE)
    await plant(app_engine, b, THEIRS)

    async with business_transaction(app_engine, b) as connection:
        hits = await vector_search(connection, question(a, reader="staff"))

    assert hits == []


# ── Which chunks are candidates ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [(MODEL, [("same-model", "1", 0)]), (OTHER_MODEL, [("other-model", "1", 0)])],
    ids=["model-a", "model-b"],
)
async def test_only_chunks_of_the_querys_embedding_model_are_candidates(
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
    model: str,
    expected: list[tuple[str, str, int]],
) -> None:
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            Row("same-model", 0, toward(0.5)),
            Row("other-model", 0, toward(1.0), model=OTHER_MODEL),  # nearer, other space
        ],
    )

    hits = await search(app_engine, question(a, model=model))

    assert keys(hits) == expected
    assert {hit.embedding_model for hit in hits} == {model}


@pytest.mark.parametrize("reader", ["customer", "staff"])
async def test_draft_and_retired_documents_are_never_returned(
    app_engine: AsyncEngine, businesses: tuple[str, str], reader: Reader
) -> None:
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            Row("approved", 0, toward(0.25)),
            Row("draft", 0, toward(1.0), status="draft"),
            Row("retired", 0, toward(0.75), status="retired"),
            Row("draft-staff", 0, toward(0.5), status="draft", audience="staff_only"),
        ],
    )

    hits = await search(app_engine, question(a, reader=reader))

    assert keys(hits) == [("approved", "1", 0)]


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        ("customer", [("public-faq", "1", 0)]),
        ("staff", [("staff-notes", "1", 0), ("public-faq", "1", 0)]),  # the positive control
    ],
)
async def test_staff_only_documents_reach_staff_but_never_a_customer(
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
    reader: Reader,
    expected: list[tuple[str, str, int]],
) -> None:
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            Row("public-faq", 0, toward(0.5)),
            Row("staff-notes", 0, toward(1.0), audience="staff_only"),
        ],
    )

    hits = await search(app_engine, question(a, reader=reader))

    assert keys(hits) == expected


BEFORE_THE_LIMIT: Final = (  # for a customer, each of the first three fails exactly one filter
    Row("draft-faq", 0, toward(1.0), status="draft"),
    Row("staff-notes", 0, toward(0.75), audience="staff_only"),
    Row("other-model-faq", 0, toward(1.0), model=OTHER_MODEL),
    Row("public-faq", 0, toward(0.5)),
    Row("public-prices", 0, toward(0.25)),
)


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, [("public-faq", "1", 0)]),
        (2, [("public-faq", "1", 0), ("public-prices", "1", 0)]),
    ],
    ids=["k=1", "k=2"],
)
async def test_every_filter_applies_before_the_limit(
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
    k: int,
    expected: list[tuple[str, str, int]],
) -> None:
    # Three nearer chunks that do not qualify: a LIMIT taken before any one of the status,
    # audience or model filters (in a subquery, or by filtering in Python afterwards) lets them
    # fill the window, and fewer than k hits come back.
    a, _ = businesses
    await plant(app_engine, a, BEFORE_THE_LIMIT)

    hits = await search(app_engine, question(a, k=k, reader="customer"))

    assert keys(hits) == expected


@pytest.mark.parametrize(
    "mine",
    [(), (Row("draft", 0, toward(1.0), status="draft"),)],
    ids=["empty-knowledge-base", "nothing-qualifies"],
)
async def test_no_candidates_means_an_empty_list(
    app_engine: AsyncEngine, businesses: tuple[str, str], mine: tuple[Row, ...]
) -> None:
    a, b = businesses
    await plant(app_engine, a, mine)
    await plant(app_engine, b, THEIRS)  # the other business has chunks: none of them may appear

    assert await search(app_engine, question(a, reader="staff")) == []


# ── What a hit carries ──────────────────────────────────────────────────────────────────────


async def test_each_hit_carries_what_a_citation_needs(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    row = Row(  # not Row's defaults ("en", "faq"): a constant in the SELECT fails here
        "opening-hours",
        3,
        toward(1.0),
        version="2026-10",
        page=2,
        language="hi",
        content_kind="pricing",
    )
    ids = await plant(app_engine, a, [row])

    (found,) = await search(app_engine, question(a))

    assert found.model_dump(exclude={"score"}) == {
        "chunk_id": ids.chunks[row.key],
        "document_id": ids.documents[("opening-hours", "2026-10")],
        "source_id": "opening-hours",
        "version": "2026-10",
        "title": "Title of opening-hours v2026-10",
        "language": "hi",
        "content_kind": ContentKind.PRICING,
        "page": 2,
        "chunk_index": 3,
        "content": "Chunk 3 of opening-hours v2026-10",
        "embedding_model": MODEL,
    }
    assert found.score == pytest.approx(1.0, abs=1e-6)
