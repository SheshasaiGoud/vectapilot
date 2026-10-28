"""Human faithfulness labels (M3 PR 7): `faithfulness_labels.jsonl`, its template and parser, the
summary's faithfulness section and its §6 verdicts (ADR 0013 decision 10), and the label sheet's
cited text, rebuilt from the corpus after the freeze and corpus checks (`evalrun/cited.py`).

Every run folder, label and question here is TEST-ONLY and lives in pytest's tmp_path; the corpus
is the committed synthetic sample (`data/sample`), read only, or a TEST-ONLY copy of it in
tmp_path when a test edits a file. The summary tests use only `not_scored` results, so H2 (shesha's
[HAND-WRITE]) is never asked for anything: faithfulness is counted by the harness alone."""

import builtins
import hashlib
import io
import json
import logging
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import pytest

from tests.evalrun.helpers import (
    CONFIG,
    CORPUS_DIR,
    PAWS,
    PAWS_STORE_HOURS,
    REPO,
    RUN_ID,
    SENTINEL_QUESTION,
    SUNRISE,
    SUNRISE_HOURS,
    SUNRISE_PARKING,
    question,
    run_info,
)
from vectapilot_api.evalrun import cli
from vectapilot_api.evalrun.cited import (
    NOT_FROZEN,
    LabelSheetRefused,
    check_corpus,
    cited_texts,
)
from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.evalrun.report import (
    ANSWERS_FILE,
    FAITHFULNESS_DEFINITION,
    FAITHFULNESS_LABELS_FILE,
    LABELS_FILE,
    RESULTS_FILE,
    RUN_FILE,
    TARGETS,
    CitedText,
    Summary,
    SummaryError,
    faithfulness_template,
    parse_faithfulness,
    read_answers,
    read_results,
    read_run,
    render_json,
    render_label_sheet,
    render_markdown,
    summarize,
    terminal_text,
)
from vectapilot_api.evalrun.scoring import (
    AnswerLine,
    AnswerStatus,
    HitRef,
    MetricFunctions,
    QuestionResult,
    SourceRef,
    base_fields,
)
from vectapilot_api.evalset.corpus import Corpus, normalize_anchor
from vectapilot_api.evalset.files import DEFAULT_DATASET, read_corpus, read_dataset
from vectapilot_api.evalset.questions import EvalQuestion, Split, dump_question, load_manifest
from vectapilot_api.evalset.relevance import RetrievedChunk, matched_groups
from vectapilot_api.evalset.split import build_freeze
from vectapilot_api.ingestion.types import Chunk, ChunkingConfig
from vectapilot_api.llm_cli import GitInfo

TRACE: Final = "0123456789abcdef0123456789abcdef"
COMMIT: Final = "c" * 40
CHUNKING: Final = ChunkingConfig(max_chars=CONFIG.max_chars, overlap_chars=CONFIG.overlap_chars)
EVAL_IDS: Final = {SUNRISE: "sunrise-clinic-ev-aaaaaaaaaa", PAWS: "paws-and-co-ev-bbbbbbbbbb"}


def _unused(*args: object) -> object:
    raise AssertionError("faithfulness never asks H2 for a value")


NO_H2: Final = MetricFunctions(
    hit_at_k=_unused,  # type: ignore[arg-type]
    all_groups_at_k=_unused,  # type: ignore[arg-type]
    reciprocal_rank=_unused,  # type: ignore[arg-type]
    mean_hit_at_k=_unused,  # type: ignore[arg-type]
    mean_reciprocal_rank=_unused,  # type: ignore[arg-type]
)
"""H2's slot for summaries over `not_scored` results only: any call fails the test."""


# ── TEST-ONLY run folders for the summary ────────────────────────────────────────────────────


def shown(
    question_id: str,
    *,
    business: str = SUNRISE,
    language: str = "en",
    category: str = "faq",
    outcome: str = "answered",
) -> QuestionResult:
    """A TEST-ONLY result line with no evidence groups (not scored: H2 is never called)."""
    return QuestionResult(
        question_id=question_id,
        business_id=business,
        eval_business_id=EVAL_IDS[business],
        category=category,
        subtype=None,
        language_group=language,
        author_type="team",
        split_group=None,
        expected_outcome="answered",
        n_groups=0,
        retrieval="not_run" if outcome == "not_run" else "not_scored",
        outcome=outcome,
        trace_id=None if outcome == "not_run" else TRACE,
    )


def mixed() -> list[QuestionResult]:
    """Seven questions: five shown answers (sunrise en faq x2, sunrise hi policy, paws en faq,
    paws en safety), then one of each outcome that is never shown (dont_know, handoff, error,
    not_run)."""
    return [
        shown("s-1"),
        shown("s-2"),
        shown("s-3", language="hi", category="policy"),
        shown("p-1", business=PAWS),
        shown("p-2", business=PAWS, category="safety"),
        shown("x-dont-know", outcome="dont_know"),
        shown("x-handoff", outcome="handoff"),
        shown("x-error", outcome="error"),
        shown("x-not-run", outcome="not_run"),
    ]


def many(sunrise: Sequence[bool], paws: Sequence[bool] = ()) -> tuple[list[QuestionResult], str]:
    """Shown answers labelled faithful (True) or unfaithful (False): the results, and a filled
    faithfulness_labels.jsonl."""
    results = [shown(f"s-{i:02d}") for i in range(len(sunrise))]
    results += [shown(f"p-{i:02d}", business=PAWS) for i in range(len(paws))]
    flags = dict(zip((r.question_id for r in results), [*sunrise, *paws], strict=True))
    return results, filled(results, {q for q, ok in flags.items() if ok})


def filled(
    results: Sequence[QuestionResult], faithful: set[str], *, leave: frozenset[str] = frozenset()
) -> str:
    """The template with every line filled (TEST-ONLY labeller), except the ids in `leave`."""
    lines = []
    for line in faithfulness_template(RUN_ID, results).splitlines():
        payload = json.loads(line)
        if payload["question_id"] not in leave:
            payload |= {
                "label": "faithful" if payload["question_id"] in faithful else "unfaithful",
                "labelled_by": "test-only",
                "labelled_on": "2026-10-28",
            }
        lines.append(json.dumps(payload))
    return "".join(f"{line}\n" for line in lines)


def write_run(
    base: Path,
    results: Sequence[QuestionResult],
    faithfulness: str | None = None,
    **changes: Any,
) -> Path:
    """A TEST-ONLY live run folder `base/RUN_ID` (dry_run False unless `changes` says so)."""
    folder = base / RUN_ID
    folder.mkdir(parents=True)
    info = run_info(questions_selected=len(results), **({"dry_run": False} | changes))
    (folder / RUN_FILE).write_text(info.model_dump_json(indent=2), encoding="utf-8")
    (folder / RESULTS_FILE).write_text(
        "".join(r.model_dump_json() + "\n" for r in results), encoding="utf-8"
    )
    if faithfulness is not None:
        (folder / FAITHFULNESS_LABELS_FILE).write_text(faithfulness, encoding="utf-8")
    return folder


# ── The template and the parser ──────────────────────────────────────────────────────────────


def test_the_template_has_one_empty_line_per_shown_answer_and_no_text() -> None:
    lines = [json.loads(line) for line in faithfulness_template(RUN_ID, mixed()).splitlines()]

    assert [line["question_id"] for line in lines] == ["s-1", "s-2", "s-3", "p-1", "p-2"]
    assert all(
        line
        == {"question_id": line["question_id"], "run_id": RUN_ID, "label": None}
        | {"labelled_by": None, "labelled_on": None, "note": None}
        for line in lines
    )


