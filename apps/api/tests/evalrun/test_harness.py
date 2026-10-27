"""The question loop (`evalrun/harness.run_questions`) over the real `AnswerService`, with H1, the
document lookup and H2 as stand-ins (`tests/evalrun/helpers.py`): prepared hits, prepared
document rows and prepared metric values; and `execute`'s refusals and interrupts with its
database seams replaced. No Postgres, no paid call."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, cast

import pytest

from tests.canary.read_paths import pending_hand_writes
from tests.conftest import make_settings
from tests.embeddings.scripted import FAKE_SPEC
from tests.evalrun.helpers import (
    CORPUS_DIR,
    PAWS,
    PAWS_STORE_HOURS,
    SENTINEL_QUESTION,
    SUNRISE,
    SUNRISE_HOURS,
    SUNRISE_PARKING,
    Parts,
    PreparedMetrics,
    corpus,
    harness_parts,
    hit,
    question,
)
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.evalrun import harness
from vectapilot_api.evalrun.budget import CappedEmbedder, CapReached, CostCap
from vectapilot_api.evalrun.dryrun import DRY_RUN_ANSWER, DryRunAnswerProvider
from vectapilot_api.evalrun.harness import (
    INTERRUPTED_BEFORE_QUESTIONS,
    HarnessRefused,
    QuestionsOutcome,
    RecordingSearch,
    RunConfig,
    Target,
    execute,
    run_questions,
)
from vectapilot_api.evalrun.ingest import IngestCount
from vectapilot_api.evalrun.scoring import H2_METRICS, AnswerLine, MetricFunctions, QuestionResult
from vectapilot_api.evalrun.store import document_rows
from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.evalset.relevance import RetrievedChunk
from vectapilot_api.gateway.errors import InvalidRequest, Transient
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import RetryPolicy
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.kb.service import KbService
from vectapilot_api.retrieval.types import SearchHit
from vectapilot_api.retrieval.vector import vector_search

EVAL: Final = {SUNRISE: "sunrise-clinic-ev-aaaaaaaaaa", PAWS: "paws-and-co-ev-bbbbbbbbbb"}
CONFIG: Final = RunConfig(
    chunking=ChunkingConfig(),
    search_k=5,
    metric_k=5,
    rag_min_score=0.3,
    weak_evidence_outcome="dont_know",
    llm_max_retries=2,
    llm_max_repairs=1,
)
HOURS: Final = (
    "Opening hours: Monday to Friday: 8:00 to 20:00. Saturday: 9:00 to 14:00. Sunday: closed."
)
OTHER: Final = "TEST-ONLY chunk text that answers nothing zq9"
VISIBLE: Final[dict[str, tuple[RetrievedChunk, ...]]] = {
    SUNRISE: (RetrievedChunk(SUNRISE, "faq", "2026-10", HOURS),),
    PAWS: (RetrievedChunk(PAWS, "faq", "2026-09", OTHER),),
}
NO_RETRY: Final = RetryPolicy(max_retries=0, base_delay_s=0.5, max_delay_s=8.0)


def owned(parts: Parts, business: str, *hits: SearchHit) -> list[SearchHit]:
    """The hits, each document given an approved public row of `business`'s eval namespace."""
    return parts.lookup.own(EVAL[business], *hits)


async def run(
    parts: Parts,
    questions: list[EvalQuestion],
    prepared: PreparedMetrics,
    config: RunConfig = CONFIG,
    *,
    functions: MetricFunctions | None = None,
    dry_run: bool = False,
    mapping: dict[str, str] = EVAL,
) -> tuple[QuestionsOutcome, list[tuple[QuestionResult, AnswerLine | None]], list[str]]:
    sunk: list[tuple[QuestionResult, AnswerLine | None]] = []
    lines: list[str] = []
    outcome = await run_questions(
        questions,
        answer_service=parts.answer,
        recorder=parts.recorder,
        mapping=mapping,
        visible=VISIBLE,
        config=config,
        cap=parts.cap,
        sink=lambda result, answer: sunk.append((result, answer)),
        progress=lines.append,
        functions=prepared.functions() if functions is None else functions,
        dry_run=dry_run,
    )
    return outcome, sunk, lines


