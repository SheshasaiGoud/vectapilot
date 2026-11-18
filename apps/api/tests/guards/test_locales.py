"""M7 PR 3: the locale registry (`guards/locales.py`, G6, D1 (a)): G3's pattern rule over every
registered pattern, what each pattern finds, the ASCII view, and an adversarial input at the cap.

These are team code and pass now; H2 (`mask_pii`, which runs these patterns and resolves their
overlaps) is tested in `test_mask_pii.py`. Personal data comes from the red-team generator
(reserved ranges, `redteam/pii.py`) or is a digit string that belongs to nobody (all nines);
non-ASCII characters are built from code points (RUF001).
"""

import re
import time
from typing import Final

import pytest

from vectapilot_api.guards.caps import INPUT_CAPS, Surface
from vectapilot_api.guards.checksums import Checksum
from vectapilot_api.guards.locales import (
    DEFAULT_LOCALE_IDS,
    LOCALES,
    MASK_CAP,
    MAX_REPEAT,
    NFC_EXPANSION,
    PROBE,
    Assert,
    Chars,
    Item,
    Locale,
    Look,
    OneOf,
    PiiKind,
    PiiPattern,
    ascii_view,
    locales,
    pattern_problems,
)
from vectapilot_api.guards.normalize import clean
from vectapilot_api.redteam.pii import LABEL_OF, TEST_CARDS, PiiGenerator, PiiType, Placeholder

GEN: Final = PiiGenerator()
DIGIT: Final = Chars("[0-9]")
SEP: Final = Chars("[ -]", 0, 1)
PATTERNS: Final = [pattern for locale in LOCALES.values() for pattern in locale.patterns]


def value(kind: PiiType, n: int = 1) -> str:
    return GEN.value("locales", Placeholder(kind, n))


def spans(text: str) -> dict[str, tuple[int, int]]:
    """Every pattern id that finds something in `text`'s view, with its first span."""
    view = ascii_view(clean(text))
    found = {p.id: p.regex.search(view) for p in PATTERNS}
    return {pid: m.span() for pid, m in found.items() if m is not None}


# ── The registry ─────────────────────────────────────────────────────────────────────────────


def test_g6_names_nine_kinds_and_the_suites_labels_are_the_same_nine() -> None:
    assert [k.value for k in PiiKind] == [
        "PHONE",
        "EMAIL",
        "AADHAAR",
        "PAN",
        "CARD",
        "UPI",
        "IFSC",
        "PASSPORT",
        "ID_NUMBER",
    ]
    assert {label.value.upper() for label in LABEL_OF.values()} == {k.value for k in PiiKind}


def test_two_locales_india_first_for_every_business() -> None:
    assert list(LOCALES) == ["in", "generic"]
    assert DEFAULT_LOCALE_IDS == ("in", "generic")
    assert [loc.id for loc in locales()] == ["in", "generic"]
    assert [loc.id for loc in locales(["generic"])] == ["generic"]


@pytest.mark.parametrize("ids", [["xx"], ["in", "in"]])
def test_an_unknown_or_repeated_locale_id_is_refused(ids: list[str]) -> None:
    with pytest.raises(ValueError, match="unknown or repeated locale ids"):
        locales(ids)


def test_every_pattern_id_is_unique_and_names_its_locale() -> None:
    ids = [p.id for p in PATTERNS]
    assert len(ids) == len(set(ids))
    for locale in LOCALES.values():
        assert all(p.id.startswith(f"{locale.id}.") for p in locale.patterns)


def test_the_cap_is_a_cleaned_message_s_longest() -> None:
    assert MASK_CAP == NFC_EXPANSION * INPUT_CAPS[Surface.MESSAGE] == 6_000


