"""H2 as written (`extracteval/metrics.py`), beyond the hand-counted fixtures of test_metrics.py.

- **End to end:** a dry harness run over the committed TEST-ONLY fixtures with the stand-in
  router (deliberately not H1) and the real `H2_EXTRACT_METRICS` writes a consistent summary and
  exits as a clean dry run does; `summarize` agrees. With the stand-in router every missing
  value goes to review, so this run has no `absent`, withheld or unverified field: those paths
  (the absent half of silent errors and of the identity) are covered by the seeded test and
  test_metrics.py only. Revisit once H1 exists and a dry run can produce absent fields.
- **The identity on many inputs:** seeded random fields that keep `ScoredField`'s rules and the
  data's own (an absent field has no value; a value against a null gold is wrong; review has a
  reason): review recall's misses are exactly the silent errors, every field-level n is the same,
  and straight-through documents are the complete documents with no wrong field.
- **Edge cases the contracts state.**

Every field here is made up (or a dry run's plumbing); no number here is a result.
"""

import random
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from tests.extracteval.fakes import STAND_IN_ROUTE
from tests.extracteval.harness_helpers import dry_args, dry_repo, io_for
from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.extracteval.cli import harness_main
from vectapilot_api.extracteval.metrics import (
    ABSENT,
    ACCEPTED,
    COMPLETE,
    H2_EXTRACT_METRICS,
    LEVELS,
    NEEDS_REVIEW,
    REVIEW,
    Accuracy,
    DocumentCount,
    FieldCount,
    ReviewPrecision,
    ScoredField,
    SilentErrors,
    field_accuracy,
    review_precision,
    review_rate,
    silent_errors,
    straight_through,
)
from vectapilot_api.extracteval.runinfo import (
    DRY_EXTRACT_REPORTS,
    DRY_LABEL,
    SUMMARY_MD,
    read_results,
)
from vectapilot_api.extracteval.summary import scored_fields

S, P = "sunrise-clinic", "paws-and-co"
SEED: Final = 20261115
CASES: Final = 400
REASONS: Final = ("image_page", "label_present", "not_found", "quote_not_found", "truncated")


def scored(
    document: str,
    field: str,
    status: str,
    *,
    doc_status: str = NEEDS_REVIEW,
    level: str | None = None,
    reasons: Sequence[str] = (),
    value: bool = True,
    gold_null: bool = False,
    correct: bool = True,
) -> ScoredField:
    return ScoredField(
        document=document,
        business_id=S,
        field=field,
        scored=True,
        document_status=doc_status,
        status=status,
        level=level,
        reasons=tuple(reasons),
        has_value=value,
        gold_null=gold_null,
        correct=correct,
    )


def not_scored(document: str, field: str) -> ScoredField:
    return ScoredField(
        document=document,
        business_id=S,
        field=field,
        scored=False,
        document_status=None,
        status=None,
        level=None,
        reasons=(),
        has_value=False,
        gold_null=False,
        correct=None,
    )


# ── End to end: the harness with the real H2 ─────────────────────────────────────────────────


