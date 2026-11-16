"""G14: a guard does not trust the check it guards [proposal; carried ADR 0017 E3].

At startup (`create_app`) and in CI, every configured term must match itself through `clean`,
`fold` and H1, and its obfuscated variants too, and must not match its negatives. Each term runs
alone, in a set of its own, so a hit can only be its own.

**Positives** (every one must hit):
- `itself`: the term as its file writes it (a `near` term: one word of each set, in set order);
- `zwj`: a ZWJ after its first character; `hyphen`: its words joined by hyphens;
- `accent`: a combining acute after its first letter (on a Devanagari or Telugu letter it is
  another script's mark, which the fold removes too [proposal]), and `homoglyph`: its first letter
  with a Cyrillic or Greek look-alike (Latin-script terms only);
- `punctuation`: brackets and marks around it; `sentence`: a filler word on each side [proposal];
- `near` terms also: `order` (the sets reversed), `word` (every other word of every set) and
  `window_edge` (the words spread over exactly the window) [proposal].

**Negatives** (none may hit):
- `prefixed` and `suffixed`: a `phrase` term glued to a letter of its own first word, so it
  starts or ends inside a word ("cchest pain", "chest painp") [proposal]. Not for a Telugu term
  (decided 2026-11-16, D-indic (b)): Telugu glues its case endings to the noun, so whether a
  Telugu term may hit inside a longer word is open until the Telugu reviewer (D6);
- `outside_window`: a `near` term's words spread one word wider than its window [proposal];
- `configured`: every fixed negative of `platform.json` ("paint the chest") against every
  business's whole term set and the unresolved set.

**Outcomes.** A miss or a wrong hit raises `SelftestError`, which stops startup; it names the
file, the location and the variant, never the term. While H1 is a stub (`NotImplementedError`),
the result is `matcher_unavailable`: startup goes on and every guarded route hands off (G3, wired
in PR 6-8). Any other exception from the matcher stops startup too.

`verified_hits` is the runtime half of "do not trust the check" (G14): a hit for a term or a
category the matcher was not given is refused (`UnexpectedHit`, a `guard_error` at the route).
"""

import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from vectapilot_api.guards.config import (
    BusinessGuards,
    ConfiguredTerm,
    GuardConfig,
    GuardConfigError,
)
from vectapilot_api.guards.match import match_terms
from vectapilot_api.guards.normalize import CONFUSABLES, ZWJ, clean, fold
from vectapilot_api.guards.types import Folded, PhraseTerm, TermHit, TermSet

Matcher = Callable[[Folded, TermSet], tuple[TermHit, ...]]
"""H1's signature (`match.match_terms`)."""

COMBINING_ACUTE: Final = "\u0301"
HOMOGLYPHS: Final = {
    latin: look_alike
    for look_alike, latin in reversed(tuple(CONFUSABLES.items()))
    if latin.islower() and latin.isascii() and not look_alike.isascii()
}
"""A lower-case Latin letter -> the first look-alike `normalize.CONFUSABLES` maps to it (reversed,
so the table's first entry wins: Cyrillic before Greek)."""
GLUE_OPEN_SCRIPTS: Final = ("TELUGU ",)
"""`unicodedata.name` prefixes of the scripts whose terms get no glued negatives (D-indic (b),
2026-11-16): strict whole words hold for Latin and Devanagari; "suffix-tolerant matching for te"
is an open question for the Telugu reviewer (D6)."""


class Positive(StrEnum):
    ITSELF = "itself"
    ZWJ = "zwj"
    HYPHEN = "hyphen"
    ACCENT = "accent"
    HOMOGLYPH = "homoglyph"
    PUNCTUATION = "punctuation"
    SENTENCE = "sentence"
    ORDER = "order"
    WORD = "word"
    WINDOW_EDGE = "window_edge"


class Negative(StrEnum):
    PREFIXED = "prefixed"
    SUFFIXED = "suffixed"
    OUTSIDE_WINDOW = "outside_window"
    CONFIGURED = "configured"


class SelftestStatus(StrEnum):
    PASSED = "passed"
    MATCHER_UNAVAILABLE = "matcher_unavailable"
    """H1 is still a stub: every guarded route hands off (G3, G14)."""


@dataclass(frozen=True)
class SelftestResult:
    """What ran: counts of checks, never a measurement."""

    status: SelftestStatus
    terms: int
    positives: int
    negatives: int


class SelftestError(GuardConfigError):
    """A term failed G14. Names the file, the location and the variant, never the term."""


class UnexpectedHit(ValueError):
    """The matcher returned a hit for a term or a category it was not given."""


class _Unavailable(Exception):
    """Internal: the matcher raised `NotImplementedError` (H1 is a stub)."""


def screen_text(text: str) -> Folded:
    """What a screen sees: `fold(clean(text))`, as a guarded route will compute it."""
    return fold(clean(text))


def verified_hits(hits: Sequence[TermHit], terms: TermSet) -> tuple[TermHit, ...]:
    """`hits`, if each one is a `TermHit` for a term of `terms` with that term's category; else
    `UnexpectedHit` (G14: team code does not trust H1's output)."""
    categories = {term.id: term.category for term in terms.terms}
    for hit in hits:
        if not isinstance(hit, TermHit) or categories.get(hit.term_id) is not hit.category:
            raise UnexpectedHit("the matcher returned a hit for a term it was not given")
    return tuple(hits)


def _glue_open(text: str) -> bool:
    """Whether `text` has a letter of a script in `GLUE_OPEN_SCRIPTS`."""
    return any(unicodedata.name(c, "").startswith(GLUE_OPEN_SCRIPTS) for c in text)


def _latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all("a" <= c <= "z" for c in letters)