def test_a_valid_message_still_fits_the_cap_after_clean() -> None:
    # G1 counts 2,000 on the raw text, and NFC can make it longer: a precomposed nukta letter
    # (U+095E, which Hindi keyboards type) becomes two code points, and U+FB2C, NFC's worst case,
    # becomes three. Both stay within the cap; three times is tight.
    nukta = chr(0x095E) * 10 + "a" * (INPUT_CAPS[Surface.MESSAGE] - 10)
    worst = chr(0xFB2C) * INPUT_CAPS[Surface.MESSAGE]

    assert len(clean(nukta)) == INPUT_CAPS[Surface.MESSAGE] + 10
    assert len(clean(worst)) == MASK_CAP


# ── G3's rule, over every registered pattern ─────────────────────────────────────────────────


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.id)
def test_every_registered_pattern_follows_g3s_rule(pattern: PiiPattern) -> None:
    assert pattern_problems(pattern.items, pattern.kind, pattern.checksum) == ()


_CLASSES_AND_ESCAPES: Final = re.compile(r"\\.|\[(?:\\.|[^\]\\])+\]")


@pytest.mark.parametrize("pattern", PATTERNS, ids=lambda p: p.id)
def test_no_compiled_pattern_can_nest_a_quantifier(pattern: PiiPattern) -> None:
    # The grammar's promise, checked on the regex text itself: with escapes and classes taken
    # out, no `*` or `+` is left, and the only quantified group is an optional one.
    bare = _CLASSES_AND_ESCAPES.sub("C", pattern.regex.pattern)
    assert "*" not in bare and "+" not in bare
    assert re.search(r"\)[{]", bare) is None


def test_nested_and_overlapping_quantifiers_are_refused() -> None:
    def problems(*items: Item) -> tuple[str, ...]:
        return pattern_problems(items, PiiKind.PHONE, None)

    assert problems(Chars("[0-9]", 1, 4), DIGIT) == ("items 0 and 1 overlap",)
    # Across an optional item, up to the next mandatory one.
    assert problems(Chars("[0-9]", 1, 4), SEP, DIGIT) == ("items 0 and 2 overlap",)
    assert problems(OneOf(("0",), optional=True), DIGIT) == ("items 0 and 1 overlap",)
    # Lookarounds are skipped; disjoint sets are fine.
    assert problems(Chars("[a-z]", 1, 4), Assert(Look.NOT_AFTER, "x"), DIGIT) == ()
    assert problems(SEP, DIGIT, SEP, DIGIT) == ()
    # A group of literals after a variable item: compared by its options' first characters,
    # and a mandatory group ends the look-ahead.
    assert problems(Chars("[0-9+]", 0, 1), OneOf(("+91", "0091")), DIGIT) == (
        "items 0 and 1 overlap",
    )
    assert problems(SEP, OneOf(("+91",)), Chars("[0-9 ]", 0, 1)) == ()
    # A quantifier inside a quantifier cannot be written: a repeat holds one character set.
    assert problems(Chars("(?:[0-9]?)", 1, 4)) == ("item 0 is not one character set",)
    assert problems(Chars("[0-9]+")) == ("item 0 is not one character set",)
    assert problems(Chars("ab")) == ("item 0 is not one character set",)
    assert problems(Assert(Look.NOT_BEFORE, "[0-9]{2}"), DIGIT) == (
        "item 0 is not one character set",
    )
    # A set that is not valid regex is reported, never compiled for the overlap check.
    assert problems(Chars("[0-9", 1, 4), DIGIT) == ("item 0 is not one character set",)


def test_every_repeat_is_bounded() -> None:
    def problems(*items: Item) -> tuple[str, ...]:
        return pattern_problems(items, PiiKind.ID_NUMBER, None)

    assert problems(Chars("[a-z]", 1, MAX_REPEAT), Chars("@")) == ()
    assert problems(Chars("[a-z]", 1, MAX_REPEAT + 1), Chars("@")) == (
        "item 0 repeats outside 0..64",
    )
    # A pattern of one item may reach the cap: with nothing after it, it never backtracks.
    assert problems(Chars("[0-9]", 20, MASK_CAP)) == ()
    assert problems(Chars("[0-9]", 20, MASK_CAP + 1)) == ("item 0 repeats outside 0..6000",)
    assert problems(Chars("[0-9]", 3, 2), Chars("@")) == ("item 0 repeats outside 0..64",)
    assert problems(Chars("[0-9]", 0, 0), Chars("@")) == ("item 0 repeats outside 0..64",)


