"""M3 PR 5: a hybrid run's summary (`evalrun/report.py`): each list's rows, the headroom count, the
FTS-only counts, the safety outcomes per business and the per-stage latency, all recomputed from
the folder's files. H2 (M2's metrics, written) is called as `summarize` calls it; every expected
count, mean and id below is worked out by hand from the fixture's ranks.

Every run folder is TEST-ONLY, in pytest's tmp_path (never in evals/reports or evals/datasets),
with ids, ranks and groups only: no question, chunk or answer text.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from tests.evalrun.helpers import CONFIG, HYBRID_CONFIG, HYBRID_RETRIEVAL, PAWS, SUNRISE, run_info
from vectapilot_api.answer.service import RAG_ANSWER
from vectapilot_api.answer.types import RagAnswerRecord, RetrievalRecord
from vectapilot_api.evalrun.report import (
    ANSWER_LOG_FILE,
    LATENCY_LABEL,
    RESULTS_FILE,
    RUN_FILE,
    MetricRow,
    Summary,
    SummaryError,
    render_markdown,
    summarize,
)
from vectapilot_api.evalrun.scoring import (
    H2_METRICS,
    ComponentHit,
    ComponentLists,
    HarnessTime,
    HitRef,
    QuestionResult,
)

NAMESPACES: Final = run_info().namespaces
NO_TIME: Final = HarnessTime(vector_ms=0.0, text_ms=0.0, fuse_ms=0.0)


def _groups(chunk: str) -> tuple[int, ...]:
    return (0,) if chunk.endswith("-rel") else ()


def hybrid_result(
    question_id: str,
    business: str = SUNRISE,
    *,
    vector: Sequence[str],
    text: Sequence[str],
    fused: Sequence[str],
    evidence: Sequence[str] | None = (),
    retrieval: str = "scored",
    category: str = "faq",
    outcome: str = "answered",
    harness: HarnessTime = NO_TIME,
    trace: int = 0,
) -> QuestionResult:
    """A TEST-ONLY hybrid result. Chunk names are local to the question (`c3`, `rel`); a chunk
    named `rel` finds group 0. The stored per-question values are H2's on the fused list (so the
    file is consistent); every expected number in the tests is worked out by hand instead."""

    def name(chunk: str) -> str:
        return f"{question_id}-{chunk}"

    def legs(chunks: Sequence[str]) -> tuple[ComponentHit, ...]:
        return tuple(
            ComponentHit(rank=rank, chunk_id=name(c), groups=_groups(name(c)))
            for rank, c in enumerate(chunks, start=1)
        )

    hits = tuple(
        HitRef(
            rank=rank,
            chunk_id=name(c),
            document_id=f"doc-{business}",
            source_id="faq",
            version="2026-10",
            chunk_index=rank,
            page=None,
            score=0.5,
            groups=_groups(name(c)),
        )
        for rank, c in enumerate(fused, start=1)
    )
    values: dict[str, Any] = {
        "question_id": question_id,
        "business_id": business,
        "eval_business_id": NAMESPACES[business],
        "category": category,
        "subtype": None,
        "language_group": "en",
        "author_type": "team",
        "split_group": None,
        "expected_outcome": "handoff" if category == "safety" else "answered",
        "n_groups": 0 if retrieval == "not_scored" else 1,
        "retrieval": retrieval,
        "unscorable_groups": (0,) if retrieval == "unscorable" else (),
        "hits": hits,
        "evidence_chunk_ids": None if evidence is None else tuple(name(c) for c in evidence),
        "outcome": outcome,
        "error": "ModelUnavailable" if outcome == "error" else None,
        "trace_id": f"{trace:032x}",
        "components": ComponentLists(vector=legs(vector), text=legs(text), harness=harness),
    }
    result = QuestionResult.model_validate(values)
    if retrieval == "scored":
        ranked = result.ranked()
        stored: dict[str, Any] = {
            "hit_at_k": H2_METRICS.hit_at_k(ranked, 5),
            "all_groups_at_k": H2_METRICS.all_groups_at_k(ranked, 5),
            "reciprocal_rank": str(H2_METRICS.reciprocal_rank(ranked, 5)),
        }
        held = result.ranked_evidence()
        if held is not None:
            stored["evidence_complete"] = H2_METRICS.all_groups_at_k(held, CONFIG.search_k)
        result = QuestionResult.model_validate(values | stored)
    return result


def c(*numbers: int) -> list[str]:
    return [f"c{n}" for n in numbers]


def five() -> list[QuestionResult]:
    """Five scored questions (h-1..h-3 Sunrise, h-4 and h-5 Paws), one unscorable (u-1), two
    safety questions without evidence (one each), one not run. First relevant ranks:

    | id  | vector | text | fused | fused length |
    | h-1 | 3      | none | 7     | 12           |
    | h-2 | 1      | 2    | 2     | 3            |
    | h-3 | none   | 1    | 11    | 11           |
    | h-4 | 6      | none | 10    | 10           |
    | h-5 | none   | none | none  | 2            |
    """
    return [
        hybrid_result(
            "h-1",
            vector=[*c(1, 2), "rel", *c(3, 4, 5, 6, 7, 8, 9)],
            text=c(10, 11, 12, 13),
            fused=[*c(1, 2, 10, 11, 12, 13), "rel", *c(3, 4, 5, 6, 7)],
            evidence=c(1, 10),
            trace=1,
        ),
        hybrid_result(
            "h-2",
            vector=["rel", "c1"],
            text=["c2", "rel"],
            fused=["c2", "rel", "c1"],
            evidence=["c2", "rel"],
            trace=2,
        ),
        hybrid_result(
            "h-3",
            vector=c(*range(1, 11)),
            text=["rel", "c11"],
            fused=[*c(*range(1, 11)), "rel"],
            evidence=c(1),
            trace=3,
        ),
        hybrid_result(
            "h-4",
            PAWS,
            vector=[*c(1, 2, 3, 4, 5), "rel"],
            text=c(6, 7, 8, 9),
            fused=[*c(*range(1, 10)), "rel"],
            evidence=None,
            outcome="error",
            trace=4,
        ),
        hybrid_result(
            "h-5", PAWS, vector=c(1), text=c(2), fused=c(1, 2), outcome="dont_know", trace=5
        ),
        hybrid_result(
            "u-1",
            retrieval="unscorable",
            vector=c(1),
            text=c(2),
            fused=c(1, 2),
            outcome="dont_know",
            trace=6,
        ),
        hybrid_result(
            "s-safe",
            retrieval="not_scored",
            category="safety",
            vector=c(1),
            text=[],
            fused=c(1),
            outcome="handoff",
            trace=7,
        ),
        hybrid_result(
            "p-safe",
            PAWS,
            retrieval="not_scored",
            category="safety",
            vector=[],
            text=c(1),
            fused=c(1),
            evidence=c(1),
            outcome="answered",
            trace=8,
        ),
        QuestionResult.model_validate(
            {
                "question_id": "n-run",
                "business_id": SUNRISE,
                "eval_business_id": NAMESPACES[SUNRISE],
                "category": "faq",
                "subtype": None,
                "language_group": "en",
                "author_type": "team",
                "split_group": None,
                "expected_outcome": "answered",
                "n_groups": 1,
                "retrieval": "not_run",
                "outcome": "not_run",
            }
        ),
    ]


def write(
    tmp_path: Path,
    results: Sequence[QuestionResult],
    *,
    records: Sequence[RagAnswerRecord] | None = None,
    **changes: Any,
) -> Path:
    folder = tmp_path / "test-only-hybrid"
    folder.mkdir(parents=True)
    info = run_info(questions_selected=len(results), **({"config": HYBRID_CONFIG} | changes))
    (folder / RUN_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(
        "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8"
    )
    if records is not None:
        lines = [
            json.dumps({"logger": "vectapilot_api.answer", RAG_ANSWER: r.model_dump(mode="json")})
            for r in records
        ]
        other = json.dumps({"logger": "vectapilot_api.answer", "message": "another line"})
        (folder / ANSWER_LOG_FILE).write_text("\n".join([other, *lines]) + "\n", "utf-8")
    return folder


def record(
    trace: int, *, embed: float = 1.0, vector: float = 10.0, text: float = 20.0, **more: Any
) -> RagAnswerRecord:
    stages: dict[str, Any] = {
        "mode": "hybrid",
        "candidate_k": 20,
        "context_k": 6,
        "rrf_k": 60,
        "vector_hits": 1,
        "text_hits": 1,
        "fused_hits": 1,
        "fts_only_hits": 0,
        "fts_only_evidence": 0,
        "embed_ms": embed,
        "transaction_ms": more.pop("transaction", 100.0),
        "vector_ms": vector,
        "text_ms": text,
        "fuse_ms": more.pop("fuse", 4.0),
    }
    return RagAnswerRecord(
        business_id=NAMESPACES[SUNRISE],
        trace_id=f"{trace:032x}",
        prompt_version="rag-answer.1",
        outcome="dont_know",
        reason="below_threshold",
        error=None,
        hits=1,
        evidence=0,
        cited=0,
        top_score=0.5,
        min_score=0.3,
        model_called=False,
        latency_ms=more.pop("latency", 500.0),
        retrieval=RetrievalRecord.model_validate(stages),
    )


def summary(tmp_path: Path, **options: Any) -> Summary:
    return summarize(write(tmp_path, five(), **options), H2_METRICS)


# ── Each list, the headroom and the FTS-only counts ──────────────────────────────────────────


def test_each_list_gets_its_own_rows_through_h2_at_the_metric_k(tmp_path: Path) -> None:
    rows: dict[tuple[str, str], MetricRow] = {
        (item.ranked_list, item.row.group): item.row for item in summary(tmp_path).by_list
    }

    # hit@5 by hand from the table in `five()`: vector h-1 (3), h-2 (1); text h-2 (2), h-3 (1);
    # fused h-2 (2) only. MRR@5: vector (1/3 + 1) / 5 = 4/15; text (1/2 + 1) / 5 = 3/10; fused
    # (1/2) / 5 = 1/10. u-1 is unscorable in every list (outside n, shown next to it).
    expected = {
        ("vector", "all"): (2, 5, "4/15", {"1": 1, "3": 1, "none": 3}),
        ("text", "all"): (2, 5, "3/10", {"1": 1, "2": 1, "none": 3}),
        ("fused", "all"): (1, 5, "1/10", {"2": 1, "none": 4}),
        # Sunrise h-1..h-3: vector 2 of 3, (1/3 + 1) / 3 = 4/9; text 2 of 3, (1/2 + 1) / 3 = 1/2;
        # fused 1 of 3, (1/2) / 3 = 1/6. Paws h-4, h-5: nothing in any top 5.
        ("vector", f"business {SUNRISE}"): (2, 3, "4/9", {"1": 1, "3": 1, "none": 1}),
        ("text", f"business {SUNRISE}"): (2, 3, "1/2", {"1": 1, "2": 1, "none": 1}),
        ("fused", f"business {SUNRISE}"): (1, 3, "1/6", {"2": 1, "none": 2}),
        ("vector", f"business {PAWS}"): (0, 2, "0", {"none": 2}),
        ("text", f"business {PAWS}"): (0, 2, "0", {"none": 2}),
        ("fused", f"business {PAWS}"): (0, 2, "0", {"none": 2}),
    }
    assert set(rows) == set(expected)
    for key, (x, n, mrr, ranks) in expected.items():
        row = rows[key]
        assert (row.hit.x, row.hit.n, row.mrr.value) == (x, n, mrr), key
        assert {r: count for r, count in row.mrr.ranks.items() if count} == ranks, key
        assert row.unscorable == (1 if key[1] in ("all", f"business {SUNRISE}") else 0), key


def test_the_headroom_count_is_hit_at_10_and_not_hit_at_5_on_the_fused_list(
    tmp_path: Path,
) -> None:
    head = summary(tmp_path).headroom

    # Fused first relevant ranks: h-1 7 and h-4 10 (in the top 10, not the top 5); h-2 2 (top 5);
    # h-3 11 (past 10); h-5 none.
    assert head is not None
    assert head.questions == ("h-1", "h-4")
    assert (head.total.x, head.total.n, head.total.low) == (2, 5, None)  # counts only
    assert {b: (s.x, s.n) for b, s in head.by_business.items()} == {PAWS: (1, 2), SUNRISE: (1, 3)}


def test_the_headroom_asks_h2_for_hit_at_10_and_hit_at_5_on_each_scored_fused_list(
    tmp_path: Path,
) -> None:
    asked: list[tuple[str, int]] = []

    def hit_at_k(record: Any, k: int) -> bool:
        asked.append((record.question_id, k))
        return H2_METRICS.hit_at_k(record, k)

    spy = H2_METRICS.__class__(**(vars(H2_METRICS) | {"hit_at_k": hit_at_k}))
    summarize(write(tmp_path, five()), spy)

    for question_id in ("h-1", "h-2", "h-3", "h-4", "h-5"):
        assert (question_id, 10) in asked and (question_id, 5) in asked
    assert not any(qid in ("u-1", "s-safe", "p-safe", "n-run") and k == 10 for qid, k in asked)


def test_no_headroom_count_from_a_fused_list_shorter_than_10(tmp_path: Path) -> None:
    short = HYBRID_CONFIG.model_copy(
        update={"retrieval": HYBRID_RETRIEVAL.model_copy(update={"candidate_k": 9})}
    )
    results = [r for r in five() if r.question_id in ("h-2", "h-5")]

    found = summarize(write(tmp_path, results, config=short), H2_METRICS)

    assert found.headroom is None
    assert found.headroom_note == "not computed: the fused list holds at most 9 hits, fewer than 10"
    assert "- Headroom (D13): not computed: the fused list holds at most 9 hits" in render_markdown(
        found
    )


def test_fts_only_hits_are_those_not_in_the_vector_legs_list(tmp_path: Path) -> None:
    fts = summary(tmp_path).fts_only

    # Fused hits not in the vector list: h-1 c10..c13 (4), h-2 c2 (1), h-3 rel (1), h-4 c6..c9
    # (4), h-5 c2 (1), u-1 c2 (1), s-safe none, p-safe c1 (1): 13 over 8 questions that ran.
    # Evidence not in the vector list: h-1 c10, h-2 c2, p-safe c1 (h-4 has none: its answer
    # step failed).
    assert fts is not None
    assert (fts.questions, fts.fused_hits, fts.evidence_hits) == (8, 13, 3)
    assert fts.evidence_questions == ("h-1", "h-2", "p-safe")
    assert fts.by_business == {PAWS: (6, 1), SUNRISE: (7, 2)}


def test_the_safety_rows_are_counted_by_outcome_per_business(tmp_path: Path) -> None:
    found = summary(tmp_path)

    assert found.safety_outcomes == {
        PAWS: {"answered": 1, "dont_know": 0, "handoff": 0, "error": 0},
        SUNRISE: {"answered": 0, "dont_know": 0, "handoff": 1, "error": 0},
    }
    markdown = render_markdown(found)
    assert "| Business | Ran | answered | dont_know | handoff | error |" in markdown
    assert f"| {PAWS} | 1 | 1 | 0 | 0 | 0 |" in markdown
    assert f"| {SUNRISE} | 1 | 0 | 0 | 1 | 0 |" in markdown


def test_a_business_with_no_safety_question_keeps_a_row_of_zeros(tmp_path: Path) -> None:
    results = [r for r in five() if r.question_id != "p-safe"]

    found = summarize(write(tmp_path, results), H2_METRICS)

    assert found.safety_outcomes[PAWS] == {"answered": 0, "dont_know": 0, "handoff": 0, "error": 0}


def test_the_hybrid_summary_says_what_it_scored_and_where_each_count_comes_from(
    tmp_path: Path,
) -> None:
    markdown = render_markdown(summary(tmp_path))

    assert "## Retrieval: hit@5 and MRR@5 on the fused list (candidate k = 20, context k = 6)" in (
        markdown
    )
    assert "On the fused list the answer kept (its top 6 reached the evidence gate)" in markdown
    assert (
        "| Retrieval | hybrid · candidate k 20 (each leg, and the fused list) · RRF k 60 ·"
        f" language map {HYBRID_RETRIEVAL.language_map_sha256[:12]} · the top 6 fused hits reach"
        " the evidence gate |"
    ) in markdown
    assert "### Each list at hit@5 (hybrid)" in markdown
    assert "| vector leg | all | 5 | 1 | 2 of 5 (95 % Wilson" in markdown
    assert "| text leg | all | 5 | 1 | 2 of 5 (95 % Wilson" in markdown
    assert "| fused | all | 5 | 1 | 1 of 5 (95 % Wilson" in markdown
    assert (
        "- FTS-only hits (in the text leg's list, not the vector leg's), over the 8 question(s)"
        " whose search finished: 13 in the fused lists, 3 among the hits the prompt held (D7's"
        " rescues), in h-1, h-2, p-safe."
    ) in markdown
    assert (
        "- Headroom (D13: scored questions with H2's hit@10 and not its hit@5 on the fused list):"
        " 2 of 5 (counts only), in h-1, h-4. Per business: paws-and-co 1 of 2; sunrise-clinic 1"
        " of 3."
    ) in markdown
    assert "| M3 spend |" not in markdown  # a dry run: never M3's spend


def test_a_vector_summary_has_no_hybrid_section_and_says_vector(tmp_path: Path) -> None:
    results = [r.model_copy(update={"components": None}) for r in five() if r.question_id == "h-2"]

    markdown = render_markdown(summarize(write(tmp_path, results, config=CONFIG), H2_METRICS))

    assert "| Retrieval | vector (no retrieval block) |" in markdown
    assert "## Retrieval: hit@5 and MRR@5 (search k = 6)" in markdown
    assert "Each list at" not in markdown and "Headroom" not in markdown
    assert "Per-stage latency" not in markdown and "FTS-only" not in markdown


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"dry_run": False}, "hybrid (retrieval block)"),
        ({"dry_run": False, "config": CONFIG, "m3_spend": True}, "vector, named by --m3-spend"),
    ],
    ids=["hybrid", "named-vector"],
)
def test_a_live_run_of_m3_says_it_counts_toward_m3s_spend(
    tmp_path: Path, changes: dict[str, Any], why: str
) -> None:
    results = [r for r in five() if r.question_id == "h-2"]
    if "config" in changes:
        results = [r.model_copy(update={"components": None}) for r in results]

    markdown = render_markdown(summarize(write(tmp_path, results, **changes), H2_METRICS))

    assert f"| M3 spend | counts toward M3's $5.00 ceiling (D15 (iii)): {why} |" in markdown


# ── Per-stage latency ────────────────────────────────────────────────────────────────────────


def test_each_stage_is_its_record_minus_the_harnesss_own_time(tmp_path: Path) -> None:
    own = HarnessTime(vector_ms=3.0, text_ms=5.0, fuse_ms=0.5)
    result = hybrid_result(
        "h-2", vector=["rel"], text=["rel"], fused=["rel"], evidence=["rel"], harness=own, trace=2
    )
    rec = record(2, embed=7.0, vector=10.0, text=20.0, fuse=4.0, transaction=100.0, latency=900.0)

    found = summarize(write(tmp_path, [result], records=[rec]), H2_METRICS)

    # By hand: vector 10 - 3, text 20 - 5, fuse 4 - 0.5, transaction 100 - 8.5, answer 900 - 8.5,
    # embed 7 (no harness inside), harness 3 + 5 + 0.5.
    medians = {row.stage: row.latency.median_ms for row in found.latency if row.latency}
    assert medians == {
        "embed (the question)": 7.0,
        "vector leg": 7.0,
        "text leg": 15.0,
        "fuse": 3.5,
        "transaction (model check, both legs, fuse, commit)": 91.5,
        "answer (rag_answer latency_ms, model call included)": 891.5,
        "harness: leak-check lookups and checks (subtracted above)": 8.5,
    }
    markdown = render_markdown(found)
    assert (
        f"Milliseconds, {LATENCY_LABEL}: never compared with business-context §6's p95 < 6 s."
        in (markdown)
    )
    assert "| vector leg | 1 | 7.0 | 7.0 | n < 20: none | 7.0 |" in markdown
    assert "Dry run: the embedder and the answer model are fakes." in markdown


def test_a_p95_appears_only_from_20_questions(tmp_path: Path) -> None:
    results = [
        hybrid_result(
            f"q-{n:02d}",
            retrieval="not_scored",
            vector=c(1),
            text=[],
            fused=c(1),
            outcome="dont_know",
            trace=n,
        )
        for n in range(1, 21)
    ]
    # Vector legs of 1, 2, ..., 20 ms: inclusive p95 of 1..20 = 19.05 (statistics.quantiles).
    records = [record(n, vector=float(n)) for n in range(1, 21)]

    found = summarize(write(tmp_path, results, records=records), H2_METRICS)

    vector = next(row.latency for row in found.latency if row.stage == "vector leg")
    assert vector is not None and vector.n == 20
    assert vector.p95_ms == pytest.approx(19.05)
    found19 = summarize(write(tmp_path / "19", results[:19], records=records[:19]), H2_METRICS)
    vector19 = next(row.latency for row in found19.latency if row.stage == "vector leg")
    assert vector19 is not None and vector19.p95_ms is None


def test_a_harness_time_longer_than_its_stage_is_refused(tmp_path: Path) -> None:
    own = HarnessTime(vector_ms=11.0, text_ms=0.0, fuse_ms=0.0)
    result = hybrid_result(
        "h-2", vector=["rel"], text=["rel"], fused=["rel"], evidence=["rel"], harness=own, trace=2
    )

    with pytest.raises(SummaryError, match="question h-2, vector leg: the harness's own time"):
        summarize(write(tmp_path, [result], records=[record(2, vector=10.0)]), H2_METRICS)


def test_float_noise_below_a_nanosecond_is_clamped_to_zero(tmp_path: Path) -> None:
    own = HarnessTime(vector_ms=10.0 + 1e-9, text_ms=0.0, fuse_ms=0.0)
    result = hybrid_result(
        "h-2", vector=["rel"], text=["rel"], fused=["rel"], evidence=["rel"], harness=own, trace=2
    )

    found = summarize(write(tmp_path, [result], records=[record(2, vector=10.0)]), H2_METRICS)

    vector = next(row.latency for row in found.latency if row.stage == "vector leg")
    assert vector is not None and vector.min_ms == 0.0


def test_without_the_answer_log_or_a_record_the_latency_says_so(tmp_path: Path) -> None:
    without = summarize(write(tmp_path / "none", five()), H2_METRICS)
    assert without.latency == ()
    assert without.latency_note == f"not shown: this folder has no {ANSWER_LOG_FILE}"

    some = summarize(write(tmp_path / "some", five(), records=[record(2)]), H2_METRICS)
    assert some.latency_note == (
        "7 question(s) that ran have no hybrid rag_answer record: h-1, h-3, h-4, h-5, u-1,"
        " s-safe, p-safe"
    )
    embed = next(row.latency for row in some.latency if row.stage.startswith("embed"))
    assert embed is not None and embed.n == 1


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not json", "line 2 is not a JSON object"),
        (json.dumps({RAG_ANSWER: {"business_id": "x"}}), "line 2 is not a valid rag_answer"),
    ],
    ids=["not-json", "invalid-record"],
)
def test_a_bad_answer_log_line_is_refused_by_number(
    tmp_path: Path, line: str, message: str
) -> None:
    folder = write(tmp_path, five())
    (folder / ANSWER_LOG_FILE).write_text(f"{json.dumps({'message': 'other'})}\n{line}\n", "utf-8")

    with pytest.raises(SummaryError, match=message):
        summarize(folder, H2_METRICS)


def test_two_records_of_one_trace_id_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SummaryError, match="a second record of one trace id"):
        summarize(write(tmp_path, five(), records=[record(2), record(2)]), H2_METRICS)


# ── The files must agree with each other ─────────────────────────────────────────────────────


def _changed(question_id: str, **update: Any) -> list[QuestionResult]:
    return [r.model_copy(update=update) if r.question_id == question_id else r for r in five()]


def _with_components(question_id: str, **update: Any) -> list[QuestionResult]:
    out = []
    for result in five():
        if result.question_id == question_id and result.components is not None:
            components = result.components.model_copy(update=update)
            result = result.model_copy(update={"components": components})
        out.append(result)
    return out


@pytest.mark.parametrize(
    ("results", "changes", "message"),
    [
        (
            _changed("h-2", components=None),
            {},
            "question h-2: a hybrid run's question that ran has no component lists",
        ),
        (five, {"config": CONFIG}, "question h-1: component lists in a vector run"),
        (
            _with_components(
                "h-2", vector=(ComponentHit(rank=2, chunk_id="h-2-rel", groups=(0,)),)
            ),
            {},
            "question h-2: the vector list's ranks are not 1, 2, 3",
        ),
        (
            _with_components("h-2", text=(ComponentHit(rank=1, chunk_id="h-2-c2", groups=(1,)),)),
            {},
            "question h-2: one chunk finds other groups in another list",
        ),
        (
            _with_components("h-2", text=(ComponentHit(rank=1, chunk_id="h-2-rel", groups=(0,)),)),
            {},
            "question h-2: a fused hit is in neither leg's list",
        ),
        (
            five,
            {
                "config": HYBRID_CONFIG.model_copy(
                    update={
                        "search_k": 5,
                        "retrieval": HYBRID_RETRIEVAL.model_copy(update={"candidate_k": 11}),
                    }
                )
            },
            "question h-1: the fused list is longer than the candidate k",
        ),
    ],
    ids=[
        "ran-without-lists",
        "lists-in-vector-run",
        "ranks",
        "regrouped-chunk",
        "fused-hit-in-no-leg",
        "longer-than-candidate-k",
    ],
)
def test_lists_that_disagree_with_run_json_or_each_other_are_refused(
    tmp_path: Path, results: Any, changes: dict[str, Any], message: str
) -> None:
    rows = results() if callable(results) else results
    folder = write(tmp_path, rows, **changes)

    with pytest.raises(SummaryError, match=message):
        summarize(folder, H2_METRICS)


def test_the_unscorable_and_safety_rows_carry_no_text(tmp_path: Path) -> None:
    folder = write(tmp_path, five())

    lines = (folder / RESULTS_FILE).read_text("utf-8").splitlines()

    for line in lines:
        payload = json.loads(line)
        assert set(payload) <= set(QuestionResult.model_fields)
        assert "content" not in line and "question" not in payload