def test_a_template_or_a_missing_file_leaves_every_shown_answer_unlabelled(
    tmp_path: Path,
) -> None:
    results = mixed()
    template = summarize(
        write_run(tmp_path / "a", results, faithfulness_template(RUN_ID, results)), NO_H2
    )
    missing = summarize(write_run(tmp_path / "b", results), NO_H2)

    for summary, present in ((template, True), (missing, False)):
        f = summary.faithfulness
        assert (f.file_present, f.shown, f.labelled, f.rows) == (present, 5, 0, ())
        assert f.unlabelled == ("s-1", "s-2", "s-3", "p-1", "p-2")
        assert {row.verdict for row in f.targets} == {
            "no verdict: 5 shown answer(s) not labelled for faithfulness yet"
        }
    assert "Not labelled yet: 0 of 5 shown answers labelled." in render_markdown(template)
    assert (
        f"not labelled yet: 5 ({FAITHFULNESS_LABELS_FILE} is missing: `evalrun"
        " faithfulness-template` writes it)" in render_markdown(missing)
    )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda p: p | {"question_id": "zz-unknown"}, "not a question of this run"),
        (lambda p: p | {"question_id": 7}, "not a question of this run"),
        (lambda p: p | {"question_id": "x-dont-know"}, "answer was not shown"),
        (lambda p: p | {"question_id": "x-handoff"}, "answer was not shown"),
        (lambda p: p | {"question_id": "x-error"}, "answer was not shown"),
        (lambda p: p | {"question_id": "x-not-run"}, "answer was not shown"),
        (lambda p: p | {"run_id": "another-run"}, "run_id is not this run's"),
        (lambda p: p | {"label": "correct"}, "is not a valid faithfulness label: label"),
        (lambda p: p | {"labelled_by": None}, "is not a valid faithfulness label: labelled_by"),
        (lambda p: p | {"labelled_on": "soon"}, "is not a valid faithfulness label: labelled_on"),
        (lambda p: p | {"extra": 1}, "is not a valid faithfulness label: extra"),
    ],
    ids=[
        "unknown",
        "not-a-string",
        "dont-know",
        "handoff",
        "error",
        "not-run",
        "other-run",
        "correctness-value",
        "unsigned",
        "bad-date",
        "extra-field",
    ],
)
def test_a_bad_faithfulness_line_is_refused_by_line_number(
    tmp_path: Path, edit: Any, message: str
) -> None:
    results = mixed()
    lines = filled(results, set()).splitlines()
    lines[0] = json.dumps(edit(json.loads(lines[0])))

    with pytest.raises(SummaryError, match=f"{FAITHFULNESS_LABELS_FILE} line 1.*{message}"):
        summarize(write_run(tmp_path, results, "\n".join(lines)), NO_H2)


def test_a_second_line_for_one_answer_and_a_line_that_is_not_json_are_refused(
    tmp_path: Path,
) -> None:
    results = mixed()
    lines = filled(results, set()).splitlines()

    with pytest.raises(SummaryError, match="line 6: one faithfulness label per shown answer"):
        summarize(write_run(tmp_path / "a", results, "\n".join([*lines, lines[2]])), NO_H2)
    with pytest.raises(SummaryError, match="line 2 is not a JSON object"):
        summarize(write_run(tmp_path / "b", results, f"{lines[0]}\n[1, 2]\n"), NO_H2)
    with pytest.raises(SummaryError, match="line 3 is not a JSON object"):
        summarize(write_run(tmp_path / "c", results, f"{lines[0]}\n\nnope {{\n"), NO_H2)


def test_a_null_line_is_the_answers_line_so_a_second_line_after_it_is_refused(
    tmp_path: Path,
) -> None:
    # A template (null) line counts as the answer's one line: a filled line for the same answer
    # after it, or before it, or a second null line, is a duplicate, never a quiet fill-in
    # (mutation audit B2: a null line not recorded as seen).
    results = mixed()
    template = faithfulness_template(RUN_ID, results).splitlines()
    labelled = filled(results, {"s-1"}).splitlines()
    cases = {
        "null-then-filled": ([*template, labelled[0]], 6),
        "filled-then-null": ([*labelled, template[0]], 6),
        "null-twice": ([template[0], template[0]], 2),
    }

    for name, (lines, number) in cases.items():
        with pytest.raises(
            SummaryError, match=f"line {number}: one faithfulness label per shown answer"
        ):
            summarize(write_run(tmp_path / name, results, "\n".join(lines)), NO_H2)


def test_a_refusal_never_echoes_the_value_it_refused() -> None:
    hidden = "TEST-ONLY-zq7-labeller-text"
    line = json.dumps(
        {"question_id": "s-1", "run_id": RUN_ID, "label": hidden, "labelled_by": "test-only"}
        | {"labelled_on": "2026-10-28", "note": None}
    )

    with pytest.raises(SummaryError) as refused:
        parse_faithfulness(line, {"s-1"}, {"s-1"}, RUN_ID)
    assert hidden not in str(refused.value)


def test_blank_lines_are_skipped_and_template_lines_count_as_unlabelled() -> None:
    results = mixed()
    text = filled(results, {"s-1"}, leave=frozenset({"s-2", "p-2"}))

    labels, missing = parse_faithfulness(
        "\n" + text.replace("\n", "\n\n"),
        {r.question_id for r in results},
        {"s-1", "s-2", "s-3", "p-1", "p-2"},
        RUN_ID,
    )

    assert {q: label.label for q, label in labels.items()} == {
        "s-1": "faithful",
        "s-3": "unfaithful",
        "p-1": "unfaithful",
    }
    assert missing == {"s-2", "p-2"}


def test_correctness_and_faithfulness_never_read_each_others_file(tmp_path: Path) -> None:
    results = mixed()
    faithful_line = filled(results, {"s-1"}).splitlines()[0]

    with pytest.raises(SummaryError, match=f"{LABELS_FILE} line 1 is not a valid label: label"):
        folder = write_run(tmp_path / "a", results)
        (folder / LABELS_FILE).write_text(faithful_line + "\n", encoding="utf-8")
        summarize(folder, NO_H2)
    summary = summarize(write_run(tmp_path / "b", results, filled(results, {"s-1"})), NO_H2)
    assert (summary.labelled, summary.faithfulness.labelled) == (0, 5)


# ── The summary: x of n, Wilson, per group ───────────────────────────────────────────────────


def test_faithfulness_is_x_of_n_with_wilson_per_business_language_and_category(
    tmp_path: Path,
) -> None:
    results = mixed()

    summary = summarize(write_run(tmp_path, results, filled(results, {"s-1", "s-3", "p-2"})), NO_H2)

    f = summary.faithfulness
    rows = {row.group: row for row in f.rows}
    assert {g: (r.faithful.x, r.faithful.n) for g, r in rows.items()} == {
        "all": (3, 5),
        "business paws-and-co": (1, 2),
        "business sunrise-clinic": (2, 3),
        "language en": (2, 4),
        "language hi": (1, 1),
        "category faq": (1, 3),
        "category policy": (1, 1),
        "category safety": (1, 1),
    }
    everything = rows["all"].faithful
    assert (everything.low, everything.high) == (
        wilson_interval(3, 5).low,
        wilson_interval(3, 5).high,
    )
    assert rows["category safety"].counts_only and rows["category safety"].faithful.low is None
    assert f.unfaithful == ("s-2", "p-1") and f.unlabelled == ()
    markdown = render_markdown(summary)
    # Hand-worked Wilson 95 % for 3 of 5: centre (0.6 + z²/10) / (1 + z²/5) = 0.5566, half-width
    # z·sqrt(0.6·0.4/5 + z²/100) / (1 + z²/5) = 0.3258, so 23.07 % to 88.24 %: "23 % to 88 %".
    assert "| all | 3 of 5 (95 % Wilson 23 % to 88 %) |" in markdown
    assert "| category safety | 1 of 1 (counts only) |" in markdown
    assert "- Labelled unfaithful: s-2, p-1" in markdown


def test_no_answer_shown_says_so_and_gives_no_verdict(tmp_path: Path) -> None:
    results = [shown("x-1", outcome="dont_know"), shown("x-2", outcome="handoff")]

    summary = summarize(write_run(tmp_path, results, ""), NO_H2)

    assert summary.faithfulness.shown == 0
    assert {row.verdict for row in summary.faithfulness.targets} == {"no verdict: no shown answer"}
    assert "No answer was shown: nothing to label for faithfulness." in render_markdown(summary)


# ── §6's ≥ 95 % under decision 10 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("faithful", "unfaithful", "verdict", "shown_at_95"),
    [
        (19, 1, "met", False),  # exactly 95 %: the point estimate reaches it; Wilson 76 % to 99 %
        (73, 0, "met", True),  # Wilson lower end 73 / (73 + z²) = 95.0008 %: shown too
        (72, 0, "met", False),  # 72 / (72 + z²) = 94.93 %: met, not shown at 95 % confidence
        (18, 2, "not met", False),
        (9, 0, "no verdict: n < 10 (counts only)", None),
    ],
    ids=["exactly-95", "shown-73", "not-shown-72", "not-met", "small-n"],
)
def test_the_95_percent_target_follows_decision_10(
    tmp_path: Path, faithful: int, unfaithful: int, verdict: str, shown_at_95: bool | None
) -> None:
    results, labels = many([True] * faithful + [False] * unfaithful)

    summary = summarize(write_run(tmp_path, results, labels), NO_H2)

    rows = {row.group: row for row in summary.faithfulness.targets}
    everything = rows["all"]
    assert (everything.metric, everything.target, everything.verdict) == (
        "faithfulness",
        "19/20",
        verdict,
    )
    assert everything.shown_at_95 is shown_at_95
    assert rows["business sunrise-clinic"].verdict == verdict
    assert rows["business paws-and-co"].verdict == "no verdict: no shown answer"
    text = {
        (True, "met"): "met (point estimate; the 95 % Wilson lower end reaches it too)",
        (False, "met"): "met (point estimate; not shown at 95 % confidence)",
    }.get((bool(shown_at_95), verdict), verdict)
    assert f"| faithfulness | all | ≥ 95 % | {faithful} of {faithful + unfaithful}" in (
        render_markdown(summary)
    )
    assert f" | {text} |" in render_markdown(summary)


