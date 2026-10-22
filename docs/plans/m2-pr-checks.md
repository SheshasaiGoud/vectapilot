# M2 — acceptance checks per PR

> **Status (2026-10-22):** PR 0–5, the H1 scaffold and PR 5b merged; PR 6 built on
> `m2/search-and-answer` (not yet a PR).

ADRs 0008 (embeddings) and 0009 (database roles and RLS) record the decisions and their sources.
This file holds what each M2 PR must prove; each PR copies its section into its own acceptance
check. "PR 0–8" are plan step numbers, not GitHub pull-request numbers. Tags: **[0008]** /
**[0009]** = from that ADR · **[decided]** = below · **[note]** = Maya's note, resolved in that PR.

## Decided 2026-10-21 (shesha: "yes to all recommendations")
1. **Eval v0:** 40+ human-written questions in a customer's voice, labelled synthetic; 1–2 friends
   write about 10 without seeing the documents.
2. **Embeddings:** hosted Voyage AI over plain REST with `httpx2`, no SDK, once price, data
   retention and Hindi/Telugu support are checked (ADR 0008; what is still unverified is in PR 4).
3. **Business per request:** an `X-Business-Id` header until auth (M8), refused in production
   (ADR 0009).
4. **Faithfulness judge in M3;** M2 reports correctness from human labels.
5. **Eval v0 languages:** English plus about 10 Hindi/Hinglish questions.

## ⚠ Playbook deviations
- Eval v0 questions are synthetic (human-written), not "40+ real questions"; every report says so.
  Replace or extend them once a real pilot business exists (needed for credible M11 results).
- Full-text search (`tsv` column) moves to M3, with hybrid fusion. M2 is vector-only.
- The faithfulness judge moves to M3; M2 has no faithfulness number.

## Out of scope for M2
Full-text, hybrid fusion (RRF) and reranking (M3) · faithfulness judge (M3) · ANN index (revisit in
M3) · authentication (M8) · document extraction (M6) · embedding cache in Redis (M9) · a Telugu
eval set · real customer data · the admin screen.

## PR 0 — `m2/adrs`
- ADR 0008 and 0009 written; every Voyage fact has a URL and "checked 2026-10-21"; each unverified
  fact appears under PR 4. CLAUDE.md lists both ADRs; links resolve.

## PR 1 — `m2/db-roles-rls`
- **[0009]** Migration: the app role (`NOSUPERUSER NOBYPASSRLS`, owns nothing, minimal grants) and
  the first business-owned tables with non-null `business_id`, RLS enabled and forced, policy with
  `nullif(current_setting('app.business_id', true), '')` in `USING` and `WITH CHECK`.
- **[0009]** Chunks store `embedding vector(1024)`, the embedding model id and dimension **[0008]**.
- The business registry table (ids validated as `BusinessId`) and `llm_calls` with RLS, carried
  over from ADR 0006 ("Deferred: `llm_calls` table + RLS, registry check (M2)").
- **[0009]** API and Alembic use different database URLs; startup fails if the app role is a
  superuser, has `BYPASSRLS` or can act as a relation's owner (one test each). No password in git.
- CI fails the session when `GITHUB_ACTIONS=true` and `RUN_INTEGRATION` is not `1` (Zara).
- **[note]** How the role and its password are created (init script or migration) is settled here.
- **[0009]** One helper opens a transaction and runs `set_config(..., true)` first; the
  `X-Business-Id` dependency validates with `BusinessId`, gives the same error for unknown and
  malformed ids, and returns 400 outside the `local`/`test`/`ci` allowlist.
- **Before M10** (Zara): `db_provision.py` builds the SCRAM-SHA-256 verifier client-side (stdlib
  `hashlib`/`hmac`) and sends that instead of the plaintext password.

