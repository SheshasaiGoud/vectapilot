"""H3 — output checks on model text (`[HAND-WRITE]`, D5 (b): written by Claude at shesha's request,
on its own branch `m7/output-checks`; guide `docs/learning/m7-output-checks.md`, M7 PR 6).

G8: H3 reads what the model wrote before the customer sees it: a `reply`'s text, an `answer`
kind's answer text, `/v1/answer`'s answer (PR 8, behind G11) and a proposal's ticket text
(`create_ticket`'s `summary` argument). It never runs on a hand-off or on a
`confirmation_required` summary (a code template). A finding replaces the whole response with a
hand-off (`guard_output`, G2): the route never edits the model's text, and never shows it.

Around H3, as team code: the input cap per surface (`caps`, G3), the term matcher (H1, passed in
the context so the route's test seam reaches it too) and `selftest.verified_hits` (G14), the PII
patterns and their ASCII view (`locales`, H2's detector), the dose vocabulary below, and the route
that turns a finding into a hand-off and a `guard` record (`guards.agent`). `check_reply`'s body
and its private helpers (after it) were written by Claude at shesha's request on 2026-11-23 (D5
(b)); the explain-back is shesha's. A check that raises still hands off (`guard_error`, G3: fail
closed). The red-team suite's output cases are pending on H1 until it is written (a case waits on
the first unwritten hand-write it needs: H1, then H2, then H3).

**The dose vocabulary** (`DOSE_NUMBERS`, `DOSE_UNITS`, `ADVICE_VERBS`, `TIMES`, `PER_DAY`,
`DAY_IN`, `RANGE_JOINS`, `FREQUENCY_WORDS`, `EVERY`, `HOURS`) is team data, like a PII pattern:
G8's "dose patterns in en and hi, with digits, number words and fractions, units and frequency
phrases" are a shape, which a phrase list cannot express (any digit run is a number). Each word is
stored folded (`normalize.fold` over `normalize.clean`), so H3 compares it with the folded reply's
words as they are. A missing word is a data change with its own test, never H3's job. The phrase
lists themselves (success claims, advice imperatives with their own numbers, diagnosis, abuse)
stay in `data/safety/` (G4). The review of 2026-11-23 (zara-safety, ananya-rag) widened the
vocabulary and rules 3-6 (each a tightening); the guide's §5 lists what it changed.
"""

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import accumulate
from types import MappingProxyType
from typing import Final

from vectapilot_api.guards.caps import Surface, require_within_cap
from vectapilot_api.guards.locales import Locale, ascii_view
from vectapilot_api.guards.normalize import clean, fold
from vectapilot_api.guards.pii import SQUEEZED_OUT
from vectapilot_api.guards.selftest import verified_hits
from vectapilot_api.guards.types import Clean, Finding, Folded, TermCategory, TermHit, TermSet
from vectapilot_api.intents.policy import BusinessType

Matcher = Callable[[Folded, TermSet], tuple[TermHit, ...]]
"""H1's signature (`match.match_terms`), as `selftest.Matcher`."""


class OutputKind(StrEnum):
    """Which model text H3 reads (G8)."""

    REPLY = "reply"
    ANSWER = "answer"
    TICKET_TEXT = "ticket_text"


OUTPUT_FINDINGS: Final[frozenset[Finding]] = frozenset(
    {
        Finding.OUTPUT_SUCCESS_CLAIM,
        Finding.OUTPUT_MEDICAL_ADVICE,
        Finding.OUTPUT_DIAGNOSIS,
        Finding.OUTPUT_ABUSE,
        Finding.OUTPUT_PII,
        Finding.OUTPUT_BOUNDARY,
        Finding.OUTPUT_UNGROUNDED_DIGITS,
    }
)
"""Every code `check_reply` may return; the route refuses any other (G14: team code does not trust
the check it guards)."""


def _folded(words: Iterable[str]) -> frozenset[str]:
    return frozenset(fold(clean(word)) for word in words)


