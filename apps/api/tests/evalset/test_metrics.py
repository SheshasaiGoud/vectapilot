"""H2 tests: hit@k, all groups at k, reciprocal rank, and their means (guide:
docs/learning/m2-metrics.md).

The metrics are [HAND-WRITE]: shesha writes them. Until then they raise NotImplementedError, so the
whole module is marked `xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong answer    → a normal failure (only NotImplementedError counts as expected);
- implemented, right answer    → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, delete the `pytestmark` statement below and run again: every
test must then simply pass. Do not edit the tests to make them pass; change the code.

No database, no network: these run everywhere, in about a second. Every expected value is worked
out by hand in the comment next to it, and compared exactly (`Fraction`, `bool`): no tolerance.
A record is written `[rank 1, rank 2, ...]`, each rank the groups that hit finds (`·` = none);
every record and id here is made up.
"""

import itertools
from collections.abc import Callable, Sequence
from fractions import Fraction
from typing import Any, Final

import pytest

from vectapilot_api.evalset.metrics import (
    all_groups_at_k,
    hit_at_k,
    mean_hit_at_k,
    mean_reciprocal_rank,
    reciprocal_rank,
)
from vectapilot_api.evalset.ranking import RankedRelevance

pytestmark = pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason="H2: shesha implements the retrieval metrics — see docs/learning/m2-metrics.md",
)


def record(question_id: str, n_groups: int, *ranks: tuple[int, ...]) -> RankedRelevance:
    return RankedRelevance(f"example-{question_id}", n_groups, ranks)


NONE: Final[tuple[int, ...]] = ()

# One group each.
FIRST: Final = record("first", 1, (0,), NONE, NONE, NONE, NONE)  # [0, ·, ·, ·, ·]
THIRD: Final = record("third", 1, NONE, NONE, (0,), NONE, NONE)  # [·, ·, 0, ·, ·]
MISS: Final = record("miss", 1, NONE, NONE, NONE, NONE, NONE)  # [·, ·, ·, ·, ·]
EMPTY: Final = record("empty", 1)  # [] (the search returned nothing)
SHORT: Final = record("short", 1, NONE, (0,))  # [·, 0]: two hits only
SIXTH: Final = record("sixth", 1, NONE, NONE, NONE, NONE, NONE, (0,))  # [·, ·, ·, ·, ·, 0]
TWICE: Final = record("twice", 1, NONE, (0,), (0,), NONE)  # [·, 0, 0, ·]

# Several groups.
ONE_OF_TWO: Final = record("one-of-two", 2, (0,), NONE, NONE, NONE, NONE)  # [0, ·, ·, ·, ·]
GROUP_1_FIRST: Final = record("group-1-first", 2, NONE, (1,), NONE, (0,), NONE)  # [·, 1, ·, 0, ·]
GROUP_0_FIRST: Final = record("group-0-first", 2, NONE, (0,), NONE, (1,), NONE)  # [·, 0, ·, 1, ·]
SAME_GROUP_TWICE: Final = record("same-twice", 2, (0,), (0,), NONE, NONE)  # [0, 0, ·, ·]
BOTH_IN_ONE_HIT: Final = record("both-in-one", 2, NONE, (0, 1), NONE)  # [·, 0+1, ·]
THREE: Final = record("three", 3, (2,), NONE, (0,), NONE, NONE, (1,))  # [2, ·, 0, ·, ·, 1]
TWO_OF_THREE: Final = record("two-of-three", 3, (0,), (0,), (1,))  # [0, 0, 1]

EVERY_RECORD: Final = (
    FIRST,
    THIRD,
    MISS,
    EMPTY,
    SHORT,
    SIXTH,
    TWICE,
    ONE_OF_TWO,
    GROUP_1_FIRST,
    GROUP_0_FIRST,
    SAME_GROUP_TWICE,
    BOTH_IN_ONE_HIT,
    THREE,
    TWO_OF_THREE,
)
KS: Final = range(1, 9)  # 1..8: past every list above (the longest has 6 hits)

