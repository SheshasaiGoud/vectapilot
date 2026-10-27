"""M3 PR 5: the question loop in hybrid mode (`evalrun/harness.py`) over the real `AnswerService`.

Both legs and the fusion are team-side stand-ins (`ScriptedSearch`, `ScriptedTextSearch`, and
`StandInFuse`, which is deliberately not RRF: M3 H1 and H2 are shesha's [HAND-WRITE]s), with
prepared document rows; H2 (M2's metrics, written) is called as the harness calls it. No
Postgres, no paid call. Every question, chunk text and hit list is TEST-ONLY.
"""

import io
import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from tests.conftest import make_settings
from tests.embeddings.scripted import FAKE_SPEC
from tests.evalrun.helpers import (
    CORPUS_DIR,
    HYBRID_CONFIG,
    PAWS,
    SUNRISE,
    Parts,
    PreparedLookup,
    corpus,
    harness_parts,
    hit,
    question,
    run_info,
)
from tests.retrieval.standins import FakeDatabase, StandInFuse, TickingClock, fused
from vectapilot_api.answer.service import RAG_ANSWER
from vectapilot_api.evalrun import harness
from vectapilot_api.evalrun.budget import CostCap
from vectapilot_api.evalrun.harness import (
    FuseCall,
    QuestionsOutcome,
    RecordingSearch,
    RunConfig,
    RunOutcome,
    SearchCall,
    Target,
    run_questions,
)
from vectapilot_api.evalrun.report import (
    ANSWER_LOG_FILE,
    RESULTS_FILE,
    RUN_FILE,
    SEARCH_K_ABOVE_CANDIDATE_K,
    summarize,
)
from vectapilot_api.evalrun.scoring import H2_METRICS, AnswerLine, DocumentRow, QuestionResult
from vectapilot_api.evalset.relevance import RetrievedChunk
from vectapilot_api.gateway.errors import Transient
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.llm_cli import LogCapture
from vectapilot_api.retrieval.search import RetrievalConfig
from vectapilot_api.retrieval.types import SearchHit

pytestmark = pytest.mark.anyio

EVAL: Final = {SUNRISE: "sunrise-clinic-ev-aaaaaaaaaa", PAWS: "paws-and-co-ev-bbbbbbbbbb"}
HOURS: Final = (
    "Opening hours: Monday to Friday: 8:00 to 20:00. Saturday: 9:00 to 14:00. Sunday: closed."
)
OTHER: Final = "TEST-ONLY chunk text that answers nothing zq9"
VISIBLE: Final[dict[str, tuple[RetrievedChunk, ...]]] = {
    SUNRISE: (RetrievedChunk(SUNRISE, "faq", "2026-10", HOURS),),
    PAWS: (RetrievedChunk(PAWS, "faq", "2026-09", OTHER),),
}
HYBRID: Final = RetrievalConfig(mode="hybrid", candidate_k=20, rrf_k=60)
VECTOR_CONFIG: Final = RunConfig(
    chunking=ChunkingConfig(),
    search_k=5,
    metric_k=5,
    rag_min_score=0.3,
    weak_evidence_outcome="dont_know",
    llm_max_retries=2,
    llm_max_repairs=1,
)
CONFIG: Final = replace(VECTOR_CONFIG, retrieval=HYBRID)


def owned(parts: Parts, business: str, *hits: SearchHit, **row: str) -> list[SearchHit]:
    return parts.lookup.own(EVAL[business], *hits, **row)


async def run(
    parts: Parts, *questions: str, config: RunConfig = CONFIG
) -> tuple[QuestionsOutcome, list[tuple[QuestionResult, AnswerLine | None]]]:
    sunk: list[tuple[QuestionResult, AnswerLine | None]] = []
    outcome = await run_questions(
        [question(q) for q in questions],
        answer_service=parts.answer,
        recorder=parts.recorder,
        mapping=EVAL,
        visible=VISIBLE,
        config=config,
        cap=parts.cap,
        sink=lambda result, answer: sunk.append((result, answer)),
        progress=lambda line: None,
        functions=H2_METRICS,
        dry_run=True,
    )
    return outcome, sunk