@pytest.mark.anyio
async def test_scored_not_scored_and_unscorable_questions_each_keep_their_count() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script += [
            owned(parts, SUNRISE, hit(OTHER, 0.9), hit(HOURS, 0.8)),
            owned(parts, SUNRISE, hit(OTHER, 0.2)),
            owned(parts, PAWS, hit(OTHER, 0.9, version="2026-09")),
        ]
        prepared = PreparedMetrics()
        prepared.prepare("t-scored", hit=True, rr=Fraction(1, 2))
        questions = [
            question("t-scored", text=SENTINEL_QUESTION),
            question("t-none", evidence=(), expected="dont_know", category="out_of_scope"),
            question("t-unscorable", PAWS, evidence=((PAWS_STORE_HOURS,),)),
        ]

        outcome, sunk, lines = await run(parts, questions, prepared)

    assert (outcome.stop, outcome.leak) == (None, None)
    scored, none, unscorable = outcome.results
    assert [r for r, _ in sunk] == outcome.results
    assert (scored.retrieval, scored.hit_at_k, scored.reciprocal_rank) == ("scored", True, "1/2")
    assert [h.groups for h in scored.hits] == [(), (0,)]
    assert scored.eval_business_id == EVAL[SUNRISE] and scored.business_id == SUNRISE
    assert (scored.outcome, scored.model_called) == ("answered", True)
    assert [s.chunk_id for s in scored.sources] == [scored.hits[0].chunk_id]
    assert scored.evidence_chunk_ids == tuple(h.chunk_id for h in scored.hits)
    assert scored.trace_id is not None and scored.prompt_version == "rag-answer.1"
    assert (none.retrieval, none.outcome, none.reason) == (
        "not_scored",
        "dont_know",
        "below_threshold",
    )
    assert none.model_called is False and none.hit_at_k is None
    assert (unscorable.retrieval, unscorable.unscorable_groups) == ("unscorable", (0,))
    assert unscorable.outcome == "answered" and unscorable.hit_at_k is None
    # Every search ran for the question's eval business, at the run's search k.
    assert [(q.business_id, q.k) for q in parts.search.calls] == [
        (EVAL[SUNRISE], 5),
        (EVAL[SUNRISE], 5),
        (EVAL[PAWS], 5),
    ]
    # H2 was called for the scored question only, at the metric k.
    assert {(name, qid, k) for name, qid, k in prepared.calls} == {
        ("hit_at_k", "t-scored", 5),
        ("all_groups_at_k", "t-scored", 5),
        ("reciprocal_rank", "t-scored", 5),
    }
    answer = sunk[0][1]
    assert answer is not None and answer.answer == DRY_RUN_ANSWER
    assert sunk[1][1] is not None and sunk[1][1].answer is None
    assert "hit@5 yes · first relevant rank 2" in lines[0]
    for line in lines:  # never question or chunk text
        assert "zq7" not in line and "zq9" not in line and "Opening hours" not in line


@pytest.mark.anyio
async def test_a_hit_from_another_business_aborts_the_run_loudly() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        leaked = hit(OTHER, 0.95, version="2026-09")
        script += [
            owned(parts, SUNRISE, hit(HOURS, 0.9)) + owned(parts, PAWS, leaked),
            owned(parts, SUNRISE, hit(HOURS, 0.9)),
        ]
        prepared = PreparedMetrics()
        prepared.prepare("t-2", hit=True, rr=Fraction(1))

        outcome, sunk, _ = await run(parts, [question("t-1"), question("t-2")], prepared)

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith("CROSS-BUSINESS LEAK: question t-1, rank 2")
    assert str(leaked.document_id) in outcome.leak and EVAL[PAWS] in outcome.leak
    assert [(r.question_id, r.retrieval, r.outcome) for r in outcome.results] == [
        ("t-1", "not_run", "not_run"),
        ("t-2", "not_run", "not_run"),
    ]
    assert len(parts.search.calls) == 1  # nothing after the leak
    assert [answer for _, answer in sunk] == [None, None]
    assert prepared.calls == []


@pytest.mark.anyio
async def test_a_hit_whose_document_is_not_visible_is_a_leak_too() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script.append([*owned(parts, SUNRISE, hit(HOURS, 0.9)), hit(OTHER, 0.8)])

        outcome, _, _ = await run(parts, [question("t-1")], PreparedMetrics())

    assert outcome.leak is not None and "is not visible in it" in outcome.leak


