"""M7 PR 1: `clean` and `fold` (G5), the invisible-character hit rule and D3's script check.

Every invisible or look-alike character is written as an escape, so the test source shows exactly
what it tests. The text is synthetic.
"""

import time
import unicodedata

import pytest

from vectapilot_api.guards import normalize
from vectapilot_api.guards.caps import INPUT_CAPS, Surface
from vectapilot_api.guards.normalize import (
    APOSTROPHE_LIKE,
    CLEAN_DROPPED_SELECTORS,
    CONFUSABLES,
    DEFAULT_IGNORABLE,
    HINDI_VARIANTS,
    INVISIBLE_RUN_HIT,
    TAGS,
    VARIATION_SUPPLEMENT,
    VS15,
    VS16,
    ZWJ,
    ZWNJ,
    ZWSP,
    clean,
    count_invisible,
    fold,
    folded_away,
    uncovered_languages,
)
from vectapilot_api.tools import config as tools_config

TAG_RUN = "".join(map(chr, (0xE0001, 0xE0068, 0xE0069, 0xE007F)))
"""A tag sequence: BEGIN, `h`, `i`, CANCEL ("ASCII smuggling")."""
BIDI = ("\u202a", "\u202b", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069")
"""Embeddings, overrides and isolates, with their terminators where they have one."""
SOFT_HYPHEN = "\u00ad"
BOM = "\ufeff"
VS1_14 = tuple(map(chr, CLEAN_DROPPED_SELECTORS))
VS17_256_EDGES = (chr(VARIATION_SUPPLEMENT[0]), chr(VARIATION_SUPPLEMENT[-1]))
CHEST_PAIN_HI = "सीने में दर्द"
IGNORABLE = tuple(map(chr, sorted(DEFAULT_IGNORABLE)))
"""Hangul fillers (`Lo`), the grapheme joiner, Khmer inherent vowels and Mongolian selectors (`Mn`):
invisible, but not format characters."""


# ── clean ────────────────────────────────────────────────────────────────────────────────────


def test_is_invisible_is_tools_config_s_predicate_imported_not_restated() -> None:
    assert vars(normalize)["is_invisible"] is tools_config.is_invisible
    assert not hasattr(tools_config, "_invisible")


@pytest.mark.parametrize(
    "hidden",
    [TAG_RUN, ZWSP, *BIDI, *VS1_14, *VS17_256_EDGES, SOFT_HYPHEN, BOM, "\x00", "\x1b", "\ue000"],
)
def test_clean_drops_what_a_person_cannot_see(hidden: str) -> None:
    assert clean(f"ch{hidden}est pain") == "chest pain"


@pytest.mark.parametrize("kept", [ZWNJ, ZWJ, VS15, VS16])
def test_clean_keeps_the_joiners_and_the_presentation_selectors(kept: str) -> None:
    assert clean(f"a{kept}b") == f"a{kept}b"


def test_clean_drops_exactly_vs1_to_vs14() -> None:
    assert [chr(code) for code in CLEAN_DROPPED_SELECTORS] == [
        chr(c) for c in range(0xFE00, 0xFE0E)
    ]
    assert all(clean(f"x{s}") == "x" for s in VS1_14)


def test_clean_drops_everything_the_tools_predicate_refuses() -> None:
    # Sampled over the whole code space: `clean` can never keep a character `tools.json` refuses.
    for code in range(0, 0x110000, 7):
        character = chr(code)
        if tools_config.is_invisible(character):
            assert clean(f"a{character}b") == "ab", hex(code)


def test_clean_applies_nfc_and_collapses_whitespace_keeping_single_line_breaks() -> None:
    assert clean("  cafe\u0301 \t ok \n\n\n  next\u2028line  ") == "café ok\nnext\nline"
    assert unicodedata.is_normalized("NFC", clean("e\u0301"))


def test_clean_keeps_devanagari_and_telugu_text_as_written() -> None:
    assert clean(CHEST_PAIN_HI) == CHEST_PAIN_HI
    assert clean("క్\u200cష") == "క్\u200cష"


# ── fold ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hidden",
    [
        TAG_RUN,
        ZWSP,
        ZWNJ,
        ZWJ,
        VS15,
        VS16,
        *BIDI,
        *VS1_14,
        *VS17_256_EDGES,
        SOFT_HYPHEN,
        BOM,
        "\x01",
    ],
)
def test_fold_drops_every_invisible_character_joiners_and_selectors_included(hidden: str) -> None:
    assert fold(f"c{hidden}hest pain") == "chest pain"


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("Chést Päin", "chest pain"),  # Latin accents removed
        ("chest\u0301 pai\u0308n", "chest pain"),  # combining marks too
        ("c\u20ddhest", "chest"),  # an enclosing mark
        ("\u0301chest", "chest"),  # a mark without a letter before it
        ("\u0441hest pain", "chest pain"),  # Cyrillic small es
        ("\u0421HEST \u0420AIN", "chest pain"),  # Cyrillic capitals
        ("\u0397elp", "help"),  # Greek capital eta
        (
            "\uff23\uff28\uff25\uff33\uff34 \uff50\uff41\uff49\uff4e",
            "chest pain",
        ),  # fullwidth letters
        ("\U0001d41c\U0001d421\U0001d41e\U0001d42c\U0001d42d", "chest"),  # mathematical bold
        ("\u0131t \u0237am", "it jam"),  # dotless letters NFKD leaves alone
        ("\u01f0", "j"),  # a mark the case-fold adds
        ("chest-pain", "chest pain"),
        ("chest...pain!!", "chest pain"),
        ("«chest» (pain) — now?", "chest pain now"),
        ("chest+pain=bad $5", "chest pain bad 5"),  # symbols too
        ("  CHEST\n\tPAIN  ", "chest pain"),
        ("ß", "ss"),
    ],
)
def test_fold(text: str, folded: str) -> None:
    assert fold(text) == folded