@pytest.fixture
def hybrid() -> Iterator[Parts]:
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=StandInFuse()
    ) as parts:
        yield parts


# ── What a hybrid run scores and keeps ───────────────────────────────────────────────────────


async def test_a_hybrid_run_scores_the_fused_list_and_keeps_each_legs_list(hybrid: Parts) -> None:
    relevant, middling, keyword = hit(HOURS, 0.9), hit(OTHER, 0.5), hit(OTHER, 0.2, chunk_index=1)
    hybrid.search.script.append(owned(hybrid, SUNRISE, relevant, middling))
    hybrid.text.script.append([*owned(hybrid, SUNRISE, keyword), relevant])

    outcome, sunk = await run(hybrid, "t-1")

    [result] = outcome.results
    # StandInFuse keeps each chunk where first met, vector list first: relevant, middling, keyword.
    assert [h.chunk_id for h in result.hits] == [
        str(relevant.chunk_id),
        str(middling.chunk_id),
        str(keyword.chunk_id),
    ]
    assert [h.groups for h in result.hits] == [(0,), (), ()]
    assert [h.score for h in result.hits] == [0.9, 0.5, 0.2]  # each the cosine, never RRF's
    # H2 on the fused list: the relevant chunk at rank 1.
    assert (result.retrieval, result.hit_at_k, result.reciprocal_rank) == ("scored", True, "1")
    components = result.components
    assert components is not None
    assert [(h.rank, h.chunk_id, h.groups) for h in components.vector] == [
        (1, str(relevant.chunk_id), (0,)),
        (2, str(middling.chunk_id), ()),
    ]
    assert [(h.rank, h.chunk_id, h.groups) for h in components.text] == [
        (1, str(keyword.chunk_id), ()),
        (2, str(relevant.chunk_id), (0,)),
    ]
    # The top 5 fused hits reach the cosine gate (0.3): the keyword chunk (0.2) is no evidence.
    assert result.evidence_chunk_ids == (str(relevant.chunk_id), str(middling.chunk_id))
    # Each leg was asked for the candidate k, and the fusion got exactly the two legs at RRF k.
    assert [q.k for q in hybrid.search.calls] == [20] and [q.k for q in hybrid.text.calls] == [20]
    [(lists, rrf_k, limit)] = hybrid.fuse.calls
    assert (rrf_k, limit) == (60, 20)
    assert lists == [[relevant, middling], [keyword, relevant]]
    assert next(answer for _, answer in sunk) is not None
    # No text in the line: ids, ranks and groups only.
    line = result.model_dump_json()
    assert HOURS not in line and OTHER not in line and "TEST-ONLY title" not in line


async def test_a_vector_run_runs_no_text_leg_and_no_fusion_and_keeps_m2s_line() -> None:
    with harness_parts([], list(EVAL.values()), text_script=[[hit(OTHER, 0.9)]]) as parts:
        relevant = hit(HOURS, 0.9)
        parts.search.script.append(owned(parts, SUNRISE, relevant))

        outcome, _ = await run(parts, "t-1", config=VECTOR_CONFIG)

    [result] = outcome.results
    assert parts.text.calls == [] and parts.fuse.calls == []
    assert [q.k for q in parts.search.calls] == [5]
    assert result.components is None
    assert "components" not in json.loads(result.model_dump_json())  # exactly M2's keys


async def test_a_failed_answer_model_still_scores_the_fused_list() -> None:
    with harness_parts(
        [],
        list(EVAL.values()),
        retrieval=HYBRID,
        text_script=[],
        provider=FakeProvider([Transient()]),
        retry_policy=RetryPolicy(max_retries=0, base_delay_s=0.5, max_delay_s=8.0),
    ) as parts:
        relevant, keyword = hit(HOURS, 0.9), hit(OTHER, 0.1)
        parts.search.script.append(owned(parts, SUNRISE, relevant))
        parts.text.script.append(owned(parts, SUNRISE, keyword))

        outcome, _ = await run(parts, "t-1")

    [result] = outcome.results
    assert outcome.stop is None, outcome.stop
    assert (result.outcome, result.error) == ("error", "ModelUnavailable")
    assert [h.chunk_id for h in result.hits] == [str(relevant.chunk_id), str(keyword.chunk_id)]
    assert result.components is not None and result.evidence_chunk_ids is None
    assert (result.retrieval, result.hit_at_k) == ("scored", True)