def test_empty_matches_bad_options_and_a_misused_checksum_are_refused() -> None:
    assert pattern_problems((SEP,), PiiKind.PHONE, None) == (
        "the pattern can match the empty string",
    )
    assert pattern_problems((OneOf(("+91",), optional=True),), PiiKind.PHONE, None) == (
        "the pattern can match the empty string",
    )
    for options in (("+9", "+91"), ("+91", "+9"), ("0091", "0091")):  # either order
        assert pattern_problems((OneOf(options),), PiiKind.PHONE, None) == (
            "item 0 has an option that is a prefix of another",
        )
    empty: tuple[tuple[str, ...], ...] = ((), ("",))
    for no_text in empty:
        assert pattern_problems((OneOf(no_text), DIGIT), PiiKind.PHONE, None) == (
            "item 0 has an empty option",
        )
    assert pattern_problems((DIGIT,), PiiKind.ID_NUMBER, Checksum.LUHN) == (
        "a checksum's valid label is not ID_NUMBER",
    )


def test_a_pattern_or_locale_that_breaks_the_rules_cannot_be_built() -> None:
    with pytest.raises(ValueError, match=r"^pattern t\.bad: items 0 and 1 overlap$"):
        PiiPattern("t.bad", PiiKind.PHONE, (Chars("[0-9]", 1, 4), DIGIT))
    good = PiiPattern("t.good", PiiKind.PHONE, (DIGIT,))
    for patterns in ((), (good, good)):
        with pytest.raises(ValueError, match="at least one pattern, each id once"):
            Locale("t", patterns)


def test_the_probe_holds_every_script_the_patterns_meet() -> None:
    for code in (0x41, 0xE9, 0x0915, 0x0C15, 0x2013, 0xFF0B, 0x0A):
        assert chr(code) in PROBE


# ── What each pattern finds (team code: the patterns themselves) ─────────────────────────────

EXACT: Final[dict[PiiType, set[str]]] = {
    PiiType.EMAIL: {"generic.email"},
    PiiType.PHONE_IN: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_IN_0091: {"in.phone.country_code", "generic.digits_14"},
    PiiType.PHONE_IN_DASH: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_IN_SPACED: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_IN_DEVANAGARI: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_IN_TELUGU: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_IN_FULLWIDTH: {"in.phone.country_code", "generic.phone_12"},
    PiiType.PHONE_INTL: {"generic.phone_12"},
    PiiType.AADHAAR: {"in.aadhaar"},
    PiiType.AADHAAR_MISTYPED: {"in.aadhaar"},
    PiiType.PAN: {"in.pan"},
    PiiType.UPI: {"in.upi"},
    PiiType.IFSC: {"in.ifsc"},
    PiiType.PASSPORT: {"in.passport"},
}
"""By hand: which patterns find exactly the generated value (`+91 00000 ddddd` is 12 digits
after a `+`, so `generic.phone_12` finds it too; `0091 00000 ddddd` is 14 digits, so
`generic.digits_14` does; H2 breaks such ties by registry order, India first)."""


@pytest.mark.parametrize("kind", list(EXACT))
def test_the_patterns_that_find_exactly_each_generated_value(kind: PiiType) -> None:
    text = f"see {value(kind)}. ok"
    whole = (4, 4 + len(value(kind)))
    found = spans(text)

    assert {pid for pid, span in found.items() if span == whole} == EXACT[kind]
    # No pattern reaches past the value into the words around it, the full stop included.
    assert all(whole[0] <= start and end <= whole[1] for start, end in found.values())