@pytest.mark.anyio
async def test_a_failed_answer_model_is_an_outcome_and_retrieval_is_still_scored() -> None:
    script: list[list[SearchHit]] = []
    provider = FakeProvider([Transient()])
    with harness_parts(
        script, list(EVAL.values()), provider=provider, retry_policy=NO_RETRY
    ) as parts:
        script += [owned(parts, SUNRISE, hit(HOURS, 0.9)), owned(parts, SUNRISE, hit(OTHER, 0.1))]
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        outcome, sunk, _ = await run(
            parts,
            [
                question("t-1"),
                question("t-2", evidence=(), expected="dont_know", category="out_of_scope"),
            ],
            prepared,
        )

    first, second = outcome.results
    assert (first.outcome, first.error, first.retrieval) == ("error", "ModelUnavailable", "scored")
    assert first.hit_at_k is True and first.evidence_chunk_ids is None
    assert first.evidence_complete is None
    assert sunk[0][1] is not None and sunk[0][1].answer is None
    assert (second.outcome, outcome.stop) == ("dont_know", None)


@pytest.mark.anyio
async def test_an_invalid_request_stops_the_run_after_that_question() -> None:
    script: list[list[SearchHit]] = []
    provider = FakeProvider([InvalidRequest()])
    with harness_parts(script, list(EVAL.values()), provider=provider) as parts:
        script += [owned(parts, SUNRISE, hit(HOURS, 0.9)), owned(parts, SUNRISE, hit(HOURS, 0.9))]
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        outcome, _, _ = await run(parts, [question("t-1"), question("t-2")], prepared)

    assert [r.outcome for r in outcome.results] == ["error", "not_run"]
    assert outcome.stop is not None and "invalid_request" in outcome.stop
    assert len(parts.search.calls) == 1


@pytest.mark.anyio
async def test_a_call_that_would_cross_the_cap_is_never_made_and_the_run_stops() -> None:
    script: list[list[SearchHit]] = []
    provider = FakeProvider()
    with harness_parts(
        script, list(EVAL.values()), provider=provider, cap=CostCap(cap=Decimal("0.001"))
    ) as parts:
        script += [owned(parts, SUNRISE, hit(HOURS, 0.9)), owned(parts, SUNRISE, hit(HOURS, 0.9))]

        outcome, sunk, _ = await run(parts, [question("t-1"), question("t-2")], PreparedMetrics())

    assert provider.calls == []
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]
    assert outcome.stop is not None and "an answer-model call not sent" in outcome.stop
    assert [answer for _, answer in sunk] == [None, None]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "stop"),
    [
        (RuntimeError("TEST-ONLY"), "question t-1: RuntimeError before its answer step finished"),
        (asyncio.CancelledError(), "interrupted during question t-1"),
    ],
    ids=["error", "interrupt"],
)
async def test_a_failure_before_the_answer_step_finished_stops_the_run(
    failure: BaseException, stop: str
) -> None:
    with harness_parts([], list(EVAL.values())) as parts:
        parts.search.raises = failure

        outcome, _, _ = await run(parts, [question("t-1"), question("t-2")], PreparedMetrics())

    assert outcome.stop == stop
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]


@pytest.mark.anyio
async def test_h1_not_written_yet_is_never_an_outcome() -> None:
    with harness_parts([], list(EVAL.values())) as parts:
        parts.search.raises = NotImplementedError("H1: TEST-ONLY")

        with pytest.raises(NotImplementedError):
            await run(parts, [question("t-1")], PreparedMetrics())


@pytest.mark.anyio
async def test_evidence_complete_is_all_groups_on_the_prompts_evidence_at_the_search_k() -> None:
    script: list[list[SearchHit]] = []
    config = RunConfig(
        chunking=ChunkingConfig(),
        search_k=6,
        metric_k=5,
        rag_min_score=0.3,
        weak_evidence_outcome="dont_know",
        llm_max_retries=2,
        llm_max_repairs=1,
    )
    with harness_parts(script, list(EVAL.values()), context_k=6) as parts:
        hours, weak = hit(HOURS, 0.9), hit(OTHER, 0.1)
        script.append(owned(parts, SUNRISE, hours, weak))
        prepared = PreparedMetrics(metric_k=5)
        prepared.prepare("t-1", hit=True, rr=Fraction(1), all_groups=True)
        prepared.evidence_all["t-1"] = False

        outcome, _, _ = await run(parts, [question("t-1", category="ambiguous")], prepared, config)

    [result] = outcome.results
    assert len(result.hits) == 2  # hit@k's list: both hits, the weak one included
    assert result.evidence_chunk_ids == (str(hours.chunk_id),)  # the weak hit is below 0.3
    assert (result.all_groups_at_k, result.evidence_complete) == (True, False)
    assert ("all_groups_at_k", "t-1", 6) in prepared.calls
    assert parts.search.calls[0].k == 6


