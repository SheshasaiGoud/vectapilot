"""H2 (M6) — the extraction metric functions. [HAND-WRITE] (decided D7 (b), 2026-11-11: "shesha
writes H1 `route_fields`; Claude writes H2 at shesha's request"). Written by Claude at shesha's
request on 2026-11-15, on its own branch `m6/extraction-metrics`, reviewed by shesha; the
explain-back (guide §7) is shesha's and part of acceptance.

Guide: docs/learning/m6-metrics.md · Tests: tests/extracteval/test_metrics.py (hand-counted
fixtures, counted before the bodies existed), tests/extracteval/test_metrics_h2.py (the harness
end to end with these five, the review-recall identity on seeded inputs, the edge cases) and
tests/extracteval/test_metrics_types.py (the types below).

**Built by the team and tested:** the input (`ScoredField`, one per (document, field), filled by
the harness from `results.jsonl`), the result types (`Accuracy`, `FieldCount`, `DocumentCount`,
`SilentErrors`, `ReviewPrecision`), each of which checks itself when built, and the bundle the
harness calls them through (`ExtractMetrics`, `H2_EXTRACT_METRICS`; the harness's own tests pass
stand-ins). **H2 is the five functions at the end of this file.**

The scoring rules they follow (docs/plans/m6-pr-checks.md, "Scoring rules"; the README of
`evals/datasets/doc_extraction/v0.1/`):
- **The unit:** a field is one (document, field name); the document is the unit for intervals.
  A field whose document is not scored (`scored` False: not run, or a harness error) is outside
  every n; its document id is listed as not scored where the result type has room for it.
- **Exact counts with ids,** never a float or a bare percentage: `k of n` as ints and the ids
  behind `k`, sorted, each once. A field's id is `<document id>/<field>` (`ScoredField.key`).
- **Wilson on documents** (`evalrun.intervals.wilson_interval`, 95 %), None when n is 0 ("not
  measured"). Field-level intervals are the harness's (a seeded bootstrap over documents).
- **Subsets are the harness's job,** not H2's: per business, per field, per level, accepted
  precision (`field_accuracy` over accepted fields) and review recall (`review_rate` over the
  fields that are not correct) come from calling these five on filtered inputs.

This module never imports `vectapilot_api.extraction` (tested): `correct` arrives computed by the
eval's own comparison (`compare.py`), and statuses and levels as plain strings.
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from vectapilot_api.evalrun.intervals import WilsonInterval, wilson_interval

ACCEPTED: Final = "accepted"
REVIEW: Final = "review"
ABSENT: Final = "absent"
COMPLETE: Final = "complete"
NEEDS_REVIEW: Final = "needs_review"
FIELD_STATUSES: Final = frozenset({ACCEPTED, REVIEW, ABSENT})
DOCUMENT_STATUSES: Final = frozenset({COMPLETE, NEEDS_REVIEW})
LEVELS: Final = ("high", "medium", "low")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sorted_unique(ids: tuple[str, ...]) -> bool:
    return all(isinstance(i, str) for i in ids) and list(ids) == sorted(set(ids))


# ── The input (team-built; the harness fills it from results.jsonl) ───────────────────────────


@dataclass(frozen=True)
class ScoredField:
    """One field of one document, as H2 reads it. A scored field has a status, its document's
    status and `correct`; a field of a document that is not scored has none of them."""

    document: str
    """The document's id."""
    business_id: str
    field: str
    scored: bool
    """False: the document was not run, or the harness could not get an extraction."""
    document_status: str | None
    """`complete` (no field in review) or `needs_review`; None when not scored."""
    status: str | None
    """`accepted`, `review` or `absent`; None when not scored."""
    level: str | None
    """`high`, `medium` or `low` for a value; None without one."""
    reasons: tuple[str, ...]
    """The field's reason codes, sorted (`not_found`, `label_present`, `truncated`, ...)."""
    has_value: bool
    """The system returned a value for the field."""
    gold_null: bool
    """The gold says the document does not give the field."""
    correct: bool | None
    """The value is the gold (`compare.same_value`; both null is correct); None when not scored."""

    def __post_init__(self) -> None:
        filled = (self.status, self.document_status, self.correct)
        if self.scored and any(item is None for item in filled):
            raise ValueError("a scored field has a status, its document's status and correct")
        if not self.scored and (any(item is not None for item in filled) or self.has_value):
            raise ValueError("a field that is not scored has no status, correct or value")
        if self.status is not None and self.status not in FIELD_STATUSES:
            raise ValueError("status is accepted, review or absent")
        if self.document_status is not None and self.document_status not in DOCUMENT_STATUSES:
            raise ValueError("document_status is complete or needs_review")
        if self.level is not None and self.level not in LEVELS:
            raise ValueError("level is high, medium or low")
        if not _sorted_unique(self.reasons):
            raise ValueError("reasons are sorted, each once")

    @property
    def key(self) -> str:
        """`<document id>/<field>`: how every result names a field."""
        return f"{self.document}/{self.field}"


