"""H2 (M4) — the router's metric functions. [HAND-WRITE]: written by Claude at shesha's request
(D4 (b)), on its own branch `m4/metrics`, reviewed by shesha; the explain-back is shesha's.

Guide: docs/learning/m4-metrics.md · Tests: tests/intenteval/test_metrics.py (strict xfails
until the bodies are written; its hand-computed fixtures are the contract).

Built by the team and tested (tests/intenteval/test_metrics_types.py): the result types
`ConfusionMatrix`, `ClassScores`, `MacroF1` and `ReliabilityBin`, and the `IntentMetrics`
bundle the harness calls them through (`H2_INTENT_METRICS`; the harness's own tests pass
stand-ins). The four function bodies are H2's.

The scoring rules they follow (docs/plans/m4-pr-checks.md, "Scoring rules"):
- **Counts are exact `Fraction`s; probabilities stay floats.** Precision, recall, F1 and
  accuracy are ratios of counts, so they are exact (1/3 stays 1/3); a confidence is a float, and
  so are a bin's mean confidence and the ECE.
- **A fail-closed row** (the LLM raised a `GatewayError`: a hand-off with no intent) is decided
  `None`. It has its own column, `fail_closed`, never a row of its own (no gold label is "fail
  closed"), and it is wrong for every intent metric: a false negative of its gold intent, and a
  false positive of none.
- **Macro-F1 over the fixed eight intents**, always eight, in `DECISION_ORDER`, with the guide's
  0/0 rule. Whether a slice has enough rows to show it is the report's decision, not H2's.
- **Calibration:** `RELIABILITY_BINS` (5) equal-width bins of the top probability over [0, 1],
  placed exactly (the guide, "Bins").
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

RELIABILITY_BINS: Final = 5
"""The number of equal-width bins of the reliability table (plan, "Calibration")."""

Decided = Intent | None
"""What a mode decided for one row: an intent, or None for a fail-closed hand-off."""

COLUMNS: Final[tuple[Decided, ...]] = (*DECISION_ORDER, None)
"""The confusion matrix's columns: the eight intents in decision order, then `fail_closed`."""

FAIL_CLOSED_COLUMN: Final = "fail_closed"
"""How the `None` column is printed."""


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_unit_fraction(value: object) -> bool:
    return isinstance(value, Fraction) and 0 <= value <= 1


@dataclass(frozen=True)
class ConfusionMatrix:
    """`counts[gold][decided]`: how many rows of gold intent `gold` were decided `decided`.

    Rows: every intent, in `DECISION_ORDER`. Columns: `COLUMNS` (every intent, then None for
    `fail_closed`). Every cell is present (zeros included) and a non-negative int. Read-only."""

    counts: Mapping[Intent, Mapping[Decided, int]]

    def __post_init__(self) -> None:
        if tuple(self.counts) != DECISION_ORDER:
            raise ValueError("a confusion matrix has one row per intent, in decision order")
        frozen: dict[Intent, Mapping[Decided, int]] = {}
        for gold, row in self.counts.items():
            if tuple(row) != COLUMNS:
                raise ValueError(
                    "every row has one column per intent in decision order, then fail_closed"
                )
            if not all(_is_count(n) for n in row.values()):
                raise ValueError("every cell is a count: an int >= 0")
            frozen[gold] = MappingProxyType(dict(row))
        object.__setattr__(self, "counts", MappingProxyType(frozen))

    def cell(self, gold: Intent, decided: Decided) -> int:
        return self.counts[gold][decided]

    def gold_total(self, gold: Intent) -> int:
        """Rows whose gold intent is `gold` (its support)."""
        return sum(self.counts[gold].values())

    def decided_total(self, decided: Decided) -> int:
        """Rows decided `decided` (None: fail-closed)."""
        return sum(row[decided] for row in self.counts.values())

    @property
    def total(self) -> int:
        return sum(self.gold_total(gold) for gold in DECISION_ORDER)


@dataclass(frozen=True)
class ClassScores:
    """One intent's counts and exact scores (the guide, "Per-intent scores")."""

    intent: Intent
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: Fraction
    recall: Fraction
    f1: Fraction

    def __post_init__(self) -> None:
        counts = (self.true_positives, self.false_positives, self.false_negatives)
        if not all(_is_count(n) for n in counts):
            raise ValueError("true and false positives and false negatives are counts (ints >= 0)")
        if not all(_is_unit_fraction(v) for v in (self.precision, self.recall, self.f1)):
            raise ValueError("precision, recall and F1 are exact Fractions in [0, 1]")

    @property
    def support(self) -> int:
        """Rows whose gold intent is this one."""
        return self.true_positives + self.false_negatives


@dataclass(frozen=True)
class MacroF1:
    """Every intent's scores, in `DECISION_ORDER`, and their macro average."""

    per_intent: tuple[ClassScores, ...]
    macro_f1: Fraction

    def __post_init__(self) -> None:
        if tuple(s.intent for s in self.per_intent) != DECISION_ORDER:
            raise ValueError("macro-F1 has the scores of every intent, in decision order")
        if not _is_unit_fraction(self.macro_f1):
            raise ValueError("macro-F1 is an exact Fraction in [0, 1]")


