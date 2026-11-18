"""The locale registry for PII masking (G6, D1 (a)): which shapes are personal data, as data.

**A locale** (`in`, `generic`) is a named tuple of `PiiPattern`s. A pattern says which `PiiKind`
its span gets, which checksum (if any) picks between that kind and `ID_NUMBER`, and the shape
itself, written in a small grammar instead of free regex text:
- `Chars(chars, min, max)`: one character set (`[0-9]`, `[ -]`, `\\.`), repeated;
- `OneOf(options, optional)`: literal alternatives (`+91` or `0091`), matched as written;
- `Assert(look, chars)`: a one-character lookaround (never after a digit, never before one).
The compiled regex is derived (`PiiPattern.regex`); H2 (`pii.mask_pii`) runs it.

**G3's written rule for patterns** (`pattern_problems`, checked when a pattern is built, so a
bad pattern stops the import; `tests/guards/test_locales.py` re-checks every registered one):
1. *No nested quantifiers.* A quantifier applies to one character set (`Chars`) or makes a
   group of literals optional (`OneOf`); the grammar cannot write a quantifier inside another.
2. *No overlapping quantifiers.* After a variable-length item (a `Chars` whose `min < max`, an
   optional `OneOf`), no item that could follow it directly (up to and including the next
   mandatory one; lookarounds are skipped) may match a character it can match. So `[ -]?[0-9]`
   is allowed and `[0-9]{1,4}[0-9]` is not: at every point the regex has one way forward.
3. *Bounded.* Every maximum is at most `MAX_REPEAT`; a pattern of exactly one item may reach
   `MASK_CAP`, because with nothing after it a failed match never backtracks past its minimum.
4. A pattern never matches the empty string; a literal option is never a prefix of another; a
   checksum never labels a span `ID_NUMBER` when it holds (that is the label for a failed one).
Rule 2 compares character sets on `PROBE`, a fixed alphabet of the characters the platform's
texts hold (ASCII, Latin-1 letters, Devanagari, Telugu, general punctuation and fullwidth
forms): enough for the classes used here, and a new class with a character outside it gets a
character added there first. With 1-3, each start position costs at most a bounded number of
steps, so masking is linear in the text by construction; G3's adversarial-input test at the
cap shows it with a generous bound (a bound, not a measurement). `PiiPattern.anywhere` (the same
pattern inside a lookahead) finds a match at every start position, each one bounded the same way.

**The ASCII view** (`ascii_view`). Patterns are written over ASCII digits and letters. H2 runs
them on `ascii_view(text)`: every decimal digit of any script becomes its ASCII digit
(`unicodedata.digit`), every fullwidth ASCII form its ASCII character, and every dash (Unicode
category `Pd`: the non-breaking hyphen U+2011 that word processors put in phone numbers, en and
em dashes) and the minus sign U+2212 a hyphen, one character for one, so a span found in the
view is the same span in `text`, and H2 replaces it there. Devanagari, Telugu and fullwidth phone
numbers (G6) and dashed groups need no pattern of their own (ananya's PR 3 review: the red-team
suite's literal screen already counts en and em dashes as separators).

**Which locales a business uses** is open until Q4b names the other countries: every business
masks with `DEFAULT_LOCALE_IDS` (`in`, then `generic`) [proposal]. Masking more only tightens
(G2), and the order decides a tie between two patterns that find the same span (H2).

The `in` patterns: phones with `+91` or `0091` and any single space or hyphen between digits
(`+91-`, per-digit spacing); 10-digit mobiles starting 6-9 with any single space or hyphen between
digits (5-5, 3-3-4, 4-3-3, 4-6, 2-2-2-2-2, per digit); a trunk `0` and 10 digits the same way (a
mobile after its trunk, or an STD code and a landline number: `040 2345 6789`); Aadhaar as 12
digits grouped 4-4-4 or not grouped (Verhoeff picks the label); PAN; a GSTIN, whose characters 3-12
are its holder's PAN, labelled `PAN` (ananya's PR 3 review, decided 2026-11-18, shesha); IFSC;
passport (a letter and 7 digits); UPI handles. The `generic` patterns: e-mail addresses; `+` phones
of 8-15 digits with any single separator; every 13-19 digit run with any single separator (Luhn
picks the label); and any longer run of digits. Aadhaar and the 13-19 digit runs also take a full
stop between digits (`4111.1111.1111.1111`) (the same review, decided 2026-11-18, shesha). No
pattern requires a real issuer's range (the red-team generator's values sit in reserved ones: an
Aadhaar starting with 0 or 1, `+91 00000`): shape decides, and a mistyped identifier is still
masked. Ten digits starting with 0 to 5 alone (`RT-0987654321`, l-011) are no shape: an order id,
not a phone. A phone pattern refuses only a digit right before it, never a `+`: with the `+`
refused too, `++91 00000 30947` or `1+9999999999` matched no pattern at all (zara's PR 3 mutation
check, decided 2026-11-18, shesha). A UPI handle's name, like an e-mail's local part, has at least
one character (the same check, decided 2026-11-18, shesha).

Names, street addresses, bank account numbers and formats outside these shapes are not masked:
a documented gap (D1 (a); `docs/learning/m7-masking.md` §2.6).
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from types import MappingProxyType
from typing import Final

from vectapilot_api.guards.caps import INPUT_CAPS, Surface
from vectapilot_api.guards.checksums import Checksum
from vectapilot_api.guards.types import Clean


class PiiKind(StrEnum):
    """G6's types; each one's placeholder is its value in brackets (`pii.PLACEHOLDERS`)."""

    PHONE = "PHONE"
    EMAIL = "EMAIL"
    AADHAAR = "AADHAAR"
    PAN = "PAN"
    CARD = "CARD"
    UPI = "UPI"
    IFSC = "IFSC"
    PASSPORT = "PASSPORT"
    ID_NUMBER = "ID_NUMBER"
    """An identifier-shaped run whose checksum failed, or a run of 20 or more digits."""


NFC_EXPANSION: Final = 3
"""NFC makes a text at most three times longer (a Unicode stability guarantee, UAX #15), and the
rest of `normalize.clean` only removes or collapses characters."""
MASK_CAP: Final = NFC_EXPANSION * INPUT_CAPS[Surface.MESSAGE]
"""The longest text masking reads (G3): 6,000 characters, a valid message after `clean` (ananya's
PR 3 review: G1's 2,000 is counted on the raw text, and NFC splits a precomposed nukta letter
such as U+095E into two code points, so a valid Hindi message can grow past 2,000). Tool-result
fields are shorter; a log field is cut to `redact.FIELD_CAP` before masking (G10)."""
MAX_REPEAT: Final = 64
"""Rule 3's bound on a quantifier (module docstring): an e-mail's local part is at most 64."""


# ── The grammar ──────────────────────────────────────────────────────────────────────────────

_ONE_SET: Final = re.compile(r"\\[dwsDWS]|\\[^A-Za-z0-9]|\[(?:\\.|[^\]\\])+\]|[^\\\[\]().*+?{}|^$]")
"""One character set: `\\d`-style class, an escaped symbol, a bracketed class, or one literal."""


@dataclass(frozen=True)
class Chars:
    """One character set, repeated from `min` to `max` times."""

    chars: str
    min: int = 1
    max: int = 1

    @property
    def variable(self) -> bool:
        return self.min < self.max

    @property
    def regex(self) -> str:
        if self.min == self.max:
            return self.chars if self.min == 1 else f"{self.chars}{{{self.min}}}"
        if (self.min, self.max) == (0, 1):
            return f"{self.chars}?"
        return f"{self.chars}{{{self.min},{self.max}}}"


@dataclass(frozen=True)
class OneOf:
    """Literal alternatives, matched as written; `optional` makes the group optional."""

    options: tuple[str, ...]
    optional: bool = False

    @property
    def variable(self) -> bool:
        return self.optional

    @property
    def regex(self) -> str:
        group = "(?:" + "|".join(re.escape(option) for option in self.options) + ")"
        return group + "?" if self.optional else group


class Look(StrEnum):
    NOT_AFTER = "not_after"
    """The span never starts right after one of these characters: `(?<!C)`."""
    NOT_BEFORE = "not_before"
    """The span never ends right before one of these characters: `(?!C)`."""
    ENDS_WITH = "ends_with"
    """The span's last character is one of these: `(?<=C)`."""


_LOOKS: Final[Mapping[Look, str]] = MappingProxyType(
    {Look.NOT_AFTER: "(?<!{})", Look.NOT_BEFORE: "(?!{})", Look.ENDS_WITH: "(?<={})"}
)


@dataclass(frozen=True)
class Assert:
    """A one-character lookaround; it matches no text."""

    look: Look
    chars: str

    @property
    def regex(self) -> str:
        return _LOOKS[self.look].format(self.chars)


Item = Chars | OneOf | Assert


# ── G3's rule ────────────────────────────────────────────────────────────────────────────────


def _probe() -> frozenset[str]:
    ranges = (
        (0x20, 0x7E),  # ASCII printable
        (0xC0, 0xFF),  # Latin-1 letters
        (0x0900, 0x097F),  # Devanagari
        (0x0C00, 0x0C7F),  # Telugu
        (0x2010, 0x205E),  # general punctuation
        (0xFF01, 0xFF5E),  # fullwidth ASCII forms
    )
    return frozenset(chr(code) for start, end in ranges for code in range(start, end + 1)) | {"\n"}


PROBE: Final = _probe()
"""Rule 2's alphabet (module docstring)."""


@cache
def _matched(chars: str) -> frozenset[str]:
    """The characters of `PROBE` one character set matches (cached per set: a pure function)."""
    compiled = re.compile(chars)
    return frozenset(c for c in PROBE if compiled.fullmatch(c))


def _starts(item: Chars | OneOf) -> frozenset[str]:
    """The characters `item` can start with."""
    if isinstance(item, Chars):
        return _matched(item.chars)
    return frozenset(option[0] for option in item.options)


def _mandatory(item: Chars | OneOf) -> bool:
    """Whether `item` always consumes at least one character."""
    if isinstance(item, Chars):
        return item.min > 0
    return not item.optional


def _shape_problems(index: int, item: Item, only_item: bool) -> list[str]:
    if isinstance(item, OneOf):
        options = item.options
        if not options or not all(options):
            return [f"item {index} has an empty option"]
        prefixes = [
            a
            for i, a in enumerate(options)
            for j, b in enumerate(options)
            if i != j and b.startswith(a)
        ]
        return [f"item {index} has an option that is a prefix of another"] if prefixes else []
    problems: list[str] = []
    if not _ONE_SET.fullmatch(item.chars):
        problems.append(f"item {index} is not one character set")
    if isinstance(item, Chars):
        bound = MASK_CAP if only_item else MAX_REPEAT
        if not 0 <= item.min <= item.max or not 1 <= item.max <= bound:
            problems.append(f"item {index} repeats outside 0..{bound}")
    return problems


def _overlap_problems(items: tuple[Item, ...]) -> list[str]:
    """Rule 2: what may follow a variable-length item, up to the next mandatory one."""
    problems: list[str] = []
    for i, first in enumerate(items):
        if isinstance(first, Assert) or not first.variable:
            continue
        for j in range(i + 1, len(items)):
            after = items[j]
            if isinstance(after, Assert):
                continue
            if _starts(first) & _starts(after):
                problems.append(f"items {i} and {j} overlap")
            if _mandatory(after):
                break
    return problems


def _shortest(items: tuple[Item, ...]) -> int:
    total = 0
    for item in items:
        if isinstance(item, Chars):
            total += item.min
        elif isinstance(item, OneOf) and not item.optional and item.options:
            total += min(len(option) for option in item.options)
    return total


def pattern_problems(
    items: tuple[Item, ...], kind: PiiKind, checksum: Checksum | None
) -> tuple[str, ...]:
    """Every way a pattern breaks G3's rule (module docstring), as fixed texts with item
    indexes; empty when it follows it."""
    problems: list[str] = []
    if checksum is not None and kind is PiiKind.ID_NUMBER:
        problems.append("a checksum's valid label is not ID_NUMBER")
    for index, item in enumerate(items):
        problems.extend(_shape_problems(index, item, len(items) == 1))
    if problems:
        return tuple(problems)  # the sets themselves are unreliable: stop before comparing
    problems.extend(_overlap_problems(items))
    if _shortest(items) < 1:
        problems.append("the pattern can match the empty string")
    return tuple(problems)


# ── Patterns and locales ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PiiPattern:
    """One shape of personal data. Refused at construction when it breaks G3's rule."""

    id: str
    kind: PiiKind
    """The label of every span it finds, or, with a checksum, of every span whose check holds."""
    items: tuple[Item, ...]
    checksum: Checksum | None = None
    """When set, a span whose check fails is labelled `ID_NUMBER` instead (G6)."""
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)
    anywhere: re.Pattern[str] = field(init=False, repr=False, compare=False)
    """The same pattern inside a lookahead: its `finditer` gives a match at every start position
    (group 1 is the span), where `regex.finditer` skips a start inside an earlier match (H2)."""

    def __post_init__(self) -> None:
        problems = pattern_problems(self.items, self.kind, self.checksum)
        if problems:
            raise ValueError(f"pattern {self.id}: {'; '.join(problems)}")
        source = "".join(i.regex for i in self.items)
        object.__setattr__(self, "regex", re.compile(source))
        object.__setattr__(self, "anywhere", re.compile(f"(?=({source}))"))


