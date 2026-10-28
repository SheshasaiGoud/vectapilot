"""The run folder's summary (`evalrun/report.py`): every number recomputed from results.jsonl
through H2 (stand-ins with prepared values here; the two central tests also run with shesha's
H2 once it is written), intervals, breakdowns, status lines, labels. Every run folder here is
TEST-ONLY and lives in pytest's tmp_path."""

import json
import math
import random
import re
import sys
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.canary.read_paths import pending_hand_writes
from tests.evalrun.helpers import (
    CONFIG,
    HYBRID_RETRIEVAL,
    PAWS,
    RUN_ID,
    SUNRISE,
    PreparedMetrics,
    position_weighted,
    question,
    run_info,
)
from vectapilot_api.answer.prompt import PROMPT_VERSION
from vectapilot_api.answer.service import ANSWER_ALIAS
from vectapilot_api.evalrun import report
from vectapilot_api.evalrun.intervals import bootstrap_interval, wilson_interval
from vectapilot_api.evalrun.report import (
    LABELS_FILE,
    RESULTS_FILE,
    RUN_FILE,
    ConfigInfo,
    RetrievalInfo,
    SummaryError,
    config_fingerprint,
    label_template,
    language_map_canonical,
    language_map_sha256,
    render_label_sheet,
    render_markdown,
    run_fingerprint_payload,
    status_of,
    summarize,
    write_summary,
)
from vectapilot_api.evalrun.scoring import (
    H2_METRICS,
    AnswerLine,
    HitRef,
    MetricFunctions,
    QuestionResult,
    SourceRef,
)
from vectapilot_api.evalset.ranking import RankedRelevance
from vectapilot_api.gateway.catalog import load_catalog


def result(
    question_id: str,
    *,
    business: str = SUNRISE,
    language: str = "en",
    category: str = "faq",
    retrieval: str = "scored",
    groups: tuple[tuple[int, ...], ...] = ((0,),),
    n_groups: int = 1,
    hit: bool | None = True,
    rr: str | None = "1",
    all_groups: bool | None = None,
    evidence: tuple[str, ...] | None = ("c1",),
    evidence_complete: bool | None = True,
    outcome: str = "answered",
    expected: str = "answered",
    reason: str | None = None,
    split_group: str | None = None,
) -> QuestionResult:
    scored = retrieval == "scored"
    return QuestionResult(
        question_id=question_id,
        business_id=business,
        eval_business_id=f"{business}-ev-aaaaaaaaaa",
        category=category,
        subtype=None,
        language_group=language,
        author_type="team",
        split_group=split_group,
        expected_outcome=expected,
        n_groups=n_groups,
        retrieval=retrieval,
        hits=tuple(
            HitRef(
                rank=rank,
                chunk_id=f"c{rank}",
                document_id=f"d{rank}",
                source_id="faq",
                version="2026-10",
                chunk_index=rank,
                page=None,
                score=0.9,
                groups=found,
            )
            for rank, found in enumerate(groups, start=1)
        ),
        evidence_chunk_ids=evidence if outcome != "not_run" else None,
        hit_at_k=hit if scored else None,
        all_groups_at_k=(hit if all_groups is None else all_groups) if scored else None,
        reciprocal_rank=rr if scored else None,
        evidence_complete=evidence_complete if scored and evidence is not None else None,
        outcome=outcome,
        reason=reason,
        trace_id="0123456789abcdef0123456789abcdef",
    )


def prepared_for(
    results: list[QuestionResult],
    means: Mapping[frozenset[str], tuple[Fraction, Fraction]] | None = None,
) -> PreparedMetrics:
    """H2's stand-in, prepared with exactly the values each result line stores, and with the
    hand-written means (`TWELVE_MEANS` unless given): (mean hit@5, MRR@5) per set of ids."""
    prepared = PreparedMetrics(metric_k=CONFIG.metric_k)
    for ids, (hit, mrr) in (TWELVE_MEANS if means is None else means).items():
        prepared.prepare_mean(ids, hit=hit, mrr=mrr)
    for r in results:
        if r.retrieval == "scored":
            assert r.hit_at_k is not None and r.reciprocal_rank is not None
            prepared.prepare(
                r.question_id,
                hit=r.hit_at_k,
                rr=Fraction(r.reciprocal_rank),
                all_groups=bool(r.all_groups_at_k),
            )
            if r.evidence_complete is not None:
                prepared.evidence_all[r.question_id] = r.evidence_complete
    return prepared