def test_fold_keeps_devanagari_matras_and_signs() -> None:
    assert fold(CHEST_PAIN_HI) == CHEST_PAIN_HI
    # A non-spacing sign after a spacing vowel sign still belongs to the letter: `ं` stays.
    assert fold("नहीं") == "नहीं"
    assert fold("దగ్గు") == "దగ్గు"  # Telugu signs stay too


def test_hindi_chest_pain_folds_equal_with_and_without_zwnj_or_zwj() -> None:
    plain = fold(CHEST_PAIN_HI)
    assert fold("सीने में दर्\u200cद") == plain
    assert fold("सी\u200dने में द\u200cर्द") == plain


# ── fold: the bypasses from ananya's PR 1 review ─────────────────────────────────────────────


@pytest.mark.parametrize("ignorable", IGNORABLE, ids=[f"U+{ord(c):04X}" for c in IGNORABLE])
def test_a_default_ignorable_that_is_not_a_format_character_is_dropped(ignorable: str) -> None:
    # Inside a Latin word, between two words, and after a Devanagari letter (where a mark of the
    # Mongolian or Khmer block would otherwise stay as the letter's sign).
    assert fold(f"c{ignorable}hest pain") == "chest pain"
    assert fold(f"chest{ignorable}pain") == "chestpain"  # then H1's `\s*`, as for a ZWSP
    assert fold(f"द{ignorable}र्द") == fold("दर्द")
    assert clean(f"ch{ignorable}est pain") == "chest pain"


@pytest.mark.parametrize("ignorable", IGNORABLE, ids=[f"U+{ord(c):04X}" for c in IGNORABLE])
def test_a_run_of_three_default_ignorables_is_a_hit(ignorable: str) -> None:
    assert count_invisible(f"a{ignorable * 3}b").hit
    lone = count_invisible(f"a{ignorable}b")
    assert (lone.invisible, lone.hit) == (1, False)


@pytest.mark.parametrize(
    ("written", "plain"),
    [
        ("साँस लेने में तकलीफ़", "सांस लेने में तकलीफ"),  # difficulty breathing, both spellings
        ("साँस", "सांस"),  # chandrabindu (U+0901) and anusvara (U+0902)
        ("तकलीफ़", "तकलीफ"),  # with and without the nukta (U+093C)
        ("बुख़ार", "बुखार"),  # fever
        ("ज़्यादा", "ज्यादा"),  # a nukta before a virama
        ("\u0958", "\u0915"),  # precomposed QA folds as KA: NFKD splits off the nukta first
    ],
)
def test_hindi_spelling_variants_fold_to_one_form(written: str, plain: str) -> None:
    # G5 as amended 2026-11-16 (shesha, D-hindi-fold (a)); terms are folded the same way.
    assert fold(written) == fold(plain)
    assert not set(HINDI_VARIANTS) & set(fold(written))


