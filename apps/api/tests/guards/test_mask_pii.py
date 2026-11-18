"""H2's contract, executable: `guards.pii.mask_pii` (`[HAND-WRITE]`, D5 (b): written by Claude at
shesha's request on `m7/pii-masking`; guide `docs/learning/m7-masking.md`).

**One switch.** Every test here runs H2, so the module carries one `pytestmark`: a strict xfail on
`NotImplementedError`. While H2 is the stub, each shows `xfailed`. Once H2 is written, a test it
passes shows `XPASS(strict)` (a failure on purpose) and a test it fails shows `FAILED` with the
assert's diff. When every one is `XPASS(strict)`, delete the `pytestmark` statement below.

**Expected values are derived by hand** in the comments, never by running a masker. Personal data
comes only from the red-team generator's types (reserved ranges: `+91 00000`, an Aadhaar starting
with 0 or 1, `.example`, `ZZZZ`, `Z`; `redteam/pii.py`), from published checksum test vectors (the
payment processors' test cards, Rosetta Code's Luhn vectors), or is a digit string that belongs
to nobody (all ones, all nines). The expected text never holds a value: it is the template with
each value written as its placeholder. Texts go through `clean` first, as a guarded route hands
them over. Non-ASCII digits are built from code points (RUF001).

The contract (`mask_pii`'s docstring, the plan's "H2"): 1. an empty locale list is a ValueError,
over the cap an OverCap, neither naming any text; 2. every pattern over the ASCII view, a
candidate at every start position, spans with their separators; 3. overlapping candidates join
into one span, labelled by its best candidate: longest, then leftmost, then the earlier pattern;
4. shape decides, the checksum only picks the label (valid: the pattern's kind, invalid:
`ID_NUMBER`); 5. placeholders in, everything else kept, counts per label; 6. passes repeat until
one finds nothing (at most `MAX_PASSES` that replace), so the result is idempotent.
"""

import logging
import time
from typing import Final

import pytest

from vectapilot_api.guards.caps import INPUT_CAPS, OverCap, Surface
from vectapilot_api.guards.checksums import decimal_digits
from vectapilot_api.guards.locales import (
    MASK_CAP,
    Assert,
    Chars,
    Locale,
    Look,
    PiiKind,
    PiiPattern,
    locales,
)
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.pii import Masked, MaskingIncomplete, mask_pii
from vectapilot_api.redteam.pii import (
    TEST_CARDS,
    PiiGenerator,
    PiiType,
    Placeholder,
    leak_forms,
)

pytestmark = pytest.mark.xfail(
    strict=True, raises=NotImplementedError, reason="H2 (mask_pii) is written on m7/pii-masking"
)

GEN: Final = PiiGenerator()
DEFAULT: Final = locales()
DEVANAGARI_ZERO: Final = 0x0966
TELUGU_ZERO: Final = 0x0C66
FULLWIDTH_ZERO: Final = 0xFF10
NON_BREAKING_HYPHEN: Final = chr(0x2011)
EN_DASH: Final = chr(0x2013)

PHONE, EMAIL, AADHAAR, CARD, ID_NUMBER = (
    PiiKind.PHONE,
    PiiKind.EMAIL,
    PiiKind.AADHAAR,
    PiiKind.CARD,
    PiiKind.ID_NUMBER,
)

KIND_OF: Final[dict[PiiType, PiiKind]] = {
    PiiType.EMAIL: EMAIL,
    PiiType.PHONE_IN: PHONE,
    PiiType.PHONE_IN_0091: PHONE,  # ties with a 14-digit run; India's pattern is first (§3)
    PiiType.PHONE_IN_DASH: PHONE,
    PiiType.PHONE_IN_SPACED: PHONE,
    PiiType.PHONE_IN_DEVANAGARI: PHONE,
    PiiType.PHONE_IN_TELUGU: PHONE,
    PiiType.PHONE_IN_FULLWIDTH: PHONE,
    PiiType.PHONE_INTL: PHONE,
    PiiType.AADHAAR: AADHAAR,  # Verhoeff-valid by construction
    PiiType.AADHAAR_MISTYPED: ID_NUMBER,  # the check digit is wrong by construction
    PiiType.PAN: PiiKind.PAN,
    PiiType.CARD: CARD,  # a published test card: Luhn-valid
    PiiType.UPI: PiiKind.UPI,
    PiiType.IFSC: PiiKind.IFSC,
    PiiType.PASSPORT: PiiKind.PASSPORT,
}
"""By hand, from G6 and the generator's docstring (and the suite's `LABEL_OF`, which agrees:
`test_locales.py`)."""


