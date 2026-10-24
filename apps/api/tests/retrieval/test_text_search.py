"""H1 (M3) tests against real Postgres: `text_search` (guide: docs/learning/m3-hybrid.md).

`text_search` is [HAND-WRITE], written by shesha (D5, decided 2026-10-24). Until then it raises
NotImplementedError, so this module is a strict xfail on it: "xfailed" while it is a stub, a normal
failure for a wrong answer, "XPASS(strict)" (a failure on purpose) once a case passes. Do not edit
a test to make it pass; change the code. The mark comes from the one switch,
`pending_hand_write("text_search")` (tests/canary/read_paths.py): removing "text_search" from
`PENDING_HAND_WRITE` turns this module and the canary suite's `text_search` checks on together.

Integration (like test_vector_search.py): these need real Postgres. CI runs them; locally
`make test-integration`. Without RUN_INTEGRATION=1 every test here is skipped. The fixtures are in
tests/retrieval/conftest.py, the planting helpers in tests/retrieval/corpus.py. Rows are written
and searched as the app role, except where the owner searches because RLS cannot help there.

**A hand-checkable mini corpus.** Every chunk is a few plain words, and each word's lexeme was
checked with `to_tsvector` on the local stack (PostgreSQL 18.6, 2026-10-24). Where a test compares
ranks, the chunks compared have four lexemes each, distinct unless a word is repeated on purpose
(the comment says so); only the length test compares a short chunk with a long one, on purpose.
A chunk's config comes from its language:
`en` → `english` (stems, drops stop words), `te` → `simple` (keeps every word as it is), `hi` →
`hindi` (Latin-letter words go to the English stemmer). How `ts_rank` (two-argument form) orders
chunks of one config, which is all these tests need: each distinct question lexeme a chunk holds
once adds the same amount, and the sum is divided by the number of distinct question lexemes.
So more matched lexemes rank higher, and chunks that match the same number tie exactly, wherever
in the chunk the lexemes stand. A lexeme held more than once adds a little more, but all its
repeats together add less than one more distinct lexeme.

The question's vector is always `Q`, and each chunk's vector has an exact cosine with it
(`tests/retrieval/vectors.py`). The cosines are chosen against the text order, so ranking or
breaking ties by the cosine fails; the score is still checked to be that cosine.
"""

import logging
import math
from typing import Final

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.read_paths import pending_hand_write
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
from tests.retrieval.vectors import Q, toward, vector
from vectapilot_api.db import business_transaction
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import (
    MAX_K,
    MAX_QUESTION_CHARS,
    Reader,
    SearchHit,
    TextQuery,
    VectorQuery,
)
from vectapilot_api.retrieval.vector import vector_search

# A strict xfail while "text_search" is in PENDING_HAND_WRITE: one switch with the canary paths.
pytestmark = [integration, pytest.mark.anyio, *pending_hand_write("text_search")]


def question(
    business_id: str,
    text: str,
    *,
    k: int = MAX_K,
    reader: Reader = "customer",
    model: str = MODEL,
) -> TextQuery:
    return TextQuery(
        business_id=business_id,
        question=text,
        embedding=Q,
        embedding_model=model,
        k=k,
        reader=reader,
    )


async def search(engine: AsyncEngine, query: TextQuery) -> list[SearchHit]:
    """`text_search` inside a business transaction for the query's own business, which must still
    be open, for that business, when it returns."""
    async with business_transaction(engine, query.business_id) as connection:
        hits = await text_search(connection, query)
        assert await connection.scalar(BUSINESS_SETTING) == query.business_id, (
            "text_search ended the caller's business transaction (a commit or rollback?)"
        )
        return hits


# ── Matching, rank, ties ────────────────────────────────────────────────────────────────────

