"""H2 (M3) tests: `rrf_fuse`, Reciprocal Rank Fusion (guide: docs/learning/m3-hybrid.md).

`rrf_fuse` is [HAND-WRITE], written by shesha (D5). Until then it raises NotImplementedError, so
the whole module is a strict xfail on it:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong answer    → a normal failure (only NotImplementedError counts as expected);
- implemented, right answer    → "XPASS(strict)", a failure on purpose: that is your signal.

Do not edit a test to make it pass; change the code. No database, no network: these run
everywhere, in about 4 seconds (the hash-seed test starts several interpreters). Every expected
value is worked out by hand in the comment next to it and compared exactly (`Fraction`): no
tolerance. A list is written `[A, B, C]`, best first; ranks count from 1.

The hits are made up. Where a tie is broken, source ids, chunk ids and cosines are chosen so that
sorting by name, by id or by cosine does not give the expected order by accident; each hit's
content and title carry a marker that no error message may show.
"""

import itertools
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Final

import pytest

from tests.canary.read_paths import pending_hand_write
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.retrieval.fusion import RRF_K, rrf_fuse
from vectapilot_api.retrieval.types import FusedHit, SearchHit

# A strict xfail while "rrf_fuse" is in PENDING_HAND_WRITE (tests/canary/read_paths.py): H2's PR
# removes the name once every case here shows XPASS(strict).
pytestmark = [*pending_hand_write("rrf_fuse")]

TEXT_MARKER: Final = "kq4fusion"
"""In every hit's content and title: an error message that shows it leaked chunk text."""


def hit(
    source_id: str,
    *,
    n: int,
    version: str = "1",
    chunk_index: int = 0,
    score: float = 0.5,
    title: str | None = None,
) -> SearchHit:
    """A made-up hit; `n` fixes its chunk id (`UUID(int=n)`), so id order is chosen per test."""
    return SearchHit(
        chunk_id=uuid.UUID(int=n),
        document_id=uuid.UUID(int=1000 + n),
        source_id=source_id,
        version=version,
        title=title or f"Title of {source_id} {TEXT_MARKER}",
        language="en",
        content_kind=ContentKind.FAQ,
        page=None,
        chunk_index=chunk_index,
        content=f"Text of {source_id} v{version} #{chunk_index} {TEXT_MARKER}",
        score=score,
        embedding_model="fake-embedding-1024",
    )


def order(fused: Sequence[FusedHit]) -> list[tuple[str, str, int]]:
    return [(f.hit.source_id, f.hit.version, f.hit.chunk_index) for f in fused]


def names(fused: Sequence[FusedHit]) -> list[str]:
    return [f.hit.source_id for f in fused]


def rrf_scores(fused: Sequence[FusedHit]) -> list[Fraction]:
    return [f.rrf_score for f in fused]


def all_ranks(fused: Sequence[FusedHit]) -> list[tuple[int | None, ...]]:
    return [f.ranks for f in fused]


# ── The worked example (the plan's, at k = 1) ───────────────────────────────────────────────

# Names: alphabetical gives a, m, q, z (B, A, D, C); chunk-id order gives B, D, A, C. The
# expected order is A, C, B, D: neither forwards nor backwards.
A: Final = hit("m-hours", n=3, score=0.25)
B: Final = hit("a-fees", n=1, score=1.0)  # the best cosine: it must not matter
C: Final = hit("z-parking", n=4, score=0.75)
D: Final = hit("q-vaccines", n=2, score=0.5)
VECTOR: Final = (A, B, C)
# C and A again, equal but built separately: in PR 4 each leg builds its own SearchHit for one
# chunk, so a second sighting must be compared with `==`, never `is`.
TEXT: Final = (C.model_copy(), A.model_copy(), D)


