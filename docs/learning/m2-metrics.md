# M2 hand-write: retrieval metrics, hit@k and MRR (H2)

This is M2's second `[HAND-WRITE]` piece. Your H1 search returns a ranked list of chunks;
`evalset/relevance.py` (PR 7) says which of a question's evidence groups each chunk finds. Your
functions turn that into the two retrieval numbers M2 reports: **hit@k** (did the evidence make it
into the top k at all?) and **MRR** (how high up was it?). noah's harness (PR 8) runs them on dev
while tuning and on test once per reported configuration, and adds what a report needs around
them (counts, intervals, dataset version, commit, config); your live run (PR 9) reports them. The functions are pure Python over small tuples: no
database, no network, no model.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Your branch |
|---|---|---|---|---|---|
| **H2** | `hit_at_k`, `all_groups_at_k`, `reciprocal_rank`, `mean_hit_at_k` and `mean_reciprocal_rank` in [`evalset/metrics.py`](../../apps/api/src/vectapilot_api/evalset/metrics.py) | [`tests/evalset/test_metrics.py`](../../apps/api/tests/evalset/test_metrics.py): 38 tests, 142 cases (no database) | about 10–25 lines (all five together) | 2 h (proposal, §5) | `m2/metrics` |

- **The docstring is the spec.** The module docstring states the rules every function shares
  (1-based ranks, what "relevant" means, the cut-off `k`, exact `Fraction` values, purity); each
  function's docstring adds its own. The tests are that contract made executable, with every
  expected value worked out by hand in a comment. Read the docstrings first, then the tests.
- **Already there and tested** ([`evalset/ranking.py`](../../apps/api/src/vectapilot_api/evalset/ranking.py),
  [`tests/evalset/test_ranking.py`](../../apps/api/tests/evalset/test_ranking.py): 20 tests,
  38 cases, passing): `RankedRelevance`, the record you receive (checked when built: at least one
  group, group indexes in range, ascending, each once per rank, tuples only);
  `ranked_relevance`, which builds it from a question and its hits with
  `relevance.matched_groups`; `select_for_retrieval` and `has_evidence`, which list the questions
  without evidence as *not scored* instead of dropping them; `check_k` (an int of at least 1, a
  bool refused) and `check_results` (non-empty, each question once). Call the two checks; do not
  rewrite them.
- **Not your job** (PR 8): running the search, choosing the split, the questions and `k`,
  breaking results down by business, language and category, intervals, the report, the leak
  check, and keys that are unscorable at a chunk size. Your functions see only `RankedRelevance`
  records and `k`.

## 1. How to work

> **These tests run locally.** No Docker, no Postgres: `uv run pytest tests/evalset/test_metrics.py`
> takes about a second. CI runs the same module; there is no integration part.

```bash
cd apps/api
git switch main && git pull                  # once the scaffold PR is merged
git switch -c m2/metrics
uv run pytest tests/evalset/test_ranking.py tests/evalset/test_metrics.py -v
#   ranking: 38 passed · metrics: 142 xfailed
# write the bodies …
uv run pytest tests/evalset/test_metrics.py -v
uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .
```

**Reading the output (strict xfail, as in M1 and H1).** `test_metrics.py` starts with
`pytestmark = pytest.mark.xfail(strict=True, raises=NotImplementedError, …)`. While it is there:

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but gives a wrong answer or raises something else | `FAILED`, with the assert diff (only `NotImplementedError` counts as expected) |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

The five functions share one module, so you can write them one at a time: a test that calls only
finished functions turns `XPASS(strict)` or `FAILED`, the rest stay `xfailed`. A suggested order:
`hit_at_k`, `reciprocal_rank`, `all_groups_at_k`, then the two means (they use the first two).
The property tests near the end call several functions and stay `xfailed` until all of those are
done. When every case shows `XPASS(strict)`, **delete the `pytestmark = …` statement** (the only
marker; H2 has no canary entry) and run again: every test must then simply pass.

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. `mypy` (strict) and `ruff` apply as everywhere. Commit with your own message (for
example `feat(evals): implement hit@k and MRR`), push, open a PR and ask `@agent-noah-evals` to
review it (`@agent-kenji-ml` is welcome on the definitions). Then explain it back (§6).

## 2. The concepts (hint level 1)

### 2.1 What you get: ranked groups, not chunks
A question's expected evidence is a list of **groups**; any key of a group finds it (ADR 0012).
`RankedRelevance.groups_by_rank` has one entry per retrieved hit, best first: the indexes of the
groups that hit finds, or `()` for an irrelevant hit. A `multi_hop` question with two groups
(0 = fact A in one document, 1 = fact B in another) and five hits might look like this:

