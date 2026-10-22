# M2 hand-write: top-k exact vector search (H1)

This is M2's `[HAND-WRITE]` retrieval piece. Ingestion, embeddings and the knowledge-base routes
(PR 3–5) put each business's chunks and their vectors in Postgres. Your function finds the chunks
closest to a question, for **one** business, from **approved** sources only. PR 6 then builds the
cited answer on top of it, and noah's harness (PR 8) measures it with your H2 metrics. Fusion with
full-text search comes in M3, so H1 is vector search only.

> **Status (2026-10-22).** `vector_search` is written, by Claude at shesha's request; its tests run
> as plain tests and "vector_search" is out of `PENDING_HAND_WRITE`. The Postgres cases first run
> in CI (Docker is unreachable locally). This guide stays as the reference for the explain-back,
> which is still part of acceptance.

## Your task

| | Write the body of | So that these pass | Size | Time-box | Your branch |
|---|---|---|---|---|---|
| **H1** | `vector_search` in [`retrieval/vector.py`](../../apps/api/src/vectapilot_api/retrieval/vector.py) | [`tests/retrieval/test_vector_search.py`](../../apps/api/tests/retrieval/test_vector_search.py): 12 tests, 21 cases (Postgres) · [`test_vector_search_binding.py`](../../apps/api/tests/retrieval/test_vector_search_binding.py): 1 test, 2 cases (no database) · the canary suite's `vector_search` read path: 2 cases (Postgres) | about 10–25 lines, the SQL included | 3 h (decided) | `m2/retrieval` |

- **The docstring is the spec.** `vector_search`'s docstring states the whole contract (where it
  runs, which chunks qualify, the operator and score, the order, `k`, binding, the result). The
  tests are that contract made executable. Read the docstring first, then the tests.
- **Already there and tested:** `VectorQuery` (validates the business id, the 1024 finite numbers
  of unit length, the model id, `1 ≤ k ≤ MAX_K` and the reader before any SQL; 23 tests, 81
  cases in `tests/retrieval/test_types.py`), `SearchHit` (what you return, including the
  document's `language` and `content_kind`), `VISIBLE_AUDIENCES`, `kb.store.vector_literal`, and
  `db.business_transaction`.
- **Not your job:** opening the transaction (the caller does it), refusing mixed model ids (PR 6
  calls `embeddings.guard.require_one_model` first), embedding the question, the "I don't know"
  threshold and the answer (PR 6), full-text and fusion (M3).

## 1. How to work

> **Postgres right now (2026-10-22):** Docker is unreachable on the Mac, so the 21 Postgres cases
> and the 2 canary cases run **only in CI**; locally they show `skipped`. The 2 binding cases need
> no database and run everywhere: a fast local signal, but only about the statement's shape.

> **While Docker is down.** The binding test checks only the statement's shape and bindings (one
> `execute`, nothing after it, every value a real bind, `[]` for no rows). It never sends the SQL
> to Postgres, so it cannot tell whether Postgres accepts the statement or which rows come back.
> CI runs on pull requests only (and on pushes to `main`), so each push to your PR is one full CI
> run: open a **draft PR** from `m2/retrieval` early, then read the `api (lint, types, tests)`
> log: `FAILED` with an assert diff = a wrong answer; `XPASS(strict)` = that case passes.
> **Question for you before you start (rule 10):** (a) get Docker Desktop working first
> (recommended; `make test-integration` needs it anyway); (b) go CI-only and lengthen the
> time-box; or (c) a local Postgres with pgvector, which is global software and needs your
> explicit yes first (rules 1 and 11). Nothing is installed until you choose.

```bash
cd apps/api
git switch main && git pull                  # once the scaffold PR is merged
git switch -c m2/retrieval
uv run pytest tests/retrieval -v             # types pass · binding: 2 xfailed · Postgres: 21 skipped
# write the body …
uv run pytest tests/retrieval/test_vector_search_binding.py -v
uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .
```

Push your branch and open a PR: CI's `api (lint, types, tests)` job runs every Postgres test and
the canary suite with `RUN_INTEGRATION=1`. Once Docker works locally, `make test-integration`
(repository root) starts the stack and runs the same tests; with the stack already up (`make up`),
`RUN_INTEGRATION=1` plus the two database URLs from the `Makefile` does it inside `apps/api`.

**Reading the output (strict xfail, as in M1).** Both test modules start with a `pytestmark` that
includes `xfail(strict=True, raises=NotImplementedError)`. While it is there:

