# M2 — acceptance checks per PR

> **Status (2026-10-22):** PR 0–8, PR 5b and the H1 and H2 scaffolds merged (GitHub #23–#34; PR 8
> is #34). PR 9 prepared (#35). H2's bodies written by Claude at shesha's request (`m2/metrics`).
> What remains is shesha's: H1, the H2 explain-back, the questions
> and freeze, accounts and keys, the live run, the note, the explain-backs and the quiz, in order
> in [`m2-close-out.md`](m2-close-out.md).

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
  documents; every category except `action` (M5) and `document` (M6, decided below); each has its
  expected evidence (groups of anchor keys, decided below) and a human correctness label (per
  run). A held-out test split is frozen and never tuned on.

**Built (2026-10-22, noah; everything except the questions, which are shesha's and friends').**
- Corpus `data/sample/` (README: synthetic, fictional, fake contacts, `.example` domains, 112 as
  the only real number): `sunrise-clinic` and `paws-and-co`, 9 documents each (18; 129 chunks at
  the default size), Markdown, HTML, CSV and one PDF (`fees-2026-04.pdf`, built from its
  `.pdf.txt` by `tests/ingestion/pdf_builder.py`, no new package), all six content kinds, a
  staff-only document per business, a retired `fees` version (Sunrise) and a draft (Paws), the same
  source ids with confusable facts in both, emergency guidance without medical advice, and one
  Hindi FAQ (`faq-hi`, `review: pending`). A manifest per business carries the upload parameters.
- Dataset `evals/datasets/support_qa/v0.1/`: `manifest.json` (`stage: collecting`, split rule,
  requirements 40 / 10 Hindi / 10 friend_blind / 3 per category / 15 per business / 3 safety
  per business / 8 dont_know),
  `questions.jsonl` **empty**, README (schema, categories, keys, split, freeze, versions),
  `friends-brief.md` + `friends-template.csv`. Guide: `docs/learning/m2-eval-questions.md`.
- Code `vectapilot_api/evalset/` (no model, network or database): evidence is a list of groups
  of interchangeable `(source_id, version, anchor)` keys (anchor 8-100 characters) resolved
  through the real loaders and chunker to chunk indexes at a given chunk size; the
  content-relevance rule PR 8 scores with (`relevance.py`); row checks (placeholders and
  personal data refused); `ready` with a dry-run split preview; coverage counts (split × category, author,
  language, business); `stratified-systematic-v2` split, a balance check that `freeze` enforces,
  `replaces` for corrected questions in a later version, `freeze.json` pins (questions, corpus
  files, a hash of each corpus manifest); per-run `CorrectnessLabel`. CLI:
  `uv run python -m vectapilot_api.evalset check|ready|report|lookup|show|freeze|import-friends`;
  `make eval-check`. Tests: `tests/evalset/` (no Postgres).
- Review fixes (2026-10-22, kenji, zara, ananya, maya): v1's split rule never put position 0 in
  test and sorted `friend_blind` first, so friends' questions almost never reached test (measured
  on attribute-only rows); replaced by v2 before any question existed. Never-answer documents now
  use values found in no approved document (a test checks it); emergency lists are examples with
  a catch-all line, and `faq-hi`'s emergency section matches the English one (still
  `review: pending`); two product names made obviously fictional; the "relevant hit" rule is by
  content, not chunk number; corpus statistics after the edits: 129 sections → 129 chunks at
  1200/150 (0 split; longest section 936 characters, median 167), 138 at 400/50 (6 split), 184 at
  200/40 (32 split), so v0.1 cannot meaningfully tune the chunk size.
- CI stays green with zero rows (`collecting` checks rows only); `freeze` switches the manifest to
  `frozen`, and the same test then enforces the requirements and pins.
- Repository layout (this plan only): `data/sample/<business_id>/` (synthetic corpus) ·
  `evals/datasets/support_qa/<version>/` (eval v0) · `apps/api/src/vectapilot_api/evalset/`.