RANKED_QUESTION: Final = "Do you treat an axolotl, a quokka or a zebrafish?"
"""english: axolotl, quokka, treat, zebrafish (do, you, an, a, or are stop words)."""
RANKED: Final = (  # planted in this order: neither by rank nor by name
    Row("one-b", 0, toward(1.0), body="zebrafish food delivered weekly"),  # 1 of 4
    Row("none", 0, toward(1.0), body="parrot cages cleaned weekly"),  # 0: never a hit
    Row("three", 0, toward(0.25), body="axolotl quokka zebrafish care"),  # 3 of 4
    Row("one-a", 0, toward(0.75), body="axolotl water changed daily"),  # 1 of 4
    Row("two", 0, toward(0.5), body="axolotl quokka tanks cleaned"),  # 2 of 4
)
RANKED_BEST_FIRST: Final = [  # by matched lexemes; one-a and one-b tie and go by name
    (("three", "1", 0), 0.25),
    (("two", "1", 0), 0.5),
    (("one-a", "1", 0), 0.75),
    (("one-b", "1", 0), 1.0),  # the best cosine, ranked last: the cosine never orders here
]


@pytest.mark.parametrize(
    "k", [1, 2, 4, MAX_K], ids=["k=1", "k=2", "k=4-all", "k=max_k-more-than-there-are"]
)
async def test_matching_chunks_come_back_by_ts_rank_with_their_cosine_scores(
    app_engine: AsyncEngine, businesses: tuple[str, str], k: int
) -> None:
    a, _ = businesses
    await plant(app_engine, a, RANKED)

    hits = await search(app_engine, question(a, RANKED_QUESTION, k=k))

    expected = RANKED_BEST_FIRST[:k]  # k = MAX_K: all four matches, no padding, no error
    assert keys(hits) == [key for key, _ in expected]
    # Exact in float32 (see vectors.py); the tolerance only absorbs how a formula is written.
    assert scores(hits) == pytest.approx([score for _, score in expected], abs=1e-6)