def test_the_worked_example_at_k_1() -> None:
    fused = rrf_fuse([VECTOR, TEXT], k=1, limit=10)

    # A: 1/(1+1) + 1/(1+2) = 1/2 + 1/3 = 5/6     (rank 1 in VECTOR, 2 in TEXT)
    # C: 1/(1+3) + 1/(1+1) = 1/4 + 1/2 = 3/4     (rank 3 in VECTOR, 1 in TEXT)
    # B: 1/(1+2)           = 1/3                 (rank 2 in VECTOR only)
    # D:           1/(1+3) = 1/4                 (rank 3 in TEXT only)
    assert [f.hit for f in fused] == [A, C, B, D]  # each hit unchanged
    assert rrf_scores(fused) == [Fraction(5, 6), Fraction(3, 4), Fraction(1, 3), Fraction(1, 4)]
    assert all(type(f.rrf_score) is Fraction for f in fused)
    assert all_ranks(fused) == [(1, 2), (3, 1), (2, None), (None, 3)]


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(1, [A]), (2, [A, C]), (3, [A, C, B]), (4, [A, C, B, D]), (10, [A, C, B, D])],
    ids=["limit=1", "limit=2", "limit=3", "limit=4-all", "limit=10-more-than-there-are"],
)
def test_the_limit_keeps_the_best(limit: int, expected: list[SearchHit]) -> None:
    fused = rrf_fuse([VECTOR, TEXT], k=1, limit=limit)

    assert [f.hit for f in fused] == expected  # the best `limit`, never padded


def test_the_limit_cuts_the_fused_list_never_the_input_lists() -> None:
    # k = 1, limit = 1. Lists: [a-top-of-one, b-in-both] and [b-in-both].
    #   b-in-both:    ranks 2, 1     1/3 + 1/2 = 5/6
    #   a-top-of-one: rank 1 only    1/2
    # Cutting each list to `limit` first would leave [a-top-of-one] and [b-in-both], 1/2 each with
    # best rank 1, and the name would then pick a-top-of-one.
    top, both = hit("a-top-of-one", n=1, score=1.0), hit("b-in-both", n=2)

    fused = rrf_fuse([[top, both], [both]], k=1, limit=1)

    assert [f.hit for f in fused] == [both]
    assert rrf_scores(fused) == [Fraction(5, 6)]
    assert all_ranks(fused) == [(2, 1)]


def test_swapping_the_lists_changes_only_the_ranks() -> None:
    fused = rrf_fuse([TEXT, VECTOR], k=1, limit=10)

    assert [f.hit for f in fused] == [A, C, B, D]
    assert rrf_scores(fused) == [Fraction(5, 6), Fraction(3, 4), Fraction(1, 3), Fraction(1, 4)]
    assert all_ranks(fused) == [(2, 1), (1, 3), (None, 2), (3, None)]  # follow the lists' order


def test_the_default_k_is_60() -> None:
    fused = rrf_fuse([VECTOR, TEXT], limit=10)

    # A: 1/61 + 1/62 = (62 + 61)/(61·62) = 123/3782;  C: 1/63 + 1/61 = (61 + 63)/(61·63) = 124/3843
    # A > C because 1/62 > 1/63; B: 1/62 > D: 1/63.
    assert RRF_K == 60
    assert [f.hit for f in fused] == [A, C, B, D]
    assert rrf_scores(fused) == [
        Fraction(123, 3782),
        Fraction(124, 3843),
        Fraction(1, 62),
        Fraction(1, 63),
    ]


def test_only_ranks_count_never_a_hits_own_score() -> None:
    # One list, cosines rising down the list: the fused order is the list's order anyway.
    low, mid, high = hit("c-low", n=1, score=-0.5), hit("b-mid", n=2), hit("a-high", n=3, score=1.0)

    fused = rrf_fuse([[low, mid, high]], limit=3)

    assert [f.hit for f in fused] == [low, mid, high]
    assert rrf_scores(fused) == [Fraction(1, 61), Fraction(1, 62), Fraction(1, 63)]
    assert all_ranks(fused) == [(1,), (2,), (3,)]


# ── Ties ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("swap", [False, True], ids=["as-given", "lists-swapped"])
def test_an_equal_score_and_best_rank_is_decided_by_source_id_byte_by_byte(swap: bool) -> None:
    # Both 1/(60+1) = 1/61, both best rank 1. Byte order: "B" is 0x42, "a" is 0x61, so B-prices
    # comes first; a case-insensitive or linguistic comparison would put a-hours first, and so
    # would the input order (a-hours's list is given first), the chunk ids and the cosines (a
    # hit's own score never breaks a tie either).
    hours, prices = hit("a-hours", n=1, score=0.9), hit("B-prices", n=2, score=0.1)
    lists = [[prices], [hours]] if swap else [[hours], [prices]]

    fused = rrf_fuse(lists, limit=10)

    assert names(fused) == ["B-prices", "a-hours"]
    assert rrf_scores(fused) == [Fraction(1, 61), Fraction(1, 61)]