DOSE_NUMBERS: Final = _folded(
    (
        # en: G8's "one to ten, half, quarter, 'a'/'an' plus a unit"
        *("a", "an", "one", "two", "three", "four", "five"),
        *("six", "seven", "eight", "nine", "ten", "half", "quarter"),
        # hi (Devanagari): one to ten, half
        *("एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ", "दस", "आधा", "आधी"),
        # hi (romanised, as PR 4's term files write it)
        *("ek", "do", "teen", "char", "paanch", "aadha", "aadhi"),
    )
)
"""Number words, folded. Every word of only ASCII digits is a number too (`fold` maps every
script's decimal digits to ASCII, and a fraction's slash to a space: "1/2" is two numbers)."""

DOSE_UNITS: Final = _folded(
    (
        *("tablet", "tablets", "pill", "pills", "drop", "drops", "capsule", "capsules"),
        *("ml", "mg", "spoon", "spoons", "spoonful", "spoonfuls", "teaspoon", "teaspoons"),
        *("गोली", "गोलियां", "चम्मच", "बूंद", "कैप्सूल"),
        *("goli", "goliyan", "chammach"),
        # review 2026-11-23 (zara-safety, ananya-rag): each with its own test
        *("tsp", "tbsp", "tablespoon", "tablespoons", "teaspoonful", "puff", "puffs"),
        *("sachet", "sachets", "dose", "doses", "mcg", "tab", "tabs"),
        *("बूंदें", "boond", "boondein"),
    )
)
"""G8's units (tablet, pill, drop, ml, mg, spoon, गोली), with plurals and the same units in hi."""

ADVICE_VERBS: Final = _folded(
    (
        *("take", "takes", "taking", "give", "gives", "giving"),
        *("लें", "लीजिए", "लेते", "दें", "दीजिए", "खाएं", "खिलाएं", "पिलाएं", "डालें"),
        *("lein", "lijiye", "dijiye", "khaiye", "khilaiye", "pilaiye"),
        # review 2026-11-23 (zara-safety): passives, past forms and administration verbs ("do"
        # is not one: it is a number), each with its own test
        *("taken", "given", "took", "gave", "put", "apply", "applied", "use", "add"),
        *("administer", "feed", "mix", "place", "instil", "drink", "swallow", "chew"),
        *("dein", "de", "lo", "lena", "daalein", "dalein", "khilao", "pilao"),
    )
)
"""Advice imperatives a dose follows (G8), folded."""

TIMES: Final = _folded(("times", "time", "बार", "baar", "bar", "x"))
"""What follows a number in a frequency ("3 times", "3x" read as "3" "x", "दो बार")."""
MORE: Final = fold(clean("more"))
"""The one word that may sit between a number and its `TIMES` word ("3 more times a day")."""
PER_DAY: Final = tuple(
    tuple(fold(clean(w)) for w in day)
    for day in (
        ("a", "day"),
        ("per", "day"),
        ("daily",),
        ("day",),  # "3 times/day" folds to "3 times day"
        ("each", "day"),
        ("every", "day"),
        ("in", "a", "day"),
    )
)
"""What follows `<number> times` (or a `FREQUENCY_WORDS` word) in an en frequency phrase."""
DAY_IN: Final = tuple(
    tuple(fold(clean(w)) for w in day)
    for day in (
        ("दिन", "में"),
        ("din", "mein"),
        ("din", "me"),
        ("रोज",),
        ("रोजाना",),
        ("roz",),
        ("rozana",),
        ("har", "din"),
    )
)
"""What precedes `<number> बार` in a hi frequency phrase ("दिन में दो बार", "roz do baar")."""
RANGE_JOINS: Final = _folded(("से", "se", "to", "or", "या", "ya"))
"""What may join two numbers into a range ("2 to 3", "do se teen", "do ya teen"); "2-3" folds to
"2 3", two numbers in a row, a range without a join."""
FREQUENCY_WORDS: Final = _folded(("once", "twice", "thrice"))
"""A number and `times` in one word: before a `PER_DAY` sequence, a frequency ("twice daily")."""
EVERY: Final = _folded(("every", "हर", "har"))
HOURS: Final = _folded(("hours", "hrs", "घंटे", "ghante"))
"""`EVERY`, a number or a range, then an `HOURS` word is a frequency ("every 6 hours")."""
VERB_REACH: Final = 3
"""A dose's advice verb is among the `VERB_REACH` words before its number ("give him half") or
after its unit (Hindi puts the verb last: "दो गोली दें")."""
UNIT_REACH: Final = 2
"""A dose's unit is one of the `UNIT_REACH` words after its number ("two paracetamol tablets")."""


