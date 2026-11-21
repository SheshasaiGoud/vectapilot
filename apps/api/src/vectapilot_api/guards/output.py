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
that turns a finding into a hand-off and a `guard` record (`guards.agent`). While `check_reply` is
the stub, every reply, answer and ticket text on a guarded route hands off (`guard_error`, G3: fail
closed), and the red-team suite's output cases are `pending:H3`.

**The dose vocabulary** (`DOSE_NUMBERS`, `DOSE_UNITS`, `ADVICE_VERBS`, `TIMES`, `PER_DAY`, the
Hindi frequency words) is team data, like a PII pattern: G8's "dose patterns in en and hi, with
digits, number words and fractions, units and frequency phrases" are a shape, which a phrase list
cannot express (any digit run is a number). Each word is stored folded (`normalize.fold` over
`normalize.clean`), so H3 compares it with the folded reply's words as they are. A missing word is
a data change with its own test, never H3's job. The phrase lists themselves (success claims,
advice imperatives with their own numbers, diagnosis, abuse) stay in `data/safety/` (G4).
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from vectapilot_api.guards.locales import Locale
from vectapilot_api.guards.normalize import clean, fold
from vectapilot_api.guards.types import Clean, Finding, Folded, TermHit, TermSet
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
    )
)
"""G8's units (tablet, pill, drop, ml, mg, spoon, गोली), with plurals and the same units in hi."""

ADVICE_VERBS: Final = _folded(
    (
        *("take", "takes", "taking", "give", "gives", "giving"),
        *("लें", "लीजिए", "लेते", "दें", "दीजिए", "खाएं", "खिलाएं", "पिलाएं", "डालें"),
        *("lein", "lijiye", "dijiye", "khaiye", "khilaiye", "pilaiye"),
    )
)
"""Advice imperatives a dose follows (G8), folded."""

TIMES: Final = _folded(("times", "time", "बार", "baar", "bar"))
PER_DAY: Final = (
    tuple(fold(clean(w)) for w in ("a", "day")),
    tuple(fold(clean(w)) for w in ("per", "day")),
    tuple(fold(clean(w)) for w in ("daily",)),
)
"""What follows `<number> times` in an en frequency phrase."""
DAY_IN: Final = (
    tuple(fold(clean(w)) for w in ("दिन", "में")),
    tuple(fold(clean(w)) for w in ("din", "mein")),
    tuple(fold(clean(w)) for w in ("din", "me")),
)
"""What precedes `<number> बार` in a hi frequency phrase ("दिन में दो बार")."""
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
       `fold(clean(reply))` (split on whitespace). A *number* is a word of only ASCII digits or
       a word in `DOSE_NUMBERS`. A finding is either
       - a *dose*: a number, then a `DOSE_UNITS` word among the next `UNIT_REACH` words, with an
         `ADVICE_VERBS` word among the `VERB_REACH` words before the number or the `VERB_REACH`
         words after that unit ("give him 5 drops", "दो गोली दें"); or
       - a *frequency*: a number, then a `TIMES` word, then one of `PER_DAY`'s word sequences
         ("3 times a day"); or one of `DAY_IN`'s sequences, then a number, then a `TIMES` word
         ("दिन में दो बार").
    4. **PII** (`OUTPUT_PII`). H2's detector is the locales' patterns: every candidate
       (`PiiPattern.anywhere.finditer`, group 1) over `locales.ascii_view(clean(text))`. A
       candidate's *key* is its span's letters and digits, case-folded. A key of the reply that
       is not a key of `context.message`, of a `context.cited` text or of a `context.guidance`
       line (each found the same way) is a finding. A tool result is never an exemption.
    5. **Boundary** (`OUTPUT_BOUNDARY`): a `context.boundary_lines` entry appears in the reply,
       both compared case-folded with every run of whitespace as one space and no space at
       either end ("<<<END MESSAGE 4f9c..." holds the stem "<<<END MESSAGE ").
    6. **Ungrounded digits** (`OUTPUT_UNGROUNDED_DIGITS`, the answer kind only). In the reply and
       in every cited text, after `locales.ascii_view(clean(text))`: remove each comma that has a
       digit on both sides (thousands separators: "1,200" and "1,20,000"), then take every
       maximal run of ASCII digits. A run of the reply that is not exactly a run of some cited
       text is a finding ("60" is not grounded by "600").
    7. **Each finding at most once**, in the order `Finding` declares them.
    """
    # TODO(H3): implement the contract above (guide: docs/learning/m7-output-checks.md). Its tests
    # are tests/guards/test_check_reply.py, behind one switch (its `pytestmark`).
    raise NotImplementedError(
        "H3: check_reply is written on m7/output-checks (m7-output-checks.md)"
    )
