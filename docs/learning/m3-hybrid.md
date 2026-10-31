# M3 hand-writes: full-text search (H1) and Reciprocal Rank Fusion (H2)

These are M3's two `[HAND-WRITE]` pieces. M2's `vector_search` finds chunks by meaning. **H1**,
`text_search`, finds them by their words, with Postgres' built-in full-text search, for **one**
business, from **approved** sources only. **H2**, `rrf_fuse`, merges the two ranked lists into one.
PR 4 then runs both legs and the fusion in one snapshot, behind a setting that stays `vector` until
D15's paired comparison says otherwise. The decisions behind every rule here are in
[ADR 0014](../adr/0014-hybrid-retrieval.md) and the plan's "Decided 2026-10-23"
([`m3-pr-checks.md`](../plans/m3-pr-checks.md)).

> **Status (2026-10-24, M3 PR 3 scaffold).** Both bodies raise `NotImplementedError`; their tests
> are strict xfails. shesha writes both, H1 and H2 (D5, decided 2026-10-24). Both wait behind the
> one switch, `PENDING_HAND_WRITE` in `tests/canary/read_paths.py` ("text_search", "rrf_fuse").
> Besides H1's own 34 cases, "text_search" also holds 6 canary cases: the `text_search` read path
> as each business, and the same path on the owner's connection, where only your `WHERE` clause
> keeps the other business out (§2.6), as each business with `MAX_K` hits and with one.
> `scripts/canary_gate.py` allows those xfails, and only those, until the name is removed.

> **Status (2026-10-28).** `text_search` (H1) and `rrf_fuse` (H2) are written, by Claude at
> shesha's request for the deadline; `PENDING_HAND_WRITE` is empty and every test that ran them
> as a strict xfail is a plain test. This guide stays as the reference for the explain-backs,
> which are still part of acceptance.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Branch | Who |
|---|---|---|---|---|---|---|
| **H1** | `text_search` in [`retrieval/text.py`](../../apps/api/src/vectapilot_api/retrieval/text.py) | [`tests/retrieval/test_text_search.py`](../../apps/api/tests/retrieval/test_text_search.py): 17 tests, 32 cases (Postgres) · [`test_text_search_binding.py`](../../apps/api/tests/retrieval/test_text_search_binding.py): 1 test, 2 cases (no database) · the canary suite's `text_search` read path and its owner-connection check ([`tests/canary/test_leakage.py`](../../apps/api/tests/canary/test_leakage.py): 6 cases, Postgres) | about 18 lines of SQL, plus the call | 3 h (decided) | `m3/text-search` | shesha (D5, decided 2026-10-24) |
| **H2** | `rrf_fuse` in [`retrieval/fusion.py`](../../apps/api/src/vectapilot_api/retrieval/fusion.py) | [`tests/retrieval/test_fusion.py`](../../apps/api/tests/retrieval/test_fusion.py): 21 tests, 40 cases (no database) | about 20 lines | 2 h (decided) | `m3/rrf` | shesha (D5) |

- **The docstring is the spec.** Each function's docstring states the whole contract; the tests
  are that contract made executable, with every expected value worked out by hand in a comment
  next to it. Read the docstring first, then the tests.
- **Already there and tested** ([`tests/retrieval/test_hybrid_types.py`](../../apps/api/tests/retrieval/test_hybrid_types.py):
  16 tests, 57 cases, passing):
  - `TextQuery`: `VectorQuery`'s fields (one shared base, so both legs check alike) plus the
    question, checked exactly like the API's `Question`.
  - `FusedHit`: the unchanged `SearchHit`, `rrf_score` as a `Fraction`, and `ranks` per list
    (`None` where absent); deliberately no `score`.
  - Also there: `SearchHit`, migration 0005 (`chunks.ts_config`, the stored `chunks.tsv`), the
    language map (`ingestion/language_map.py`), `kb.store.vector_literal`, `db.business_transaction`,
    and M2's `vector_search` (`retrieval/vector.py`), the closest model for H1's shape.
