"""M3 PR 4: `POST /v1/answer` and `AnswerService` in hybrid mode, without Postgres. No source, no
claim.

Both legs and the fusion are team-side stand-ins (`tests/retrieval/standins.py`; M3 H1 and H2 are
shesha's [HAND-WRITE]s). Where a test needs particular RRF scores, the fusion is scripted
(`StandInFuse(fused=[...])`), so the scores are chosen by hand to make a wrong gate visible.
Vector mode is M2's path: test_answer.py runs it unchanged.

Extends test_answer.py's record checks: the `rag_answer` record gains an optional `retrieval` block
in hybrid mode (counts and durations only), keeps `record_version` 1, and M2's records still
validate (ADR 0007 and ADR 0011 amendments).
"""

import ast
import logging
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest

from tests.answer.apps import A, answer_service, post_answer, serve
from tests.answer.providers import CitingProvider, boundary_of, parse_blocks, reply
from tests.retrieval.standins import (
    HybridSearch,
    StandInFuse,
    TickingClock,
    build_hybrid_search,
    fused,
    hit,
)
from vectapilot_api.answer.prompt import PROMPT_VERSION
from vectapilot_api.answer.service import RAG_ANSWER, RagConfig
from vectapilot_api.answer.types import AnswerRun, RagAnswerRecord
from vectapilot_api.config import Settings
from vectapilot_api.gateway.errors import ModelRefused, Transient
from vectapilot_api.gateway.gateway import LLM_CALL
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.log import JsonFormatter
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import RetrievalConfig
from vectapilot_api.retrieval.types import NUL, SearchHit

CANARY: Final = "canary-hybrid-answer-91d3"
"""Planted in questions, chunk texts, titles and model answers: never in a record or a log line."""
QUESTION: Final = "Do you open on Sundays?"

GOOD = hit("On Sundays we open at 10.", 0.8, source_id="hours", title="Opening hours")
STUFFED = hit("Sunday Sunday open open Sunday.", 0.01, source_id="stuffed", title="Stuffed")
"""Keyword-stuffed and approved: the full-text leg ranks it first, its cosine is 0.01."""
RESCUE = hit("Sonntag: 10 Uhr.", 0.6, source_id="hours-de", title="Sunday hours")
"""Only the full-text leg finds it, and its cosine passes the gate: an FTS-only rescue (D7)."""

M2_RECORD: Final[dict[str, Any]] = {
    "record_version": 1,
    "business_id": A,
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "prompt_version": PROMPT_VERSION,
    "outcome": "answered",
    "reason": None,
    "error": None,
    "hits": 4,
    "evidence": 3,
    "cited": 1,
    "top_score": 0.82,
    "min_score": 0.3,
    "model_called": True,
    "latency_ms": 12.5,
}
"""A `rag_answer` record exactly as M2 wrote it (test_answer.py's first test)."""


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[dict[str, Any]]:
    return [getattr(r, message) for r in caplog.records if r.getMessage() == message]