def test_an_equal_score_is_decided_by_the_best_single_rank_before_the_source_id() -> None:
    # k = 1. Lists: [c-first, e-second, a-both] and [b-first, d-second, a-both].
    #   c-first:  rank 1 in the first only            1/2
    #   b-first:  rank 1 in the second only           1/2
    #   a-both:   rank 3 in both     1/4 + 1/4      = 1/2   (best single rank 3)
    #   e-second: rank 2 in the first only            1/3
    #   d-second: rank 2 in the second only           1/3
    # Three tie at 1/2: best ranks 1, 1 and 3, so a-both comes last of them although its name
    # sorts first and its cosine is the best; b-first and c-first then go by name. d-second and
    # e-second tie at 1/3 with best rank 2 each: by name, against the input order.
    c_first, e_second = hit("c-first", n=5), hit("e-second", n=4)
    a_both = hit("a-both", n=3, score=1.0)
    b_first, d_second = hit("b-first", n=2), hit("d-second", n=1)

    fused = rrf_fuse([[c_first, e_second, a_both], [b_first, d_second, a_both]], k=1, limit=10)

    assert names(fused) == ["b-first", "c-first", "a-both", "d-second", "e-second"]
    assert rrf_scores(fused) == [
        Fraction(1, 2),
        Fraction(1, 2),
        Fraction(1, 2),
        Fraction(1, 3),
        Fraction(1, 3),
    ]
    assert all_ranks(fused) == [(None, 1), (1, None), (3, 3), (None, 2), (2, None)]


def test_then_the_version_byte_by_byte_then_the_chunk_index_as_a_number() -> None:
    # Three lists of one hit each: 1/61 and best rank 1 for all three, one source. Versions: "V3"
    # before "v2" ("V" is 0x56, "v" is 0x76); chunk index 2 before 10 (a number, not the text
    # "10" < "2"). Given in the opposite order, with chunk ids in the opposite order too.
    v2_0 = hit("prices", n=1, version="v2", chunk_index=0)
    v3_10 = hit("prices", n=2, version="V3", chunk_index=10)
    v3_2 = hit("prices", n=3, version="V3", chunk_index=2)

    fused = rrf_fuse([[v2_0], [v3_10], [v3_2]], limit=10)

    assert order(fused) == [("prices", "V3", 2), ("prices", "V3", 10), ("prices", "v2", 0)]
    assert all_ranks(fused) == [(None, None, 1), (None, 1, None), (1, None, None)]


# The 47/60 case, at k = 1: P, X, Y, Q below. Shared by the next two tests.
P_EVERYWHERE: Final = hit("m-everywhere", n=4)
X_EXACT: Final = hit("a-exact", n=3, score=0.25)
Y_EXACT: Final = hit("b-exact", n=2, score=1.0)  # a better cosine than a-exact: it must not matter
Q_TWO_LISTS: Final = hit("z-two-lists", n=1)
EXACT_LISTS: Final = (
    (P_EVERYWHERE, X_EXACT, Y_EXACT),
    (P_EVERYWHERE, Q_TWO_LISTS, X_EXACT, Y_EXACT),
    (P_EVERYWHERE, Y_EXACT, Q_TWO_LISTS, X_EXACT),
)
EXACT_RANKS: Final = [(1, 1, 1), (2, 3, 4), (3, 4, 2), (None, 2, 3)]
"""P, X, Y, Q's ranks in the three lists, as given (worked out in the next test)."""


def test_scores_are_exact_fractions_so_float_rounding_never_decides_a_tie() -> None:
    # k = 1. Lists: [P, X, Y], [P, Q, X, Y], [P, Y, Q, X].
    #   P (m-everywhere): rank 1 in all three   1/2 + 1/2 + 1/2         = 3/2
    #   X (a-exact):      ranks 2, 3, 4         1/3 + 1/4 + 1/5         = 47/60
    #   Y (b-exact):      ranks 3, 4, 2         1/4 + 1/5 + 1/3         = 47/60 (the same terms)
    #   Q (z-two-lists):  ranks -, 2, 3         1/3 + 1/4               = 7/12 = 35/60
    # X and Y tie exactly, with best rank 2 each, so the name decides: a-exact first (b-exact's
    # better cosine does not count). As floats added one list at a time (a running total,
    # `total += 1 / (k + rank)`) they do not tie: X gives 0.7833333333333332 and Y
    # 0.7833333333333333, so that would put Y first. Python's built-in sum() of the same floats is
    # compensated (Neumaier summation, since Python 3.12), and so is math.fsum: both give
    # 0.7833333333333333 twice here, so this case cannot see them. The next test can.
    fused = rrf_fuse(EXACT_LISTS, k=1, limit=10)

    assert names(fused) == ["m-everywhere", "a-exact", "b-exact", "z-two-lists"]
    assert rrf_scores(fused) == [
        Fraction(3, 2),
        Fraction(47, 60),
        Fraction(47, 60),
        Fraction(7, 12),
    ]
    assert all(type(f.rrf_score) is Fraction for f in fused)
    assert all_ranks(fused) == [(1, 1, 1), (2, 3, 4), (3, 4, 2), (None, 2, 3)]