@dataclass(frozen=True)
class Locale:
    id: str
    patterns: tuple[PiiPattern, ...]

    def __post_init__(self) -> None:
        ids = [pattern.id for pattern in self.patterns]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError(f"locale {self.id}: at least one pattern, each id once")


DIGIT: Final = "[0-9]"
SEPARATOR: Final = "[ -]"
GROUP_SEPARATOR: Final = "[ .-]"
"""Aadhaar's and the 13-19 digit runs' separator: a full stop too (module docstring)."""
LETTER: Final = "[A-Za-z]"
ALNUM: Final = "[A-Za-z0-9]"


def spaced_digits(count: int, separator: str = SEPARATOR) -> tuple[Item, ...]:
    """`count` digits with at most one separator (a space or hyphen by default) between any two."""
    items: list[Item] = [Chars(DIGIT)]
    for _ in range(count - 1):
        items += [Chars(separator, 0, 1), Chars(DIGIT)]
    return tuple(items)


def _digits(*groups: int, separator: str = SEPARATOR) -> tuple[Item, ...]:
    """Digit groups of the given sizes, each pair joined by at most one separator."""
    items: list[Item] = []
    for index, size in enumerate(groups):
        if index:
            items.append(Chars(separator, 0, 1))
        items.append(Chars(DIGIT, size, size))
    return tuple(items)