@dataclass(frozen=True)
class OutputContext:
    """What H3 may read besides the model's text (G8). Never the database, never a tool result.

    - `business_type` and `health`: the medical-advice check runs for every type, diagnosis
      phrases for health types (`health` is true for an unresolved business: fail towards
      safety, G4);
    - `message`: the customer's message after `normalize.clean`, before masking: a PII-shaped
      token the customer wrote themselves is not "PII from nowhere";
    - `kind`: which text this is;
    - `cited`: the clean texts of the chunks an answer cites (answer kinds only; empty
      otherwise): the only grounding for an answer's digits and an exemption for its PII;
    - `guidance`: the business's guidance lines (`data/safety/businesses/<id>.json`), also an
      exemption for PII (the clinic's own number);
    - `terms`: the business's output phrases (success claims, medical advice, diagnosis, abuse;
      `config.BusinessGuards.output`), matched with `matcher` over `normalize.fold`'s text;
    - `boundary_lines`: fixed prompt text whose echo is a finding: the boundary markers' stems
      and the system prompt's sentences that hold no per-turn value (`screens.boundary_lines`);
    - `locales`: the PII locales (`locales.locales()`) whose patterns are H2's detector.
    """

    business_type: BusinessType | None
    health: bool
    message: Clean
    kind: OutputKind
    cited: tuple[Clean, ...]
    guidance: tuple[str, ...]
    terms: TermSet
    matcher: Matcher
    boundary_lines: tuple[str, ...]
    locales: Sequence[Locale]

    def __post_init__(self) -> None:
        if self.cited and self.kind is not OutputKind.ANSWER:
            raise ValueError("only an answer has cited chunks")


OutputCheck = Callable[[str, OutputContext], tuple[Finding, ...]]
"""`check_reply`'s signature: routes and seams take one, H3 by default (tests pass stand-ins)."""


