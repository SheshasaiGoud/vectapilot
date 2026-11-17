"""M7 PR 2: placeholders, the seeded generator and the literal shape checks (`redteam/pii.py`).

Every literal in this file is itself a reserved or fake form (`+91 00000`, Aadhaar starting with
0, published card test numbers, `ZZZZ`/`Z` codes, `.example`): the repository never holds a
number that could be a person's. Look-alike and invisible characters are written as escapes.
"""

import re

import pytest

from vectapilot_api.redteam.pii import (
    DEFAULT_SEED,
    LABEL_OF,
    TEST_CARDS,
    VECTOR_CASE_ID,
    MaskLabel,
    PiiGenerator,
    PiiType,
    Placeholder,
    fold_for_check,
    leak_forms,
    literal_findings,
    luhn_valid,
    placeholders,
    vector_digests,
    verhoeff_valid,
)

GEN = PiiGenerator()


def value(kind: PiiType, case_id: str = "e-001", n: int = 1) -> str:
    return GEN.value(case_id, Placeholder(kind, n))


def test_every_type_has_a_g6_label() -> None:
    assert set(LABEL_OF) == set(PiiType)
    assert set(LABEL_OF.values()) == set(MaskLabel)  # every label can be exercised


@pytest.mark.parametrize("kind", list(PiiType))
def test_every_generated_value_is_a_pii_shaped_literal(kind: PiiType) -> None:
    # The same detector `check` runs: a generated value written into a case is refused.
    assert literal_findings(value(kind)), kind


def test_generation_is_deterministic_per_seed_case_type_and_n() -> None:
    assert value(PiiType.AADHAAR) == value(PiiType.AADHAAR)
    assert value(PiiType.AADHAAR, n=1) != value(PiiType.AADHAAR, n=2)
    assert value(PiiType.AADHAAR, "e-001") != value(PiiType.AADHAAR, "e-002")
    other = PiiGenerator(DEFAULT_SEED + 1).value("e-001", Placeholder(PiiType.AADHAAR, 1))
    assert other != value(PiiType.AADHAAR)


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_the_seed_is_a_32_bit_integer(seed: int) -> None:
    with pytest.raises(ValueError, match="32-bit"):
        PiiGenerator(seed)


def test_values_come_from_reserved_or_fictional_ranges() -> None:
    for case_id in (f"e-{i:03d}" for i in range(40)):
        aadhaar = GEN.value(case_id, Placeholder(PiiType.AADHAAR, 1)).replace(" ", "")
        mistyped = GEN.value(case_id, Placeholder(PiiType.AADHAAR_MISTYPED, 1)).replace(" ", "")
        assert aadhaar[0] in "01" and verhoeff_valid(aadhaar)  # UIDAI never issues 0 or 1
        assert mistyped[0] in "01" and not verhoeff_valid(mistyped)
        for kind in (
            PiiType.PHONE_IN,
            PiiType.PHONE_IN_0091,
            PiiType.PHONE_IN_DASH,
            PiiType.PHONE_IN_DEVANAGARI,
            PiiType.PHONE_IN_TELUGU,
            PiiType.PHONE_IN_FULLWIDTH,
        ):
            digits = re.sub(r"\D", "", fold_for_check(GEN.value(case_id, Placeholder(kind, 1))))
            assert digits.removeprefix("0091").removeprefix("91").startswith("00000"), kind
        spaced = fold_for_check(GEN.value(case_id, Placeholder(PiiType.PHONE_IN_SPACED, 1)))
        assert re.sub(r"\D", "", spaced).startswith("9100000")
        assert value(PiiType.EMAIL, case_id).endswith("@mail.example")
        assert GEN.value(case_id, Placeholder(PiiType.PHONE_INTL, 1)).startswith("+44 20 7946 0")
        assert GEN.value(case_id, Placeholder(PiiType.CARD, 1)) in TEST_CARDS
        assert GEN.value(case_id, Placeholder(PiiType.PAN, 1))[3] == "Z"
        assert GEN.value(case_id, Placeholder(PiiType.IFSC, 1)).startswith("ZZZZ0")
        assert GEN.value(case_id, Placeholder(PiiType.PASSPORT, 1)).startswith("Z")
        assert GEN.value(case_id, Placeholder(PiiType.UPI, 1)).endswith("@examplebank")


def test_the_published_test_cards_pass_luhn() -> None:
    assert all(luhn_valid(card.replace(" ", "")) for card in TEST_CARDS)
    assert not luhn_valid("4111111111111112")


def test_the_digit_scripts_are_what_they_say() -> None:
    assert re.search("[\u0966-\u096f]", value(PiiType.PHONE_IN_DEVANAGARI))
    assert re.search("[\u0c66-\u0c6f]", value(PiiType.PHONE_IN_TELUGU))
    assert re.search("[\uff10-\uff19]", value(PiiType.PHONE_IN_FULLWIDTH))


def test_fill_replaces_every_placeholder_and_reuses_a_numbered_one() -> None:
    text = "call {{pii:phone_in}} or {{pii:phone_in:1}}, not {{pii:phone_in:2}}"
    filled = GEN.fill("f-001", text)
    assert "{{" not in filled
    first, again, other = re.findall(r"\+91 00000 \d{5}", filled)
    assert first == again != other


def test_fill_refuses_an_unknown_type_without_echoing_anything() -> None:
    with pytest.raises(ValueError, match=r"^an unknown placeholder type$"):
        GEN.fill("f-001", "{{pii:name}}")


