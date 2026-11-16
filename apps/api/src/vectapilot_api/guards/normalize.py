"""One normaliser, two outputs (G5): `clean` for the model, `fold` for the screens.

**`clean`** is what the model, the embedder and masking see. It drops what a person cannot see
but a model reads as tokens: every character `tools.config.is_invisible` refuses in configured
text (format, private-use, unassigned and surrogate code points, ZWNJ and ZWJ excepted, and
variation selectors 17-256), imported rather than restated, plus variation selectors 1-14 and
stray control characters. It keeps ZWNJ and ZWJ (Devanagari and Telugu need them to pick a
conjunct's shape) and VS15/VS16 (text and emoji presentation), applies NFC and collapses
whitespace.

**`fold`** is what the red-flag, injection and output screens see, and every configured term
goes through the same fold, so both sides of a match always agree:
1. drop every format, private-use, unassigned and surrogate code point (ZWNJ and ZWJ included),
   every variation selector, the default-ignorable characters that are not format characters
   (`DEFAULT_IGNORABLE`) and stray control characters;
2. map look-alikes to Latin (`CONFUSABLES`), then NFKD (fullwidth and mathematical letters become
   plain ones);
3. map look-alikes again, case-fold, map again;
4. NFKD again; map the Hindi spelling variants to one form (`HINDI_VARIANTS`: chandrabindu to
   anusvara, the nukta dropped); remove the combining marks of Latin, Greek and Cyrillic letters,
   orphaned marks, and on any other letter the marks of another script (a Devanagari or Telugu
   letter keeps its own signs); then NFC;
5. map every decimal digit to its ASCII digit (`१०८` and `౧౦౮` become `108`), turn every
   punctuation and symbol character and every apostrophe-like letter (`APOSTROPHE_LIKE`) into a
   space, and collapse whitespace.

Step 3 runs before the marks are removed because case-folding can add one: U+01F0 (j with caron)
folds to `j` plus a combining caron. Step 2 maps before NFKD because a look-alike with a
compatibility decomposition would otherwise escape the table: U+03F2 (lunate sigma, which looks
like `c`) becomes a final sigma under NFKD. `HINDI_VARIANTS` is G5 as amended on 2026-11-16
(shesha, D-hindi-fold (a)): साँस and सांस, तकलीफ़ and तकलीफ fold equal; the Telugu equivalents
wait for the Telugu reviewer (D6).

A `[proposal]` beyond the plan's list, each closing a bypass: stray control characters are
dropped (`ch\\x01est`), enclosing marks are removed like non-spacing ones (`c\\u20ddhest`), a
mark without a letter before it is removed, and the dotless U+0131 and U+0237 map to `i` and `j`.
From ananya's PR 1 review: the non-format default-ignorables are dropped (`c` + HANGUL FILLER +
`hest`), another script's mark on an Indic letter is removed (a combining acute on `द`), Latin
IPA, small-capital and Armenian look-alikes are in the table, apostrophe-like letters are spaces
(`can` + U+02BC + `t` folds as `can't` does), and every decimal digit is ASCII.

Look-alikes and invisible characters are written as escapes in this module and its tests, so the
source shows exactly which code point each one is.

**The invisible-character hit** (an injection signal, G5): a tag character (U+E0000-E007F, "ASCII
smuggling"), a variation selector 17-256, or a run of `INVISIBLE_RUN_HIT` or more characters that
the fold drops in step 1. A lone soft hyphen or byte-order mark, common in PDF text, is counted but
is not a hit. ZWNJ and ZWJ count towards a run: otherwise two joiners alone could carry hidden
bits. **Count the raw text, before `clean`:** `clean` removes tags, VS17-256 and zero-width spaces,
so counts taken after it lose the signal (a guarded route counts the request text, then cleans,
then folds).

**Uncovered languages** (D3 (i)): `uncovered_languages` names each `not_covered` language whose
script range (from `platform.json`) has a character in the text. Romanised text in such a
language is not detected: a known gap.

Everything here is pure and linear in the text; every surface's text is bounded by its input cap
(`caps`, G3) before it gets here.
"""

import re
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vectapilot_api.guards.types import Clean, Folded
from vectapilot_api.tools.config import is_invisible