| Your code | pytest shows |
|---|---|
| still raises `NotImplementedError` | `xfailed`: expected, not a failure |
| runs, but gives a wrong answer or raises something else | `FAILED`, with the assert diff |
| runs and the test passes | `XPASS(strict)`: reported as a failure **on purpose**; that is your signal |

When every case shows `XPASS(strict)`, remove **three** markers in your PR, then everything must
simply pass:
1. the `xfail` entry of `pytestmark` in `test_vector_search_binding.py`;
2. the `xfail` entry of `pytestmark` in `test_vector_search.py` (keep `integration` and `anyio`);
3. `"vector_search"` from `PENDING_HAND_WRITE` in `tests/canary/read_paths.py` (it also turns on
   PR 6's Postgres tests and PR 8's harness run, `tests/evalrun/test_postgres_harness.py`, which
   waits for H2 as well). The canary suite's
   `vector_search` read path already calls your function (as staff, the widest reader); until
   then its two cases are strict xfails, which `scripts/canary_gate.py` allows as a recorded gap.
   A leak there shows `FAILED`, never `XPASS`. Note what it proves: like every canary read path it
   runs as the app role under RLS, so it proves isolation **through RLS**, not your `WHERE`
   clause (a search without the clause passes it too). The clause is proven by
   `test_vector_search.py`'s owner-connection and other-business-transaction tests, which the
   canary gate does not cover. A proposal for zara and you once H1 lands: mirror those two checks
   into `tests/canary`, or make the gate require those test ids to pass.
   **PR 6 pointer (one switch, decided 2026-10-22):** removing this entry also turns on PR 6's
   canary paths (`GET /v1/kb/search`, `POST /v1/answer`) and the H1-dependent tests in
   `tests/answer/test_postgres_search_and_answer.py`, which all ask `pending_hand_write`. If one
   of those PR 6 tests fails in your H1 PR, ananya fixes that test in a separate commit; you never
   change H1 to make a PR 6 test pass (H1's own tests above are the ones your code answers to).

**Rules.** Do not edit a test to make it pass; change the code. If you think a test is wrong, say so
in your PR. `mypy` (strict) and `ruff` apply as everywhere. Commit with your own message (for
example `feat(retrieval): implement vector_search`), push, open a PR and ask `@agent-ananya-rag` to
review it (`@agent-zara-safety` also looks at the business filter). Then explain it back (§5).

## 2. The concepts (hint level 1)

### 2.1 Exact search vs ANN
Exact (brute-force) search compares the question with **every** candidate vector and sorts: recall
is 100 % by definition and the cost grows linearly with the number of chunks. An ANN index (HNSW,
IVFFlat) finds *approximately* the nearest neighbours much faster, but may miss some, and a
selective filter (one business among many) can leave it with too few results after filtering.
ADR 0008 chose exact search at pilot size; M3 revisits it with measured latency. So: no index, no
`SET` of index parameters, and nothing you write depends on one.

### 2.2 pgvector's operators, and why unit vectors make cosine equal the dot product
pgvector has `<->` (Euclidean distance), `<#>` (the **negative** inner product, so that "smaller is
closer" like every other distance, because Postgres index scans only go in ascending order) and
`<=>` (cosine distance = 1 − cosine similarity, from 0 to 2). Cosine similarity is `a·b / (|a||b|)`.
When both lengths are 1 the denominator is 1, so the cosine *is* the dot product, and
`|a − b|² = 2 − 2·a·b`: all three operators give the same order. The spec still pins one:
`<=>`, with `score = 1 − distance`. It survives vectors that are only nearly unit (stored ones are
checked to 0.001), its score is a familiar similarity in [−1, 1], and it matches the
`vector_cosine_ops` operator class should M3 add an index. pgvector stores `float32`, which is why
the tests use components that are multiples of 1/4: their sums are exact.

### 2.3 Why the business filter belongs inside the SQL, even with RLS
RLS is the second line of defence (ADR 0004, 0009). It depends on *who* is connected: a superuser,
a `BYPASSRLS` role and (without `FORCE`) the table owner all skip it, and the local and CI owner is
a superuser. The first line is your `WHERE` clause. Business-context §5 says the filter is part of
the search, never applied afterwards: filtering in Python after `LIMIT` would have read another
business's rows out of the database already, and would return fewer than `k` hits. One test runs
your function on the owner's connection, where only your `WHERE` clause can keep the other business
out; another opens the transaction for business B and asks for A, where both layers must agree.

### 2.4 Binding a vector as a parameter
Every value goes in as a bound parameter (`:name` in `text()`, and a dict), never by string
formatting: that is what makes SQL injection impossible, and it keeps one statement shape for every
question. Without the `pgvector` Python package there is no vector type on the Python side, so the
vector travels as text in pgvector's own format (`vector_literal`) and the SQL casts it, exactly as
`kb/store.py` writes chunks. Lists bind too: a Postgres array is how one parameter can hold several
allowed audiences (think `= ANY(...)`).

### 2.5 A deterministic order
Ties are real, not theoretical: the same paragraph in two sources (a price list and an FAQ that
quotes it) has exactly the same vector and so exactly the same score. Postgres promises no
order between equal sort keys, so without a tie-break two runs may differ, and so may eval numbers
(hit@k) on the same data. The tie-break keys must survive re-ingestion: `chunks.id` is random, so it
changes every time the corpus is loaded again. And text order depends on the *collation*:
linguistic collations (`en_US`) differ between operating-system library versions, so the spec
compares byte by byte with `COLLATE "C"`.

### 2.6 What may be retrieved
Only approved documents answer customers; draft and retired never do; staff-only material is never
quoted to customers (business-context §3.4). `VectorQuery.audiences` turns the reader into the
allowed audiences. Vectors of two models are never compared (ADR 0008), so the model id is part of
the filter. A chunk joins its document on **both** keys, as the composite foreign key does.

**Versions need no filter of their own.** A source has at most one approved version at a time
(decided 2026-10-22): migration 0004 (PR 5b, diego) adds a partial unique index on the approved
rows, starting with `business_id`, and approving a new version retires the previous one in the same
transaction. So `status = 'approved'` already keeps the old (retired) and the next (draft) versions
out. The version stays in the tie-break: with one approved version per source it never decides
between two candidates, but it keeps the order total on a database without 0004.

### 2.7 Inside the caller's transaction
`app.business_id` is set per transaction (ADR 0009, `set_config(..., true)`), so the caller opens
it and you only run one `SELECT` on the connection you are given; a `commit` here would end the
caller's transaction and, with it, the business setting. One transaction gives PR 6 one connection
and one business setting, but **not** one snapshot: the codebase sets no isolation level, so
Postgres' default READ COMMITTED applies, where each statement sees its own snapshot. If a reindex
commits between PR 6's model check and the search, the search simply finds no chunks of the old
model, or a mix that the model filter keeps apart: vectors of two models are still never compared.
PR 6 keeps READ COMMITTED (ADR 0011, accepted 2026-10-22): a single snapshot (REPEATABLE READ)
is not needed for correctness, because your model filter keeps the vectors apart.

## 3. Common mistakes
- Filtering or re-sorting in Python after the query (see §2.3).
- Sorting by score ascending, or returning the distance as the score.
- No tie-break, a tie-break on `chunks.id`, or text keys without `COLLATE "C"`.
- `LIMIT` inside a subquery *before* the status, audience or model filter: fewer than `k` hits.
- Hard-coding `'public'` instead of using the reader's audiences (the staff case catches it).
- Picking a "newest" version yourself (`DISTINCT ON`, `max(version)`): the status filter already
  does it (§2.6), and `version` is free text.
- An f-string, `%` or `exec_driver_sql` for any value; or a `begin`/`commit`/`set_config` here.
- In `text()`, `:name::vector` is not read as a parameter (Postgres would receive the text
  `:name::vector`); write `CAST(:name AS vector)`, as `kb/store.py` does.
- `<#>` without remembering that it is negative.

## 4. Test by test, with level-2 hints
Open a hint only for the test you are stuck on, and only after §2.

- **`test_one_statement_with_every_value_bound_and_none_in_the_sql`** (2 cases, no database).
  <details><summary>Hint</summary>

  The stand-in records every call. `execute` returns an empty result, so your function runs to
  its end and must return `[]`; any other method (a `commit`, say) stops it and shows up in the
  list of calls. The message names what it found in the SQL text, a parameter the statement does
  not really bind (§3, `CAST`), or which value it could not find among the bound ones.

  </details>
- **`test_the_k_nearest_come_back_best_first_with_their_cosine_scores`** (4 cases).
  <details><summary>Hint</summary>

  If the order is reversed, look at the direction of the sort; if the scores are 0, 0.25, 0.5 …,
  you are returning distances (§2.2). `k = 50` with six chunks must return six, not fail.

  </details>
- **`test_ties_are_broken_by_source_id_byte_by_byte_then_chunk_index`**.
  <details><summary>Hint</summary>

  If `a-faq` comes before `B-faq`, your text comparison is linguistic, not byte-wise (§2.5). If it
  fails on its first line ("premise: …"), the database's default collation changed, not your code.

  </details>
- **`test_only_the_approved_version_of_a_source_is_a_candidate`**.
  <details><summary>Hint</summary>

  The retired and the draft versions of `prices` are nearer than the approved `V3`, and one of
  them has exactly V3's chunk 0: only the status filter keeps them out (§2.6). The three approved
  sources tie, so the source id decides, byte-wise (`Hours` before `fees`).

  </details>
- **`test_another_businesss_nearer_chunks_are_never_returned`**,
  **`test_the_business_filter_is_in_the_sql_even_where_rls_cannot_help`** (2 cases: k = 1 and
  k = 50), **`test_a_transaction_for_another_business_finds_nothing`**.
  <details><summary>Hint</summary>

  The first can pass on RLS alone; the other two cannot. If they fail, the business is missing
  from your `WHERE` clause (§2.3).

  </details>
- **`test_only_chunks_of_the_querys_embedding_model_are_candidates`** (2 cases),
  **`test_draft_and_retired_documents_are_never_returned`** (2),
  **`test_staff_only_documents_reach_staff_but_never_a_customer`** (2).
  <details><summary>Hint</summary>

  Each failing case names a filter that is missing or a constant where `query` has the value.

  </details>
- **`test_every_filter_applies_before_the_limit`** (2 cases: k = 1 and k = 2).
  <details><summary>Hint</summary>

  Fewer hits than expected means a `LIMIT` was taken before one of the filters (§3).

  </details>
- **`test_no_candidates_means_an_empty_list`** (2 cases).
  <details><summary>Hint</summary>

  An empty result is a normal outcome, not an error.

  </details>
- **`test_each_hit_carries_what_a_citation_needs`**.
  <details><summary>Hint</summary>

  The diff shows the field: some come from the chunk, some from its document.

  </details>

## 5. The hint ladder, the time-box and decisions
The ladder is M1's: level 1 = §2, level 2 = §4, level 3 = pseudo-code in chat on request (never in
this file), level 4 = a pairing review where you type. **Time-box: 3 h** of writing (reading this
guide does not count). When it is up and you are stuck, go one level up. If you ask Claude to
write it, Claude reminds you of the learning goal once and respects your decision; the
explain-back stays part of acceptance.

**Decided 2026-10-22 (shesha):**

- **Versions: one approved version per source** (option c). Migration 0004 and an ADR come from
  diego in a small separate PR, **PR 5b**, before PR 6 exposes search: a partial unique index on
  the approved rows, starting with `business_id`, and approving a new version retires the previous
  one in the same transaction. Your SQL does **not** filter versions (§2.6). Every test plants data
  that is valid with and without 0004 (`plant` checks it).
- **A hit carries the document's `language` and `content_kind`** for PR 6's citations. There is
  still **no language filter** in H1: Q6 (cross-lingual and code-mixed questions) is open.
- **You write H1** on your own branch `m2/retrieval`, time-box 3 h.
- **Defaults accepted:** `MAX_K = 50`; `reader` = `customer` or `staff`, with PR 6 passing
  `customer` until M8 auth; `<=>` as the operator, score = 1 − distance; byte-order
  (`COLLATE "C"`) tie-break on source id, then version, then chunk index; a business whose chunks
  carry several model ids is refused by PR 6 (`require_one_model`), not here; a past `review_by`
  date does not hide a document.

**Still open:** how you run the Postgres tests while Docker is down, (a), (b) or (c) from §1.

## 6. Explain it back (part of acceptance)
- RLS already filters every row, so why does `vector_search` name the business in its `WHERE`
  clause? Name two situations in which only that clause protects.
- For unit vectors, why do `<=>`, `<#>` and `<->` give the same order? Which one did we pin, how is
  the score defined, and why is `<#>` negative?
- Why is the vector bound as a parameter and cast inside the SQL? Name two different things that
  go wrong with an f-string.
- Why does the tie-break use source id, version and chunk index with `COLLATE "C"`, and not the
  chunk id or nothing? What does that have to do with eval numbers?
- What do we gain and lose with exact search instead of an ANN index at pilot size, and what can
  happen when an ANN index meets a selective business filter?
- Why does search compare only one model id's vectors, and what does a business's search return
  after the configured model changes but before the reindex?
- Your SQL never mentions versions. What guarantees that a superseded version is never quoted,
  and why does the version still appear in the `ORDER BY`?