# ── The results (team-built, checked when built) ─────────────────────────────────────────────


@dataclass(frozen=True)
class Accuracy:
    """`correct of n` scored fields, the wrong ones by key, and the documents not scored."""

    correct: int
    n: int
    wrong: tuple[str, ...]
    """The `n - correct` fields that are not correct, as `<document>/<field>`, sorted."""
    not_scored: tuple[str, ...]
    """The ids of the documents whose fields are outside n, sorted, each once."""

    def __post_init__(self) -> None:
        if not (_is_count(self.correct) and _is_count(self.n) and self.correct <= self.n):
            raise ValueError("correct and n are counts (ints >= 0) with correct <= n")
        if not _sorted_unique(self.wrong) or len(self.wrong) != self.n - self.correct:
            raise ValueError("wrong holds the n - correct field keys, sorted, each once")
        if not _sorted_unique(self.not_scored):
            raise ValueError("not_scored holds document ids, sorted, each once")


@dataclass(frozen=True)
class FieldCount:
    """`k of n` scored fields, and the `k` by key (sorted, each once)."""

    k: int
    n: int
    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not (_is_count(self.k) and _is_count(self.n) and self.k <= self.n):
            raise ValueError("k and n are counts (ints >= 0) with k <= n")
        if not _sorted_unique(self.fields) or len(self.fields) != self.k:
            raise ValueError("fields holds the k field keys, sorted, each once")


@dataclass(frozen=True)
class DocumentCount:
    """`x of n` scored documents, the `x` by id, and the Wilson interval (None when n is 0)."""

    x: int
    n: int
    documents: tuple[str, ...]
    interval: WilsonInterval | None

    def __post_init__(self) -> None:
        if not (_is_count(self.x) and _is_count(self.n) and self.x <= self.n):
            raise ValueError("x and n are counts (ints >= 0) with x <= n")
        if not _sorted_unique(self.documents) or len(self.documents) != self.x:
            raise ValueError("documents holds the x document ids, sorted, each once")
        if (self.interval is None) != (self.n == 0):
            raise ValueError("the interval is None exactly when n is 0")
        if self.interval is not None and (self.interval.successes, self.interval.n) != (
            self.x,
            self.n,
        ):
            raise ValueError("the interval is the Wilson interval of x of n")


@dataclass(frozen=True)
class SilentErrors:
    """Silent errors as fields (`k of n` scored fields) and as documents (`x of n` scored
    documents with at least one, with Wilson)."""

    fields: FieldCount
    documents: DocumentCount

    def __post_init__(self) -> None:
        owners = {key.rsplit("/", 1)[0] for key in self.fields.fields}
        if owners != set(self.documents.documents):
            raise ValueError("the documents are exactly those of the silent fields")