def value(kind: PiiType, n: int = 1) -> str:
    return GEN.value("h2", Placeholder(kind, n))


def mask(text: str, chosen: tuple[Locale, ...] = DEFAULT) -> Masked:
    return mask_pii(clean(text), chosen)


def p(kind: PiiKind) -> str:
    return f"[{kind.value}]"


def in_script(text: str, zero: int) -> str:
    return "".join(chr(zero + int(c)) if c.isascii() and c.isdigit() else c for c in text)


# ── 1. Every format of category E ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", list(PiiType))
def test_each_format_becomes_its_placeholder(kind: PiiType) -> None:
    masked = mask(f"Reach me at {value(kind)} please")

    assert masked.text == f"Reach me at {p(KIND_OF[kind])} please"
    assert masked.counts == {KIND_OF[kind]: 1}


@pytest.mark.parametrize("kind", list(PiiType))
def test_a_full_stop_or_comma_after_a_value_stays(kind: PiiType) -> None:
    # The span ends at the value: an e-mail address ends in a letter, digit or hyphen, a UPI
    # handle may sit before a full stop, a code is a whole word.
    masked = mask(f"It is {value(kind)}. And {value(kind, 2)}, too")

    assert masked.text == f"It is {p(KIND_OF[kind])}. And {p(KIND_OF[kind])}, too"
    assert masked.counts == {KIND_OF[kind]: 2}


def test_hindi_around_a_devanagari_phone_is_kept_exactly() -> None:
    # e-014's shape: "my number is ... , call before delivery".
    before = "मेरा नंबर"
    after = "है, फ़ोन कर देना।"
    masked = mask(f"{before} {value(PiiType.PHONE_IN_DEVANAGARI)} {after}")

    assert masked.text == f"{before} [PHONE] {after}"


# ── 2. Shape decides; the checksum only picks the label (G6) ─────────────────────────────────


@pytest.mark.parametrize("kind", [PiiType.AADHAAR, PiiType.AADHAAR_MISTYPED])
@pytest.mark.parametrize("separator", [" ", "", "-", ".", NON_BREAKING_HYPHEN, EN_DASH])
def test_an_aadhaar_shaped_run_is_masked_in_every_grouping(kind: PiiType, separator: str) -> None:
    # 12 digits grouped 4-4-4 (spaces, hyphens, full stops, Unicode dashes: the view makes each
    # dash a hyphen) or not grouped: valid -> [AADHAAR]; the mistyped one -> [ID_NUMBER], masked
    # all the same.
    written = value(kind).replace(" ", separator)
    assert mask(f"ID {written} ok").text == f"ID {p(KIND_OF[kind])} ok"


@pytest.mark.parametrize("count", range(13, 20))
def test_any_13_to_19_digit_run_is_masked_even_when_luhn_fails(count: int) -> None:
    # n ones, from the right: ceil(n/2) ones kept, floor(n/2) doubled to 2. 13 -> 7 + 12 = 19,
    # 14 -> 21, 15 -> 22, 16 -> 24, 17 -> 25, 18 -> 27, 19 -> 28: never a multiple of 10, so
    # Luhn fails every one and each is [ID_NUMBER], never left in the text.
    masked = mask(f"ref {'1' * count} ok")

    assert masked.text == "ref [ID_NUMBER] ok"
    assert masked.counts == {ID_NUMBER: 1}


@pytest.mark.parametrize("card", TEST_CARDS)
def test_a_published_test_card_is_a_card_and_one_wrong_digit_an_id_number(card: str) -> None:
    # Luhn-valid as published -> [CARD]. The last digit is never doubled, so adding 1 to it
    # (9 becomes 0) moves the sum by +1 or -9: no longer a multiple of 10 -> [ID_NUMBER], still
    # masked.
    wrong = card[:-1] + str((int(card[-1]) + 1) % 10)

    assert mask(f"card {card} ok").text == "card [CARD] ok"
    assert mask(f"card {wrong} ok").text == "card [ID_NUMBER] ok"


