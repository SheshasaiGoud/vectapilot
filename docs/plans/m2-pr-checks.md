# M2 — acceptance checks per PR

> **Status (2026-10-21):** planned; PR 0 in review.

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

## PR 3 — `m2/ingestion` (ananya)
- Loaders for Markdown, HTML and CSV (standard library) and PDF (`pypdf`, page by page; needs
  shesha's rule-11 yes on install day). Every chunk carries document, version, page (if any) and
  type for filters and citations. Nothing is hard-coded to one business.
- Configurable, heading-aware chunking; the size comes from the business config and is tuned on
  the dev split only. Golden files per format, including PDF page numbers.

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

## PR 5 — `m2/kb-endpoints` (diego)
- `POST /v1/kb/documents` and `POST /v1/kb/reindex`, synchronous (background workers are M9),
  scoped by `X-Business-Id`. Re-ingesting the same document version is idempotent (test).

## H1 — `m2/retrieval` (`[HAND-WRITE]`, shesha)
- ananya provides the interface, tests and a short guide; shesha writes top-k exact vector search
  with the `business_id` filter inside the SQL (vector only; fusion is M3). The canary suite
  stays green.

## PR 6 — `m2/search-and-answer` (ananya)
- `GET /v1/kb/search?q=` returns only the business's approved public chunks.
- A cited answer through the gateway's `complete_structured`: every cited chunk id was retrieved
  for that business; weak or no evidence ⇒ an honest "I don't know" or a hand-off, no invented
  facts. Each answer carries `sources[]`, `confidence` (v0 = top retrieval score, labelled
  uncalibrated), `route` = `rag` (until M4) and `trace_id`.
- Retrieved text is data, never instructions: it is delimited in the prompt, and adversarial
  documents are part of the tests.

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