@pytest.mark.parametrize(
    ("changes", "verdict"),
    [
        ({"dry_run": True}, "no verdict: dry run"),
        ({"status": "partial", "stop": "x"}, "no verdict: status partial"),
        ({"status": "failed", "stop": "x"}, "no verdict: status failed"),
        ({"status": "aborted_leak", "stop": "x"}, "no verdict: status aborted_leak"),
    ],
    ids=["dry", "partial", "failed", "leak"],
)
def test_no_verdict_for_anything_but_a_complete_live_run(
    tmp_path: Path, changes: dict[str, Any], verdict: str
) -> None:
    results, labels = many([True] * 20)

    summary = summarize(write_run(tmp_path, results, labels, **changes), NO_H2)

    assert {row.verdict for row in summary.faithfulness.targets} == {verdict}
    assert {row.shown_at_95 for row in summary.faithfulness.targets} == {None}


def test_one_unlabelled_shown_answer_stops_every_verdict(tmp_path: Path) -> None:
    results, _ = many([True] * 20, [True] * 10)
    ids = {r.question_id for r in results}

    summary = summarize(
        write_run(tmp_path, results, filled(results, ids, leave=frozenset({"p-03"}))), NO_H2
    )

    assert {row.verdict for row in summary.faithfulness.targets} == {
        "no verdict: 1 shown answer(s) not labelled for faithfulness yet"
    }
    assert summary.faithfulness.unlabelled == ("p-03",)
    assert "Incomplete: not every shown answer is labelled, so not a result." in render_markdown(
        summary
    )


def test_each_business_gets_its_own_verdict(tmp_path: Path) -> None:
    results, labels = many([True] * 10, [True] * 8 + [False] * 2)

    summary = summarize(write_run(tmp_path, results, labels), NO_H2)

    rows = {
        row.group: (row.stat.x, row.stat.n, row.verdict) for row in summary.faithfulness.targets
    }
    assert rows == {
        "all": (18, 20, "not met"),
        "business paws-and-co": (8, 10, "not met"),
        "business sunrise-clinic": (10, 10, "met"),
    }


def test_faithfulness_and_correctness_decide_independently(tmp_path: Path) -> None:
    results, labels = many([True] * 20)

    summary = summarize(write_run(tmp_path, results, labels, split="test"), NO_H2)

    correctness = {row.verdict for row in summary.targets if row.metric == "correctness"}
    assert correctness == {"no verdict: 20 answer(s) not labelled yet"}
    assert {row.verdict for row in summary.faithfulness.targets if row.group == "all"} == {"met"}
    assert "correctness: 20 answer(s) not labelled yet" in summary.not_for_note
    assert not any("aithful" in reason for reason in summary.not_for_note)
    assert "aithful" not in summary.status_line


def test_unlabelled_faithfulness_never_holds_back_a_test_run_that_may_feed_the_note(
    tmp_path: Path,
) -> None:
    results, _ = many([True] * 3)
    folder = write_run(tmp_path, results, split="test")
    correctness = "".join(
        json.dumps(
            {"question_id": r.question_id, "run_id": RUN_ID, "label": "correct"}
            | {"labelled_by": "test-only", "labelled_on": "2026-10-28", "note": None}
        )
        + "\n"
        for r in results
    )
    (folder / LABELS_FILE).write_text(correctness, encoding="utf-8")

    summary = summarize(folder, NO_H2)

    assert summary.status_line.startswith("Complete live held-out test run: may feed the note")
    assert summary.faithfulness.labelled == 0


def m2_markdown(summary: Summary) -> str:
    """summary.md above the faithfulness section: M2's part, status line and Targets included."""
    before, heading, _ = render_markdown(summary).partition("## Faithfulness (human labels)\n")
    assert heading
    return before


@pytest.mark.parametrize(
    ("correctness_unlabelled", "status_line", "not_for_note"),
    [
        (
            0,
            f"Complete live held-out test run: may feed the note (config {'f' * 12}).",
            (),
        ),
        (
            2,
            "Held-out test run, not for the note yet: correctness: 2 answer(s) not labelled yet.",
            ("correctness: 2 answer(s) not labelled yet",),
        ),
    ],
    ids=["may-feed", "not-yet"],
)
def test_m2s_part_of_the_summary_is_byte_identical_whatever_the_faithfulness_file_says(
    tmp_path: Path,
    correctness_unlabelled: int,
    status_line: str,
    not_for_note: tuple[str, ...],
) -> None:
    # Decision 3 (decided 2026-10-28, shesha): faithfulness never changes the status line, the
    # Targets table or "Not for the note". TEST-ONLY live test runs, one per faithfulness file,
    # each with the same correctness labels (mutation audit N2-N4).
    results = [shown(f"s-{i:02d}") for i in range(12)]
    results += [shown(f"p-{i:02d}", business=PAWS) for i in range(10)]
    results += [shown("x-dont-know", outcome="dont_know")]
    results += [shown("x-handoff", business=PAWS, outcome="handoff")]
    answered = [r.question_id for r in results if r.outcome == "answered"]
    correctness = "".join(
        json.dumps(
            {"question_id": r.question_id, "run_id": RUN_ID, "label": "correct"}
            | {"labelled_by": "test-only", "labelled_on": "2026-10-28", "note": None}
        )
        + "\n"
        for r in results[correctness_unlabelled:]
    )
    variants = {
        "no-file": None,
        "empty": "",
        "template": faithfulness_template(RUN_ID, results),
        "part-labelled": filled(results, set(answered), leave=frozenset(answered[:3])),
        "not-met": filled(results, set(answered[::2])),
        "met": filled(results, set(answered)),
    }
    summaries = {}
    for name, text in variants.items():
        folder = write_run(tmp_path / name, results, text, split="test")
        (folder / LABELS_FILE).write_text(correctness, encoding="utf-8")
        summaries[name] = summarize(folder, NO_H2)

    baseline = summaries["no-file"]
    assert (baseline.status_line, baseline.not_for_note) == (status_line, not_for_note)
    assert "## Targets (business-context §6)" in m2_markdown(baseline)
    for name, summary in summaries.items():
        assert m2_markdown(summary) == m2_markdown(baseline), name
        # summary.json: every byte but the faithfulness key's value is the same.
        same_key = summary.model_copy(update={"faithfulness": baseline.faithfulness})
        assert render_json(same_key) == render_json(baseline), name
    # The files did differ where they should: in the faithfulness section alone.
    assert [s.faithfulness.targets[0].verdict for s in summaries.values()] == [
        "no verdict: 22 shown answer(s) not labelled for faithfulness yet",
        "no verdict: 22 shown answer(s) not labelled for faithfulness yet",
        "no verdict: 22 shown answer(s) not labelled for faithfulness yet",
        "no verdict: 3 shown answer(s) not labelled for faithfulness yet",
        "not met",
        "met",
    ]


def test_the_section_is_last_and_says_what_it_measures_in_full(tmp_path: Path) -> None:
    results = mixed()

    markdown = render_markdown(
        summarize(write_run(tmp_path, results, filled(results, {"s-1", "s-3", "p-2"})), NO_H2)
    )

    before, heading, section = markdown.partition("## Faithfulness (human labels)\n")
    assert heading and "aithful" not in before
    assert before.index("## Targets (business-context §6)") < len(before)
    assert "| faithfulness |" not in before
    assert section == "\n".join(
        [
            "",
            f"{FAITHFULNESS_DEFINITION} One label per shown answer (outcome answered), not per"
            " claim; written by a person, never by a model (no LLM judge in M3). Labels:"
            f" {FAITHFULNESS_LABELS_FILE}.",
            "",
            "- Shown answers: 5 · labelled: 5 · not labelled yet: 0",
            "- Not labelled yet: none",
            "- Labelled unfaithful: s-2, p-1",
            "",
            "| Group | Faithful |",
            "|---|---|",
            "| all | 3 of 5 (95 % Wilson 23 % to 88 %) |",
            "| business paws-and-co | 1 of 2 (95 % Wilson 9 % to 91 %) |",
            "| business sunrise-clinic | 2 of 3 (95 % Wilson 21 % to 94 %) |",
            "| language en | 2 of 4 (95 % Wilson 15 % to 85 %) |",
            "| language hi | 1 of 1 (95 % Wilson 21 % to 100 %) |",
            "| category faq | 1 of 3 (95 % Wilson 6 % to 79 %) |",
            "| category policy | 1 of 1 (95 % Wilson 21 % to 100 %) |",
            "| category safety | 1 of 1 (counts only) |",
            "",
            "Business-context §6's faithfulness target, ≥ 95 %, under the Targets table's rules"
            " (ADR 0013 decision 10): met when the point estimate reaches it, always read with its"
            ' 95 % Wilson interval, "not shown at 95 % confidence" when the lower end is below'
            " it; no verdict below n = 10, for a dry, partial or failed run, or while a shown"
            " answer is unlabelled. A release needs every business to meet it.",
            "Split dev: tuning information only, not the held-out result.",
            "",
            "| Metric | Group | Target | Measured | Verdict |",
            "|---|---|---|---|---|",
            "| faithfulness | all | ≥ 95 % | 3 of 5 (95 % Wilson 23 % to 88 %) | no verdict:"
            " n < 10 (counts only) |",
            "| faithfulness | business paws-and-co | ≥ 95 % | 1 of 2 (95 % Wilson 9 % to 91 %) |"
            " no verdict: n < 10 (counts only) |",
            "| faithfulness | business sunrise-clinic | ≥ 95 % | 2 of 3 (95 % Wilson 21 % to 94 %)"
            " | no verdict: n < 10 (counts only) |",
            "",
        ]
    )