@pytest.mark.parametrize(
    ("number", "kind"),
    [("1234567812345670", CARD), ("1234567812345678", ID_NUMBER)],  # Rosetta Code's Luhn pair
)
def test_rosetta_codes_16_digit_pair(number: str, kind: PiiKind) -> None:
    assert mask(f"no. {number}").text == f"no. {p(kind)}"


def test_a_checksum_valid_number_of_the_wrong_shape_is_not_masked() -> None:
    # 79927398713 passes Luhn (Wikipedia's example), but 11 digits starting with 7 are no shape
    # of G6's: not 10 (a mobile), not 12 (Aadhaar), not 13-19. Shape decides, both ways.
    masked = mask("code 79927398713 ok")

    assert masked.text == "code 79927398713 ok"
    assert masked.counts == {}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ref 11111111111 ok", "ref 11111111111 ok"),  # 11 digits, starting with 1
        ("ref 1111 11 111111 ok", "ref 1111 11 111111 ok"),  # 12, but grouped 4-2-6
        ("ref 11111111111111111111 ok", "ref [ID_NUMBER] ok"),  # 20 digits: one longer run
    ],
)
def test_the_edges_of_the_digit_shapes(text: str, expected: str) -> None:
    assert mask(text).text == expected


@pytest.mark.parametrize(
    ("written", "kind"),
    [
        (in_script(TEST_CARDS[0], DEVANAGARI_ZERO), CARD),
        (in_script(value(PiiType.AADHAAR), TELUGU_ZERO), AADHAAR),
        (in_script(value(PiiType.AADHAAR_MISTYPED), FULLWIDTH_ZERO), ID_NUMBER),
    ],
)
def test_the_checksum_reads_unicode_digits(written: str, kind: PiiKind) -> None:
    # The view gives the patterns ASCII digits; the label comes from the span's own digits.
    assert mask(f"no. {written} ok").text == f"no. {p(kind)} ok"


@pytest.mark.parametrize(
    ("written", "kind"),
    [
        ("9999999999", PHONE),  # a bare 10-digit mobile (nines: nobody's number)
        ("99999 99999", PHONE),  # 5-5
        ("999-999-9999", PHONE),  # 3-3-4
        ("0 99999 99999", PHONE),  # after a trunk 0: joins the bare 5-5 inside it
        (in_script(value(PiiType.AADHAAR), DEVANAGARI_ZERO), AADHAAR),
        (TEST_CARDS[0].replace(" ", ""), CARD),  # a card without spaces
    ],
    ids=["mobile", "mobile-5-5", "mobile-3-3-4", "trunk", "aadhaar-devanagari", "card-unspaced"],
)
def test_the_shapes_the_red_team_readme_leaves_to_these_tests(written: str, kind: PiiKind) -> None:
    # evals/datasets/redteam/v0.1/README.md, "Not in E ... covered by H2's unit tests".
    assert mask(f"call {written} now").text == f"call {p(kind)} now"


@pytest.mark.parametrize(
    ("written", "kind"),
    [
        (value(PiiType.PHONE_IN_DASH).replace("-", NON_BREAKING_HYPHEN), PHONE),
        (value(PiiType.PHONE_IN_DASH).replace("-", EN_DASH), PHONE),
        ("9999" + EN_DASH + "999" + EN_DASH + "999", PHONE),  # a bare 4-3-3 mobile
        ("040 2345 6789", PHONE),  # an STD code and a landline number
        (TEST_CARDS[0].replace(" ", "."), CARD),  # the 12-digit Aadhaar shape inside joins it
        (f"00{value(PiiType.PAN)}1Z5", PiiKind.PAN),  # a GSTIN holds its holder's PAN
    ],
    ids=["nb-hyphen", "en-dash", "mobile-4-3-3", "landline", "card-dotted", "gstin"],
)
def test_the_shapes_ananyas_review_added(written: str, kind: PiiKind) -> None:
    assert mask(f"no. {written} ok").text == f"no. {p(kind)} ok"


# ── 3. Overlaps: candidates join; longest, then leftmost, then the earlier pattern ───────────


