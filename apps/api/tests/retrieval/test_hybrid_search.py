"""M3 PR 4: hybrid `SearchService` and `GET /v1/kb/search` in hybrid mode, without Postgres.

Both legs and the fusion are team-side stand-ins (`tests/retrieval/standins.py`): M3 H1's
`text_search` and M3 H2's `rrf_fuse` are shesha's [HAND-WRITE]s and still stubs, and the stand-ins
are deliberately not them (prepared hits, no matching, no RRF arithmetic). Everything around them
is real: the embedding runner and its record, the snapshot request, the model check, the order of
the steps, the stats, the route. The real legs against Postgres: `test_hybrid_snapshot.py` and the
canary suite.

Small, hand-checkable lists: `V1`, `V2` come from the vector leg, `T1` from the full-text leg
only, and `BOTH` from both (one chunk, one `SearchHit`, as both legs return it).
"""

import inspect
import logging
import time
from dataclasses import asdict
from fractions import Fraction
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from tests.answer.apps import A, B, get_search, serve
from tests.conftest import make_settings
from tests.retrieval.standins import (
    HYBRID,
    Connection,
    StandInFuse,
    TickingClock,
    build_hybrid_search,
    build_search,
    fused,
    hit,
)
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.embeddings.guard import ReindexRequired
from vectapilot_api.embeddings.runner import EMBEDDING_CALL
from vectapilot_api.log import JsonFormatter
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.fusion import RRF_K, rrf_fuse
from vectapilot_api.retrieval.search import (
    RetrievalConfig,
    SearchService,
    SearchStats,
    fts_only,
    fused_search,
    new_trace_id,
)
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import (
    CANDIDATE_K,
    MAX_K,
    NUL,
    Question,
    TextQuery,
    check_question,
)
from vectapilot_api.retrieval.vector import vector_search

QUESTION: Final = "Do you open on Sundays?"
CANARY: Final = "canary-hybrid-7c4e"
"""In questions, chunk texts and titles: never in a log line or an error body."""

V1 = hit("We open at 9 on weekdays.", 0.81, source_id="hours", chunk_index=0)
V2 = hit("Parking is free.", 0.52, source_id="parking")
BOTH = hit("On Sundays we open at 10.", 0.77, source_id="hours", chunk_index=1)
T1 = hit("Sunday Sunday Sunday open open.", 0.08, source_id="stuffed")
"""Keyword-stuffed: the full-text leg ranks it first, its cosine is low."""

VECTOR_LIST: Final = [V1, BOTH, V2]
TEXT_LIST: Final = [T1, BOTH]


def search_logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, EMBEDDING_CALL) for r in caplog.records if r.getMessage() == EMBEDDING_CALL]


# ── Settings and the config ───────────────────────────────────────────────────────────────


def test_the_retrieval_settings_are_the_provisional_platform_defaults() -> None:
    # Unmeasured starting values (config.py): vector until D15's rule says otherwise.
    fields = Settings.model_fields
    assert fields["rag_retrieval_mode"].default == "vector"
    assert fields["rag_candidate_k"].default == CANDIDATE_K == 20
    assert fields["rag_rrf_k"].default == RRF_K == 60
    assert RetrievalConfig.from_settings(make_settings()) == RetrievalConfig()
    assert RetrievalConfig() == RetrievalConfig(mode="vector", candidate_k=20, rrf_k=60)
    # The stand-ins' hybrid config is the platform default in hybrid mode.
    assert RetrievalConfig.from_settings(make_settings(rag_retrieval_mode="hybrid")) == HYBRID