def test_a_test_split_section_has_no_tuning_line_and_an_empty_group_reads_no_shown_answer(
    tmp_path: Path,
) -> None:
    results = [shown("s-1"), shown("s-2")]

    markdown = render_markdown(
        summarize(write_run(tmp_path, results, filled(results, {"s-1"}), split="test"), NO_H2)
    )

    assert "tuning information only" not in markdown.partition("## Faithfulness")[2]
    assert (
        "| faithfulness | business paws-and-co | ≥ 95 % | no shown answer | no verdict: no shown"
        " answer |" in markdown
    )


def test_measured_tells_no_shown_answer_from_none_labelled_yet(tmp_path: Path) -> None:
    # TEST-ONLY: two shown Sunrise answers and none for Paws (its one question handed off), as a
    # business may have on v0.1's 12-question test split. Paws reads "no shown answer" in both
    # cells, ahead of the run-wide unlabelled reason, which is about Sunrise's answers (decision
    # 10, decided 2026-10-28, shesha).
    results = [shown("s-1"), shown("s-2"), shown("p-1", business=PAWS, outcome="handoff")]

    partly = summarize(
        write_run(tmp_path / "a", results, filled(results, {"s-1"}, leave=frozenset({"s-2"}))),
        NO_H2,
    )
    unlabelled = summarize(write_run(tmp_path / "b", results), NO_H2)

    assert partly.faithfulness.shown_by_group == {
        "all": 2,
        "business paws-and-co": 0,
        "business sunrise-clinic": 2,
    }
    one = "no verdict: 1 shown answer(s) not labelled for faithfulness yet"
    two = "no verdict: 2 shown answer(s) not labelled for faithfulness yet"
    none_shown = "no verdict: no shown answer"
    one_of_one = "1 of 1 (95 % Wilson 21 % to 100 %)"
    for markdown, rows in (
        (
            render_markdown(partly),
            [
                ("all", one_of_one, one),
                ("business paws-and-co", "no shown answer", none_shown),
                ("business sunrise-clinic", one_of_one, one),
            ],
        ),
        (
            render_markdown(unlabelled),
            [
                ("all", "none labelled (2 shown)", two),
                ("business paws-and-co", "no shown answer", none_shown),
                ("business sunrise-clinic", "none labelled (2 shown)", two),
            ],
        ),
    ):
        for group, measured, verdict in rows:
            assert f"| faithfulness | {group} | ≥ 95 % | {measured} | {verdict} |" in markdown


def test_the_definitions_give_correctness_in_full_with_its_expected_outcome() -> None:
    # The short form ("the answer matches an approved source") would call a faithful answer to a
    # question that expects a hand-off correct: the guide's own faithful-but-incorrect example.
    assert "the reply does what the expected outcome says" in FAITHFULNESS_DEFINITION
    assert "every claim matches an approved source" in FAITHFULNESS_DEFINITION


# ── The label sheet: cited text rebuilt from the corpus ──────────────────────────────────────

CORPUS: Final = read_corpus(CORPUS_DIR, [SUNRISE, PAWS])


def chunk_with(corpus: Corpus, business: str, source_id: str, version: str, anchor: str) -> Chunk:
    """The chunk of the document that holds `anchor`, at the run's chunk config."""
    document = corpus.document(business, source_id, version)
    assert document is not None
    return next(
        c for c in corpus.chunks(document, CHUNKING) if anchor in normalize_anchor(c.content)
    )


def first_chunk(business: str, source_id: str, version: str) -> Chunk:
    document = CORPUS.document(business, source_id, version)
    assert document is not None
    return CORPUS.chunks(document, CHUNKING)[0]


def refs(q: EvalQuestion, chunk: Chunk, rank: int, **change: Any) -> tuple[HitRef, SourceRef]:
    """The run's record of one cited chunk: its hit (with the groups it finds) and its source."""
    rebuilt = RetrievedChunk(q.business_id, chunk.source_id, chunk.version, chunk.content)
    fields: dict[str, Any] = {
        "chunk_id": f"chunk-{q.id}-{rank}",
        "source_id": chunk.source_id,
        "version": chunk.version,
        "chunk_index": chunk.chunk_index,
        "page": chunk.page,
        "score": 0.8,
    } | change
    groups = fields.pop("groups", matched_groups(rebuilt, q))
    hit = HitRef(rank=rank, document_id=f"doc-{rank}", groups=groups, **fields)
    return hit, SourceRef(**fields)


def line_pair(
    q: EvalQuestion,
    cited: Sequence[tuple[HitRef, SourceRef]] = (),
    *,
    outcome: AnswerStatus = "answered",
    answer: str | None = "TEST-ONLY answer zq7-reply",
) -> tuple[QuestionResult, AnswerLine]:
    sources = tuple(source for _, source in cited) if outcome == "answered" else ()
    result = QuestionResult.model_validate(
        {
            **base_fields(q, EVAL_IDS[q.business_id]),
            "retrieval": "scored",
            "hits": tuple(hit for hit, _ in cited),
            "evidence_chunk_ids": tuple(hit.chunk_id for hit, _ in cited),
            "outcome": outcome,
            "sources": sources,
            "trace_id": TRACE,
        }
    )
    reply = AnswerLine(
        question_id=q.id,
        trace_id=TRACE,
        expected_outcome=q.expected_outcome,
        outcome=outcome,
        reason=None if outcome == "answered" else "below_threshold",
        error=None,
        answer=answer if outcome == "answered" else None,
        sources=sources,
    )
    return result, reply


HOURS_Q: Final = question("tq-hours", text=SENTINEL_QUESTION, split="dev")
PARKING_Q: Final = question("tq-parking", evidence=((SUNRISE_PARKING,),), split="dev")
PAWS_Q: Final = question("tq-paws", PAWS, evidence=((PAWS_STORE_HOURS,),), split="dev")
NONE_Q: Final = question(
    "tq-none", evidence=(), expected="dont_know", category="out_of_scope", split="dev"
)
QUESTIONS: Final = (HOURS_Q, PARKING_Q, PAWS_Q, NONE_Q)

HOURS_CHUNK: Final = chunk_with(CORPUS, SUNRISE, "faq", "2026-10", SUNRISE_HOURS.anchor)
PAWS_CHUNK: Final = chunk_with(CORPUS, PAWS, "faq", "2026-09", PAWS_STORE_HOURS.anchor)
PDF_CHUNK: Final = first_chunk(SUNRISE, "fees", "2026-04")


def write_frozen_dataset(root: Path, corpus_dir: Path = CORPUS_DIR) -> Path:
    """A TEST-ONLY frozen copy of v0.1's manifest with `QUESTIONS`, and its freeze.json over the
    corpus in `corpus_dir` (the same three files `evalset freeze` writes)."""
    folder = root / DEFAULT_DATASET
    folder.mkdir(parents=True)
    manifest = load_manifest((REPO / DEFAULT_DATASET / "manifest.json").read_text("utf-8"))
    manifest = manifest.model_copy(update={"stage": "frozen"})
    corpus = read_corpus(corpus_dir, manifest.businesses)
    splits: dict[str, Split] = {q.id: "dev" for q in QUESTIONS}
    freeze = build_freeze(manifest, QUESTIONS, splits, corpus.pins(), date(2026, 10, 28))
    (folder / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    (folder / "questions.jsonl").write_text(
        "".join(dump_question(q) + "\n" for q in QUESTIONS), "utf-8"
    )
    (folder / "freeze.json").write_text(json.dumps(freeze.model_dump(mode="json")) + "\n", "utf-8")
    return folder


def default_lines() -> list[tuple[QuestionResult, AnswerLine]]:
    """Two shown answers (the hours one cites the faq chunk and the fees PDF's first chunk; the
    Paws one cites its faq chunk) and one dont_know."""
    return [
        line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1), refs(HOURS_Q, PDF_CHUNK, 2)]),
        line_pair(PAWS_Q, [refs(PAWS_Q, PAWS_CHUNK, 1)]),
        line_pair(NONE_Q, outcome="dont_know"),
    ]