def write_run(
    tmp_path: Path, results: list[QuestionResult], labels: str | None = None, **changes: Any
) -> Path:
    folder = tmp_path / RUN_ID
    folder.mkdir()
    info = run_info(questions_selected=len(results), **changes)
    (folder / RUN_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(
        "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8"
    )
    if labels is not None:
        (folder / LABELS_FILE).write_text(labels, encoding="utf-8")
    return folder


def twelve() -> list[QuestionResult]:
    """12 scored questions (sunrise 8: en 6 and hi 2; paws 4), plus one of each other count.

    Every stored value is what a correct H2 gives for the stored groups, so the same file serves
    the stand-in and H2: misses (q-03, q-07, q-11) have one irrelevant hit (hit False, RR 0,
    prompt evidence incomplete); q-02, q-05 and q-09 find their group first at rank 2, 3 and 5
    (RR 1/2, 1/3, 1/5) and the prompt held every hit; every other hit is at rank 1 (RR 1).
    """
    misses = {"q-03", "q-07", "q-11"}
    found_at = {"q-02": ((), (0,)), "q-05": ((), (), (0,)), "q-09": ((), (), (), (), (0,))}
    ranks = {"q-02": "1/2", "q-05": "1/3", "q-09": "1/5"}
    out: list[QuestionResult] = []
    for i in range(1, 13):
        qid = f"q-{i:02d}"
        miss = qid in misses
        groups = ((),) if miss else found_at.get(qid, ((0,),))
        out.append(
            result(
                qid,
                business=SUNRISE if i <= 8 else PAWS,
                language="hi" if i in (1, 2) else "en",
                category="safety" if i in (4, 10) else ("policy" if i in (5, 6) else "faq"),
                groups=groups,
                hit=not miss,
                rr="0" if miss else ranks.get(qid, "1"),
                evidence=tuple(f"c{rank}" for rank in range(1, len(groups) + 1)),
                evidence_complete=not miss,
                outcome="handoff" if i == 4 else "answered",
                expected="handoff" if i in (4, 10) else "answered",
            )
        )
    out.append(
        result(
            "q-multi",
            category="multi_hop",
            groups=((0,), (1,)),
            n_groups=2,
            all_groups=True,
            evidence_complete=False,
        )
    )
    out += [
        result(
            "q-none",
            retrieval="not_scored",
            n_groups=0,
            groups=(),
            outcome="dont_know",
            expected="dont_know",
            reason="below_threshold",
            category="out_of_scope",
        ),
        result("q-unscorable", retrieval="unscorable", outcome="answered"),
        result("q-skipped", retrieval="not_run", outcome="not_run", groups=()),
    ]
    return out


def qids(*numbers: int, multi: bool = False) -> frozenset[str]:
    return frozenset({f"q-{n:02d}" for n in numbers} | ({"q-multi"} if multi else set()))


TWELVE_MEANS: Final[dict[frozenset[str], tuple[Fraction, Fraction]]] = {
    # (mean hit@5, MRR@5) for each scored group of `twelve()`, worked out by hand from its stored
    # values (misses q-03, q-07, q-11; RR 1/2 at q-02, 1/3 at q-05, 1/5 at q-09; q-multi RR 1).
    # all: 10 hits of 13; (7 + 1/2 + 1/3 + 1/5) / 13 = (241/30) / 13.
    qids(*range(1, 13), multi=True): (Fraction(10, 13), Fraction(241, 390)),
    # paws (q-09..q-12): 3 of 4; (1/5 + 1 + 0 + 1) / 4.
    qids(9, 10, 11, 12): (Fraction(3, 4), Fraction(11, 20)),
    # sunrise (q-01..q-08, q-multi): 7 of 9; (5 + 1/2 + 1/3) / 9 = (35/6) / 9.
    qids(*range(1, 9), multi=True): (Fraction(7, 9), Fraction(35, 54)),
    # en (q-03..q-12, q-multi): 8 of 11; (6 + 1/3 + 1/5) / 11 = (98/15) / 11.
    qids(*range(3, 13), multi=True): (Fraction(8, 11), Fraction(98, 165)),
    # hi (q-01, q-02): 2 of 2; (1 + 1/2) / 2.
    qids(1, 2): (Fraction(1), Fraction(3, 4)),
    # faq (q-01, 02, 03, 07, 08, 09, 11, 12): 5 of 8; (3 + 1/2 + 1/5) / 8 = (37/10) / 8.
    qids(1, 2, 3, 7, 8, 9, 11, 12): (Fraction(5, 8), Fraction(37, 80)),
    # policy (q-05, q-06): 2 of 2; (1/3 + 1) / 2.
    qids(5, 6): (Fraction(1), Fraction(2, 3)),
    # multi_hop (q-multi) and safety (q-04, q-10; counts only, its MRR is never asked): all 1.
    qids(multi=True): (Fraction(1), Fraction(1)),
    qids(4, 10): (Fraction(1), Fraction(1)),
}

WITH_H2: Final = pytest.param(H2_METRICS, marks=pending_hand_writes("retrieval_metrics"), id="h2")


def metrics_for(which: MetricFunctions | None, results: list[QuestionResult]) -> MetricFunctions:
    """`None`: the stand-in prepared for `results`; otherwise H2 itself."""
    return prepared_for(results).functions() if which is None else which


@pytest.mark.parametrize("which", [pytest.param(None, id="stand-in"), WITH_H2])
def test_every_number_is_recomputed_through_h2_with_its_interval(
    tmp_path: Path, which: MetricFunctions | None
) -> None:
    results = twelve()

    summary = summarize(write_run(tmp_path, results), metrics_for(which, results))

    rows = {row.group: row for row in summary.retrieval}
    everything = rows["all"]
    # 13 scored (12 + the multi_hop one): 10 hits (3 misses).
    assert (everything.hit.x, everything.hit.n) == (10, 13)
    wilson = wilson_interval(10, 13)
    assert (everything.hit.low, everything.hit.high) == (wilson.low, wilson.high)
    # MRR: ten hits at rank 1 except q-02 (1/2), q-05 (1/3), q-09 (1/5); three zeros.
    # (7·1 + 1/2 + 1/3 + 1/5) / 13 = (7 + 31/30) / 13 = 241/390.
    assert everything.mrr.value == "241/390"
    assert everything.mrr.ranks == {"1": 7, "2": 1, "3": 1, "4": 0, "5": 1, "none": 3}
    assert everything.mrr.low is not None and everything.mrr.high is not None
    assert Fraction(everything.mrr.low) <= Fraction(everything.mrr.high)
    assert (summary.scored, summary.selected) == (13, 16)
    assert (summary.not_scored, summary.unscorable, summary.not_run) == (
        ("q-none",),
        ("q-unscorable",),
        ("q-skipped",),
    )
    # Breakdowns: per business, per language, per category, in that order.
    assert [row.group for row in summary.retrieval] == [
        "all",
        "business paws-and-co",
        "business sunrise-clinic",
        "language en",
        "language hi",
        "category faq",
        "category policy",
        "category multi_hop",
        "category safety",
    ]
    assert (rows["business sunrise-clinic"].hit.x, rows["business sunrise-clinic"].hit.n) == (7, 9)
    assert rows["business sunrise-clinic"].mrr.low is None  # n = 9 < 10: no bootstrap


@pytest.mark.parametrize("which", [pytest.param(None, id="stand-in"), WITH_H2])
def test_the_bootstrap_resamples_questions_through_h2s_mean_with_the_recorded_seed(
    tmp_path: Path, which: MetricFunctions | None
) -> None:
    # With the stand-in, each resample's statistic is a prepared value, not a mean (helpers):
    # this checks the plumbing (questions resampled with the recorded seed and count, renamed,
    # handed to the function), not an MRR.
    results = twelve()
    functions = metrics_for(which, results)

    summary = summarize(write_run(tmp_path, results), functions)

    scored = [r.ranked() for r in results if r.retrieval == "scored"]
    expected = bootstrap_interval(
        scored,
        lambda sample: functions.mean_reciprocal_rank(
            [
                RankedRelevance(f"{r.question_id}~{i}", r.n_groups, r.groups_by_rank)
                for i, r in enumerate(sample)
            ],
            5,
        ),
        seed=20261022,
        resamples=200,
    )
    everything = summary.retrieval[0]
    assert (everything.mrr.low, everything.mrr.high) == (str(expected.low), str(expected.high))


def oracle(rrs: list[Fraction], *, seed: int, resamples: int) -> tuple[Fraction, Fraction]:
    """The percentile bootstrap written out here, independently of `intervals.py`: `resamples`
    draws of len(rrs) questions with `int(Random(seed).random() * n)`, the stand-in's statistic
    on each, sorted; at 95 % the values of rank ceil(B/40) and ceil(39B/40), counted from 1."""
    rng = random.Random(seed)  # noqa: S311 (not crypto)
    n = len(rrs)
    values = sorted(
        position_weighted([rrs[int(rng.random() * n)] for _ in range(n)]) for _ in range(resamples)
    )
    return values[math.ceil(resamples / 40) - 1], values[math.ceil(resamples * 39 / 40) - 1]


def test_the_bootstrap_resamples_each_rows_own_questions_with_run_jsons_seed_and_count(
    tmp_path: Path,
) -> None:
    # The mutation audit's M7b (seed), M7c (resampling unit) and M7d (resample count): run.json
    # names a seed and a count that are not the defaults, and the stand-in statistic depends on
    # the whole resample, so each of the three changes the interval.
    results = twelve()
    seed, resamples = 7, 300  # ranks 8 and 293 at 95 %

    summary = summarize(
        write_run(tmp_path, results, bootstrap_seed=seed, bootstrap_resamples=resamples),
        prepared_for(results).functions(),
    )

    rows = {row.group: row.mrr for row in summary.retrieval}
    scored = [r for r in results if r.retrieval == "scored"]  # results.jsonl order
    for group, members in [
        ("all", scored),  # 13 questions
        ("language en", [r for r in scored if r.language_group == "en"]),  # 11
    ]:
        rrs = [Fraction(r.reciprocal_rank or "") for r in members]
        low, high = oracle(rrs, seed=seed, resamples=resamples)
        assert (rows[group].low, rows[group].high) == (str(low), str(high)), group
        # The check can tell them apart: another seed, count or set of questions moves it.
        assert oracle(rrs, seed=20261022, resamples=resamples) != (low, high)
        assert oracle(rrs, seed=seed, resamples=2000) != (low, high)
    # A row resamples its own questions, never the run's: all 13 give another interval than en's.
    all_rrs = [Fraction(r.reciprocal_rank or "") for r in scored]
    assert oracle(all_rrs, seed=seed, resamples=resamples) != (
        Fraction(rows["language en"].low or ""),
        Fraction(rows["language en"].high or ""),
    )


def test_the_summary_says_mrr_intervals_are_approximate_at_small_n(tmp_path: Path) -> None:
    results = twelve()

    markdown = render_markdown(
        summarize(write_run(tmp_path, results), prepared_for(results).functions())
    )

    note = "approximate at small n (percentile bootstrap undercovers)"
    assert f"MRR bootstrap intervals: {note}." in markdown
    assert f"with the MRR value from n = 10: {note}" in markdown


def test_safety_is_counts_only_never_a_rate(tmp_path: Path) -> None:
    results = twelve()

    summary = summarize(write_run(tmp_path, results), prepared_for(results).functions())

    safety = next(row for row in summary.retrieval if row.group == "category safety")
    assert safety.counts_only
    assert (safety.hit.x, safety.hit.n, safety.hit.low, safety.hit.high) == (2, 2, None, None)
    assert safety.mrr.value is None
    assert {b: (s.x, s.n) for b, s in summary.safety_handoff.items()} == {
        PAWS: (0, 1),
        SUNRISE: (1, 1),
    }
    markdown = render_markdown(summary)
    assert "| category safety | 2 | 0 | 2 of 2 (counts only) | counts only |" in markdown
    assert "paws-and-co handoff 0 of 1; sunrise-clinic handoff 1 of 1" in markdown


def test_a_percentage_appears_only_from_ten_questions(tmp_path: Path) -> None:
    results = twelve()

    markdown = render_markdown(
        summarize(write_run(tmp_path, results), prepared_for(results).functions())
    )

    assert "| all | 13 | 1 | 10 of 13, 77 % (95 % Wilson" in markdown
    assert "| business sunrise-clinic | 9 | 1 | 7 of 9 (95 % Wilson" in markdown


def test_all_groups_and_evidence_complete_are_their_own_counts(tmp_path: Path) -> None:
    results = twelve()

    summary = summarize(write_run(tmp_path, results), prepared_for(results).functions())

    assert summary.all_groups is not None
    assert (summary.all_groups.x, summary.all_groups.n) == (1, 1)
    assert {c: (s.x, s.n) for c, s in summary.evidence_complete.items()} == {"multi_hop": (0, 1)}
    assert "Evidence complete, multi_hop" in render_markdown(summary)


def test_answers_are_counted_against_the_expected_outcome(tmp_path: Path) -> None:
    results = twelve()

    summary = summarize(write_run(tmp_path, results), prepared_for(results).functions())

    assert summary.outcomes == {
        "answered": {"answered": 12},
        "dont_know": {"dont_know": 1},
        "handoff": {"handoff": 1, "answered": 1},
    }
    assert summary.reasons == {"below_threshold": 1}


def test_an_edited_results_file_is_refused(tmp_path: Path) -> None:
    results = twelve()
    prepared = prepared_for(results)
    prepared.hits["q-01"] = False  # H2 now disagrees with the stored line

    with pytest.raises(SummaryError, match="question q-01: the stored metric values differ"):
        summarize(write_run(tmp_path, results), prepared.functions())


def test_an_h2_whose_mean_disagrees_with_its_count_is_refused(tmp_path: Path) -> None:
    results = twelve()
    prepared = prepared_for(results)
    prepared.mean_hit_offset = Fraction(1, 1000)

    with pytest.raises(SummaryError, match="mean_hit_at_k disagrees with its hit_at_k count"):
        summarize(write_run(tmp_path, results), prepared.functions())


def test_a_results_file_with_a_missing_line_is_refused(tmp_path: Path) -> None:
    folder = write_run(tmp_path, twelve())
    (folder / RUN_FILE).write_text(
        run_info(questions_selected=99).model_dump_json(), encoding="utf-8"
    )

    with pytest.raises(SummaryError, match="99 question"):
        summarize(folder, PreparedMetrics().functions())


def test_an_h2_whose_mrr_disagrees_with_its_reciprocal_ranks_is_refused(tmp_path: Path) -> None:
    results = twelve()
    prepared = prepared_for(results)
    prepared.mean_rr_offset = Fraction(1, 1000)

    with pytest.raises(SummaryError, match="mean_reciprocal_rank disagrees with its"):
        summarize(write_run(tmp_path, results), prepared.functions())


def test_a_first_relevant_rank_beyond_the_metric_k_is_refused(tmp_path: Path) -> None:
    # A broken H2 (stand-in): RR 1/6 at k = 5, i.e. a first relevant rank of 6, outside 1..5.
    six = result("q-six", groups=((), (), (), (), (), (0,)), rr="1/6", evidence=None)
    means = {frozenset({"q-six"}): (Fraction(1), Fraction(1, 6))}

    with pytest.raises(SummaryError, match=r"first relevant rank lies outside 1\.\.5"):
        summarize(write_run(tmp_path, [six]), prepared_for([six], means).functions())


def few(*rrs: str) -> list[QuestionResult]:
    """Scored TEST-ONLY results with these reciprocal ranks, first relevant hit at 1/RR."""
    out = []
    for number, rr in enumerate(rrs, start=1):
        first = 0 if rr == "0" else Fraction(rr).denominator
        groups = ((),) if first == 0 else ((),) * (first - 1) + ((0,),)
        out.append(result(f"q-{number:02d}", groups=groups, hit=first > 0, rr=rr, evidence=None))
    return out


def test_below_ten_questions_no_mrr_value_is_shown_only_the_rank_counts(tmp_path: Path) -> None:
    results = few("1", "1/2", "0")
    # Hand-worked: hit@5 2 of 3; MRR (1 + 1/2 + 0) / 3 = 1/2.
    means = {frozenset({"q-01", "q-02", "q-03"}): (Fraction(2, 3), Fraction(1, 2))}

    summary = summarize(write_run(tmp_path, results), prepared_for(results, means).functions())
    markdown = render_markdown(summary)

    row = next(line for line in markdown.splitlines() if line.startswith("| all |"))
    assert "n < 10: no MRR value; see first relevant rank" in row
    assert "1: 1 · 2: 1 · 3: 0 · 4: 0 · 5: 0 · none: 1" in row
    assert re.search(r"\d\.\d{3}", row) is None  # no MRR decimal anywhere in the row
    assert summary.retrieval[0].mrr.value == "1/2"  # summary.json keeps the exact value


def test_a_zero_width_bootstrap_is_called_degenerate_never_a_95_percent_interval(
    tmp_path: Path,
) -> None:
    results = few(*["1"] * 10)  # every resample's stand-in statistic is 1: zero width
    ids = frozenset(r.question_id for r in results)

    markdown = render_markdown(
        summarize(
            write_run(tmp_path, results),
            prepared_for(results, {ids: (Fraction(1), Fraction(1))}).functions(),
        )
    )

    row = next(line for line in markdown.splitlines() if line.startswith("| all |"))
    assert (
        "1.000 (bootstrap degenerate: its 2.5th and 97.5th percentiles are both 1.000, no"
        " interval; see the hit@5 Wilson interval)" in row
    )
    assert "95 % bootstrap" not in markdown


def test_every_retrieval_row_shows_its_unscorable_questions(tmp_path: Path) -> None:
    # twelve() has one unscorable question (sunrise, en, faq); add one more whose category has
    # no scored question at all, so its row is all unscorable.
    results = [*twelve(), result("q-only-unscorable", retrieval="unscorable", category="ambiguous")]

    summary = summarize(write_run(tmp_path, results), prepared_for(results).functions())

    rows = {row.group: (row.hit.n, row.unscorable) for row in summary.retrieval}
    assert rows["all"] == (13, 2)
    assert rows["business sunrise-clinic"] == (9, 2)
    assert rows["business paws-and-co"] == (4, 0)
    assert rows["language en"] == (11, 2)
    assert rows["category faq"] == (8, 1)
    assert rows["category ambiguous"] == (0, 1)
    markdown = render_markdown(summary)
    assert "| category ambiguous | 0 | 1 | none scored | none scored |  |" in markdown
    assert json.loads(summary.model_dump_json())["retrieval"][0]["unscorable"] == 2  # summary.json


def test_a_conservative_hit_rate_counts_every_unscorable_question_as_a_miss(
    tmp_path: Path,
) -> None:
    # Decided 2026-10-22: next to n, a line where unscorable questions count as misses, so a chunk
    # config cannot look better by making its hard questions unscorable.
    results = [*twelve(), result("q-only-unscorable", retrieval="unscorable", category="ambiguous")]

    summary = summarize(write_run(tmp_path, results), prepared_for(results).functions())

    rows = {row.group: row.conservative for row in summary.retrieval}
    # all: 10 hits of 13 scored + 2 unscorable; sunrise 7 of 9 + 2; paws 3 of 4 + 0.
    assert (rows["all"].x, rows["all"].n) == (10, 15)
    assert (rows["all"].low, rows["all"].high) == (
        wilson_interval(10, 15).low,
        wilson_interval(10, 15).high,
    )
    assert (rows["business sunrise-clinic"].x, rows["business sunrise-clinic"].n) == (7, 11)
    assert (rows["business paws-and-co"].x, rows["business paws-and-co"].n) == (3, 4)
    assert (rows["category ambiguous"].x, rows["category ambiguous"].n) == (0, 1)
    assert rows["category safety"].low is None  # counts only, as its row
    assert (
        "- Conservative hit@5, every unscorable question counted as a miss: all 10 of 15, 67 %"
        " (95 % Wilson " in render_markdown(summary)
    )


def hits_then_misses(hits: int, misses: int) -> tuple[list[QuestionResult], PreparedMetrics]:
    """TEST-ONLY scored Sunrise results, `hits` at rank 1 then `misses`, with their hand-written
    means: hit@5 hits/(hits + misses), and the same for MRR (every RR is 1 or 0)."""
    results = few(*(["1"] * hits + ["0"] * misses))
    rate = Fraction(hits, hits + misses)
    ids = frozenset(r.question_id for r in results)
    return results, prepared_for(results, {ids: (rate, rate)})


@pytest.mark.parametrize(
    ("hits", "misses", "verdict", "shown"),
    [
        (9, 1, "met", False),  # 90 %, Wilson 60 % to 98 %: met, not shown at 95 % confidence
        (40, 0, "met", True),  # Wilson lower end 40/(40 + z²) = 91.2 %: shown too
        (8, 2, "not met", False),
        (8, 1, "no verdict: n < 10 (counts only)", None),
    ],
    ids=["met-not-shown", "met-shown", "not-met", "small-n"],
)
def test_a_target_is_met_by_the_point_estimate_and_says_when_95_percent_does_not_show_it(
    tmp_path: Path, hits: int, misses: int, verdict: str, shown: bool | None
) -> None:
    results, prepared = hits_then_misses(hits, misses)

    summary = summarize(write_run(tmp_path, results, dry_run=False), prepared.functions())

    rows = {(row.metric, row.group): row for row in summary.targets}
    everything = rows[("hit@5", "all")]
    assert (everything.verdict, everything.shown_at_95, everything.target) == (
        verdict,
        shown,
        "9/10",
    )
    assert rows[("hit@5", "business sunrise-clinic")].verdict == verdict
    assert rows[("hit@5", "business paws-and-co")].verdict == "no verdict: none scored"
    assert rows[("correctness", "all")].verdict.startswith("no verdict: ")  # not labelled
    expected = {
        (True, "met"): "met (point estimate; the 95 % Wilson lower end reaches it too)",
        (False, "met"): "met (point estimate; not shown at 95 % confidence)",
    }.get((bool(shown), verdict), verdict)
    assert f"| hit@5 | all | ≥ 90 % | {hits} of {hits + misses}" in render_markdown(summary)
    assert f" | {expected} |" in render_markdown(summary)


@pytest.mark.parametrize(
    ("changes", "verdict"),
    [
        ({}, "no verdict: dry run"),
        ({"dry_run": False, "status": "partial", "stop": "x"}, "no verdict: status partial"),
        (
            {"dry_run": False, "config": CONFIG.model_copy(update={"metric_k": 3})},
            "no verdict: this run's metric k is 3, the target is hit@5",
        ),
    ],
    ids=["dry", "partial", "metric-k-3"],
)
def test_no_target_verdict_from_a_dry_or_partial_run_or_another_k(
    tmp_path: Path, changes: dict[str, Any], verdict: str
) -> None:
    results, prepared = hits_then_misses(10, 0)
    prepared.metric_k = changes.get("config", CONFIG).metric_k

    summary = summarize(write_run(tmp_path, results, **changes), prepared.functions())

    assert {row.verdict for row in summary.targets if row.metric == "hit@5"} == {verdict}


def test_correctness_gets_a_verdict_only_once_every_answer_is_labelled(tmp_path: Path) -> None:
    results, prepared = hits_then_misses(10, 0)
    ids = {r.question_id for r in results}
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    partly = summarize(
        write_run(
            tmp_path / "a",
            results,
            "\n".join(filled(results, ids).splitlines()[:9]),  # 9 of 10 labelled
            dry_run=False,
        ),
        prepared.functions(),
    )
    labelled = summarize(
        write_run(tmp_path / "b", results, filled(results, ids), dry_run=False),
        prepared.functions(),
    )

    assert {r.verdict for r in partly.targets if r.metric == "correctness"} == {
        "no verdict: 1 answer(s) not labelled yet"
    }
    rows = {(r.metric, r.group): r for r in labelled.targets}
    assert rows[("correctness", "all")].verdict == "met"
    assert (rows[("correctness", "all")].stat.x, rows[("correctness", "all")].stat.n) == (10, 10)


def test_evidence_complete_names_the_questions_it_could_not_count(tmp_path: Path) -> None:
    results = [
        result("q-a", category="multi_hop", groups=((0,), (1,)), n_groups=2, evidence=("c1", "c2")),
        result(
            "q-b",
            category="multi_hop",
            groups=((0,), (1,)),
            n_groups=2,
            evidence=None,
            outcome="error",
        ),
    ]
    means = {frozenset({"q-a", "q-b"}): (Fraction(1), Fraction(1))}

    summary = summarize(write_run(tmp_path, results), prepared_for(results, means).functions())

    assert {c: (s.x, s.n) for c, s in summary.evidence_complete.items()} == {"multi_hop": (1, 1)}
    assert summary.evidence_not_counted == {"multi_hop": ("q-b",)}
    assert (
        "rag_min_score 0.3; not hit@k): 1 of 1 (1 scored multi_hop question(s) not counted,"
        " answer-step error: q-b)" in render_markdown(summary)
    )


def test_a_key_no_stored_chunk_holds_is_counted_even_when_its_group_is_found(
    tmp_path: Path,
) -> None:
    results = [
        result("q-a").model_copy(update={"unheld_keys": ((0, 1),)}),
        result("q-b"),
        result("q-c", retrieval="unscorable").model_copy(update={"unheld_keys": ((0, 0),)}),
    ]
    means = {frozenset({"q-a", "q-b"}): (Fraction(1), Fraction(1))}

    summary = summarize(write_run(tmp_path, results), prepared_for(results, means).functions())

    assert (summary.unheld_keys, summary.unheld_key_questions) == (1, ("q-a",))
    assert (
        "- Evidence keys not held at this config (their groups are still findable): 1, in q-a"
        in render_markdown(summary)
    )


def test_data_not_marked_synthetic_is_never_called_synthetic(tmp_path: Path) -> None:
    results = twelve()

    markdown = render_markdown(
        summarize(
            write_run(tmp_path, results, dataset_synthetic=False),
            prepared_for(results).functions(),
        )
    )

    assert "NOT synthetic (dataset manifest `synthetic: false`" in markdown
    assert "fictional businesses" not in markdown and "synthetic: NO" in markdown


def test_run_json_with_a_metric_k_above_its_search_k_is_invalid() -> None:
    with pytest.raises(ValidationError, match="metric k"):
        ConfigInfo.model_validate(CONFIG.model_dump() | {"metric_k": 7})


# ── Status lines ─────────────────────────────────────────────────────────────────────────────

LIVE_TEST: Final = {"dry_run": False, "split": "test"}


@pytest.mark.parametrize(
    ("changes", "unlabelled", "line"),
    [
        ({}, 0, "DRY RUN: fake embedder and canned fake answers; no number in this report is a"),
        (
            {"dry_run": False, "status": "partial", "stop": "TEST-ONLY stop"},
            0,
            "PARTIAL live run, not a result: TEST-ONLY stop.",
        ),
        ({"dry_run": False}, 0, "Complete live run on the dev split: tuning numbers"),
        (
            LIVE_TEST,
            0,
            "Complete live held-out test run: may feed the note (config ffffffffffff).",
        ),
        (
            LIVE_TEST,
            3,
            "Held-out test run, not for the note yet: correctness: 3 answer(s) not labelled yet.",
        ),
        (
            LIVE_TEST | {"test_rerun_reason": "first-run-stopped-by-cap"},
            0,
            "Complete live held-out test RERUN (reason: first-run-stopped-by-cap): may feed the"
            " note (config ffffffffffff).",
        ),
        (
            {"dry_run": False, "status": "aborted_leak", "stop": "CROSS-BUSINESS LEAK: q"},
            0,
            "ABORTED: cross-business leak, a hard gate (business-context §6)",
        ),
        (
            {"dry_run": False, "status": "aborted_leak", "stop": "AUDIENCE LEAK: q"},
            0,
            "ABORTED: a document no customer may see was retrieved (business-context §3.4)",
        ),
        ({"dry_run": False, "status": "failed", "stop": "TEST-ONLY"}, 0, "FAILED: TEST-ONLY."),
        ({"status": "failed", "stop": "TEST-ONLY"}, 0, "DRY RUN · FAILED: TEST-ONLY."),
        (
            {"status": "aborted_leak", "stop": "CROSS-BUSINESS LEAK: q"},
            0,
            "DRY RUN · ABORTED: cross-business leak",
        ),
        (
            {"status": "partial", "stop": "TEST-ONLY stop"},
            0,
            "DRY RUN · PARTIAL run, not a result: TEST-ONLY stop.",
        ),
    ],
    ids=[
        "dry",
        "partial",
        "dev",
        "test",
        "test-unlabelled",
        "test-rerun",
        "leak",
        "audience-leak",
        "failed",
        "dry-failed",
        "dry-leak",
        "dry-partial",
    ],
)
def test_the_status_line_never_calls_a_dry_partial_or_dev_run_a_result(
    changes: dict[str, Any], unlabelled: int, line: str
) -> None:
    status, _ = status_of(run_info(**changes), unlabelled=unlabelled)

    assert status.startswith(line)


@pytest.mark.parametrize(
    ("changes", "unlabelled"),
    [
        ({"uncommitted_changes": True}, 0),
        ({"dataset_committed": False}, 0),
        ({"corpus_review_pending": 1}, 0),
        ({}, 2),
        ({"test_rerun_reason": "first-run-stopped-by-cap"}, 2),
    ],
    ids=["dirty", "dataset-not-committed", "review-pending", "unlabelled", "rerun-unlabelled"],
)
def test_a_test_run_with_any_reason_against_it_never_says_it_may_feed_the_note(
    changes: dict[str, Any], unlabelled: int
) -> None:
    line, reasons = status_of(run_info(**(LIVE_TEST | changes)), unlabelled=unlabelled)

    assert reasons
    assert "may feed the note" not in line and "to report" not in line
    assert line.endswith(f"not for the note yet: {'; '.join(reasons)}.")


@pytest.mark.parametrize("split", ["test", "dev"])
def test_a_stop_rule_tripped_by_the_last_question_is_complete_yet_never_for_the_note(
    split: str,
) -> None:
    # Decided 2026-10-22, as M1 (2026-10-15): every question ran, so the run is complete (exit 0),
    # but the stop keeps it out of the note.
    stop = "after question q-12: an answer call ended invalid_request: TEST-ONLY"

    line, reasons = status_of(run_info(dry_run=False, split=split, stop=stop), unlabelled=0)

    assert f"stopped by its last question: {stop}" in reasons
    assert "may feed the note" not in line
    if split == "test":
        assert line == (
            f"Held-out test run, not for the note yet: stopped by its last question: {stop}."
        )


def test_a_test_rerun_shows_its_reason_and_the_ledger_in_the_summary(tmp_path: Path) -> None:
    results = twelve()
    folder = write_run(
        tmp_path,
        results,
        filled(results, {r.question_id for r in results}),  # labelled: no reason against it
        **LIVE_TEST,
        test_rerun_reason="first-run-stopped-by-cap",
        test_runs_before_config=1,
        test_runs_before_freeze=2,
    )

    summary = summarize(folder, prepared_for(results).functions())
    markdown = render_markdown(summary)

    assert summary.status_line.startswith(
        "Complete live held-out test RERUN (reason: first-run-stopped-by-cap): may feed the note"
    )
    assert summary.status_line.endswith(
        "; the note cites every test-run ledger entry of this frozen version."
    )
    assert (
        "| Test-run ledger | 1 earlier test run(s) of this config, 2 of this frozen version (any"
        " config); rerun reason: first-run-stopped-by-cap |" in markdown
    )


def test_a_dev_run_has_no_ledger_row_and_a_dry_run_names_no_answering_model(
    tmp_path: Path,
) -> None:
    results = twelve()

    markdown = render_markdown(
        summarize(write_run(tmp_path, results), prepared_for(results).functions())
    )

    assert "Test-run ledger" not in markdown
    assert "(answered: none (canned dry-run reply))" in markdown
    assert "(answered: claude-sonnet-5-5)" not in markdown


def test_only_a_complete_labelled_live_test_run_on_a_committed_frozen_dataset_feeds_the_note() -> (
    None
):
    clean = run_info(dry_run=False, split="test")
    assert status_of(clean, unlabelled=0)[1] == ()
    reasons = status_of(
        run_info(
            split="unassigned",
            dataset_stage="collecting",
            corpus_review_pending=1,
            uncommitted_changes=True,
            dataset_committed=False,
            status="partial",
            stop="x",
        ),
        unlabelled=3,
    )[1]
    assert reasons == (
        "dry run: fake providers, never a measurement",
        "status partial",
        "split unassigned: only the held-out test split is reported",
        "the dataset is not frozen",
        "1 corpus document(s) still review: pending",
        "uncommitted changes: the commit does not name the code that ran",
        "dataset or corpus not as committed",
        "correctness: 3 answer(s) not labelled yet",
    )


def test_the_summary_says_synthetic_from_the_manifests_and_names_dataset_commit_and_config(
    tmp_path: Path,
) -> None:
    results = twelve()

    markdown = render_markdown(
        summarize(write_run(tmp_path, results), prepared_for(results).functions())
    )

    assert "`synthetic: true`" in markdown and "synthetic: yes" in markdown
    assert "support_qa v0.1 (stage frozen; freeze.json sha256 " + "0" * 64 in markdown
    assert "a" * 40 in markdown and "f" * 64 in markdown
    assert "search k 6 · metric k 5 · rag_min_score 0.3" in markdown
    assert "## Retrieval: hit@5 and MRR@5 (search k = 6)" in markdown
    assert "(dry run: fake usage, not a cost)" in markdown


# ── Labels ───────────────────────────────────────────────────────────────────────────────────


def test_the_label_template_has_one_empty_line_per_question_that_ran() -> None:
    lines = [json.loads(line) for line in label_template(RUN_ID, twelve()).splitlines()]

    assert [line["question_id"] for line in lines][-3:] == ["q-multi", "q-none", "q-unscorable"]
    assert len(lines) == 15  # 16 selected, one not run
    assert all(line["label"] is None and line["run_id"] == RUN_ID for line in lines)


def filled(results: list[QuestionResult], correct: set[str]) -> str:
    lines = []
    for line in label_template(RUN_ID, results).splitlines():
        payload = json.loads(line)
        payload |= {
            "label": "correct" if payload["question_id"] in correct else "incorrect",
            "labelled_by": "shesha",
            "labelled_on": "2026-10-30",
        }
        lines.append(json.dumps(payload))
    return "\n".join(lines) + "\n"


def test_correctness_comes_only_from_filled_labels(tmp_path: Path) -> None:
    results = twelve()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    unlabelled = summarize(
        write_run(tmp_path / "a", results, label_template(RUN_ID, results)),
        prepared_for(results).functions(),
    )

    assert (unlabelled.labelled, unlabelled.unlabelled, unlabelled.correctness) == (0, 15, ())
    assert "Not labelled yet: 0 of 15 answers labelled" in render_markdown(unlabelled)
    assert "correctness: 15 answer(s) not labelled yet" in unlabelled.not_for_note

    correct = {r.question_id for r in results[:10]}
    labelled = summarize(
        write_run(tmp_path / "b", results, filled(results, correct)),
        prepared_for(results).functions(),
    )

    everything = labelled.correctness[0]
    assert (everything.group, everything.correct.x, everything.correct.n) == ("all", 10, 15)
    assert everything.correct.low == wilson_interval(10, 15).low
    safety = next(r for r in labelled.correctness if r.group == "category safety")
    assert safety.counts_only and safety.correct.low is None
    assert not any("labelled" in reason for reason in labelled.not_for_note)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p | {"question_id": "q-skipped"}, "not a question this run answered"),
        (lambda p: p | {"question_id": "q-unknown"}, "not a question this run answered"),
        (lambda p: p | {"run_id": "another-run"}, "run_id is not this run's"),
        (lambda p: p | {"label": "maybe"}, "is not a valid label: label"),
        (lambda p: p | {"labelled_by": None}, "is not a valid label: labelled_by"),
        (lambda p: p | {"extra": 1}, "is not a valid label: extra"),
    ],
    ids=["not-run", "unknown", "other-run", "bad-label", "unsigned", "extra-field"],
)
def test_a_bad_label_line_is_refused_by_line_number(
    tmp_path: Path, edit: Any, message: str
) -> None:
    results = twelve()
    lines = filled(results, set()).splitlines()
    lines[0] = json.dumps(edit(json.loads(lines[0])))

    with pytest.raises(SummaryError, match=f"line 1.*{message}|line 1: {message}"):
        summarize(write_run(tmp_path, results, "\n".join(lines)), prepared_for(results).functions())