def test_the_run_config_refuses_a_metric_k_above_the_search_k_and_bad_values() -> None:
    def config(**changes: object) -> RunConfig:
        values: dict[str, object] = {
            "chunking": ChunkingConfig(),
            "search_k": 5,
            "metric_k": 5,
            "rag_min_score": 0.3,
            "weak_evidence_outcome": "dont_know",
            "llm_max_retries": 2,
            "llm_max_repairs": 1,
        }
        return RunConfig(**(values | changes))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=r"metric k \(10\) must be at most the search's k \(5\)"):
        config(metric_k=10)
    with pytest.raises(ValueError, match="search k must be at most 50"):
        config(search_k=51, metric_k=5)
    with pytest.raises(ValueError, match="rag_min_score must be between 0 and 1"):
        config(rag_min_score=1.5)
    assert config().rag().context_k == 5


def test_the_recording_search_defaults_to_h1_and_the_row_lookup() -> None:
    recorder = RecordingSearch()

    assert recorder._search is vector_search
    assert recorder._lookup is document_rows


# ── Leaks are caught in the search, before anything else happens ─────────────────────────────


class FailingProvider:
    """An answer model whose every send fails with `error`; it counts what it was sent."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.requests = 0

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        raise self.error

    async def aclose(self) -> None:
        """Nothing to release."""


@pytest.mark.anyio
@pytest.mark.parametrize("after", ["cap-refusal", "ctrl-c", "error"])
async def test_a_leak_aborts_the_run_whatever_the_answer_step_would_have_done_next(
    after: str,
) -> None:
    # Each variant's answer step would stop the run without scoring (a call over the cap, a
    # Ctrl-C at the call, an error after the search): the leak must still be the outcome.
    provider: DryRunAnswerProvider | FailingProvider = DryRunAnswerProvider()
    cap = CostCap(cap=Decimal("0.001")) if after == "cap-refusal" else None
    if after == "ctrl-c":
        provider = FailingProvider(asyncio.CancelledError())
    elif after == "error":
        provider = FailingProvider(RuntimeError("TEST-ONLY"))
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values()), provider=provider, cap=cap) as parts:
        leaked = hit(OTHER, 0.95, version="2026-09")
        script.append(owned(parts, SUNRISE, hit(HOURS, 0.9)) + owned(parts, PAWS, leaked))

        outcome, sunk, _ = await run(parts, [question("t-1"), question("t-2")], PreparedMetrics())

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith("CROSS-BUSINESS LEAK: question t-1, rank 2: document")
    assert f"{leaked.document_id} belongs to {EVAL[PAWS]}" in outcome.leak
    assert provider.requests == 0  # the answer model never saw the leaked hit
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]
    assert [answer for _, answer in sunk] == [None, None]


@pytest.mark.anyio
async def test_the_second_layer_never_trusts_a_mapping_that_disagrees_with_the_question() -> None:
    # The search's own check passes (the row is the searched eval business), but the mapping
    # sends that eval id back to Paws, not to the question's Sunrise: the scoring layer aborts.
    tangled = {SUNRISE: EVAL[SUNRISE], PAWS: EVAL[SUNRISE]}
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script.append(owned(parts, SUNRISE, hit(HOURS, 0.9)))

        outcome, _, _ = await run(parts, [question("t-1")], PreparedMetrics(), mapping=tangled)

    assert outcome.leak is not None and outcome.stop == outcome.leak
    assert outcome.leak.startswith("CROSS-BUSINESS LEAK: question t-1, rank 1")
    assert [r.outcome for r in outcome.results] == ["not_run"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "audience"),
    [("approved", "staff_only"), ("draft", "public"), ("retired", "public")],
    ids=["staff-only", "draft", "retired"],
)
async def test_a_hit_no_customer_may_see_aborts_the_run_by_ids(status: str, audience: str) -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        bait = hit(OTHER, 0.95)
        script.append(
            owned(parts, SUNRISE, hit(HOURS, 0.9))
            + parts.lookup.own(EVAL[SUNRISE], bait, status=status, audience=audience)
        )

        outcome, _, _ = await run(parts, [question("t-1")], PreparedMetrics())

    assert outcome.leak is not None
    assert outcome.leak.startswith(
        f"AUDIENCE LEAK: question t-1, rank 2: document {bait.document_id}"
    )
    assert f"is status {status}, audience {audience}" in outcome.leak
    assert "zq9" not in outcome.leak  # never chunk text
    assert [r.outcome for r in outcome.results] == ["not_run"]


# ── What the harness measures ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "which",
    [
        pytest.param(None, id="stand-in"),
        pytest.param(H2_METRICS, marks=pending_hand_writes("retrieval_metrics"), id="h2"),
    ],
)
async def test_hit_at_k_is_taken_on_the_ranked_list_before_the_threshold(
    which: MetricFunctions | None,
) -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        weak = hit(HOURS, 0.2)  # relevant (finds group 0), but below rag_min_score 0.3
        script.append(owned(parts, SUNRISE, weak))
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        outcome, _, _ = await run(parts, [question("t-1")], prepared, functions=which)

    [result] = outcome.results
    assert (result.outcome, result.reason, result.model_called) == (
        "dont_know",
        "below_threshold",
        False,
    )
    assert result.evidence_chunk_ids == ()  # the prompt held nothing
    assert result.hits[0].groups == (0,) and result.hits[0].score == 0.2
    # H2 (or its stand-in) got the ranked list with the below-threshold hit: hit@5, RR 1.
    assert (result.hit_at_k, result.reciprocal_rank) == (True, "1")
    if which is None:
        [record] = [record for name, record in prepared.given if name == "hit_at_k"]
        assert record.groups_by_rank == ((0,),)


@pytest.mark.anyio
async def test_the_question_is_sent_exactly_as_the_api_would_send_it() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script.append(owned(parts, SUNRISE, hit(HOURS, 0.9)))
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        await run(parts, [question("t-1", text="  TEST-ONLY padded question zq7\n")], prepared)

    assert parts.embedder.batches == [["TEST-ONLY padded question zq7"]]  # stripped, as the route


@pytest.mark.anyio
async def test_a_question_the_api_would_refuse_refuses_the_run_by_id_before_any_search() -> None:
    with (
        harness_parts([], list(EVAL.values())) as parts,
        pytest.raises(HarnessRefused, match="question t-2: its text is not") as refused,
    ):
        await run(parts, [question("t-1"), question("t-2", text=" \n ")], PreparedMetrics())

    assert parts.search.calls == [] and refused.value.__context__ is None


@pytest.mark.anyio
async def test_a_search_that_returns_more_than_k_hits_stops_everything_as_a_bug() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        parts.search.truncate = False  # an H1 that ignores k
        script.append(owned(parts, SUNRISE, *(hit(HOURS, 0.9) for _ in range(6))))
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        with pytest.raises(RuntimeError, match="the search returned 6 hits for k = 5"):
            await run(parts, [question("t-1")], prepared)


@pytest.mark.anyio
async def test_a_key_no_stored_chunk_holds_is_recorded_though_its_group_is_found() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script.append(owned(parts, SUNRISE, hit(HOURS, 0.9)))
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))
        either = question("t-1", evidence=((SUNRISE_HOURS, SUNRISE_PARKING),))

        outcome, _, _ = await run(parts, [either], prepared)

    [result] = outcome.results  # VISIBLE holds the hours, not the parking sentence
    assert (result.retrieval, result.unheld_keys) == ("scored", ((0, 1),))


@pytest.mark.anyio
async def test_a_dry_runs_progress_line_shows_no_dollar_amount() -> None:
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script.append(owned(parts, SUNRISE, hit(HOURS, 0.9)))
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))

        _, _, lines = await run(parts, [question("t-1")], prepared, dry_run=True)

    assert lines[0].endswith("run total $0 (dry run: fake usage)")


# ── execute: refusals and interrupts before the database or the questions ────────────────────


def target(**changes: Any) -> Target:
    values: dict[str, Any] = {
        "corpus": corpus(),
        "corpus_dir": CORPUS_DIR,
        "questions": (question("t-1"),),
        "mapping": EVAL,
    }
    return Target(**(values | changes))


async def execute_with(
    settings: Settings, *, dry_run: bool, cap: CostCap, **kwargs: Any
) -> harness.RunOutcome:
    return await execute(
        kwargs.pop("target", target()),
        settings,
        CONFIG,
        cap,
        dry_run=dry_run,
        owner_url=None,
        records=lambda trace_id: None,
        sink=lambda result, answer: None,
        progress=lambda line: None,
        **kwargs,
    )


def no_database(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("TEST-ONLY: the database must not be reached")


PAID_KEY: Final = "test-only-not-a-key"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("settings", "dry_run", "cap", "message"),
    [
        (
            {"environment": "local", "embeddings_provider": "voyage", "voyage_api_key": PAID_KEY},
            True,
            None,
            "a dry run needs the fake embedder and the fake answer model",
        ),
        (
            {"environment": "local", "llm_provider": "anthropic", "anthropic_api_key": PAID_KEY},
            True,
            None,
            "a dry run needs the fake embedder and the fake answer model",
        ),
        ({}, False, None, "a live run needs a cap"),
    ],
    ids=["dry-voyage", "dry-anthropic", "live-without-cap"],
)
async def test_execute_never_pays_without_live_and_its_cap(
    monkeypatch: pytest.MonkeyPatch,
    settings: dict[str, Any],
    dry_run: bool,
    cap: Decimal | None,
    message: str,
) -> None:
    monkeypatch.setattr(harness, "create_async_engine", no_database)

    with pytest.raises(HarnessRefused, match=message):
        await execute_with(make_settings(**settings), dry_run=dry_run, cap=CostCap(cap=cap))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mapping", "settings", "message"),
    [
        ({SUNRISE: SUNRISE, PAWS: EVAL[PAWS]}, {}, "sunrise-clinic is not the derived eval"),
        ({SUNRISE: EVAL[PAWS], PAWS: EVAL[PAWS]}, {}, "is not the derived eval business id of"),
        (
            EVAL,
            {
                "environment": "production",
                "llm_provider": "anthropic",
                "anthropic_api_key": PAID_KEY,
                "embeddings_provider": "voyage",
                "voyage_api_key": PAID_KEY,
            },
            "--fresh never runs with environment production",
        ),
    ],
    ids=["dataset-id", "other-business", "production"],
)
async def test_fresh_deletes_only_derived_eval_ids_and_never_in_production(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, str],
    settings: dict[str, Any],
    message: str,
) -> None:
    monkeypatch.setattr(harness, "create_async_engine", no_database)
    live = bool(settings)

    with pytest.raises(HarnessRefused, match=message):
        await execute_with(
            make_settings(**settings),
            dry_run=not live,
            cap=CostCap(cap=Decimal("0.50") if live else None),
            target=target(mapping=mapping),
            fresh=True,
        )


class _Engine:
    async def dispose(self) -> None:
        """Nothing to release."""


class _Database:
    def __init__(self, engine: Any) -> None:
        self.engine = engine

    async def verify_role(self) -> None:
        """The role check passes (no database)."""


@pytest.mark.anyio
@pytest.mark.parametrize("step", ["registering", "ingest"])
async def test_a_ctrl_c_before_the_questions_ends_the_run_partial_not_lost(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    class Store:
        def __init__(self, database: Any) -> None:
            self.database = database

        async def business_exists(self, business_id: str) -> bool:
            if step == "registering":
                raise asyncio.CancelledError
            return True

    async def interrupted_ingest(*args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(harness, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(harness, "Database", _Database)
    monkeypatch.setattr(harness, "PostgresEvalStore", Store)
    monkeypatch.setattr(harness, "ingest_corpus", interrupted_ingest)

    outcome = await execute_with(make_settings(), dry_run=True, cap=CostCap(cap=None))

    assert (outcome.status, outcome.stop, outcome.results) == (
        "partial",
        INTERRUPTED_BEFORE_QUESTIONS,
        [],
    )


# ── execute: the run's final status (mutation audit M13a/M13b; decided 2026-10-22) ───────────


class ThenFails:
    """The dry-run answer model, except that answer call `fail_on` (from 1) raises `error`."""

    def __init__(self, fail_on: int, error: BaseException) -> None:
        self.fail_on = fail_on
        self.error = error
        self.requests = 0

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests += 1
        if self.requests == self.fail_on:
            raise self.error
        return await DryRunAnswerProvider().send(request)

    async def aclose(self) -> None:
        """Nothing to release."""


@dataclass
class Offline:
    """`execute` with its database, ingest and wiring replaced: the real question loop runs over
    `harness_parts` (scripted search, prepared rows, the capped gateway) with `execute`'s cap."""

    script: list[list[SearchHit]] = field(default_factory=list)
    provider: Any = None
    ingest: str = "done"
    """"done", "cap-refused" (a request over the cap) or "stop-rule" (a paid call tripped one)."""
    ingested: list[CostCap] = field(default_factory=list)
    foreign: list[SearchHit] = field(default_factory=list)
    """Hits whose document rows are Paws's: a leak into a Sunrise question's search."""

    async def ingest_corpus(self, *args: Any, should_stop: Any, **kwargs: Any) -> Any:
        cap = self.ingested[0]
        if self.ingest == "cap-refused":
            raise CapReached("TEST-ONLY an embedding request not sent")
        if self.ingest == "stop-rule":
            cap.stop_after("TEST-ONLY an embedding request was billed beyond its token bound")
        return (IngestCount(SUNRISE, EVAL[SUNRISE], 9, 9, 40, 0),)

    @asynccontextmanager
    async def wired(
        self, settings: Settings, database: Any, config: RunConfig, cap: CostCap, **kwargs: Any
    ) -> AsyncIterator[harness.Parts]:
        self.ingested.append(cap)
        with harness_parts(
            self.script, list(EVAL.values()), provider=self.provider, cap=cap, retry_policy=NO_RETRY
        ) as parts:
            for hits in self.script:
                owned(parts, SUNRISE, *hits)
            owned(parts, PAWS, *self.foreign)
            yield harness.Parts(
                kb=cast(KbService, None),
                answer=parts.answer,
                recorder=parts.recorder,
                embedding_model=FAKE_SPEC.model,
            )


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Offline:
    class Store:
        def __init__(self, database: Any) -> None:
            self.database = database

        async def business_exists(self, business_id: str) -> bool:
            return True

    async def visible(*args: Any, **kwargs: Any) -> dict[str, tuple[RetrievedChunk, ...]]:
        return VISIBLE

    stand_in = Offline()
    monkeypatch.setattr(harness, "create_async_engine", lambda *a, **k: _Engine())
    monkeypatch.setattr(harness, "Database", _Database)
    monkeypatch.setattr(harness, "PostgresEvalStore", Store)
    monkeypatch.setattr(harness, "stored_visible", visible)
    monkeypatch.setattr(harness, "ingest_corpus", stand_in.ingest_corpus)
    monkeypatch.setattr(harness, "wired", stand_in.wired)
    return stand_in