# Four questions, first relevant ranks 1, 3, none and 2.
FOUR: Final = (FIRST, THIRD, MISS, SHORT)

Metric = Callable[[RankedRelevance, int], object]


# --- hit_at_k ------------------------------------------------------------------------------------


@pytest.mark.parametrize("k", [1, 3, 5])
def test_a_relevant_first_hit_is_a_hit_at_every_k(k: int) -> None:
    assert hit_at_k(FIRST, k) is True  # exactly True: a bool, not 1


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, False),  # ranks 1..1: ·
        (2, False),  # ranks 1..2: · ·  (k smaller than the first relevant rank, 3)
        (3, True),  # ranks 1..3: · · 0
        (5, True),
    ],
)
def test_a_hit_at_rank_3_counts_from_k_3_on(k: int, expected: bool) -> None:
    assert hit_at_k(THIRD, k) is expected


@pytest.mark.parametrize(("result", "k"), [(MISS, 1), (MISS, 5), (MISS, 8), (EMPTY, 1), (EMPTY, 5)])
def test_no_relevant_hit_is_never_a_hit(result: RankedRelevance, k: int) -> None:
    assert hit_at_k(result, k) is False


@pytest.mark.parametrize(
    ("result", "k", "expected"),
    [
        (SHORT, 1, False),  # rank 1 only: ·
        (SHORT, 5, True),  # k = 5, two hits: the whole list [·, 0]
        (SHORT, 50, True),
        (EMPTY, 50, False),  # nothing to look at, even with k = 50
    ],
)
def test_k_beyond_the_list_looks_at_the_whole_list(
    result: RankedRelevance, k: int, expected: bool
) -> None:
    assert hit_at_k(result, k) is expected


def test_a_relevant_hit_just_past_k_is_not_a_hit() -> None:
    assert hit_at_k(SIXTH, 5) is False  # ranks 1..5 are all ·; the 0 is at rank 6
    assert hit_at_k(SIXTH, 6) is True


@pytest.mark.parametrize(
    ("result", "k"),
    [
        (ONE_OF_TWO, 1),  # finds group 0 only, of 2: still a hit
        (GROUP_1_FIRST, 2),  # finds group 1 only, of 2: still a hit
        (THREE, 1),  # finds group 2 only, of 3
    ],
)
def test_any_one_group_is_enough_for_hit_at_k(result: RankedRelevance, k: int) -> None:
    assert hit_at_k(result, k) is True


def test_a_group_found_twice_is_still_just_a_hit() -> None:
    assert hit_at_k(TWICE, 1) is False  # rank 1: ·
    assert hit_at_k(TWICE, 3) is True  # 0 at ranks 2 and 3: True, not a count of 2


# --- all_groups_at_k -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, False),  # ·          → no group
        (2, False),  # · 1        → group 1 only
        (3, False),  # · 1 ·      → group 1 only
        (4, True),  # · 1 · 0    → groups 0 and 1
        (5, True),
    ],
)
def test_all_groups_needs_every_group_in_the_top_k(k: int, expected: bool) -> None:
    assert all_groups_at_k(GROUP_1_FIRST, k) is expected


def test_one_group_of_two_is_not_all() -> None:
    assert all_groups_at_k(ONE_OF_TWO, 5) is False  # [0, ·, ·, ·, ·]: group 1 never found


def test_two_relevant_hits_for_the_same_group_are_not_two_groups() -> None:
    # [0, 0, ·, ·] with 2 groups: two relevant hits, but only group 0. Counting relevant hits
    # (2 >= n_groups) would say True.
    assert all_groups_at_k(SAME_GROUP_TWICE, 2) is False
    assert all_groups_at_k(SAME_GROUP_TWICE, 4) is False


def test_as_many_relevant_hits_as_groups_are_not_all_groups() -> None:
    # [0, 0, 1] with 3 groups: three relevant hits, as many as there are groups, but only groups
    # 0 and 1. Counting relevant hits (3 >= n_groups) would say True; group 2 is never found.
    assert all_groups_at_k(TWO_OF_THREE, 3) is False
    assert all_groups_at_k(TWO_OF_THREE, 8) is False  # k past the list: still no group 2