def test_two_labels_for_one_question_and_non_json_are_refused(tmp_path: Path) -> None:
    results = twelve()
    lines = filled(results, set()).splitlines()
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    with pytest.raises(SummaryError, match="line 16: one label per question and run"):
        summarize(
            write_run(tmp_path / "a", results, "\n".join([*lines, lines[0]])),
            prepared_for(results).functions(),
        )
    with pytest.raises(SummaryError, match="line 1 is not a JSON object"):
        summarize(write_run(tmp_path / "b", results, "nope\n"), prepared_for(results).functions())


# ── Fingerprint, label sheet, files ──────────────────────────────────────────────────────────


def fingerprint(**changes: Any) -> str:
    values: dict[str, Any] = {
        "dataset": "support_qa",
        "version": "v0.1",
        "freeze_sha256": "0" * 64,
        "embedding_model": "voyage-4",
        "answer_model": "claude-sonnet-5-5",
        "catalog_version": "2026-10-14.1",
        "prompt_version": "rag-answer.1",
        "config": CONFIG,
    }
    return config_fingerprint(**(values | changes))


@pytest.mark.parametrize(
    "change",
    [
        {"version": "v0.2"},
        {"freeze_sha256": "1" * 64},
        {"embedding_model": "voyage-4-lite"},
        {"answer_model": "claude-haiku-5-5"},
        {"catalog_version": "2026-11-01.1"},
        {"prompt_version": "rag-answer.2"},
        {"config": CONFIG.model_copy(update={"rag_min_score": 0.35})},
        {"config": CONFIG.model_copy(update={"max_chars": 800})},
        {"config": CONFIG.model_copy(update={"metric_k": 3})},
    ],
)
def test_the_config_fingerprint_moves_with_everything_a_run_measures(
    change: dict[str, Any],
) -> None:
    assert fingerprint() == fingerprint()
    assert fingerprint(**change) != fingerprint()


