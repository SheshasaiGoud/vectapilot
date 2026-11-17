"""Personal data in the red-team suite (scoring rules, "No PII-shaped literal is ever committed").

**Placeholders, never values.** A case writes personal data only as `{{pii:<type>}}`, or
`{{pii:<type>:<n>}}` [proposal] when one case needs the same value twice (a reply that repeats
the customer's own number) or two different values of one type (n = 1-9; bare `<type>` is `:1`).
`PiiType` is the closed list of types; each one names the G6 mask label it should get
(`LABEL_OF`), so `expect: masked:<labels>` can be checked against the case's placeholders.

**The generator** (`PiiGenerator`) fills placeholders at run time, seeded and deterministic per
(seed, case id, type, n), from the standard library only. Values come from ranges that cannot
belong to anyone where a format has one, and from fictional ones where it has none [proposal,
unverified where marked]:
- phones: `+91 00000 ddddd` in every written form of G6 (`+91`, `0091`, `+91-`, per-digit
  spacing, Devanagari, Telugu and fullwidth digits): no Indian number starts `00000`; abroad,
  Ofcom's drama range `+44 20 7946 0ddd` (reserved for fiction);
- e-mail: `customerNN@mail.example` (RFC 2606's reserved `.example`), never a name;
- Aadhaar: 12 digits starting with 0 or 1 (UIDAI never issues one), Verhoeff-valid, and a
  mistyped one whose check digit is wrong (G6 labels it `[ID_NUMBER]`);
- cards: published payment-processor test numbers (`TEST_CARDS`), Luhn-valid;
- PAN, IFSC, passport and UPI have no published reserved range: a PAN whose holder-type letter
  (the fourth) is `Z`, an IFSC with the bank code `ZZZZ`, a passport starting with `Z`, and a UPI
  handle at the fictional `@examplebank` [proposal; unverified that no issuer uses these].
Values are never written to disk or a log: the generator returns strings and nothing else
(`freeze.json` pins only SHA-256 digests of fixed test vectors, `vector_digests`).

**Literal shape checks** (`literal_findings`) are the dataset's own screen, stricter than
`evalset.personal_data` (which misses UPI handles, PAN, IFSC and passports, and needs a dot in an
e-mail's domain). Text is first folded so a disguise cannot hide a number: NFKC (fullwidth to
ASCII), every Unicode decimal digit to ASCII (Devanagari, Telugu), every invisible character
removed. Then: any e-mail-shaped `x@y` (with or without a dot: a UPI handle is one without),
any run of 8 or more digits joined by at most three separator characters, where every
punctuation, symbol, separator or space character but `+` counts as one (en and em dashes,
slashes, underscores, commas: ananya's PR 2 review), so a disguised split is still found (phones
in every form, Aadhaar, cards), a PAN, an IFSC and a passport shape. ISO dates, `dd/mm/yyyy`
dates (also with `-` or `.`) and `hh:mm` times are removed first, and a record id whose prefix
is letters and whose digits start with 0 and number at most 10 (`RT-0987654321`: a phone-like
order id that cannot be a full Indian mobile) is not a finding [proposal]. Each finding is a
fixed description plus a canonical key (digits only, or the case-folded address) used only to
compare with a business's published fake contact lines; neither is ever printed.

Checksums are written here, not imported from the masking code (PR 3, H2): a guard does not
trust the check it guards (G14), and the suite must not either.
"""

import hashlib
import json
import random
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from urllib.parse import quote, quote_plus

from vectapilot_api.tools.config import is_invisible

GENERATOR_VERSION: Final = "redteam-pii.1"
"""Bumped whenever any generator's output can change; `freeze.json` pins it."""
DEFAULT_SEED: Final = 20261116
"""The seed `freeze` records [proposal: the date PR 2 began]. Any non-negative 32-bit integer."""
SEED_MAX: Final = 2**32 - 1


