"""H2 — retrieval metrics: hit@k and reciprocal rank / MRR. [HAND-WRITE], written by Claude at
shesha's request (2026-10-22); shesha's explain-back is still part of acceptance.

Guide: docs/learning/m2-metrics.md · Tests: tests/evalset/test_metrics.py, plain tests now
("retrieval_metrics" is out of `PENDING_HAND_WRITE` in tests/canary/read_paths.py).

**Input.** One `ranking.RankedRelevance` per question, already checked when it was built: per rank
of the search's ranked list (before any score threshold, `ranking.ranked_relevance`), the
evidence groups that hit finds (`relevance.matched_groups`), and the question's number of groups
(at least 1). Questions without evidence never get a record: `ranking.select_for_retrieval` lists
them as not scored. Which questions, which split and which k are the caller's choice (PR 8's
harness, which also adds the counts and intervals a report needs). These functions only compute.

**Rules shared by every function here** (each docstring adds its own):

- **Ranks start at 1.** The hit at rank r is `groups_by_rank[r - 1]`.
- **Relevant at a rank** means that hit finds at least one evidence group (a non-empty tuple).
  Which group, and how many, does not matter for `hit_at_k` and `reciprocal_rank`.
- **The cut-off `k`.** Only ranks 1 to k count. `k` is an int of at least 1, else `ValueError`
  (exactly what `ranking.check_k` checks), raised before anything is computed. A `k` larger than
  the list is allowed: a search returns fewer hits than asked when fewer chunks qualify, and
  ranks past the end of the list are simply not relevant. (A larger k than the search was run
  with would report a shallow search under a deeper label; PR 8 asserts, per run, that the
  metric's k is at most the search's k.)
- **Exact values.** Reciprocal ranks and means are `fractions.Fraction`, also when the value is 0
  or 1: 1/3 stays exactly 1/3, so results are compared with `==`, and the order of the questions
  cannot change a mean by a rounding unit. The report converts to `float` for display only. A
  `Fraction` is reduced (2/4 is stored as 1/2), so a mean does not tell how many questions it
  rests on: the caller reports n from its own count.
- **Pure.** Inputs are never modified; nothing is printed or logged; the same input always gives
  the same output.
"""

from collections.abc import Sequence
from fractions import Fraction

from vectapilot_api.evalset.ranking import RankedRelevance, check_k, check_results


def hit_at_k(result: RankedRelevance, k: int) -> bool:
    """Whether at least one of the question's evidence groups is found in the top `k` hits.

    Contract:

    - **True** when at least one of ranks 1 to `k` is relevant (finds any group); **False**
      otherwise: no relevant hit at all, the first relevant hit past rank `k`, or an empty list.
    - **Several groups: any one is enough** (the recommended default, guide §5). A `multi_hop`
      question whose top `k` finds 1 of its 2 groups is a hit. `all_groups_at_k` is the stricter
      measure, a separate function.
    - **Repeats change nothing.** The same group found at several ranks, or several groups found
      by one hit, still give `True`, never a count.
    - **Monotone in k.** A hit at `k` is a hit at every larger `k`.
    - **Type.** A `bool`, exactly (`True`/`False`, not 1/0).
    - **Errors.** `ValueError` when `k` is not an int of at least 1 (a bool is refused), before
      anything is computed.
    """
    check_k(k)
    # Slicing never fails past the end; an empty tuple (an irrelevant hit) is falsy.
    return any(result.groups_by_rank[:k])


