"""H2 — PII masking (`[HAND-WRITE]`, D5 (b): written by Claude at shesha's request, on its own
branch `m7/pii-masking`; guide `docs/learning/m7-masking.md`).

Masking runs before every third party and every log (G6, G10): the customer message, the
free-text string fields of tool results, and later the question on the answer path and each
formatted log line (`RedactingJsonFormatter`, after H2). It replaces each personal-data span
with a typed placeholder (`[PHONE]`, `[AADHAAR]`, ...) that cannot be turned back into the value:
no index, no hash, no vault. `Masked` carries the masked text and counts by type, never a value.

Around H2, as team code: the patterns per locale and the ASCII view (`locales`), Verhoeff and
Luhn over Unicode digits (`checksums`), the input cap (`locales.MASK_CAP`, G3) and this file's
types and bounds. H2's body and its private helpers below were written by Claude at shesha's
request on 2026-11-20 (D5 (b)); the explain-back is shesha's. With it written, the log formatter
masks free text (`redact.log_masker`) and the red-team suite's category E runs as hard cases.

Rules 2, 3 and 6 of the contract follow ananya's PR 3 review (decided 2026-11-18, shesha):
candidates at every start position that join when they overlap (so two numbers one space apart, or
a card pasted with its expiry and CVV, leave no digit visible), and passes repeated until one finds
nothing (so a code glued to a number is masked too).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, NamedTuple

from vectapilot_api.guards.caps import OverCap
from vectapilot_api.guards.checksums import checksum_valid, decimal_digits
from vectapilot_api.guards.locales import MASK_CAP as MASK_CAP
from vectapilot_api.guards.locales import Locale, PiiKind, PiiPattern, ascii_view
from vectapilot_api.guards.types import Clean

PLACEHOLDERS: Final[Mapping[PiiKind, str]] = MappingProxyType(
    {kind: f"[{kind.value}]" for kind in PiiKind}
)
"""G6's typed, non-reversible placeholders: `[PHONE]`, `[EMAIL]`, `[AADHAAR]`, `[PAN]`, `[CARD]`,
`[UPI]`, `[IFSC]`, `[PASSPORT]` and `[ID_NUMBER]`."""


MAX_PASSES: Final = 3
"""Passes that may replace spans (rule 6). A placeholder holds no match, so a later pass can only
find a span whose one-character lookaround a replacement freed (`pan ABCZZ9999Z9999999999`
settles in two). Such frees can chain, with the registered patterns too: a `+` phone right after
a digit is refused until that digit is inside a placeholder, so `+` phones glued one after
another need one pass each. The bound caps the work for any text and any patterns (G3); a text
that still holds a span after it raises `MaskingIncomplete` (fail closed)."""

SQUEEZED_OUT: Final = frozenset({"\u200c", "\u200d", "\ufe0e", "\ufe0f", "\u20e3"})
"""What `clean` keeps that can sit inside a number without splitting it for a reader: ZWNJ and
ZWJ (Indic conjuncts, emoji sequences), VS15 and VS16 (text and emoji presentation) and the
keycap mark U+20E3 (`9`, VS16, U+20E3 is the keycap digit nine). Rule 2's second view leaves them
out (the PR 3 open item, handled in H2's PR)."""


class MaskingIncomplete(RuntimeError):
    """Masking still found a span after `MAX_PASSES` passes (rule 6): fail closed (G3).

    Raised with no arguments, so its message is fixed and can never hold any of the text."""

    def __init__(self) -> None:
        super().__init__(f"masking still found a span after {MAX_PASSES} passes")


def _counts() -> Mapping[PiiKind, int]:
    return MappingProxyType({})


@dataclass(frozen=True)
class Masked:
    """`mask_pii`'s result: the masked text and how many spans of each kind it replaced.

    Counts only, never a value or a position. A count must be backed by its placeholder in the
    text (a customer may type a placeholder too, so the text can hold more, never fewer). The
    repr shows the counts only: if masking ever missed a span, a traceback or a log of this
    object still could not print the text.
    """

    text: Clean
    counts: Mapping[PiiKind, int] = field(default_factory=_counts)

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        if not all(isinstance(kind, PiiKind) for kind in self.counts):
            raise ValueError("counts are keyed by PiiKind")
        if any(n < 1 or self.text.count(PLACEHOLDERS[k]) < n for k, n in self.counts.items()):
            raise ValueError("each count is positive and backed by its placeholder in the text")

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __repr__(self) -> str:
        counts = ", ".join(f"{kind.value}={n}" for kind, n in sorted(self.counts.items()))
        return f"Masked(counts={{{counts}}})"


def mask_pii(text: Clean, locales: Sequence[Locale]) -> Masked:
    """`text` with every personal-data span of `locales`' patterns replaced by its placeholder.
    Pure: no I/O, no logging, no clock, nothing kept between calls.

    The contract (`docs/plans/m7-pr-checks.md`, "H2"; G3, G6):
    1. `locales` empty is a `ValueError`; `text` longer than `MASK_CAP` is a `caps.OverCap`
       (G3: fail closed, never truncate). Neither message holds any of the text. The cap is
       checked once, on `text`: a later pass's text may be longer (a placeholder can be longer
       than its span).
    2. Candidates: every pattern of every locale, in the given order, runs over
       `locales.ascii_view(text)` (Unicode digits, fullwidth forms and dashes as ASCII, one
       character for one), so a span there is the same span in `text`. A candidate is the
       pattern's match at **every** start position (`PiiPattern.anywhere.finditer`, group 1),
       not only `regex.finditer`'s non-overlapping ones. A span includes its separators.
       When the view holds a character of `SQUEEZED_OUT`, every pattern also runs over the view
       without them (the squeezed view), and each candidate there maps back through an offset
       map: from its first character to just before the next character the squeezed view kept
       (or the text's end), so the characters squeezed out inside it and right after it (a
       keycap's marks) are part of its span. Both views' candidates go to rule 3 together, so
       the second view only adds spans (G2: it only tightens).
    3. Overlaps: candidates that share a character join into one span, and so on through every
       candidate they join (touching is not sharing: `[0, 2)` and `[2, 4)` stay two). Each
       joined span takes the label of its best candidate: the longest, then the leftmost, then
       the earlier pattern (locale order, then pattern order).
    4. Labels: shape decides that a span is masked; a pattern's checksum only picks its label:
       the best candidate's own decimal digits (`checksums.decimal_digits`) pass the check ->
       the pattern's kind; fail -> `ID_NUMBER`. A pattern without a checksum gives its kind.
    5. Each joined span is replaced by `PLACEHOLDERS[label]`; every other character is kept
       exactly. `Masked.counts` holds one count per label used, and nothing else.
    6. Rules 2-5 are one pass. Passes repeat on the result until one finds nothing, and the
       counts add up over the passes; so masking the result again changes nothing and counts
       nothing. If `MAX_PASSES` passes have each replaced something and the next one would
       still find a span, raise `MaskingIncomplete` (G3: fail closed).
    """
    if not locales:
        raise ValueError("masking needs at least one locale")
    if len(text) > MASK_CAP:
        raise OverCap(f"the text to mask is longer than its cap of {MASK_CAP}")
    patterns = tuple(pattern for locale in locales for pattern in locale.patterns)
    counts: dict[PiiKind, int] = {}
    current: str = text
    for _ in range(MAX_PASSES):
        spans = _joined_spans(current, patterns)
        if not spans:
            return Masked(Clean(current), counts)
        current = _replaced(current, spans, patterns, counts)
    if _joined_spans(current, patterns):
        raise MaskingIncomplete()
    return Masked(Clean(current), counts)


class _Span(NamedTuple):
    """One joined span (rule 3) and its best candidate: offsets in the pass's text, never text."""

    start: int
    end: int
    best_start: int
    best_end: int
    order: int
    """The best candidate's pattern, as its index in the locales' patterns, flattened in order."""


def _squeezed(view: str) -> tuple[str, list[int]] | None:
    """The view without `SQUEEZED_OUT`, and the view's offset of each of its characters plus one
    last entry, the view's length (so a span's end maps to the next kept character); None when
    the view holds none of them (rule 2)."""
    kept = [i for i, character in enumerate(view) if character not in SQUEEZED_OUT]
    if len(kept) == len(view):
        return None
    return "".join(view[i] for i in kept), [*kept, len(view)]


def _longest_per_start(text: str, patterns: tuple[PiiPattern, ...]) -> list[tuple[int, int]]:
    """Rule 2: for each start offset that has a candidate, the end and the pattern of the longest
    one starting there, the earlier pattern on a tie; `(-1, 0)` elsewhere.

    Keeping only that one per start changes nothing in rule 3: every other candidate starting
    there lies inside it, so it joins the same span, and it is never better (shorter, or the same
    span from a later pattern). This keeps the work after the regexes linear: no sort."""
    view = ascii_view(Clean(text))
    views: list[tuple[str, list[int] | None]] = [(view, None)]
    squeezed = _squeezed(view)
    if squeezed is not None:
        views.append(squeezed)
    best = [(-1, 0)] * len(view)
    for order, pattern in enumerate(patterns):  # pattern by pattern: a tie keeps the earlier
        for searched, offsets in views:
            for match in pattern.anywhere.finditer(searched):
                start, end = match.span(1)
                if offsets is not None:
                    start, end = offsets[start], offsets[end]
                if end > best[start][0]:
                    best[start] = (end, order)
    return best


def _joined_spans(text: str, patterns: tuple[PiiPattern, ...]) -> list[_Span]:
    """Rule 3 in one sweep from left to right: a candidate that starts before the current span's
    end shares a character with it and joins it; one that starts at or after it (touching is not
    sharing) opens the next span. The best candidate is the longest; a later one of the same
    length is further right, so the leftmost stays; the earlier pattern was kept per start."""
    spans: list[_Span] = []
    for start, (end, order) in enumerate(_longest_per_start(text, patterns)):
        if end < 0:
            continue
        if spans and start < spans[-1].end:
            last = spans[-1]
            if end - start > last.best_end - last.best_start:
                last = last._replace(best_start=start, best_end=end, order=order)
            spans[-1] = last._replace(end=max(last.end, end))
        else:
            spans.append(_Span(start, end, start, end, order))
    return spans


def _label(text: str, pattern: PiiPattern, span: _Span) -> PiiKind:
    """Rule 4: the best candidate's own decimal digits pick its pattern's kind or `ID_NUMBER`."""
    if pattern.checksum is None:
        return pattern.kind
    digits = decimal_digits(text[span.best_start : span.best_end])
    return pattern.kind if checksum_valid(pattern.checksum, digits) else PiiKind.ID_NUMBER


def _replaced(
    text: str, spans: list[_Span], patterns: tuple[PiiPattern, ...], counts: dict[PiiKind, int]
) -> str:
    """Rule 5: each span's placeholder in its place, every other character copied as it is; the
    labels are added to `counts`."""
    parts: list[str] = []
    copied = 0
    for span in spans:
        label = _label(text, patterns[span.order], span)
        parts += (text[copied : span.start], PLACEHOLDERS[label])
        counts[label] = counts.get(label, 0) + 1
        copied = span.end
    parts.append(text[copied:])
    return "".join(parts)