@dataclass
class Sheet:
    root: Path
    dataset: Path
    corpus: Path
    run_dir: Path

    def run(self, *args: str) -> int:
        io_ = cli.Io(
            load_settings=lambda live: cli.load_settings(False),
            owner_url=lambda: None,
            git=lambda: GitInfo(root=self.root, commit=COMMIT, uncommitted_changes=False),
            head_file=lambda root, path: None,
            now=lambda: datetime(2026, 10, 28, tzinfo=UTC),
            stdin=io.StringIO(),
            stdout_is_terminal=lambda: False,
            execute=_never,
        )
        argv = ["--dataset", str(self.dataset), "--corpus", str(self.corpus), *args]
        return cli.run(argv, io_)


async def _never(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("label-sheet and faithfulness-template never run a question")


def write_label_run(
    tmp_path: Path,
    lines: Sequence[tuple[QuestionResult, AnswerLine]] | None = None,
    *,
    corpus_dir: Path = CORPUS_DIR,
    freeze_sha: str | None = "computed",
    **changes: Any,
) -> Sheet:
    root = tmp_path / "repo"
    dataset = write_frozen_dataset(root, corpus_dir)
    pairs = default_lines() if lines is None else list(lines)
    run_dir = root / "evals/reports/support_qa/v0.1/20261028T000000Z-live-dev"
    run_dir.mkdir(parents=True)
    sha = hashlib.sha256((dataset / "freeze.json").read_bytes()).hexdigest()
    info = run_info(
        run_id=run_dir.name,
        dry_run=False,
        questions_selected=len(pairs),
        freeze_sha256=sha if freeze_sha == "computed" else freeze_sha,
        commit=COMMIT,
        **changes,
    )
    (run_dir / RUN_FILE).write_text(info.model_dump_json(indent=2), "utf-8")
    (run_dir / RESULTS_FILE).write_text("".join(r.model_dump_json() + "\n" for r, _ in pairs))
    (run_dir / ANSWERS_FILE).write_text("".join(a.model_dump_json() + "\n" for _, a in pairs))
    return Sheet(root, dataset, corpus_dir, run_dir)


def snapshot(folder: Path) -> dict[str, str]:
    return {
        str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def gutter(text: str) -> str:
    """How the sheet prints a chunk's text: each line behind the gutter."""
    return "\n".join(f"      | {line}" for line in terminal_text(text).split("\n"))


def test_the_label_sheet_prints_each_cited_chunks_text_rebuilt_at_the_runs_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    sheet = write_label_run(tmp_path)
    before = snapshot(tmp_path)
    caplog.set_level(logging.DEBUG)

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 0

    out = capsys.readouterr().out
    assert gutter(HOURS_CHUNK.content) in out and gutter(PAWS_CHUNK.content) in out
    assert gutter(PDF_CHUNK.content) in out
    assert f"  [1] faq 2026-10 chunk {HOURS_CHUNK.chunk_index} · page none" in out
    assert f"  [2] fees 2026-04 chunk 0 · page {PDF_CHUNK.page}" in out
    assert PDF_CHUNK.page is not None
    assert f"Question: {SENTINEL_QUESTION}" in out
    assert "Answer:\n      > TEST-ONLY answer zq7-reply" in out
    assert "every claim in the answer is supported by the text of the chunks it cites" in out
    assert "Neither label stands in for the other." in out
    assert "Cited text: rebuilt from the corpus at chunks 1200/150 (run.json's config)" in out
    assert FAITHFULNESS_LABELS_FILE in out.splitlines()[-1]
    none_block = out.partition("── tq-none ──")[2]
    assert "Cited chunks" not in none_block  # a dont_know answer is never labelled for it
    # Terminal only: no file written or changed anywhere, and no log record holds any text.
    assert snapshot(tmp_path) == before
    logged = caplog.text + "".join(str(record.args) for record in caplog.records)
    for text in (HOURS_CHUNK.content[:40], SENTINEL_QUESTION, "zq7-reply"):
        assert text not in logged


def test_cited_texts_are_the_corpus_chunks_at_the_runs_chunk_config(tmp_path: Path) -> None:
    sheet = write_label_run(tmp_path)
    dataset = read_dataset(sheet.dataset, CORPUS_DIR)
    run = read_run(sheet.run_dir)

    cited = cited_texts(run, read_results(sheet.run_dir), read_answers(sheet.run_dir), dataset)

    assert {q: [item.text for item in items] for q, items in cited.items()} == {
        "tq-hours": [HOURS_CHUNK.content, PDF_CHUNK.content],
        "tq-paws": [PAWS_CHUNK.content],
    }


TUNED: Final = ChunkingConfig(max_chars=800, overlap_chars=100)
"""A run.json chunk config other than the defaults (1200/150), as a dev-tuned run may record."""


def faq_hi_chunks(config: ChunkingConfig) -> dict[int, Chunk]:
    document = CORPUS.document(SUNRISE, "faq-hi", "2026-10")
    assert document is not None
    return {chunk.chunk_index: chunk for chunk in CORPUS.chunks(document, config)}


def test_cited_text_is_rebuilt_at_run_jsons_own_chunk_config_not_a_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # TEST-ONLY run.json at 800/100. The sample's markdown sections are short enough that most
    # chunks read the same at any config; faq-hi's last section is not. Its chunk 5 has another
    # text at 1200/150 (same index, page and groups: no refusal, only other text), and its chunk 6
    # another text at 800/0 and none at 1200/150. So a rebuild at the defaults, at overlap 0 or at
    # a fixed config shows or refuses the wrong chunk (mutation audit L1-L3).
    tuned = faq_hi_chunks(TUNED)
    defaults = faq_hi_chunks(ChunkingConfig())
    no_overlap = faq_hi_chunks(ChunkingConfig(max_chars=800, overlap_chars=0))
    assert (CHUNKING, ChunkingConfig()) == (ChunkingConfig(), ChunkingConfig(max_chars=1200))
    assert tuned[5].content != defaults[5].content and 6 not in defaults
    assert tuned[6].content != no_overlap[6].content
    config = CONFIG.model_copy(update={"max_chars": 800, "overlap_chars": 100})
    one = [line_pair(HOURS_Q, [refs(HOURS_Q, tuned[5], 1)])]
    both = [line_pair(HOURS_Q, [refs(HOURS_Q, tuned[5], 1), refs(HOURS_Q, tuned[6], 2)])]
    sheet = write_label_run(tmp_path / "both", both, config=config)

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 0

    out = capsys.readouterr().out
    assert "Cited text: rebuilt from the corpus at chunks 800/100 (run.json's config)" in out
    assert gutter(tuned[5].content) in out and gutter(tuned[6].content) in out
    assert gutter(defaults[5].content) not in out
    assert [item.text for item in sheet_texts(sheet)["tq-hours"]] == [
        tuned[5].content,
        tuned[6].content,
    ]
    alone = write_label_run(tmp_path / "one", one, config=config)
    assert [item.text for item in sheet_texts(alone)["tq-hours"]] == [tuned[5].content]


WRITE_FLAGS: Final = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def test_the_label_sheet_opens_nothing_for_writing_anywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The snapshot above sees tmp_path only. Here every write-mode open, anywhere, is recorded
    # and refused while the sheet runs: builtins.open, io.open (pathlib's way in) and os.open
    # (tempfile's), so the sheet cannot leave chunk text in a file outside tmp_path either
    # (mutation audit K2).
    sheet = write_label_run(tmp_path)
    attempts: list[str] = []
    real_open, real_os_open = io.open, os.open

    def read_only_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if set(mode) & set("wax+"):
            attempts.append(f"open {file!r} {mode!r}")
            raise PermissionError("TEST-ONLY: the label sheet opens nothing for writing")
        return real_open(file, mode, *args, **kwargs)

    def read_only_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & WRITE_FLAGS:
            attempts.append(f"os.open {path!r} {flags:#o}")
            raise PermissionError("TEST-ONLY: the label sheet opens nothing for writing")
        return real_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(builtins, "open", read_only_open)
        patched.setattr(io, "open", read_only_open)
        patched.setattr(os, "open", read_only_os_open)
        code = sheet.run("label-sheet", str(sheet.run_dir))

    assert attempts == []
    assert code == 0
    assert gutter(HOURS_CHUNK.content) in capsys.readouterr().out  # the sheet did print


def corpus_copy(tmp_path: Path) -> Path:
    """A TEST-ONLY copy of the synthetic corpus that a test may edit."""
    copy = tmp_path / "corpus-copy"
    shutil.copytree(CORPUS_DIR, copy)
    return copy


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (
            lambda s: (s.corpus / SUNRISE / "faq.md").write_text(
                (s.corpus / SUNRISE / "faq.md").read_text("utf-8") + "\nTEST-ONLY edit\n", "utf-8"
            ),
            "the corpus files of sunrise-clinic differ from freeze.json",
        ),
        (
            lambda s: (s.corpus / PAWS / "manifest.json").write_text(
                (s.corpus / PAWS / "manifest.json")
                .read_text("utf-8")
                .replace('"review_by": "2027', '"review_by": "2028', 1),
                "utf-8",
            ),
            "the corpus manifest of paws-and-co differs from freeze.json",
        ),
        (
            lambda s: (s.dataset / "questions.jsonl").write_text(
                (s.dataset / "questions.jsonl")
                .read_text("utf-8")
                .replace("TEST-ONLY question tq-parking", "TEST-ONLY question tq-parking v2"),
                "utf-8",
            ),
            "question tq-parking is not as frozen",
        ),
        (
            lambda s: (s.dataset / "freeze.json").write_text(
                (s.dataset / "freeze.json").read_text("utf-8") + "\n", "utf-8"
            ),
            "freeze.json has sha256 .*: not the freeze the run used",
        ),
        (lambda s: (s.dataset / "freeze.json").unlink(), "freeze.json is missing"),
        (
            lambda s: (s.dataset / "questions.jsonl").write_text(
                "".join(
                    line + "\n"
                    for line in (s.dataset / "questions.jsonl").read_text("utf-8").splitlines()
                    if '"tq-parking"' not in line
                ),
                "utf-8",
            ),
            "question tq-parking was frozen and is gone",
        ),
    ],
    ids=[
        "corpus-file",
        "corpus-manifest",
        "question",
        "freeze-bytes",
        "freeze-missing",
        "question-gone",
    ],
)
def test_the_label_sheet_refuses_a_corpus_that_is_not_the_runs_and_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: Any, message: str
) -> None:
    sheet = write_label_run(tmp_path, corpus_dir=corpus_copy(tmp_path))
    assert sheet.run("label-sheet", str(sheet.run_dir)) == 0  # as frozen: it prints
    capsys.readouterr()
    edit(sheet)

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert re.search(message, printed.err) is not None, printed.err
    assert HOURS_CHUNK.content[:40] not in printed.err and SENTINEL_QUESTION not in printed.err