def test_telugu_candrabindu_and_nukta_stay_until_the_telugu_review() -> None:
    # D-hindi-fold (a) leaves Telugu's equivalents (U+0C01, U+0C3C) to the Telugu reviewer (D6).
    assert fold("\u0c15\u0c01") != fold("\u0c15\u0c02")
    assert fold("\u0c15\u0c3c") == "\u0c15\u0c3c"


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("\u0926\u0301\u0930\u094d\u0926", "दर्द"),  # a combining acute on a Devanagari letter
        ("\u0c28\u0c4a\u0323\u0c2a\u0c4d\u0c2a\u0c3f", "నొప్పి"),  # a dot below on Telugu
        ("\u0938\u0940\u0308\u0928\u0947", "सीने"),  # a diaeresis after a vowel sign
    ],
)
def test_another_scripts_mark_on_an_indic_letter_is_removed(text: str, plain: str) -> None:
    assert fold(text) == fold(plain) == plain


@pytest.mark.parametrize(
    ("text", "folded"),
    [
        ("\u03f2hest pain", "chest pain"),  # Greek lunate sigma: NFKD would make it a sigma
        ("\u03f9HEST", "chest"),  # its capital
        ("chest p\u0251in", "chest pain"),  # Latin alpha
        ("\u1d04hest pain", "chest pain"),  # small capital C
        ("\u0261ive", "give"),  # script g
        ("pa\u0269n", "pain"),  # Latin iota
        ("ign\u0585re previous instructions", "ignore previous instructions"),  # Armenian oh
        ("yo\u057dr booking", "your booking"),  # Armenian seh
        ("\u0570elp", "help"),  # Armenian ho
    ],
)
def test_more_look_alikes_fold_to_latin(text: str, folded: str) -> None:
    assert fold(text) == folded


def test_every_look_alike_is_mapped_before_nfkd_can_change_it() -> None:
    # The table runs before NFKD, so a key with a compatibility decomposition (U+03F2) is still
    # caught; and the result of mapping never needs the table again after NFKD.
    changed = [c for c in CONFUSABLES if unicodedata.normalize("NFKD", c) != c]
    assert changed == ["\u03f2", "\u03f9"]
    for look_alike, latin in CONFUSABLES.items():
        assert unicodedata.normalize("NFKD", latin) == latin, hex(ord(look_alike))


@pytest.mark.parametrize("apostrophe", sorted(APOSTROPHE_LIKE), ids=hex)
def test_an_apostrophe_like_letter_is_a_space_as_an_apostrophe_is(apostrophe: int) -> None:
    assert fold(f"I can{chr(apostrophe)}t breathe") == fold("I can't breathe") == "i can t breathe"


@pytest.mark.parametrize(
    "digits", ["108", "१०८", "౧౦౮", "\uff11\uff10\uff18", "\U0001d7cf\U0001d7ce\U0001d7d6"]
)
def test_every_decimal_digit_folds_to_ascii(digits: str) -> None:
    # Devanagari, Telugu, fullwidth and mathematical bold digits: a term with "108" hits them.
    assert fold(f"call {digits} now") == "call 108 now"


def test_counts_must_come_from_the_raw_text_clean_removes_the_signal() -> None:
    # The precondition PR 6's wiring test checks against: count, then clean, then fold.
    assert count_invisible(f"hi{TAG_RUN}").hit
    after_clean = count_invisible(clean(f"hi{TAG_RUN}"))
    assert (after_clean.invisible, after_clean.tags, after_clean.hit) == (0, 0, False)


def test_fold_of_clean_is_fold_on_the_new_rules_too() -> None:
    text = f"\u03f2{IGNORABLE[0]}hést p\u0251in, साँस \u0926\u0301र्द १०८ can\u02bct {TAG_RUN}"
    assert fold(clean(text)) == fold(text)
    assert fold(fold(text)) == fold(text)


def test_fold_maps_every_confusable_to_the_letter_it_imitates() -> None:
    for look_alike, latin in CONFUSABLES.items():
        assert fold(look_alike) == latin.casefold(), hex(ord(look_alike))


def test_fold_is_idempotent_and_never_returns_an_invisible_character() -> None:
    text = f"\uff23{ZWJ}hést{TAG_RUN}-\u0440ain {CHEST_PAIN_HI}{ZWNJ}{VS16}"
    once = fold(text)
    assert fold(once) == once
    assert not any(folded_away(c) for c in once)


def test_fold_of_nothing_visible_is_empty() -> None:
    assert fold(f"{ZWSP}{TAG_RUN} --- {BOM}") == ""


# ── fold: pinned by PR 1's mutation check (each test kills a mutant that survived) ───────────