@pytest.mark.parametrize(
    "bad",
    [
        {"rag_retrieval_mode": "bm25"},
        {"rag_candidate_k": 9},
        {"rag_candidate_k": MAX_K + 1},
        {"rag_candidate_k": 10, "rag_context_k": 11},
        {"rag_rrf_k": 0},
    ],
    ids=["unknown mode", "below 10", "above MAX_K", "below rag_context_k", "rrf k 0"],
)
def test_out_of_range_retrieval_settings_stop_startup(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as raised:
        make_settings(**bad)

    # Fail fast, naming fields and rules, never a value.
    assert "bm25" not in str(raised.value)


def test_the_candidate_k_rule_names_both_settings_in_vector_mode_too() -> None:
    with pytest.raises(ValidationError, match="rag_candidate_k must be >= rag_context_k"):
        make_settings(rag_context_k=30, rag_candidate_k=20, rag_retrieval_mode="vector")
    # At the boundaries it starts.
    assert make_settings(rag_context_k=20, rag_candidate_k=20).rag_candidate_k == 20
    assert make_settings(rag_context_k=5, rag_candidate_k=MAX_K).rag_candidate_k == MAX_K


@pytest.mark.parametrize(
    "fields",
    [{"mode": "bm25"}, {"candidate_k": 0}, {"candidate_k": MAX_K + 1}, {"rrf_k": 0}],
)
def test_a_retrieval_config_out_of_range_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetrievalConfig(**fields)


def test_the_app_builds_its_search_in_the_configured_mode() -> None:
    for mode in ("vector", "hybrid"):
        app = create_app(make_settings(rag_retrieval_mode=mode, rag_candidate_k=30, rag_rrf_k=7))
        with TestClient(app):
            service = app.state.search_service
            assert service.retrieval == RetrievalConfig(mode=mode, candidate_k=30, rrf_k=7)
            # The real legs and fusion (the [HAND-WRITE] stubs until H1 and H2 land).
            assert service.vector_search is vector_search
            assert service.text_search is text_search
            assert service.fuse is rrf_fuse


# ── NUL (decided 2026-10-24): refused at the edge, in every mode ─────────────────────────────


@pytest.mark.parametrize("value", [NUL, f"{CANARY}{NUL}", f"Sunday{NUL}hours", f" {NUL} "])
def test_a_question_with_a_nul_character_is_refused_without_echoing_it(value: str) -> None:
    with pytest.raises(ValueError) as raised:
        check_question(value)
    with pytest.raises(ValidationError):
        TypeAdapter(Question).validate_python(value)

    # The message names the rule, never the value (the routes' 422s: test_hybrid_answer.py).
    assert "NUL" in str(raised.value) and CANARY not in str(raised.value)


# ── Vector mode: M2's path, nothing hybrid runs ────────────────────────────────────────────


@pytest.mark.anyio
async def test_vector_mode_runs_m2s_path_and_never_the_text_leg_or_the_fusion() -> None:
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST}, retrieval=RetrievalConfig())
    stats = SearchStats()

    result = await hybrid.service.search(
        A, QUESTION, k=2, reader="customer", trace_id=new_trace_id(), stats=stats
    )

    assert list(result.hits) == [V1, BOTH] and result.fused is None
    assert hybrid.database.snapshots == [False]  # READ COMMITTED, as in M2 (ADR 0011, 5)
    assert [q.k for _, q in hybrid.standin.calls] == [2]
    assert hybrid.text.calls == [] and hybrid.fuse.calls == []
    assert stats == SearchStats()  # vector mode leaves it untouched


def test_a_search_service_without_a_config_is_in_vector_mode() -> None:
    assert build_search().service.retrieval == RetrievalConfig(mode="vector")


# ── Hybrid mode: one embedding, one snapshot, the check, both legs, the fusion ───────────


@pytest.mark.anyio
async def test_hybrid_embeds_once_then_checks_and_runs_both_legs_and_the_fusion_in_one_snapshot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST})
    trace_id = new_trace_id()

    result = await hybrid.service.search(A, QUESTION, k=5, reader="customer", trace_id=trace_id)

    # One embedding, as a query, with this request's trace id.
    assert hybrid.embedder.batches == [[QUESTION]]
    [record] = search_logged(caplog)
    assert (record["input_type"], record["trace_id"]) == ("query", trace_id)
    # One business transaction, opened as a snapshot, for this business only.
    assert hybrid.database.opened == [A] and hybrid.database.snapshots == [True]
    [(model_connection, business)] = hybrid.stored.calls
    [(vector_connection, vector_query)] = hybrid.standin.calls
    [(text_connection, text_query)] = hybrid.text.calls
    assert business == A and isinstance(model_connection, Connection)
    assert model_connection is vector_connection is text_connection  # one transaction
    assert model_connection.business_id == A
    # Both legs at candidate_k, with the same business, vector, model and reader.
    assert (vector_query.k, text_query.k) == (CANDIDATE_K, CANDIDATE_K)
    for query in (vector_query, text_query):
        assert (query.business_id, query.reader) == (A, "customer")
        assert query.embedding == fake_vector(QUESTION, 1024)
        assert query.embedding_model == hybrid.embedder.model == result.embedding_model
    assert text_query.question == QUESTION
    # The fusion: [vector list, text list], vector first, with RRF's k and the asked k as limit.
    [(lists, rrf_k, limit)] = hybrid.fuse.calls
    assert lists == [VECTOR_LIST, TEXT_LIST] and (rrf_k, limit) == (60, 5)
    # The hits are the fused list in fused order (the stand-in: first met, vector list first),
    # as plain SearchHits whose score is still each hit's cosine.
    assert list(result.hits) == [V1, BOTH, V2, T1]
    assert result.fused is not None
    assert [item.hit for item in result.fused] == list(result.hits)
    assert [h.score for h in result.hits] == [0.81, 0.77, 0.52, 0.08]