def test_one_hit_can_find_every_group() -> None:
    assert all_groups_at_k(BOTH_IN_ONE_HIT, 1) is False  # rank 1: ·
    assert all_groups_at_k(BOTH_IN_ONE_HIT, 2) is True  # rank 2 finds 0 and 1


@pytest.mark.parametrize(("k", "expected"), [(3, False), (4, True)])
def test_the_order_the_groups_are_found_in_does_not_matter(k: int, expected: bool) -> None:
    # [·, 1, ·, 0, ·] and [·, 0, ·, 1, ·]: both complete at rank 4, neither before.
    assert all_groups_at_k(GROUP_1_FIRST, k) is expected
    assert all_groups_at_k(GROUP_0_FIRST, k) is expected


@pytest.mark.parametrize(("k", "expected"), [(1, False), (3, False), (5, False), (6, True)])
def test_three_groups_need_all_three(k: int, expected: bool) -> None:
    # [2, ·, 0, ·, ·, 1]: group 2 at rank 1, group 0 at rank 3, group 1 only at rank 6.
    assert all_groups_at_k(THREE, k) is expected


@pytest.mark.parametrize("result", [FIRST, THIRD, MISS, EMPTY, SHORT, SIXTH, TWICE])
def test_with_one_group_all_groups_is_hit_at_k(result: RankedRelevance) -> None:
    for k in KS:
        assert all_groups_at_k(result, k) is hit_at_k(result, k), f"k = {k}"


# --- reciprocal_rank -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (FIRST, Fraction(1)),  # first relevant rank 1 → 1/1
        (THIRD, Fraction(1, 3)),  # rank 3 → 1/3 (0-based positions would give 1/2)
        (SHORT, Fraction(1, 2)),  # rank 2 of a two-hit list → 1/2
        (GROUP_1_FIRST, Fraction(1, 2)),  # group 1 at rank 2 is the first relevant hit
        (THREE, Fraction(1)),  # group 2 at rank 1
    ],
)
def test_reciprocal_rank_is_one_over_the_first_relevant_rank(
    result: RankedRelevance, expected: Fraction
) -> None:
    assert reciprocal_rank(result, 5) == expected


def test_ranks_start_at_one() -> None:
    # Rank r is groups_by_rank[r - 1]. With 0-based ranks FIRST would divide by 0 and THIRD
    # would give 1/2.
    assert reciprocal_rank(FIRST, 5) == Fraction(1)
    assert reciprocal_rank(THIRD, 5) == Fraction(1, 3)


@pytest.mark.parametrize(
    ("result", "k"),
    [
        (MISS, 5),  # nothing relevant anywhere
        (EMPTY, 5),  # no hits at all
        (SIXTH, 5),  # relevant only at rank 6, past k = 5
        (THIRD, 2),  # relevant only at rank 3, past k = 2
    ],
)
def test_nothing_relevant_in_the_top_k_gives_zero(result: RankedRelevance, k: int) -> None:
    assert reciprocal_rank(result, k) == Fraction(0)


@pytest.mark.parametrize(
    ("k", "expected"), [(5, Fraction(0)), (6, Fraction(1, 6)), (8, Fraction(1, 6))]
)
def test_the_cut_off_decides_whether_a_late_hit_counts(k: int, expected: Fraction) -> None:
    assert reciprocal_rank(SIXTH, k) == expected  # [·, ·, ·, ·, ·, 0]


@pytest.mark.parametrize(
    "result",
    [
        TWICE,  # [·, 0, 0, ·]: 1/2, not 1/2 + 1/3
        GROUP_0_FIRST,  # [·, 0, ·, 1, ·]: 1/2; the second group at rank 4 adds nothing
        BOTH_IN_ONE_HIT,  # [·, 0+1, ·]: 1/2, not 1/2 + 1/2
    ],
)
def test_only_the_first_relevant_hit_counts(result: RankedRelevance) -> None:
    assert reciprocal_rank(result, 5) == Fraction(1, 2)