| Rank | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Position in the tuple | 0 | 1 | 2 | 3 | 4 |
| Groups found | `()` | `(1,)` | `()` | `(0,)` | `()` |

Read the second row twice: **rank r lives at position r − 1**. The record carries no text, score
or chunk id, because the metrics need none of them. The same group may appear at several ranks
(two chunks can quote the same sentence; `faq` and `faq-hi` hold the same fact), and one hit can
find two groups at once.

**Which hit list.** PR 6 keeps two: `AnswerRun.hits`, everything the search returned (best first,
`rag_context_k` of them at most), and `AnswerRun.evidence`, only the hits scoring at least
`rag_min_score`, which are the chunks the prompt held. The records are built from the first one
(`AnswerRun.hits`, or `SearchResult.hits` from the search alone), **before the threshold**. So
hit@k and MRR@k measure retrieval alone: tuning `rag_min_score` on dev changes what the answer
does, never these two numbers.

### 2.2 hit@k vs recall@k vs precision@k
Let rel(r) mean "the hit at rank r finds at least one group", and G the question's groups.

- **hit@k** asks *did anything useful make it into the top k?* For one question it is 1 if
  rel(r) holds for some r ≤ k, else 0. Averaged over questions it is the share of questions with
  a hit, also called success@k. It is the playbook's retrieval number because an answer needs at
  least one passage to stand on.
- **recall@k** asks *how much of the evidence is in the top k?* For one question:
  |groups found in ranks 1..k| / |G|. With one group it equals hit@k. `all_groups_at_k` is the
  strict form of it, "recall@k = 1": every group is there.
- **precision@k** asks *how much of the top k is useful?* |relevant hits in ranks 1..k| / k. It
  counts hits, not groups, and it falls when k grows past the number of relevant chunks: with one
  relevant chunk, precision@5 is at most 1/5 however good the search is. That is why M2 does not
  use it as a headline.

In the table above: hit@1 = 0, hit@2 = 1; recall@2 = 1/2, recall@4 = 1; precision@5 = 2/5.

**Naming varies.** Some papers and libraries report hit@k as top-k accuracy, hit rate or even
recall@k, so check the definition before comparing a number with another system: an outside
"recall@5" may be this guide's hit@5, not its recall@5.

### 2.3 Reciprocal rank and MRR
The **reciprocal rank** of one question is 1/r*, where r* is the rank of its *first* relevant hit,
and 0 when there is none. **MRR** is its mean over the questions: MRR = (1/|Q|) Σ_q RR(q). In the
table above, RR = 1/2.

- **Only the first relevant hit counts.** RR models a reader who stops at the first useful result.
  A second relevant hit, a second group, the same group again: none of them changes RR. hit@k and
  recall@k are where "how much" shows up.
- **The scale is steep at the top.** Rank 1 → 1, rank 2 → 1/2, rank 3 → 1/3, rank 5 → 1/5. Moving
  the first relevant hit from rank 2 to 1 adds 1/2; moving it from 5 to 3 adds only 2/15. MRR
  rewards getting the best evidence to the very top, which is what a prompt that reads top-down
  benefits from.
- **The cut-off.** `reciprocal_rank(result, k)` is RR@k: a first relevant hit past rank k counts as
  0, exactly as if there were none. So MRR@k and hit@k are measured on the same top k, and RR@k is
  above 0 exactly when hit@k is 1. Which k M2 reports is a decision (§5, D2).

### 2.4 Several groups: any or all
For a question with one group, "found" is unambiguous. With two or more (`multi_hop`, and
`ambiguous`, which has a group per reading) there are two honest questions:

- **any group in the top k** (`hit_at_k`, and RR by construction): did retrieval find *something*
  relevant? The standard hit@k; comparable across categories, and with other systems once their
  definition is checked (§2.2).
- **all groups in the top k** (`all_groups_at_k`): did retrieval find *everything* the answer
  needs? A `multi_hop` answer built from one of its two facts is wrong or incomplete, so "any" can
  call a retrieval good that cannot support the answer.

Neither is "the right one"; they measure different things. The recommended default, the contract
and the open decision are in §5 (D1).

### 2.5 Why exact fractions
The values are ratios of small whole numbers, so `fractions.Fraction` holds them exactly and the
tests compare with `==`, no tolerance. With floats, the mean of the reciprocal ranks 1/3, 1/3 and
1/2 is `0.38888888888888884`, while 7/18 is `0.3888888888888889`: `==` says they differ (Python
3.12.15, run on 2026-10-22). `Decimal`, M1's choice for money, does not help here: it is exact for
decimal fractions like 0.1, but 1/3 has no finite decimal form. A `Fraction` is reduced (2/4 is
stored as 1/2), so the mean alone does not say how many questions it rests on; the report takes n
from its own count. Floats appear only when the report prints.

