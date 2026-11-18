"""Verhoeff and Luhn over Unicode digits (G6, D1 (a)): team code, standard library only.

**What a checksum is for here.** Shape decides whether a span is masked; a checksum only picks
its label (G6). A 12-digit run in the Aadhaar grouping is `[AADHAAR]` when its Verhoeff check
holds and `[ID_NUMBER]` when it does not; a 13-19 digit run is `[CARD]` when its Luhn check holds
and `[ID_NUMBER]` when it does not. Either way it is masked: a mistyped identifier is still
personal data, so a checksum never lets a span through.

**Unicode digits.** `decimal_digits` reads every decimal digit (`Nd`) of any script with
`unicodedata.digit`, so the Devanagari, Telugu and fullwidth forms of `4111`
(U+096A U+0967..., U+0C6A U+0C67..., U+FF14 U+FF11...) all count as `4111`, and skips
everything else (spaces, hyphens). A superscript or circled digit is not `Nd` (it is `No`) and
is skipped, as `\\d` skips it.

- **Verhoeff** (Jacobus Verhoeff, 1969) is the dihedral-group check UIDAI uses for Aadhaar's last
  digit. It catches every single-digit error and every swap of two adjacent digits, which Luhn
  does not (Luhn misses `09` <-> `90`).
- **Luhn** (ISO/IEC 7812-1) is the payment-card check: double every second digit from the right,
  subtract 9 from any result above 9, and the sum must end in 0.

The tables below are Verhoeff's published ones (multiplication `D` of the dihedral group D5,
permutation `P`). `tests/guards/test_checksums.py` checks both functions against published
test vectors and against the red-team suite's own implementation (`redteam/pii.py`), which is
written separately on purpose (G14: a guard does not trust the check it guards).
"""

import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from typing import Final


class Checksum(StrEnum):
    """The check a pattern's span is labelled by (`locales.PiiPattern.checksum`)."""

    VERHOEFF = "verhoeff"
    LUHN = "luhn"


_D: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
"""Verhoeff's multiplication table: the dihedral group D5."""
_P: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
"""Verhoeff's permutation table: row `i % 8` permutes the digit at position `i` from the right."""


def decimal_digits(text: str) -> tuple[int, ...]:
    """The value of every decimal digit (`Nd`, any script) in `text`, in order; every other
    character is skipped (module docstring)."""
    return tuple(unicodedata.digit(c) for c in text if unicodedata.category(c) == "Nd")


def _checked(digits: Sequence[int]) -> Sequence[int]:
    if any(not 0 <= digit <= 9 for digit in digits):
        raise ValueError("a digit is an integer from 0 to 9")
    return digits


def verhoeff_valid(digits: Sequence[int]) -> bool:
    """Whether `digits`, check digit last, pass Verhoeff's check. No digits is not valid."""
    check = 0
    for position, digit in enumerate(reversed(_checked(digits))):
        check = _D[check][_P[position % 8][digit]]
    return bool(digits) and check == 0


def luhn_valid(digits: Sequence[int]) -> bool:
    """Whether `digits`, check digit last, pass Luhn's check. No digits is not valid."""
    total = 0
    for position, digit in enumerate(reversed(_checked(digits))):
        doubled = digit * 2 if position % 2 else digit
        total += doubled - 9 if doubled > 9 else doubled
    return bool(digits) and total % 10 == 0


def checksum_valid(checksum: Checksum, digits: Sequence[int]) -> bool:
    """`checksum`'s verdict on `digits`."""
    if checksum is Checksum.VERHOEFF:
        return verhoeff_valid(digits)
    return luhn_valid(digits)