- **Not your job:** running both legs in one transaction and snapshot, the settings, the evidence
  gate and the `rag_answer` fields (PR 4, ananya); the canary read paths and the gate (zara);
  measuring anything (PR 5, noah; the live runs are shesha's, PR 9).

## 1. How to work

**H1** needs Postgres for 32 of its 34 cases; Docker works (2026-10-23).

```bash
cd apps/api
git switch main && git pull                  # once PR 3 is merged
git switch -c m3/text-search
# binding: 2 xfailed · Postgres: 32 skipped (no RUN_INTEGRATION)
uv run pytest tests/retrieval/test_text_search.py tests/retrieval/test_text_search_binding.py -v
make test-integration                        # from the repository root: the 32 Postgres cases too
```

With the stack already up (`make up`), the same Postgres tests run inside `apps/api` without
rebuilding anything, if all three URLs are given (otherwise `Settings` falls back to `.env`):

```bash
RUN_INTEGRATION=1 \
VECTAPILOT_DATABASE_URL="postgresql+asyncpg://vectapilot_app:${VECTAPILOT_APP_DB_PASSWORD:-vectapilot-app-dev}@127.0.0.1:5432/vectapilot" \
VECTAPILOT_MIGRATION_DATABASE_URL="postgresql+asyncpg://vectapilot:${POSTGRES_PASSWORD:-vectapilot-dev}@127.0.0.1:5432/vectapilot" \
VECTAPILOT_REDIS_URL="redis://127.0.0.1:6379/0" \
uv run pytest tests/retrieval/test_text_search.py tests/retrieval/test_text_search_binding.py -v
```

**H2** needs no database: `uv run pytest tests/retrieval/test_fusion.py -v` (40 xfailed, about
4 seconds; the hash-seed test starts several interpreters).

**Reading the output (strict xfail, as in M1 and M2).**

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but gives a wrong answer or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

When every case of your piece shows `XPASS(strict)`, remove its name from `PENDING_HAND_WRITE` in
`tests/canary/read_paths.py` (`"text_search"` for H1, `"rrf_fuse"` for H2; see the status note
above), then everything must simply pass. For H1 that one line also turns on the canary suite's
`text_search` read path. A leak there shows `FAILED`, never `XPASS`. If a team test outside your
piece fails in your PR, its owner fixes that test in a separate commit; your code never changes to
suit it (decided for M2, carried into M3).

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say
so in your PR. `mypy` (strict) and `ruff` apply as everywhere. Commit with your own message (for
example `feat(retrieval): implement text_search`), push, open a PR. Reviews: H1 by
`@agent-ananya-rag` and `@agent-zara-safety`; H2 by `@agent-ananya-rag` and `@agent-kenji-ml`.
Then explain it back (§6); the explain-back is recorded in CLAUDE.md.

## 2. The concepts (hint level 1)

Postgres' own documentation is the reference: chapter 12, "Full Text Search", and §9.13, "Text
Search Functions and Operators" (PostgreSQL 18).

### 2.1 tsvector, tsquery and text-search configs
Full-text search does not compare strings. A **config** (`english`, `hindi`, `simple`) runs a
parser that splits text into tokens (words, numbers, URLs, e-mail addresses …) and dictionaries
that turn each token into a **lexeme** (lower-cased, and for `english` stemmed: "opening",
"opens" and "opened" all become `open`) or drop it as a **stop word** ("the", "of", "is").
- A `tsvector` is a document's sorted set of lexemes with their positions. Migration 0005 stores
  one per chunk: `chunks.tsv`, generated from `content` with the chunk's own `chunks.ts_config`.
- A `tsquery` is a boolean expression over lexemes: `&` (and), `|` (or), `!` (not), `<->`
  (followed by), `:*` (prefix), with parentheses.
- `tsv @@ query` is true when the document satisfies the query.
- Our configs: `english` stems and drops English stop words. `simple` only lower-cases: no
  stemming, no stop list. `hindi` stems Devanagari with the Hindi stemmer and sends Latin-letter
  words to the English one, so romanized Hinglish gets English lexemes. The language map
  (`en` → `english`, `hi` → `hindi`, everything else → `simple`) is D2's; a chunk's config never
  changes after insert.
- A lexeme only matches a lexeme: `open` (stemmed) never matches `opening` (unstemmed). That is
  why the config matters on *both* sides.

### 2.2 From a customer's question to a tsquery, safely
Postgres has several functions that build a `tsquery` from text. Read their documentation with
one question in mind each time: *what does it do with text a customer typed?*
- `to_tsquery` (and a cast to `tsquery`) read tsquery **syntax**. A customer's `&`, `|`, `!`,
  `(`, `:*`, `<->`, `'` or `\` becomes an operator, or a syntax error. Postgres' error then quotes
  the text (`syntax error in tsquery: "& | !"`, checked on 18.6), and SQLAlchemy adds the bound
  values to its own error text unless the engine hides them (the API's does; test engines don't).
  So the spec says: never `to_tsquery` on the question as written.
- `plainto_tsquery` and `websearch_to_tsquery` never raise a syntax error, whatever the text. But
  both **AND** every
  lexeme ("Do you treat an axolotl, a quokka or a zebrafish?" would need a chunk with `treat`,
  `axolotl`, `quokka` and `zebrafish` all at once), and `websearch_to_tsquery` reads its own
  syntax: `-word` negates, quotes make phrases, `or` is an operator.
- The spec wants an **OR** of the question's lexemes: a chunk qualifies if it holds at least one.
  How to get there safely is the core of H1. Postgres gives you functions that turn text into
  lexemes in a config, functions that turn text into a `tsquery`, and operators that combine
  `tsquery` values (§9.13). One trap: a lexeme itself can hold operator characters, and quotes.
  A URL is one token, so `http://x.example/a?q=(x&y=2:*` gives the lexeme `x.example/a?q=(x&y=2:*`,
  and `http://x.example/a'b` gives `x.example/a'b`. Joined into tsquery text as they are,
  unquoted, such lexemes turn into operators or a syntax error.
- A question with no lexeme in a config (only stop words, only punctuation) gives an empty
  `tsquery`. Matching against it is false, and Postgres sends a NOTICE (not an error, and it does
  not contain the question).

### 2.3 Why each chunk's own config (D2)
The question must be parsed **per chunk**, with that chunk's `ts_config`, in SQL: a function
call can take a column (`c.ts_config`) as its config argument. With one config for every chunk:
- `english` for all: an `english` chunk's "open" matches, but the question "Opening of the
  clinic" then also matches a `simple` chunk that stores the unstemmed word `open`, and it never
  matches a `simple` chunk on the words that config keeps.
- `simple` for all: "Opening" stays `opening` and never meets an `english` chunk's `open`.
- A hard-coded config also hard-codes a language, which rule 9 forbids.

The price of doing it right is visible in the tests (ADR 0014, "Consequences"): `simple` and
`hindi` have no stop list, so a `simple` chunk that shares only "of" and "the" with the question
matches. `test_the_question_is_parsed_with_each_chunks_own_config` plants exactly that chunk. A
question in one script never matches a chunk in another (Devanagari against Latin), and there is
no language filter (D1): Hindi questions with only English evidence rely on the vector leg.

### 2.4 `ts_rank`, and why "no corpus statistics" is an isolation property
`ts_rank(tsv, query)` scores one document against one query, using only that document's lexemes
(how often each query lexeme occurs, with its weight) and the number of lexemes in the query.
With the two-argument form (default weights, no length normalisation; the spec pins it), each
distinct query lexeme a chunk holds once adds the same amount, and the sum is divided by the
number of distinct query lexemes. So, within one config, chunks that match more lexemes rank
higher, and chunks that match the same number tie exactly, wherever the lexemes stand in the
chunk. A lexeme held more than once adds a little more, but less than one more distinct lexeme.
Across configs the scale differs: `simple` keeps stop words, so the same question has more lexemes
there, and each match is worth less (in the config test, `en-doors` matches 1 of the question's 2
`english` lexemes and `te-menu` 2 of its 4 `simple` ones, and they tie). The tests are built on
that.

BM25 (the classic ranking of search engines, and of the extensions ADR 0014 declined) also uses
**corpus statistics**: how many documents contain each term (IDF) and the average document
length. In our shared `chunks` table, those statistics would cover every business's rows (ADR
0014: "corpus-wide statistics shared across businesses"). Business A's documents would then change
business B's ranking, and B's rankings would leak something about A's content. `ts_rank` reads
one chunk and the question, nothing else. So no business can move another's ranking. That is an
isolation property (business-context §5), not just a simplification. The price: no IDF, so a
common word counts as much as a rare one (which is one reason fusion and the cosine stay in the
loop).

