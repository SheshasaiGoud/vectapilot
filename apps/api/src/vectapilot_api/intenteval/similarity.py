"""The near-duplicate screen (M4 PR 1, ADR 0015 decision 8): character 3-gram Jaccard on
normalised text, at a threshold fixed in the manifest before any row exists.

**Why a screen at all.** Training rows are drafted by Claude and eval rows by people, in the blind
order, so a train-eval collision is a coincidence, but one that leaks: a classifier that saw a
near copy of a test message in training measures memory, not routing. Two near-identical eval rows
in different splits leak the same way between dev (tuned on) and test. So `check` refuses a
train-eval pair at or above the threshold, and requires an eval-eval pair at or above it to share
a `split_group` (which keeps them in one split).

**Normalisation.** Unicode NFKC, case-folded; every character that is neither a letter or digit
nor a combining mark becomes a space (punctuation and symbols do not make two messages differ;
Devanagari vowel signs are combining marks, so Hindi keeps them); whitespace runs collapse to one
space, trimmed. **3-grams** are the overlapping 3-character windows of that string, spaces
included, as a set. A string shorter than 3 characters is its own single gram (empty: no gram).

**Jaccard** is the size of the two gram sets' intersection over the size of their union, as an
exact `Fraction`; two texts with the same normalised string score 1 (two empty sets included). A
pair is a near-duplicate when `100 * |A & B| >= threshold_percent * |A | B|`: integer arithmetic,
so no float decides a boundary case.

**Why 3-grams and 70 % (fixed 2026-11-05, before any row).** Character grams need no tokenizer
and work the same for English, Hindi and Hinglish. On placeholder strings of Greek letter names
(tests/intenteval/test_similarity.py pins them), six words with a one-character typo score 31/32,
with a word appended 31/35, with two pairs of neighbours swapped 27/35, and with one word in six
replaced 26/37 (just above 70 %), while nine words sharing only their first six score 31/55. So
70 % catches a lightly edited copy and leaves a text that merely shares a template alone. These
are properties of the measure on made-up strings, not a measurement of any row. It is a screen,
not proof of independence: a
translation or a paraphrase passes it, which is why translations need a `split_group` by rule
(README).
"""

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

GRAM: Final = 3


def normalize(text: str) -> str:
    """`text` as the screen compares it (the module docstring)."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(
        c if c.isalnum() or unicodedata.category(c).startswith("M") else " " for c in folded
    )
    return " ".join(kept.split())


def trigrams(text: str) -> frozenset[str]:
    """The set of 3-character windows of `normalize(text)`."""
    normalized = normalize(text)
    if len(normalized) < GRAM:
        return frozenset({normalized} if normalized else ())
    return frozenset(normalized[i : i + GRAM] for i in range(len(normalized) - GRAM + 1))


def _jaccard(first: frozenset[str], second: frozenset[str]) -> Fraction:
    union = len(first | second)
    return Fraction(1) if union == 0 else Fraction(len(first & second), union)


def jaccard(first: str, second: str) -> Fraction:
    """The 3-gram Jaccard similarity of two texts, exact; 1 for the same normalised text."""
    if normalize(first) == normalize(second):
        return Fraction(1)
    return _jaccard(trigrams(first), trigrams(second))


def at_or_above(similarity: Fraction, threshold_percent: int) -> bool:
    return similarity * 100 >= threshold_percent


@dataclass(frozen=True)
class NearPair:
    """Two rows whose texts are near-duplicates, by id (never by text)."""

    first: str
    second: str
    similarity: Fraction

    @property
    def percent(self) -> int:
        """The similarity in whole percent, rounded down (for messages only)."""
        return int(self.similarity * 100)


@dataclass(frozen=True)
class _Prepared:
    id: str
    normalized: str
    grams: frozenset[str]


def _prepare(rows: Sequence[tuple[str, str]]) -> list[_Prepared]:
    prepared = []
    for row_id, text in rows:
        normalized = normalize(text)
        prepared.append(_Prepared(row_id, normalized, trigrams(text)))
    return prepared


def _similarity(first: _Prepared, second: _Prepared) -> Fraction:
    if first.normalized == second.normalized:
        return Fraction(1)
    return _jaccard(first.grams, second.grams)


def near_pairs_between(
    left: Sequence[tuple[str, str]], right: Sequence[tuple[str, str]], threshold_percent: int
) -> list[NearPair]:
    """Every `(left id, right id)` pair at or above the threshold, in input order. Rows are
    `(id, text)`."""
    rights = _prepare(right)
    pairs = []
    for first in _prepare(left):
        for second in rights:
            similarity = _similarity(first, second)
            if at_or_above(similarity, threshold_percent):
                pairs.append(NearPair(first.id, second.id, similarity))
    return pairs


def near_pairs_within(rows: Sequence[tuple[str, str]], threshold_percent: int) -> list[NearPair]:
    """Every pair of `rows` at or above the threshold, each pair once, in input order."""
    prepared = _prepare(rows)
    pairs = []
    for i, first in enumerate(prepared):
        for second in prepared[i + 1 :]:
            similarity = _similarity(first, second)
            if at_or_above(similarity, threshold_percent):
                pairs.append(NearPair(first.id, second.id, similarity))
    return pairs
