"""What Python's `re` does with Devanagari and Telugu words (M7 PR 1, the H1 scaffold). These pass
now: they pin the standard library's behaviour that H1 must work around, so a Python upgrade
that changes it shows up here first (guide: `docs/learning/m7-term-matching.md` §2.4).

`\\w` is a letter, a digit or `_` (`str.isalnum()`, plus `_`). A vowel sign (`Mn` or `Mc`) or a
virama is neither, so `\\b`, the edge between `\\w` and not-`\\w`, falls *inside* an Indic word
after one, and is *missing* after a word that ends in one. `\\b` alone therefore both misses
whole words (a recall loss: an emergency not screened) and hits inside words (the contract's
"never starts or ends inside a word" broken).

Every string here is unchanged by the fold (checked below), so H1 sees exactly these.
"""

import re
import unicodedata

import pytest

from vectapilot_api.guards.normalize import fold

DEVANAGARI_SIGN_E = "े"  # े, `Mn`
DEVANAGARI_SIGN_II = "ी"  # ी, `Mc`
DEVANAGARI_SIGN_O = "ो"  # ो, `Mc`
DEVANAGARI_VIRAMA = "्"  # ्, `Mn`
TELUGU_SIGN_I = "ి"  # ి, `Mn`


SIGNS = (DEVANAGARI_SIGN_E, DEVANAGARI_SIGN_II, DEVANAGARI_SIGN_O, DEVANAGARI_VIRAMA, TELUGU_SIGN_I)


@pytest.mark.parametrize("sign", SIGNS)
def test_a_vowel_sign_or_virama_is_a_mark_and_not_a_word_character(sign: str) -> None:
    assert unicodedata.category(sign) in {"Mn", "Mc"}
    assert re.fullmatch(r"\w", sign) is None
    assert not sign.isalnum()


def test_a_consonant_is_a_word_character() -> None:
    assert re.fullmatch(r"\w", "द") is not None  # द
    assert re.fullmatch(r"\w", "న") is not None  # న


@pytest.mark.parametrize(
    ("term", "text"),
    [
        ("सीने", "सीने में दर्द"),  # ends in े: no `\b` between े and the space
        ("నొప్పి", "ఛాతీ నొప్పి"),  # ends in ి: the same in Telugu
    ],
)
def test_b_misses_a_whole_word_that_ends_in_a_sign(term: str, text: str) -> None:
    assert term in text.split()  # it *is* a whole word of the text
    assert re.search(rf"\b{re.escape(term)}\b", text) is None


def test_b_hits_inside_a_word_when_a_sign_follows() -> None:
    # "दर्दों" (pains) is one word, yet `\bदर्द\b` matches its first four characters: ो after
    # the last द is not `\w`, so `\b` sits between them.
    found = re.search(r"\bदर्द\b", "दर्दों")
    assert found is not None
    assert found.span() == (0, 4) and len("दर्दों") == 6  # it ends two characters into the word


def test_b_is_right_where_a_consonant_follows_or_precedes() -> None:
    assert re.search(r"\bदर्द\b", "दर्दनाक") is None  # a consonant (न) follows
    assert re.search(r"\bदर्द\b", "सरदर्द") is None  # a consonant (र) precedes
    assert re.search(r"\bदर्द\b", "सीने में दर्द") is not None


@pytest.mark.parametrize(
    "text",
    ["सीने में दर्द", "दर्दों", "दर्दनाक", "सरदर्द", "ఛాతీ నొప్పి", "నొప్పిగా", "కడుపునొప్పి"],
)
def test_the_fold_leaves_these_words_as_they_are(text: str) -> None:
    assert fold(text) == text