LOOK_ALIKES = (
    # Cyrillic, capital
    "\u0410\u0412\u0421\u0415\u041d\u0406\u0408\u041a\u041c\u041e\u0420\u0405\u0422\u0425\u04ae"
    "\u04c0\u051a\u051c"
    # Cyrillic, small
    "\u0430\u0441\u0501\u0435\u04bb\u0456\u0458\u04cf\u043e\u0440\u051b\u0455\u051d\u0445\u0443"
    # Greek, capital, then small
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
    "\u03b1\u03b3\u03b9\u03ba\u03bd\u03bf\u03c1\u03c5\u03c7"
    # Lunate sigma and its capital; dotless, IPA and small capitals; Armenian
    "\u03f2\u03f9"
    "\u0131\u0237\u0251\u0261\u0269\u1d04\u1d0f"
    "\u0570\u057d\u0585"
)
"""The table's keys written out, so a deleted entry fails here: iterating `CONFUSABLES` itself
cannot notice one missing."""


@pytest.mark.parametrize("hidden", ["\ue000", "\U000f0000", "\u0378"], ids=["Co", "Co-15", "Cn"])
def test_fold_and_the_run_rule_drop_private_use_and_unassigned(hidden: str) -> None:
    # Mutants n02 and n03: `Co` or `Cn` missing from the hidden categories.
    assert fold(f"c{hidden}hest pain") == "chest pain"
    assert count_invisible(f"a{hidden * 3}b").hit


def test_the_confusables_table_is_pinned_independently_of_itself() -> None:
    # Mutant n26: an entry deleted (U+0455, Cyrillic dze, for `s`).
    assert "".join(CONFUSABLES) == LOOK_ALIKES
    assert fold(LOOK_ALIKES) == (
        "abcehijkmopstxyiqw"  # Cyrillic, capital
        "acdehijlopqswxy"  # Cyrillic, small
        "abezhikmnoptyx"  # Greek, capital
        "ayikvopux"  # Greek, small
        "cc"  # lunate sigma and its capital
        "ijagico"  # dotless, IPA, small capitals
        "huo"  # Armenian
    )


def test_a_look_alike_under_a_precomposed_accent_folds_to_latin() -> None:
    # Mutant n10: no NFKD before the table's second pass, so the accent hides the look-alike.
    assert fold("\u0386") == "a"  # Greek capital alpha with tonos
    assert fold("\u0450") == "e"  # Cyrillic small ie with grave


def test_a_capital_that_case_folds_into_the_table_is_mapped() -> None:
    # Mutant n12: no table pass after the case-fold (the module's own example, U+0500).
    assert fold("\u0500") == "d"  # Cyrillic capital komi de
    assert fold("\u0423") == "y"  # Cyrillic capital u


def test_fold_recomposes_kept_indic_signs() -> None:
    # Mutant n18: no final NFC. Telugu's ai sign decomposes under NFKD (U+0C46 + U+0C56);
    # Devanagari's nukta letters no longer show it, since the nukta is folded away.
    assert fold("\u0c15\u0c48") == "\u0c15\u0c48"  # Telugu ka + ai
    assert unicodedata.is_normalized("NFC", fold("\u0c15\u0c48 \u0929"))


def test_fold_removes_marks_of_cyrillic_and_greek_letters_outside_the_table() -> None:
    # A pin, not a kill: since ananya's a3 every combining mark is another script's mark on a
    # Cyrillic or Greek letter, so mutants n22 and n23 (`_STRIPPED_SCRIPTS` narrowed) are
    # equivalent. This holds the behaviour whichever rule removes the mark.
    assert fold("\u0439") == "\u0438"  # Cyrillic short i -> i
    assert fold("\u03ad") == "\u03b5"  # Greek epsilon with tonos -> epsilon


# ── the invisible-character hit (an injection signal) ────────────────────────────────────────


def test_a_tag_run_is_a_hit() -> None:
    counts = count_invisible(f"hello{TAG_RUN}")
    assert counts.hit
    assert (counts.tags, counts.invisible, counts.longest_run) == (4, 4, 4)


def test_a_single_tag_character_or_vs17_to_256_is_a_hit() -> None:
    assert count_invisible(f"a{chr(TAGS[0x41])}b").hit
    assert all(count_invisible(f"a{s}b").hit for s in VS17_256_EDGES)
    assert count_invisible(f"a{VS17_256_EDGES[0]}b").supplement_selectors == 1


def test_vs17_itself_is_a_supplement_selector_and_a_hit() -> None:
    # Mutant n39: the supplement range starting one late. Written out, not taken from the range.
    counts = count_invisible("a\U000e0100b")
    assert counts.supplement_selectors == 1
    assert counts.hit


