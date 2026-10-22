"""One question's retrieval, scored by content (`evalrun/scoring.py`): the leak check, the
unscorable check, the record kept per question, and the H2 calls (stand-ins here; the real H2
waits behind `PENDING_HAND_WRITE`)."""

import uuid
from fractions import Fraction
from typing import Any, Final

import pytest

from tests.canary.read_paths import pending_hand_writes
from tests.evalrun.helpers import (
    PAWS,
    SUNRISE,
    SUNRISE_HOURS,
    SUNRISE_PARKING,
    PreparedMetrics,
    hit,
    question,
)
from vectapilot_api.evalrun.scoring import (
    H2_METRICS,
    CrossBusinessLeak,
    QuestionResult,
    base_fields,
    check_metric_k,
    first_relevant_rank,
    hit_refs,
    not_run,
    retrieved_chunks,
    score_retrieval,
    unscorable_groups,
)
from vectapilot_api.evalset import metrics
from vectapilot_api.evalset.relevance import RetrievedChunk

EVAL_ID: Final = "sunrise-clinic-ev-0123456789"
MAPPING: Final = {SUNRISE: EVAL_ID, PAWS: "paws-and-co-ev-9876543210"}
HOURS_TEXT: Final = (
    "## Opening hours\n\nMonday to Friday: 8:00 to 20:00. Saturday: 9:00 to 14:00.\nSunday: closed."
)


# ── The leak check ───────────────────────────────────────────────────────────────────────────


def test_each_hit_takes_its_business_from_its_document_row() -> None:
    hits = [hit(HOURS_TEXT), hit("TEST-ONLY unrelated text")]
    rows = {h.document_id: EVAL_ID for h in hits}

    chunks = retrieved_chunks(question("t-1"), EVAL_ID, hits, rows, MAPPING)

    assert [c.business_id for c in chunks] == [SUNRISE, SUNRISE]
    assert [(c.source_id, c.version) for c in chunks] == [("faq", "2026-10")] * 2


@pytest.mark.parametrize(
    "row",
    ["paws-and-co-ev-9876543210", "paws-and-co", SUNRISE, None],
    ids=["other-eval-business", "other-real-business", "dataset-id-not-namespace", "not-visible"],
)
def test_a_hit_from_any_other_business_is_a_leak_named_by_ids_only(row: str | None) -> None:
    leaked = hit(f"TEST-ONLY {row} text")
    hits = [hit(HOURS_TEXT), leaked]
    rows = {hits[0].document_id: EVAL_ID} | ({} if row is None else {leaked.document_id: row})

    with pytest.raises(CrossBusinessLeak) as raised:
        retrieved_chunks(question("t-1"), EVAL_ID, hits, rows, MAPPING)

    message = str(raised.value)
    assert message.startswith("CROSS-BUSINESS LEAK: question t-1, rank 2")
    assert str(leaked.document_id) in message
    assert "TEST-ONLY" not in message  # never chunk text
    assert (raised.value.question_id, raised.value.rank) == ("t-1", 2)


def test_a_mapping_that_disagrees_with_the_question_is_never_trusted() -> None:
    hits = [hit(HOURS_TEXT)]
    wrong = {PAWS: EVAL_ID}  # the eval id maps back to another dataset business

    with pytest.raises(CrossBusinessLeak):
        retrieved_chunks(question("t-1"), EVAL_ID, hits, {hits[0].document_id: EVAL_ID}, wrong)


# ── Unscorable at this config ────────────────────────────────────────────────────────────────


def stored(content: str, *, business: str = SUNRISE, version: str = "2026-10") -> RetrievedChunk:
    return RetrievedChunk(business_id=business, source_id="faq", version=version, content=content)


def test_a_group_whose_key_a_stored_chunk_holds_is_scorable() -> None:
    assert unscorable_groups(question("t-1"), [stored(HOURS_TEXT)]) == ()