class MaskLabel(StrEnum):
    """G6's placeholder labels, lower case as `expect: masked:<labels>` writes them."""

    PHONE = "phone"
    EMAIL = "email"
    AADHAAR = "aadhaar"
    PAN = "pan"
    CARD = "card"
    UPI = "upi"
    IFSC = "ifsc"
    PASSPORT = "passport"
    ID_NUMBER = "id_number"


class PiiType(StrEnum):
    """What a `{{pii:<type>}}` placeholder asks the generator for."""

    EMAIL = "email"
    PHONE_IN = "phone_in"
    PHONE_IN_0091 = "phone_in_0091"
    PHONE_IN_DASH = "phone_in_dash"
    PHONE_IN_SPACED = "phone_in_spaced"
    PHONE_IN_DEVANAGARI = "phone_in_devanagari"
    PHONE_IN_TELUGU = "phone_in_telugu"
    PHONE_IN_FULLWIDTH = "phone_in_fullwidth"
    PHONE_INTL = "phone_intl"
    AADHAAR = "aadhaar"
    AADHAAR_MISTYPED = "aadhaar_mistyped"
    PAN = "pan"
    CARD = "card"
    UPI = "upi"
    IFSC = "ifsc"
    PASSPORT = "passport"


LABEL_OF: Final[Mapping[PiiType, MaskLabel]] = MappingProxyType(
    {
        PiiType.EMAIL: MaskLabel.EMAIL,
        PiiType.PHONE_IN: MaskLabel.PHONE,
        PiiType.PHONE_IN_0091: MaskLabel.PHONE,
        PiiType.PHONE_IN_DASH: MaskLabel.PHONE,
        PiiType.PHONE_IN_SPACED: MaskLabel.PHONE,
        PiiType.PHONE_IN_DEVANAGARI: MaskLabel.PHONE,
        PiiType.PHONE_IN_TELUGU: MaskLabel.PHONE,
        PiiType.PHONE_IN_FULLWIDTH: MaskLabel.PHONE,
        PiiType.PHONE_INTL: MaskLabel.PHONE,
        PiiType.AADHAAR: MaskLabel.AADHAAR,
        PiiType.AADHAAR_MISTYPED: MaskLabel.ID_NUMBER,
        PiiType.PAN: MaskLabel.PAN,
        PiiType.CARD: MaskLabel.CARD,
        PiiType.UPI: MaskLabel.UPI,
        PiiType.IFSC: MaskLabel.IFSC,
        PiiType.PASSPORT: MaskLabel.PASSPORT,
    }
)
"""The label G6 should give each type's value (shape decides; the checksum picks the label)."""


# ── Placeholders ─────────────────────────────────────────────────────────────────────────────

PLACEHOLDER: Final = re.compile(r"\{\{pii:([a-z0-9_]+)(?::([1-9]))?\}\}")
"""`{{pii:<type>}}` or `{{pii:<type>:<n>}}`. Any other `{{` is refused by `check`."""
_BRACES: Final = re.compile(r"\{\{|\}\}")


@dataclass(frozen=True)
class Placeholder:
    type: PiiType
    n: int


def placeholders(text: str) -> tuple[tuple[Placeholder, ...], int]:
    """The valid placeholders in `text`, in order, and how many `{{` or `}}` are left that are not
    part of one (an unknown type, a missing type, a malformed one): each is a problem."""
    found: list[Placeholder] = []
    bad = 0
    for match in PLACEHOLDER.finditer(text):
        try:
            found.append(Placeholder(PiiType(match.group(1)), int(match.group(2) or "1")))
        except ValueError:
            bad += 1
    rest = PLACEHOLDER.sub(" ", text)
    return tuple(found), bad + len(_BRACES.findall(rest))


def without_placeholders(text: str) -> str:
    """`text` with every placeholder replaced by a space (what the literal check reads)."""
    return PLACEHOLDER.sub(" ", text)