def test_a_card_wins_over_the_aadhaar_shape_inside_it() -> None:
    # "4111 1111 1111 1111": `in.aadhaar` finds its first 12 digits (14 characters), the
    # 16-digit run all 19 characters: the longer one wins, the shorter is dropped.
    masked = mask("card 4111 1111 1111 1111 ok")

    assert masked.text == "card [CARD] ok"
    assert masked.counts == {CARD: 1}


def test_an_email_wins_over_the_upi_handle_inside_it() -> None:
    # `in.upi` finds `customerNN@mail` (no dot after the @), `generic.email` the whole address.
    masked = mask(f"mail {value(PiiType.EMAIL)} ok")

    assert masked.text == "mail [EMAIL] ok"
    assert masked.counts == {EMAIL: 1}


def test_a_per_digit_spaced_phone_is_one_span() -> None:
    # `generic.phone_8` to `_11` find shorter prefixes of `+91 0 0 0 0 0 d d d d d`; they join
    # the 12-digit spans of `in.phone.country_code` and `generic.phone_12` (the same 23
    # characters: India's pattern is earlier) -> one [PHONE].
    assert mask(f"call {value(PiiType.PHONE_IN_SPACED)} ok").text == "call [PHONE] ok"


@pytest.mark.parametrize(
    ("ids", "expected"),
    [(("in", "generic"), "[PHONE]"), (("generic", "in"), "[CARD]")],
)
def test_the_same_span_goes_to_the_earlier_locale(ids: tuple[str, ...], expected: str) -> None:
    # "0091 00000 00000" is `in.phone.country_code` and `generic.digits_14` over the same 16
    # characters. With India first: [PHONE]. With generic first: Luhn on 00910000000000, from
    # the right: ten zeros, then 1 (kept) and 9 (doubled: 18 -> 9): 10, valid -> [CARD].
    assert mask("no. 0091 00000 00000", locales(ids)).text == f"no. {expected}"


AB: Final = PiiPattern("t.ab", PHONE, (Chars("a"), Chars("b")))
BC: Final = PiiPattern("t.bc", EMAIL, (Chars("b"), Chars("c")))
BCD: Final = PiiPattern("t.bcd", CARD, (Chars("b"), Chars("c"), Chars("d")))
BC_TOO: Final = PiiPattern("t.bc_too", PiiKind.PAN, (Chars("b"), Chars("c")))
AA: Final = PiiPattern("t.aa", PHONE, (Chars("a"), Chars("a")))
AAB: Final = PiiPattern("t.aab", PiiKind.UPI, (Chars("a"), Chars("a"), Chars("b")))
BB: Final = PiiPattern("t.bb", PiiKind.PASSPORT, (Chars("b"), Chars("b")))
BCC: Final = PiiPattern("t.bcc", PiiKind.IFSC, (Chars("b"), Chars("c"), Chars("c")))
"""TEST-ONLY patterns (plain letters, no personal data) that pin the overlap rule alone."""


@pytest.mark.parametrize(
    ("patterns", "text", "expected", "counts"),
    [
        # "ab" (0-2) and "bc" (1-3) share the "b": one span 0-3. Equal length: the leftmost
        # candidate, "ab", gives the label.
        ((BC, AB), "abc", "[PHONE]", {PHONE: 1}),
        # "ab" (0-2) and "bcd" (1-4): one span 0-4; the longest, "bcd", gives the label,
        # whatever its position or order.
        ((AB, BCD), "abcd", "[CARD]", {CARD: 1}),
        # The same span "bc" from two patterns: the earlier pattern's label.
        ((BC_TOO, BC), "xbcx", "x[PAN]x", {PiiKind.PAN: 1}),
        ((BC, BC_TOO), "xbcx", "x[EMAIL]x", {EMAIL: 1}),
        # Spans that do not overlap are all kept, each counted; touching is not overlapping.
        ((AB,), "ab ab", "[PHONE] [PHONE]", {PHONE: 2}),
        ((AB,), "abab", "[PHONE][PHONE]", {PHONE: 2}),
        # Every start position: "aa" at 0 and at 1 (`finditer` alone finds only 0-2 and would
        # leave the last "a"): one span 0-3.
        ((AA,), "aaa", "[PHONE]", {PHONE: 1}),
        # "aab" (0-3) and "bcc" (3-6) only touch; "bb" (2-4) shares a character with each, so all
        # three join into 0-6. The longest are "aab" and "bcc"; the leftmost, "aab", gives the
        # label, though "bb" is the earlier pattern.
        ((BB, BCC, AAB), "aabbcc", "[UPI]", {PiiKind.UPI: 1}),
    ],
)
def test_the_overlap_order_on_test_only_patterns(
    patterns: tuple[PiiPattern, ...], text: str, expected: str, counts: dict[PiiKind, int]
) -> None:
    masked = mask(text, (Locale("t", patterns),))

    assert masked.text == expected
    assert masked.counts == counts