VECTOR_PIN: Final = "226ee6440a16d96d39733df5bc03ca9490e12e6d90545386a85e4511d1dff3f3"
"""The fingerprint of `pinned_vector_fingerprint()`'s fixed vector config, computed on M3 PR 6's
merged code (4140a48, before PR 5) with catalog 2026-10-14.1 and prompt rag-answer.1: so it also
proves PR 5's optional `retrieval` block leaves a vector fingerprint exactly as M2 made it."""


def pinned_vector_fingerprint() -> str:
    """A fixed vector config's fingerprint, with the catalog version read from
    gateway/models.toml and the prompt version from answer/prompt.py: never literals."""
    catalog = load_catalog()
    return config_fingerprint(
        dataset="support_qa",
        version="v0.1",
        freeze_sha256="0" * 64,
        embedding_model="voyage-4",
        answer_model=catalog.aliases[ANSWER_ALIAS].model,
        catalog_version=catalog.catalog_version,
        prompt_version=PROMPT_VERSION,
        config=ConfigInfo(
            max_chars=1200,
            overlap_chars=150,
            search_k=5,
            metric_k=5,
            rag_min_score=0.3,
            weak_evidence_outcome="dont_know",
            llm_max_retries=2,
            llm_max_repairs=1,
        ),
    )