## PR 2 — `m2/canary-suite-ci`
- **[0009]** CI `api` job gains a Postgres service (pgvector image pinned by digest) and runs the
  canary suite on every PR: cross-business reads through SQL, vector search and API routes; no
  business ⇒ 0 rows (fresh connection and reused pooled connection); app role attributes; catalog
  check that every table with `business_id` has RLS and `FORCE`.
- Proved on purpose: dropping `FORCE`, or connecting as the owner, turns the suite red.
- **[PR 1 CI]** Catalog check: every UNIQUE constraint and unique index on a business-owned table
  starts with `business_id`. Uniqueness is enforced across all rows, ignoring RLS, so a key
  without it lets one business learn from an error that another's row exists (found in PR 1's
  first CI run on `chunks`, fixed 2026-10-21).
- Recheck Zara's PR 1 nits: find the tables from `information_schema` (not a hard-coded list);
  migration 0002's downgrade must not drop an app role that existed before it; `RESET
  app.business_id` when a connection returns to the pool.

**Built (2026-10-21, Zara; not yet run against Postgres: Docker is unreachable on the Mac, so the
live run, including every red proof, happens in CI or once Docker works).** Suite:
`apps/api/tests/canary/` (`test_leakage.py`; decisions in `checks.py`, queries in `catalog.py`).
- Postgres service pinned to `pgvector/pgvector:pg18@sha256:2358fcba…bc11` (read 2026-10-21).
  CI fails if no canary test passed or one was skipped: `scripts/canary_gate.py` reads pytest's
  JUnit report (xfail allowed).
- Tables come from `pg_catalog` (every relation with `business_id`; `information_schema` hides
  what the reader may not see): `test_the_catalog_finds_the_business_tables`. RLS + FORCE:
  `test_every_business_table_has_rls_enabled_and_forced` (views need `security_invoker`;
  materialized views fail). Policy exactly ADR 0009's, no looser permissive one:
  `test_every_policy_is_exactly_the_business_policy`. Unique/exclusion keys:
  `test_every_unique_key_starts_with_business_id`. Foreign keys into business tables pair
  `business_id`: `test_foreign_keys_into_business_tables_keep_the_business`. App role:
  `test_the_suite_runs_as_an_app_role_that_cannot_skip_rls`.
- Reads (SQL): `test_no_business_means_no_rows_on_a_{fresh,reused_pooled}_connection`,
  `test_a_business_sees_only_its_own_rows_on_a_{fresh,reused_pooled}_connection`. **Extension
  point:** vector search (H1) and API routes (PR 5/6) are not built yet; each adds its path to
  `READ_PATHS` in `tests/canary/read_paths.py`, which
  `test_no_read_path_shows_another_businesss_canary` runs for both businesses.
- Proved on purpose, live in CI (each breaks one thing in a rolled-back transaction):
  `test_red_proof_dropping_force`, `…_a_unique_key_without_business_id`, `…_an_open_extra_policy`,
  `…_an_app_role_that_owns_a_table`, `…_a_role_that_skips_rls_leaks_the_canary` (connecting as
  the owner). Without Postgres: `tests/test_canary_checks.py`.
- Nit 2: migration 0002 marks the role it creates (a role comment) and the downgrade drops only a
  marked role: `test_the_downgrade_drops_only_a_role_the_upgrade_created`. 0002 was edited after
  merge (no deployed database; an older local one keeps its role on downgrade).
- Nit 3, **not done, for shesha:** a pool `RESET` must run *after* the pool's rollback (a RESET
  inside the rolled-back transaction is undone) and inside SQLAlchemy's async greenlet; it can't be
  verified here. `business_transaction` never sets a session value. The gap is pinned by
  `test_a_session_level_business_does_not_survive_the_pool` (`xfail(strict=True)`).
- Exception for shesha to confirm: a primary key on one `uuid` column defaulting to
  `gen_random_uuid()` passes the key check (unguessable), valid only while no API accepts a
  client-chosen id. Alternative: `PRIMARY KEY (business_id, id)` in a new migration.

- **[decided 2026-10-21]** (shesha) The pool-return `RESET app.business_id` waits until it can be
  proven against Postgres (pinned by a strict-xfail test); random-uuid primary keys stay exempt
  from the unique-key rule; Redis and the `compose.yaml` images get pinned by digest in a later
  small PR (with M10's deployment hardening at the latest).

## PR 3 — `m2/ingestion` (ananya)
- Loaders for Markdown, HTML and CSV (standard library) and PDF (`pypdf`, page by page; needs
  shesha's rule-11 yes on install day). Every chunk carries document, version, page (if any) and
  type for filters and citations. Nothing is hard-coded to one business.
- Configurable, heading-aware chunking; the size comes from the business config and is tuned on
  the dev split only. Golden files per format, including PDF page numbers.

- **[decided 2026-10-21]** (shesha) `documents.source_type` is the file format (markdown, html,
  csv, pdf); the content kind from business-context §3.4 (faq, pricing, policy …) becomes its own
  field when business configs list their sources (PR 5). Non-UTF-8 text and encrypted PDFs are
  refused; DOCX later. Limits (10 MiB, 500 pages, 2M characters) and chunk defaults (1,200
  characters, 150 overlap) are unmeasured starting values, tuned on the dev split only (PR 8).
  No ADR for chunking: configuration, documented here and in the code.

## PR 4 — `m2/embeddings`
- **[0008]** One embeddings module; only its adapter knows the Voyage URL. `httpx2` declared as a
  direct runtime dependency (`uv.lock` diff reviewed). Requests send `input_type` and
  `truncation=false`; at most 1,000 texts and the model's token limit per request.
- **[0008]** Deterministic fake embedder; `test`/`ci` ⇒ fake, `production` ⇒ not fake.
- **[0008]** `VECTAPILOT_VOYAGE_API_KEY` as ADR 0007; canary tests show the key in no error text.
- **[0008]** Search refuses mixed model ids; a model change means a reindex (test).
- **Verify before the first live call** (shesha, from Voyage's docs or dashboard, dated): Hindi and
  Telugu support; API region and data residency; whether a spend limit exists; limits without a
  payment method; the training opt-out is done (it needs a payment method and cannot be undone
  in the dashboard). Price and model id re-checked on the day.
- **[PR 4 build] Also verify on the first live call:** the statuses Voyage sends match
  `embeddings/voyage.py`'s table; whether a request-id header exists (none is read); the reported
  `usage.total_tokens` stays under `base.token_upper_bound` (UTF-8 bytes + 8 per text, an assumed
  bound used for batching).

- **[decided 2026-10-21]** (shesha) Retrying failed embedding batches (via H2's
  `call_with_retries`; every error carries `retryable` and `retry_after`) and the metadata-only
  record per embedding call move to PR 5. Each chunk stores the model id Voyage returns (two ids
  in one response are refused; search refuses mixed models). The token bound "UTF-8 bytes + 8
  per text" is unverified: compare it with Voyage's reported tokens on the first live call.

## PR 5 — `m2/kb-endpoints` (diego)
- `POST /v1/kb/documents` and `POST /v1/kb/reindex`, synchronous (background workers are M9),
  scoped by `X-Business-Id`. Re-ingesting the same document version is idempotent (test).

**Built (2026-10-21, Diego; Postgres parts not yet run: Docker is unreachable, they run in CI).**
- Request format: the file is the raw body, `Content-Type` = its format, metadata in query
  parameters (`kb/router.py` says why: no `python-multipart`, no base64, size checked while
  streaming). Same bytes under a stored version ⇒ 200 and no embedding; other bytes ⇒ 409.
- Migration 0003: `documents.content_kind` (§3.4 list, CHECK constraint, no default).
- `embeddings/runner.py`: per-batch retries through H2's `call_with_retries` (the gateway's
  retry policy) and one `embedding_call` log record per batch, metadata only.
- Canary: `tests/canary/write_paths.py` (`WRITE_PATHS`) with
  `test_no_write_path_changes_another_businesss_rows`, `test_a_write_path_without_a_business_is_refused`
  and `test_red_proof_a_write_into_another_business_is_caught`.
- **[decided 2026-10-21]** (shesha) Content kinds accepted as built (faq, service_list,
  product_catalogue, pricing, appointment_policy, other). Same-bytes re-upload is a no-op,
  embedding retries reuse the `llm_*` retry settings, and reindex holds a document's chunks in
  memory (fine at pilot sizes). Query strings carry customer text, so `log.py` strips them from
  uvicorn's access log (`DropQueryString`, tested).

## H1 — `m2/retrieval` (`[HAND-WRITE]`, shesha)
- ananya provides the interface, tests and a short guide; shesha writes top-k exact vector search
  with the `business_id` filter inside the SQL (vector only; fusion is M3). The canary suite
  stays green.

**Scaffold (2026-10-21, ananya; branch `m2/retrieval-scaffold`; Postgres parts not yet run:
Docker is unreachable, they run in CI).** `retrieval/types.py`: `VectorQuery` (all checks that
need no database) and `SearchHit` (citation fields, including the document's `language` and
`content_kind`), 23 tests / 81 cases in `tests/retrieval/test_types.py`, passing.
`retrieval/vector.py`: `vector_search`, body `raise NotImplementedError`, docstring = the spec
(`<=>`, score = 1 − distance, tie-break source id → version → chunk index with `COLLATE "C"`,
`1 ≤ k ≤ 50`, approved + reader's audiences + one model id, the caller's business transaction,
every value bound). Strict-xfail contract tests:
`test_vector_search.py` (12 tests / 21 cases, Postgres; one runs on the owner's connection, where
RLS cannot help, at k = 1 and k = 50; one checks that every filter applies before the `LIMIT`)
and `test_vector_search_binding.py` (2 cases, no database: one `execute`, nothing after it, every
value a real bind). Canary: the `vector_search` read path is wired and listed in
`PENDING_HAND_WRITE` (a strict xfail, allowed by the gate) until H1 lands. It runs as the app role
under RLS, so it proves isolation through RLS, not the `WHERE` clause; the clause is proven by
`test_vector_search.py`'s owner-connection and other-business-transaction tests, which
`canary_gate.py` does not cover (proposal for zara and shesha after H1: mirror those two checks,
and PR 5b's
`tests/kb/test_postgres_store.py::test_the_retire_names_the_business_even_where_rls_cannot_help`,
into `tests/canary`, or make the gate require their test ids to pass). Guide:
`docs/learning/m2-retrieval.md`; its §5 lists the decisions. Still open: how H1 is tested while
Docker is down (guide §1).

- **[decided 2026-10-22]** (shesha) Versions: at most one approved version per source (option c),
  enforced by migration 0004 in PR 5b; H1's SQL does not filter versions, and every H1 test plants
  data valid with and without 0004. `SearchHit` carries the document's `language` and
  `content_kind` for PR 6's citations; no language filter in H1 (Q6 stays open). shesha writes H1
  on `m2/retrieval`, time-box 3 h. Defaults accepted: `MAX_K` = 50, reader `customer`/`staff`,
  `<=>` with score = 1 − distance, `COLLATE "C"` tie-break, mixed model ids refused by PR 6 via
  `require_one_model`, `review_by` not filtered.

## PR 5b — `m2/one-approved-version` (diego)
- Small and separate; **must land before PR 6** exposes search.
- Migration 0004: a partial unique index on `documents` that starts with `business_id`, over
  `(business_id, source_id)` where `status = 'approved'` (the canary key check applies to it).
- Approving a new version (today: uploading it with `status=approved`) retires the previously
  approved version of the same source **in the same transaction**. Test: afterwards exactly one
  approved version, the new one; if the write fails, the old one is still approved.
- An ADR recording option (c) and why (business-context §3.4: stale documents are the top cause
  of wrong answers).

**Built (2026-10-22, Diego; Postgres parts not yet run: Docker is unreachable, they run in CI).**
ADR 0010. Migration 0004: `documents_one_approved_version` on `(business_id, source_id) WHERE
status = 'approved'`; existing duplicates stop the upgrade with a fixed message naming no data.
`kb/store.py`: an approved save retires the previous approved version, then inserts, in one
transaction (`retired_version` in the response); a version stored meanwhile rolls the retire
back; the index refusing a concurrent approval is `409 approval_conflict`. Same bytes with another
status (e.g. re-approving a retired version) is `409 status_conflict`. No new grant (0002 already
gives `UPDATE` on `documents`). Canary: the key check reads a partial index's `WHERE` (pure tests
and a red proof), and the write path `POST /v1/kb/documents (approve a new version)` leaves the
other business's approved `canary` source untouched (under RLS: it proves RLS, not the store's
`WHERE`). Tests: `tests/kb/test_approval.py` (local, including the retire's SQL text: business,
source, `status = 'approved'`, another version), `tests/kb/test_postgres_store.py` and
`tests/test_migrations.py` (Postgres: one approved version afterwards, retiring only the approved
version of that source while a draft and another source stay, rollback on a failed write, the
concurrent approval, the index, the store's own `business_id` filter on the owner's connection,
0004's downgrade/upgrade and its refusal). That owner-connection test is outside `canary_gate.py`,
like H1's (see the H1 proposal above).
- **[decided 2026-10-22]** (shesha) Migration 0004 stops on existing duplicates with its fixed
  message (nothing is retired by guesswork). Same bytes with another status is
  `409 status_conflict` (to roll back, upload as a new version; revisit with M8's approve step).
  The response keeps `retired_version`.

## PR 6 — `m2/search-and-answer` (ananya)
- `GET /v1/kb/search?q=` returns only the business's approved public chunks.
- A cited answer through the gateway's `complete_structured`: every cited chunk id was retrieved
  for that business; weak or no evidence ⇒ an honest "I don't know" or a hand-off, no invented
  facts. Each answer carries `sources[]`, `confidence` (v0 = top retrieval score, labelled
  uncalibrated), `route` = `rag` (until M4) and `trace_id`.
- Retrieved text is data, never instructions: it is delimited in the prompt, and adversarial
  documents are part of the tests.

**Built (2026-10-22, ananya; Postgres parts not yet run: Docker is unreachable, they run in CI,
and those that reach `vector_search` wait for H1).**
- `GET /v1/kb/search?q=&k=` (`retrieval/search.py`, `retrieval/router.py`): the question is
  embedded as `query` through the runner (one `embedding_call`, with the request's `trace_id`),
  `require_one_model` runs (else `503 reindex_required`), then H1's `vector_search` as `customer`,
  both in one business transaction (READ COMMITTED). `q` 1–2,000 characters, stripped, valid
  Unicode; `k` 1–50, default 5. No threshold. Typed errors with fixed messages; nothing echoed.
- `POST /v1/answer` with `{"question": ...}` (`answer/`): evidence = hits scoring at least
  `rag_min_score` (0.3: **provisional and unmeasured**, tuned on the dev split in PR 8). No
  evidence ⇒ `dont_know` (or `handoff` by setting) with no model call. Otherwise the versioned
  prompt (`rag-answer.1`), `complete_structured` (alias `large`), then fail-closed verification:
  shown only if the model answered, wrote a text and cited ≥ 1 id, and every cited id was in that
  prompt; else the weak-evidence outcome, no text. Response: `outcome`, `answer` (null unless
  answered), `sources` (cited chunks only), `confidence` (top score, `calibrated: false`),
  `route: rag`, `trace_id` (also in the `embedding_call`, `llm_call` and new metadata-only
  `rag_answer` records), `prompt_version`.
- Prompt: each chunk in a block whose markers carry a boundary that no untrusted text contains
  (deterministic, checked); documents first, question last. Tests: forged markers, injected
  instructions, fake system prompts, forged ids, a model that follows them. `PROMPT_FINGERPRINTS`
  pins each version's sha256 (fixed texts, a canonical rendering, `ModelAnswer`'s schema): CI
  fails when the prompt changes without a new `PROMPT_VERSION`.
- Canary: `GET /v1/kb/search` and `POST /v1/answer` (its prompt too) are read paths, pending on
  H1 through `NEEDS_HAND_WRITE` + `PENDING_HAND_WRITE`; `tests/answer/test_postgres_search_and_answer.py`
  uses the same switch, so H1's step 3 turns all of it on. Both businesses carry the same `bait`
  chunk (configured model, the fake's vector of one shared text, titled with their own canary)
  and the API paths ask it, so the other business's bait is a same-model candidate scoring 1.0:
  a leaking route would show its canary (zara's review, Z1). Local proof of the plumbing:
  `tests/test_canary_checks.py` (no Postgres, including a red proof over that bait). To add once
  H1 lands (zara, ananya): a Postgres red proof that an API path turns red.
- Open for shesha (options in the proposed [ADR 0011](../adr/0011-cited-answers-v0.md), status
  Proposed, and in the PR description): threshold value, `dont_know` vs `handoff`, dropping a
  whole answer on one unverified citation, answer language (Q6), isolation level, per-business
  configuration, the question limit, the endpoint's shape, trace-id plumbing **including error
  responses** (typed 502/503 bodies carry no trace id today; proposal: an `X-Trace-Id` header on
  every `/v1/kb/search` and `/v1/answer` response); typed errors (503/502) vs `dont_know` for
  `reindex_required` and model failures; **the paid HTTP path before M7's budgets** (ADR 0006
  "Exposure" says no HTTP route, but with `llm_provider=anthropic` every `POST /v1/answer` with
  evidence makes an uncapped `large` call; proposal: the API refuses `anthropic` unless an
  explicit opt-in setting, default off, and amend ADR 0006); one switch vs a separate flag for
  PR 6's H1-dependent Postgres tests (they first run inside H1's PR); how PR 6 lands (one PR with
  owner reviews, or a two- or three-way stacked split).
- **[decided 2026-10-22]** (shesha) ADR 0011 accepted with every recommended default
  (threshold 0.3, provisional and unmeasured, tuned in PR 8 on the dev split; `dont_know` until
  M8 can alert staff; any unverified citation drops the whole answer; answer in the question's
  language until Q6; READ COMMITTED; platform defaults until a per-business config loader;
  `POST /v1/answer`; 2,000 characters; typed 502/503 errors). An `X-Trace-Id` header on every
  search and answer response, errors included. Until M7 budgets, the API refuses a paid provider
  unless `api_allow_paid_llm` is on (ADR 0006 amendment). One PR. One switch for H1: if a PR 6 test
  fails in the H1 PR, ananya fixes her test in a separate commit; H1 never changes to suit it.

## PR 7 — `m2/eval-v0` (noah)
- The corpus: synthetic documents for two fictional businesses under `data/`, labelled synthetic.
- **[decided 1, 5]** 40+ questions, ~10 Hindi/Hinglish, ~10 written by friends without seeing the
  documents; every category except `action` (M5); each has its expected source chunk(s) and a
  human correctness label slot. A held-out test split is frozen and never tuned on.

## H2 — `m2/metrics` (`[HAND-WRITE]`, shesha)
- hit@k and MRR, with noah's tests and a short guide.

## PR 8 — `m2/eval-harness` (noah)
- The eval harness and the baseline report: hit@5, MRR and correctness, each with dataset
  version, commit, model ids and config, reproducible from the commit. The fake embedder's numbers
  are never reported as results.

## PR 9 — close-out (shesha's live run)
- Live ingestion and the eval v0 run; human correctness labels; a "what I measured" note with
  measured hit@5, MRR and correctness against the §6 targets, per language. No target is claimed
  unless met. The study PDF for M2.