def test_a_grouped_run_of_20_is_one_span() -> None:
    # "1111 1111 1111 1111 1111": no pattern spans five groups (`generic.long_digits` needs 20
    # digits in a row). `generic.digits_16` starts at the first and at the second group (0-19,
    # 5-24; a 13-15 or 17-19 digit window never ends at a group's end), the Aadhaar shape at the
    # first three (0-14, 5-19, 10-24): all join into 0-24. The best candidates are the two
    # 19-character runs; the leftmost (0-19) gives the label: Luhn on sixteen ones is 8 + 16 =
    # 24, not a multiple of 10 -> one [ID_NUMBER], no group left.
    masked = mask("1111 1111 1111 1111 1111")

    assert masked.text == "[ID_NUMBER]"
    assert masked.counts == {ID_NUMBER: 1}


@pytest.mark.parametrize(
    ("text", "expected", "kind"),
    [
        # A card, its expiry and its CVV, one space apart: 23 digits in groups 4-4-4-4-4-3.
        # Candidates: `digits_16` from group 1 (0-19) and group 2 (5-24), `digits_19` from group
        # 2 (5-28; from group 1 the 19th digit is inside "1225"), `digits_15` from group 3
        # (10-28), the Aadhaar shape from groups 1-3: all join into the whole run. The longest
        # is `digits_19` (23 characters): Luhn on 1111111111111225123, from the right,
        # 3 + 4 + 1 + 1 (5 doubled: 10 -> 1) + 2 + 4 + seven ones + six doubled ones (12) = 34,
        # not a multiple of 10 -> [ID_NUMBER]. With "dropped whole" its first group stayed.
        ("card 4111 1111 1111 1111 1225 123 ok", "card [ID_NUMBER] ok", ID_NUMBER),
        # A phone next to an Aadhaar-shaped run (all nines and ones: nobody's numbers; offsets
        # in the run): the phone (0-15), `digits_16` from "91" (1-20), `digits_14` and
        # `digits_18` from the first "99999" (4-20, 4-25), `digits_13` and `digits_17` from the
        # second (10-25, 10-30), the bare mobile (4-15) and the Aadhaar shape (16-30) all join.
        # The longest is `digits_18` (21 characters): Luhn on ten nines then eight ones, from the
        # right: ones 4 + 4 x 2 = 12, nines 5 x 9 + 5 x 9 (18 -> 9) = 90: 102 -> [ID_NUMBER].
        ("call +91 99999 99999 1111 1111 1111", "call [ID_NUMBER]", ID_NUMBER),
        # An Aadhaar-shaped run, then a bare mobile: the Aadhaar shape (0-14), the mobile
        # (15-25), `digits_18` from group 2 (5-25) and `digits_14` from group 3 (10-25) join.
        # The longest is `digits_18` (20 characters): ten nines then eight ones read from the
        # right as above: 102 -> [ID_NUMBER].
        ("aadhaar 1111 1111 1111 9999999999", "aadhaar [ID_NUMBER]", ID_NUMBER),
        # A test card, then an Aadhaar-shaped run: `digits_16` from groups 1 to 4 (each 19
        # characters) and the Aadhaar shape from groups 1 to 5 join into the whole run. The
        # leftmost of the longest is the card itself, Luhn-valid as published -> [CARD]. With
        # `finditer` alone the last group of the Aadhaar stayed.
        ("card 4111 1111 1111 1111 1111 1111 1111 ok", "card [CARD] ok", CARD),
    ],
    ids=["card-expiry-cvv", "phone-then-aadhaar", "aadhaar-then-mobile", "card-then-aadhaar"],
)
def test_numbers_one_space_apart_leave_no_digit_visible(
    text: str, expected: str, kind: PiiKind
) -> None:
    masked = mask(text)

    assert masked.text == expected
    assert masked.counts == {kind: 1}