@pytest.mark.parametrize(
    ("result", "expected"), [(THIRD, Fraction(1, 3)), (MISS, Fraction(0)), (FIRST, Fraction(1))]
)
def test_reciprocal_rank_is_an_exact_fraction(result: RankedRelevance, expected: Fraction) -> None:
    value = reciprocal_rank(result, 5)
    assert isinstance(value, Fraction)  # also for 0 and 1 (an int 0 would compare equal)
    assert value == expected


# --- the means over a set of questions -----------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, Fraction(1, 4)),  # FIRST only: 1 of 4
        (2, Fraction(2, 4)),  # FIRST, SHORT (rank 2): 2 of 4 = 1/2
        (3, Fraction(3, 4)),  # FIRST, SHORT, THIRD (rank 3): 3 of 4
        (5, Fraction(3, 4)),  # MISS never: still 3 of 4
    ],
)
def test_mean_hit_at_k_is_the_share_of_questions_with_a_hit(k: int, expected: Fraction) -> None:
    assert mean_hit_at_k(FOUR, k) == expected


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, Fraction(1, 4)),  # (1 + 0 + 0 + 0) / 4
        (2, Fraction(3, 8)),  # (1 + 0 + 0 + 1/2) / 4 = (3/2) / 4
        (3, Fraction(11, 24)),  # (1 + 1/3 + 0 + 1/2) / 4 = (6/6 + 2/6 + 3/6) / 4 = (11/6) / 4
        (5, Fraction(11, 24)),  # nothing new after rank 3
    ],
)
def test_mrr_is_the_mean_reciprocal_rank(k: int, expected: Fraction) -> None:
    assert mean_reciprocal_rank(FOUR, k) == expected


@pytest.mark.parametrize(
    "missing",
    [MISS, EMPTY],  # a miss with five hits, and a search that returned nothing
)
def test_a_question_without_a_hit_still_counts_in_n(missing: RankedRelevance) -> None:
    # (1 + 0) / 2, not 1 / 1: dividing only by the questions with a hit would give 1.
    assert mean_reciprocal_rank([FIRST, missing], 5) == Fraction(1, 2)
    assert mean_hit_at_k([FIRST, missing], 5) == Fraction(1, 2)


def test_the_mean_of_one_question_is_its_own_value() -> None:
    assert mean_reciprocal_rank([THIRD], 5) == Fraction(1, 3)
    assert mean_hit_at_k([THIRD], 5) == Fraction(1)


def test_the_means_over_several_groups_use_any_group() -> None:
    # Reciprocal ranks at k = 5: ONE_OF_TWO 1, GROUP_1_FIRST 1/2, SAME_GROUP_TWICE 1,
    # BOTH_IN_ONE_HIT 1/2. MRR = (1 + 1/2 + 1 + 1/2) / 4 = 3/4. Every one is a hit: 4 of 4.
    several = [ONE_OF_TWO, GROUP_1_FIRST, SAME_GROUP_TWICE, BOTH_IN_ONE_HIT]
    assert mean_reciprocal_rank(several, 5) == Fraction(3, 4)
    assert mean_hit_at_k(several, 5) == Fraction(1)


def test_the_order_of_the_questions_does_not_matter() -> None:
    for order in itertools.permutations(FOUR):  # all 24 orders
        assert mean_reciprocal_rank(order, 5) == Fraction(11, 24)
        assert mean_hit_at_k(order, 5) == Fraction(3, 4)


def test_the_means_are_exact_fractions_and_the_input_is_untouched() -> None:
    third_again = record("third-again", 1, NONE, NONE, (0,))  # [·, ·, 0]: another question
    questions = [THIRD, third_again, SHORT]
    before = list(questions)

    mrr = mean_reciprocal_rank(questions, 5)  # (1/3 + 1/3 + 1/2) / 3 = (7/6) / 3 = 7/18
    rate = mean_hit_at_k(questions, 5)  # 3 of 3

    assert isinstance(mrr, Fraction)
    assert mrr == Fraction(7, 18)
    assert isinstance(rate, Fraction)  # also when the value is 1
    assert rate == Fraction(1)
    assert questions == before