# ── Every list is checked before the answer step ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("owner", "row", "line"),
    [
        (PAWS, {}, "CROSS-BUSINESS LEAK: question t-1, rank 2 of the text leg: document"),
        (
            SUNRISE,
            {"audience": "staff_only"},
            "AUDIENCE LEAK: question t-1, rank 2 of the text leg: document",
        ),
        (SUNRISE, {"status": "draft"}, "AUDIENCE LEAK: question t-1, rank 2 of the text leg"),
    ],
    ids=["other-business", "staff-only", "draft"],
)
async def test_a_leak_in_the_text_leg_aborts_even_when_the_fusion_drops_it(
    owner: str, row: dict[str, str], line: str
) -> None:
    clean, leaked = hit(HOURS, 0.9), hit(OTHER, 0.95, version="2026-09")
    # The fusion keeps only the clean hit: the leaked one never reaches the fused list.
    fuse = StandInFuse(fused=[fused(clean, Fraction(1, 61), (1, None))])
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=fuse
    ) as parts:
        parts.search.script.append(owned(parts, SUNRISE, clean))
        parts.text.script.append(
            owned(parts, SUNRISE, hit(OTHER, 0.4)) + owned(parts, owner, leaked, **row)
        )

        outcome, sunk = await run(parts, "t-1", "t-2")

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith(line)
    assert str(leaked.document_id) in outcome.leak
    assert fuse.calls == []  # aborted inside the text leg, before the fusion
    assert parts.cap.llm_calls == 0  # the answer model never saw anything
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]
    assert [answer for _, answer in sunk] == [None, None]


async def test_a_leak_in_the_vector_leg_names_its_list_in_a_hybrid_run(hybrid: Parts) -> None:
    leaked = hit(OTHER, 0.95, version="2026-09")
    hybrid.search.script.append(owned(hybrid, PAWS, leaked))
    hybrid.text.script.append([])

    outcome, _ = await run(hybrid, "t-1")

    assert outcome.leak is not None
    assert outcome.leak.startswith(
        f"CROSS-BUSINESS LEAK: question t-1, rank 1 of the vector leg: document"
        f" {leaked.document_id} belongs to {EVAL[PAWS]}"
    )
    assert hybrid.text.calls == []  # the vector leg's check stopped the search


@pytest.mark.parametrize(
    ("row", "detail"),
    [
        ({"audience": "staff_only"}, "status approved, audience staff_only"),
        ({"status": "draft"}, "status draft, audience public"),
    ],
    ids=["staff-only", "draft"],
)
async def test_an_audience_leak_in_the_vector_leg_names_its_list_in_a_hybrid_run(
    hybrid: Parts, row: dict[str, str], detail: str
) -> None:
    # Mutation audit gap: the vector leg's AUDIENCE LEAK line in a hybrid run names the list, as
    # the text leg's does; caught inside the vector leg, before the text leg and the fusion.
    clean, hidden = hit(HOURS, 0.9), hit(OTHER, 0.8, version="2026-09")
    hybrid.search.script.append(
        owned(hybrid, SUNRISE, hidden, **row) + owned(hybrid, SUNRISE, clean)
    )
    hybrid.text.script.append(owned(hybrid, SUNRISE, clean))

    outcome, sunk = await run(hybrid, "t-1")

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith(
        f"AUDIENCE LEAK: question t-1, rank 1 of the vector leg: document {hidden.document_id}"
    )
    assert detail in outcome.leak
    assert hybrid.text.calls == [] and hybrid.fuse.calls == []
    assert hybrid.cap.llm_calls == 0
    assert [answer for _, answer in sunk] == [None]