### 2.5 The score stays the cosine
`text_search` ranks by `ts_rank` but returns `score` = the cosine to the question's embedding,
written exactly as `vector_search` writes it. Two reasons.
- ADR 0011's evidence gate compares `score` with `rag_min_score`, a cosine threshold (D7).
  `ts_rank` has another scale, depends on the config and the question's length, and says nothing
  about meaning.
- A chunk found by both legs must come back as **equal** `SearchHit`s, so that `rrf_fuse` can
  tell "one chunk in two lists" (normal) from "two different hits with one id" (a bug). A score
  written another way (a dot product with `<#>`, say) differs in the last bits once a vector is
  only nearly unit, as real embeddings are. `test_a_chunk_both_legs_find_is_the_same_search_hit`
  uses such a vector.

Only chunks of the question's embedding model are candidates, as in the vector leg: the cosine
is then defined, and both legs see the same candidates.

### 2.6 The business filter belongs in the SQL, again
Everything from M2's guide §2.3 holds. RLS is the second line, and the first is your `WHERE`
clause: `c.business_id = :business_id`. The owner's connection skips RLS, so only your clause
protects there. A transaction opened for another business must find nothing. Since PR 3 the canary
suite also checks this on the owner's connection, so a leg that relies on RLS alone cannot pass
the canary gate. It checks with `MAX_K` hits and with one: with one, a business filter applied only
after the `LIMIT` lets the other business's equally good chunk take the only place, and your own
business gets nothing back.