def answered(offline: Offline, count: int) -> list[EvalQuestion]:
    """`count` TEST-ONLY Sunrise questions, each with a hit that finds its group."""
    offline.script += [[hit(HOURS, 0.9)] for _ in range(count)]
    return [question(f"t-{n}") for n in range(1, count + 1)]


async def execute_offline(
    offline: Offline, questions: list[EvalQuestion], cap: Decimal | None = None, **kwargs: Any
) -> harness.RunOutcome:
    prepared = PreparedMetrics()
    for q in questions:
        prepared.prepare(q.id, hit=True, rr=Fraction(1))
    return await execute_with(
        make_settings(),
        dry_run=True,
        cap=CostCap(cap=cap),
        target=target(questions=tuple(questions)),
        functions=prepared.functions(),
        **kwargs,
    )


@pytest.mark.anyio
async def test_execute_is_partial_when_the_cap_stops_the_question_loop(offline: Offline) -> None:
    questions = answered(offline, 2)

    outcome = await execute_offline(offline, questions, cap=Decimal("0.001"))

    assert outcome.status == "partial"
    assert outcome.stop is not None
    assert outcome.stop.startswith("question t-1: an answer-model call not sent")
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fail_on", "status", "outcomes"),
    [(2, "complete", ["answered", "error"]), (1, "partial", ["error", "not_run"])],
    ids=["last-question", "first-of-two"],
)
async def test_a_stop_rule_tripped_by_the_last_question_completes_the_run_with_its_stop(
    offline: Offline, fail_on: int, status: str, outcomes: list[str]
) -> None:
    # Decided 2026-10-22, as M1: the last question tripped the stop rule, so none is left unrun:
    # complete (exit 0), the stop kept for run.json and "not for the note".
    offline.provider = ThenFails(fail_on, InvalidRequest())
    questions = answered(offline, 2)

    outcome = await execute_offline(offline, questions)

    assert outcome.status == status
    assert [r.outcome for r in outcome.results] == outcomes
    assert outcome.stop is not None
    assert outcome.stop.startswith(f"after question t-{fail_on}: an answer call ended invalid_")