@pytest.mark.parametrize(
    ("text", "valid", "bad"),
    [
        ("{{pii:email}} and {{pii:card:2}}", 2, 0),
        ("{{pii:}}", 0, 2),  # no type: a stray {{ and }}
        ("{{pii:name}}", 0, 1),  # an unknown type
        ("{{pii:email:0}}", 0, 2),  # n is 1-9
        ("{{name}}", 0, 2),
        ("only {{ one", 0, 1),
        ("plain text", 0, 0),
    ],
)
def test_placeholders(text: str, valid: int, bad: int) -> None:
    found, stray = placeholders(text)
    assert (len(found), stray) == (valid, bad)


def test_vector_digests_pin_every_type_without_a_value() -> None:
    digests = vector_digests(GEN)
    assert set(digests) == {kind.value for kind in PiiType}
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests.values())
    assert digests == vector_digests(PiiGenerator(DEFAULT_SEED))
    assert digests != vector_digests(PiiGenerator(DEFAULT_SEED + 1))
    assert VECTOR_CASE_ID == "test-vector"


@pytest.mark.parametrize(
    "text",
    [
        "+91 00000 12345",
        "+91-00000-12345",
        "0091 00000 12345",
        "0091-00000-12345",
        "00000 12345",
        "0000012345",
        "(+91) 00000 12345",
        "+ 9 1 0 0 0 0 0 1 2 3 4 5",
        "+91 00000\u200b12345",  # a zero-width space inside
        "+91 00000\u00ad12345",  # a soft hyphen inside
        "+\u096f\u0967 \u0966\u0966\u0966\u0966\u0966 \u0967\u0968\u0969\u096a\u096b",
        "\uff0b\uff19\uff11 \uff10\uff10\uff10\uff10\uff10 \uff11\uff12\uff13\uff14\uff15",
        "0000 1111 2222",
        "000011112222",
        "4111 1111 1111 1111",
        "3782-822463-10005",
        "customer01@mail.example",
        "someone@localhost",  # an e-mail without a dot in the domain
        "customer01@examplebank",  # a UPI handle
        "ABCZE1234F",
        "abcze1234f",
        "ZZZZ0123456",
        "Z1234567",
        # ananya's PR 2 review: any punctuation, symbol or space character splits nothing.
        "call 00000\u201312345",  # an en dash
        "00000/12345",
        "00000_12345",
        "00000   12345",  # three spaces
        "00000 , 12345",
        "00000\u2010\u2010\u201012345",  # three hyphens (U+2010)
        "4111\u20141111\u20141111\u20141111",  # em dashes
    ],
)
def test_pii_shapes_are_found(text: str) -> None:
    assert literal_findings(f"TEST-ONLY before {text} after")


@pytest.mark.parametrize(
    "text",
    [
        "on 2026-11-16 at 10:00",
        "call 112 now",
        "order RT-0987654321",  # a phone-like order id that cannot be a full mobile
        "slot S-17 and booking B-101",
        "price 1,500.00 for 2 x 500 mg",
        "{{pii:phone_in}} {{pii:aadhaar}}",
        "Date of birth: 14/03/1990; valid until 31.12.2030",  # dd/mm/yyyy dates
        "00000    12345",  # four separators end a run
    ],
)
def test_ordinary_text_is_not_a_finding(text: str) -> None:
    assert literal_findings(text) == []


def test_a_record_id_exemption_is_narrow() -> None:
    assert literal_findings("order RT-1000000001")  # digits not starting with 0
    assert literal_findings("order RT-00000000001")  # 11 digits
    assert literal_findings("order 0987654321")  # no record-id prefix


def test_eight_digits_is_the_shortest_run_found() -> None:
    # Mutation check: the boundary of `DIGIT_RUN_MIN` is pinned on both sides.
    assert [f.description for f in literal_findings("TEST-ONLY 0000 0000")] == [
        "a phone-like number"
    ]
    assert literal_findings("TEST-ONLY 0000 000") == []


@pytest.mark.parametrize("joiner", ["\u200c", "\u200d"], ids=["zwnj", "zwj"])
def test_a_joiner_cannot_split_a_number(joiner: str) -> None:
    # `tools.config.is_invisible` keeps ZWNJ and ZWJ (Indic scripts need them), so the fold
    # drops them itself; without that, 7 + 5 digits would be two runs too short to find.
    assert literal_findings(f"TEST-ONLY +91 00000{joiner}12345")


def test_nfkc_folds_a_fullwidth_at_sign() -> None:
    # Fullwidth digits fold through `unicodedata.decimal` too; only NFKC folds U+FF20.
    assert literal_findings("TEST-ONLY customer01\uff20mail.example")


def test_fake_forms_are_marked_for_the_published_allowlist() -> None:
    phone, email, real_domain = (
        literal_findings("+91 00000 10001")[0],
        literal_findings("desk@clinic-one.example")[0],
        literal_findings("desk@clinic-one.test")[0],
    )
    assert phone.fake and email.fake and not real_domain.fake
    assert not literal_findings("+91 00000 1000 1")[0].fake  # not the exact fake form


def test_a_leak_check_searches_every_form_a_value_can_take() -> None:
    # Booleans only: a failing assert must not print a generated value.
    spaced = value(PiiType.PHONE_IN_SPACED)
    forms = leak_forms(spaced)
    raw = spaced in forms
    path = spaced.replace(" ", "%20") in forms  # an access line's path keeps the +
    query = spaced.replace(" ", "+").replace("+", "%2B", 1) in forms
    email = any("%40" in form for form in leak_forms(value(PiiType.EMAIL)))
    escaped = any("\\u" in form for form in leak_forms(value(PiiType.PHONE_IN_DEVANAGARI)))

    assert (raw, path, query, email, escaped) == (True, True, True, True, True)