def test_a_mean_over_three_questions_is_exact() -> None:
    # Hits at ranks 1 and 3, then a miss. hit@5: 2 of 3 = 2/3.
    # MRR@5: (1 + 1/3 + 0) / 3 = (4/3) / 3 = 4/9.
    # Neither 2/3 nor 4/9 has an exact float form, so a float converted as it is shows here:
    # Fraction(2 / 3) is 6004799503160661/9007199254740992, not 2/3. A float rounded back with
    # Fraction(x).limit_denominator() does NOT show here (it returns 2/3 and 4/9 again); the next
    # test is the one that catches that.
    questions = [FIRST, THIRD, MISS]

    rate = mean_hit_at_k(questions, 5)
    mrr = mean_reciprocal_rank(questions, 5)

    assert isinstance(rate, Fraction)
    assert rate == Fraction(2, 3)
    assert isinstance(mrr, Fraction)
    assert mrr == Fraction(4, 9)


def first_relevant_at(question_id: str, rank: int) -> RankedRelevance:
    """One group, found only by the hit at `rank`: `rank - 1` irrelevant hits, then group 0."""
    return record(question_id, 1, *([NONE] * (rank - 1)), (0,))


def test_an_mrr_with_a_large_denominator_is_exact() -> None:
    # First relevant hits at ranks 97, 101 and 103 (three primes), all within k = 103.
    #   1/97 + 1/101 + 1/103 = (101·103 + 97·103 + 97·101) / (97·101·103)
    #                        = (10403 + 9991 + 9797) / 1009091 = 30191 / 1009091
    #   MRR@103 = (30191 / 1009091) / 3 = 30191 / 3027273
    # Already reduced: 30191 leaves a remainder when divided by 3 (2), 97 (24), 101 (93) and
    # 103 (12), the only prime factors of 3027273. limit_denominator() picks the nearest fraction
    # with a denominator of at most 1,000,000, so it cannot give 3027273 back: a float mean
    # rounded back with it fails here. (A hit rate x/n has a denominator of at most n, which
    # limit_denominator() recovers for any n up to a million, so only MRR can show this.)
    questions = [
        first_relevant_at("rank-97", 97),
        first_relevant_at("rank-101", 101),
        first_relevant_at("rank-103", 103),
    ]
    expected = Fraction(30191, 3027273)
    assert Fraction(float(expected)).limit_denominator() != expected  # the test's own premise

    mrr = mean_reciprocal_rank(questions, 103)

    assert isinstance(mrr, Fraction)
    assert mrr == expected


@pytest.mark.parametrize("mean", [mean_hit_at_k, mean_reciprocal_rank])
def test_an_empty_set_is_an_error_not_zero(
    mean: Callable[[Sequence[RankedRelevance], int], Fraction],
) -> None:
    with pytest.raises(ValueError) as excinfo:
        mean([], 5)
    # A ZeroDivisionError already fails pytest.raises(ValueError); this line also rules out a
    # ValueError subclass (e.g. statistics.StatisticsError from statistics.mean([])): the contract
    # is the plain ValueError from check_results.
    assert excinfo.type is ValueError


@pytest.mark.parametrize(
    "repeat",
    [
        THIRD,  # the same record again
        # A different record with THIRD's id, as when two runs are merged: a check by record
        # equality would miss it. Questions are named by id.
        record("third", 1, (0,)),  # [0]
    ],
)
@pytest.mark.parametrize("mean", [mean_hit_at_k, mean_reciprocal_rank])
def test_a_question_counted_twice_is_an_error(
    mean: Callable[[Sequence[RankedRelevance], int], Fraction], repeat: RankedRelevance
) -> None:
    with pytest.raises(ValueError, match="example-third"):
        mean([THIRD, FIRST, repeat], 5)  # example-third would weigh double