NOT_AFTER_DIGIT: Final = Assert(Look.NOT_AFTER, DIGIT)
NOT_BEFORE_DIGIT: Final = Assert(Look.NOT_BEFORE, DIGIT)


def _word(pattern_id: str, kind: PiiKind, *items: Item) -> PiiPattern:
    """A letters-and-digits code that is a whole word (no ASCII letter or digit either side)."""
    return PiiPattern(
        pattern_id,
        kind,
        (Assert(Look.NOT_AFTER, ALNUM), *items, Assert(Look.NOT_BEFORE, ALNUM)),
    )


PAN_SHAPE: Final = (Chars(LETTER, 5, 5), Chars(DIGIT, 4, 4), Chars(LETTER))

INDIA: Final = Locale(
    "in",
    (
        PiiPattern(
            "in.phone.country_code",
            PiiKind.PHONE,
            (
                NOT_AFTER_DIGIT,
                OneOf(("+91", "0091")),
                Chars(SEPARATOR, 0, 1),
                *spaced_digits(10),
                NOT_BEFORE_DIGIT,
            ),
        ),
        PiiPattern(
            "in.phone.mobile",
            PiiKind.PHONE,
            (
                NOT_AFTER_DIGIT,
                Chars("[6-9]"),
                *spaced_digits(10)[1:],
                NOT_BEFORE_DIGIT,
            ),
        ),
        PiiPattern(
            "in.phone.trunk",
            PiiKind.PHONE,
            (
                NOT_AFTER_DIGIT,
                Chars("0"),
                Chars(SEPARATOR, 0, 1),
                *spaced_digits(10),
                NOT_BEFORE_DIGIT,
            ),
        ),
        PiiPattern(
            "in.aadhaar",
            PiiKind.AADHAAR,
            (NOT_AFTER_DIGIT, *_digits(4, 4, 4, separator=GROUP_SEPARATOR), NOT_BEFORE_DIGIT),
            Checksum.VERHOEFF,
        ),
        _word("in.pan", PiiKind.PAN, *PAN_SHAPE),
        _word(
            "in.gstin",
            PiiKind.PAN,
            Chars(DIGIT, 2, 2),
            *PAN_SHAPE,
            Chars(ALNUM),
            Chars("[Zz]"),
            Chars(ALNUM),
        ),
        _word("in.ifsc", PiiKind.IFSC, Chars(LETTER, 4, 4), Chars("0"), Chars(ALNUM, 6, 6)),
        _word("in.passport", PiiKind.PASSPORT, Chars(LETTER), Chars(DIGIT, 7, 7)),
        PiiPattern(
            "in.upi",
            PiiKind.UPI,
            (
                Chars(r"[\w.-]", 1, MAX_REPEAT),
                Chars("@"),
                Chars(LETTER),
                Chars(ALNUM, 1, MAX_REPEAT - 1),
                Assert(Look.NOT_BEFORE, r"[\w@-]"),
            ),
        ),
    ),
)
"""India (module docstring). A UPI handle has no dot after the `@`; one with a dot is an
e-mail address, and `generic.email`'s longer span gives the overlap its label (H2). The PAN
inside a GSTIN is not a whole word, so only `in.gstin` finds it."""