ZWNJ: Final = "\u200c"
ZWJ: Final = "\u200d"
ZWSP: Final = "\u200b"
VS15: Final = "\ufe0e"
VS16: Final = "\ufe0f"
VARIATION_SELECTORS: Final = range(0xFE00, 0xFE10)
"""VS1-16 (`Mn`). `clean` drops VS1-14 and keeps VS15 and VS16; `fold` drops all sixteen."""
CLEAN_DROPPED_SELECTORS: Final = range(0xFE00, 0xFE0E)
"""VS1-14: no script we serve needs them."""
VARIATION_SUPPLEMENT: Final = range(0xE0100, 0xE01F0)
"""VS17-256 (`Mn`): always an injection hit."""
TAGS: Final = range(0xE0000, 0xE0080)
"""Tag characters (`Cf`): always an injection hit."""
INVISIBLE_RUN_HIT: Final = 3
"""A run of this many characters that fold's first step drops is an injection hit [proposal]."""
DEFAULT_IGNORABLE: Final = frozenset(
    {
        0x034F,  # COMBINING GRAPHEME JOINER (Mn)
        0x115F,  # HANGUL CHOSEONG FILLER (Lo)
        0x1160,  # HANGUL JUNGSEONG FILLER (Lo)
        0x17B4,  # KHMER VOWEL INHERENT AQ (Mn)
        0x17B5,  # KHMER VOWEL INHERENT AA (Mn)
        0x180B,  # MONGOLIAN FREE VARIATION SELECTOR ONE (Mn)
        0x180C,  # MONGOLIAN FREE VARIATION SELECTOR TWO (Mn)
        0x180D,  # MONGOLIAN FREE VARIATION SELECTOR THREE (Mn)
        0x180F,  # MONGOLIAN FREE VARIATION SELECTOR FOUR (Mn)
        0x3164,  # HANGUL FILLER (Lo)
        0xFFA0,  # HALFWIDTH HANGUL FILLER (Lo)
    }
)
"""The default-ignorable code points that are not format characters (`Cf`), so the category rule
misses them: invisible, yet a letter or a mark that would stay inside a word ("c" + HANGUL FILLER
+ "hest pain") [proposal]. No script we serve needs any of them; `clean` and `fold` drop them."""
APOSTROPHE_LIKE: Final = frozenset(
    {
        0x02B9,  # MODIFIER LETTER PRIME
        0x02BA,  # MODIFIER LETTER DOUBLE PRIME
        0x02BB,  # MODIFIER LETTER TURNED COMMA
        0x02BC,  # MODIFIER LETTER APOSTROPHE
        0x02C8,  # MODIFIER LETTER VERTICAL LINE
        0xA78B,  # LATIN CAPITAL LETTER SALTILLO
        0xA78C,  # LATIN SMALL LETTER SALTILLO
    }
)
"""Letters (`Lm`, `Lu`, `Ll`) that look like an apostrophe or a quote: fold turns them into a
space as it does punctuation, so `can` + U+02BC + `t` folds as "can't" does [proposal]."""
HINDI_VARIANTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "\u0901": "\u0902",  # DEVANAGARI SIGN CANDRABINDU -> ANUSVARA (साँस, सांस)
        "\u093c": "",  # DEVANAGARI SIGN NUKTA, dropped (तकलीफ़, तकलीफ)
    }
)
"""Hindi spellings used interchangeably, folded to one form after NFKD (which splits a precomposed
nukta letter such as U+0958 into its base and U+093C). G5 as amended 2026-11-16 (shesha,
D-hindi-fold (a)): a little precision for recall. Telugu's (U+0C01, U+0C3C) wait for the Telugu
reviewer (D6)."""

_HIDDEN_CATEGORIES: Final = frozenset({"Cf", "Co", "Cn", "Cs"})
_STRIPPED_MARKS: Final = frozenset({"Mn", "Me"})
_STRIPPED_SCRIPTS: Final = ("LATIN ", "GREEK ", "CYRILLIC ")
"""`unicodedata.name` prefixes of the letters whose marks fold removes."""
_LINE_BREAKS: Final = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
"""What `str.splitlines` splits on."""
_WHITESPACE: Final = re.compile(r"\s+")