def check_reply(reply: str, context: OutputContext) -> tuple[Finding, ...]:
    """Every G8 finding in `reply`, as codes from `OUTPUT_FINDINGS`; empty when it may be shown.
    Pure: no I/O, no logging, no clock.

    The contract (`docs/plans/m7-pr-checks.md`, "G8" and "H3"; guide §2 explains each rule):
    1. **Cap.** `reply` longer than its kind's cap (`caps.INPUT_CAPS`: a reply and an answer
       `Surface.REPLY`, 1,500; ticket text `Surface.TICKET_TEXT`, 500) is `caps.OverCap` (G3:
       fail closed, never truncated). `clean(reply)` is what every rule below reads.
    2. **Phrases.** `context.matcher(fold(clean(reply)), context.terms)`, its hits passed through
       `selftest.verified_hits` (a hit for a term it was not given is `UnexpectedHit`). By the
       hit's category: `success_claim` is `OUTPUT_SUCCESS_CLAIM` on a reply only (the loop cannot
       confirm, W1); `medical_advice` is `OUTPUT_MEDICAL_ADVICE` on every kind and business type;
       `diagnosis` is `OUTPUT_DIAGNOSIS` only when `context.health`; `abuse` is `OUTPUT_ABUSE`.
       A hit of any other category is ignored (the context's terms are output terms only).
    3. **Doses** (`OUTPUT_MEDICAL_ADVICE`, every kind and business type), over the words of
       `fold(clean(reply))`: split on whitespace, and a word that starts with ASCII digits and
       goes on with other characters is read as two words, those digits and the rest ("10mg" is
       "10" "mg", "3x" is "3" "x"). A *number* is a word of only ASCII digits or a word in
       `DOSE_NUMBERS`; a *range* is a number, then a number or a `RANGE_JOINS` word and a number
       ("2 3" from "2-3", "do ya teen"). A finding is either
       - a *dose*: a number, then a `DOSE_UNITS` word among the next `UNIT_REACH` words, with an
         `ADVICE_VERBS` word among the `VERB_REACH` words before the number or the `VERB_REACH`
         words after that unit ("give him 5 drops", "दो गोली दें"); or
       - a *frequency*: a number or a range, then a `TIMES` word (or `MORE` and a `TIMES`
         word), then one of `PER_DAY`'s word sequences ("3 times a day", "3x daily"), or with
         one of `DAY_IN`'s sequences just before the number or range ("दिन में दो बार",
         "roz 2-3 baar"); a `FREQUENCY_WORDS` word, then a `PER_DAY` sequence ("twice daily");
         or an `EVERY` word, a number or a range, then an `HOURS` word ("every 6 hours").
    4. **PII** (`OUTPUT_PII`). H2's detector is the locales' patterns: every candidate
       (`PiiPattern.anywhere.finditer`, group 1) over `locales.ascii_view(clean(text))`; the
       reply's candidates also come from its squeezed view, the same view without
       `pii.SQUEEZED_OUT` (H2's second view: a joiner or a keycap mark inside a number), which
       only adds keys. A candidate's *key* is its span's letters and digits, case-folded. A key
       of the reply that is not a key of `context.message`, of a `context.cited` text or of a
       `context.guidance` line (each found over its ASCII view alone) is a finding. A tool
       result is never an exemption.
    5. **Boundary** (`OUTPUT_BOUNDARY`): a `context.boundary_lines` entry, without the sentence
       punctuation (`.:;!?`) at its end, appears in the reply, both compared without
       `pii.SQUEEZED_OUT`, case-folded, with every run of whitespace as one space and no space
       at either end ("<<<END MESSAGE 4f9c..." holds the stem "<<<END MESSAGE ").
    6. **Ungrounded digits** (`OUTPUT_UNGROUNDED_DIGITS`, the answer kind only). In the reply and
       in every cited text, after `locales.ascii_view(clean(text))`: remove each comma that has a
       digit on both sides (thousands separators: "1,200" and "1,20,000"), then take every
       maximal run of ASCII digits; the reply's runs also come from its second view, the view
       without `pii.SQUEEZED_OUT` and with every character that has a Unicode digit value
       (superscript and circled digits) as that ASCII digit, which only adds runs. A run of the
       reply that is not exactly a run of some cited text is a finding ("60" is not grounded by
       "600").
    7. **Each finding at most once**, in the order `Finding` declares them.
    """
    require_within_cap(_OUTPUT_SURFACES[context.kind], reply)  # rule 1, on the raw text
    cleaned = clean(reply)
    folded = fold(cleaned)
    found = _phrase_findings(folded, context)  # rule 2
    if _has_dose(folded):  # rule 3
        found.add(Finding.OUTPUT_MEDICAL_ADVICE)
    if _unexempt_pii(cleaned, context):  # rule 4
        found.add(Finding.OUTPUT_PII)
    if _echoes_boundary(cleaned, context.boundary_lines):  # rule 5
        found.add(Finding.OUTPUT_BOUNDARY)
    if context.kind is OutputKind.ANSWER and _ungrounded_digits(cleaned, context.cited):  # rule 6
        found.add(Finding.OUTPUT_UNGROUNDED_DIGITS)
    return tuple(finding for finding in Finding if finding in found)  # rule 7


# ── H3's private helpers, one per rule (written by Claude at shesha's request, D5 (b)) ───────

_OUTPUT_SURFACES: Final[Mapping[OutputKind, Surface]] = MappingProxyType(
    {
        OutputKind.REPLY: Surface.REPLY,
        OutputKind.ANSWER: Surface.REPLY,
        OutputKind.TICKET_TEXT: Surface.TICKET_TEXT,
    }
)
"""Rule 1: the cap each kind is read against (an answer is bounded as a reply is, `caps`)."""