def test_scores_tie_exactly_where_even_a_compensated_float_sum_does_not() -> None:
    # k = 1. Lists: [b-ones, a-twos], [b-ones, a-twos] and [a-twos, x2, x3, x4, b-ones].
    #   a-twos: ranks 2, 2, 1    1/3 + 1/3 + 1/2   = 7/6
    #   b-ones: ranks 1, 1, 5    1/2 + 1/2 + 1/6   = 7/6
    #   x2, x3, x4: ranks 2, 3, 4 in the third list only: 1/3, 1/4, 1/5
    # a-twos and b-ones tie exactly, with best rank 1 each, so the name decides: a-twos first
    # (against b-ones's better cosine, smaller chunk id and first place in the input). As floats
    # they do not tie however they are added: built-in sum(), math.fsum and a running total all
    # give 1.1666666666666665 for a-twos and 1.1666666666666667 for b-ones, and would put b-ones
    # first. The error is in the terms, before any sum: 1/6 as a float is exactly half of 1/3 as
    # a float, which is 1/3 - d (d ≈ 1.85e-17), so b-ones's float terms add up to exactly 7/6 - d/2
    # and a-twos's to 7/6 - 2d, and even a correctly rounded sum of them gives two floats.
    a_twos, b_ones = hit("a-twos", n=2, score=0.25), hit("b-ones", n=1, score=1.0)
    x2, x3, x4 = hit("x2", n=3), hit("x3", n=4), hit("x4", n=5)
    lists = [[b_ones, a_twos], [b_ones, a_twos], [a_twos, x2, x3, x4, b_ones]]

    fused = rrf_fuse(lists, k=1, limit=10)

    assert names(fused) == ["a-twos", "b-ones", "x2", "x3", "x4"]
    assert rrf_scores(fused) == [
        Fraction(7, 6),
        Fraction(7, 6),
        Fraction(1, 3),
        Fraction(1, 4),
        Fraction(1, 5),
    ]
    assert all_ranks(fused) == [
        (2, 2, 1),
        (1, 1, 5),
        (None, None, 2),
        (None, None, 3),
        (None, None, 4),
    ]


LIST_ORDERS: Final = list(itertools.permutations(range(3)))


@pytest.mark.parametrize(
    "list_order", LIST_ORDERS, ids=["lists-" + "".join(map(str, o)) for o in LIST_ORDERS]
)
def test_ties_never_depend_on_the_order_of_the_lists(list_order: tuple[int, ...]) -> None:
    # The 47/60 lists in all six orders: the same names and scores every time, and each hit's
    # ranks follow its lists. a-exact and b-exact still tie at 47/60 with best rank 2, so the name
    # decides. A body that kept each chunk's rank from where it first saw it (no minimum) would
    # see b-exact at rank 2 and a-exact at rank 4 when [P, Y, Q, X] comes first, and put b-exact
    # first.
    fused = rrf_fuse([EXACT_LISTS[i] for i in list_order], k=1, limit=10)

    assert names(fused) == ["m-everywhere", "a-exact", "b-exact", "z-two-lists"]
    assert rrf_scores(fused) == [
        Fraction(3, 2),
        Fraction(47, 60),
        Fraction(47, 60),
        Fraction(7, 12),
    ]
    assert all_ranks(fused) == [tuple(ranks[i] for i in list_order) for ranks in EXACT_RANKS]