### 2.6 Small n: counts and intervals (PR 8's job, not yours)
At 40 questions the test split holds about 12 (ADR 0012), and a category 1–2. Not all 12 are
scored for retrieval: ADR 0012 keeps at least 8 of the 40 without evidence (§2.7), so at most
about 10 test questions are scored, fewer when a hand-off points at nothing. A made-up example,
not a result: 7 of 9 scored questions with a hit is "78 %", but a 95 % Wilson interval for 7 of 9
runs from about 45 % to 94 %; 8 of 9 (one more question) is "89 %". Reporting the bare percentage
invites reading noise as progress. So PR 8 could report "hit@5: 7 of 9 (78 %, 95 % interval
45–94 %); 3 not scored", with the dataset version, commit and config, and only counts (`x of n`)
for a category with one or two questions. That line is a proposal: whether PR 8 must show an
interval, and which method, is shesha's decision for PR 8's checks. "n scored, m not scored"
goes next to every retrieval number either way.

- **MRR is not a proportion.** It is a mean of values in {0, 1/k, …, 1}, so it has no `x of n`
  form, and a binomial interval such as Wilson does not apply. At small n PR 8 can show the
  first-relevant ranks as counts (rank 1: x, rank 2: y, …, none: z). How to put an interval on
  it (for example a bootstrap over questions or split groups) is PR 8's choice.
- **The questions are not independent.** A question, its translations and its cross-business
  twins share one `split_group`, so they always land in the same split. The effective n can be
  below the question count, and an interval that treats each question as independent is too
  narrow.

Your functions return the exact value; n and the interval are the harness's. Keep them apart: a
metric function that also rounds, prints or picks an interval method is three decisions in one
place.

### 2.7 Who is in n
- **A question without evidence groups is not scored.** For `dont_know`, `out_of_scope` and a
  hand-off that points to nothing there is nothing to find, so hit@k is *undefined* for it, not
  0 (counting it as 0 would punish retrieval for a correct "nothing here"; as 1, reward it). No
  `RankedRelevance` can exist for it, and `select_for_retrieval` lists it as not scored, so the
  report says "n scored, m not scored" and nothing disappears silently.
- **A question whose search returned nothing is scored**, as a miss: hit@k = 0, RR = 0.
- **A question that cannot be scored at this config is a third count.** If one of its groups has
  no key that any stored chunk holds (at a small chunk size a boundary can cut an anchor), PR 8
  takes it out before building records and reports it as "unscorable at this config", next to
  scored and not scored: a chunking artefact is not a retrieval miss. Your functions never see it.
- **Each question counts once.** Listed twice it would weigh double; `check_results` refuses it,
  by question id (two different records with the same id are still one question).

## 3. Common mistakes
- **0-based ranks.** `enumerate` starts at 0; ranks start at 1. A relevant first hit then divides
  by zero, and rank 3 gives 1/2.
- **The wrong n.** Dividing MRR by the number of questions *with* a hit (it then ignores every
  miss), by k, or by a count that includes the not-scored questions.
- **Skipping questions silently.** Leaving out a question with an empty hit list, or catching an
  error and moving on. Every record you are given is one question in n.
- **Float equality**, or returning a float or an int where the contract says `Fraction`. `0` and
  `Fraction(0)` compare equal, so only a type check catches it (some tests have one).
- **A float inside a `Fraction`.** `Fraction(a / b)` divides first, as floats, and converts the
  rounded float: `Fraction(2 / 3)` is not `Fraction(2, 3)`. Use `Fraction(a, b)`, or divide a
  `Fraction` by an int. `.limit_denominator()` does not repair it: it guesses the nearest fraction
  with a denominator of at most a million, right for 2/3, wrong for an MRR of 30191/3027273.
- **Counting relevant hits instead of distinct groups** for "all groups": `[0, 0]` is two
  relevant hits but one group.
- **Summing the reciprocal ranks of every relevant hit.** RR is the first one only.
- **Forgetting the cut-off** in RR: a first relevant hit at rank 6 is 0 at k = 5, not 1/6.
- **`True` as k.** Python treats `True` as 1; the contract refuses it.
- **Refusing k beyond the list.** A search may return fewer hits than asked; a slice past the end
  of a tuple is simply shorter in Python, no error, and that is the intended behaviour.
- **Returning 0 for an empty set**, or letting `ZeroDivisionError` escape: the contract is a
  `ValueError` from the check, before anything is computed.