@pytest.mark.parametrize("lone", [SOFT_HYPHEN, BOM, ZWSP, ZWNJ, ZWJ, VS16])
def test_a_lone_invisible_character_is_counted_but_is_not_a_hit(lone: str) -> None:
    counts = count_invisible(f"pdf{lone}text, and another{lone}one")
    assert counts.invisible == 2
    assert counts.longest_run == 1
    assert not counts.hit


def test_a_run_of_three_invisible_characters_is_a_hit_and_two_is_not() -> None:
    assert INVISIBLE_RUN_HIT == 3
    assert count_invisible(f"a{ZWSP}{SOFT_HYPHEN}{ZWJ}b").hit
    assert not count_invisible(f"a{ZWSP}{SOFT_HYPHEN}b{ZWJ}c").hit
    assert count_invisible(f"a{ZWNJ * 3}b").hit  # joiners count towards a run


def test_ordinary_emoji_sequences_are_not_a_hit() -> None:
    # Heart on fire, rainbow flag, a family: VS16 and ZWJ in runs of at most two.
    for emoji in (
        "\u2764\ufe0f\u200d\U0001f525",
        "\U0001f3f3\ufe0f\u200d\U0001f308",
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",
    ):
        assert not count_invisible(emoji).hit


def test_a_subdivision_flag_is_a_hit_a_known_false_positive() -> None:
    # England's flag: a black flag plus the tags "gbeng" and CANCEL. Decided 2026-11-16: any tag
    # character stays a hit (a hit only tightens); PR 2 lists it as a known false positive for
    # the over-escalation count.
    england = "\U0001f3f4" + "".join(chr(0xE0000 + ord(c)) for c in "gbeng") + "\U000e007f"
    counts = count_invisible(f"Order shipped to {england}?")
    assert counts.hit and counts.tags == 6


def test_counts_of_visible_text_are_zero() -> None:
    counts = count_invisible(f"plain text {CHEST_PAIN_HI}")
    assert (counts.invisible, counts.tags, counts.supplement_selectors, counts.longest_run) == (
        0,
        0,
        0,
        0,
    )
    assert not counts.hit


# ── D3 (i): uncovered languages ──────────────────────────────────────────────────────────────

SCRIPTS = {"te": ((0x0C00, 0x0C7F),), "hi": ((0x0900, 0x097F),)}


@pytest.mark.parametrize("code", [0x0C00, 0x0C15, 0x0C4D, 0x0C7F])
def test_a_telugu_block_character_names_telugu_when_it_is_not_covered(code: int) -> None:
    assert uncovered_languages(f"my pet {chr(code)} ok", ("te",), SCRIPTS) == {"te"}


@pytest.mark.parametrize("text", ["my dog is limping", CHEST_PAIN_HI, "\u0bff\u0c80", ""])
def test_text_without_a_telugu_block_character_is_covered(text: str) -> None:
    assert uncovered_languages(text, ("te",), SCRIPTS) == frozenset()


def test_romanised_telugu_is_not_detected_a_known_gap() -> None:
    assert uncovered_languages("naa kukka ki debba tagilindi", ("te",), SCRIPTS) == frozenset()


def test_a_covered_language_is_never_named() -> None:
    assert uncovered_languages("దగ్గు", (), SCRIPTS) == frozenset()
    assert uncovered_languages(f"దగ్గు {CHEST_PAIN_HI}", ("te", "hi"), SCRIPTS) == {"te", "hi"}


def test_a_not_covered_language_without_a_script_range_is_a_key_error() -> None:
    with pytest.raises(KeyError):
        uncovered_languages("text", ("ta",), SCRIPTS)


# ── G3: linear in the text, at the largest cap ───────────────────────────────────────────────


def test_the_normaliser_stays_fast_on_adversarial_text_at_the_largest_cap() -> None:
    cap = max(INPUT_CAPS.values())
    assert cap == INPUT_CAPS[Surface.OBSERVATION]
    # Marks piled on one base, invisible runs and punctuation: the worst shapes for each step.
    unit = "a\u0301\u0302\u0303" + ZWJ * 3 + TAG_RUN + "-\u0441" + "ीं"
    text = (unit * (cap // len(unit) + 1))[:cap]
    started = time.perf_counter()
    clean(text)
    fold(text)
    count_invisible(text)
    uncovered_languages(text, ("te", "hi"), SCRIPTS)
    # A generous bound, not a measurement: a quadratic step would take minutes here.
    assert time.perf_counter() - started < 5.0