RLS also has a cost. `@@` and `<=>` are not *leakproof* (Postgres does not trust them to reveal
nothing about the rows they see, through an error for example). So under RLS the planner applies
the policy before them, which may keep an index on them from being used for the app role (ADR 0014
decision 9; PR 8 measures it).

### 2.7 Reciprocal Rank Fusion
Two lists, two incomparable scores: a cosine in [−1, 1], uncalibrated and model-specific, and a
`ts_rank` whose scale depends on the config and the question. RRF ignores both scores and uses
only **positions**. A chunk at rank r in a list earns 1/(k + r) from it, and its fused score is the
sum over the lists that hold it (Cormack, Clarke and Büttcher, 2009):

    rrf(d) = Σ over the lists that hold d of 1 / (k + rank of d in that list)     (ranks from 1)

- **k** damps the top. With k = 60 (fixed, D6), rank 1 earns 1/61 ≈ 0.0164 and rank 10 earns
  1/70 ≈ 0.0143. Being found by both legs matters more than being first in one. With k = 1, rank
  1 earns 1/2 and rank 10 earns 1/11, so the top ranks dominate. k is not tuned: about 28 dev
  questions would fit noise.
- **Worked example** (the plan's, and a test's). k = 1, vector [A, B, C], text [C, A, D]:
  A = 1/2 + 1/3 = 5/6, C = 1/4 + 1/2 = 3/4, B = 1/3, D = 1/4. Fused order A, C, B, D.
- **Why not average or take the max of the scores?** Averaging needs both on one scale. Min-max
  normalisation over a 20-item list depends on what else happened to come back, and is undefined
  for a list of one. Taking the max lets one leg's extreme win alone: a chunk stuffed with the
  question's keywords tops `ts_rank` whatever it means. Ranks need no calibration, and a chunk
  both legs agree on rises.
- **Exact `Fraction`.** In Python, `1 / (k + r)` is a float, already rounded before any sum
  starts. Two scores that are equal as fractions can then differ in the last bit, and that ghost
  difference would decide a tie the spec breaks by best rank and then by name.
  - The same terms in another order: at k = 1, ranks (2, 3, 4) and (3, 4, 2) give exactly 47/60
    each. Added one list at a time (a running total, `total += 1 / (k + r)`), they give
    0.7833333333333332 and 0.7833333333333333. Python's built-in `sum()` is compensated
    (Neumaier summation, since Python 3.12), and so is `math.fsum`: both tie on these two.
  - Different terms: at k = 1, ranks (1, 1, 5) give 1/2 + 1/2 + 1/6 and ranks (2, 2, 1) give
    1/3 + 1/3 + 1/2, exactly 7/6 each. As floats, `sum()`, `math.fsum` and a running total all
    give 1.1666666666666667 and 1.1666666666666665. No way of adding repairs it, because the
    error is in the terms: the float 1/6 is exactly half the float 1/3, which is 1/3 − d, so the
    float terms add up to exactly 7/6 − d/2 and 7/6 − 2d.

  `Fraction(1, k + r)` is exact. `FusedHit` refuses a float `rrf_score`, and the tests compare
  every score exactly, so a float can only slip in through the order (the 47/60 and 7/6 tests)
  or turned back into a `Fraction` (the k = 1000 test).