def _filler(words: Sequence[str]) -> str:
    """A word that is none of `words`: `x`, `xx`, ..."""
    filler = "x"
    while filler in words:
        filler += "x"
    return filler


def _with_accent(text: str) -> str:
    index = next(i for i, c in enumerate(text) if c.isalpha())
    return text[: index + 1] + COMBINING_ACUTE + text[index + 1 :]


def _with_homoglyph(text: str) -> str | None:
    for index, character in enumerate(text):
        if character in HOMOGLYPHS:
            return text[:index] + HOMOGLYPHS[character] + text[index + 1 :]
    return None


def _spread(words: Sequence[str], fillers: int, filler: str) -> str:
    """The first word, `fillers` filler words, then the rest."""
    return " ".join([words[0], *([filler] * fillers), *words[1:]])


def positives(configured: ConfiguredTerm) -> tuple[tuple[Positive, str], ...]:
    """Every positive text of one term (module docstring)."""
    spec, term = configured.spec, configured.term
    found: list[tuple[Positive, str]] = []
    base: str
    if isinstance(term, PhraseTerm):
        base = term.text
        # As the file writes it (capitals, accents, joiners), so clean and fold are tested too.
        found.append((Positive.ITSELF, spec.text or base))
        filler = _filler(term.words)
    else:
        firsts = [words[0] for words in term.sets]
        base = " ".join(firsts)
        found.append((Positive.ITSELF, " ".join(words[0] for words in spec.sets or term.sets)))
        found.append((Positive.ORDER, " ".join(reversed(firsts))))
        for position, words in enumerate(term.sets):
            for word in words[1:]:
                swapped = [*firsts[:position], word, *firsts[position + 1 :]]
                found.append((Positive.WORD, " ".join(swapped)))
        filler = _filler([word for words in term.sets for word in words])
        found.append((Positive.WINDOW_EDGE, _spread(firsts, term.window - len(firsts), filler)))
    found.append((Positive.ZWJ, base[:1] + ZWJ + base[1:]))
    if " " in base:
        found.append((Positive.HYPHEN, base.replace(" ", "-")))
    if any(c.isalpha() for c in base):  # a term of digits alone has no letter to accent
        found.append((Positive.ACCENT, _with_accent(base)))
    if _latin(base):
        homoglyph = _with_homoglyph(base)
        if homoglyph is not None:
            found.append((Positive.HOMOGLYPH, homoglyph))
    found.append((Positive.PUNCTUATION, f"({base})!"))
    found.append((Positive.SENTENCE, f"{filler} {base} {filler}"))
    return tuple(found)


def negatives(configured: ConfiguredTerm) -> tuple[tuple[Negative, str], ...]:
    """The negatives derived from one term (module docstring)."""
    term = configured.term
    if isinstance(term, PhraseTerm):
        if _glue_open(term.text):
            return ()
        first, last = term.words[0], term.words[-1]
        return (
            (Negative.PREFIXED, first[0] + term.text),
            (Negative.SUFFIXED, term.text + last[0]),
        )
    firsts = [words[0] for words in term.sets]
    filler = _filler([word for words in term.sets for word in words])
    spread = _spread(firsts, term.window - len(firsts) + 1, filler)
    return ((Negative.OUTSIDE_WINDOW, spread),)


def _hits(matcher: Matcher, text: str, terms: TermSet, where: str) -> tuple[TermHit, ...]:
    error_type = None
    try:
        hits = matcher(screen_text(text), terms)
    except NotImplementedError:
        raise _Unavailable from None
    except Exception as error:  # any other failure stops startup, named below
        error_type = type(error).__name__
    if error_type is not None:
        raise SelftestError(f"G14: the term matcher raised {error_type} on {where}")
    try:
        return verified_hits(hits, terms)
    except UnexpectedHit:
        pass
    raise SelftestError(f"G14: the term matcher returned a hit it was not given on {where}")


def _checks(config: GuardConfig) -> Iterator[tuple[TermSet, str, str, bool]]:
    """Every check: the term set, the text, what an error names, and whether a hit is wanted."""
    for configured in config.terms:
        alone = TermSet((configured.term,))
        for positive, text in positives(configured):
            yield alone, text, f"{configured.where} ({positive.value})", True
        for negative, text in negatives(configured):
            yield alone, text, f"{configured.where} ({negative.value} negative)", False
    sets: list[tuple[str, BusinessGuards]] = [
        (f"{business_id}'s terms", guards) for business_id, guards in config.businesses.items()
    ]
    sets.append(("the unresolved set", config.unresolved))
    for spec, where in config.negatives:
        for scope, guards in sets:
            yield (
                guards.all_terms(),
                spec.text,
                f"{where} ({Negative.CONFIGURED.value} negative, {scope})",
                False,
            )


def run_selftest(config: GuardConfig, matcher: Matcher = match_terms) -> SelftestResult:
    """G14 over the whole configuration (module docstring). Raises `SelftestError` on a miss or a
    negative's hit; returns `matcher_unavailable` while H1 is a stub."""
    counts = {True: 0, False: 0}
    try:
        for terms, text, where, wanted in _checks(config):
            hits = _hits(matcher, text, terms, where)
            if wanted and not any(hit.term_id == terms.terms[0].id for hit in hits):
                raise SelftestError(f"G14: {where} does not match its term")
            if not wanted and hits:
                raise SelftestError(f"G14: {where} matches a term")
            counts[wanted] += 1
    except _Unavailable:
        return SelftestResult(SelftestStatus.MATCHER_UNAVAILABLE, len(config.terms), 0, 0)
    return SelftestResult(SelftestStatus.PASSED, len(config.terms), counts[True], counts[False])