def test_the_label_sheet_refuses_another_dataset_version_or_business(tmp_path: Path) -> None:
    sheet = write_label_run(tmp_path)
    dataset = read_dataset(sheet.dataset, CORPUS_DIR)
    run = read_run(sheet.run_dir)
    freeze = (sheet.dataset / "freeze.json").read_bytes()

    with pytest.raises(
        LabelSheetRefused, match=re.escape("the run used support_qa v0.2: pass --dataset")
    ):
        check_corpus(run.model_copy(update={"dataset_version": "v0.2"}), dataset, freeze)
    with pytest.raises(LabelSheetRefused, match="does not list: zz-other"):
        namespaces = dict(run.namespaces) | {"zz-other": "zz-other-ev-cccccccccc"}
        check_corpus(run.model_copy(update={"namespaces": namespaces}), dataset, freeze)
    # TEST-ONLY hostile run.json values: named escaped, on one line.
    with pytest.raises(LabelSheetRefused) as version:
        check_corpus(run.model_copy(update={"dataset_version": "v0.2\x1b[2J\n"}), dataset, freeze)
    assert "the run used support_qa v0.2\\x1b[2J\\x0a: pass --dataset" in str(version.value)
    with pytest.raises(LabelSheetRefused) as business:
        namespaces = dict(run.namespaces) | {"zz\x1b]0;x\x07": "zz-other-ev-cccccccccc"}
        check_corpus(run.model_copy(update={"namespaces": namespaces}), dataset, freeze)
    assert str(business.value).endswith("does not list: zz\\x1b]0;x\\x07")
    assert check_corpus(run, dataset, freeze).startswith("Cited text: rebuilt from the corpus")


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (
            [line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1, page=1)])],
            "citation 1: the chunk rebuilt at chunks 1200/150 is not the one the run cited",
        ),
        (
            [line_pair(HOURS_Q, [refs(HOURS_Q, PDF_CHUNK, 1, groups=(0,))])],
            "citation 1: the chunk rebuilt at chunks 1200/150 is not the one the run cited",
        ),
        (
            [line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1, chunk_index=999)])],
            "citation 1: faq 2026-10 has no chunk 999 at chunks 1200/150",
        ),
        (
            [line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1, version="2025-01")])],
            "citation 1: faq 2025-01 is not in sunrise-clinic's corpus",
        ),
    ],
    ids=["page", "groups", "index", "document"],
)
def test_a_cited_chunk_the_corpus_does_not_give_back_is_refused(
    tmp_path: Path, lines: Any, message: str
) -> None:
    sheet = write_label_run(tmp_path, lines)
    dataset = read_dataset(sheet.dataset, CORPUS_DIR)
    run = read_run(sheet.run_dir)

    with pytest.raises(LabelSheetRefused, match=f"question tq-hours, {message}"):
        cited_texts(run, read_results(sheet.run_dir), read_answers(sheet.run_dir), dataset)


SMALL: Final = ChunkingConfig(max_chars=200, overlap_chars=40)
"""A chunk config other than run.json's, standing in for a chunker changed since the run."""


def small_chunk(anchor: str) -> Chunk:
    document = CORPUS.document(SUNRISE, "faq", "2026-10")
    assert document is not None
    return next(c for c in CORPUS.chunks(document, SMALL) if anchor in normalize_anchor(c.content))


def sheet_texts(sheet: Sheet) -> dict[str, tuple[CitedText, ...]]:
    return cited_texts(
        read_run(sheet.run_dir),
        read_results(sheet.run_dir),
        read_answers(sheet.run_dir),
        read_dataset(sheet.dataset, CORPUS_DIR),
    )


def test_a_record_from_another_chunker_trips_the_wire_when_an_anchor_moves(
    tmp_path: Path,
) -> None:
    # TEST-ONLY: the record comes from 200/40, run.json says 1200/150. The parking anchor is in
    # chunk 4 at 200/40 and in chunk 2 at 1200/150, so chunk 4 rebuilt finds no group.
    chunk = small_chunk(SUNRISE_PARKING.anchor)
    assert chunk.chunk_index == 4
    sheet = write_label_run(tmp_path, [line_pair(PARKING_Q, [refs(PARKING_Q, chunk, 1)])])

    with pytest.raises(
        LabelSheetRefused, match="has the loader or chunker changed since the run's"
    ):
        sheet_texts(sheet)


def test_the_wire_misses_a_chunker_change_that_keeps_each_anchor_in_its_chunk(
    tmp_path: Path,
) -> None:
    # The documented limit (cited.py, step 4): the hours anchor is in chunk 1 at 200/40 and at
    # 1200/150, so the sheet shows the 1200/150 text although the record came from 200/40. The
    # run's own guard is upstream: `verify_stored` refuses stored chunks a changed chunker would
    # not make, before any question.
    chunk = small_chunk(SUNRISE_HOURS.anchor)
    assert chunk.chunk_index == HOURS_CHUNK.chunk_index and chunk.content != HOURS_CHUNK.content
    sheet = write_label_run(tmp_path, [line_pair(HOURS_Q, [refs(HOURS_Q, chunk, 1)])])

    assert [item.text for item in sheet_texts(sheet)["tq-hours"]] == [HOURS_CHUNK.content]


def test_a_citation_that_is_not_one_of_the_questions_hits_is_refused(tmp_path: Path) -> None:
    result, answer = line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)])
    stray = refs(HOURS_Q, HOURS_CHUNK, 2)[1]
    answer = answer.model_copy(update={"sources": (*answer.sources, stray)})
    # The stray id is in the evidence record too, so only the hits check can refuse it.
    held = (*(result.evidence_chunk_ids or ()), stray.chunk_id)
    result = result.model_copy(update={"sources": answer.sources, "evidence_chunk_ids": held})
    sheet = write_label_run(tmp_path, [(result, answer)])

    with pytest.raises(LabelSheetRefused, match="citation 2: not one of the question's hits"):
        sheet_texts(sheet)