- **[decided 2026-10-22] (shesha)** every recommendation accepted. (1) Businesses
  `sunrise-clinic` + `paws-and-co`; `faq-hi` stays `review: pending` (now per document in the
  corpus manifest) until shesha reviews it, and `freeze` refuses while any corpus document is
  pending; 112 stays. (2) JSONL; a key is `source_id` + `version` + verbatim anchor (8-100
  characters); correctness labels per run. (3) `expected_evidence` replaces `expected_sources`:
  groups of interchangeable keys, any key finds its group; `answered` ≥ 1 group, `multi_hop` ≥ 2,
  `out_of_scope` and `dont_know` 0, `handoff` optional; the content-relevance helper is
  `evalset/relevance.py` (typed, tested), and H2 works on ranked groups. (4) Split v2 at 30 %;
  when `freeze` refuses on balance, add or remove one question, never change the seed (README);
  an optional hashed `split_group` (renamed from `group` so it cannot be mistaken for an
  evidence group) makes translations and cross-business twins share one split, and identical
  text in two businesses is allowed only when they share one. (5) Eight required categories,
  one precedence order: safety > handoff_request > adversarial > out_of_scope > ambiguous >
  multi_hop > policy > faq; `action` and `document` refused, `import-friends` drops the rows
  shesha triages as such and reports the count; optional hashed `subtype` (safety: emergency |
  professional_judgement; out_of_scope: off_domain | unanswerable | non_public_only);
  `ambiguous` is answered, with a group per reading; refund requests are `handoff_request`,
  refund-policy questions `policy`. (6) Ready minimums: 40 total; 10 Hindi/Hinglish counted only
  for a business whose manifest lists `hi` (Sunrise; the brief invites Hindi for Sunrise only);
  10 friend_blind; 3 per category; 15 per business; 3 safety per business; 8 `dont_know` (some
  about staff-only, retired or draft-only facts); a question in a language its business does
  not list expects `handoff`. (7) `author_type` is `team` | `friend_blind`; a "prices are
  fictional" caveat is ignored when labelling. (8) Friends may write everyday emergencies, but
  nothing about self-harm and no real health details; shesha writes the sensitive safety
  questions.
- Mutation-audit gaps closed (2026-10-22): a golden test pins `assign_splits` on a TEST-ONLY
  layout where two category repairs compete, and a subprocess runs it under two
  `PYTHONHASHSEED` values that iterate the categories in opposite orders (M2b/M2f; checked: a
  set-ordered repair fails it); case-only anchors and source ids never resolve or match
  (M3a/M3d); the CLI's `freeze` refuses placeholder rows (M5b); the `example-` id rule and
  lowercase markers (M5e/M5f); markers match on NFKC-casefolded text with
  `example[\W_]*only|test[-_\u2010-\u2015\u2212]+only` (D5b, narrowed 2026-10-22: the test
  marker needs a hyphen, underscore or dash, so a real "blood test only" passes);
  manifest errors keep their rule text (D10).

## H2 — `m2/metrics` (`[HAND-WRITE]`, shesha)
- hit@k and MRR, with noah's tests and a short guide.
- **Input (decided 2026-10-22, item 3):** per question, its ranked hits as
  `evalset.relevance.matched_groups` gives them (for each hit, the indexes of the evidence groups
  it finds; empty = not relevant) and its number of groups. `relevance.py` only says which hit
  finds which group; the metrics on top are H2's, including how several groups count (for
  `multi_hop`: any group in the top k, or all of them), a question for H2's guide.

**Scaffold (2026-10-22, noah; branch `m2/metrics-scaffold`; everything runs locally, no
database).** `evalset/ranking.py` (built, not hand-write): `RankedRelevance` (question id,
`n_groups` ≥ 1, `groups_by_rank` = per rank the group indexes `matched_groups` gives; checked when
built), `ranked_relevance` (a question and its ranked hits → the record), `select_for_retrieval`
and `has_evidence` (questions without evidence are listed as not scored, never dropped),
`check_k`, `check_results`; 20 tests / 38 cases in `tests/evalset/test_ranking.py`, passing.
`evalset/metrics.py`: `hit_at_k`, `all_groups_at_k`, `reciprocal_rank`, `mean_hit_at_k`,
`mean_reciprocal_rank`, bodies `raise NotImplementedError`, docstrings = the spec (ranks from 1;
relevant = finds any group; `k` an int ≥ 1, a `k` beyond the list allowed; RR cut off at `k`,
first relevant hit only; exact `Fraction` results; an empty set or a question listed twice is a
`ValueError`). Strict-xfail tests: `tests/evalset/test_metrics.py`, 38 tests / 142 cases,
hand-computed literals, property tests on hand-chosen records. Checked against a scratch reference
kept outside the repository (deleted afterwards) and 19 deliberately wrong variants (0-based
ranks, the wrong n, hits counted as groups, no cut-off, float results, …), each failing at least
one test. Guide: `docs/learning/m2-metrics.md`; its §5 lists the open decisions (D1 any or all
groups, D2 MRR's depth, D3 `Fraction`, D4 which questions count, D5 time-box 2 h on
`m2/metrics`, D6 the metric's k against the search's k), each with a recommended default the code
follows today.

