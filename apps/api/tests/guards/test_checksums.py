"""M7 PR 3: Verhoeff and Luhn over Unicode digits (`guards/checksums.py`, G6, D1 (a)).

Every number here is a published test vector, cited where it is used, or a digit string that
belongs to nobody (all ones). Non-ASCII digits are built from their script's zero code point, so
no look-alike character is written in the source (RUF001).
"""

import random

import pytest

from vectapilot_api.guards.checksums import (
    Checksum,
    checksum_valid,
    decimal_digits,
    luhn_valid,
    verhoeff_valid,
)
from vectapilot_api.redteam import pii as suite

DEVANAGARI_ZERO = 0x0966
TELUGU_ZERO = 0x0C66
FULLWIDTH_ZERO = 0xFF10


def in_script(text: str, zero: int) -> str:
    """`text` with each ASCII digit written in the script whose zero is `zero`."""
    return "".join(chr(zero + int(c)) if c.isascii() and c.isdigit() else c for c in text)


def digits(text: str) -> tuple[int, ...]:
    return decimal_digits(text)


# ── Published test vectors ───────────────────────────────────────────────────────────────────

# Verhoeff: Wikipedia, "Verhoeff algorithm" (worked example: the check digit of 236 is 3), and
# Rosetta Code, "Verhoeff algorithm" (236 -> 3, 12345 -> 1, 123456789012 -> 0).
VERHOEFF_VALID = ("2363", "123451", "1234567890120")
# Luhn: Wikipedia, "Luhn algorithm" (7992739871 with check digit 3), and Rosetta Code, "Luhn test
# of credit card numbers" (49927398716 and 1234567812345670 pass; 49927398717 and
# 1234567812345678 fail).
LUHN_VALID = ("79927398713", "49927398716", "1234567812345670")
LUHN_INVALID = ("49927398717", "1234567812345678")


@pytest.mark.parametrize("number", VERHOEFF_VALID)
def test_verhoeff_accepts_the_published_vectors(number: str) -> None:
    assert verhoeff_valid(digits(number))


@pytest.mark.parametrize("number", VERHOEFF_VALID)
def test_verhoeff_refuses_every_wrong_check_digit(number: str) -> None:
    body, check = number[:-1], int(number[-1])
    for wrong in set(range(10)) - {check}:
        assert not verhoeff_valid(digits(f"{body}{wrong}"))


@pytest.mark.parametrize("number", VERHOEFF_VALID)
def test_verhoeff_refuses_every_swap_of_two_different_adjacent_digits(number: str) -> None:
    # Verhoeff's design goal (the reason UIDAI chose it for Aadhaar): no adjacent transposition
    # goes unnoticed. 2363 -> 3263, 2633, 2336: all refused.
    for i in range(len(number) - 1):
        if number[i] != number[i + 1]:
            swapped = number[:i] + number[i + 1] + number[i] + number[i + 2 :]
            assert not verhoeff_valid(digits(swapped)), i


@pytest.mark.parametrize("number", LUHN_VALID)
def test_luhn_accepts_the_published_vectors(number: str) -> None:
    assert luhn_valid(digits(number))


@pytest.mark.parametrize("number", LUHN_INVALID)
def test_luhn_refuses_the_published_failures(number: str) -> None:
    assert not luhn_valid(digits(number))


@pytest.mark.parametrize("card", suite.TEST_CARDS)
def test_luhn_accepts_the_payment_processors_test_cards(card: str) -> None:
    assert luhn_valid(digits(card))  # the separators are skipped


def test_luhn_misses_a_09_swap_that_verhoeff_would_catch() -> None:
    # By hand, from the right: 091 -> 1 + (9*2 = 18 -> 9) + 0 = 10; 901 -> 1 + (0*2) + 9 = 10.
    # Both pass Luhn: why Aadhaar uses Verhoeff (module docstring).
    assert luhn_valid(digits("091")) and luhn_valid(digits("901"))


# ── Unicode digits ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("zero", [DEVANAGARI_ZERO, TELUGU_ZERO, FULLWIDTH_ZERO])
def test_both_checks_read_devanagari_telugu_and_fullwidth_digits(zero: int) -> None:
    for number in VERHOEFF_VALID:
        written = in_script(number, zero)
        assert written != number and verhoeff_valid(digits(written))
    for number in LUHN_VALID:
        assert luhn_valid(digits(in_script(number, zero)))
    assert not luhn_valid(digits(in_script(LUHN_INVALID[0], zero)))


def test_decimal_digits_reads_every_script_and_skips_everything_else() -> None:
    mixed = "4-" + in_script("1", DEVANAGARI_ZERO) + " " + in_script("1", TELUGU_ZERO) + "x1"
    assert decimal_digits(mixed) == (4, 1, 1, 1)
    # A superscript two and a circled one are `No`, not `Nd`: skipped, as `\d` skips them.
    assert decimal_digits("²①") == ()


# ── Edges ────────────────────────────────────────────────────────────────────────────────────


def test_no_digits_is_never_valid() -> None:
    assert not verhoeff_valid(())
    assert not luhn_valid(())


@pytest.mark.parametrize("bad", [(10,), (-1,), (1, 2, 30)])
def test_a_value_outside_0_to_9_is_refused(bad: tuple[int, ...]) -> None:
    with pytest.raises(ValueError, match="0 to 9"):
        verhoeff_valid(bad)
    with pytest.raises(ValueError, match="0 to 9"):
        luhn_valid(bad)


def test_checksum_valid_dispatches_by_name() -> None:
    assert checksum_valid(Checksum.VERHOEFF, digits("2363"))
    assert not checksum_valid(Checksum.VERHOEFF, digits("79927398713"))
    assert checksum_valid(Checksum.LUHN, digits("79927398713"))
    assert not checksum_valid(Checksum.LUHN, digits("2364"))


def test_both_agree_with_the_red_team_suites_own_implementation() -> None:
    # Two implementations written apart (G14): the suite's must not import this one.
    rng = random.Random(20261117)  # noqa: S311 (seeded test data, not a secret)
    for _ in range(2_000):
        number = "".join(str(rng.randrange(10)) for _ in range(rng.randrange(1, 20)))
        assert verhoeff_valid(digits(number)) == suite.verhoeff_valid(number), number
        assert luhn_valid(digits(number)) == suite.luhn_valid(number), number
