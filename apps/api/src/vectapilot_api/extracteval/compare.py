"""The eval's own comparison of a value with its gold (M6 PR 4; plan, "Scoring rules", "Correct
value").

Gold is canonical (ISO dates, enum ids, identifiers and text as printed after their label, or null
when the document does not give the field). The extraction core returns a canonical value where
its checks allow it (`extraction.signals`, "The stored value and span"); this module decides, on
its own terms, whether that value is the gold:
- **date, enum, identifier:** exact, after trimming surrounding whitespace on both sides;
- **text:** whitespace collapsed (runs of whitespace become one space, ends trimmed),
  case-folded, and every character that is not a letter or digit removed **at either end**, on
  both sides (`trim`): E2's own text check takes "the quote minus its matched label and
  punctuation" (`signals.trim`), so `Larkfield Mutual Ltd` and `Larkfield Mutual Ltd.` are the
  same value, while inner punctuation still counts (`Dr. Tomas Brell` is not `Dr Tomas Brell`).
  Amended 2026-11-15 in M6 PR 4's review (ananya-rag), before any document existed;
- **null:** both null is correct; one null and one value is not.

**Independent of the code it scores.** This module imports nothing from `vectapilot_api` (the
field's type arrives as a plain string), so a bug in the core's canonical forms cannot also make
the eval agree with it (tests/extracteval/test_compare.py checks the imports, directly and in a
fresh interpreter). No NFKC, no dash or apostrophe folding: a value the core rendered differently
from the gold counts as wrong, which is what a reviewer would see.
"""

from typing import Final, Literal

FieldKind = Literal["text", "date", "enum", "identifier"]
FIELD_KINDS: Final[tuple[FieldKind, ...]] = ("text", "date", "enum", "identifier")
EXACT_KINDS: Final[frozenset[str]] = frozenset({"date", "enum", "identifier"})


def trim(text: str) -> str:
    """`text` without the characters that are not letters or digits at either end (written here
    again, never imported: see the module docstring)."""
    start, end = 0, len(text)
    while start < end and not text[start].isalnum():
        start += 1
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return text[start:end]


def collapse(text: str) -> str:
    """Whitespace collapsed, case-folded and trimmed at both ends (the text rule)."""
    return trim(" ".join(text.split()).casefold())


def same_value(kind: FieldKind, gold: str | None, value: str | None) -> bool:
    """Whether `value` is `gold` for a field of `kind` (module docstring). `ValueError` for a kind
    outside `FIELD_KINDS` (a caller bug, never a silent "wrong")."""
    if kind not in FIELD_KINDS:
        raise ValueError("kind is one of text, date, enum, identifier")
    if gold is None or value is None:
        return gold is None and value is None
    if kind in EXACT_KINDS:
        return gold.strip() == value.strip()
    return collapse(gold) == collapse(value)