# --- a bad k -------------------------------------------------------------------------------------


def _hit(result: RankedRelevance, k: int) -> object:
    return hit_at_k(result, k)


def _all(result: RankedRelevance, k: int) -> object:
    return all_groups_at_k(result, k)


def _rr(result: RankedRelevance, k: int) -> object:
    return reciprocal_rank(result, k)


def _same_hits(result: RankedRelevance) -> RankedRelevance:
    # A second question with the same hit list, so a mean sees two questions and only that kind
    # of list: with EMPTY, no question in the set has a single hit to look at.
    return RankedRelevance(f"{result.question_id}-again", result.n_groups, result.groups_by_rank)


def _mean_hit(result: RankedRelevance, k: int) -> object:
    return mean_hit_at_k([result, _same_hits(result)], k)


def _mrr(result: RankedRelevance, k: int) -> object:
    return mean_reciprocal_rank([result, _same_hits(result)], k)


@pytest.mark.parametrize(
    "result",
    [
        FIRST,  # [0, ·, ·, ·, ·]: a hit at k = 1, so True cannot pass as 1 silently
        EMPTY,  # []: an early "no hits, nothing to look at" return must not skip the check
    ],
    ids=["FIRST", "EMPTY"],
)
@pytest.mark.parametrize(
    "k",
    [
        0,
        -1,
        True,  # True == 1 in Python, and still refused
        5.0,  # not an int, though 5.0 == 5: a comparison-based check lets it through
        "5",  # not an int: `k < 1` raises TypeError here, not the contract's ValueError
    ],
)
@pytest.mark.parametrize("metric", [_hit, _all, _rr, _mean_hit, _mrr])
def test_a_bad_k_is_refused(metric: Metric, k: Any, result: RankedRelevance) -> None:
    with pytest.raises(ValueError) as excinfo:
        metric(result, k)
    assert excinfo.type is ValueError


# --- properties, on every record above and k = 1..8 (hand-chosen cases, no random input) ---------


def test_hit_at_k_never_goes_down_as_k_grows() -> None:
    for result in EVERY_RECORD:
        for k in KS:
            if hit_at_k(result, k):
                assert hit_at_k(result, k + 1), (result.question_id, k)


def test_all_groups_implies_a_hit_and_never_goes_down() -> None:
    for result in EVERY_RECORD:
        for k in KS:
            if all_groups_at_k(result, k):
                assert hit_at_k(result, k), (result.question_id, k)
                assert all_groups_at_k(result, k + 1), (result.question_id, k)


def test_reciprocal_rank_stays_in_its_bounds_and_agrees_with_hit_at_k() -> None:
    for result in EVERY_RECORD:
        for k in KS:
            value = reciprocal_rank(result, k)
            assert 0 <= value <= 1, (result.question_id, k)
            assert (value > 0) is hit_at_k(result, k), (result.question_id, k)
            if value > 0:
                assert value >= Fraction(1, k), (result.question_id, k)
            assert reciprocal_rank(result, k + 1) >= value, (result.question_id, k)


def test_at_k_1_reciprocal_rank_and_hit_are_the_same_number() -> None:
    for result in EVERY_RECORD:
        expected = Fraction(1) if hit_at_k(result, 1) else Fraction(0)
        assert reciprocal_rank(result, 1) == expected, result.question_id


@pytest.mark.parametrize(
    "questions",
    [
        FOUR,
        (MISS, EMPTY),  # nothing found anywhere
        (FIRST,),
        EVERY_RECORD,
    ],
)
def test_mrr_lies_between_zero_and_the_hit_rate(questions: Sequence[RankedRelevance]) -> None:
    for k in KS:
        mrr = mean_reciprocal_rank(questions, k)
        rate = mean_hit_at_k(questions, k)
        assert 0 <= mrr <= rate <= 1, k
        if k == 1:
            assert mrr == rate