@pytest.mark.parametrize("card", TEST_CARDS)
def test_every_published_test_card_is_one_digit_run(card: str) -> None:
    found = spans(f"card {card} ok")
    count = sum(c.isdigit() for c in card)
    assert found[f"generic.digits_{count}"] == (5, 5 + len(card))


@pytest.mark.parametrize(
    ("text", "pattern_id"),
    [
        ("call 9999999999 now", "in.phone.mobile"),
        ("call 99999 99999 now", "in.phone.mobile"),
        ("call 999-999-9999 now", "in.phone.mobile"),
        ("call 9999 999 999 now", "in.phone.mobile"),  # 4-3-3
        ("call 9999 999999 now", "in.phone.mobile"),  # 4-6
        ("call 99 99 99 99 99 now", "in.phone.mobile"),  # 2-2-2-2-2
        ("call 9 9 9 9 9 9 9 9 9 9 now", "in.phone.mobile"),  # per digit
        ("call 0 99999 99999 now", "in.phone.trunk"),
        ("call 099999-99999 now", "in.phone.trunk"),
        ("call 0999 999 9999 now", "in.phone.trunk"),
        ("call 040 2345 6789 now", "in.phone.trunk"),  # an STD code and a landline number
        ("call 080-23456789 now", "in.phone.trunk"),
        ("call 04023456789 now", "in.phone.trunk"),
    ],
)
def test_phones_without_a_country_code(text: str, pattern_id: str) -> None:
    found = spans(text)
    assert found[pattern_id] == (5, len(text) - 4)


NON_BREAKING_HYPHEN: Final = chr(0x2011)
EN_DASH: Final = chr(0x2013)
MINUS: Final = chr(0x2212)


@pytest.mark.parametrize("separator", [" ", "", "-", ".", NON_BREAKING_HYPHEN, EN_DASH])
def test_aadhaar_grouped_or_not(separator: str) -> None:
    written = value(PiiType.AADHAAR).replace(" ", separator)
    assert spans(f"id {written} ok")["in.aadhaar"] == (3, 3 + len(written))


@pytest.mark.parametrize("dash", [NON_BREAKING_HYPHEN, EN_DASH, MINUS])
def test_a_phone_written_with_unicode_dashes_is_found(dash: str) -> None:
    # ananya's PR 3 review: a word processor's non-breaking hyphen hid the whole number.
    written = value(PiiType.PHONE_IN_DASH).replace("-", dash)
    assert spans(f"call {written} now")["in.phone.country_code"] == (5, 5 + len(written))
    assert spans(f"call 99999{dash}99999 now")["in.phone.mobile"] == (5, 16)


def test_a_card_with_full_stops_is_one_digit_run() -> None:
    card = TEST_CARDS[0].replace(" ", ".")
    assert spans(f"card {card} ok")["generic.digits_16"] == (5, 5 + len(card))


def test_a_gstin_is_found_whole_and_the_pan_inside_it_is_not() -> None:
    # A GSTIN: a state code, its holder's PAN, an entity code, `Z`, a check character.
    gstin = f"00{value(PiiType.PAN)}1Z5"
    found = spans(f"GSTIN {gstin} ok")

    assert found == {"in.gstin": (6, 6 + len(gstin))}


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("call ++9999999999 now", {"in.phone.mobile": (7, 17), "generic.phone_10": (6, 17)}),
        ("++91 00000 30947", {"in.phone.country_code": (1, 16), "generic.phone_12": (1, 16)}),
        ("1+9999999999", {"in.phone.mobile": (2, 12)}),  # `+` after a digit: no `+` phone
        ("call +09999999999 now", {"in.phone.trunk": (6, 17), "generic.phone_11": (5, 17)}),
    ],
)
def test_a_plus_before_a_phone_never_hides_it(text: str, found: dict[str, tuple[int, int]]) -> None:
    # zara's PR 3 mutation check: with a `+` refused before the digits too, a doubled or glued
    # plus left each of these numbers visible. Each pattern refuses only a digit there.
    assert spans(text) == found