_PHRASE_FINDINGS: Final[Mapping[TermCategory, Finding]] = MappingProxyType(
    {
        TermCategory.SUCCESS_CLAIM: Finding.OUTPUT_SUCCESS_CLAIM,
        TermCategory.MEDICAL_ADVICE: Finding.OUTPUT_MEDICAL_ADVICE,
        TermCategory.DIAGNOSIS: Finding.OUTPUT_DIAGNOSIS,
        TermCategory.ABUSE: Finding.OUTPUT_ABUSE,
    }
)
"""Rule 2: each output category's code."""


def _phrase_findings(folded: Folded, context: OutputContext) -> set[Finding]:
    """Rule 2: one matcher call, its hits verified (G14) before any category is read; a success
    claim counts on a reply only, a diagnosis for a health business only, any other category
    never."""
    hits = verified_hits(context.matcher(folded, context.terms), context.terms)
    counts = {
        TermCategory.SUCCESS_CLAIM: context.kind is OutputKind.REPLY,
        TermCategory.MEDICAL_ADVICE: True,
        TermCategory.DIAGNOSIS: context.health,
        TermCategory.ABUSE: True,
    }
    return {_PHRASE_FINDINGS[hit.category] for hit in hits if counts.get(hit.category, False)}


def _is_number(word: str) -> bool:
    """Rule 3: a word of only ASCII digits, or a number word."""
    return (word.isascii() and word.isdigit()) or word in DOSE_NUMBERS


def _is_dose(words: tuple[str, ...], at: int) -> bool:
    """Rule 3's dose for the number at `at`: a unit among the next `UNIT_REACH` words, and an
    advice verb among the `VERB_REACH` words before the number or after such a unit."""
    near = list(words[max(0, at - VERB_REACH) : at])
    unit = False
    for index in range(at + 1, min(at + 1 + UNIT_REACH, len(words))):
        if words[index] in DOSE_UNITS:
            unit = True
            near += words[index + 1 : index + 1 + VERB_REACH]
    return unit and not ADVICE_VERBS.isdisjoint(near)


def _range_end(words: tuple[str, ...], at: int) -> int:
    """Rule 3: the index of the last number of the range that starts at `at` (`at` itself when
    no range starts there)."""
    after = words[at + 1 : at + 3]
    if after[:1] and _is_number(after[0]):
        return at + 1
    if len(after) == 2 and after[0] in RANGE_JOINS and _is_number(after[1]):
        return at + 2
    return at


def _per_day_at(words: tuple[str, ...], at: int) -> bool:
    """Rule 3: one of `PER_DAY`'s sequences starts at `at`."""
    return any(words[at : at + len(day)] == day for day in PER_DAY)


def _is_frequency(words: tuple[str, ...], at: int) -> bool:
    """Rule 3's frequency for the number at `at`, or the range it starts: `every` before it and
    an `HOURS` word after it; or a `TIMES` word after it (`more` may come first), then a
    `PER_DAY` sequence (en) or with a `DAY_IN` sequence just before it (hi)."""
    end = _range_end(words, at)
    after = words[end + 1 : end + 3]
    if not HOURS.isdisjoint(after[:1]):
        return not EVERY.isdisjoint(words[max(0, at - 1) : at])
    skip = 1 if after[:1] == (MORE,) else 0
    if TIMES.isdisjoint(after[skip : skip + 1]):
        return False
    day_in = any(words[max(0, at - len(day)) : at] == day for day in DAY_IN)
    return day_in or _per_day_at(words, end + 2 + skip)


_LEADING_DIGITS: Final = re.compile(r"[0-9]+")


def _words(folded: Folded) -> tuple[str, ...]:
    """Rule 3's words: split on whitespace, a word that starts with ASCII digits and goes on with
    other characters read as two ("10mg" is "10" "mg")."""
    words: list[str] = []
    for word in folded.split():
        digits = _LEADING_DIGITS.match(word)
        cut = digits.end() if digits else 0
        words.extend((word[:cut], word[cut:]) if 0 < cut < len(word) else (word,))
    return tuple(words)


def _shape_at(words: tuple[str, ...], at: int) -> bool:
    """Rule 3 at one word: a dose or a frequency at a number, or a frequency word's `PER_DAY`."""
    if _is_number(words[at]):
        return _is_dose(words, at) or _is_frequency(words, at)
    return words[at] in FREQUENCY_WORDS and _per_day_at(words, at + 1)