@dataclass(frozen=True)
class ReliabilityBin:
    """One bin of the reliability table: the rows whose top probability falls in it.

    `low` and `high` are the bin's exact edges; `n` its rows, `correct` how many of them the
    classifier got right; `mean_confidence` (a float) and `accuracy` (`correct / n`, exact) are
    None for an empty bin, never 0."""

    low: Fraction
    high: Fraction
    n: int
    correct: int
    mean_confidence: float | None
    accuracy: Fraction | None

    def __post_init__(self) -> None:
        if not (isinstance(self.low, Fraction) and isinstance(self.high, Fraction)):
            raise ValueError("a bin's edges are exact Fractions")
        if not 0 <= self.low < self.high <= 1:
            raise ValueError("a bin's edges satisfy 0 <= low < high <= 1")
        if not (_is_count(self.n) and _is_count(self.correct) and self.correct <= self.n):
            raise ValueError("n and correct are counts, with correct <= n")
        if self.n == 0:
            if self.mean_confidence is not None or self.accuracy is not None:
                raise ValueError("an empty bin has no mean confidence and no accuracy (None)")
            return
        mean = self.mean_confidence
        if not (isinstance(mean, float) and math.isfinite(mean) and 0 <= mean <= 1):
            raise ValueError("a bin's mean confidence is a float in [0, 1]")
        if self.accuracy != Fraction(self.correct, self.n):
            raise ValueError("a bin's accuracy is exactly correct / n")


# ── H2: the four functions (shesha's [HAND-WRITE], written by Claude at shesha's request) ──────


def confusion_matrix(gold: Sequence[Intent], decided: Sequence[Decided]) -> ConfusionMatrix:
    """Count every row: row `gold[i]`, column `decided[i]` (None is the `fail_closed` column).

    Contract (guide: docs/learning/m4-metrics.md, "The confusion matrix"):
    - `gold` and `decided` have one entry per row, in the same order; a length mismatch raises
      ValueError. No rows is a valid, all-zero matrix.
    - Every gold entry is an `Intent`; every decided entry an `Intent` or None. Anything else
      raises ValueError, a plain string included even when it names an intent (`isinstance(x,
      Intent)` decides): the harness parses results into `Intent`s first.
    - Every cell is present, zeros included (`ConfusionMatrix` checks the shape).
    """
    raise NotImplementedError(
        "H2 (M4): write confusion_matrix (guide: docs/learning/m4-metrics.md)"
    )


def macro_f1(matrix: ConfusionMatrix) -> MacroF1:
    """Every intent's precision, recall and F1, exactly, and their mean over the eight intents.

    Contract (guide, "Per-intent scores" and "The 0/0 rule"), for each intent c:
    - TP = cell(c, c); FP = rows decided c whose gold is another intent; FN = rows of gold c
      decided anything else, the `fail_closed` column included.
    - precision = TP / (TP + FP); recall = TP / (TP + FN); F1 = 2·TP / (2·TP + FP + FN).
    - **The 0/0 rule:** any of the three whose denominator is 0 is `Fraction(0)`, never an error
      and never left out: macro-F1 is the plain mean of all eight F1 values, always eight.
    - Every value is an exact `Fraction` (never a float).
    """
    raise NotImplementedError("H2 (M4): write macro_f1 (guide: docs/learning/m4-metrics.md)")


def reliability_bins(
    confidences: Sequence[float], correct: Sequence[bool], *, bins: int = RELIABILITY_BINS
) -> tuple[ReliabilityBin, ...]:
    """The reliability table: `bins` equal-width bins of [0, 1] by confidence.

    Contract (guide, "Bins"):
    - Bin i (0-based) has the exact edges i/bins and (i+1)/bins. A confidence p goes to bin
      `min(floor(Fraction(p) * bins), bins - 1)`: computed exactly from the float's own value
      (no float multiplication), and 1.0 joins the last bin.
    - Each bin: n, correct, the mean confidence (`math.fsum` of its confidences / n, a float) and
      the accuracy `Fraction(correct, n)`; both None when n is 0. Always `bins` bins, in order.
    - ValueError for a length mismatch, a confidence that is not a finite number in [0, 1], a
      `correct` entry that is not a bool, or `bins` that is not an int >= 1.
    """
    raise NotImplementedError(
        "H2 (M4): write reliability_bins (guide: docs/learning/m4-metrics.md)"
    )


def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> float:
    """ECE: the mean gap between confidence and accuracy, weighted by each bin's share of rows.

    Contract (guide, "ECE"): with N the rows of all bins, ECE = Σ (n_b / N) · |accuracy_b -
    mean_confidence_b| over the non-empty bins, as a float in [0, 1]. ValueError when N is 0 (no
    rows, no ECE). Biased upward at small n (the guide says why); the report prints n with it.
    """
    raise NotImplementedError(
        "H2 (M4): write expected_calibration_error (guide: docs/learning/m4-metrics.md)"
    )


# ── How the harness calls H2 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntentMetrics:
    """The four H2 functions the harness calls (signatures as above)."""

    confusion_matrix: Callable[[Sequence[Intent], Sequence[Decided]], ConfusionMatrix]
    macro_f1: Callable[[ConfusionMatrix], MacroF1]
    reliability_bins: Callable[[Sequence[float], Sequence[bool]], tuple[ReliabilityBin, ...]]
    expected_calibration_error: Callable[[Sequence[ReliabilityBin]], float]


H2_INTENT_METRICS: Final = IntentMetrics(
    confusion_matrix=confusion_matrix,
    macro_f1=macro_f1,
    reliability_bins=reliability_bins,
    expected_calibration_error=expected_calibration_error,
)
"""H2 as written. Until then each function raises `NotImplementedError`: the harness never
catches it as a result, and a summary is then not written ("H2 not written yet")."""