def test_a_group_no_stored_chunk_holds_is_unscorable_never_dropped() -> None:
    # The hours anchor straddles two chunks here (as a small chunk size can make it): neither
    # stored chunk holds all of it.
    straddled = [stored("Saturday: 9:00 to"), stored("14:00. Sunday: closed.")]
    two_groups = question("t-2", evidence=((SUNRISE_PARKING,), (SUNRISE_HOURS,)))

    assert unscorable_groups(
        two_groups, [*straddled, stored("free parking for 12 cars behind the building")]
    ) == (1,)


def test_any_key_of_a_group_is_enough_and_other_businesses_never_count() -> None:
    either = question("t-3", evidence=((SUNRISE_PARKING, SUNRISE_HOURS),))

    assert unscorable_groups(either, [stored(HOURS_TEXT)]) == ()
    assert unscorable_groups(either, [stored(HOURS_TEXT, business=PAWS)]) == (0,)
    assert unscorable_groups(either, [stored(HOURS_TEXT, version="2025-10")]) == (0,)


# ── The metric's k ───────────────────────────────────────────────────────────────────────────


def test_a_metric_k_above_the_search_k_is_refused() -> None:
    check_metric_k(5, 5)
    check_metric_k(1, 50)
    with pytest.raises(ValueError, match=r"metric k \(6\) must be at most the search's k \(5\)"):
        check_metric_k(6, 5)


@pytest.mark.parametrize("k", [0, True, 5.0])
def test_a_bad_k_is_refused_either_way(k: Any) -> None:
    with pytest.raises(ValueError, match="k must be"):
        check_metric_k(k, 5)
    with pytest.raises(ValueError, match="k must be"):
        check_metric_k(5, k)


# ── Scoring one question (H2 stand-ins) ──────────────────────────────────────────────────────


def chunks_for(*contents: str) -> list[RetrievedChunk]:
    return [stored(content) for content in contents]


def test_a_question_without_evidence_is_not_scored_and_h2_is_never_called() -> None:
    prepared = PreparedMetrics()
    no_evidence = question("t-4", evidence=(), expected="dont_know", category="out_of_scope")

    score = score_retrieval(
        no_evidence,
        chunks_for(HOURS_TEXT),
        None,
        (),
        metric_k=5,
        search_k=5,
        functions=prepared.functions(),
    )

    assert (score.status, score.hit_at_k, score.reciprocal_rank) == ("not_scored", None, None)
    assert prepared.calls == []


def test_an_unscorable_question_is_its_own_count_and_h2_is_never_called() -> None:
    prepared = PreparedMetrics()

    score = score_retrieval(
        question("t-5"),
        chunks_for(HOURS_TEXT),
        None,
        (0,),
        metric_k=5,
        search_k=5,
        functions=prepared.functions(),
    )

    assert (score.status, score.unscorable, score.hit_at_k) == ("unscorable", (0,), None)
    assert prepared.calls == []


def test_a_scored_question_gets_h2s_values_at_the_metric_k_and_evidence_at_the_search_k() -> None:
    prepared = PreparedMetrics(metric_k=3)
    prepared.prepare("t-6", hit=True, rr=Fraction(1, 2), all_groups=False)
    prepared.evidence_all["t-6"] = True

    score = score_retrieval(
        question("t-6"),
        chunks_for("TEST-ONLY other", HOURS_TEXT),
        chunks_for(HOURS_TEXT),
        (),
        metric_k=3,
        search_k=5,
        functions=prepared.functions(),
    )

    assert (score.status, score.hit_at_k, score.all_groups_at_k) == ("scored", True, False)
    assert (score.reciprocal_rank, score.evidence_complete) == (Fraction(1, 2), True)
    assert {(name, k) for name, _, k in prepared.calls} == {
        ("hit_at_k", 3),
        ("all_groups_at_k", 3),
        ("reciprocal_rank", 3),
        ("all_groups_at_k", 5),
    }