def _has_dose(folded: Folded) -> bool:
    """Rule 3 over the folded words: a fixed number of comparisons per word, so linear."""
    words = _words(folded)
    return any(_shape_at(words, at) for at in range(len(words)))


def _squeezed(view: str) -> str:
    """Rules 4-6: `view` without `pii.SQUEEZED_OUT` (what a reader does not see inside a token)."""
    return "".join(character for character in view if character not in SQUEEZED_OUT)


def _key_text(view: str) -> tuple[str, tuple[int, ...]]:
    """Rule 4: the view's letters and digits, case-folded once (each character on its own, as
    `str.casefold` does), and where each view character's part of them starts: a candidate's
    key is one slice of them, whatever the candidate's length."""
    kept = [character.casefold() if character.isalnum() else "" for character in view]
    return "".join(kept), tuple(accumulate((len(part) for part in kept), initial=0))


def _pii_keys(view: str, locales: Sequence[Locale]) -> set[str]:
    """Rule 4: the key of every candidate of H2's detector in `view`."""
    keys, offsets = _key_text(view)
    return {
        keys[offsets[match.start(1)] : offsets[match.end(1)]]
        for locale in locales
        for pattern in locale.patterns
        for match in pattern.anywhere.finditer(view)
    }


def _unexempt_pii(cleaned: Clean, context: OutputContext) -> bool:
    """Rule 4: a key of the reply (either view) that neither the message, a cited text nor a
    guidance line holds. Never a tool result. An exempt text is only read while a reply key is
    left, and its detector only runs when a left key is a substring of its letters and digits
    (every candidate's key is one: skipping the rest changes nothing)."""
    view = ascii_view(cleaned)
    left = _pii_keys(view, context.locales)
    squeezed = _squeezed(view)
    if squeezed != view:
        left |= _pii_keys(squeezed, context.locales)
    for exempt in (context.message, *context.cited, *context.guidance):
        if not left:
            break
        exempt_view = ascii_view(clean(exempt))
        keys, _ = _key_text(exempt_view)
        if any(key in keys for key in left):
            left -= _pii_keys(exempt_view, context.locales)
    return bool(left)


_SENTENCE_FINAL: Final = ".:;!? "
"""Rule 5: what a boundary line ends without (an echo that changes only that still counts)."""


def _squashed(text: str) -> str:
    """Rule 5: without `SQUEEZED_OUT`, case-folded, every whitespace run one space, none at
    either end."""
    return " ".join(_squeezed(text).casefold().split())


def _echoes_boundary(cleaned: Clean, lines: Sequence[str]) -> bool:
    """Rule 5: a boundary line inside the reply (a fixed number of substring searches)."""
    reply = _squashed(cleaned)
    return any(_squashed(line).rstrip(_SENTENCE_FINAL) in reply for line in lines)


_GROUPING_COMMA: Final = re.compile(r"(?<=[0-9]),(?=[0-9])")
"""Rule 6: a thousands separator, a comma with a digit on both sides ("1,200", "1,20,000")."""
_DIGIT_RUN: Final = re.compile(r"[0-9]+")


def _digit_runs(view: str) -> set[str]:
    """Rule 6: the maximal ASCII digit runs of a view, thousands separators removed."""
    return set(_DIGIT_RUN.findall(_GROUPING_COMMA.sub("", view)))


def _as_digit(character: str) -> str:
    """Rule 6's second view: a character with a Unicode digit value as that ASCII digit."""
    value = unicodedata.digit(character, None)
    return character if value is None else str(value)


def _ungrounded_digits(cleaned: Clean, cited: Sequence[Clean]) -> bool:
    """Rule 6: a run of the answer (either view) that is exactly no run of any cited text."""
    view = ascii_view(cleaned)
    left = _digit_runs(view) | _digit_runs("".join(map(_as_digit, _squeezed(view))))
    for text in cited:
        left -= _digit_runs(ascii_view(clean(text)))
    return bool(left)