- **Sorting or otherwise changing the caller's list.** The order of the questions never matters
  to a mean, so there is no reason to touch it.

## 4. Test by test, with level-2 hints
Open a hint only for the test you are stuck on, and only after §2. The records are written
`[rank 1, rank 2, …]` with `·` for an irrelevant hit; all ids are made up (`example-…`).

- **`hit_at_k`**: `test_a_relevant_first_hit_is_a_hit_at_every_k` (3 cases),
  `test_a_hit_at_rank_3_counts_from_k_3_on` (4), `test_no_relevant_hit_is_never_a_hit` (5),
  `test_k_beyond_the_list_looks_at_the_whole_list` (4),
  `test_a_relevant_hit_just_past_k_is_not_a_hit`, `test_any_one_group_is_enough_for_hit_at_k` (3),
  `test_a_group_found_twice_is_still_just_a_hit`.
  <details><summary>Hint</summary>

  If k = 1 fails on `FIRST`, your cut-off is one short. If a test says `1 is True` failed, you
  returned a number, not a bool. If `ONE_OF_TWO` fails, you are checking for all groups (§2.4).

  </details>
- **`all_groups_at_k`**: `test_all_groups_needs_every_group_in_the_top_k` (5),
  `test_one_group_of_two_is_not_all`, `test_two_relevant_hits_for_the_same_group_are_not_two_groups`,
  `test_as_many_relevant_hits_as_groups_are_not_all_groups`, `test_one_hit_can_find_every_group`, `test_the_order_the_groups_are_found_in_does_not_matter` (2),
  `test_three_groups_need_all_three` (4), `test_with_one_group_all_groups_is_hit_at_k` (7).
  <details><summary>Hint</summary>

  Think in sets: which distinct group indexes appear in the top k, and is that every index from 0
  to `n_groups − 1`? If `SAME_GROUP_TWICE` or `TWO_OF_THREE` passes as True, you are counting hits
  (§3).

  </details>
- **`reciprocal_rank`**: `test_reciprocal_rank_is_one_over_the_first_relevant_rank` (5),
  `test_ranks_start_at_one`, `test_nothing_relevant_in_the_top_k_gives_zero` (4),
  `test_the_cut_off_decides_whether_a_late_hit_counts` (3),
  `test_only_the_first_relevant_hit_counts` (3), `test_reciprocal_rank_is_an_exact_fraction` (3).
  <details><summary>Hint</summary>

  `ZeroDivisionError` or 1/2 for `THIRD` means 0-based ranks. 5/6 for `TWICE` means you added
  every relevant hit. 1/6 for `SIXTH` at k = 5 means the cut-off is missing. A failed
  `isinstance(value, Fraction)` means a float or a bare 0 slipped out.

  </details>
- **The means**: `test_mean_hit_at_k_is_the_share_of_questions_with_a_hit` (4),
  `test_mrr_is_the_mean_reciprocal_rank` (4), `test_a_question_without_a_hit_still_counts_in_n` (2),
  `test_the_mean_of_one_question_is_its_own_value`,
  `test_the_means_over_several_groups_use_any_group`,
  `test_the_order_of_the_questions_does_not_matter`,
  `test_the_means_are_exact_fractions_and_the_input_is_untouched`,
  `test_a_mean_over_three_questions_is_exact`, `test_an_mrr_with_a_large_denominator_is_exact`,
  `test_an_empty_set_is_an_error_not_zero` (2), `test_a_question_counted_twice_is_an_error` (4).
  <details><summary>Hint</summary>

  A mean is a sum over every record divided by how many records there are; check both parts
  against §3's "wrong n". If 7/18 comes out as `0.388…84`, a float got into the sum; if 2/3 is
  off by a hair, a float got into the division (§3). If only the 30191/3027273 test fails, a float
  was rounded back with `limit_denominator()` (§3). The two error tests want the checks from
  `ranking.py` before any arithmetic; a repeated question is found by its id, not by comparing
  records.

  </details>
- **A bad k**: `test_a_bad_k_is_refused` (50 cases: five functions × k = 0, −1, `True`, `5.0`,
  `"5"` × a record with hits, `FIRST`, and one with none, `EMPTY`).
  <details><summary>Hint</summary>

  Call `ranking.check_k` first in each function, the means included, even though they call
  functions that check it again. A hand-rolled `k < 1` lets `True` and `5.0` through, and raises
  `TypeError`, not `ValueError`, for `"5"`. If only the `EMPTY` cases fail, an early return for an
  empty hit list comes before the check.

  </details>