def all_groups_at_k(result: RankedRelevance, k: int) -> bool:
    """Whether every one of the question's evidence groups is found in the top `k` hits.

    Contract:

    - **True** when each group index from 0 to `n_groups - 1` is found by at least one of ranks 1
      to `k`; **False** otherwise (an empty list included).
    - **Distinct groups count, not relevant hits.** With 2 groups, group 0 found at rank 1 and
      again at rank 2 is two relevant hits but one group: False.
    - **Order does not matter.** Group 1 before group 0 counts the same as group 0 before group 1.
      One hit that finds two groups finds both.
    - **For a question with one group** it equals `hit_at_k`. For any question, `True` here means
      `hit_at_k` is `True` too, and it is monotone in `k`.
    - **Why it exists.** A `multi_hop` answer needs every group, and an `ambiguous` one needs the
      group of every reading, so "any group" can call a retrieval good that cannot support the
      answer. On the search's ranked list it says whether retrieval found every group; whether
      the prompt then held them also depends on the score threshold (guide §2.1). How PR 8
      reports it (recommended: as a count `x of n` for questions with 2 or more groups, next to
      hit@k) is open, guide §5.
    - **Type and errors.** A `bool`, exactly. `ValueError` for a bad `k`, as in `hit_at_k`.
    """
    check_k(k)
    # A set keeps each group once, however many hits find it.
    found = {group for groups in result.groups_by_rank[:k] for group in groups}
    return found == set(range(result.n_groups))


def reciprocal_rank(result: RankedRelevance, k: int) -> Fraction:
    """1 divided by the rank of the first relevant hit within the top `k`; 0 when there is none.

    Contract:

    - **Value.** `Fraction(1, r)` where r is the smallest rank from 1 to `k` whose hit is relevant
      (finds any group): rank 1 gives 1, rank 3 gives 1/3. `Fraction(0)` when none of ranks 1 to
      `k` is relevant, also when the list is empty.
    - **Cut off at k (this is RR@k).** A relevant hit at rank `k + 1` or later gives 0, the same as
      no relevant hit at all. With `k` larger than the list, the whole list is searched.
    - **Only the first relevant hit counts.** Later relevant hits add nothing: not a second group,
      not the same group again. With several groups this is "any group" by construction: the
      first rank that finds any of them.
    - **Bounds.** Always 0 <= value <= 1; a value above 0 is at least 1/k. It is above 0 exactly
      when `hit_at_k(result, k)` is True, and it never goes down as `k` grows.
    - **Type and errors.** A `Fraction`, also for 0 and 1. `ValueError` for a bad `k`, as in
      `hit_at_k`.
    """
    check_k(k)
    for rank, groups in enumerate(result.groups_by_rank[:k], start=1):  # ranks start at 1
        if groups:
            return Fraction(1, rank)
    return Fraction(0)


def mean_hit_at_k(results: Sequence[RankedRelevance], k: int) -> Fraction:
    """hit@k over a set of questions: the number of questions with `hit_at_k` True, divided by the
    number of questions.

    Contract:

    - **n is every question given**, once each: a question with no relevant hit, or with an empty
      hit list, counts in n as a miss. Nothing is skipped.
    - **Errors.** `ValueError` before anything is computed when `k` is bad (as in `hit_at_k`), when
      `results` is empty (a mean over no questions is undefined: never 0, never
      `ZeroDivisionError`), or when two results name the same question (it would weigh double;
      the message names it). `ranking.check_results` checks the last two.
    - **Order does not matter**: any order of the same questions gives the same value.
    - **Type and bounds.** An exact `Fraction` from 0 to 1 (the module docstring: n is not
      recoverable from it, so the report states n itself).
    """
    check_k(k)
    check_results(results)
    hits = sum(hit_at_k(result, k) for result in results)  # True counts as 1
    return Fraction(hits, len(results))  # n is every question, misses included


def mean_reciprocal_rank(results: Sequence[RankedRelevance], k: int) -> Fraction:
    """MRR@k: the mean of `reciprocal_rank(result, k)` over a set of questions.

    Contract:

    - **n is every question given**, once each: a question whose reciprocal rank is 0 counts in n.
      Dividing only by the questions with a relevant hit is the classic mistake (guide §3).
    - **Errors, order, type and bounds** as in `mean_hit_at_k`. In addition, for the same
      questions and `k`, MRR@k is never above hit@k (each reciprocal rank is at most 1 and is 0
      exactly when that question has no hit), and at `k = 1` the two are equal.
    """
    check_k(k)
    check_results(results)
    total = sum((reciprocal_rank(result, k) for result in results), Fraction(0))
    return total / len(results)  # Fraction / int stays exact; n includes every 0
