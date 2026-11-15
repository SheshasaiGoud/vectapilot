"""Team stand-ins for the extraction harness's tests. **None of these is H2 or H1.**

- `canned_metrics` returns fixed results whatever the input: it lets the summary and the harness
  be tested before H2 (`extracteval/metrics.py`) is written, and keeps their tests independent of
  it. It computes nothing, so it can never pass for the real definitions
  (tests/extracteval/test_metrics.py holds those, as strict xfails until H2 is written).
- `consistent_metrics` is `canned_metrics` with nothing silent and every scored field it is given
  "in review": review recall's misses (none) are then exactly the silent errors (none) whatever
  the input, so a run can end with exit 0. It never reads a status, a level or `correct`, so it
  cannot pass for H2 either. `canned_metrics` breaks that identity on most inputs, which is how
  the tests reach the summary's "Inconsistent" path.
- `RecordingMetrics` is `canned_metrics` that also records which fields each call was given, so a
  test can check the subsets the harness hands to H2 (per business, per level, accepted, the
  fields that are not correct).
- `UNWRITTEN_H2` raises `NotImplementedError` from every function, as the stubs do, so the
  harness's "H2 not written yet" path stays tested once H2 is written.
- The router stand-in is `tests/extraction/standins.py`'s `accept_everything` (deliberately not
  H1: every value accepted, every missing value absent; the invariant check then forces back to
  review what the signals do not justify).
- `unwritten_h1` raises `NotImplementedError` as the H1 stub does, so the harness's "H1 not
  written yet" paths stay tested once shesha writes `route_fields` (M6 PR 4 review, zara-safety:
  tests on the real stub would turn red the day H1 lands).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Final, NoReturn

from tests.extraction.standins import accept_everything
from vectapilot_api.evalrun.intervals import wilson_interval
from vectapilot_api.extracteval.metrics import (
    Accuracy,
    DocumentCount,
    ExtractMetrics,
    FieldCount,
    ReviewPrecision,
    ScoredField,
    SilentErrors,
)
from vectapilot_api.extraction.routing import DocumentSignals, ExtractionDecision, ReviewPolicy

STAND_IN_ROUTE: Final = accept_everything


def unwritten_h1(doc: DocumentSignals, policy: ReviewPolicy) -> ExtractionDecision:
    """H1 as the stub is (module docstring)."""
    raise NotImplementedError("TEST-ONLY: H1 unwritten")


CANNED_ACCURACY: Final = Accuracy(correct=1, n=2, wrong=("canned/wrong",), not_scored=())
CANNED_FIELDS: Final = FieldCount(k=1, n=2, fields=("canned/review",))
CANNED_DOCUMENTS: Final = DocumentCount(
    x=1, n=2, documents=("canned",), interval=wilson_interval(1, 2)
)
CANNED_SILENT: Final = SilentErrors(
    fields=FieldCount(k=1, n=2, fields=("canned/silent",)), documents=CANNED_DOCUMENTS
)
CANNED_PRECISION: Final = ReviewPrecision(
    wrong=0, n=1, fields=(), without_value=1, by_reason=(("not_found", 1),), null_gold=0
)


def canned_metrics() -> ExtractMetrics:
    """Fixed results whatever the input (module docstring)."""
    return ExtractMetrics(
        field_accuracy=lambda fields: CANNED_ACCURACY,
        silent_errors=lambda fields: CANNED_SILENT,
        review_rate=lambda fields: CANNED_FIELDS,
        review_precision=lambda fields: CANNED_PRECISION,
        straight_through=lambda fields: CANNED_DOCUMENTS,
    )


NO_SILENT: Final = SilentErrors(
    fields=FieldCount(k=0, n=0, fields=()), documents=DocumentCount(0, 0, (), None)
)


def _everything_in_review(fields: Sequence[ScoredField]) -> FieldCount:
    keys = tuple(sorted({item.key for item in fields if item.scored}))
    return FieldCount(k=len(keys), n=len(keys), fields=keys)


def consistent_metrics() -> ExtractMetrics:
    """`canned_metrics` for which the summary's identity holds (module docstring)."""
    return replace(
        canned_metrics(),
        silent_errors=lambda fields: NO_SILENT,
        review_rate=_everything_in_review,
    )


@dataclass
class RecordingMetrics:
    """Canned results, and every call as `(function, sorted field keys)` (module docstring)."""

    calls: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    seen: list[tuple[str, tuple[ScoredField, ...]]] = field(default_factory=list)
    """Every call with the fields themselves, in the order given."""

    def _note(self, name: str, fields: Sequence[ScoredField]) -> None:
        self.calls.append((name, tuple(sorted(item.key for item in fields))))
        self.seen.append((name, tuple(fields)))

    def bundle(self) -> ExtractMetrics:
        def accuracy(fields: Sequence[ScoredField]) -> Accuracy:
            self._note("field_accuracy", fields)
            return CANNED_ACCURACY

        def silent(fields: Sequence[ScoredField]) -> SilentErrors:
            self._note("silent_errors", fields)
            return CANNED_SILENT

        def rate(fields: Sequence[ScoredField]) -> FieldCount:
            self._note("review_rate", fields)
            return CANNED_FIELDS

        def precision(fields: Sequence[ScoredField]) -> ReviewPrecision:
            self._note("review_precision", fields)
            return CANNED_PRECISION

        def straight(fields: Sequence[ScoredField]) -> DocumentCount:
            self._note("straight_through", fields)
            return CANNED_DOCUMENTS

        return ExtractMetrics(accuracy, silent, rate, precision, straight)

    def given(self, name: str) -> list[tuple[str, ...]]:
        return [keys for called, keys in self.calls if called == name]

    def fields_given(self, name: str) -> list[tuple[ScoredField, ...]]:
        return [fields for called, fields in self.seen if called == name]


def _unwritten(fields: object) -> NoReturn:
    raise NotImplementedError("TEST-ONLY: H2 unwritten")


UNWRITTEN_H2: Final = ExtractMetrics(
    field_accuracy=_unwritten,
    silent_errors=_unwritten,
    review_rate=_unwritten,
    review_precision=_unwritten,
    straight_through=_unwritten,
)
"""H2 as the stubs are (module docstring): every function raises `NotImplementedError`."""