# ── 4. Counts, and everything else kept ──────────────────────────────────────────────────────


def test_several_kinds_in_one_message() -> None:
    # e-017's shape.
    card, email, phone = value(PiiType.CARD), value(PiiType.EMAIL), value(PiiType.PHONE_IN)
    masked = mask(f"Refund for order RT-0412: card {card}, e-mail {email}, phone {phone}.")

    assert masked.text == "Refund for order RT-0412: card [CARD], e-mail [EMAIL], phone [PHONE]."
    assert masked.counts == {CARD: 1, EMAIL: 1, PHONE: 1}
    assert masked.total == 3


@pytest.mark.parametrize(
    "text",
    [
        "Where is my order RT-0987654321?",
        "I paid INR 1,299 on 13/11/2026 for order RT-0415.",
        "Pickup on 2026-11-17 from 9:00 to 14:00, a 5 kg bag.",
        "",
    ],
)
def test_text_without_personal_data_comes_back_unchanged(text: str) -> None:
    masked = mask(text)

    assert masked.text == clean(text)
    assert masked.counts == {}


# ── 5. Passes, idempotent and non-reversible ─────────────────────────────────────────────────


def test_a_code_glued_to_a_number_is_masked_in_a_second_pass() -> None:
    # "SPNZT8176J9999999999": the PAN is no whole word (a digit follows it), the ten nines are a
    # bare mobile (a letter before them is not a digit or `+`). Pass 1 gives
    # "pan <PAN>[PHONE] ok"; now a `[` follows the PAN: pass 2 masks it; pass 3 finds nothing.
    masked = mask(f"pan {value(PiiType.PAN)}9999999999 ok")

    assert masked.text == "pan [PAN][PHONE] ok"
    assert masked.counts == {PiiKind.PAN: 1, PHONE: 1}


Q_LAST: Final = PiiPattern("t.q_last", PHONE, (Chars("q"), Assert(Look.NOT_BEFORE, "q")))
"""TEST-ONLY: a "q" with no "q" after it, so each pass frees exactly the one before it."""


def test_passes_end_when_one_finds_nothing() -> None:
    # "qqq" -> "qq[PHONE]" -> "q[PHONE][PHONE]" -> "[PHONE][PHONE][PHONE]": three passes that
    # replace, then a fourth that finds nothing (no "q" in a placeholder).
    masked = mask("qqq", (Locale("t", (Q_LAST,)),))

    assert masked.text == "[PHONE][PHONE][PHONE]"
    assert masked.counts == {PHONE: 3}


def test_a_text_that_does_not_settle_fails_closed() -> None:
    # "qqqq": after three passes "q[PHONE][PHONE][PHONE]" still holds a span.
    with pytest.raises(MaskingIncomplete) as raised:
        mask("qqqq", (Locale("t", (Q_LAST,)),))

    assert str(raised.value) == str(MaskingIncomplete())


@pytest.mark.parametrize("kind", [PiiType.PHONE_IN, PiiType.EMAIL, PiiType.CARD, PiiType.PAN])
def test_masking_a_masked_text_changes_nothing(kind: PiiType) -> None:
    once = mask(f"value {value(kind)} here")
    twice = mask_pii(once.text, DEFAULT)

    assert twice.text == once.text
    assert twice.counts == {}


def test_two_different_values_mask_to_the_same_text() -> None:
    # No index, no hash: nothing in the result tells the two numbers apart (non-reversible).
    first, second = value(PiiType.PHONE_IN, 1), value(PiiType.PHONE_IN, 2)
    assert first != second
    assert mask(f"call {first}").text == mask(f"call {second}").text == "call [PHONE]"


def test_no_digit_of_a_value_survives() -> None:
    kinds = (PiiType.PHONE_IN_TELUGU, PiiType.AADHAAR, PiiType.CARD, PiiType.PASSPORT)
    masked = mask(" and ".join(value(k) for k in kinds))

    assert decimal_digits(masked.text) == ()