# ── Checksums (written here on purpose: module docstring) ────────────────────────────────────

_VERHOEFF_D: Final = (
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
_VERHOEFF_P: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
_VERHOEFF_INV: Final = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def _verhoeff_checksum(digits: str) -> int:
    check = 0
    for i, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(digit)]]
    return check


def verhoeff_valid(digits: str) -> bool:
    return _verhoeff_checksum(digits) == 0


def verhoeff_digit(digits: str) -> str:
    """The check digit that makes `digits + it` Verhoeff-valid."""
    return str(_VERHOEFF_INV[_verhoeff_checksum(digits + "0")])


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, digit in enumerate(reversed(digits)):
        value = int(digit) * (2 if i % 2 else 1)
        total += value - 9 if value > 9 else value
    return total % 10 == 0


# ── The generator ────────────────────────────────────────────────────────────────────────────

TEST_CARDS: Final = (
    "4111 1111 1111 1111",
    "4012 8888 8888 1881",
    "5555 5555 5555 4444",
    "5105 1051 0510 5100",
    "3782 822463 10005",
    "3714 496353 98431",
)
"""Payment processors' published test numbers (Visa, Mastercard, American Express): accepted by
no live network. Each is Luhn-valid (tested)."""


def _digit_table(zero: int, plus: str = "+") -> dict[int, str]:
    """ASCII digits (and `+`) to the ten digits starting at code point `zero`, built from code
    points so no look-alike character is written in the source (RUF001)."""
    table = {ord(str(i)): chr(zero + i) for i in range(10)}
    table[ord("+")] = plus
    return table


_DEVANAGARI_DIGITS: Final = _digit_table(0x0966)
_TELUGU_DIGITS: Final = _digit_table(0x0C66)
_FULLWIDTH: Final = _digit_table(0xFF10, plus=chr(0xFF0B))
_LETTERS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randrange(10)) for _ in range(n))


def _letters(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(_LETTERS) for _ in range(n))


def _aadhaar(rng: random.Random, *, valid: bool) -> str:
    body = str(rng.randrange(2)) + _digits(rng, 10)
    check = verhoeff_digit(body)
    if not valid:
        check = str((int(check) + 1 + rng.randrange(9)) % 10)
    number = body + check
    return f"{number[:4]} {number[4:8]} {number[8:]}"


def _value(kind: PiiType, rng: random.Random) -> str:
    local = _digits(rng, 5)
    match kind:
        case PiiType.EMAIL:
            return f"customer{_digits(rng, 2)}@mail.example"
        case PiiType.PHONE_IN:
            return f"+91 00000 {local}"
        case PiiType.PHONE_IN_0091:
            return f"0091 00000 {local}"
        case PiiType.PHONE_IN_DASH:
            return f"+91-00000-{local}"
        case PiiType.PHONE_IN_SPACED:
            return "+91 " + " ".join("00000" + local)
        case PiiType.PHONE_IN_DEVANAGARI:
            return f"+91 00000 {local}".translate(_DEVANAGARI_DIGITS)
        case PiiType.PHONE_IN_TELUGU:
            return f"+91 00000 {local}".translate(_TELUGU_DIGITS)
        case PiiType.PHONE_IN_FULLWIDTH:
            return f"+91 00000 {local}".translate(_FULLWIDTH)
        case PiiType.PHONE_INTL:
            return f"+44 20 7946 0{_digits(rng, 3)}"
        case PiiType.AADHAAR:
            return _aadhaar(rng, valid=True)
        case PiiType.AADHAAR_MISTYPED:
            return _aadhaar(rng, valid=False)
        case PiiType.PAN:
            return f"{_letters(rng, 3)}Z{_letters(rng, 1)}{_digits(rng, 4)}{_letters(rng, 1)}"
        case PiiType.CARD:
            return rng.choice(TEST_CARDS)
        case PiiType.UPI:
            return f"customer{_digits(rng, 2)}@examplebank"
        case PiiType.IFSC:
            return f"ZZZZ0{_digits(rng, 6)}"
        case PiiType.PASSPORT:
            return f"Z{_digits(rng, 7)}"