CONFUSABLES: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Cyrillic, capital
        "\u0410": "A",
        "\u0412": "B",
        "\u0421": "C",
        "\u0415": "E",
        "\u041d": "H",
        "\u0406": "I",
        "\u0408": "J",
        "\u041a": "K",
        "\u041c": "M",
        "\u041e": "O",
        "\u0420": "P",
        "\u0405": "S",
        "\u0422": "T",
        "\u0425": "X",
        "\u04ae": "Y",
        "\u04c0": "I",
        "\u051a": "Q",
        "\u051c": "W",
        # Cyrillic, small
        "\u0430": "a",
        "\u0441": "c",
        "\u0501": "d",
        "\u0435": "e",
        "\u04bb": "h",
        "\u0456": "i",
        "\u0458": "j",
        "\u04cf": "l",
        "\u043e": "o",
        "\u0440": "p",
        "\u051b": "q",
        "\u0455": "s",
        "\u051d": "w",
        "\u0445": "x",
        "\u0443": "y",
        # Greek, capital
        "\u0391": "A",
        "\u0392": "B",
        "\u0395": "E",
        "\u0396": "Z",
        "\u0397": "H",
        "\u0399": "I",
        "\u039a": "K",
        "\u039c": "M",
        "\u039d": "N",
        "\u039f": "O",
        "\u03a1": "P",
        "\u03a4": "T",
        "\u03a5": "Y",
        "\u03a7": "X",
        # Greek, small
        "\u03b1": "a",
        "\u03b3": "y",
        "\u03b9": "i",
        "\u03ba": "k",
        "\u03bd": "v",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c5": "u",
        "\u03c7": "x",
        # Greek lunate sigma: NFKD would turn it into a sigma, so the table runs before NFKD too
        "\u03f2": "c",
        "\u03f9": "C",
        # Latin letters that NFKD leaves alone: dotless, IPA and small capitals [proposal]
        "\u0131": "i",
        "\u0237": "j",
        "\u0251": "a",
        "\u0261": "g",
        "\u0269": "i",
        "\u1d04": "c",
        "\u1d0f": "o",
        # Armenian, small [proposal]
        "\u0570": "h",
        "\u057d": "u",
        "\u0585": "o",
    }
)
"""Look-alikes mapped to the Latin letter they imitate (a small stdlib table, G5). Capitals map
to capitals before the case-fold, so U+0397 (capital eta) becomes `h`, while U+03B7 (small eta)
stays itself."""


def _stray_control(character: str) -> bool:
    """A control character (`Cc`) that is not whitespace, such as NUL or ESC."""
    return unicodedata.category(character) == "Cc" and not character.isspace()


def _cleaned_away(character: str) -> bool:
    """What `clean` drops."""
    code = ord(character)
    return (
        is_invisible(character)
        or code in CLEAN_DROPPED_SELECTORS
        or code in DEFAULT_IGNORABLE
        or _stray_control(character)
    )


def folded_away(character: str) -> bool:
    """What `fold`'s first step drops: everything `clean` drops, plus ZWNJ, ZWJ, VS15 and VS16."""
    code = ord(character)
    return (
        unicodedata.category(character) in _HIDDEN_CATEGORIES
        or code in VARIATION_SELECTORS
        or code in VARIATION_SUPPLEMENT
        or code in DEFAULT_IGNORABLE
        or _stray_control(character)
    )


def _collapse_keeping_lines(text: str) -> str:
    """Each whitespace run becomes one line break if it holds one, else one space; trimmed."""

    def one(run: re.Match[str]) -> str:
        return "\n" if any(c in _LINE_BREAKS for c in run.group()) else " "

    return _WHITESPACE.sub(one, text).strip()


def clean(text: str) -> Clean:
    """`text` as the model may see it (module docstring). Line breaks survive as single `\\n`
    so a chunk's table or a page's lines keep their shape [proposal]."""
    kept = "".join(c for c in text if not _cleaned_away(c))
    return Clean(_collapse_keeping_lines(unicodedata.normalize("NFC", kept)))


def _script(character: str) -> str:
    """The first word of a character's name (`DEVANAGARI`, `TELUGU`, `COMBINING`, ...)."""
    return unicodedata.name(character, "").split(" ", 1)[0]