GENERIC: Final = Locale(
    "generic",
    (
        PiiPattern(
            "generic.email",
            PiiKind.EMAIL,
            (
                Chars(r"[\w.+-]", 1, MAX_REPEAT),
                Chars("@"),
                Chars(r"[\w-]", 1, MAX_REPEAT - 1),
                Chars(r"\."),
                Chars(r"[\w.-]", 1, MAX_REPEAT),
                Assert(Look.ENDS_WITH, r"[\w-]"),
            ),
        ),
        *(
            PiiPattern(
                f"generic.phone_{count}",
                PiiKind.PHONE,
                (
                    NOT_AFTER_DIGIT,
                    Chars(r"\+"),
                    Chars(SEPARATOR, 0, 1),
                    *spaced_digits(count),
                    NOT_BEFORE_DIGIT,
                ),
            )
            for count in range(8, 16)
        ),
        *(
            PiiPattern(
                f"generic.digits_{count}",
                PiiKind.CARD,
                (NOT_AFTER_DIGIT, *spaced_digits(count, GROUP_SEPARATOR), NOT_BEFORE_DIGIT),
                Checksum.LUHN,
            )
            for count in range(13, 20)
        ),
        PiiPattern("generic.long_digits", PiiKind.ID_NUMBER, (Chars(DIGIT, 20, MASK_CAP),)),
    ),
)
"""Everywhere (module docstring). An e-mail address ends in a letter, digit or hyphen, so a
sentence's full stop stays outside the span. A run of 20 or more digits has no lookaround: it
is one span however long (up to the cap), never a chunk with a visible rest."""