def test_the_longest_phone_and_card_runs_are_registered() -> None:
    assert spans("call +999999999999999 now")["generic.phone_15"] == (5, 21)
    assert spans("card " + "1" * 19 + " ok") == {"generic.digits_19": (5, 24)}


def test_a_run_of_exactly_20_digits_is_one_long_digits_span() -> None:
    assert spans("ref " + "1" * 20 + " ok") == {"generic.long_digits": (4, 24)}


def test_a_run_at_the_cap_is_one_long_digits_span() -> None:
    [long_digits] = [p for p in PATTERNS if p.id == "generic.long_digits"]
    assert [m.span() for m in long_digits.regex.finditer("1" * MASK_CAP)] == [(0, MASK_CAP)]


def test_the_checksums_are_wired_to_their_patterns() -> None:
    assert {p.id: p.checksum for p in PATTERNS if p.checksum is not None} == {
        "in.aadhaar": Checksum.VERHOEFF,
        **{f"generic.digits_{n}": Checksum.LUHN for n in range(13, 20)},
    }


def test_a_card_is_never_cut_into_an_aadhaar() -> None:
    assert spans(f"card {TEST_CARDS[1].replace(' ', '')} ok") == {"generic.digits_16": (5, 21)}


@pytest.mark.parametrize(
    ("text", "pattern_id"),
    [
        ("code ABCD1234E ok", "in.pan"),  # four letters, not five
        ("code ZZZZ1771467 ok", "in.ifsc"),  # the fifth character is not 0
        ("code Z86175471 ok", "in.passport"),  # eight digits, not seven
        ("mail xy@ab-cd.example ok", "in.upi"),  # a hyphen after the `@` part: an e-mail's
    ],
)
def test_near_misses_of_each_code_shape(text: str, pattern_id: str) -> None:
    assert pattern_id not in spans(text)


def test_a_one_character_upi_name_is_a_handle() -> None:
    # As an e-mail's local part: one character is enough (zara's PR 3 mutation check).
    assert spans("pay a@examplebank ok") == {"in.upi": (4, 17)}


def test_anywhere_finds_a_match_at_every_start_where_finditer_skips_one() -> None:
    # Five groups of four: `regex.finditer` finds the 16-digit run from the first group only
    # (the second group starts inside it); `anywhere` finds it from the first and the second.
    [digits_16] = [p for p in PATTERNS if p.id == "generic.digits_16"]
    view = ascii_view(clean("1111 1111 1111 1111 1111"))

    assert [m.span() for m in digits_16.regex.finditer(view)] == [(0, 19)]
    assert [m.span(1) for m in digits_16.anywhere.finditer(view)] == [(0, 19), (5, 24)]


@pytest.mark.parametrize(
    "text",
    [
        "Where is my order RT-0987654321?",  # l-011's phone-like order id: starts with 0
        "I paid INR 1,299 on 13/11/2026 for order RT-0415.",
        "Pickup on 2026-11-17 from 9:00 to 14:00, a 5 kg bag.",
        "Booking B-201, slot P-02, ticket TK-1001.",
        "1111 11 111111",  # 12 digits, but not grouped 4-4-4
        "11111111111",  # 11 digits: no shape of G6's
        "call 5999999999 now",  # 10 digits starting with 5: no mobile
        "sizes 6 7 8 9 10, PIN 500032, open 9:00-17:00",  # 6 digits at most in a run
        "",
    ],
)
def test_ordinary_support_text_matches_no_pattern(text: str) -> None:
    assert spans(text) == {}
    assert all(p.anywhere.search(ascii_view(clean(text))) is None for p in PATTERNS)