@pytest.mark.anyio
@pytest.mark.parametrize(("k", "legs_k"), [(1, 20), (20, 20), (MAX_K, MAX_K)])
async def test_each_leg_gets_at_least_candidate_k_and_the_fused_list_is_cut_to_k(
    k: int, legs_k: int
) -> None:
    many = [hit(f"Chunk {i}.", 0.9 - i / 100, chunk_index=i) for i in range(MAX_K)]
    hybrid = build_hybrid_search({A: many}, {A: []})
    stats = SearchStats()

    result = await hybrid.service.search(
        A, QUESTION, k=k, reader="customer", trace_id=new_trace_id(), stats=stats
    )

    assert [q.k for _, q in hybrid.standin.calls] == [legs_k]
    assert [q.k for _, q in hybrid.text.calls] == [legs_k]
    # The fusion weighs each leg's whole candidate list; only its output is cut to k (at k=1, a
    # leg cut to k before the fusion would hand it one vector hit instead of twenty).
    assert [lists for lists, _, _ in hybrid.fuse.calls] == [[many[:legs_k], []]]
    assert [limit for _, _, limit in hybrid.fuse.calls] == [k]
    assert list(result.hits) == many[:k]
    # The record's candidate_k is what the legs were asked for: k itself when k > candidate_k.
    assert stats.candidate_k == legs_k


@pytest.mark.anyio
async def test_chunks_of_another_model_mean_reindex_required_and_no_leg_runs() -> None:
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST}, stored={A: frozenset({"old"})})

    with pytest.raises(ReindexRequired):
        await hybrid.service.search(A, QUESTION, k=5, reader="customer", trace_id=new_trace_id())

    assert hybrid.standin.calls == [] and hybrid.text.calls == [] and hybrid.fuse.calls == []


@pytest.mark.anyio
async def test_each_business_gets_its_own_transaction_check_and_legs() -> None:
    theirs = hit(f"B's price list {CANARY}.", 0.99, source_id="b")
    hybrid = build_hybrid_search({A: [V1], B: [theirs]}, {A: [T1], B: [theirs]})

    mine = await hybrid.service.search(A, QUESTION, k=5, reader="customer", trace_id=new_trace_id())

    assert list(mine.hits) == [V1, T1]
    assert hybrid.database.opened == [A]
    assert [b for _, b in hybrid.stored.calls] == [A]
    assert {q.business_id for _, q in hybrid.standin.calls + hybrid.text.calls} == {A}


# ── The stats: counts and durations, metadata only ──────────────────────────────────────────


@pytest.mark.anyio
async def test_the_stats_count_each_list_and_time_each_stage_on_the_monotonic_clock() -> None:
    clock = TickingClock(step=0.002)  # every reading 2 ms after the last
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST}, clock=clock)
    stats = SearchStats()

    await hybrid.service.search(
        A, QUESTION, k=5, reader="customer", trace_id=new_trace_id(), stats=stats
    )

    # Eleven readings in order: embed start and end, the transaction's opening, vector start and
    # end, text start and end, fuse start and end, the fake database's commit, the transaction's
    # end. Each stage's pair is one step (2 ms) apart; the transaction spans eight steps (16 ms),
    # so a transaction_ms read before the commit (seven steps) fails here.
    assert asdict(stats) == {
        "candidate_k": 20,
        "vector_hits": 3,
        "text_hits": 2,
        "fused_hits": 4,
        "fts_only_hits": 1,  # T1: in the text list only
        "embed_ms": pytest.approx(2.0),
        "transaction_ms": pytest.approx(16.0),
        "vector_ms": pytest.approx(2.0),
        "text_ms": pytest.approx(2.0),
        "fuse_ms": pytest.approx(2.0),
    }


@pytest.mark.anyio
async def test_a_search_that_fails_in_the_text_leg_leaves_the_stages_it_finished() -> None:
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST})
    hybrid.text.raises = NotImplementedError("H1")  # what the stub raises
    stats = SearchStats()

    with pytest.raises(NotImplementedError):
        await hybrid.service.search(
            A, QUESTION, k=5, reader="customer", trace_id=new_trace_id(), stats=stats
        )

    assert stats.embed_ms is not None and stats.vector_ms is not None
    assert (stats.candidate_k, stats.vector_hits) == (20, 3)
    assert (stats.text_hits, stats.text_ms, stats.fused_hits, stats.fuse_ms) == (None,) * 4
    assert stats.transaction_ms is None
    assert hybrid.fuse.calls == []


@pytest.mark.anyio
async def test_fts_only_hits_are_counted_in_the_fused_list_not_in_the_text_leg() -> None:
    # The legs: vector [V1, BOTH, V2], text [T1, BOTH]; T1 is in the text leg only. Cut at k=2,
    # the fused list is [V1, BOTH]: T1 did not make it, so no fused hit is FTS-only. Counting
    # over the legs instead would say 1.
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST})
    stats = SearchStats()

    await hybrid.service.search(
        A, QUESTION, k=2, reader="customer", trace_id=new_trace_id(), stats=stats
    )

    assert (stats.vector_hits, stats.text_hits) == (3, 2)
    assert (stats.fused_hits, stats.fts_only_hits) == (2, 0)


