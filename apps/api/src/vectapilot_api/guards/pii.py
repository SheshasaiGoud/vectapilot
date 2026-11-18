"""H2 — PII masking (`[HAND-WRITE]`, D5 (b): written by Claude at shesha's request, on its own
branch `m7/pii-masking`; guide `docs/learning/m7-masking.md`).

Masking runs before every third party and every log (G6, G10): the customer message, the
free-text string fields of tool results, and later the question on the answer path and each
formatted log line (`RedactingJsonFormatter`, after H2). It replaces each personal-data span
with a typed placeholder (`[PHONE]`, `[AADHAAR]`, ...) that cannot be turned back into the value:
no index, no hash, no vault. `Masked` carries the masked text and counts by type, never a value.

Around H2, as team code: the patterns per locale and the ASCII view (`locales`), Verhoeff and
Luhn over Unicode digits (`checksums`), the input cap (`locales.MASK_CAP`, G3) and this file's
types and bounds. While `mask_pii` is the stub, every guarded route that masks hands off (G3:
fail closed), and the red-team suite's category E is `pending:H2`.

Rules 2, 3 and 6 of the contract follow ananya's PR 3 review (decided 2026-11-18, shesha):
candidates at every start position that join when they overlap (so two numbers one space apart, or
a card pasted with its expiry and CVV, leave no digit visible), and passes repeated until one finds
nothing (so a code glued to a number is masked too).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vectapilot_api.guards.locales import MASK_CAP as MASK_CAP
from vectapilot_api.guards.locales import Locale, PiiKind
from vectapilot_api.guards.types import Clean

PLACEHOLDERS: Final[Mapping[PiiKind, str]] = MappingProxyType(
    {kind: f"[{kind.value}]" for kind in PiiKind}
)
"""G6's typed, non-reversible placeholders: `[PHONE]`, `[EMAIL]`, `[AADHAAR]`, `[PAN]`, `[CARD]`,
`[UPI]`, `[IFSC]`, `[PASSPORT]` and `[ID_NUMBER]`."""


MAX_PASSES: Final = 3
"""Passes that may replace spans (rule 6). Masking a text with the registered patterns settles in
two: a placeholder holds no match, so a later pass can only find a span whose one-character
lookaround a replacement freed (`pan ABCZZ9999Z9999999999`). The bound keeps the work linear
for any patterns (G3)."""


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
    # TODO(H2): implement the contract above. Its tests are tests/guards/test_mask_pii.py,
    # behind one switch (its `pytestmark`): when every case shows XPASS(strict), delete it.
    raise NotImplementedError("H2: mask_pii is written on m7/pii-masking (m7-masking.md)")