def test_the_best_single_rank_is_the_lowest_in_any_list_not_a_sum_or_the_first_seen() -> None:
    # k = 1. Lists: [x1, q-two, x3, x4, z-one] and [z-one, q-two].
    #   z-one: ranks 5, 1    1/6 + 1/2 = 4/6 = 2/3   (best single rank 1)
    #   q-two: ranks 2, 2    1/3 + 1/3       = 2/3   (best single rank 2)
    #   x1:    rank 1        1/2
    #   x3:    rank 3        1/4
    #   x4:    rank 4        1/5
    # z-one and q-two tie at 2/3, and z-one's best rank, 1, beats q-two's 2: z-one comes first,
    # although its name sorts later, its rank sum is higher (6 against 4), and the first list sees
    # q-two (rank 2) before z-one (rank 5).
    x1, q_two, x3, x4 = hit("x1", n=4), hit("q-two", n=1), hit("x3", n=5), hit("x4", n=3)
    z_one = hit("z-one", n=2)

    fused = rrf_fuse([[x1, q_two, x3, x4, z_one], [z_one, q_two]], k=1, limit=10)

    assert names(fused) == ["z-one", "q-two", "x1", "x3", "x4"]
    assert rrf_scores(fused) == [
        Fraction(2, 3),
        Fraction(2, 3),
        Fraction(1, 2),
        Fraction(1, 4),
        Fraction(1, 5),
    ]
    assert all_ranks(fused) == [(5, 1), (2, 2), (1, None), (3, None), (4, None)]


def test_scores_stay_exact_where_a_float_turned_back_into_a_fraction_would_not() -> None:
    # k = 1000. Lists: [x-two-lists] and [y-one-list, x-two-lists].
    #   x-two-lists: ranks 1, 2    1/1001 + 1/1002 = (1002 + 1001)/(1001·1002) = 2003/1003002
    #   y-one-list:  rank -, 1     1/1001
    # 2003 is prime and 1003002 = 2003·500 + 1502, so 2003/1003002 is already reduced, and its
    # denominator is above 10**6: a float sum turned back with `Fraction(...).limit_denominator()`
    # (whose default limit is 10**6) cannot come back to it.
    x, y = hit("x-two-lists", n=1), hit("y-one-list", n=2)

    fused = rrf_fuse([[x], [y, x]], k=1000, limit=10)

    assert [f.hit for f in fused] == [x, y]
    assert rrf_scores(fused) == [Fraction(2003, 1003002), Fraction(1, 1001)]
    assert all(type(f.rrf_score) is Fraction for f in fused)
    assert all_ranks(fused) == [(1, 2), (None, 1)]


# ── Duplicates, empty input, arguments ──────────────────────────────────────────────────────


def test_the_same_hit_in_two_lists_is_one_fused_hit() -> None:
    # Equal but another object, as PR 4's two legs return one chunk: compared with `==`.
    again = A.model_copy()
    assert again == A and again is not A, "premise: an equal hit, built separately"

    fused = rrf_fuse([[A], [again]], limit=10)

    assert [f.hit for f in fused] == [A]
    assert rrf_scores(fused) == [Fraction(2, 61)]  # 1/61 + 1/61
    assert all_ranks(fused) == [(1, 1)]


@pytest.mark.parametrize(
    "lists",
    [
        [[A, B, A]],  # the same hit twice in one list
        [[A, B], [C, D, A.model_copy(update={"chunk_index": 7})]],  # dup id, unequal, other list
        [[A, A.model_copy(update={"score": 0.0})]],  # one chunk id, two hits, one list
        [[A], [A.model_copy(update={"score": 0.0})]],  # another cosine: another model?
        [[A], [A.model_copy(update={"title": f"Another title {TEXT_MARKER}"})]],  # another version?
    ],
    ids=[
        "twice-in-one-list",
        "other-chunk-index",
        "two-hits-one-list",
        "other-cosine",
        "other-title",
    ],
)
def test_one_chunk_id_twice_in_a_list_or_as_two_different_hits_raises(
    lists: list[list[SearchHit]],
) -> None:
    with pytest.raises(ValueError) as error:
        rrf_fuse(lists, limit=10)

    assert TEXT_MARKER not in str(error.value), "the message must not show a hit's text or title"


@pytest.mark.parametrize(
    "lists", [[], [[]], [[], [], []]], ids=["no-lists", "one-empty", "three-empty"]
)
def test_no_lists_or_only_empty_ones_give_an_empty_list(lists: list[list[SearchHit]]) -> None:
    assert rrf_fuse(lists, limit=10) == []


def test_an_empty_list_next_to_a_full_one_is_a_list_that_holds_nothing() -> None:
    fused = rrf_fuse([[], [B, A]], limit=10)

    assert [f.hit for f in fused] == [B, A]
    assert rrf_scores(fused) == [Fraction(1, 61), Fraction(1, 62)]
    assert all_ranks(fused) == [(None, 1), (None, 2)]