async def test_the_fused_list_check_alone_catches_a_staff_only_hit_when_the_legs_are_unchecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation audit M3c, defence in depth: with every leg's own check bypassed (TEST-ONLY: the
    # recorder still reads and records each leg's rows, but checks nothing), the fused list's
    # check alone must stop a staff-only hit before the answer step. `_score`'s second layer
    # checks the business only, so without the fused check the hit would reach the model.
    async def record_unchecked(
        self: RecordingSearch,
        connection: Any,
        business_id: str,
        k: int,
        hits: Any,
        into: list[SearchCall],
        name: str,
    ) -> None:
        found = await self._lookup(connection, [item.document_id for item in hits])
        into.append(SearchCall(business_id, k, tuple(hits), dict(found)))

    monkeypatch.setattr(RecordingSearch, "_record", record_unchecked)
    clean, staff = hit(HOURS, 0.9), hit(OTHER, 0.8, version="2026-09")
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=StandInFuse()
    ) as parts:
        parts.search.script.append(
            owned(parts, SUNRISE, clean) + owned(parts, SUNRISE, staff, audience="staff_only")
        )
        parts.text.script.append([])

        outcome, sunk = await run(parts, "t-1", "t-2")

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith(
        f"AUDIENCE LEAK: question t-1, rank 2 of the fused list: document {staff.document_id}"
    )
    # Both legs ran unchecked (the bypass is real), and the fusion's check stopped the search.
    assert len(parts.search.calls) == len(parts.text.calls) == len(parts.fuse.calls) == 1
    assert parts.cap.llm_calls == 0
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]
    assert [answer for _, answer in sunk] == [None, None]


async def test_a_fused_hit_that_no_leg_returned_fails_closed() -> None:
    clean, invented = hit(HOURS, 0.9), hit(HOURS, 0.9, chunk_index=3)
    fuse = StandInFuse(fused=[fused(invented, Fraction(1, 61), (1, None))])
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=fuse
    ) as parts:
        # Even with a Sunrise row of its own, a hit no leg returned has no row the legs read.
        owned(parts, SUNRISE, invented)
        parts.search.script.append(owned(parts, SUNRISE, clean))
        parts.text.script.append([])

        outcome, _ = await run(parts, "t-1")

    assert outcome.leak is not None
    assert outcome.leak.startswith(
        f"CROSS-BUSINESS LEAK: question t-1, rank 1 of the fused list: document"
        f" {invented.document_id} is not visible in it"
    )
    assert parts.cap.llm_calls == 0


async def test_a_fused_hit_of_another_business_is_caught_in_the_fusion() -> None:
    clean, foreign = hit(HOURS, 0.9), hit(OTHER, 0.9, version="2026-09")
    # The fusion returns a hit a leg returned for another question's business: only the fused
    # check sees it here, since this search's legs never returned it.
    fuse = StandInFuse(
        fused=[
            fused(clean, Fraction(1, 61), (1, None)),
            fused(foreign, Fraction(1, 62), (2, None)),
        ]
    )
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=fuse
    ) as parts:
        parts.search.script.append(owned(parts, SUNRISE, clean))
        parts.text.script.append([])
        owned(parts, PAWS, foreign)

        outcome, _ = await run(parts, "t-1")

    assert outcome.leak is not None
    assert outcome.leak.startswith("CROSS-BUSINESS LEAK: question t-1, rank 2 of the fused list")