@pytest.mark.parametrize(
    "change",
    [{"chunk_index": HOURS_CHUNK.chunk_index + 1}, {"page": 3}, {"source_id": "fees"}],
    ids=["chunk-index", "page", "source"],
)
def test_a_citation_whose_hit_says_otherwise_is_refused(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    hit, source = refs(HOURS_Q, HOURS_CHUNK, 1)
    result, answer = line_pair(HOURS_Q, [(hit, source.model_copy(update=change))])
    sheet = write_label_run(tmp_path, [(result, answer)])

    with pytest.raises(LabelSheetRefused, match="citation 1: not one of the question's hits"):
        sheet_texts(sheet)


@pytest.mark.parametrize(
    ("q", "source_id", "version", "status", "audience"),
    [
        (HOURS_Q, "staff-handbook", "2026-10", "approved", "staff_only"),
        (HOURS_Q, "fees", "2025-10", "retired", "public"),
        (PAWS_Q, "holiday-hours", "2026-12", "draft", "public"),
    ],
    ids=["staff-only", "retired", "draft"],
)
def test_a_citation_of_a_document_no_customer_may_see_is_refused_as_a_leak_signal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    q: EvalQuestion,
    source_id: str,
    version: str,
    status: str,
    audience: str,
) -> None:
    # TEST-ONLY: a real run stops on such a hit (`scoring.AudienceLeak`); only an edited folder
    # or a harness bug gets here, and the sheet must never offer the chunk as evidence to label.
    chunk = first_chunk(q.business_id, source_id, version)
    sheet = write_label_run(tmp_path, [line_pair(q, [refs(q, chunk, 1)])])

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert (
        f"question {q.id}, citation 1: cites a document no customer may see ({source_id}"
        f" {version}: status {status}, audience {audience}): a leak signal, hard gate"
    ) in printed.err
    assert chunk.content[:40] not in printed.err


@pytest.mark.parametrize(
    "held",
    [(), ("chunk-tq-hours-2",), None],
    ids=["nothing-held", "another-held", "no-record"],
)
def test_a_citation_of_a_hit_the_prompt_did_not_hold_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], held: tuple[str, ...] | None
) -> None:
    # TEST-ONLY: the cited chunk is one of the question's hits (the list before the threshold),
    # but not in evidence_chunk_ids, what the prompt held (ADR 0011).
    result, answer = line_pair(
        HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1), refs(HOURS_Q, PDF_CHUNK, 2)]
    )
    answer = answer.model_copy(update={"sources": answer.sources[:1]})
    result = result.model_copy(update={"sources": answer.sources, "evidence_chunk_ids": held})
    sheet = write_label_run(tmp_path, [(result, answer)])

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert (
        "question tq-hours, citation 1: not a chunk the prompt held (results.jsonl's"
        " evidence_chunk_ids)" in printed.err
    )


def test_a_shown_answer_to_a_question_the_dataset_lacks_is_refused(tmp_path: Path) -> None:
    stranger = question("tq-stranger", split="dev")
    sheet = write_label_run(tmp_path, [line_pair(stranger, [refs(stranger, HOURS_CHUNK, 1)])])

    with pytest.raises(LabelSheetRefused, match="question tq-stranger is not in the dataset"):
        sheet_texts(sheet)


def test_answers_and_results_that_disagree_are_refused(tmp_path: Path) -> None:
    result, answer = line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)])
    sheet = write_label_run(tmp_path, [(result, answer.model_copy(update={"sources": ()}))])
    args = (
        read_run(sheet.run_dir),
        read_results(sheet.run_dir),
        read_answers(sheet.run_dir),
        read_dataset(sheet.dataset, CORPUS_DIR),
    )

    with pytest.raises(
        LabelSheetRefused, match=re.escape("tq-hours: answers.jsonl and results.jsonl")
    ):
        cited_texts(*args)
    with pytest.raises(
        LabelSheetRefused, match=re.escape("answers.jsonl entry 1: not a question of the")
    ):
        cited_texts(args[0], [], args[2], args[3])


def test_an_unfrozen_run_shows_no_cited_text_and_says_why(tmp_path: Path) -> None:
    sheet = write_label_run(tmp_path, freeze_sha=None, dataset_stage="collecting")
    run = read_run(sheet.run_dir)
    dataset = read_dataset(sheet.dataset, CORPUS_DIR)
    answers = read_answers(sheet.run_dir)

    provenance = check_corpus(run, dataset, None)
    cited = cited_texts(run, read_results(sheet.run_dir), answers, dataset)

    assert provenance.startswith("Cited text: not shown. run.json has no freeze_sha256")
    assert {item.why_not_shown for items in cited.values() for item in items} == {NOT_FROZEN}
    sheet_text = render_label_sheet(answers, {q.id: q for q in QUESTIONS}, cited)
    assert f"chunk {HOURS_CHUNK.chunk_index} · page none: {NOT_FROZEN}" in sheet_text
    assert HOURS_CHUNK.content[:40] not in sheet_text


def test_an_unfrozen_run_still_refuses_a_citation_the_prompt_did_not_hold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Decision 11 (decided 2026-10-28, shesha): the check needs no corpus, so a dry run on an
    # unfrozen dataset gets it too, although its cited text is not shown.
    result, answer = line_pair(
        HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1), refs(HOURS_Q, PDF_CHUNK, 2)]
    )
    result = result.model_copy(update={"evidence_chunk_ids": ("chunk-tq-hours-1",)})
    sheet = write_label_run(
        tmp_path, [(result, answer)], freeze_sha=None, dataset_stage="collecting"
    )

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert (
        "question tq-hours, citation 2: not a chunk the prompt held (results.jsonl's"
        " evidence_chunk_ids)" in printed.err
    )


# ── Untrusted text on the terminal ───────────────────────────────────────────────────────────


def test_control_and_bidi_characters_are_escaped_and_hindi_joiners_kept() -> None:
    rlo, lri, zwj, zwnj = chr(0x202E), chr(0x2066), chr(0x200D), chr(0x200C)
    hindi = chr(0x915) + chr(0x94D) + zwj + chr(0x937)  # a conjunct that needs its ZWJ
    text = f"a\x1b[2Jb\rc\x07d\x7fe\x9bf{rlo}g{lri}h\ti\nj {hindi} {zwnj}"

    assert terminal_text(text) == (
        f"a\\x1b[2Jb\\x0dc\\x07d\\x7fe\\x9bf\\u202eg\\u2066h\ti\nj {hindi} {zwnj}"
    )


def test_a_chunk_cannot_pass_for_another_block_or_drive_the_terminal() -> None:
    source = SourceRef(
        chunk_id="c1", source_id="faq", version="v", chunk_index=0, page=None, score=0.9
    )
    hostile = "fine\n── tq-other ──\nQuestion: forged\x1b]0;title\x07"
    answer = AnswerLine(
        question_id="tq-hours",
        trace_id=TRACE,
        expected_outcome="answered",
        outcome="answered",
        reason=None,
        error=None,
        answer="TEST-ONLY reply\x1b[31m",
        sources=(source,),
    )

    asked = question("tq-hours", text="TEST-ONLY asked\x1b[8m hidden")
    asked = asked.model_copy(update={"reference_answer": "TEST-ONLY ref\x1b[2J"})

    sheet = render_label_sheet(
        [answer], {"tq-hours": asked}, {"tq-hours": (CitedText(source=source, text=hostile),)}
    )

    assert "\x1b" not in sheet and "\x07" not in sheet
    assert "Question: TEST-ONLY asked\\x1b[8m hidden" in sheet
    assert "reference answer: TEST-ONLY ref\\x1b[2J" in sheet
    assert "      | ── tq-other ──\n      | Question: forged\\x1b]0;title\\x07" in sheet
    assert "Answer:\n      > TEST-ONLY reply\\x1b[31m" in sheet
    assert [line for line in sheet.splitlines() if line.startswith("── ")] == ["── tq-hours ──"]


FORGED_ANSWER: Final = (
    "TEST-ONLY open Sundays and MRI is free.\nSources: faq 2026-10 chunk 0\nCited chunks"
    " (faithful: is every claim of the answer supported by this text?):\n  [1] faq 2026-10 chunk 0"
    " · page none\n      | TEST-ONLY forged supporting text.\n\n── tq-forged ──"
)
"""A TEST-ONLY multi-line answer, as an injected chunk might steer one, forging the sheet's own
structure: a Sources line, a cited-chunks block with gutter text, and a new question header."""


