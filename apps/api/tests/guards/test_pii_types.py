"""M7 PR 3: H2's types around the stub (`guards/pii.py`): the placeholders, the cap and `Masked`.
These are team code and pass now; `mask_pii` itself is tested in `test_mask_pii.py`."""

from types import MappingProxyType

import pytest

from vectapilot_api.guards.locales import LOCALES, MASK_CAP, PiiKind, ascii_view
from vectapilot_api.guards.pii import MASK_CAP as PII_MASK_CAP
from vectapilot_api.guards.pii import MAX_PASSES, PLACEHOLDERS, Masked, MaskingIncomplete
from vectapilot_api.guards.types import Clean


def test_the_placeholders_are_g6s_nine() -> None:
    assert sorted(PLACEHOLDERS.values()) == sorted(
        [
            "[PHONE]",
            "[EMAIL]",
            "[AADHAAR]",
            "[PAN]",
            "[CARD]",
            "[UPI]",
            "[IFSC]",
            "[PASSPORT]",
            "[ID_NUMBER]",
        ]
    )
    assert isinstance(PLACEHOLDERS, MappingProxyType)
    assert PII_MASK_CAP == MASK_CAP


def test_no_pattern_finds_anything_in_a_placeholder() -> None:
    # Why contract rule 6's passes end: a placeholder holds no digit, no `@` and no code-shaped
    # word, so a later pass can only find a span a replacement freed, never one inside a
    # placeholder (at any start position, `anywhere`).
    every = Clean(" ".join(PLACEHOLDERS.values()) + " " + "".join(PLACEHOLDERS.values()))
    for locale in LOCALES.values():
        for pattern in locale.patterns:
            assert pattern.regex.search(ascii_view(every)) is None, pattern.id
            assert pattern.anywhere.search(ascii_view(every)) is None, pattern.id


def test_an_unsettled_text_fails_closed_with_a_fixed_message() -> None:
    error = MaskingIncomplete()

    assert MAX_PASSES == 3
    assert isinstance(error, RuntimeError)
    assert str(error) == "masking still found a span after 3 passes"
    assert error.args == ("masking still found a span after 3 passes",)


def test_masked_holds_counts_by_kind_read_only() -> None:
    masked = Masked(
        Clean("call [PHONE] or [PHONE], mail [EMAIL]"), {PiiKind.PHONE: 2, PiiKind.EMAIL: 1}
    )

    assert masked.total == 3
    assert isinstance(masked.counts, MappingProxyType)
    assert Masked(Clean("nothing here")).counts == {}


def test_a_masked_repr_shows_counts_only() -> None:
    masked = Masked(Clean("text [CARD] that the repr must not show"), {PiiKind.CARD: 1})
    assert repr(masked) == "Masked(counts={CARD=1})"
    assert repr(Masked(Clean("x"))) == "Masked(counts={})"


@pytest.mark.parametrize(
    "counts",
    [
        {PiiKind.PHONE: 0},
        {PiiKind.PHONE: 2},  # only one `[PHONE]` in the text
        {PiiKind.EMAIL: 1},  # no `[EMAIL]` at all
        {"PHONE": 1},  # a plain string, not the closed kind
    ],
)
def test_a_count_must_be_positive_by_kind_and_backed_by_its_placeholder(
    counts: dict[PiiKind, int],
) -> None:
    with pytest.raises(ValueError, match="count"):
        Masked(Clean("call [PHONE]"), counts)


def test_a_customer_typed_placeholder_does_not_need_a_count() -> None:
    assert Masked(Clean("I typed [PHONE] myself")).total == 0