def logged(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(JsonFormatter().format(record) for record in caplog.records)


def ask(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    search: HybridSearch,
    provider: Any,
    *,
    question: str = QUESTION,
    config: RagConfig | None = None,
    caplog: pytest.LogCaptureFixture | None = None,
) -> httpx2.Response:
    service = answer_service(search, provider, config or RagConfig(0.3, 5, "dont_know"))
    with serve(settings, monkeypatch, search, service, caplog=caplog) as client:
        return post_answer(client, {"question": question})


def prompt_ids(provider: CitingProvider) -> list[str | None]:
    [call] = provider.calls
    blocks, _ = parse_blocks(call.messages[-1].content, boundary_of(call))
    return [block.chunk_id for block in blocks]


async def run_answer(search: HybridSearch, provider: Any, config: RagConfig) -> AnswerRun:
    service = answer_service(search, provider, config)
    return await service.answer(A, QUESTION, trace_id=uuid.uuid4().hex)


# ── The evidence gate: a cosine threshold, never a fused score (D7, zara S3) ────────────────


@pytest.mark.parametrize("min_score", [0.3, 0.015], ids=["the 0.3 default", "an RRF-sized 0.015"])
def test_the_evidence_gate_never_compares_a_cosine_threshold_with_a_fused_score(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    min_score: float,
) -> None:
    # The fusion ranks the keyword-stuffed chunk first. Scores chosen by hand so that a gate on
    # the RRF score gets the opposite of a gate on the cosine, at both thresholds:
    #   STUFFED: RRF 1/61 ≈ 0.0164 (text rank 1 only), cosine 0.01;
    #   GOOD:    RRF 1/80 = 0.0125 (vector rank 20 only), cosine 0.8.
    # At 0.015 an RRF gate would admit STUFFED and refuse GOOD; at 0.3 it would refuse both (every
    # answer "I don't know"). The cosine gate admits GOOD alone, at both.
    caplog.set_level(logging.INFO)
    scripted = StandInFuse(
        fused=[fused(STUFFED, Fraction(1, 61), (None, 1)), fused(GOOD, Fraction(1, 80), (20, None))]
    )
    search = build_hybrid_search({A: [GOOD]}, {A: [STUFFED]}, fuse=scripted)
    provider = CitingProvider()  # cites every block its prompt holds

    response = ask(
        settings,
        monkeypatch,
        search,
        provider,
        config=RagConfig(min_score, 5, "dont_know"),
        caplog=caplog,
    )

    body = response.json()
    assert body["outcome"] == "answered"
    assert prompt_ids(provider) == [str(GOOD.chunk_id), None]  # never the stuffed chunk
    assert [s["chunk_id"] for s in body["sources"]] == [str(GOOD.chunk_id)]
    assert [s["score"] for s in body["sources"]] == [0.8]  # the cosine, not 1/80
    assert body["confidence"]["value"] == 0.8  # the best cosine of the hits that went on
    [record] = records(caplog, RAG_ANSWER)
    assert (record["hits"], record["evidence"], record["min_score"]) == (2, 1, min_score)
    assert (record["retrieval"]["fts_only_hits"], record["retrieval"]["fts_only_evidence"]) == (
        1,
        0,
    )


def test_a_keyword_stuffed_chunk_alone_is_never_evidence_and_no_model_is_called(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search({A: []}, {A: [STUFFED]})  # fused: STUFFED, first and only
    provider = FakeProvider()  # an empty script: any call would fail the test

    response = ask(settings, monkeypatch, search, provider, caplog=caplog)

    body = response.json()
    assert (body["outcome"], body["answer"], body["sources"]) == ("dont_know", None, [])
    assert provider.calls == [] and records(caplog, LLM_CALL) == []
    [record] = records(caplog, RAG_ANSWER)
    assert (record["reason"], record["model_called"]) == ("below_threshold", False)


@pytest.mark.anyio
async def test_only_the_top_context_k_fused_hits_go_on_and_the_run_keeps_the_whole_fused_list() -> (
    None
):
    many = [hit(f"Chunk {i}.", 0.9 - i / 100, chunk_index=i) for i in range(8)]
    search = build_hybrid_search({A: many}, {A: []})
    provider = CitingProvider()

    run = await run_answer(search, provider, RagConfig(0.3, 3, "dont_know"))

    # The search was asked for candidate_k (20): each leg at 20, the fused list cut at 20.
    assert [q.k for _, q in search.standin.calls] == [20]
    assert [limit for _, _, limit in search.fuse.calls] == [20]
    # AnswerRun.hits: the whole fused list, in fused order, as plain SearchHits (PR 5's harness
    # will score it); evidence: the top 3 (rag_context_k), all above the threshold.
    assert list(run.hits) == many
    assert all(type(h) is SearchHit for h in run.hits)
    assert list(run.evidence) == many[:3]
    assert prompt_ids(provider) == [str(h.chunk_id) for h in many[:3]] + [None]


@pytest.mark.anyio
async def test_a_good_hit_below_the_top_context_k_fused_hits_is_not_evidence() -> None:
    # The top context_k fused hits go on, then the gate (ADR 0014 decision 5, D7): a well-scoring
    # hit that fusion ranks third never reaches the prompt when only two go on.
    low1, low2 = hit("Low one.", 0.1, chunk_index=0), hit("Low two.", 0.2, chunk_index=1)
    high = hit("High.", 0.9, chunk_index=2)
    search = build_hybrid_search({A: [low1, low2, high]}, {A: []})
    provider = FakeProvider()

    run = await run_answer(search, provider, RagConfig(0.3, 2, "dont_know"))

    assert (run.response.outcome, run.reason, run.evidence) == ("dont_know", "below_threshold", ())
    assert list(run.hits) == [low1, low2, high]
    assert run.response.confidence.value == 0.2  # the best of the two that went on
    assert provider.calls == []


@pytest.mark.anyio
async def test_hybrid_evidence_stays_in_fused_order_never_re_sorted_by_cosine() -> None:
    # Fused order is the ranking the evidence keeps (ADR 0014 decision 5): the gate drops hits,
    # it never reorders them. Cosines chosen out of order (0.5, 0.9, 0.7, all at or above 0.3),
    # so a sort by cosine would put the second hit first.
    first, second, third = (
        hit(f"Chunk {i}.", score, chunk_index=i) for i, score in enumerate((0.5, 0.9, 0.7))
    )
    search = build_hybrid_search({A: [first, second, third]}, {A: []})
    provider = CitingProvider()

    run = await run_answer(search, provider, RagConfig(0.3, 3, "dont_know"))

    assert list(run.hits) == list(run.evidence) == [first, second, third]
    assert prompt_ids(provider) == [str(h.chunk_id) for h in (first, second, third)] + [None]
    assert run.response.confidence.value == 0.9  # the best cosine, wherever it stands


def test_rrf_k_comes_from_the_retrieval_config_into_the_fusion_and_the_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Not the RRF_K constant (60): the configured `rag_rrf_k`, both where the fusion is called and
    # in what the record says it was.
    caplog.set_level(logging.INFO)
    search = build_hybrid_search(
        {A: [GOOD]}, {A: [RESCUE]}, retrieval=RetrievalConfig(mode="hybrid", rrf_k=7)
    )

    ask(settings, monkeypatch, search, CitingProvider(), caplog=caplog)

    assert [rrf_k for _, rrf_k, _ in search.fuse.calls] == [7]
    [record] = records(caplog, RAG_ANSWER)
    assert record["retrieval"]["rrf_k"] == 7


def test_fts_only_hits_and_rescued_evidence_are_counted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search({A: [GOOD]}, {A: [RESCUE, STUFFED, GOOD]})
    provider = CitingProvider()

    response = ask(settings, monkeypatch, search, provider, caplog=caplog)

    assert response.json()["outcome"] == "answered"
    # Fused (stand-in order): GOOD, RESCUE, STUFFED. RESCUE and STUFFED are FTS-only; only RESCUE
    # passes the gate, so hybrid added one chunk to the prompt.
    assert prompt_ids(provider) == [str(GOOD.chunk_id), str(RESCUE.chunk_id), None]
    [record] = records(caplog, RAG_ANSWER)
    assert record["retrieval"]["fts_only_hits"] == 2
    assert record["retrieval"]["fts_only_evidence"] == 1


# ── The rag_answer record ────────────────────────────────────────────────────────────────


def test_a_hybrid_record_carries_counts_and_stage_durations_and_nothing_else(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search(
        {A: [GOOD, STUFFED]}, {A: [RESCUE, GOOD]}, clock=TickingClock(step=0.003)
    )
    provider = CitingProvider()

    response = ask(settings, monkeypatch, search, provider, caplog=caplog)

    [record] = records(caplog, RAG_ANSWER)
    assert record == {
        "record_version": 1,  # additive: ADR 0007's rule, ADR 0011 amendment
        "business_id": A,
        "trace_id": response.headers[TRACE_HEADER],
        "prompt_version": PROMPT_VERSION,
        "outcome": "answered",
        "reason": None,
        "error": None,
        "hits": 3,  # fused: GOOD, STUFFED, RESCUE
        "evidence": 2,  # GOOD and RESCUE (STUFFED's cosine is 0.01)
        "cited": 2,
        "top_score": 0.8,
        "min_score": 0.3,
        "model_called": True,
        "latency_ms": record["latency_ms"],
        "retrieval": {
            "mode": "hybrid",
            "candidate_k": 20,
            "context_k": 5,  # rag_context_k: how many top fused hits the gate saw
            "rrf_k": 60,
            "vector_hits": 2,
            "text_hits": 2,
            "fused_hits": 3,
            "fts_only_hits": 1,
            "fts_only_evidence": 1,
            # One 3 ms step per stage on the ticking clock; the transaction spans eight, the
            # fake database's commit included.
            "embed_ms": pytest.approx(3.0),
            "transaction_ms": pytest.approx(24.0),
            "vector_ms": pytest.approx(3.0),
            "text_ms": pytest.approx(3.0),
            "fuse_ms": pytest.approx(3.0),
        },
    }


def test_a_record_counts_the_whole_fused_list_but_gates_and_scores_only_the_top_context_k(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Eight fused hits, context_k 3. The best cosine (0.95) sits fourth in fused order, so a
    # record whose evidence or top_score looked past the top three would show 8 or 0.95.
    caplog.set_level(logging.INFO)
    cosines = [0.5, 0.6, 0.4, 0.95, 0.9, 0.85, 0.8, 0.75]
    many = [hit(f"Chunk {i}.", score, chunk_index=i) for i, score in enumerate(cosines)]
    search = build_hybrid_search({A: many}, {A: []})

    response = ask(
        settings,
        monkeypatch,
        search,
        CitingProvider(),
        config=RagConfig(0.3, 3, "dont_know"),
        caplog=caplog,
    )

    [record] = records(caplog, RAG_ANSWER)
    assert record["hits"] == record["retrieval"]["fused_hits"] == 8  # the whole fused list
    assert (record["retrieval"]["candidate_k"], record["retrieval"]["context_k"]) == (20, 3)
    assert record["evidence"] == 3  # the top three, all at or above 0.3
    assert record["top_score"] == 0.6 == response.json()["confidence"]["value"]


def test_a_record_whose_good_hit_is_below_the_top_context_k_has_no_evidence(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    low1, low2 = hit("Low one.", 0.1, chunk_index=0), hit("Low two.", 0.2, chunk_index=1)
    high = hit("High.", 0.9, chunk_index=2)
    search = build_hybrid_search({A: [low1, low2, high]}, {A: []})
    provider = FakeProvider()  # an empty script: any call would fail the test

    ask(
        settings,
        monkeypatch,
        search,
        provider,
        config=RagConfig(0.3, 2, "dont_know"),
        caplog=caplog,
    )

    [record] = records(caplog, RAG_ANSWER)
    assert (record["hits"], record["retrieval"]["fused_hits"], record["evidence"]) == (3, 3, 0)
    assert (record["top_score"], record["reason"], record["model_called"]) == (
        0.2,
        "below_threshold",
        False,
    )
    assert provider.calls == []


def test_a_vector_mode_record_has_exactly_m2s_fields(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search({A: [GOOD]}, {A: [RESCUE]}, retrieval=RetrievalConfig())

    ask(settings, monkeypatch, search, CitingProvider(), caplog=caplog)

    [record] = records(caplog, RAG_ANSWER)
    assert set(record) == set(M2_RECORD)  # no `retrieval` key at all
    assert search.text.calls == [] and search.fuse.calls == []


def test_m2_records_still_validate_and_serialize_unchanged() -> None:
    record = RagAnswerRecord.model_validate(M2_RECORD)

    assert record.record_version == 1 and record.retrieval is None
    assert record.model_dump(mode="json") == M2_RECORD
    assert record.model_copy(update={"hits": 5}).model_dump(mode="json") == M2_RECORD | {"hits": 5}


def test_a_hybrid_record_round_trips_and_its_block_is_closed() -> None:
    block = {
        "mode": "hybrid",
        "candidate_k": 20,
        "context_k": 5,
        "rrf_k": 60,
        "vector_hits": 2,
        "text_hits": None,
        "fused_hits": None,
        "fts_only_hits": None,
        "fts_only_evidence": None,
        "embed_ms": 1.5,
        "transaction_ms": None,
        "vector_ms": 0.5,
        "text_ms": None,
        "fuse_ms": None,
    }
    data = M2_RECORD | {"outcome": "error", "error": "NotImplementedError", "retrieval": block}

    assert RagAnswerRecord.model_validate(data).model_dump(mode="json") == data
    for extra in ({"question": CANARY}, {"chunk_ids": [CANARY]}):
        with pytest.raises(ValueError) as raised:
            RagAnswerRecord.model_validate(data | {"retrieval": block | extra})
        assert CANARY not in str(raised.value)
    with pytest.raises(ValueError):
        RagAnswerRecord.model_validate(data | {"retrieval": block | {"mode": "vector"}})


def test_no_question_chunk_title_or_answer_text_reaches_any_record_in_hybrid_mode(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    planted = hit(f"Chunk text {CANARY}.", 0.9, title=f"Title {CANARY}", source_id="p")
    text_only = hit(f"Text-only {CANARY}.", 0.7, title=f"Other {CANARY}", source_id="q")
    weak = hit(f"weak {CANARY}", 0.1, title=f"Weak {CANARY}", source_id="w")
    search = build_hybrid_search({A: [planted, weak]}, {A: [text_only, planted]})
    provider = FakeProvider(
        [
            FakeReply(text=reply("answered", f"Answer {CANARY}", [str(planted.chunk_id)])),
            FakeReply(text=reply("answered", f"Forged {CANARY}", [str(uuid.uuid4())])),
            FakeReply(text=f"invalid {CANARY}"),
            FakeReply(text=f"invalid again {CANARY}"),
        ]
    )
    service = answer_service(search, provider, RagConfig(0.3, 5, "dont_know"))
    question = f"{QUESTION} {CANARY}"
    with serve(settings, monkeypatch, search, service, caplog=caplog) as client:
        caplog.set_level(logging.DEBUG)  # create_app set the root logger back to INFO
        outcomes = [post_answer(client, {"question": question}).status_code for _ in range(3)]
        outcomes.append(post_answer(client, {"question": CANARY * 1000}).status_code)

    assert outcomes == [200, 200, 502, 422]
    assert CANARY not in logged(caplog)
    answers = records(caplog, RAG_ANSWER)
    assert len(answers) == 3 and all(r["retrieval"]["mode"] == "hybrid" for r in answers)


@pytest.mark.anyio
async def test_a_hybrid_search_that_fails_is_recorded_with_how_far_it_got(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search({A: [GOOD]}, {A: [RESCUE]})
    search.text.raises = NotImplementedError("H1")  # what the stub raises

    with pytest.raises(NotImplementedError):
        await run_answer(search, FakeProvider(), RagConfig(0.3, 5, "dont_know"))

    [record] = records(caplog, RAG_ANSWER)
    assert (record["outcome"], record["error"], record["model_called"]) == (
        "error",
        "NotImplementedError",
        False,
    )
    block = record["retrieval"]
    assert (block["mode"], block["candidate_k"], block["vector_hits"]) == ("hybrid", 20, 1)
    assert block["embed_ms"] is not None and block["vector_ms"] is not None
    assert [block[name] for name in ("text_hits", "fused_hits", "fts_only_evidence")] == [None] * 3
    assert [block[name] for name in ("text_ms", "fuse_ms", "transaction_ms")] == [None] * 3


def test_reindex_required_in_hybrid_mode_is_a_typed_503_with_a_partial_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_hybrid_search({A: [GOOD]}, {A: [RESCUE]}, stored={A: frozenset({"old"})})

    response = ask(settings, monkeypatch, search, FakeProvider(), caplog=caplog)

    assert response.status_code == 503 and response.json()["error"] == "reindex_required"
    [record] = records(caplog, RAG_ANSWER)
    assert (record["error"], record["retrieval"]["vector_hits"]) == ("ReindexRequired", None)
    assert record["retrieval"]["embed_ms"] is not None
    assert response.headers[TRACE_HEADER] == record["trace_id"]


# ── Errors: the route maps them, logs nothing of them, and has no timeout of its own ─────────


@pytest.mark.parametrize(
    ("steps", "status", "code"),
    [([Transient()] * 3, 503, "model_unavailable"), ([ModelRefused()], 502, "answer_failed")],
    ids=["retries used up", "refusal"],
)
def test_hybrid_model_failures_are_typed_errors_recorded_by_class_name_only(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    steps: list[Any],
    status: int,
    code: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    planted = hit(f"Chunk {CANARY}.", 0.9, title=f"Title {CANARY}")
    search = build_hybrid_search({A: [planted]}, {A: [planted]})

    response = ask(
        settings, monkeypatch, search, FakeProvider(steps), question=f"{CANARY}?", caplog=caplog
    )

    assert response.status_code == status and response.json()["error"] == code
    assert CANARY not in response.text and CANARY not in logged(caplog)
    [record] = records(caplog, RAG_ANSWER)
    assert record["error"] in {"ModelUnavailable", "AnswerFailed"}
    assert response.headers[TRACE_HEADER] == record["trace_id"]


ROUTE_MODULES: Final = tuple(
    Path(__file__).parents[2] / "src" / "vectapilot_api" / name
    for name in ("answer/router.py", "retrieval/router.py", "answer/service.py")
)
TIMEOUTS: Final = frozenset({"timeout", "timeout_at", "wait_for", "fail_after", "move_on_after"})


PROBE: Final = """
import asyncio, logging
logger = logging.getLogger(__name__)
async def route() -> None:
    async with asyncio.timeout(5):
        ...
"""
"""What the scan must find: a route with a timeout and a logger (its positive control)."""


def names_in(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name)
    return found


def test_no_route_level_timeout_wraps_the_answer_and_the_routes_log_no_error_text() -> None:
    # Carried from M1 (m1-pr-checks.md, "Handed on to M3"; ADR 0007 amendment): the gateway owns
    # every deadline. A route-level timeout around complete() would have to be recorded as
    # `cancelled`, not `deadline_exceeded`; none exists. The route handlers never log: a failure
    # is a typed error plus the records (whose `error` is a class name, `type(error).__name__`).
    sources = {path.name: path.read_text(encoding="utf-8") for path in ROUTE_MODULES}
    for name, source in sources.items():
        assert not names_in(source) & TIMEOUTS, name
    for path in ROUTE_MODULES[:2]:  # the two route modules
        logging_names = {"logging", "logger", "exception", "format_exc"}
        assert not names_in(path.read_text(encoding="utf-8")) & logging_names, path.name
    # The positive control: the scan sees a timeout and a logger where there is one.
    assert {"timeout", "logger", "logging"} <= names_in(PROBE)


# ── NUL in a question: a 422 on both routes in both modes, echoing nothing ──────────────────


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
@pytest.mark.parametrize("route", ["GET /v1/kb/search", "POST /v1/answer"])
def test_a_nul_in_the_question_is_a_422_that_echoes_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, mode: str, route: str
) -> None:
    retrieval = RetrievalConfig(mode="hybrid") if mode == "hybrid" else RetrievalConfig()
    search = build_hybrid_search({A: [GOOD]}, {A: [RESCUE]}, retrieval=retrieval)
    question = f"Sunday{NUL}{CANARY}"
    with serve(settings, monkeypatch, search) as client:
        if route.startswith("GET"):
            response = client.get(
                "/v1/kb/search", params={"q": question}, headers={"X-Business-Id": A}
            )
        else:
            response = post_answer(client, {"question": question})

    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_request"
    assert all(set(p) == {"loc", "type"} for p in response.json()["problems"])
    assert CANARY not in response.text and "\\u0000" not in response.text
    assert TRACE_HEADER not in response.headers  # refused before the request had a trace id
    assert search.embedder.batches == [] and search.database.opened == []