- **Ties** are real: one-hit lists, symmetric ranks. They are broken by the best single rank (a
  chunk one leg put first beats one both legs put third, at the same score), then in M2 H1's
  order: source id, version (code point by code point, which is `COLLATE "C"`'s byte order), then
  chunk index as a number. That order is total within a business, so nothing else (input order,
  a set's iteration order, the hash seed) can ever decide.
- **Duplicates** raise. A chunk twice in one list, or two different hits with one chunk id, means
  a bug upstream (mixed models, versions or businesses). Merging them silently would hide it.

### 2.8 Why the gate stays cosine, and what the types do about it (D7)
Evidence under hybrid = the fused hits whose **cosine** is at least `rag_min_score`. An RRF score
at k = 60 is at most 2/61 ≈ 0.033 for two lists (rank 1 in both), and 1/80 = 0.0125 for rank 20
in one. Compared with a 0.3 threshold, every answer would be "I don't know". A threshold re-tuned
on RRF scores would admit whatever ranks first in either leg, keyword-stuffed chunks included. So
`FusedHit` keeps the two numbers apart:
`fused.hit.score` is the cosine, `fused.rrf_score` only orders the list, and `fused.score` does not
exist. PR 4 hands the answer plain `SearchHit`s in fused order (a proposal, confirmed in PR 4),
and its test
`test_the_evidence_gate_never_compares_a_cosine_threshold_with_a_fused_score` pins the gate.
FTS-only hits (in the text list, not the vector list: `ranks[0] is None` when PR 4 passes vector
first) are counted, because they are the reason to try hybrid at all.

### 2.9 The switches (PR 4) and when hybrid would win (D15)
Platform defaults, unmeasured (rule 3):
- `rag_retrieval_mode`, `vector | hybrid`, default `vector`: vector mode stays M2's path,
  unchanged.
- `rag_candidate_k`, 20: each leg's `k` and the fused list's `limit`; at least `rag_context_k` and
  10, at most `MAX_K`.
- `rag_rrf_k`, 60: `rrf_fuse`'s `k`.

Hybrid runs both legs in one `REPEATABLE READ READ ONLY` transaction (D8), so an approval between
the legs cannot put a retired and a new version of one source into one fused list. It becomes the
default only through D15's rule, fixed in writing before any hybrid result was seen. Per business,
"vector hit, hybrid miss" must be ≤ "hybrid hit, vector miss", and no safety row vector did not
answer may be answered under hybrid. On v0.1, "no measurable difference" is a likely and
acceptable result.

## 3. Common mistakes

**H1**
- `to_tsquery(...)` or `CAST(... AS tsquery)` on the question (§2.2). Also joining the lexemes
  into tsquery text as they are, unquoted, and parsing that again: a lexeme can hold `(`, `&`,
  `!`, `:*` or a quote (a URL), so it raises or changes the query.
- `plainto_tsquery` or `websearch_to_tsquery` used as they are: AND of every lexeme, and
  `websearch_to_tsquery` turns "-word" into NOT.
- One config for every chunk, a literal `'english'` (rule 9), or the question's language.
- Splitting, lower-casing or escaping the question in Python: the question is bound unchanged
  (the binding test checks it) and parsed in SQL.
- Returning `ts_rank` as `score`, ordering by the cosine, or using the cosine to break ties.
- Writing the cosine differently from `vector_search` (§2.5).
- No tie-break, a tie-break on `chunks.id`, or text keys without `COLLATE "C"`.
- `LIMIT` in a subquery before the status, audience or model filter, or filtering in Python.
- Prefix matching (`:*`) "to help" partial words, `ts_rank_cd` instead of `ts_rank`, ranking
  against an AND of the lexemes while matching on the OR, or a `ts_rank` normalisation flag: none
  is in the spec, and each fails a test, except flags 4 and 32. Those two never change
  `ts_rank`'s order (4 only applies to `ts_rank_cd`; 32 is rank/(rank + 1)), so no test can see
  them, and they are still not the spec.
- In `text()`, `:question::text` is not read as a parameter; write `CAST(:question AS text)` if
  you need a cast (the same trap as M2's `CAST(:embedding AS vector)`).
- Logging the question or catching a database error and logging it.

**H2**
- `1 / (k + rank)`: a float. Use `Fraction(1, k + rank)` and keep it exact all the way, sorting
  included: neither `sum()` nor `math.fsum` makes floats safe (§2.7).
- Ranks from 0 (`enumerate` starts at 0 unless told otherwise).
- Using any hit's `score` (the cosine) in the fused score or the order.
- An incomplete tie-break, `str.lower()` / case-insensitive keys, or chunk index compared as text.
- Iterating a `set` of hits or of names: its order changes with the hash seed. A set of chunk
  ids keeps one order under every seed (a UUID hashes as its int), but that is still not the
  spec's order. The fixed tie tests catch it; the two-seed test only catches orders that depend
  on the seed.
- Deduplicating by `==` and silently keeping one of two different hits with one chunk id.
- Sorting a caller's list in place, or cutting to `limit` too early: `limit` cuts the fused,
  sorted list, never an input list.
- `ranks` in the wrong order: one entry per input list, in the order the lists were given.

## 4. Test by test, with level-2 hints
Open a hint only for the test you are stuck on, and only after §2.

**H1, no database**
- **`test_one_statement_with_every_value_bound_and_none_in_the_sql`** (2 cases).
  <details><summary>Hint</summary>

  As M2's binding test: one `execute`, nothing else, every value bound. New here: the question
  must be among the bound values exactly as `query.question` holds it, and appear nowhere in the
  SQL text.

  </details>

**H1, Postgres**
- **`test_matching_chunks_come_back_by_ts_rank_with_their_cosine_scores`** (4 cases).
  <details><summary>Hint</summary>

  Every chunk matches 3, 2, 1 or 0 of the question's four lexemes, and the cosines run the other
  way. If `three` comes last, you ordered by the cosine or ascending. If only `three` comes back,
  your query is an AND (§2.2). If the scores look like 0.05, 0.03 and 0.015, you returned
  `ts_rank`.

  </details>
- **`test_ts_rank_ties_are_broken_by_source_id_byte_by_byte_then_chunk_index`**.
  <details><summary>Hint</summary>

  Five chunks hold "okapi" once and tie. If `a-faq` comes before `B-faq`, your comparison is
  linguistic, not byte-wise. If the tie follows the cosines (1.0 first), the cosine is in your
  `ORDER BY`. If the first line fails ("premise: …"), the database's default collation changed,
  not your code.

  </details>
- **`test_two_distinct_lexemes_outrank_one_repeated_and_their_distance_never_counts`**.
  <details><summary>Hint</summary>

  The spec's rank counts the distinct question lexemes a chunk holds (repeats add only a little),
  never where they stand. `a-repeat` first: the ranking function is not the spec's. `b-near`
  before `a-far`: the rank was computed against another query than the OR you matched with
  (§2.2, §2.4), or the tie went by an id instead of the name.

  </details>
- **`test_the_rank_is_never_normalised_by_the_chunks_length`**.
  <details><summary>Hint</summary>

  Both chunks hold "okapi" once, so the spec's rank ties them and the name decides: `a-long`
  first. `b-short` first: your `ts_rank` reads the chunk's length (a third argument, §3), or the
  cosine or an id is in your `ORDER BY`.

  </details>
- **`test_only_the_approved_version_of_a_source_is_a_candidate`**.
  <details><summary>Hint</summary>

  The retired and draft versions hold "albatross" more often (a higher rank), and one has exactly
  V3's text: only the status filter keeps them out.

  </details>
- **`test_the_question_is_parsed_with_each_chunks_own_config`**.
  <details><summary>Hint</summary>

  Look at which chunk is wrong. `te-open` back: you parsed with `english` (or with several
  configs). `en-doors` missing: with `simple`. `te-menu` missing: not with the chunk's own config.
  §2.3.

  </details>
- **`test_a_word_never_matches_a_longer_word_that_begins_with_it`**.
  <details><summary>Hint</summary>

  If "zebra" finds the `zebrafish` chunk, your query matches prefixes (`:*`, §2.1). If the first
  assertion fails ("premise: …"), the chunk is not found by its own whole word: start with the
  ranking tests.

  </details>
- **`test_another_businesss_better_matches_are_never_returned`**,
  **`test_the_business_filter_is_in_the_sql_even_where_rls_cannot_help`** (2 cases),
  **`test_a_transaction_for_another_business_finds_nothing`**.
  <details><summary>Hint</summary>

  The first can pass on RLS alone; the other two cannot (§2.6).

  </details>
- **`test_only_chunks_of_the_querys_embedding_model_are_candidates`** (2),
  **`test_staff_only_draft_and_retired_chunks_never_reach_a_customer`** (2),
  **`test_every_filter_applies_before_the_limit`** (2),
  **`test_no_candidates_or_no_match_means_an_empty_list`** (3).
  <details><summary>Hint</summary>

  Each excluded chunk holds a word of the question, so only a filter keeps it out. Fewer hits
  than expected means a `LIMIT` came before a filter. An empty result is a normal outcome.

  </details>
- **`test_a_hostile_question_never_raises_and_never_reaches_a_log_line`** (7 cases).
  <details><summary>Hint</summary>

  The failure names the exception type only (the message may quote the question). A syntax error
  means operators reached a tsquery parser (§2.2, §3). Wrong hits with `-zebrafish`, `!` or
  `<->` mean the operators changed the query. `url` failing means the lexemes were joined into
  tsquery text as they are, unquoted; `url-with-quote` alone, that a lexeme's own quote broke
  the quoting.

  </details>
- **`test_each_hit_carries_what_a_citation_needs`**.
  <details><summary>Hint</summary>

  The same fields as `vector_search`'s hits. The chunk is `hi`: its config is `hindi`, and the
  question still matches (Latin letters, English stemmer).

  </details>
- **`test_a_chunk_both_legs_find_is_the_same_search_hit`**.
  <details><summary>Hint</summary>

  A difference in `score` in the 8th digit means your cosine is written differently from
  `vector_search`'s (§2.5).

  </details>

**H2**
- **`test_the_worked_example_at_k_1`**, **`test_the_default_k_is_60`**,
  **`test_the_limit_keeps_the_best`** (5),
  **`test_the_limit_cuts_the_fused_list_never_the_input_lists`**,
  **`test_swapping_the_lists_changes_only_the_ranks`**.
  <details><summary>Hint</summary>

  Work the example by hand first (§2.7). Wrong scores by one step: ranks from 0. Ranks in the
  wrong order after the swap: `ranks` follows the lists, not a fixed "vector, text". `a-top-of-one`
  instead of `b-in-both`: you cut the input lists to `limit` before fusing.

  </details>
- **`test_only_ranks_count_never_a_hits_own_score`**.
  <details><summary>Hint</summary>

  The cosines rise down the list. If the order flips, a hit's `score` got in. A cosine that
  decides a tie is the same mistake: the tie tests below give the tied hits cosines against the
  expected order.

  </details>
- **`test_an_equal_score_and_best_rank_is_decided_by_source_id_byte_by_byte`** (2),
  **`test_an_equal_score_is_decided_by_the_best_single_rank_before_the_source_id`**,
  **`test_then_the_version_byte_by_byte_then_the_chunk_index_as_a_number`**,
  **`test_ties_never_depend_on_the_order_of_the_lists`** (6),
  **`test_the_best_single_rank_is_the_lowest_in_any_list_not_a_sum_or_the_first_seen`**.
  <details><summary>Hint</summary>

  Write the full sort key out on paper and check it against each comment. Python compares `str`
  code point by code point, which is exactly the byte order the spec asks for, unless you change
  the strings. The best single rank is the lowest rank the chunk has in any list: not the one
  from the list you happened to read first, and not a sum.

  </details>
- **`test_scores_are_exact_fractions_so_float_rounding_never_decides_a_tie`**,
  **`test_scores_tie_exactly_where_even_a_compensated_float_sum_does_not`**.
  <details><summary>Hint</summary>

  If `b-exact` comes before `a-exact`, or `b-ones` before `a-twos`, a float decided the order
  (§2.7). Look for a `/` between two ints, in your sort key too. The first test catches a running
  float total; the second also catches `sum()` and `math.fsum`. A float turned back into a
  `Fraction` afterwards (with `limit_denominator()`, say) fails
  **`test_scores_stay_exact_where_a_float_turned_back_into_a_fraction_would_not`** instead.

  </details>
- **`test_the_same_hit_in_two_lists_is_one_fused_hit`**,
  **`test_one_chunk_id_twice_in_a_list_or_as_two_different_hits_raises`** (5),
  **`test_no_lists_or_only_empty_ones_give_an_empty_list`** (3),
  **`test_an_empty_list_next_to_a_full_one_is_a_list_that_holds_nothing`**,
  **`test_k_and_limit_below_1_raise`** (4).
  <details><summary>Hint</summary>

  Key everything by `chunk_id`. Compare a second sighting with the first one with `==`, never
  `is` (each leg builds its own objects, so one chunk arrives as two equal hits), and keep the
  messages free of any hit's text or title.

  </details>
- **`test_the_input_is_never_changed_and_any_sequence_is_accepted`**,
  **`test_the_golden_case_is_the_same_under_two_hash_seeds`**.
  <details><summary>Hint</summary>

  The first: no in-place sort of what you were given, and tuples work too. The second runs your
  function in two fresh interpreters whose sets iterate differently; if it fails, a set's order
  (or another order you did not choose) decided a tie.

  </details>

## 5. The hint ladder, the time-boxes and decisions
The ladder is M1's and M2's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request
(never in this file), level 4 = a pairing review where you type. **Time-boxes: H1 3 h, H2 2 h** of
writing (reading this guide does not count). When one is up and you are stuck, go one level up. If
you ask Claude to write a piece, Claude reminds you of the learning goal once and respects your
decision; the explain-back stays part of acceptance.

**Decided 2026-10-23 (shesha), as they bear on H1 and H2:**
- **D2.** Per-chunk config from the document's language through the platform map, stored by
  migration 0005.
- **D5.** shesha writes H2, and H1 too, from this guide (H1 decided 2026-10-24).
- **D6.**
  - H1: an OR of the question's lexemes in each chunk's own config, ranked by `ts_rank`.
  - `score` stays the cosine.
  - H2: RRF with k = 60 as an exact `Fraction`, and `FusedHit` as a wrapper.
  - `candidate_k` 20 (provisional); no GIN index, no BM25.
- **D7.** Evidence = fused hits with a cosine ≥ `rag_min_score`, re-tuned on dev only; FTS-only
  rescues are counted.
- **D8.** One `REPEATABLE READ READ ONLY` snapshot per hybrid search (PR 4).
- **D13.** No reranking in M3.
- **D15.** The variants, the adoption rule and the $5.00 ceiling, fixed before the first run.

**Decided 2026-10-24 (shesha), in PR 3's review** (this scaffold's proposals, all accepted):
- `ts_rank` in its two-argument form: default weights, no length normalisation. The tests tell
  it from `ts_rank_cd`, from a rank against an AND query and from every normalisation flag that
  can change an order (1, 2, 8 and 16, checked on 18.6). Flags 4 and 32 never change `ts_rank`'s
  order, so no test sees them, and the spec pins the one form.
- `rrf_fuse` raises `ValueError` for `k` or `limit` below 1.
- `FusedHit`'s field names (`hit`, `rrf_score`, `ranks`), and `ranks` by list position.
- The moved helpers: `Question` from `retrieval/search.py` to `retrieval/types.py` (re-exported,
  so every import still works), because `TextQuery` holds one; the search tests' planting
  helpers, fixtures and recording connection from M2's vector test modules to
  `tests/retrieval/corpus.py`, `conftest.py` and `recording.py`, unchanged, so both legs are
  tested the same way.
- The canary gate's xfail rule: only the exact reason `pending_reason` builds is accepted, and a
  static scan (`tests/test_canary_checks.py`) allows no other `xfail` in `tests/canary/`.
- M2's own business-clause tests stay where they are; the canary suite mirrors them on the
  owner's connection instead of moving them.
- The API-level red proof for the full-text leg lands in PR 4, when a route first runs it.

**Decided 2026-10-24 (shesha): a NUL character in a question** (was open, rule 10). Postgres text
cannot hold U+0000, so binding a question that has one makes the statement fail (`invalid byte
sequence for encoding "UTF8": 0x00`, checked on 18.6). `Question` accepts it today: vector mode
never sends the question to Postgres, but hybrid mode would answer such a question with a 503
`database_unavailable`. Decided: option (a), `Question` refuses U+0000 with a 422 on both routes,
in both modes (one rule at the edge, and no customer types a NUL). It deliberately changes M2's
vector-mode input check (a NUL becomes a 422 there too), needs a one-line amendment to ADR 0014
decision 5, and lands in PR 4 or a tiny PR of its own, never in H1. Not chosen: (b) only
`TextQuery` refuses it and PR 4 maps the error; (c) hybrid mode strips it before the text leg.
No code changes for it in PR 3. It is not H1's to handle, and H1's tests do not send it.

## 6. Explain it back (part of acceptance)
**H1**
- Why is the question parsed with each chunk's own config? Use `en-doors`, `te-menu` and
  `te-open` from the tests to show what one config for every chunk would get wrong.
- Why an OR of lexemes, and why not `plainto_tsquery` or `websearch_to_tsquery` as they are?
- What would `to_tsquery` do with a customer's "& | !", and why does it matter that the error
  quotes the text? Why is the `url` case in the hostile tests?
- What does `ts_rank` read, and what does BM25 read that it does not? Why is that difference an
  isolation property in a shared table under RLS?
- Why is `score` the cosine and not `ts_rank`? Name the two things that would break otherwise.
- RLS already hides other businesses' rows. Name two situations in which only your `WHERE`
  clause protects.
- `@@` is not leakproof. What may that mean for a GIN index on `tsv` under RLS, and why must
  PR 8 measure it as the app role?

**H2**
- Write the RRF formula and work the k = 1 example by hand. What does a larger k change?
- Why fuse ranks instead of averaging the cosine and `ts_rank`, or taking the larger one?
- Why `Fraction`? Explain the 47/60 example.
- Why must an RRF score never meet `rag_min_score`, and how do the types make that hard?
- Why does a duplicate chunk id raise instead of being merged? What could it mean upstream?
- Why is the result checked under two `PYTHONHASHSEED` values, when your code may never use a set?

**Hybrid (for PR 4 and the close-out)**
- What can go wrong between the two legs under READ COMMITTED, and how does one
  `REPEATABLE READ READ ONLY` snapshot prevent it?
- Why was D15's adoption rule fixed in writing before the first hybrid run?