@dataclass(frozen=True)
class PiiGenerator:
    """Fills placeholders at run time (module docstring). Holds no value: each one is derived
    from (version, seed, case id, type, n) when asked for, and returned, never kept."""

    seed: int = DEFAULT_SEED

    def __post_init__(self) -> None:
        if not 0 <= self.seed <= SEED_MAX:
            raise ValueError("the seed is a non-negative 32-bit integer")

    def value(self, case_id: str, placeholder: Placeholder) -> str:
        key = f"{GENERATOR_VERSION}|{self.seed}|{case_id}|{placeholder.type}|{placeholder.n}"
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        # Reproducible test data, not a secret: a seeded PRNG is the point (S311).
        rng = random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311
        return _value(placeholder.type, rng)

    def fill(self, case_id: str, text: str) -> str:
        """`text` with every placeholder filled. Raises `ValueError` (no value in the message) on
        a placeholder of an unknown type: `check` refuses those before any run."""

        def one(match: re.Match[str]) -> str:
            try:
                kind = PiiType(match.group(1))
            except ValueError:
                raise ValueError("an unknown placeholder type") from None
            return self.value(case_id, Placeholder(kind, int(match.group(2) or "1")))

        return PLACEHOLDER.sub(one, text)


PATH_SAFE: Final = "!$&'()*+,;=:@/"
"""RFC 3986's sub-delimiters, `:`, `@` and `/`: what a client leaves unencoded in a path."""


def leak_forms(value: str) -> frozenset[str]:
    """Every form a generated value can take in a log line or an access line, for F's leak check
    (leo's PR 2 review: httpx percent-encodes a path's spaces, so a search for the raw value
    alone would pass without testing anything): raw, percent-encoded (strictly, as a path
    keeps it, and as a query) and
    JSON-escaped (ASCII-only and not)."""
    return frozenset(
        {
            value,
            quote(value, safe=""),
            quote(value),
            quote(value, safe=PATH_SAFE),
            quote_plus(value),
            json.dumps(value)[1:-1],
            json.dumps(value, ensure_ascii=False)[1:-1],
        }
    )


VECTOR_CASE_ID: Final = "test-vector"


def vector_digests(generator: PiiGenerator) -> dict[str, str]:
    """`freeze.json`'s test vectors: per type, the SHA-256 of the value for a fixed case id and
    n = 1. A digest, so the pins show any change in a generator without writing a value."""
    return {
        kind.value: hashlib.sha256(
            generator.value(VECTOR_CASE_ID, Placeholder(kind, 1)).encode("utf-8")
        ).hexdigest()
        for kind in PiiType
    }


# ── Literal shape checks ─────────────────────────────────────────────────────────────────────

_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*")
_DIGIT_RUN: Final = re.compile(r"\+?\d(?: {0,3}\d)*")
"""A digit run on the separator-folded text (`_separators_folded`): at most three separators."""
_SEPARATOR_CATEGORIES: Final = frozenset("PSZ")
_ISO_DATE: Final = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_DMY_DATE: Final = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])([/.-])(?:0?[1-9]|1[0-2])\1(?:19|20)\d{2}(?!\d)"
)
"""A `dd/mm/yyyy` date (also with `-` or `.`), as a document's visible lines print one."""
_TIME: Final = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")
_PAN: Final = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}\d{4}[A-Za-z](?![A-Za-z0-9])")
_IFSC: Final = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{4}0[A-Za-z0-9]{6}(?![A-Za-z0-9])")
_PASSPORT: Final = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\d{7}(?![A-Za-z0-9])")
_RECORD_TOKEN: Final = re.compile(r"[A-Za-z0-9-]+")
_RECORD_ID: Final = re.compile(r"^[A-Z]{2,}-[0-9]+$")
_FAKE_PHONE: Final = re.compile(r"^\+91 00000 \d{5}$")
"""The sample corpus's fake contact form (`evalset.personal_data.FAKE_PHONE`), checked on the
folded run."""
RESERVED_DOMAIN: Final = ".example"
DIGIT_RUN_MIN: Final = 8
RECORD_DIGITS_MAX: Final = 10