def test_the_fingerprint_pin_turns_red_on_any_catalog_or_prompt_bump() -> None:
    # D3's tripwire (M3 plan, "Order of work" 1): from M2's first live dev run to M3's last live
    # answer run, nothing may change `models.toml` or `PROMPT_VERSION`, since either changes every
    # run's fingerprint (refusing M2's test run, splitting M2's and M3's runs). A bump turns this
    # test red. After the window, updating VECTOR_PIN is a deliberate, reviewed change.
    assert pinned_vector_fingerprint() == VECTOR_PIN, (
        "the config fingerprint moved: gateway/models.toml's catalog_version or answer/prompt.py's"
        " PROMPT_VERSION changed (or the fingerprint rule did). Inside D3's measurement window that"
        " is not allowed (docs/plans/m3-pr-checks.md, Order of work 1); after it, update VECTOR_PIN"
        " in a reviewed change."
    )


@pytest.mark.parametrize(
    ("module", "name", "value"),
    [
        ("catalog", "catalog_version", "2026-11-01.1"),
        ("prompt", "PROMPT_VERSION", "rag-answer.2"),
    ],
)
def test_the_pin_reads_the_live_catalog_and_prompt_version(
    monkeypatch: pytest.MonkeyPatch, module: str, name: str, value: str
) -> None:
    # The red proof: a bumped catalog or prompt version reaches the pinned computation.
    if module == "prompt":
        monkeypatch.setattr(sys.modules[__name__], "PROMPT_VERSION", value)
    else:
        real = load_catalog()
        bumped = real.model_copy(update={name: value})
        monkeypatch.setattr(sys.modules[__name__], "load_catalog", lambda: bumped)

    assert pinned_vector_fingerprint() != VECTOR_PIN