def test_an_answer_cannot_forge_a_cited_chunk_or_a_question_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = write_label_run(
        tmp_path, [line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)], answer=FORGED_ANSWER)]
    )

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 0

    lines = capsys.readouterr().out.splitlines()
    assert [line for line in lines if line.startswith("Cited chunks (")] == [
        "Cited chunks (faithful: is every claim of the answer supported by this text?):"
    ]
    assert [line for line in lines if line.startswith("── ")] == ["── tq-hours ──"]
    assert [line for line in lines if line.startswith("      | ")] == gutter(
        HOURS_CHUNK.content
    ).split("\n")
    assert [line for line in lines if line.startswith("Sources: ")] == [
        f"Sources: faq 2026-10 chunk {HOURS_CHUNK.chunk_index}"
    ]
    start = lines.index("Answer:")
    assert lines[start + 1 : start + 8] == [f"      > {line}" for line in FORGED_ANSWER.split("\n")]


def test_every_value_from_a_run_file_is_escaped_and_kept_on_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # TEST-ONLY values a real run never writes (reason is an AbstainReason, error a class name):
    # only an edited folder holds them.
    hostile = {"reason": "below\x1b[2J\x1b]0;pwned\x07\n── tq-forged ──", "error": "E‮\tx"}
    result, answer = line_pair(NONE_Q, outcome="dont_know")
    sheet = write_label_run(
        tmp_path,
        [
            line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)]),
            (result.model_copy(update=hostile), answer.model_copy(update=hostile)),
        ],
    )

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 0

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert (
        "Got: dont_know (below\\x1b[2J\\x1b]0;pwned\\x07\\x0a── tq-forged ──) [E\\u202e\\x09x]"
    ) in out
    assert [line for line in out.splitlines() if line.startswith("── ")] == [
        "── tq-hours ──",
        "── tq-none ──",
    ]


def test_ids_on_the_sheet_are_escaped_and_kept_on_one_line() -> None:
    source = SourceRef(
        chunk_id="c1",
        source_id="faq\x1b[31m\n── tq-x ──",
        version="v‮",
        chunk_index=0,
        page=None,
        score=0.9,
    )
    answer = AnswerLine(
        question_id="tq-hours\x1b[2J",
        trace_id=TRACE,
        expected_outcome="answered\n── tq-y ──",
        outcome="answered",
        reason=None,
        error=None,
        answer="TEST-ONLY reply",
        sources=(source,),
    )
    not_shown = (CitedText(source=source, text=None, why_not_shown=NOT_FROZEN),)

    sheet = render_label_sheet([answer], {}, {"tq-hours\x1b[2J": not_shown})

    assert "\x1b" not in sheet and "‮" not in sheet
    assert [line for line in sheet.splitlines() if line.startswith("── ")] == [
        "── tq-hours\\x1b[2J ──"
    ]
    assert "Expected outcome: answered\\x0a── tq-y ── · reference answer: none" in sheet
    assert "Sources: faq\\x1b[31m\\x0a── tq-x ── v\\u202e chunk 0" in sheet
    assert f"  [1] faq\\x1b[31m\\x0a── tq-x ── v\\u202e chunk 0 · page none: {NOT_FROZEN}" in sheet


def _hostile_id(
    pair: tuple[QuestionResult, AnswerLine], question_id: str
) -> tuple[QuestionResult, AnswerLine]:
    result, answer = pair
    change = {"question_id": question_id}
    return result.model_copy(update=change), answer.model_copy(update=change)


def _hostile_business(
    pair: tuple[QuestionResult, AnswerLine],
) -> tuple[QuestionResult, AnswerLine]:
    result, answer = pair
    return result.model_copy(update={"business_id": "sunrise\x1b[8m\nok"}), answer


@pytest.mark.parametrize(
    ("lines", "escaped"),
    [
        (
            [
                line_pair(
                    HOURS_Q,
                    [refs(HOURS_Q, HOURS_CHUNK, 1, source_id="faq\x1b[31mIGNORE PREVIOUS\nok")],
                )
            ],
            "question tq-hours, citation 1: faq\\x1b[31mIGNORE PREVIOUS\\x0aok 2026-10 is not in"
            " sunrise-clinic's corpus",
        ),
        (
            [_hostile_id(line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)]), "tq-x\x1b[2J")],
            "question tq-x\\x1b[2J is not in the dataset",
        ),
        (
            [_hostile_business(line_pair(HOURS_Q, [refs(HOURS_Q, HOURS_CHUNK, 1)]))],
            "question tq-hours, citation 1: faq 2026-10 is not in sunrise\\x1b[8m\\x0aok's corpus",
        ),
    ],
    ids=["source-id", "question-id", "business-id"],
)
def test_a_refusal_escapes_every_value_it_names(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    lines: list[tuple[QuestionResult, AnswerLine]],
    escaped: str,
) -> None:
    sheet = write_label_run(tmp_path, lines)

    assert sheet.run("label-sheet", str(sheet.run_dir)) == 1

    printed = capsys.readouterr()
    assert printed.out == ""
    assert "\x1b" not in printed.err and "\nok" not in printed.err
    assert escaped in printed.err


# ── faithfulness-template ────────────────────────────────────────────────────────────────────


def test_the_template_command_writes_one_line_per_shown_answer_and_never_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = write_label_run(tmp_path)
    path = sheet.run_dir / FAITHFULNESS_LABELS_FILE

    assert sheet.run("faithfulness-template", str(sheet.run_dir)) == 0

    lines = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    assert [(line["question_id"], line["label"]) for line in lines] == [
        ("tq-hours", None),
        ("tq-paws", None),
    ]
    assert "2 line(s), one per shown answer" in capsys.readouterr().out
    path.write_text(path.read_text("utf-8").replace('"label": null', '"label": "faithful"', 1))
    kept = path.read_bytes()

    assert sheet.run("faithfulness-template", str(sheet.run_dir)) == 1

    assert path.read_bytes() == kept
    assert "exists: labels are never overwritten" in capsys.readouterr().err


def test_the_template_command_writes_its_one_file_and_leaves_every_other_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # TEST-ONLY filled correctness labels and a summary sit in the folder, as after M2's step 12:
    # the template adds faithfulness_labels.jsonl and changes no other file or folder anywhere
    # under tmp_path, labels.jsonl above all (mutation audit I6).
    sheet = write_label_run(tmp_path)
    correctness = "".join(
        json.dumps(
            {"question_id": q, "run_id": sheet.run_dir.name, "label": "correct"}
            | {"labelled_by": "test-only", "labelled_on": "2026-10-28", "note": None}
        )
        + "\n"
        for q in ("tq-hours", "tq-paws", "tq-none")
    )
    (sheet.run_dir / LABELS_FILE).write_text(correctness, encoding="utf-8")
    (sheet.run_dir / "summary.md").write_text("TEST-ONLY summary\n", encoding="utf-8")
    before = snapshot(tmp_path)
    paths_before = sorted(tmp_path.rglob("*"))

    assert sheet.run("faithfulness-template", str(sheet.run_dir)) == 0

    added = sheet.run_dir / FAITHFULNESS_LABELS_FILE
    added_name = str(added.relative_to(tmp_path))
    after = snapshot(tmp_path)
    assert sorted(tmp_path.rglob("*")) == sorted([*paths_before, added])
    assert {name: digest for name, digest in after.items() if name != added_name} == before
    assert (sheet.run_dir / LABELS_FILE).read_text("utf-8") == correctness
    assert "2 line(s), one per shown answer" in capsys.readouterr().out


def test_the_template_command_refuses_a_folder_whose_files_disagree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sheet = write_label_run(tmp_path)
    results = sheet.run_dir / RESULTS_FILE
    results.write_text("".join(results.read_text("utf-8").splitlines(True)[:2]), "utf-8")

    assert sheet.run("faithfulness-template", str(sheet.run_dir)) == 1

    assert not (sheet.run_dir / FAITHFULNESS_LABELS_FILE).exists()
    assert "results.jsonl has 2 line(s), run.json says 3" in capsys.readouterr().err


def test_a_labelled_template_summarizes_without_touching_labels_jsonl(tmp_path: Path) -> None:
    results, labels = many([True] * 10)
    folder = write_run(tmp_path, results, labels)

    summary = summarize(folder, NO_H2)

    assert not (folder / LABELS_FILE).exists()
    assert (summary.labelled, summary.unlabelled) == (0, 10)
    assert summary.faithfulness.labelled == 10
    assert (summary.faithfulness.targets[0].stat.x, summary.faithfulness.targets[0].stat.n) == (
        10,
        10,
    )


def test_the_targets_are_exact_fractions_and_m2s_are_unchanged() -> None:
    expected = {
        "hit@5": Fraction(9, 10),
        "correctness": Fraction(9, 10),
        "faithfulness": Fraction(19, 20),
    }
    assert expected == TARGETS