def _strip_marks(text: str) -> str:
    """Remove each `Mn`/`Me` mark whose base (the last non-mark character before it) is a Latin,
    Greek or Cyrillic letter, or is not a letter at all, and each mark of another script than its
    base's (a combining acute on `द`, a dot below on `న`); keep a script's own signs."""
    kept: list[str] = []
    base: str | None = None
    for character in text:
        category = unicodedata.category(character)
        if category in _STRIPPED_MARKS:
            if base is None or not unicodedata.category(base).startswith("L"):
                continue
            if unicodedata.name(base, "").startswith(_STRIPPED_SCRIPTS):
                continue
            if _script(character) != _script(base):
                continue
        elif not category.startswith("M"):
            # A spacing mark (`Mc`, such as Devanagari's `ी`) is not a base: the anusvara after
            # it in `नहीं` still belongs to `ह`, so it stays.
            base = character
        kept.append(character)
    return "".join(kept)


def _confusables(text: str) -> str:
    return "".join(CONFUSABLES.get(c, c) for c in text)


def _hindi_variants(text: str) -> str:
    return "".join(HINDI_VARIANTS.get(c, c) for c in text)


def _ascii_digit(character: str) -> str:
    """A decimal digit (`Nd`, any script) as its ASCII digit; anything else as it is."""
    if unicodedata.category(character) == "Nd":
        return str(unicodedata.decimal(character))
    return character


def _spaced(character: str) -> str:
    if unicodedata.category(character)[0] in "PS" or ord(character) in APOSTROPHE_LIKE:
        return " "
    return _ascii_digit(character)


def fold(text: str) -> Folded:
    """`text` as the screens see it (module docstring, steps 1-5)."""
    visible = "".join(c for c in text if not folded_away(c))
    # Mapped before NFKD (a look-alike NFKD would decompose), again before the case-fold
    # (capitals keep their own look-alike) and again after it (a capital outside the table can
    # fold to a small letter inside it, as U+0500 to U+0501).
    decomposed = unicodedata.normalize("NFKD", _confusables(visible))
    mapped = _confusables(_confusables(decomposed).casefold())
    unmarked = _strip_marks(_hindi_variants(unicodedata.normalize("NFKD", mapped)))
    composed = unicodedata.normalize("NFC", unmarked)
    spaced = "".join(_spaced(c) for c in composed)
    return Folded(" ".join(spaced.split()))


@dataclass(frozen=True)
class InvisibleCounts:
    """What a text hid: counts only, never the characters' positions or the text."""

    invisible: int
    """Characters `fold`'s first step drops (ZWNJ and ZWJ included)."""
    tags: int
    supplement_selectors: int
    longest_run: int

    @property
    def hit(self) -> bool:
        """The injection signal (module docstring)."""
        return bool(self.tags or self.supplement_selectors) or self.longest_run >= INVISIBLE_RUN_HIT


def count_invisible(text: str) -> InvisibleCounts:
    """What the raw `text` hides. The raw text, before `clean`: `clean` removes the very
    characters the hit rule counts (module docstring)."""
    invisible = tags = supplement = longest = run = 0
    for character in text:
        if not folded_away(character):
            run = 0
            continue
        invisible += 1
        run += 1
        longest = max(longest, run)
        code = ord(character)
        tags += code in TAGS
        supplement += code in VARIATION_SUPPLEMENT
    return InvisibleCounts(invisible, tags, supplement, longest)


ScriptRanges = Mapping[str, tuple[tuple[int, int], ...]]
"""Language tag -> its script's inclusive code point ranges (`platform.json`'s `scripts`)."""


def uncovered_languages(
    text: str, not_covered: Collection[str], scripts: ScriptRanges
) -> frozenset[str]:
    """Each language in `not_covered` with at least one character of its script in `text`
    (D3 (i)). A language without a script range is a KeyError: the loader refuses one."""
    ranges = [
        (start, end, language) for language in not_covered for start, end in scripts[language]
    ]
    found: set[str] = set()
    for character in text:
        code = ord(character)
        found.update(language for start, end, language in ranges if start <= code <= end)
    return frozenset(found)