@dataclass(frozen=True)
class LiteralFinding:
    """One PII-shaped literal: a fixed description, a canonical key (never printed) and whether it
    is in the sample corpus's fake form (`+91 00000 ddddd`, an address at `.example`)."""

    description: str
    key: str
    fake: bool


def fold_for_check(text: str) -> str:
    """NFKC, every Unicode decimal digit to ASCII, every invisible character (joiners included)
    removed: a disguise cannot split or hide a number."""
    out: list[str] = []
    for character in unicodedata.normalize("NFKC", text):
        if is_invisible(character) or character in "\u200c\u200d":
            continue
        decimal = unicodedata.decimal(character, None)
        out.append(str(decimal) if decimal is not None else character)
    return "".join(out)


def _digit_description(count: int) -> str:
    if count == 12:
        return "an Aadhaar- or phone-shaped number"
    if count >= 13:
        return "a card- or identifier-shaped number"
    return "a phone-like number"


def _record_id(text: str, start: int, end: int) -> bool:
    """Whether the run at [start, end) sits in a record id `XX-0ddd` (module docstring)."""
    for token in _RECORD_TOKEN.finditer(text):
        if token.start() <= start and end <= token.end():
            digits = token.group(0).split("-", 1)[-1]
            return (
                _RECORD_ID.fullmatch(token.group(0)) is not None
                and digits.startswith("0")
                and len(digits) <= RECORD_DIGITS_MAX
            )
    return False


def _separators_folded(text: str) -> str:
    """`text` with every punctuation, symbol, separator and space character but `+` replaced by
    one space, so positions stay those of `text` (a record id is checked on the original)."""
    return "".join(
        " "
        if character != "+"
        and (character.isspace() or unicodedata.category(character)[0] in _SEPARATOR_CATEGORIES)
        else character
        for character in text
    )


def _digit_findings(text: str) -> Iterator[LiteralFinding]:
    for run in _DIGIT_RUN.finditer(_separators_folded(text)):
        digits = re.sub(r"\D", "", run.group(0))
        if len(digits) < DIGIT_RUN_MIN or _record_id(text, run.start(), run.end()):
            continue
        fake = _FAKE_PHONE.fullmatch(run.group(0)) is not None
        yield LiteralFinding(_digit_description(len(digits)), digits, fake)


def literal_findings(text: str) -> list[LiteralFinding]:
    """Every PII-shaped literal in `text` (module docstring), placeholders excluded."""
    folded = fold_for_check(without_placeholders(text))
    findings: list[LiteralFinding] = []
    for match in _EMAIL.finditer(folded):
        address = match.group(0).casefold()
        domain = address.rsplit("@", 1)[1]
        if "." in domain:
            fake = domain.endswith(RESERVED_DOMAIN)
            findings.append(LiteralFinding("an e-mail address", address, fake))
        else:
            findings.append(
                LiteralFinding("an e-mail address or UPI handle without a dot", address, False)
            )
    rest = _EMAIL.sub(" ", folded)
    rest = _TIME.sub(" ", _DMY_DATE.sub(" ", _ISO_DATE.sub(" ", rest)))
    findings.extend(_digit_findings(rest))
    for pattern, description in (
        (_PAN, "a PAN-shaped code"),
        (_IFSC, "an IFSC-shaped code"),
        (_PASSPORT, "a passport-shaped code"),
    ):
        findings.extend(
            LiteralFinding(description, m.group(0).upper(), False) for m in pattern.finditer(rest)
        )
    return findings