**Review fixes (2026-10-22; ananya, kenji, maya).** Records are built from the search's ranked list
before the score threshold (`AnswerRun.hits`, never `AnswerRun.evidence`), so hit@k and MRR@k do
not move with `rag_min_score`; unscorable questions are PR 8's third count, taken out before
`ranked_relevance`. New cases: a non-int `k` (`5.0`, `"5"`) for all five functions, a repeated
question id with a different record (in both modules), and a 2/3 and 4/9 mean with type checks.
Checked again against a scratch reference (115 of 115 passed; deleted afterwards) and three more
wrong variants (a float inside a `Fraction`, duplicates found by record equality, a hand-rolled
`k < 1` check), each failing at least one new case. Guide: scored n in the small-n example
(intervals now a proposal), MRR is not a proportion, clustered questions, the naming of hit@k,
`x of n` for counts, explain-back questions without printed answers.

**Test gaps closed (2026-10-22, noah).** A bad `k` is now also refused for an empty hit list in
all five functions (an early "no hits" return before `check_k` passed before; 25 → 50 cases); an
MRR of 30191/3027273 (ranks 97, 101, 103 at k = 103), whose denominator a float mean rounded back
with `limit_denominator()` cannot recover (2/3 and 4/9 it can; the comment saying otherwise is
fixed); a 3-group `[0, 0, 1]` that is not "all groups" at k = 3. Checked against a scratch
reference (142 of 142 passed with `--runxfail`; deleted afterwards): each early-return variant
fails only the new empty-list cases, the `limit_denominator()` variant only the new MRR test, and
counting hits as groups now fails 3 tests instead of 2.
- **[decided 2026-10-22]** (shesha) D1–D4 and D6 as recommended: hit@k = any group, plus
  `all_groups_at_k` reported as `x of n` for questions with two or more groups; MRR@k at the same
  k as hit@k (k = 5); exact `Fraction` results; every question with at least one evidence group
  is scored; PR 8 asserts every metric k ≤ the search's k. shesha writes the bodies on
  `m2/metrics` (time-box 2 h). For PR 8's report, every retrieval rate carries an interval
  (Wilson for hit@k counts, a bootstrap over questions for MRR). There is also an "evidence
  complete" count (`all_groups_at_k` on the prompt's evidence) for `multi_hop` and `ambiguous`,
  and questions unscorable at a config are a third count, never dropped.