async def test_ts_rank_ties_are_broken_by_source_id_byte_by_byte_then_chunk_index(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    assert await sorts_first_by_default(app_engine, "a-faq", "B-faq"), LINGUISTIC
    # Ids fixed out of order: a chunks.id or document_id tie-break, asc or desc, fails every run.
    await plant(
        app_engine,
        a,
        [  # five chunks match one lexeme (okapi) and tie; z-faq matches two
            Row("a-faq", 10, toward(1.0), body="okapi enclosure opens early"),
            Row("B-faq", 0, toward(0.25), body="okapi keeper notes posted"),
            Row("c-faq", 1, toward(0.5), body="okapi weight records kept"),
            Row("a-faq", 2, toward(0.0), body="okapi viewing deck closed"),
            Row("z-faq", 5, toward(-0.5), body="okapi narwhal feeding times"),
            Row("a-faq", 0, toward(0.75), body="okapi calf born recently"),
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

    hits = await search(app_engine, question(a, "okapi narwhal"))

    assert keys(hits) == [
        ("z-faq", "1", 5),  # two lexemes: the rank decides first, whatever the name or cosine
        ("B-faq", "1", 0),  # byte order: "B" is 0x42, "a" is 0x61 (en_US would put a-faq first)
        ("a-faq", "1", 0),
        ("a-faq", "1", 2),  # chunk_index is an integer: 2 before 10
        ("a-faq", "1", 10),
        ("c-faq", "1", 1),  # the source id before chunk_index: c-faq's 1 after a-faq's 10
    ]
    # Ordering the tie by cosine would give a10, a0, c1, B0, a2: the cosine is only the score.
    assert scores(hits) == pytest.approx([-0.5, 0.25, 0.75, 0.0, 1.0, 0.5], abs=1e-6)


async def test_two_distinct_lexemes_outrank_one_repeated_and_their_distance_never_counts(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # english; the question "okapi narwhal" has two lexemes, okapi and narwhal (to_tsvector).
    #   a-far:    okapi, pen, open, narwhal   both lexemes once, three words apart
    #   b-near:   okapi, narwhal, pen, open   both lexemes once, side by side
    #   a-repeat: okapi (three times), pen    one lexeme, repeated
    # ts_rank (two-argument form, an OR query): a-far and b-near tie exactly (0.0608 each,
    # PostgreSQL 18.6) and go by name; a-repeat ranks below them (0.0414). Each wrong function
    # gives another first place: `ts_rank_cd` (cover density) ranks a-repeat first (0.3 against
    # 0.2), and ranking against an AND of the lexemes (what `plainto_tsquery` builds) reads their
    # distance and puts b-near first. The name order (a-far, a-repeat, b-near) and the cosines
    # (a-repeat best, a-far worst) give other orders too.
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [  # planted in neither the expected nor the name order
            Row("b-near", 0, toward(0.5), body="okapi narwhal pens open"),
            Row("a-repeat", 0, toward(1.0), body="okapi okapi okapi pens"),
            Row("a-far", 0, toward(0.25), body="okapi pens open narwhal"),
        ],
        # Ids in b-near's favour: a tie broken by chunks.id or document_id ascending puts b-near
        # first.
        document_ids={("b-near", "1"): ranked_id(1), ("a-far", "1"): ranked_id(2)},
        chunk_ids={("b-near", "1", 0): ranked_id(1), ("a-far", "1", 0): ranked_id(2)},
    )

    hits = await search(app_engine, question(a, "okapi narwhal"))

    assert keys(hits) == [("a-far", "1", 0), ("b-near", "1", 0), ("a-repeat", "1", 0)]
    assert scores(hits) == pytest.approx([0.25, 0.5, 1.0], abs=1e-6)


async def test_the_rank_is_never_normalised_by_the_chunks_length(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # english; the question "okapi" has one lexeme, okapi (to_tsvector).
    #   b-short: okapi, pen                        okapi once, 2 lexemes
    #   a-long:  okapi, pen, open, daili, earli    okapi once, 5 lexemes
    # ts_rank in its two-argument form never reads a chunk's length: both 0.0608 (PostgreSQL
    # 18.6), an exact tie, so the name decides and a-long comes first. A normalisation flag that
    # divides by the length ranks the shorter chunk higher and puts b-short first: flag 1 (1 + the
    # log of the length) gives 0.0384 against 0.0235, flag 2 (the length) 0.0304 against 0.0122,
    # and 8 and 16 (the number of unique words, and 1 + its log) give the same values here. The
    # cosines put b-short first too.
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            Row("b-short", 0, toward(0.5), body="okapi pens"),
            Row("a-long", 0, toward(0.25), body="okapi pens open daily early"),
        ],
        # Ids in b-short's favour: a tie broken by chunks.id or document_id ascending puts b-short
        # first.
        document_ids={("b-short", "1"): ranked_id(1), ("a-long", "1"): ranked_id(2)},
        chunk_ids={("b-short", "1", 0): ranked_id(1), ("a-long", "1", 0): ranked_id(2)},
    )

    hits = await search(app_engine, question(a, "okapi"))

    assert keys(hits) == [("a-long", "1", 0), ("b-short", "1", 0)]
    assert scores(hits) == pytest.approx([0.25, 0.5], abs=1e-6)


async def test_only_the_approved_version_of_a_source_is_a_candidate(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # As in M2's vector tests: approving V3 retired v2, and v4 is a draft. v2's chunk 1 and v4
    # hold "albatross" more often (a higher rank), and v2's chunk 0 has V3's chunk 0's exact text:
    # none may come back, so the status filter is the version filter. The three approved sources
    # tie (one albatross each), so the source id decides, byte by byte.
    a, _ = businesses
    assert await sorts_first_by_default(app_engine, "fees", "Hours"), LINGUISTIC
    ids = await plant(
        app_engine,
        a,
        [
            Row(
                "prices",
                0,
                toward(0.5),
                version="v2",
                status="retired",
                body="albatross tours booked online",
            ),  # same text as V3's chunk 0
            Row(
                "prices",
                1,
                toward(1.0),
                version="v2",
                status="retired",
                body="albatross albatross albatross tours",
            ),  # dropped from V3
            Row("prices", 0, toward(0.5), version="V3", body="albatross tours booked online"),
            Row("fees", 0, toward(0.5), version="2026-10", body="albatross photo fee waived"),
            Row(
                "prices",
                0,
                toward(0.75),
                version="v4",
                status="draft",
                body="albatross albatross tours online",
            ),  # not approved yet
            Row("Hours", 0, toward(0.5), body="albatross colony visits daily"),
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

    hits = await search(app_engine, question(a, "albatross"))

    expected = [
        ("Hours", "1", 0),  # byte order: "H" is 0x48, "f" is 0x66 (en_US would put fees first)
        ("fees", "2026-10", 0),
        ("prices", "V3", 0),  # V3's own chunk, not retired v2's identical one
    ]
    assert keys(hits) == expected
    assert [hit.chunk_id for hit in hits] == [ids.chunks[key] for key in expected]


CONFIGS_QUESTION: Final = "Opening of the clinic"
"""english (and hindi): open, clinic. simple: opening, of, the, clinic."""


async def test_the_question_is_parsed_with_each_chunks_own_config(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # D2: the question meets each chunk's lexemes in that chunk's own config.
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            # english: door, open, daili, nine. Matches "open" (the question's "Opening",
            # stemmed by english); parsed with simple, "opening" would not match "open".
            Row("en-doors", 0, toward(0.5), body="Doors open daily nine"),
            # simple: rest, of, the, menu. Shares only stop words with the question, and simple
            # keeps them (no stop list): a match, as ADR 0014 "Consequences" records. Parsed
            # with english, the question has no "of" or "the" and this would not match.
            Row("te-menu", 0, toward(0.25), language="te", body="Rest of the menu"),
            # simple: open, door, policy, listed. The question's simple lexeme is "opening", so
            # no match; parsed with english ("open") it would match. Catches one config for all.
            Row("te-open", 0, toward(1.0), language="te", body="open door policy listed"),
            # english: room ("most", "of", "the" are english stop words): no match.
            Row("en-stop", 0, toward(0.75), body="Most of the rooms"),
            # hindi: clinic, open, hour, post (Latin letters go to the English stemmer): a match.
            Row("hi-hours", 0, toward(0.0), language="hi", body="Clinic opening hours posted"),
        ],
    )

    hits = await search(app_engine, question(a, CONFIGS_QUESTION))

    # Membership is the point here; the order is the ranking tests' subject.
    assert sorted(keys(hits)) == [("en-doors", "1", 0), ("hi-hours", "1", 0), ("te-menu", "1", 0)]


async def test_a_word_never_matches_a_longer_word_that_begins_with_it(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # english: the chunk is zebrafish, tank, clean, week; the question "zebra" is zebra (both
    # checked with to_tsvector). A lexeme only matches the same lexeme (§2.1): prefix matching
    # (zebra:*) would find zebrafish, and the spec has none.
    a, _ = businesses
    await plant(
        app_engine, a, [Row("zebra-tanks", 0, toward(0.5), body="zebrafish tanks cleaned weekly")]
    )
    found = await search(app_engine, question(a, "zebrafish"))
    assert keys(found) == [("zebra-tanks", "1", 0)], "premise: its whole word finds the chunk"

    assert await search(app_engine, question(a, "zebra")) == []


# ── One business only: in the SQL, not only through RLS ─────────────────────────────────────

TAPIR: Final = "tapir feeding hours"
"""english: tapir, feed, hour."""
MINE: Final = (Row("a-hours", 0, toward(0.5), body="tapir enclosure map printed"),)  # 1 of 3
THEIRS: Final = (  # both match more of the question: higher ranks
    Row("b-hours", 0, toward(1.0), body="tapir feeding hours listed"),  # 3 of 3
    Row("b-fees", 0, toward(0.75), body="tapir feeding fee charged"),  # 2 of 3
)


async def test_another_businesss_better_matches_are_never_returned(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, b = businesses
    await plant(app_engine, a, MINE)
    await plant(app_engine, b, THEIRS)

    as_a = await search(app_engine, question(a, TAPIR, reader="staff"))
    as_b = await search(app_engine, question(b, TAPIR, reader="staff"))

    assert keys(as_a) == [("a-hours", "1", 0)]
    assert keys(as_b) == [("b-hours", "1", 0), ("b-fees", "1", 0)]


@pytest.mark.parametrize("k", [1, MAX_K], ids=["k=1", "k=max_k"])
async def test_the_business_filter_is_in_the_sql_even_where_rls_cannot_help(
    owner_engine: AsyncEngine, app_engine: AsyncEngine, businesses: tuple[str, str], k: int
) -> None:
    # The owner is the bootstrap superuser locally and in CI (ADR 0009, "Limit"): RLS never
    # applies to it, FORCE or not. On its connection only the WHERE clause can keep b's rows out.
    # With k = 1, b's two better matches would fill a LIMIT taken before the business filter.
    a, b = businesses
    await plant(app_engine, a, MINE)
    await plant(app_engine, b, THEIRS)

    async with business_transaction(owner_engine, a) as connection:
        seen = await connection.scalar(CHUNKS_OF, {"b": b})
        assert seen == len(THEIRS), "premise: this connection must see b's chunks (RLS skipped)"
        hits = await text_search(connection, question(a, TAPIR, k=k, reader="staff"))

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
        hits = await text_search(connection, question(a, TAPIR, reader="staff"))

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
            Row("same-model", 0, toward(0.5), body="lemur house tour times"),
            Row(
                "other-model", 0, toward(1.0), model=OTHER_MODEL, body="lemur lemur house tour"
            ),  # a better match, another vector space
        ],
    )

    hits = await search(app_engine, question(a, "lemur", model=model))

    assert keys(hits) == expected
    assert {hit.embedding_model for hit in hits} == {model}


STATUS_QUESTION: Final = "walrus pelican flamingo albatross toucan"
# One unique keyword each, so each chunk matches on its own: only a filter keeps it out.
STATUS: Final = (
    Row("public-faq", 0, toward(0.25), body="walrus enclosure checks done"),
    Row("staff-notes", 0, toward(0.5), audience="staff_only", body="pelican enclosure checks done"),
    Row("draft-faq", 0, toward(1.0), status="draft", body="flamingo enclosure checks done"),
    Row("retired-faq", 0, toward(0.75), status="retired", body="albatross enclosure checks done"),
    Row(
        "draft-staff",
        0,
        toward(1.0),
        status="draft",
        audience="staff_only",
        body="toucan enclosure checks done",
    ),
)


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        ("customer", [("public-faq", "1", 0)]),
        # The positive control: staff-only material reaches staff (a tie, one keyword each: by
        # name). Draft and retired reach nobody.
        ("staff", [("public-faq", "1", 0), ("staff-notes", "1", 0)]),
    ],
)
async def test_staff_only_draft_and_retired_chunks_never_reach_a_customer(
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
    reader: Reader,
    expected: list[tuple[str, str, int]],
) -> None:
    a, _ = businesses
    await plant(app_engine, a, STATUS)

    hits = await search(app_engine, question(a, STATUS_QUESTION, reader=reader))

    assert keys(hits) == expected


BEFORE_THE_LIMIT: Final = (  # for a customer, each of the first three fails exactly one filter
    Row("draft-faq", 0, toward(1.0), status="draft", body="heron ibis stork nest"),  # 3 of 3
    Row("staff-notes", 0, toward(0.75), audience="staff_only", body="heron ibis stork feed"),
    Row("other-model-faq", 0, toward(1.0), model=OTHER_MODEL, body="heron ibis stork pond"),
    Row("public-faq", 0, toward(0.5), body="heron ibis water tank"),  # 2 of 3
    Row("public-prices", 0, toward(0.25), body="heron price list updated"),  # 1 of 3
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
    # Three better matches that do not qualify: a LIMIT taken before any one of the status,
    # audience or model filters (in a subquery, or by filtering in Python afterwards) lets them
    # fill the window, and fewer than k hits come back.
    a, _ = businesses
    await plant(app_engine, a, BEFORE_THE_LIMIT)

    hits = await search(app_engine, question(a, "heron ibis stork", k=k, reader="customer"))

    assert keys(hits) == expected


@pytest.mark.parametrize(
    "mine",
    [
        (),
        (Row("draft", 0, toward(1.0), status="draft", body="tapir feeding hours listed"),),
        (Row("parrots", 0, toward(1.0), body="parrot cages cleaned weekly"),),
    ],
    ids=["empty-knowledge-base", "nothing-qualifies", "nothing-matches"],
)
async def test_no_candidates_or_no_match_means_an_empty_list(
    app_engine: AsyncEngine, businesses: tuple[str, str], mine: tuple[Row, ...]
) -> None:
    a, b = businesses
    await plant(app_engine, a, mine)
    await plant(app_engine, b, THEIRS)  # the other business matches: none of it may appear

    assert await search(app_engine, question(a, TAPIR, reader="staff")) == []


# ── Hostile input (zara S1): the question is data, never query syntax ───────────────────────

MARKER: Final = "kq4hostile"
"""In every hostile question and in no chunk: if it shows in a log line or an error, the question
leaked there. It is a lexeme in every config, and matches nothing."""
HOSTILE_CORPUS: Final = (
    Row("zebra-tanks", 0, toward(0.5), body="zebrafish tanks cleaned weekly"),
    Row("okapi-pen", 0, toward(0.25), body="okapi enclosure opens daily"),
    Row("te-zebra", 0, toward(0.75), language="te", body="zebrafish feeding notes kept"),
    Row("parrots", 0, toward(1.0), body="parrot cages cleaned weekly"),  # never matches
)
ZEBRA: Final = ("zebra-tanks", "1", 0)
OKAPI: Final = ("okapi-pen", "1", 0)
TE_ZEBRA: Final = ("te-zebra", "1", 0)
_UNIT: Final = "& | ! ( ) :* <-> ' \\ zebrafish "
LONGEST: Final = (_UNIT * (MAX_QUESTION_CHARS // len(_UNIT) + 1))[
    : MAX_QUESTION_CHARS - len(MARKER) - 1
] + f" {MARKER}"
"""Exactly `MAX_QUESTION_CHARS` characters of operators around "zebrafish"."""
HOSTILE: Final = [
    # Only operators (and the marker): no lexeme matches, in any config.
    pytest.param(f"& | ! ( ) :* <-> ' \\ {MARKER}", [], id="only-operators"),
    # Operators around words: the words still count, the operators never do (no NOT, no prefix,
    # no phrase), so all three chunks with zebrafish or okapi match.
    pytest.param(
        f"!(zebrafish:* <-> 'okapi') & | \\ {MARKER}",
        [OKAPI, TE_ZEBRA, ZEBRA],
        id="operators-around-words",
    ),
    # A URL: to_tsvector keeps it as one token, so its lexemes themselves hold "(", "&" and
    # ":*". Joining the lexemes into tsquery text as they are, unquoted, and parsing that again
    # raises a syntax error that quotes the text, with to_tsquery and with a cast to tsquery
    # alike (PostgreSQL 18.6).
    pytest.param(f"http://x.example/a?q=(x&y=2:* zebrafish {MARKER}", [TE_ZEBRA, ZEBRA], id="url"),
    # A URL lexeme can hold a quote too: to_tsvector gives x.example/a'b and /a'b here, in every
    # config. Quoting each lexeme but leaving its own quote as it is makes the same syntax error.
    pytest.param(
        f"http://x.example/a'b zebrafish {MARKER}", [TE_ZEBRA, ZEBRA], id="url-with-quote"
    ),
    # Quotes and backslashes: the tsquery quoting characters.
    pytest.param(
        f"it's o'clock: \\'zebrafish\\' ''okapi'' {MARKER}",
        [OKAPI, TE_ZEBRA, ZEBRA],
        id="quotes-and-backslashes",
    ),
    # websearch_to_tsquery's syntax: "-" would negate and quotes would make a phrase; here they
    # are punctuation, and "OR" is a stop word in english, a plain word in simple.
    pytest.param(
        f'-zebrafish OR "okapi" {MARKER}', [OKAPI, TE_ZEBRA, ZEBRA], id="websearch-syntax"
    ),
    pytest.param(LONGEST, [TE_ZEBRA, ZEBRA], id="2000-characters"),
]


@pytest.mark.parametrize(("hostile", "expected"), HOSTILE)
async def test_a_hostile_question_never_raises_and_never_reaches_a_log_line(
    app_engine: AsyncEngine,
    businesses: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
    hostile: str,
    expected: list[tuple[str, str, int]],
) -> None:
    a, _ = businesses
    await plant(app_engine, a, HOSTILE_CORPUS)
    query = question(a, hostile)
    assert query.question == hostile, "premise: TextQuery accepts it unchanged"
    caplog.set_level(logging.DEBUG)

    try:
        hits = await search(app_engine, query)
    except NotImplementedError:
        raise
    except Exception as error:  # report the type only: the message may quote the question
        quoted = MARKER in str(error)
        pytest.fail(
            f"text_search raised {type(error).__name__} (the question in its message: {quoted})",
            pytrace=False,
        )

    assert MARKER not in caplog.text, "the question reached a log line"
    assert sorted(keys(hits)) == expected


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
        language="hi",  # hindi: open, hour, price, clinic
        content_kind="pricing",
        body="Opening hours and prices for the clinic",
    )
    ids = await plant(app_engine, a, [row])

    (found,) = await search(app_engine, question(a, "opening hours"))

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
        "content": "Opening hours and prices for the clinic",
        "embedding_model": MODEL,
    }
    assert found.score == pytest.approx(1.0, abs=1e-6)


async def test_a_chunk_both_legs_find_is_the_same_search_hit(
    app_engine: AsyncEngine, businesses: tuple[str, str]
) -> None:
    # rrf_fuse refuses two different hits with one chunk id, so the full-text leg must build
    # exactly the hit the vector leg builds: the same fields and the same float score, written as
    # vector_search writes it. (0.7, 0.1, √0.5) has length 1 in exact arithmetic but not in
    # float32, as real embeddings are only nearly unit: `1 - <=>` divides by the stored length and
    # gives 0.70000000894..., a negated `<#>` (the dot product) gives 0.69999998807... (checked on
    # PostgreSQL 18.6 with pgvector 0.8.7).
    a, _ = businesses
    await plant(
        app_engine,
        a,
        [
            Row("hours", 0, vector(0.6, 0.8), body="tapir feeding hours listed"),  # 0.36 + 0.64
            Row("fees", 0, vector(0.7, 0.1, math.sqrt(0.5)), body="tapir feeding fee charged"),
            Row("map", 0, toward(0.25), body="tapir enclosure map printed"),
            Row("parrots", 0, toward(1.0), body="parrot cages cleaned weekly"),  # vector leg only
        ],
    )
    text_query = question(a, TAPIR)
    vector_query = VectorQuery(
        business_id=a, embedding=Q, embedding_model=MODEL, k=MAX_K, reader="customer"
    )

    async with business_transaction(app_engine, a) as connection:
        by_vector = {hit.chunk_id: hit for hit in await vector_search(connection, vector_query)}
        by_text = await text_search(connection, text_query)

    assert keys(by_text) == [("hours", "1", 0), ("fees", "1", 0), ("map", "1", 0)]
    assert len(by_vector) == 4, "premise: the vector leg returns every candidate"
    for hit in by_text:
        assert hit == by_vector[hit.chunk_id], hit.source_id