def test_every_default_clock_is_the_monotonic_clock() -> None:
    # Stage durations must not jump with the wall clock (NTP, DST): `time.monotonic`, never
    # `time.time`, both where `SearchService` is built without a clock (as the app builds it)
    # and in `fused_search` (as the canary suite's hybrid read path calls it).
    assert inspect.signature(SearchService).parameters["clock"].default is time.monotonic
    assert inspect.signature(fused_search).parameters["clock"].default is time.monotonic
    app = create_app(make_settings(rag_retrieval_mode="hybrid"))
    with TestClient(app):
        assert app.state.search_service._clock is time.monotonic


def test_fts_only_is_the_hits_the_vector_list_does_not_hold() -> None:
    items = [
        fused(V1, Fraction(1, 61), (1, None)),
        fused(BOTH, Fraction(2, 61), (2, 2)),
        fused(T1, Fraction(1, 61), (None, 1)),
    ]
    assert fts_only(items) == {T1.chunk_id}
    assert fts_only([]) == frozenset()


@pytest.mark.anyio
async def test_fused_search_gives_the_vector_leg_the_text_querys_fields_without_the_question() -> (
    None
):
    hybrid = build_hybrid_search({A: VECTOR_LIST}, {A: TEXT_LIST})
    query = TextQuery(
        business_id=A,
        question=QUESTION,
        embedding=fake_vector(QUESTION, 1024),
        embedding_model="fake-embedding-1024",
        k=7,
        reader="staff",
    )
    connection: Any = Connection(A)

    lists = await fused_search(
        connection,
        query,
        limit=2,
        rrf_k=9,
        vector=hybrid.standin,
        text=hybrid.text,
        fuse=hybrid.fuse,
    )

    [(_, vector_query)] = hybrid.standin.calls
    assert vector_query.model_dump() == query.model_dump(exclude={"question"})
    assert (lists.vector, lists.text) == (tuple(VECTOR_LIST), tuple(TEXT_LIST))
    assert [item.hit for item in lists.fused] == [V1, BOTH]  # limit 2
    assert [(rrf_k, limit) for _, rrf_k, limit in hybrid.fuse.calls] == [(9, 2)]


# ── The route in hybrid mode: same shape, score stays the cosine ───────────────────────────


def test_hybrid_search_route_keeps_the_response_shape_and_the_cosine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fusion that ranks the keyword-stuffed chunk first, with RRF scores that look nothing like
    # cosines: the response must show the fused order and each hit's cosine, never an RRF score.
    scripted = StandInFuse(
        fused=[fused(T1, Fraction(2, 61), (None, 1)), fused(V1, Fraction(1, 62), (1, None))]
    )
    hybrid = build_hybrid_search({A: [V1]}, {A: [T1]}, fuse=scripted)
    with serve(settings, monkeypatch, hybrid) as client:
        response = get_search(client, q=QUESTION, k=2)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"trace_id", "embedding_model", "hits"}
    assert [h["chunk_id"] for h in body["hits"]] == [str(T1.chunk_id), str(V1.chunk_id)]
    assert [h["score"] for h in body["hits"]] == [0.08, 0.81]
    assert set(body["hits"][0]) == {
        "chunk_id",
        "document_id",
        "source_id",
        "version",
        "title",
        "page",
        "chunk_index",
        "language",
        "content_kind",
        "content",
        "score",
    }


def test_hybrid_search_route_logs_no_question_or_chunk_text(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    planted = hit(f"chunk {CANARY}", 0.9, title=f"title {CANARY}")
    hybrid = build_hybrid_search({A: [planted]}, {A: [planted]})
    with serve(settings, monkeypatch, hybrid, caplog=caplog) as client:
        caplog.set_level(logging.DEBUG)
        assert get_search(client, q=f"{QUESTION} {CANARY}").status_code == 200

    logged = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert CANARY not in logged
    assert search_logged(caplog)


def test_an_unmapped_error_in_a_hybrid_leg_reaches_the_caller_untyped(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H1's and H2's stubs raise NotImplementedError: the route lets it through (a 500), so the
    # canary's hybrid read paths are strict xfails until both are written.
    for broken in ("text", "fuse"):
        hybrid = build_hybrid_search({A: [V1]}, {A: [T1]})
        getattr(hybrid, broken).raises = NotImplementedError(broken)
        with serve(settings, monkeypatch, hybrid) as client, pytest.raises(NotImplementedError):
            get_search(client, q=QUESTION)