@dataclass(frozen=True)
class ReviewPrecision:
    """Reviewed fields with a value: `wrong of n` not correct, the wrong ones by key; and the
    reviewed fields without a value, counted apart: how many, per reason, and how many of them
    had a null gold (review not needed)."""

    wrong: int
    n: int
    fields: tuple[str, ...]
    without_value: int
    by_reason: tuple[tuple[str, int], ...]
    """`(reason, count)` sorted by reason, each reason once, counts above 0: a field without a
    value counts once under each of its reasons."""
    null_gold: int

    def __post_init__(self) -> None:
        if not (_is_count(self.wrong) and _is_count(self.n) and self.wrong <= self.n):
            raise ValueError("wrong and n are counts (ints >= 0) with wrong <= n")
        if not _sorted_unique(self.fields) or len(self.fields) != self.wrong:
            raise ValueError("fields holds the wrong field keys, sorted, each once")
        if not (_is_count(self.without_value) and _is_count(self.null_gold)):
            raise ValueError("without_value and null_gold are counts (ints >= 0)")
        if self.null_gold > self.without_value:
            raise ValueError("null_gold counts fields without a value, so at most without_value")
        reasons = [reason for reason, _ in self.by_reason]
        if not _sorted_unique(tuple(reasons)):
            raise ValueError("by_reason is sorted by reason, each once")
        if any(not _is_count(count) or count == 0 for _, count in self.by_reason):
            raise ValueError("by_reason counts are ints above 0")
        if any(count > self.without_value for _, count in self.by_reason):
            raise ValueError("no reason counts more fields than are without a value")
        if (self.without_value == 0) != (not self.by_reason):
            raise ValueError("a field in review has at least one reason")


# ── H2: the five functions ([HAND-WRITE], D7 (b)) ─────────────────────────────────────────────


def _scored(fields: Sequence[ScoredField]) -> list[ScoredField]:
    """The fields inside every n: those of scored documents."""
    return [item for item in fields if item.scored]


def _documents(fields: Sequence[ScoredField], hits: set[str]) -> DocumentCount:
    """`x of n` scored documents: n the scored documents of `fields` (each id once), x those in
    `hits`, with the Wilson interval (None when n is 0)."""
    scored = {item.document for item in _scored(fields)}
    found = tuple(sorted(scored & hits))
    interval = wilson_interval(len(found), len(scored)) if scored else None
    return DocumentCount(x=len(found), n=len(scored), documents=found, interval=interval)


def _is_silent(item: ScoredField) -> bool:
    """A wrong field nobody will look at (`silent_errors`' contract); `item` is scored."""
    if item.status == ACCEPTED:
        return not item.correct
    if item.status == ABSENT:
        return not item.gold_null
    return False  # review: a person looks at it


def field_accuracy(fields: Sequence[ScoredField]) -> Accuracy:
    """Field accuracy over `fields` (guide §1).

    Contract:
    - n is the scored fields (`scored` True), whatever their status: accepted, review or absent,
      E3's all-review fields included.
    - `correct` counts those with `correct` True; `wrong` names the others by `key`, sorted.
    - `not_scored` names each document with a field that is not scored, once, sorted.
    - An empty input is `Accuracy(0, 0, (), ())`.
    """
    scored = _scored(fields)
    wrong = tuple(sorted(item.key for item in scored if not item.correct))
    not_scored = tuple(sorted({item.document for item in fields if not item.scored}))
    return Accuracy(
        correct=len(scored) - len(wrong), n=len(scored), wrong=wrong, not_scored=not_scored
    )