@pytest.mark.parametrize(
    ("change", "rank", "why"),
    [
        ("sibling-chunk", 2, "is in neither leg's list"),
        ("changed-score", 1, "is not the hit its leg returned (a field such as its score changed;"),
        ("chunk-twice", 2, "is in the fused list twice"),
    ],
    ids=["sibling-chunk", "changed-score", "chunk-twice"],
)
async def test_a_fused_hit_that_is_not_a_legs_hit_unchanged_stops_the_run_before_the_answer(
    change: str, rank: int, why: str
) -> None:
    # Review of PR 5, ananya A2: each of these passes the document-level check (same document,
    # so same business, status and audience), and must still never reach the answer step.
    clean = hit(HOURS, 0.9)
    sibling = hit(HOURS, 0.9, chunk_index=1, document_id=clean.document_id)  # no leg returned it
    rescored = clean.model_copy(update={"score": 0.016})  # an RRF-sized "cosine"
    bad = {
        "sibling-chunk": [
            fused(clean, Fraction(1, 61), (1, None)),
            fused(sibling, Fraction(1, 62), (2, None)),
        ],
        "changed-score": [fused(rescored, Fraction(1, 61), (1, None))],
        "chunk-twice": [
            fused(clean, Fraction(1, 61), (1, None)),
            fused(clean, Fraction(1, 62), (1, None)),
        ],
    }[change]
    fuse = StandInFuse(fused=bad)
    with harness_parts(
        [], list(EVAL.values()), retrieval=HYBRID, text_script=[], fuse=fuse
    ) as parts:
        parts.search.script.append(owned(parts, SUNRISE, clean))
        parts.text.script.append([])

        outcome, sunk = await run(parts, "t-1", "t-2")

    culprit = sibling if change == "sibling-chunk" else clean
    assert outcome.leak is None  # not a leak: a broken fusion contract, a bug
    assert outcome.stop is not None
    assert outcome.stop.startswith(
        f"question t-1: rank {rank} of the fused list: chunk {culprit.chunk_id} {why}"
    )
    assert HOURS not in outcome.stop and "TEST-ONLY title" not in outcome.stop
    assert len(fuse.calls) == 1 and len(parts.recorder.fuse_calls) == 1  # recorded all the same
    assert parts.cap.llm_calls == 0  # the answer model never saw anything
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]
    assert [answer for _, answer in sunk] == [None, None]
    assert harness.final_status(outcome) == "partial"


def test_one_chunk_from_both_legs_as_two_equal_objects_passes_the_chunk_level_check() -> None:
    # Each leg builds its own SearchHit: one chunk found by both arrives as two equal objects
    # (rrf_fuse's contract), and the fused hit may be either.
    from_text = A.model_copy()
    assert from_text == A and from_text is not A

    both = fused(from_text, Fraction(2, 61), (1, 2))
    harness.check_fused_from_legs([[A], [B, from_text]], [both])

    with pytest.raises(harness.FusedListMismatch, match="rank 1 of the fused list"):
        harness.check_fused_from_legs([[A], [B]], [fused(hit(HOURS), Fraction(1, 61), (1, None))])


# ── The recorded lists must be what the run's config asks for ────────────────────────────────


def _call(k: int = 20, business: str = EVAL[SUNRISE], *hits: SearchHit) -> SearchCall:
    rows = {h.document_id: DocumentRow(business, "approved", "public") for h in hits}
    return SearchCall(business, k, tuple(hits), rows)


def _recorder(
    vector: list[SearchCall], text: list[SearchCall], fuses: list[FuseCall]
) -> RecordingSearch:
    recorder = RecordingSearch()
    recorder.calls[:], recorder.text_calls[:], recorder.fuse_calls[:] = vector, text, fuses
    return recorder


A: Final = hit(HOURS, 0.9)
B: Final = hit(OTHER, 0.5)


def _fuse_call(**changes: Any) -> FuseCall:
    values: dict[str, Any] = {
        "lists": ((A,), (B,)),
        "rrf_k": 60,
        "limit": 20,
        "fused": (fused(A, Fraction(1, 61), (1, None)), fused(B, Fraction(1, 61), (None, 1))),
    }
    return FuseCall(**(values | changes))