@pytest.mark.parametrize(
    ("k", "limit"),
    [(0, 10), (-1, 10), (60, 0), (60, -1)],
    ids=["k=0", "k=-1", "limit=0", "limit=-1"],
)
def test_k_and_limit_below_1_raise(k: int, limit: int) -> None:
    with pytest.raises(ValueError):
        rrf_fuse([VECTOR, TEXT], k=k, limit=limit)


def test_the_input_is_never_changed_and_any_sequence_is_accepted() -> None:
    vector, text = list(VECTOR), list(TEXT)
    lists = [vector, text]

    from_lists = rrf_fuse(lists, k=1, limit=10)
    from_tuples = rrf_fuse((VECTOR, TEXT), k=1, limit=10)

    assert lists == [list(VECTOR), list(TEXT)]
    assert lists[0] is vector and lists[1] is text
    assert from_lists == from_tuples


# ── Not even the hash seed may decide (golden case under two PYTHONHASHSEED values) ────────

API_DIR: Final = Path(__file__).parents[2]


def golden_lists() -> list[list[SearchHit]]:
    """[A, B, C] and [C, A, D] at k = 60, then four one-hit lists whose hits tie with each
    other: [t-4], [t-3], [t-2], [t-1] (given in reverse name order, ids in reverse too)."""
    a, b = hit("m-hours", n=3), hit("a-fees", n=1)
    c, d = hit("z-parking", n=4), hit("q-vaccines", n=2)
    ties = [hit(f"t-{i}", n=10 - i) for i in (4, 3, 2, 1)]
    return [[a, b, c], [c, a, d], *[[tie] for tie in ties]]


GOLDEN: Final = [
    ["m-hours", "123/3782"],  # 1/61 + 1/62 (ranks 1, 2)
    ["z-parking", "124/3843"],  # 1/63 + 1/61 (ranks 3, 1)
    ["t-1", "1/61"],  # four ties at 1/61 with best rank 1: by name
    ["t-2", "1/61"],
    ["t-3", "1/61"],
    ["t-4", "1/61"],
    ["a-fees", "1/62"],  # rank 2 in the first list only
    ["q-vaccines", "1/63"],  # rank 3 in the second list only
]

PROBE: Final = """
import json
from tests.retrieval.test_fusion import golden_lists
ties = [lst[0] for lst in golden_lists()[2:]]
print(json.dumps([[h.source_id for h in set(ties)], list({h.source_id for h in ties})]))
"""
"""How a set of the tied hits, and a set of their names, iterate under this hash seed."""

GOLDEN_SCRIPT: Final = """
import json
from tests.retrieval.test_fusion import golden_lists
from vectapilot_api.retrieval.fusion import rrf_fuse
try:
    fused = rrf_fuse(golden_lists(), limit=10)
except NotImplementedError:
    print(json.dumps("not implemented"))
else:
    print(json.dumps([[f.hit.source_id, str(f.rrf_score)] for f in fused]))
"""


def _run_with_hash_seed(seed: int, code: str) -> str:
    """`code` in a fresh interpreter (this one's) with PYTHONHASHSEED fixed; its stdout."""
    done = subprocess.run(  # noqa: S603 (this interpreter, fixed code, no shell)
        [sys.executable, "-c", code],
        cwd=API_DIR,
        env={**os.environ, "PYTHONHASHSEED": str(seed)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout


def test_the_golden_case_is_the_same_under_two_hash_seeds() -> None:
    # A set of hits or of names iterates in an order that changes with PYTHONHASHSEED (a hit's
    # hash includes its strings), and one test run sees only one seed. So two seeds under which
    # both sets iterate differently are found first: a fusion that let a set's order decide a tie
    # would give two different lists for the golden case under them.
    seeds: dict[str, int] = {}
    first: list[list[str]] | None = None
    for seed in range(64):
        orders = json.loads(_run_with_hash_seed(seed, PROBE))
        if first is None:
            first = orders
            seeds["first"] = seed
        elif orders[0] != first[0] and orders[1] != first[1]:
            seeds["second"] = seed
            break
    assert len(seeds) == 2, "no two hash seeds in 0-63 iterate both sets differently"

    results = [json.loads(_run_with_hash_seed(seed, GOLDEN_SCRIPT)) for seed in seeds.values()]
    if "not implemented" in results:
        raise NotImplementedError("rrf_fuse (in the golden case's interpreters)")
    assert results == [GOLDEN, GOLDEN]