def test_a_vector_config_dumps_without_a_retrieval_key_never_as_null() -> None:
    dumped = CONFIG.model_dump(mode="json")
    assert "retrieval" not in dumped
    assert '"retrieval"' not in CONFIG.model_dump_json()
    info = run_info()
    assert "retrieval" not in run_fingerprint_payload(info)["config"]  # type: ignore[operator]
    assert ConfigInfo.model_validate_json(CONFIG.model_dump_json()) == CONFIG


def test_a_hybrid_block_is_dumped_read_back_and_fingerprinted() -> None:
    hybrid = CONFIG.model_copy(update={"retrieval": HYBRID_RETRIEVAL})

    dumped = hybrid.model_dump(mode="json")

    assert dumped["retrieval"] == {
        "mode": "hybrid",
        "candidate_k": 20,
        "rrf_k": 60,
        "language_map_sha256": language_map_sha256(),
    }
    assert ConfigInfo.model_validate(dumped) == hybrid
    assert fingerprint(config=hybrid) != fingerprint()


@pytest.mark.parametrize(
    "change",
    [{"candidate_k": 30}, {"rrf_k": 61}, {"language_map_sha256": "1" * 64}],
    ids=["candidate-k", "rrf-k", "language-map"],
)
def test_the_hybrid_fingerprint_moves_with_every_retrieval_field(change: dict[str, Any]) -> None:
    base = CONFIG.model_copy(update={"retrieval": HYBRID_RETRIEVAL})
    moved = CONFIG.model_copy(update={"retrieval": HYBRID_RETRIEVAL.model_copy(update=change)})

    assert fingerprint(config=moved) != fingerprint(config=base)


