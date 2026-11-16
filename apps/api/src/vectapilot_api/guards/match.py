"""H1 — the term matcher (`[HAND-WRITE]`, D5 (b): shesha writes it; guide
`docs/learning/m7-term-matching.md`).

One pure matcher serves the red-flag, injection and output-phrase screens (ADR 0018 part 4). The
fold (`normalize.fold`), the configuration (`config`) and the self-test (`selftest`, G14) around
it are team code. While this is a stub, `selftest.run_selftest` reports the matcher as
unavailable and every guarded route hands off (G3, G14): fail closed.
"""

from vectapilot_api.guards.types import Folded, TermHit, TermSet


def match_terms(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
    """Every hit of `terms` in `text`. Pure: no I/O, no logging, no clock.

    The contract (`docs/plans/m7-pr-checks.md`, "H1"):
    1. Terms are literal text, never regex (`re.escape`); both sides are already folded (G5).
    2. A `PhraseTerm` matches whole words on both ends: Latin-script "pain" does not hit "paint",
       and a Devanagari term never starts or ends inside a word. Between a term's words, zero or
       more whitespace (`\\s*`), so "chest pain" also matches "chestpain". A Telugu term must
       hit as a whole word too; whether it may also hit glued inside a longer word (Telugu
       attaches case endings) is open until the Telugu reviewer (decided 2026-11-16, D-indic
       (b): "suffix-tolerant matching for te"), and no test pins it.
    3. A `NearTerm` matches when every one of its word sets is matched by a word, all within
       `window` consecutive words of the text, in any order.
    4. Each hit carries the term's id, its category and its start offset in `text`, never the
       text. A term hits at most once, at its leftmost match [proposal]: the screens need which
       terms hit, not how often. A `NearTerm`'s start is the earliest word of the first window
       to complete, counting each set's most recent word ("chest chest pain" starts at 6).
       Every term that hits is returned, in text order, then term id.
    5. Empty text returns no hits. A term set with a duplicate id raises `ValueError`.
    6. Linear in the text: patterns come only from escaped terms, and the window scan passes
       over the words once.
    """
    # TODO(H1, shesha): implement the contract above. Its tests are tests/guards/test_match.py,
    # behind one switch (its `pytestmark`): when every case shows XPASS(strict), delete it.
    raise NotImplementedError("H1: match_terms is shesha's to write (m7-term-matching.md)")