@pytest.mark.anyio
@pytest.mark.parametrize("ingest_only", [False, True], ids=["run", "ingest-only"])
@pytest.mark.parametrize("ingest", ["cap-refused", "stop-rule"])
async def test_execute_is_partial_when_the_cap_stops_the_ingest(
    offline: Offline, ingest: str, ingest_only: bool
) -> None:
    offline.ingest = ingest
    questions = answered(offline, 1)

    outcome = await execute_offline(offline, questions, ingest_only=ingest_only)

    assert (outcome.status, outcome.results) == ("partial", [])
    assert outcome.stop is not None and outcome.stop.startswith("ingest: TEST-ONLY")
    assert len(offline.script) == 1  # no question was asked


@pytest.mark.anyio
async def test_execute_ends_aborted_on_a_leak_with_the_questions_kept(offline: Offline) -> None:
    leaked = hit(OTHER, 0.95, version="2026-09")
    offline.script.append([leaked])
    offline.foreign.append(leaked)
    questions = [question("t-1"), question("t-2")]

    outcome = await execute_offline(offline, questions)

    assert outcome.status == "aborted_leak"
    assert outcome.stop is not None
    assert outcome.stop.startswith("CROSS-BUSINESS LEAK: question t-1, rank 1")
    assert [r.outcome for r in outcome.results] == ["not_run", "not_run"]