def silent_errors(fields: Sequence[ScoredField]) -> SilentErrors:
    """Silent errors over `fields` (guide §2). Target 0: they judge the routing.

    Contract:
    - A scored field is a silent error when its status is `accepted` and it is not correct, or
      its status is `absent` and its gold has a value (`gold_null` False). An accepted value
      whose gold is null (invented, hidden-only or planted) is not correct, so it is silent too.
      A field in `review` is never one: a person looks at it.
    - `fields`: k silent fields of n scored fields (all of them, the field-accuracy base:
      decided 2026-11-15, shesha), the k by key, sorted.
    - `documents`: x of n scored documents (each document id once) with at least one silent
      field, the x ids sorted, and `wilson_interval(x, n)` (`evalrun/intervals.py`, 95 %), None
      when n is 0.
    """
    scored = _scored(fields)
    silent = [item for item in scored if _is_silent(item)]
    keys = tuple(sorted(item.key for item in silent))
    return SilentErrors(
        fields=FieldCount(k=len(keys), n=len(scored), fields=keys),
        documents=_documents(fields, {item.document for item in silent}),
    )


def review_rate(fields: Sequence[ScoredField]) -> FieldCount:
    """Review rate over `fields` (guide §3): the staff work the routing asks for.

    Contract: k is the scored fields whose status is `review`, n all scored fields; the k by
    key, sorted. (The harness also calls it on the fields that are not correct: that k of n is
    review recall, and the n - k are its misses.)
    """
    scored = _scored(fields)
    keys = tuple(sorted(item.key for item in scored if item.status == REVIEW))
    return FieldCount(k=len(keys), n=len(scored), fields=keys)


def review_precision(fields: Sequence[ScoredField]) -> ReviewPrecision:
    """Review precision over `fields` (guide §4).

    Contract, over the scored fields whose status is `review`:
    - with a value (`has_value`): `n` of them, `wrong` those not correct, `fields` the wrong ones
      by key, sorted;
    - without a value: `without_value` of them; `by_reason` counts them under each of their
      reasons (a field with two reasons counts under both), as `(reason, count)` sorted by
      reason, reasons with no field left out; `null_gold` those whose gold is null too (the
      document does not give the field, so the review was not needed).
    """
    reviewed = [item for item in _scored(fields) if item.status == REVIEW]
    with_value = [item for item in reviewed if item.has_value]
    without_value = [item for item in reviewed if not item.has_value]
    wrong = tuple(sorted(item.key for item in with_value if not item.correct))
    reasons = Counter(reason for item in without_value for reason in item.reasons)
    return ReviewPrecision(
        wrong=len(wrong),
        n=len(with_value),
        fields=wrong,
        without_value=len(without_value),
        by_reason=tuple(sorted(reasons.items())),
        null_gold=sum(1 for item in without_value if item.gold_null),
    )


def straight_through(fields: Sequence[ScoredField]) -> DocumentCount:
    """Straight-through documents over `fields` (guide §5).

    Contract: n is the scored documents (each document id once). A document is straight-through
    when its `document_status` is `complete` and every one of its fields in `fields` is correct.
    `documents` names the x straight-through ids, sorted; `interval` is
    `wilson_interval(x, n)` (95 %), None when n is 0.
    """
    scored = _scored(fields)
    blocked = {
        item.document for item in scored if item.document_status != COMPLETE or not item.correct
    }
    return _documents(fields, {item.document for item in scored} - blocked)


# ── How the harness calls H2 ─────────────────────────────────────────────────────────────────

Fields = Sequence[ScoredField]


@dataclass(frozen=True)
class ExtractMetrics:
    """The five H2 functions the harness calls (signatures as above)."""

    field_accuracy: Callable[[Fields], Accuracy]
    silent_errors: Callable[[Fields], SilentErrors]
    review_rate: Callable[[Fields], FieldCount]
    review_precision: Callable[[Fields], ReviewPrecision]
    straight_through: Callable[[Fields], DocumentCount]


H2_EXTRACT_METRICS: Final = ExtractMetrics(
    field_accuracy=field_accuracy,
    silent_errors=silent_errors,
    review_rate=review_rate,
    review_precision=review_precision,
    straight_through=straight_through,
)
"""H2 as written (2026-11-15). The harness still handles a bundle whose functions raise
`NotImplementedError` (no summary, results kept: `runs.write_summary`; tested with a stand-in)."""