LOCALES: Final[Mapping[str, Locale]] = MappingProxyType(
    {locale.id: locale for locale in (INDIA, GENERIC)}
)
DEFAULT_LOCALE_IDS: Final = ("in", "generic")
"""Every business's locales until Q4b (module docstring) [proposal]."""


def locales(ids: Iterable[str] = DEFAULT_LOCALE_IDS) -> tuple[Locale, ...]:
    """The registered locales, in the given order. An unknown or repeated id is a ValueError
    naming it (locale ids are configuration, never customer text)."""
    chosen = tuple(ids)
    unknown = [locale_id for locale_id in chosen if locale_id not in LOCALES]
    if unknown or len(set(chosen)) != len(chosen):
        raise ValueError(f"unknown or repeated locale ids: {', '.join(chosen)}")
    return tuple(LOCALES[locale_id] for locale_id in chosen)


_FULLWIDTH: Final = range(0xFF01, 0xFF5F)
_FULLWIDTH_OFFSET: Final = 0xFEE0
MINUS_SIGN: Final = "\u2212"


def _ascii(character: str) -> str:
    category = unicodedata.category(character)
    if category == "Nd":
        return str(unicodedata.digit(character))
    if category == "Pd" or character == MINUS_SIGN:
        return "-"
    code = ord(character)
    if code in _FULLWIDTH:
        return chr(code - _FULLWIDTH_OFFSET)
    return character


def ascii_view(text: Clean) -> str:
    """`text` with every decimal digit as its ASCII digit, every fullwidth ASCII form as its ASCII
    character and every dash or minus sign as a hyphen, one character for one (module
    docstring): same length, same offsets."""
    return "".join(_ascii(character) for character in text)