@pytest.mark.parametrize(
    ("vector", "text", "fuses", "config", "message"),
    [
        ([_call(20, EVAL[SUNRISE], A)], [], [], CONFIG, "both legs and one fusion"),
        (
            [_call(5, EVAL[SUNRISE], A)],
            [_call(5, EVAL[SUNRISE], B)],
            [],
            VECTOR_CONFIG,
            "the vector leg only",
        ),
        (
            [_call(5, EVAL[SUNRISE], A)],
            [_call(20, EVAL[SUNRISE], B)],
            [_fuse_call()],
            CONFIG,
            "another business or k",
        ),
        (
            [_call(20, EVAL[SUNRISE], A)],
            [_call(20, EVAL[PAWS], B)],
            [_fuse_call()],
            CONFIG,
            "another business or k",
        ),
        (
            [_call(20, EVAL[SUNRISE], A)],
            [_call(20, EVAL[SUNRISE], B)],
            [_fuse_call(lists=((B,), (A,)))],
            CONFIG,
            "the fusion did not get [vector hits, text hits]",
        ),
        (
            [_call(20, EVAL[SUNRISE], A)],
            [_call(20, EVAL[SUNRISE], B)],
            [_fuse_call(rrf_k=61)],
            CONFIG,
            "at the run's RRF k and candidate k",
        ),
        (
            [_call(20, EVAL[SUNRISE], A)],
            [_call(20, EVAL[SUNRISE], B)],
            [_fuse_call(limit=5)],
            CONFIG,
            "at the run's RRF k and candidate k",
        ),
        (
            [_call(20, EVAL[SUNRISE], A)],
            [_call(20, EVAL[SUNRISE], B)],
            [_fuse_call(limit=20, fused=(fused(A, Fraction(1, 61), (1, None)),) * 21)],
            CONFIG,
            "the fusion returned 21 hits for limit 20",
        ),
    ],
    ids=[
        "hybrid-without-text-leg",
        "vector-with-text-leg",
        "leg-at-search-k",
        "leg-of-another-business",
        "fusion-got-other-lists",
        "fusion-rrf-k",
        "fusion-limit",
        "fused-past-limit",
    ],
)
def test_lists_that_are_not_what_the_config_asks_for_stop_the_run_as_a_bug(
    vector: list[SearchCall],
    text: list[SearchCall],
    fuses: list[FuseCall],
    config: RunConfig,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=f"question t-1: .*{_escaped(message)}"):
        harness._recorded_lists(
            question("t-1"), EVAL[SUNRISE], _recorder(vector, text, fuses), config
        )


def _escaped(text: str) -> str:
    return "".join(f"\\{c}" if c in "[]().*+?" else c for c in text)


def test_the_lists_the_config_asks_for_go_through() -> None:
    lists = harness._recorded_lists(
        question("t-1"),
        EVAL[SUNRISE],
        _recorder([_call(20, EVAL[SUNRISE], A)], [_call(20, EVAL[SUNRISE], B)], [_fuse_call()]),
        CONFIG,
    )

    assert lists.ranked == (A, B)  # the fused list, in fused order


# ── The run's config ─────────────────────────────────────────────────────────────────────────


def test_a_hybrid_run_config_refuses_a_search_k_above_the_candidate_k() -> None:
    with pytest.raises(ValueError, match="must be at most rag_candidate_k"):
        replace(CONFIG, search_k=21)
    assert SEARCH_K_ABOVE_CANDIDATE_K.startswith("in hybrid mode the search k")
    # Vector mode has no candidate k: a search k of 21 is M2's rule (at most MAX_K).
    assert replace(VECTOR_CONFIG, search_k=21).search_call_k == 21
    assert CONFIG.search_call_k == 20 and replace(CONFIG, search_k=20).search_call_k == 20
    assert VECTOR_CONFIG.search_call_k == 5 and not VECTOR_CONFIG.hybrid and CONFIG.hybrid


@pytest.mark.parametrize("settings_mode", ["vector", "hybrid"])
@pytest.mark.parametrize("config", [VECTOR_CONFIG, CONFIG], ids=["run-vector", "run-hybrid"])
async def test_the_harness_searches_in_the_runs_mode_whatever_settings_say(
    settings_mode: str, config: RunConfig
) -> None:
    settings = make_settings(rag_retrieval_mode=settings_mode)

    async with harness.wired(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]  # never opened: nothing is searched here
        config,
        CostCap(cap=None),
        dry_run=True,
        records=lambda trace_id: None,
    ) as parts:
        search = parts.answer._search  # the service `wired` built (private: no public getter)

    assert search.retrieval == config.retrieval
    # Every list goes through the recorder, whichever mode.
    assert search.vector_search is parts.recorder
    assert search.text_search == parts.recorder.text_leg
    assert search.fuse == parts.recorder.fuse