@pytest.mark.anyio
async def test_an_ingest_only_run_without_a_stop_is_complete(offline: Offline) -> None:
    outcome = await execute_offline(offline, answered(offline, 1), ingest_only=True)

    assert (outcome.status, outcome.stop, outcome.results) == ("complete", None, [])
    assert [c.chunks_written for c in outcome.ingest] == [40]


# ── wired: embeddings priced from the catalog (mutation audit M11b) ──────────────────────────


class NeverEmbeds:
    """A stand-in for the Voyage embedder: its model id and dimension, and nothing to send."""

    model = "voyage-4"
    dimension = 1024

    async def embed(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("TEST-ONLY: wiring must not embed")

    async def aclose(self) -> None:
        """Nothing to release."""


@pytest.mark.anyio
async def test_wired_prices_each_embedding_request_at_the_catalogs_list_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Paid Settings with TEST-ONLY keys; the embedder and the answer model are stand-ins, so no
    # client is built and nothing can reach a network. The price must be voyage-4's, not $0.
    prices: list[Decimal] = []

    class SpyEmbedder(CappedEmbedder):
        def __init__(self, inner: Any, price_per_mtok: Decimal, cap: CostCap) -> None:
            prices.append(price_per_mtok)
            super().__init__(inner, price_per_mtok, cap)

    monkeypatch.setattr(harness, "CappedEmbedder", SpyEmbedder)
    monkeypatch.setattr(harness, "build_embedder", lambda settings, catalog: NeverEmbeds())
    monkeypatch.setattr(harness, "build_provider", lambda settings: DryRunAnswerProvider())
    settings = make_settings(
        environment="local",
        embeddings_provider="voyage",
        voyage_api_key=PAID_KEY,
        llm_provider="anthropic",
        anthropic_api_key=PAID_KEY,
    )

    async with harness.wired(
        settings,
        cast(Database, _Database(_Engine())),
        CONFIG,
        CostCap(cap=Decimal("0.50")),
        dry_run=False,
        records=lambda trace_id: None,
    ) as parts:
        assert parts.embedding_model == "voyage-4"

    catalog_price = load_embedding_catalog().price("voyage-4")
    assert prices == [catalog_price] and catalog_price == Decimal("0.06")


# ── No question text in any log record, at any level (mutation audit M14b) ─────────────────


@pytest.mark.anyio
async def test_no_log_record_at_any_level_holds_question_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every logger of the package at DEBUG, the INFO-pinned record loggers included (gateway,
    # embeddings, answer), so a debug line with the text would be caught too.
    caplog.set_level(logging.DEBUG)
    for name in list(logging.root.manager.loggerDict):
        if name.startswith("vectapilot_api"):
            caplog.set_level(logging.DEBUG, logger=name)
    script: list[list[SearchHit]] = []
    with harness_parts(script, list(EVAL.values())) as parts:
        script += [
            owned(parts, SUNRISE, hit(HOURS, 0.9)),  # answered: a model call
            owned(parts, SUNRISE, hit(OTHER, 0.1)),  # below the threshold: no model call
        ]
        prepared = PreparedMetrics()
        prepared.prepare("t-1", hit=True, rr=Fraction(1))
        questions = [
            question("t-1", text=SENTINEL_QUESTION),
            question("t-2", text=f"{SENTINEL_QUESTION} again", evidence=(), expected="dont_know"),
        ]

        outcome, _, _ = await run(parts, questions, prepared)

    assert [r.outcome for r in outcome.results] == ["answered", "dont_know"]
    assert {r.name for r in caplog.records} >= {"vectapilot_api.gateway", "vectapilot_api.answer"}
    for record in caplog.records:
        assert "zq7" not in record.getMessage(), record.name
        assert "zq7" not in repr(vars(record)), record.name