- **Properties** (hand-chosen records, k = 1..8; no random input):
  `test_hit_at_k_never_goes_down_as_k_grows`, `test_all_groups_implies_a_hit_and_never_goes_down`,
  `test_reciprocal_rank_stays_in_its_bounds_and_agrees_with_hit_at_k`,
  `test_at_k_1_reciprocal_rank_and_hit_are_the_same_number`,
  `test_mrr_lies_between_zero_and_the_hit_rate` (4).
  <details><summary>Hint</summary>

  These check relations, not values, so a failure names the record and k in its message. If the
  value tests pass but one of these fails, the two functions it compares disagree about what
  "relevant within the top k" means, or a float slipped into one of the means (§3).

  </details>

## 5. The hint ladder, the time-box and decisions
The ladder is M1's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request (never in
this file), level 4 = a pairing review where you type. **Time-box: 2 h** of writing (decided);
reading this guide does not count. When it is up and you are stuck, go one level up. If you ask
Claude to write it, Claude reminds you of the learning goal once and respects your decision; the
explain-back stays part of acceptance.

**Decided by shesha, 2026-10-22: the recommended option (a) for D1–D4 and D6, and D5 as written.**
shesha writes the bodies. The options stay listed for the record.

- **D1. Several groups: any or all?** (a) hit@k = any group, plus `all_groups_at_k` as a separate
  measure that PR 8 reports as a count (`x of n`) for questions with two or more groups, next to
  hit@k; (b) hit@k = any group only, and `all_groups_at_k` is deleted; (c) hit@k = all groups.
  **Recommended: (a).** "Any" is the standard definition, comparable across categories and (once
  the definition is checked, §2.2) with other systems; "all" answers the question `multi_hop` and
  `ambiguous` really ask, but those are few questions (1–2 per category in test), so a count, not
  a rate. *A further proposal for PR 8, not part of H2:* `all_groups_at_k` applied to
  `AnswerRun.evidence` (the hits the prompt held) as a separately named "evidence complete"
  diagnostic for those questions; it depends on `rag_min_score`, so it is never called hit@k.
- **D2. MRR's depth.** (a) MRR@k with the same k as hit@k, k = 5, the number of hits one answer
  retrieves (`rag_context_k`); only those scoring at least `rag_min_score` reach the prompt;
  (b) MRR over the whole searched list (for example k = 10 or 50), a different depth from hit@5;
  (c) no cut-off at all, implicitly whatever the search returned. **Recommended: (a),** one depth
  for both numbers, named in the report as "hit@5, MRR@5"; (c) makes the number depend on a
  setting it does not name.
- **D3. Return type.** (a) `Fraction` (exact; floats only in the report); (b) `float` with a
  stated tolerance in the tests. **Recommended: (a)** (§2.5).
- **D4. Which questions count.** (a) every question with at least one evidence group, hand-offs
  that point to evidence included (`select_for_retrieval` today); (b) only `expected_outcome =
  answered`. **Recommended: (a),** with PR 8 breaking results down by category, so a safety
  question's retrieval is visible (as a count) without hiding it in or out of the headline.
- **D5. Time-box and branch:** 2 h, `m2/metrics`.
- **D6. The metric's k against the search's k.** A metric k above the k the search ran with
  reports a shallow search under a deeper label (hit@20 on a 5-hit list is hit@5). (a) PR 8
  asserts, per run, that every metric k is at most the search's k, and the report names both;
  (b) a team-built `searched_k` field on `RankedRelevance` (validated 1..`MAX_K`, with
  `len(groups_by_rank) <= searched_k`) that the harness checks before calling your functions.
  **Recommended: (a).** Either way your functions and their contract stay as they are.

## 6. Explain it back (part of acceptance)
- For a question with two evidence groups, what do hit@5, recall@5 and precision@5 each measure?
  Give a ranked list where hit@5 is 1 but recall@5 is 1/2.
- Why does MRR look only at the first relevant hit? Moving the first relevant hit from rank 4 to
  3, or from rank 2 to 1: which helps MRR more, by how much, and what does that say about where
  tuning effort pays off?
- One question's search returned no hits; another question has no evidence groups. How does each
  enter hit@5 and MRR, and why are they treated differently?
- Why do the functions return a `Fraction`? Show, with your own example, why `==` on floats can
  fail for a mean of reciprocal ranks, and why `Decimal` would not fix it.
- The test split holds about 12 questions. Why would a report say "7 of 9" with an interval
  instead of "78 %", why is n not 12, and why is the interval not computed inside your functions?
- For a `multi_hop` question, when does "any group in the top k" call a retrieval good that cannot
  support a correct answer, and what would you report next to hit@k?
