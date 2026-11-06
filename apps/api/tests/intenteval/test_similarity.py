"""M4 PR 1: the near-duplicate screen (character 3-gram Jaccard on normalised text).

The probe strings are Greek letter names, not messages; they pin the properties the module's
docstring quotes to justify the 70 % threshold.
"""

from fractions import Fraction
from typing import Final

import pytest

from vectapilot_api.intenteval.similarity import (
    NearPair,
    at_or_above,
    jaccard,
    near_pairs_between,
    near_pairs_within,
    normalize,
    trigrams,
)

SIX: Final = "alpha beta gamma delta epsilon zeta"


def test_normalisation_folds_case_width_punctuation_and_spacing() -> None:
    assert normalize("  Alpha,\tBETA!!  gamma… ") == "alpha beta gamma"
    assert normalize("\uff21\uff2c\uff30\uff28\uff21") == "alpha"  # full-width ALPHA (NFKC)
    assert normalize("?!...") == ""


def test_normalisation_keeps_devanagari_vowel_signs() -> None:
    # Vowel signs are combining marks: dropping them would merge different Hindi words.
    assert normalize("परीक्षण।") == "परीक्षण"
    assert normalize("कि") != normalize("क")


def test_trigrams_are_the_set_of_overlapping_windows() -> None:
    assert trigrams("Abcd!") == frozenset({"abc", "bcd"})
    assert trigrams("ab") == frozenset({"ab"})
    assert trigrams("...") == frozenset()


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        ("alpha beta gamma delta epsilon zetq", Fraction(31, 32)),  # one-character typo
        ("alpha beta gamma delta epsilon zeta theta", Fraction(31, 35)),  # a word appended
        ("beta alpha gamma epsilon delta zeta", Fraction(27, 35)),  # two pairs swapped
        ("alpha beta gamma kappa epsilon zeta", Fraction(26, 37)),  # one word in six replaced
        ("Alpha, beta; GAMMA delta epsilon zeta!", Fraction(1)),  # case and punctuation only
    ],
)
def test_light_edits_stay_at_or_above_seventy_percent(other: str, expected: Fraction) -> None:
    assert jaccard(SIX, other) == expected
    assert at_or_above(expected, 70)


def test_a_shared_template_alone_stays_below_seventy_percent() -> None:
    similarity = jaccard(
        "alpha beta gamma delta epsilon zeta eta theta iota",
        "alpha beta gamma delta epsilon zeta kappa lambda mu",
    )
    assert similarity == Fraction(31, 55)
    assert not at_or_above(similarity, 70)


def test_the_same_normalised_text_scores_one_even_when_empty() -> None:
    assert jaccard("?!", "...") == 1
    assert jaccard("ab", "AB") == 1
    assert jaccard("abc", "") == 0


def test_the_threshold_is_inclusive_and_exact() -> None:
    assert at_or_above(Fraction(7, 10), 70)
    assert not at_or_above(Fraction(699, 1000), 70)
    assert at_or_above(Fraction(1), 100)


def test_pairs_between_two_lists_come_in_input_order_with_floor_percent() -> None:
    left = [("example-a", SIX), ("example-b", "omega psi chi phi upsilon tau")]
    right = [
        ("example-x", "alpha beta gamma kappa epsilon zeta"),
        ("example-y", SIX.upper()),
    ]
    pairs = near_pairs_between(left, right, 70)
    assert pairs == [
        NearPair("example-a", "example-x", Fraction(26, 37)),
        NearPair("example-a", "example-y", Fraction(1)),
    ]
    assert [p.percent for p in pairs] == [70, 100]


def test_pairs_within_one_list_name_each_pair_once() -> None:
    rows = [
        ("example-a", SIX),
        ("example-b", SIX),
        ("example-c", "omega psi chi"),
        ("example-d", SIX),
    ]
    assert [(p.first, p.second) for p in near_pairs_within(rows, 70)] == [
        ("example-a", "example-b"),
        ("example-a", "example-d"),
        ("example-b", "example-d"),
    ]