class _Engine:
    async def dispose(self) -> None:
        """Nothing to release (TEST-ONLY: no database)."""


class _Database:
    def __init__(self, engine: Any) -> None:
        self.engine = engine


@pytest.mark.parametrize("settings_mode", ["vector", "hybrid"])
@pytest.mark.parametrize("config", [VECTOR_CONFIG, CONFIG], ids=["run-vector", "run-hybrid"])
async def test_check_run_and_execute_never_refuse_a_hybrid_setting(
    monkeypatch: pytest.MonkeyPatch, settings_mode: str, config: RunConfig
) -> None:
    # Mutation audit M15b: PR 4's refusal of a hybrid rag_retrieval_mode is gone from the harness
    # itself (check_run, which execute calls for any caller), not only from the CLI: whatever
    # Settings say, the run measures its own mode (--retrieval-mode). No database: execute stops
    # after wiring, at a stand-in for the steps before the questions.
    target = Target(
        corpus=corpus(), corpus_dir=CORPUS_DIR, questions=(question("t-1"),), mapping=EVAL
    )
    settings = make_settings(rag_retrieval_mode=settings_mode)
    paid = make_settings(
        rag_retrieval_mode=settings_mode,
        environment="local",
        llm_provider="anthropic",
        anthropic_api_key="test-only-not-a-key",
        embeddings_provider="voyage",
        voyage_api_key="test-only-not-a-key",
    )

    harness.check_run(target, settings, CostCap(cap=None), dry_run=True, fresh=True)
    harness.check_run(target, paid, CostCap(cap=Decimal("0.50")), dry_run=False, fresh=True)

    built: list[RetrievalConfig] = []

    async def before_questions(*args: Any, **kwargs: Any) -> RunOutcome:
        parts: harness.Parts = args[4]
        built.append(parts.answer._search.retrieval)  # the service `wired` built (private)
        return RunOutcome("complete", None, [])

    monkeypatch.setattr(harness, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(harness, "Database", _Database)
    monkeypatch.setattr(harness, "_before_questions", before_questions)

    outcome = await harness.execute(
        target,
        settings,
        config,
        CostCap(cap=None),
        dry_run=True,
        owner_url=None,
        records=lambda trace_id: None,
        sink=lambda result, answer: None,
        progress=lambda line: None,
    )

    assert (outcome.status, outcome.stop) == ("complete", None)
    assert built == [config.retrieval]


# ── The recorder's own time (noah N4) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("vector_ticks", "text_ticks"), [(0, 0), (3, 5)])
async def test_the_recorders_own_time_is_recorded_and_subtracted_on_one_ticking_clock(
    tmp_path: Path, vector_ticks: int, text_ticks: int
) -> None:
    # One ticking clock (each reading 0.5 s after the last: exact in binary floats) for the
    # answer, the search, the recorder and the commit. The lookup "takes" vector_ticks readings
    # in the vector leg and text_ticks in the text leg (one PreparedLookup, so the ticks are set
    # per leg by a wrapper). Readings, worked out by hand from the code's order:
    #   answer start 1 · embed: start, end (embed 1 step) · transaction open 1
    #   vector leg: start · recorder start · lookup v · recorder end · leg end
    #     -> recorder (v + 1) steps, leg (v + 3) steps
    #   text leg: the same with t -> recorder (t + 1), leg (t + 3)
    #   fuse: start · recorder start · recorder end · end -> recorder 1, fuse 3 steps
    #   commit 1 · transaction end 1 -> transaction (14 + v + t) steps
    #   answer end 1 -> answer (18 + v + t) steps
    # Minus the recorder (v + t + 3 steps): vector 2, text 2, fuse 2, transaction 11, answer 15,
    # whatever v and t are; the harness row is (v + t + 3) steps. 1 step = 500 ms.
    clock = TickingClock(step=0.5)
    lookup = PreparedLookup(clock=clock)
    ticks = iter([vector_ticks, text_ticks])
    plain = lookup.__call__

    async def timed_lookup(connection: Any, ids: Any) -> Any:
        lookup.ticks = next(ticks)
        return await plain(connection, ids)

    answer_log = io.StringIO()
    capture = LogCapture(answer_log)
    answer_logger = logging.getLogger("vectapilot_api.answer")
    answer_logger.addHandler(capture)
    try:
        with harness_parts(
            [],
            list(EVAL.values()),
            retrieval=HYBRID,
            text_script=[],
            lookup=lookup,
            clock=clock,
        ) as parts:
            parts.recorder._lookup = timed_lookup  # TEST-ONLY: per-leg ticks
            relevant, keyword = hit(HOURS, 0.9), hit(OTHER, 0.1)
            parts.search.script.append(owned(parts, SUNRISE, relevant))
            parts.text.script.append(owned(parts, SUNRISE, keyword))

            outcome, _ = await run(parts, "t-1")
    finally:
        answer_logger.removeHandler(capture)

    [result] = outcome.results
    assert result.components is not None
    own = result.components.harness
    step = 500.0
    assert (own.vector_ms, own.text_ms, own.fuse_ms) == (
        (vector_ticks + 1) * step,
        (text_ticks + 1) * step,
        1 * step,
    )
    [line] = capture.lines
    stages = json.loads(line)[RAG_ANSWER]["retrieval"]
    assert (stages["vector_ms"], stages["text_ms"], stages["fuse_ms"]) == (
        (vector_ticks + 3) * step,
        (text_ticks + 3) * step,
        3 * step,
    )
    # The folder, then the summary: each stage minus the recorder's own time.
    folder = tmp_path / "test-only-hybrid-run"
    folder.mkdir()
    info = run_info(questions_selected=1, config=HYBRID_CONFIG.model_copy(update={"search_k": 5}))
    (folder / RUN_FILE).write_text(info.model_dump_json(), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(result.model_dump_json() + "\n", encoding="utf-8")
    (folder / ANSWER_LOG_FILE).write_text(answer_log.getvalue(), encoding="utf-8")

    summary = summarize(folder, H2_METRICS)

    medians = {row.stage: row.latency.median_ms for row in summary.latency if row.latency}
    assert medians == {
        "embed (the question)": 1 * step,
        "vector leg": 2 * step,
        "text leg": 2 * step,
        "fuse": 2 * step,
        "transaction (model check, both legs, fuse, commit)": 11 * step,
        "answer (rag_answer latency_ms, model call included)": 15 * step,
        "harness: leak-check lookups and checks (subtracted above)": (vector_ticks + text_ticks + 3)
        * step,
    }
    assert all(row.latency is None or row.latency.p95_ms is None for row in summary.latency)


def test_the_recorder_defaults_to_the_written_and_stubbed_seams() -> None:
    from vectapilot_api.retrieval.fusion import rrf_fuse
    from vectapilot_api.retrieval.text import text_search

    recorder = RecordingSearch()

    assert recorder._text is text_search and recorder._fuse is rrf_fuse
    assert recorder._name_lists is False


async def test_a_failed_lookup_is_recorded_and_raised_unchanged() -> None:
    class Down(Exception):
        """TEST-ONLY: the lookup's database is gone."""

    async def lookup(connection: Any, ids: Any) -> Any:
        raise Down

    async def search(connection: Any, query: Any) -> list[SearchHit]:
        return [A]

    recorder = RecordingSearch(search, lookup)
    with pytest.raises(Down):
        await recorder(None, _query())  # type: ignore[arg-type]

    [call] = recorder.calls
    assert call.hits == (A,) and call.documents == {}


def _query() -> Any:
    from vectapilot_api.retrieval.types import VectorQuery

    return VectorQuery(
        business_id=EVAL[SUNRISE],
        embedding=(0.0,) * 1023 + (1.0,),
        embedding_model=FAKE_SPEC.model,
        k=5,
        reader="customer",
    )