KNOWN_OVER_MASKS: Final = [
    ("dates 2026-11-17 2026-11-18 ok", {"generic.digits_14", "generic.digits_16"}),
    ("isbn 978-3-16-148410-0 ok", {"generic.digits_13"}),
    ("tracking AWB 1234567890123 ok", {"generic.digits_13"}),  # a 13-digit courier AWB
    ("uuid 123e4567-e89b-12d3-a456-426614174000", {"in.aadhaar", "generic.digits_15"}),
    ("Order B2026111 ok", {"in.passport"}),
    ("order1234a", {"in.pan"}),
    ("list 7 8 9 1 2 3 4 5 6 7 ok", {"in.phone.mobile"}),
    ("at 17.385044 78.486671 ok", {"generic.digits_14", "generic.digits_16"}),
]
"""Text that is not personal data but has a G6 shape (ananya's PR 3 review): masked, which only
tightens (G2), at a cost the guide names (§2.4). Pinned so a pattern change that alters them
shows up here."""


@pytest.mark.parametrize(("text", "pattern_ids"), KNOWN_OVER_MASKS)
def test_known_over_masks(text: str, pattern_ids: set[str]) -> None:
    assert set(spans(text)) == pattern_ids


def test_an_email_keeps_its_full_stop_and_a_upi_handle_has_no_dot() -> None:
    email, upi = value(PiiType.EMAIL), value(PiiType.UPI)
    assert spans(f"to {email}.")["generic.email"] == (3, 3 + len(email))
    found = spans(f"from {upi}.")
    assert found["in.upi"] == (5, 5 + len(upi)) and "generic.email" not in found


# ── The ASCII view ───────────────────────────────────────────────────────────────────────────


def test_the_view_maps_digits_fullwidth_forms_and_dashes_one_for_one() -> None:
    devanagari_91 = chr(0x096F) + chr(0x0967)
    telugu_91 = chr(0x0C6F) + chr(0x0C67)
    fullwidth = chr(0xFF0B) + chr(0xFF19) + chr(0xFF11) + chr(0xFF21) + chr(0xFF41)
    dashes = NON_BREAKING_HYPHEN + EN_DASH + chr(0x2014) + MINUS + chr(0xFF0D)
    text = clean(f"+{devanagari_91} +{telugu_91} {fullwidth} \u0928\u0902\u092c\u0930 {dashes} x")

    view = ascii_view(text)

    assert view == "+91 +91 +91Aa \u0928\u0902\u092c\u0930 ----- x"
    assert len(view) == len(text)
    assert ascii_view(clean(view)) == view


def test_the_view_maps_both_ends_of_the_fullwidth_block() -> None:
    assert ascii_view(clean(chr(0xFF01) + chr(0xFF5E))) == "!~"


# ── An adversarial input at the cap (G3: a bound, not a measurement) ─────────────────────────

ADVERSARIAL: Final = (
    "1 " * (MASK_CAP // 2),
    "9" * MASK_CAP,
    "a" * (MASK_CAP - 1) + "@",
    "a@" * (MASK_CAP // 2),
    "a." * (MASK_CAP // 2),
    "x@y." * (MASK_CAP // 4),
    "+1-" * (MASK_CAP // 3 - 1) + "-+1",
    "ZZZZ0" * (MASK_CAP // 5),
    "9-" * (MASK_CAP // 2),
    "9." * (MASK_CAP // 2),
    "-" * MASK_CAP,
    "0 " * (MASK_CAP // 2),
)
GENEROUS_SECONDS: Final = 2.0
"""A bound for every pattern over every input together, at every start position too
(`anywhere`), far above what linear work needs."""


def test_every_adversarial_input_is_at_the_cap() -> None:
    assert all(len(text) == MASK_CAP for text in ADVERSARIAL)


def test_every_pattern_over_every_adversarial_input_stays_within_a_generous_bound() -> None:
    started = time.perf_counter()
    found = 0
    for text in ADVERSARIAL:
        view = ascii_view(clean(text))
        for pattern in PATTERNS:
            found += sum(1 for _ in pattern.regex.finditer(view))
            found += sum(1 for _ in pattern.anywhere.finditer(view))
    elapsed = time.perf_counter() - started

    assert found > 0  # the inputs exercise the patterns (non-vacuity)
    assert elapsed < GENEROUS_SECONDS