def test_an_empty_hit_list_is_scored_as_a_miss_never_skipped() -> None:
    prepared = PreparedMetrics()
    prepared.prepare("t-7", hit=False, rr=Fraction(0))

    score = score_retrieval(
        question("t-7"), [], [], (), metric_k=5, search_k=5, functions=prepared.functions()
    )

    assert (score.status, score.hit_at_k, score.reciprocal_rank) == ("scored", False, Fraction(0))


def test_the_default_metrics_are_shesha_s_h2_functions() -> None:
    assert H2_METRICS.hit_at_k is metrics.hit_at_k
    assert H2_METRICS.all_groups_at_k is metrics.all_groups_at_k
    assert H2_METRICS.reciprocal_rank is metrics.reciprocal_rank
    assert H2_METRICS.mean_hit_at_k is metrics.mean_hit_at_k
    assert H2_METRICS.mean_reciprocal_rank is metrics.mean_reciprocal_rank


@pytest.mark.parametrize(
    "marks", [pytest.param(None, marks=pending_hand_writes("retrieval_metrics"))], ids=["h2"]
)
def test_with_h2_a_first_relevant_hit_at_rank_2_is_a_hit_with_rr_one_half(marks: None) -> None:
    # Ranks: 1 = an unrelated chunk (finds no group), 2 = the hours chunk (finds group 0).
    # hit@5 = True (a relevant hit within the top 5); RR@5 = 1/2 (first relevant rank 2); with
    # one group, all groups = hit@5 = True. Evidence = only the hours chunk: all groups = True.
    score = score_retrieval(
        question("t-8"),
        chunks_for("TEST-ONLY other", HOURS_TEXT),
        chunks_for(HOURS_TEXT),
        (),
        metric_k=5,
        search_k=5,
        functions=H2_METRICS,
    )

    assert (score.hit_at_k, score.reciprocal_rank) == (True, Fraction(1, 2))
    assert (score.all_groups_at_k, score.evidence_complete) == (True, True)


# ── The record ───────────────────────────────────────────────────────────────────────────────


def test_first_relevant_rank_reads_h2s_reciprocal_rank() -> None:
    assert first_relevant_rank(Fraction(0)) is None
    assert first_relevant_rank(Fraction(1)) == 1
    assert first_relevant_rank(Fraction(1, 3)) == 3
    with pytest.raises(ValueError, match="0 or 1/r"):
        first_relevant_rank(Fraction(2, 3))


def test_a_result_line_holds_no_text_and_rebuilds_its_ranked_groups() -> None:
    q = question("t-9", text="TEST-ONLY sentinel question text zq7")
    hits = [hit("TEST-ONLY other chunk text"), hit(HOURS_TEXT)]
    chunks = retrieved_chunks(q, EVAL_ID, hits, {h.document_id: EVAL_ID for h in hits}, MAPPING)
    result = QuestionResult.model_validate(
        {
            **base_fields(q, EVAL_ID),
            "retrieval": "scored",
            "hits": hit_refs(hits, chunks, q),
            "evidence_chunk_ids": (str(hits[1].chunk_id),),
            "outcome": "answered",
        }
    )

    line = result.model_dump_json()
    assert "zq7" not in line and "Opening hours" not in line and "TEST-ONLY" not in line
    assert result.ranked().groups_by_rank == ((), (0,))
    evidence = result.ranked_evidence()
    assert evidence is not None and evidence.groups_by_rank == ((0,),)
    assert [h.rank for h in result.hits] == [1, 2]


def test_a_question_the_run_never_reached_is_kept_as_not_run() -> None:
    result = not_run(question("t-10"), EVAL_ID)

    assert (result.retrieval, result.outcome, result.hits) == ("not_run", "not_run", ())
    assert result.ranked_evidence() is None
    assert uuid.UUID(int=0)  # (the record holds ids as text; nothing else to check here)