def test_the_language_maps_version_is_the_sha256_of_its_fixed_canonical_form() -> None:
    # Decided 2026-10-25 (shesha; M3 PR 5): the version a hybrid run records is the sha256 of the
    # canonical TEXT_SEARCH_CONFIGS plus FALLBACK_CONFIG (ingestion/language_map.py), never a
    # hand-bumped constant. Both literals are pinned: the form, and its digest computed outside
    # Python (`printf '%s' '<the form>' | shasum -a 256`, 2026-10-27). A map change moves the
    # digest (and needs its own migration anyway, ADR 0014 decision 2); a change to the form
    # itself fails here first.
    canonical = '{"fallback":"simple","map":{"en":"english","hi":"hindi"}}'
    assert language_map_canonical() == canonical
    assert language_map_sha256() == (
        "bb1f8ec0000f92a26447d9f0d89cbda36c59ffbf230547144c11317fdf280dd9"
    )


def test_the_language_maps_version_moves_with_the_map_and_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = language_map_sha256()

    monkeypatch.setattr(report, "TEXT_SEARCH_CONFIGS", {"en": "english", "hi": "simple"})
    assert language_map_sha256() != today
    monkeypatch.setattr(report, "TEXT_SEARCH_CONFIGS", {"hi": "hindi", "en": "english"})
    assert language_map_sha256() == today  # the order of the entries does not count
    monkeypatch.setattr(report, "FALLBACK_CONFIG", "english")
    assert language_map_sha256() != today