def test_the_result_never_shows_a_value_in_its_repr() -> None:
    phone = value(PiiType.PHONE_IN)
    masked = mask(f"call {phone}")

    assert all(form not in repr(masked) for form in leak_forms(phone))


def test_masking_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        mask(f"call {value(PiiType.PHONE_IN)}")
    assert caplog.records == []


# ── 6. Errors name no text ───────────────────────────────────────────────────────────────────


def test_over_the_cap_fails_closed_and_the_error_holds_no_value() -> None:
    phone = value(PiiType.PHONE_IN)
    text = f"call {phone} " + "x" * MASK_CAP

    with pytest.raises(OverCap) as raised:
        mask(text)

    shown = f"{raised.value!s} {raised.value!r} {raised.value.args!r}"
    assert all(form not in shown for form in leak_forms(phone))


def test_a_valid_hindi_message_at_the_message_cap_is_masked() -> None:
    # G1 counts 2,000 characters on the raw text; `clean` (NFC) splits each precomposed nukta
    # letter (U+095E) into two, so this message is 2,010 characters when masked: within
    # `MASK_CAP` (three times the message cap), never an OverCap.
    phone = value(PiiType.PHONE_IN)
    head = chr(0x095E) * 10 + f" call {phone} "
    text = head + "a" * (INPUT_CAPS[Surface.MESSAGE] - len(head))
    assert len(text) == INPUT_CAPS[Surface.MESSAGE]

    masked = mask(text)

    assert masked.text.startswith(clean(chr(0x095E) * 10) + " call [PHONE] a")
    assert masked.counts == {PHONE: 1}


def test_exactly_at_the_cap_is_masked() -> None:
    phone = value(PiiType.PHONE_IN)
    text = f"call {phone} " + "x" * (MASK_CAP - len(phone) - 6)
    assert len(text) == MASK_CAP

    assert mask(text).text.startswith("call [PHONE] x")


def test_no_locale_is_refused_and_the_error_holds_no_value() -> None:
    phone = value(PiiType.PHONE_IN)

    with pytest.raises(ValueError) as raised:
        mask(f"call {phone}", ())

    assert all(form not in str(raised.value) for form in leak_forms(phone))


# ── 7. An adversarial input at the cap (G3: a bound, not a measurement) ──────────────────────

GENEROUS_SECONDS: Final = 2.0


def test_a_run_of_digits_at_the_cap_is_one_span() -> None:
    # `generic.long_digits` takes 20 digits up to the cap in one span: no chunk, no visible rest.
    masked = mask("9" * MASK_CAP)

    assert masked.text == "[ID_NUMBER]"
    assert masked.counts == {ID_NUMBER: 1}


def test_spaced_ones_at_the_cap_are_one_span() -> None:
    # "1 1 1 ... 1" (clean drops the last space): 3,000 ones. Every "1" may start a
    # `generic.digits_n` window (a space before it, not a digit), and a window of n ones ends
    # before a space or the end, so every window of 13 to 19 ones matches: each one is inside
    # the window that starts at it, or (the last 12) inside the 13-one window ending at the last
    # one, and neighbouring windows share ones. All join into one span. The best candidate is a
    # 19-one window (37 characters), the leftmost at 0: Luhn on nineteen ones is 10 + 18 = 28 ->
    # one [ID_NUMBER].
    masked = mask("1 " * (MASK_CAP // 2))

    assert masked.text == "[ID_NUMBER]"
    assert masked.counts == {ID_NUMBER: 1}


def test_adversarial_inputs_at_the_cap_stay_within_a_generous_bound() -> None:
    inputs = (
        "a" * (MASK_CAP - 1) + "@",
        "a@" * (MASK_CAP // 2),
        "x@y." * (MASK_CAP // 4),
        "+1-" * (MASK_CAP // 3 - 1) + "-+1",
        "ZZZZ0" * (MASK_CAP // 5),
        "9-" * (MASK_CAP // 2),
    )
    started = time.perf_counter()
    results = [mask(text) for text in inputs]
    elapsed = time.perf_counter() - started

    assert all(len(text) == MASK_CAP for text in inputs)
    assert all(result.total >= 0 for result in results)
    assert elapsed < GENEROUS_SECONDS