- **Written (2026-10-22, Claude at shesha's request, `m2/metrics`).** Five bodies, about 20
  lines; 142 of 142 cases pass; "retrieval_metrics" removed from `PENDING_HAND_WRITE`, which
  also turns on PR 8's scoring tests (all pass). shesha's explain-back (guide §6) is still due.

## PR 8 — `m2/eval-harness` (noah)
- The eval harness and the baseline report: hit@5, MRR@5 (H2, decided 2026-10-22) and
  correctness, each with dataset version, commit, model ids and config, reproducible from the
  commit. The fake embedder's numbers are never reported as results.
- Every metric k is at most the k passed to the search, asserted per run; the report names both.
- Every retrieval rate carries an interval (Wilson for hit@k counts, a bootstrap over questions
  for MRR) next to its `x of n`; plus an "evidence complete" count for `multi_hop` and
  `ambiguous` (decided 2026-10-22).
- A hit is relevant by content (`evalset/relevance.py`: `hit_matches_key`, `matched_groups`):
  same business, same `(source_id, version)`, and its normalised text contains the anchor; any
  key of a group finds the group. `SearchHit` has no business id: the harness fills
  `RetrievedChunk.business_id` from the hit's document row, never from the question, so every
  hit's document is checked against the question's business (asserted: a leak check). A key that
  no stored chunk holds is never dropped: a group none of whose keys is held makes its question
  "unscorable at this config"; a single unheld key in a group another key finds is counted in the
  report; a conservative line counts every unscorable question as a miss; chunk configs are
  compared on dev only, over the questions scorable under both (decided 2026-10-22, below). A
  different chunk size needs a fresh ingest (re-ingesting an existing version does not re-chunk).
- Reports take "synthetic" from the corpus and dataset manifests, never from chunk text; safety
  results are counts (`k of n`), never a rate gate.

**Built (2026-10-22, noah; branch `m2/eval-harness`; Postgres parts not yet run: Docker is
unreachable, they run in CI, and the end-to-end run waits for H1 and H2).**
[ADR 0013](../adr/0013-eval-harness.md) (accepted 2026-10-22) records the choices; runbook for PR 9:
[`docs/learning/m2-eval-run.md`](../learning/m2-eval-run.md).
- `apps/api/src/vectapilot_api/evalrun/` (not in `evalset`, which stays free of models, network
  and database): `uv run python -m vectapilot_api.evalrun run|ingest|summarize|label-sheet`
  (no new console script); `make eval-dry-run`. `run` = register the eval businesses (owner URL),
  ingest through `KbService`, check the stored chunks against the corpus at the chunk config, then
  each question of the split through `AnswerService`, scored with H2, into one run folder.
- Business from the document row: `RecordingSearch` wraps H1 through `SearchService`'s `search`
  seam and reads `documents.business_id` on the same connection, in the same business
  transaction (no business filter in that `WHERE`, on purpose; RLS hides another business's rows,
  which counts as a leak too). Any hit not of the question's business aborts the run (exit 4).
- Eval businesses `<business>-ev-<hash of model, max_chars, overlap>` (a fresh namespace per chunk
  config or embedding model; `--fresh` deletes only those ids' documents). Unscorable = a group no
  key of which any stored customer-visible chunk holds (`relevance.hit_matches_key`).
- Cost cap over embeddings and `large`: `CappedEmbedder` (per attempt, `token_upper_bound` ×
  price; prices added to `embeddings/models.toml` from ADR 0008's Facts) and `CappedGateway`
  (M1's formula via the new `measure.budget.request_reservation`, M1's stop rules).
- Test split: live only, frozen dataset, a committed complete live dev run with the same config
  fingerprint, and `evals/reports/<dataset>/test-runs.jsonl` appended before the first paid
  call; a second test run of one fingerprint needs `--test-rerun-reason`.
- Run folder: `run.json`, `results.jsonl` (no text), `answers.jsonl`, `labels.jsonl` (template),
  three record logs, `summary.md`/`.json` recomputed from the files through H2. Wilson 95 % for
  every `x of n`; percentile bootstrap (2,000 resamples, seed 20261022) for MRR from n = 10; a
  percentage from n = 10; safety as counts only; status line DRY RUN / PARTIAL / dev tuning /
  held-out test, and every reason a run cannot feed the note.
- H2 switch: `"retrieval_metrics"` added to `PENDING_HAND_WRITE` (with `pending_hand_writes` for
  tests that need both stubs); the canary consistency test accepts it through
  `NEEDED_OUTSIDE_READ_PATHS`. The H1 and H2 guides name the extra step.
- Tests `tests/evalrun/`: 183 tests / 282 cases after the decisions and the mutation-audit tests
  (164 / 252 before); locally 271 passed, 4 xfailed (H2), 7 skipped (Postgres). Stand-ins
  return prepared hits, document rows and metric values (both means as hand-written literals);
  nothing implements search or the metrics. Wilson checked against Newcombe (1998), Table I.
- **Review fixes (2026-10-22, noah; kenji, ananya, diego, zara, maya).** Every hit is checked in
  the search itself, before the answer step (a later cap stop, Ctrl-C or error can no longer hide
  a leak), with its document's status and audience (`AUDIENCE LEAK`, exit 4); questions validated
  as `POST /v1/answer` does; the test ledger must match HEAD before a test run, and run.json and
  the status line record reruns and earlier entries; the status line says a test run may feed the
  note only with no reason against it; MRR shown only from n = 10, a zero-width bootstrap called
  degenerate, H2's MRR cross-checked against its reciprocal ranks; unscorable counts per row,
  unheld keys and uncounted evidence by id; "synthetic" built from the manifest flags; a failure
  after the calls never says "nothing was sent"; Ctrl-C before the questions ends `partial`; dry
  runs refuse paid providers; `--fresh` deletes only derived ids; ingest stops at the cap's stop
  rule; H2's own tests now use the one `PENDING_HAND_WRITE` switch. Postgres parts (incl. a new
  leak test through `execute`, and `summarize()` at the end of the end-to-end run) still run in
  CI only.
- **[decided 2026-10-22] (shesha)**, every recommendation, one PR: (1) one derived eval business
  per (business, embedding model, chunk config); (2) answers.jsonl committed, questions by id;
  (3) the test split: live only, frozen, after a complete live dev run of the same config
  fingerprint, its ledger entry before the first paid call, a rerun needs a reason; (4) dry runs
  on unfrozen data allowed (`unassigned`), a live run needs the freeze; (5) Wilson 95 %
  everywhere, a seeded percentile bootstrap over questions (2,000 resamples) shown from n ≥ 10,
  with "approximate at small n (percentile bootstrap undercovers)" in summary.md; (6) M1's
  formula and $1.00 ceiling; (7) only a complete, labelled, live test run feeds the "what I
  measured" note, dev runs next to it labelled as tuning; (8) unscorable per group, plus a
  conservative line counting unscorable questions as misses, and chunk configs compared on dev
  only over the questions scorable under both (the aligned wording in `evalset/corpus.py`, the
  v0.1 README and this plan approved); (9) a §6 target is "met" when the point estimate reaches
  it, always shown with its Wilson interval, and "not shown at 95 % confidence" when the lower
  end is below it; `RATE_MIN_N` applies to every split; (10) a stop rule tripped by the last
  question aligns with M1 (status `complete`, a not-for-the-note reason, exit 0); a leak always
  exits 4, also after a failed summary; `--live` is refused unless the dataset and every corpus
  manifest say `synthetic: true` (a guard for a future real pilot); (11) the H2 stand-ins and the
  report's cross-checks against H2's per-question values are on the team side of the
  [HAND-WRITE] line; (12) ADR 0013 accepted with these options.
- **Built from the decisions (2026-10-22, noah).** `harness.final_status` (a last-question stop
  is `complete`, its stop kept); `status_of` adds "stopped by its last question"; summary rows
  carry `conservative` (hits of n + unscorable, shown for "all" and each business); a "Targets
  (business-context §6)" table for hit@5 and correctness, "all" and each business (no verdict
  below n = 10, for a dry, partial or failed run, at another metric k, or while answers are
  unlabelled); the bootstrap note in the Intervals row and under the retrieval table; the
  `--live` synthetic guard in `cli.run_command`. The ingest-only path's unreachable "partial"
  branch is gone (a cap stop during ingest already returns `partial` before it).
- **Mutation-audit tests (2026-10-22, noah).** M7b/c/d: the H2 stand-in's resample statistic now
  depends on the whole resample (position-weighted prepared values, not a mean), and a test with
  a non-default seed (7) and count (300) checks the "all" and "language en" intervals against a
  bootstrap written out in the test; M13a/b: `execute` ends `partial` when the cap stops the
  question loop or the ingest (a refusal or a stop rule, `ingest_only` too); M11b: `wired`
  prices embeddings at the catalog's voyage-4 price (a spy, paid Settings with TEST-ONLY keys,
  stand-in embedder and model, nothing built that could send); M10b: stdout not a terminal
  refuses `--live`, nothing sent, no folder; M14b: every package logger at DEBUG, no record holds
  question text; M15a/b/c: a corpus manifest with `synthetic: false` gives run.json
  `corpus_synthetic: false`, "NOT synthetic" in summary.md and the plan, and a refused `--live`;
  M17b: a size-only `ChunkingConfig` change is another namespace. Each was run against its mutant
  in a scratch copy (28 mutants incl. the decisions' own: all killed) and the tree restored byte
  for byte (sha256 of 175 files before and after).

## PR 9 — close-out (shesha's live run)
- Ordered checklist of every remaining step, with commands, "done when" signals, helpers and
  dependencies: [`m2-close-out.md`](m2-close-out.md). Note skeleton:
  [`docs/notes/m2-what-i-measured.md`](../notes/m2-what-i-measured.md).
- Runbook: [`docs/learning/m2-eval-run.md`](../learning/m2-eval-run.md) (prerequisites, key
  entry, the exact commands, the cost formula, labelling, what feeds the note).
- Live ingestion and the eval v0 run; human correctness labels; a "what I measured" note with
  measured hit@5, MRR and correctness against the §6 targets, per language. No target is claimed
  unless met. The study PDF for M2.