def test_a_hybrid_config_refuses_a_search_k_above_its_candidate_k() -> None:
    values = CONFIG.model_dump() | {
        "search_k": 21,
        "retrieval": HYBRID_RETRIEVAL.model_dump(),
    }
    with pytest.raises(ValidationError, match="must be at most rag_candidate_k"):
        ConfigInfo.model_validate(values)
    assert ConfigInfo.model_validate(values | {"search_k": 20}).search_k == 20
    with pytest.raises(ValidationError):
        RetrievalInfo.model_validate(
            HYBRID_RETRIEVAL.model_dump() | {"language_map_sha256": "not-a-sha"}
        )
    with pytest.raises(ValidationError):
        RetrievalInfo.model_validate(HYBRID_RETRIEVAL.model_dump() | {"mode": "vector"})


def test_the_label_sheet_shows_question_and_answer_for_the_terminal_only() -> None:
    q = question("q-01", text="TEST-ONLY what are your Sunday hours zq7")
    answer = AnswerLine(
        question_id="q-01",
        trace_id="0123456789abcdef0123456789abcdef",
        expected_outcome="answered",
        outcome="answered",
        reason=None,
        error=None,
        answer="TEST-ONLY closed on Sundays",
        sources=(
            SourceRef(
                chunk_id="c1",
                source_id="faq",
                version="2026-10",
                chunk_index=1,
                page=None,
                score=0.9,
            ),
        ),
    )

    sheet = render_label_sheet([answer], {"q-01": q})

    assert "Question: TEST-ONLY what are your Sunday hours zq7" in sheet
    assert "Answer:\n      > TEST-ONLY closed on Sundays" in sheet
    assert "Sources: faq 2026-10 chunk 1" in sheet


def test_write_summary_writes_markdown_and_json(tmp_path: Path) -> None:
    results = twelve()
    folder = write_run(tmp_path, results)
    summary = summarize(folder, prepared_for(results).functions())

    markdown = write_summary(folder, summary)

    assert (folder / "summary.md").read_text("utf-8") == markdown
    data = json.loads((folder / "summary.json").read_text("utf-8"))
    assert data["status_line"] == summary.status_line and data["scored"] == 13