def test_a_dry_run_with_the_real_h2_is_consistent_and_summarize_agrees(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    io_ = io_for(repo, route=STAND_IN_ROUTE, metrics=H2_EXTRACT_METRICS)
    code = harness_main(dry_args(dataset_dir), io_)
    printed = capsys.readouterr()
    base = repo.root / DRY_EXTRACT_REPORTS / "v0.1"
    folders = [p for p in base.iterdir() if p.is_dir()]
    assert len(folders) == 1, folders
    folder = folders[0]

    assert code == 0, printed.err
    summary = (folder / SUMMARY_MD).read_text("utf-8")
    lines = summary.splitlines()
    assert lines[0].startswith("# doc_extraction run") and lines[2].startswith(f"**{DRY_LABEL}**")
    assert "Inconsistent" not in summary and "Inconsistent" not in printed.err

    # The summary shows the real H2's results over the run's own fields, not a stand-in's.
    fields = scored_fields(read_results(folder))
    silent = silent_errors(fields).fields.fields
    assert f"Silent fields: {', '.join(silent) if silent else 'none'}" in summary
    accuracy = field_accuracy(fields)
    # Anchored to its own section: the silent-errors table has an "| all | k of n |" row too.
    section = summary.split("\n## Field accuracy", 1)[1].split("\n## ", 1)[0]
    all_rows = [row for row in section.splitlines() if row.startswith("| all |")]
    assert accuracy.n > 0
    assert len(all_rows) == 1 and all_rows[0].startswith(
        f"| all | {accuracy.correct} of {accuracy.n} |"
    )

    assert harness_main(["summarize", str(folder)], io_) == 0
    assert (folder / SUMMARY_MD).read_text("utf-8") == summary


# ── The identity on many inputs (seeded) ─────────────────────────────────────────────────────


def _field(rng: random.Random, document: str, business: str, name: str) -> ScoredField:
    """One scored field that keeps the data's rules (module docstring); document status later."""
    status = rng.choice((ACCEPTED, REVIEW, ABSENT))
    gold_null = rng.random() < 0.3
    has_value = status == ACCEPTED or (status == REVIEW and rng.random() < 0.5)
    if has_value:
        correct = False if gold_null else rng.random() < 0.6
        level: str | None = rng.choice(LEVELS)
    else:
        correct = gold_null  # no value: correct exactly when the gold is null too
        level = None
    reasons: tuple[str, ...] = ()
    if status == REVIEW:
        reasons = tuple(sorted(rng.sample(REASONS, rng.randint(1, 2))))
    return ScoredField(
        document=document,
        business_id=business,
        field=name,
        scored=True,
        document_status=NEEDS_REVIEW,
        status=status,
        level=level,
        reasons=reasons,
        has_value=has_value,
        gold_null=gold_null,
        correct=correct,
    )


def _case(rng: random.Random) -> list[ScoredField]:
    """Several documents, some not scored, in a shuffled order."""
    out: list[ScoredField] = []
    for number in range(rng.randint(0, 8)):
        document = f"d{number}"
        business = rng.choice((S, P))
        names = [f"f{i}" for i in range(rng.randint(1, 5))]
        if rng.random() < 0.2:
            out += [not_scored(document, name) for name in names]
            continue
        fields = [_field(rng, document, business, name) for name in names]
        status = NEEDS_REVIEW if any(x.status == REVIEW for x in fields) else COMPLETE
        out += [replace(x, document_status=status) for x in fields]
    rng.shuffle(out)
    return out


def test_review_recalls_misses_are_the_silent_errors_on_seeded_inputs() -> None:
    rng = random.Random(SEED)  # noqa: S311 (seeded test data, not crypto)
    seen_silent = seen_straight = seen_not_scored = seen_reviewed_value_null_gold = 0
    for _ in range(CASES):
        fields = _case(rng)
        in_n = [x for x in fields if x.scored]
        wrong = [x for x in in_n if x.correct is False]
        recall = review_rate(wrong)
        misses = sorted({x.key for x in wrong} - set(recall.fields))
        silent = silent_errors(fields)
        assert list(silent.fields.fields) == misses, fields

        accuracy = field_accuracy(fields)
        assert accuracy.n == review_rate(fields).n == silent.fields.n == len(in_n)
        assert accuracy.correct + len(wrong) == accuracy.n

        documents = {x.document for x in in_n}
        expected = sorted(
            d
            for d in documents
            if all(x.document_status == COMPLETE and x.correct for x in in_n if x.document == d)
        )
        straight = straight_through(fields)
        assert (list(straight.documents), straight.n) == (expected, len(documents))
        assert silent.documents.n == straight.n

        # Review precision splits the reviewed fields by "has a value"; "review not needed"
        # (null_gold) counts only the reviewed fields without a value.
        reviewed = [x for x in in_n if x.status == REVIEW]
        precision = review_precision(fields)
        assert precision.n + precision.without_value == review_rate(fields).k == len(reviewed)
        assert precision.null_gold == sum(1 for x in reviewed if not x.has_value and x.gold_null)
        assert precision.wrong == sum(1 for x in reviewed if x.has_value and not x.correct)

        seen_silent += silent.fields.k > 0
        seen_straight += straight.x > 0
        seen_not_scored += bool(accuracy.not_scored)
        seen_reviewed_value_null_gold += any(x.has_value and x.gold_null for x in reviewed)
    # The generator really reaches the interesting cases, not only empty ones.
    seen = (seen_silent, seen_straight, seen_not_scored, seen_reviewed_value_null_gold)
    assert min(seen) > CASES // 10


# ── Edge cases the contracts state ───────────────────────────────────────────────────────────

NOTHING_SILENT: Final = SilentErrors(FieldCount(0, 0, ()), DocumentCount(0, 0, (), None))
NO_REVIEW: Final = ReviewPrecision(
    wrong=0, n=0, fields=(), without_value=0, by_reason=(), null_gold=0
)


def test_empty_input_for_all_five() -> None:
    assert field_accuracy(()) == Accuracy(0, 0, (), ())
    assert silent_errors(()) == NOTHING_SILENT
    assert review_rate(()) == FieldCount(0, 0, ())
    assert review_precision(()) == NO_REVIEW
    assert straight_through(()) == DocumentCount(0, 0, (), None)


def test_only_unscored_fields_are_outside_every_n() -> None:
    fields = [not_scored("d2", "a"), not_scored("d1", "a"), not_scored("d2", "b")]
    assert field_accuracy(fields) == Accuracy(0, 0, (), ("d1", "d2"))
    assert silent_errors(fields) == NOTHING_SILENT
    assert review_rate(fields) == FieldCount(0, 0, ())
    assert review_precision(fields) == NO_REVIEW
    assert straight_through(fields) == DocumentCount(0, 0, (), None)


def test_a_field_with_two_reasons_counts_under_both() -> None:
    # One reviewed field without a value, two reasons: without_value 1, each reason 1.
    fields = [
        # No value against a gold value: not correct (the data rule in the module docstring).
        scored(
            "d1",
            "a",
            REVIEW,
            reasons=("image_page", "label_present"),
            value=False,
            correct=False,
        ),
        scored("d1", "b", ACCEPTED, level="high"),
    ]
    expected = ReviewPrecision(
        wrong=0,
        n=0,
        fields=(),
        without_value=1,
        by_reason=(("image_page", 1), ("label_present", 1)),
        null_gold=0,
    )
    assert review_precision(fields) == expected


def test_an_accepted_value_with_a_null_gold_is_silent_and_blocks_straight_through() -> None:
    fields = [
        scored(
            "d1", "a", ACCEPTED, doc_status=COMPLETE, level="high", gold_null=True, correct=False
        )
    ]
    assert silent_errors(fields) == SilentErrors(
        FieldCount(1, 1, ("d1/a",)), DocumentCount(1, 1, ("d1",), wilson_interval(1, 1))
    )
    assert straight_through(fields) == DocumentCount(0, 1, (), wilson_interval(0, 1))


def test_null_gold_counts_only_reviewed_fields_without_a_value() -> None:
    # d1/a: a reviewed value whose gold is null (an invented value sent to review) is a wrong
    # reviewed value, not "review not needed". d1/b: reviewed, no value, null gold.
    # Hand count: n 1 (d1/a), wrong 1 (d1/a), without_value 1 (d1/b), not_found 1, null_gold 1.
    fields = [
        scored(
            "d1",
            "a",
            REVIEW,
            level="low",
            reasons=("label_present",),
            gold_null=True,
            correct=False,
        ),
        scored("d1", "b", REVIEW, reasons=("not_found",), value=False, gold_null=True),
    ]
    expected = ReviewPrecision(
        wrong=1, n=1, fields=("d1/a",), without_value=1, by_reason=(("not_found", 1),), null_gold=1
    )
    assert review_precision(fields) == expected


def test_a_reviewed_value_with_a_null_gold_is_not_review_not_needed() -> None:
    # d1/a: as above. d1/b: a value dropped by "no quote, no value" against a real gold: no value,
    # not correct. Hand count: n 1, wrong 1, without_value 1, quote_not_found 1, null_gold 0.
    fields = [
        scored(
            "d1", "a", REVIEW, level="medium", reasons=("unsure",), gold_null=True, correct=False
        ),
        scored("d1", "b", REVIEW, reasons=("quote_not_found",), value=False, correct=False),
    ]
    expected = ReviewPrecision(
        wrong=1,
        n=1,
        fields=("d1/a",),
        without_value=1,
        by_reason=(("quote_not_found", 1),),
        null_gold=0,
    )
    assert review_precision(fields) == expected


def test_straight_through_reads_document_status_on_a_field_subset() -> None:
    # d1 is needs_review because of a field that is not in `fields` (the harness may pass a
    # subset): not straight-through, although every given field is accepted and correct.
    # Hand count: d2 only, x 1 of n 2.
    fields = [
        scored("d1", "a", ACCEPTED, doc_status=NEEDS_REVIEW, level="high"),
        scored("d2", "a", ACCEPTED, doc_status=COMPLETE, level="high"),
    ]
    assert straight_through(fields) == DocumentCount(1, 2, ("d2",), wilson_interval(1, 2))
