# M3 — acceptance checks per PR

> **Status: planned and decided 2026-10-23.** Drafted by maya on `m3/plan` from research by
> ananya-rag, noah-evals, diego-backend and zara-safety, then revised the same day after two
> independent reviews (completeness, feasibility). The revision made the smallest plan the default,
> renumbered the PRs (now PR 0–9) and moved the judge and reranking to stretch items, S1 and S2.
> shesha decided D1–D16 the same day ("Decided 2026-10-23"), and the last open question on
> 2026-10-24: shesha writes H1 too ("Decided 2026-10-24"). No question is open (D16 is looked at
> again after M2's dev run, as decided). PR 0 (this file,
> [ADR 0014](../adr/0014-hybrid-retrieval.md) and the amendments) is merged (#38), PR 1 (guards)
> (#39), PR 2 (full-text column) (#40) and its audience follow-up (#41) too, and PR 3 (hybrid
> scaffold) (#42); PR 4 (hybrid search) is in review.
> M2 is **not closed**: `support_qa` v0.1 has no questions yet (`manifest.json` says
> `stage: collecting`, `questions.jsonl` has 0 lines), and there is no live run and no baseline
> ([`m2-close-out.md`](m2-close-out.md)). M3 can be built now. Anything that compares against M2's
> baseline waits for it ("Order of work").

This file holds what each M3 PR must prove; each PR copies its section into its own acceptance
check. "PR 0–9" are plan step numbers, not GitHub pull-request numbers. H1 and H2 are M3's
`[HAND-WRITE]` pieces (rule 2), not M1's or M2's. S1 and S2 are stretch items, built only if a
decision says so (none does: D4 (b), D13 (c)). Tags: **[Dn]** = settled by decision Dn
("Decided 2026-10-23") · **[proposal]** = not covered by a decision, confirmed or changed in its
PR's review · **[carried]** = from an earlier plan or ADR, source named. Sizes are estimates, never
measurements; no number in this file is a result (rule 3).

## What M3 delivers
The README's roadmap names M3 "RAG v2 — hybrid search, reranking, abstention". On top of that come
the items M2's plan and ADRs 0006–0013 deferred to M3.
1. **Hybrid retrieval.** Postgres full-text search next to M2's vector search, fused with Reciprocal
   Rank Fusion (RRF). The `business_id` filter sits inside every statement, with RLS underneath
   (ADR 0004, 0009). It stays behind a setting that remains `vector` until a paired dev comparison
   against M2's vector baseline meets D15's adoption rule.
2. **Abstention that keeps its meaning.** The evidence gate stays a cosine threshold (ADR 0011
   decision 1) applied to the fused hits, re-tuned on dev only. Every config reports the expected ×
   actual outcome table (already in `summary.md`) and the safety rows by outcome, per business.
3. **Reranking decided with a measured number** [D13 (c)]: not built in M3. PR 5 reports the
   headroom count (relevant chunk in the fused top 10 but not the top 5), which is both the reason
   for the deviation and the trigger for revisiting it.
4. **A first faithfulness number** (business-context §6: ≥ 95 %, "M2–M3"): human faithfulness labels
   on shown answers, reported as `x of n` with a Wilson interval. LLM judges, for faithfulness and
   correctness, are deferred [D4 (b)].
5. **A paired comparison command** for two runs with the same chunk config. Comparing two chunk
   configs stays manual, as ADR 0013 records [D14].
6. **Isolation stays a hard gate.** The canary suite and the harness's leak check cover the
   full-text leg and the fused list. The canary gate stops accepting an xfail that is not a pending
   hand-write, and owner-connection clause checks are in it before H1 switches its tests on.
7. **Revisits recorded with evidence:** the ANN index (ADR 0008, measured at pilot size), the trace
   span (ADR 0007, amended [D12]), native Citations (ADR 0006, 0011 [D11]), the snapshot rule
   (ADR 0011 decision 5 [D8]), prompt caching, native structured output and query rewriting [D14].

**How we will know M3 worked** (PR 9):
- The canary suite is green in CI with the new paths, and no scored run aborted on a leak (hard
  gate).
- Committed live dev folders exist for every variant D15 lists, including the ones that lost.
- M3's live spend, summed from the committed folders (PR 5), is within the $5.00 ceiling
  [D15 (iii)].
- `compare` output for M2 vector vs M3 hybrid on dev, as discordant counts by question id.
- If hybrid is adopted: one complete, labelled live test run of it in the ledger. If vector stays:
  M2's test folder is the result, and M3 makes no test run.
- Human faithfulness `x of n` with its Wilson interval for every labelled live folder, and "no
  verdict" where ADR 0013 decision 10 says so.
- The outcome and safety tables per config, and per-stage latency labelled local.
- The pilot-size EXPLAIN output and the ADR 0008 amendment.
- shesha's note `docs/notes/m3-what-i-measured.md` and the H1/H2 explain-backs.

"No measurable difference on v0.1" is an acceptable, honest result.

## Out of scope for M3
- Moved out by D14 (one decision): query rewriting, Anthropic prompt caching, native structured
  output, the synthetic-scale latency measurement, the catalog check for tables without
  `business_id`, and a command that compares two chunk configs.
- LLM judges for faithfulness and correctness [D4 (b)]: stretch S1 is not built.
- Reranking of any kind [D13 (c)]: stretch S2 is not built. Never in M3: a local
  cross-encoder or other ML runtime (8 GB Mac, 4 GB Docker VM, data volume 93 % used), or an LLM
  reranker (a paid call per question and a new injection surface).
- BM25 extensions (ParadeDB `pg_search`, VectorChord-bm25): they are new extensions, and
  corpus-wide statistics shared across businesses would let one business influence another's
  ranking (§5). Postgres' `ts_rank` scores each chunk on its own.
- `pg_trgm` fuzzy matching (available, not installed), until dev misses show a need.
- A GIN index on `tsv` and an HNSW index, unless PR 8's EXPLAIN shows they would be used and are
  needed.
- Per-claim citations and native Citations [D11]. Also any change to the answer prompt, the model
  catalog or the gateway's output path inside the measurement window [D3].
- A runtime faithfulness or groundedness check (M7, zara, a `[HAND-WRITE]` guardrail), a judge run
  in CI (paid; CI uses the fake provider), `evals/thresholds.yaml` and the CI eval gate (M10).
- The trace span, any exporter or third-party trace store, and the Langfuse SDK (it brings classic
  `httpx` back) [D12]. The W3C `traceresponse` header (ADR 0011 decision 9, option (c)) follows
  the span. Accepting an inbound `traceparent` is also out: ADR 0011 decision 9 keeps the trace id
  server-made, and the question is revisited with M8's widget [proposal].
- Redis caches and background workers (M9).
- Chunk-size tuning on v0.1, a Telugu text-search config (Postgres has none; `simple` is the
  fallback), a Telugu eval set, and real customer data.
- A per-business configuration loader (ADR 0011 decision 6), and with it M1's "check every alias
  when a business's config loads" [carried, `m1-pr-checks.md` "Handed on to M3"].

## Playbook deviations (decided 2026-10-23 with D4, D11, D12, D13, D14, D16)
- **Reranking** (named in the README's M3 row) is not built in M3. The reason is a measured
  headroom count, and that count is the revisit trigger [D13 (c)].
- **LLM judges are not built in M3.** The faithfulness judge was moved here by M2's decision 4
  (`m2-pr-checks.md:21`), and the correctness judge comes from §6's method ("human labels + a judge
  calibrated against them", `business-context.md:252`). Both wait for a labelled pool of at least
  30 distinct shown answers; until then, M3 reports human labels only [D4 (b)].
- **The trace span moves to M9** through an ADR 0007 amendment, whatever the answer to Q4b
  [D12 (a)].
- **Query rewriting** (in ananya's M2–M3 scope) and **prompt caching** (deferred to M3 by ADR 0006)
  move out of M3 [D14].
- Faithfulness is labelled per answer, not per claim [D11].
- Eval data stays synthetic and human-written (v0.1). D16 looks at v0.2 again after M2's dev run.

## Facts this plan relies on (2026-10-23)
Checked by maya in the repository:
- **Fingerprint.** `evalrun/report.py:221-246`: `config_fingerprint` hashes `catalog_version` and
  `prompt_version` next to the whole closed `ConfigInfo` (lines 128-145). The output mode is not
  hashed. `evalrun/cli.py:537-546` passes the loaded catalog's version.
- **Test-run rule.** `evalrun/cli.py:330-352` (`committed_dev_run`): a test run needs a complete
  live dev run with the same fingerprint in `HEAD` (ADR 0013 decision 5).
- **Catalog.** `gateway/models.toml:8` says to change `catalog_version` with any change to the file
  (today `2026-10-14.1`; aliases `small` → claude-haiku-5-5 and `large` → claude-sonnet-5-5).
  `m2-close-out.md` step 11 lands a changed price "through a PR, new `catalog_version`, before the
  run".
- **Evidence gate.** `answer/service.py:161`: evidence = hits with `hit.score >= min_score`, and the
  score is a cosine (`retrieval/types.py:95` pins `SearchHit.score` to [−1, 1]; `MAX_K` = 50 at
  line 24).
- **Search transaction.** `retrieval/search.py`: the model check and M2's `vector_search` already
  run as two statements in one READ COMMITTED transaction; hybrid adds a third.
- **Answer types.** `answer/types.py:100, 111`: `AnswerRun.hits` is `tuple[SearchHit, ...]`, and
  `RagAnswerRecord` is closed with `record_version: Literal[1]`. ADR 0007 calls record versions
  "additive; a breaking change bumps it". ADR 0011 says changing `rag_answer` "later needs a new
  `record_version`". The harness copies these records into `rag_answer_log.jsonl`
  (`evalrun/report.py:89`).
- **Recording seam.** `evalrun/harness.py:198`: `RecordingSearch` wraps the vector seam only.
- **Paid opt-in.** `config.py:242`: the API's paid opt-in reads only `llm_provider`. `main.py:81`
  builds the configured embedder, Voyage included, without it, so `GET /v1/kb/search` is already an
  ungated paid path when `embeddings_provider=voyage`.
- **Canary gate and registry.**
  - `scripts/canary_gate.py:43-44`: any `pytest.xfail` under `tests.canary` passes the gate.
  - `tests/canary/test_leakage.py:414-420` holds a strict xfail that is not a hand-write (the
    pool-return `RESET` gap; shesha's decision of 2026-10-21, `m2-pr-checks.md:104-105`). CI runs
    it, because `ci.yml` sets `RUN_INTEGRATION=1`.
  - `tests/canary/read_paths.py:154` maps each read path to a single stub, and `pending_hand_writes(*stubs)`
    already exists at line 184. `tests/test_canary_checks.py:298-322` checks the registry.
  - `tests/canary/catalog.py` (`_HAS_BUSINESS_ID`) checks only relations that have a `business_id`
    column.
- **Telemetry.**
  - `uv.lock`: fastapi 0.143.0 requires `opentelemetry-api` 1.45.1. `opentelemetry-sdk` is not
    locked.
  - In the installed `fastapi/telemetry/_api.py`, tracing, metrics and logs each default to on
    (lines 55-64). Logs record "exception messages and stack traces", and a validation failure
    stores the request body (lines 151-160). `_asgi.py:299, 306` extracts an inbound trace context
    and records `url.query` once a provider exists. `main.py:116` builds `FastAPI(...)` without
    `telemetry=`.
  - `opentelemetry-api` itself provides `TracerProvider`, `MeterProvider` and
    `_logs.LoggerProvider`.
- **Dataset and corpus.** v0.1's manifest requires at least 40 questions, a 30 % test share, at
  least 8 no-answer rows and 3 safety rows per business. `data/sample` has 29 and 30 `## ` headings
  in its Markdown, plus HTML and CSV content. ananya counted 129 chunks in total at 1200/150 (not
  rechecked by maya).
- **Local data and spend.** `Makefile:32-34`: `reset-db` is `compose down --volumes`. The
  `evalrun/namespace.py` docstring says each eval business is "ingested once". `measure/budget.py:61`
  caps each run at $1.00; nothing caps a milestone's total (D15 (iii) adds M3's, PR 5).
  `evalrun/report.py:147-155`: each folder's `run.json` holds `cost.total_usd` and
  `llm_cost_complete`, written after the calls, so a killed run leaves no total.
- Migrations end at 0004, `evals/reports/` does not exist yet, and the data volume is 93 % used
  (14 GiB free).

Checked by ananya and diego on the local stack (read-only queries and rolled-back temporary tables),
not rechecked by maya:
- PostgreSQL 18.6 and pgvector 0.8.7.
- The `english`, `hindi` and `simple` configs exist; `telugu` does not. `hindi` stems but has no
  stop list.
- `to_tsvector(regconfig, text)` is IMMUTABLE (allowed in a generated column); the one-argument
  form is not.
- PG 18 makes a generated column VIRTUAL unless it says STORED, and virtual generated columns
  cannot be indexed. `ADD COLUMN ... STORED` rewrites the table.
- `websearch_to_tsquery` ANDs every term.
- `@@` (`ts_match_vq`) and `<=>` are not leakproof, so under RLS the app role may never use an index
  on them (to confirm with EXPLAIN in PR 8).

Not verified anywhere: every Voyage rerank fact (only relevant to S2), and Anthropic's minimum
cacheable prompt length (only relevant to caching, D14).

## Order of work and sequencing with M2's close-out
```
Decided:        D1–D16 (2026-10-23), H1's writer (2026-10-24) · Later: D16 (after M2's dev run)
Now:            PR 0 · PR 1 guards · PR 2 full-text column · PR 6 compare · PR 7 faithfulness labels
Then:           PR 3 scaffold + canary hardening (after PR 1 and PR 2) → H1, H2 (shesha writes both)
                PR 4 hybrid search (after PR 3) → PR 5 eval hybrid · PR 8 latency (after H1)
Measure:        PR 9 close-out, each step with its own dependency (below)
Stretch:        none in M3: S1 judges (D4 (b)) and S2 rerank (D13 (c)) are not built
```
About 12 merges, 2 of them hand-writes, plus the close-out's folder PRs. shesha reviews each one.

1. **Measurement freeze window** [D3]. From M2's first live dev run (close-out step 13) until M3's
   last live answer run (PR 9), nothing merges that changes `gateway/models.toml`, `PROMPT_VERSION`
   or the gateway's output path.
   - Why: a `catalog_version` or prompt bump changes every run's fingerprint. Between M2's dev and
     test runs, that refuses the test run and forces a paid dev rerun. The output path is not hashed
     at all, so a change there would break comparability without any warning.
   - If a price changes inside the window: shesha chooses at that time between keeping the old
     catalog (with the note marking those costs stale) and bumping it and re-running M2's vector
     dev run (paid).
   - Tripwire: PR 5's fingerprint pin turns CI red on any catalog or prompt bump.
2. **M2's test run on M2's dev code** [D3]. shesha runs M2's live test run on the branch
   that adds only the dev folder on top of the dev run's commit (`m2/close-out`, as suggested in
   M2). M3 merges on `main` then cannot affect it.
3. **Keep the local eval namespaces** [D3]. No `make reset-db`, Docker volume prune or other
   deletion of database data between M2's first live ingest and M3's close-out. M3's hybrid runs
   reuse M2's chunks and embeddings. A re-ingest is a paid re-embed, and the vectors might differ
   (unverified). If disk space runs short, free it outside the database volume. PR 6's `compare`
   warns when it sees a re-ingest.
4. **The canary gate after the `RESET`.** PR 3's stricter gate merges after PR 1's `RESET`
   [D10 (a)]. Otherwise CI turns red on `test_leakage.py:414-420`.
5. **Owner-connection clause checks before H1.** They land in PR 3, so a full-text leg without its
   business clause can never pass the canary gate on RLS alone.
6. **Land PR 2 before M2's live ingest if possible.** M2's eval namespaces then carry `tsv` from the
   start. If the ingest comes first, migration 0005's backfill covers it without re-embedding.
7. **Land PR 7 before M2's step 16 if possible.** shesha can then label correctness and
   faithfulness in one reading of each answer; otherwise it takes a second pass over M2's folders.
8. **D1 (Q6) and D3 reach into M2's close-out.** D1 sets the expected outcome of Hindi questions
   that have only English evidence (`docs/learning/m2-eval-questions.md`, "Hindi and Hinglish"),
   so PR 0 adds it to `m2-close-out.md` step 7, and D3's rules to steps 11 and 13.
9. **M3's spend ceiling** [D15 (iii)]. All of M3's paid runs together stay within $5.00, each still
   capped at $1.00. Commit each live folder before the next live run: PR 5 counts committed
   folders only.
10. **Outstanding and not M3 acceptance:**
    - M1: the live run, the note, the H2/H3 explain-backs and the quiz.
    - M2: the H1/H2 explain-backs.
    - Image pinning stays where M2 put it.

## PR 0 — `m3/plan` (maya)
- This file, with shesha's decisions of 2026-10-23 ("Decided 2026-10-23").
- [ADR 0014](../adr/0014-hybrid-retrieval.md) (hybrid retrieval), accepted, and dated amendment
  sections (nothing earlier rewritten):
  - ADR 0011: decision 4 [D1]; decision 5 [D8]; the threshold under hybrid [D7]; native Citations
    declined for M3 [D11]; `traceresponse` follows the span [D12]; the `rag_answer` record rule
    (a proposal, confirmed in PR 4).
  - ADR 0007: the span moves to M9 [D12]; stage durations; FastAPI's telemetry off (PR 1); the
    route-timeout note.
  - ADR 0006: "Exposure" [D9]; prompt caching, Citations and native structured output [D11, D14];
    the catalog freeze [D3].
  - ADR 0008: the ANN revisit, pending PR 8's measurement [D14].
  - ADR 0009: the pool-return `RESET` [D10]; the stricter gate and owner-connection checks (PR 3).
  - ADR 0013: the measurement window [D3]; the retrieval block; every list checked; `compare`;
    faithfulness labels; no judge [D4]; the spend ceiling [D15 (iii)]. The chunk-config
    comparison stays manual [D14].
  - No ADR 0015 [D4 (b)].
- `m2-close-out.md`: step 7 gains the Q6 line [D1]; steps 11 and 13 gain D3's freeze window and
  volume rule; the stale Docker lines are corrected (step 2 done). `m2-pr-checks.md`: status line.
- CLAUDE.md "Decisions" and "Current status" updated, and the README roadmap's stale status column
  (M1 still "Next") corrected. Links resolve.

## PR 1 — `m3/guards` (diego; zara reviews) · S — can start now
Three small, independent changes. None needs M2's baseline.
- **FastAPI's built-in telemetry off.** `create_app` passes `telemetry=` with tracing, metrics,
  logs, operation spans and auto-configure all off.
  - The test sets up hand-written recording providers: subclasses of `opentelemetry-api`'s
    `TracerProvider`, `MeterProvider` and `_logs.LoggerProvider`. It never uses `opentelemetry-sdk`
    or `InMemorySpanExporter`; any SDK import goes to shesha under rule 11 first. diego picks how
    FastAPI sees them (global providers restored after the test, or a `create_app` parameter) and
    says so in the PR.
  - Requests: `GET /v1/kb/search?q=` with a canary string, a 422 on `POST /v1/answer`, a forced
    500, and a request carrying an inbound `traceparent`.
  - One assertion per signal: no span, no metric and no log record, and the `traceparent` is never
    read (ADR 0011 decision 9).
  - If the test imports `opentelemetry-api` directly, the PR declares it as a direct dev dependency
    (already locked through fastapi; ADR 0008's `httpx2` precedent) and asks shesha for a yes
    (rule 11).
- **The API's paid opt-in covers every paid provider it builds** [D9]. It refuses
  `embeddings_provider = voyage`, and any later paid reranker, unless the opt-in is on. One test per
  provider; the error names the setting, never a value; the harness is unaffected (ADR 0013
  decision 4).
- **The pool-return `RESET`** [D10 (a)]. A pool hook resets `app.business_id` whenever a connection
  returns to the pool; diego picks the hook and documents why.
  `test_a_session_level_business_does_not_survive_the_pool` then XPASSes and its xfail is removed.
  Red proof: without the hook, the test fails. The canary suite stays green.
- **Built (2026-10-23, diego).** In review; nothing merged.
  - Telemetry: `main.TELEMETRY_OFF` is passed to `FastAPI(telemetry=...)`.
    `tests/test_fastapi_telemetry.py` makes the recorders the process-wide providers (FastAPI's
    default source, restored by `monkeypatch`), not a `create_app` parameter. It has one test per
    signal and a red proof with FastAPI's defaults, where every recorder fills, the canary
    included. It imports `opentelemetry-api` directly: declaring it is shesha's call under rule 11.
  - Opt-in: `config.API_PAID_PROVIDERS` holds `llm_provider` and `embeddings_provider`, with one
    refusal test per provider. A drift test fails while any `*_provider` setting is missing. The
    eval harness and the measurement CLI never call the rule (only `create_app` does), and a test
    covers each loader.
  - `RESET`: SQLAlchemy's pool `reset` event (ADR 0009 amendment, "Done"). The strict xfail is
    removed. The red proofs are tests, and one more run with the hook never installed failed as
    expected.
- **[decided 2026-10-23]** (shesha) `opentelemetry-api` is declared as a direct dev dependency
  (rule 11; already locked at 1.45.1 through fastapi, so `uv.lock` only gains the project's own
  requirement). The startup role check also refuses a connection that starts with a default
  `app.business_id` (server settings or a role default; ADR 0009 amendment). A statement timeout
  for every DB statement, the RESET included, comes later (M9/M10). The opt-in error type is the
  generic `paid_provider_not_opted_in`.

## PR 2 — `m3/fulltext-column` (diego; ananya pairs on ingestion, zara reviews) · S
- **[D2] Migration 0005:** adds `ts_config regconfig` and `tsv tsvector GENERATED ALWAYS AS
  (to_tsvector(ts_config, content)) STORED` to `chunks` (the two-argument form, with `STORED` written
  out). No GIN index [D6]. Not CONCURRENTLY (Alembic runs in a transaction, as 0004 notes). The
  downgrade drops both columns.
- **Backfill [decided, D2; diego verifies the SQL in the PR]:** existing rows get `ts_config` from
  `documents.language`, through the map as it stands at 0005 (copied into the migration), then
  `SET NOT NULL`.
  - NOT NULL validation reads every row, whatever RLS allows. With a non-superuser owner under
    FORCE RLS (M10), a row hidden from the `UPDATE` therefore stops the migration instead of
    staying NULL. Locally and in CI the owner is a superuser (ADR 0009 "Limit").
  - A test proves the backfill on planted rows of two businesses.
- **The language map:** one platform data file, BCP 47 tag → regconfig (decided, D2: `en` → `english`,
  `hi` → `hindi`, anything else → `simple`). It names no business (rule 9), and a Postgres test
  checks that every mapped config exists. A later change to the map needs its own backfill
  migration (ADR 0014).
- `kb/store.py` sets `ts_config` at insert from the document's language, bound as
  `CAST(:ts_config AS regconfig)`. Nothing selects the column raw (asyncpg's `regconfig` codec is
  unverified).
- **Tests:**
  - upgrade and downgrade;
  - an insert that sets `tsv` is refused;
  - an `en` chunk containing "fees" matches the lexeme `fee`, and an `hi` chunk gets `hindi`;
  - the canary catalog checks (RLS, FORCE, keys) stay green unchanged.
- **Canary bait:** `test_leakage.plant` sets `ts_config`, and both businesses' bait chunks share one
  rare token, so a leaking full-text leg would have something to leak (zara S2).
- **Built (2026-10-23, diego).** In review; nothing merged.
  - Migration `0005_chunk_full_text_columns.py`: `ts_config` added nullable, backfilled (the map
    written out; the join names `business_id` and `id`), `SET NOT NULL`, then `tsv ... STORED`.
    No default and no index. Its errors name a column, a config or a size, so no `DO` block.
  - The map is `ingestion/language_map.py`, a Python module rather than TOML (two entries, typed,
    no loader; a change needs a migration anyway). The line above says "data file": keeping the
    module is a question for shesha. Only the primary language subtag counts,
    lower-cased. `en-IN` → `english`, `hi-Latn` → `hindi`, and `te`, `eng`, `und` → `simple`.
    `ts_debug` on the local stack shows that `hindi` stems Latin-letter words with
    `english_stem`, so romanized Hinglish gets `english`'s lexemes. A test fails when the map
    changes without a migration that writes out the new copy.
  - `kb/store.py` binds `CAST(:ts_config AS regconfig)`. Every direct chunk insert in the tests
    names it too (`test_rls`, H1's vector tests, PR 6's Postgres tests, the canary `plant`).
  - Canary bait: `BAIT` holds `BAIT_WORD` (`zq7bait3`). Postgres reads it as a `numword`, which
    `english`, `hindi` and `simple` all keep unchanged. No file under `data/sample` or
    `evals/datasets` contains it. An owner query without a business clause returns both baits;
    the app role gets its own only.
  - The M10 guard is proven both ways. With a non-superuser owner under FORCE RLS, `SET NOT NULL`
    stops with 23502, and the message names no business. With BYPASSRLS, both businesses are
    backfilled.
  - Observed: asyncpg binds `CAST($1 AS regconfig)` as `regconfig` with a text codec, and a raw
    select returns a `str`. "Nothing selects it raw" stays the rule. `pg_upgrade` 18 refuses a
    `regconfig` column in a user table, and the column cannot be retyped in one step: a question
    for shesha before M10, with the options in ADR 0014, "Built".
  - `make migrate` reuses the image built before 0005, so the head stayed at 0004. 0005 was
    applied with `uv run alembic upgrade head` as the owner.
- **[decided 2026-10-23]** (shesha) Keep `regconfig` (D2); at M10's first major upgrade, dump and
  restore, or run 0005's downgrade and upgrade around `pg_upgrade` (which refuses `reg*` columns).
  A same-bytes re-upload with a different `language` is a 409 `language_conflict` (the stored tag
  is compared exactly). `hi-Latn` (Hinglish) maps to `hindi`. The map is a Python module
  (`ingestion/language_map.py`; ADR 0014 decision 2 amended). After adding a migration, `make up`
  runs before `make migrate` (CLAUDE.md).

### PR 2 follow-up — `m3/audience-conflict` (diego) · XS
- **[decided 2026-10-23]** (shesha) A same-bytes re-upload of a stored version with another
  `audience` (`public` against `staff_only`, either way) is a 409 `audience_conflict` with a fixed
  message that echoes no input, like `status_conflict` and `language_conflict`. A document meant
  to become staff-only must never silently keep answering customers. To make an approved public
  version staff-only, upload the content as a new version with status approved: that retires the
  public one in the same transaction. A new draft does not, and the approved public version keeps
  answering customers until an approved version replaces it (ADR 0010, amendment 2026-10-23).
- **Built (2026-10-23, diego).** In review; nothing merged. No migration, no new grant, no new
  dependency.
  - `kb/types.py`: `AudienceConflict`; `StoredDocument.audience` is required, and both of the
    store's reads return it (`find_document`, and the row `save_document` finds when the version
    was stored meanwhile).
  - The order of the checks (`kb/service.py` docstring): bytes, then status, then language, then
    audience. A re-upload that differs in several gets the first.
  - `evalrun/ingest.py` turns it into `IngestRefused`, naming `AudienceConflict` and `--fresh`,
    like the language case.
  - Tests, both directions: through the route with the in-memory store (409, nothing stored,
    changed or embedded; the same audience is still a 200 no-op; the order); against Postgres
    (the fixed 409 body and no `embedding_call` record for the refused request; the stored row
    and chunks unchanged; both store reads return the stored audience); the eval ingest refusal
    (nothing stored, changed or embedded). With the check removed, 8 of `tests/kb` and
    `tests/evalrun` (Postgres included) fail, all audience tests; with it, all 411 pass.
  - Review (zara, 2026-10-23): the code and the 409 text stand; the hint after a 409 now says a
    draft retires nothing (ADR 0010 amendment, `AudienceConflict` docstring, the decided line
    above). The Postgres and eval-ingest audience tests now also fail when the refusal comes
    only after loading and embedding (a paid embedding with Voyage), and the Postgres one when
    the detail echoes input. Retiring without a replacement is open (business-context §9, Q12).

## PR 3 — `m3/hybrid-scaffold` (ananya; zara pairs on the canary part) · M
The same shape as M2's H1 scaffold, plus the canary hardening that must precede H1. The specs
follow D6 and ADR 0014; the time-boxes are in H1 and H2.
- **`retrieval/types.py`:**
  - `TextQuery`: the business; the question, checked like `Question`; the question's embedding and
    model id; `k ≤ MAX_K`; the reader.
  - `FusedHit`: wraps an unchanged `SearchHit` and adds the RRF score as a `Fraction` and the rank
    in each list (`None` where absent).
  - The types keep an RRF score away from `rag_min_score`.
- **`retrieval/text.py`:** `text_search(connection, TextQuery) -> list[SearchHit]`, with body
  `raise NotImplementedError` and the spec as its docstring:
  - one bound statement, with `c.business_id = :business_id` in the `WHERE`;
  - approved chunks only, within the reader's audiences, of the question's embedding model;
  - `c.tsv` matched against an OR of the question's lexemes, parsed with each chunk's own
    `ts_config`, never `to_tsquery` on raw text;
  - ranked by `ts_rank`, with ties in M2 H1's order (source id, version `COLLATE "C"`, chunk index);
  - `score` = the cosine to the question's embedding, so ADR 0011's gate keeps its meaning;
  - `LIMIT :k`.
- **`retrieval/fusion.py`:** `rrf_fuse(ranked_lists, *, k=60, limit) -> list[FusedHit]`, with body
  `raise NotImplementedError`:
  - score(d) = Σ 1/(k + rank) over the lists that hold d, ranks from 1, as an exact `Fraction`;
  - ties broken by score, then best single rank, then M2 H1's order;
  - a chunk listed twice in one list, or two different hits with one chunk id, raises `ValueError`;
  - no lists, or only empty ones, give `[]`.
- **Strict-xfail tests:**
  - Binding, no database: one `execute`, every value bound.
  - Postgres, on a hand-checkable mini corpus: the owner's connection, where RLS cannot help;
    another business's transaction; staff-only, draft and retired chunks never reach a customer
    (one planted unique keyword each); another model's chunks excluded.
  - Hostile input (zara S1): tsquery syntax (`& | ! ( ) :* <-> ' \`) and a 2,000-character
    question never raise, and the question never appears in an error or log line.
  - Fusion: at k = 1, vector [A, B, C] and text [C, A, D] give A 5/6, C 3/4, B 1/3, D 1/4, in the
    order A, C, B, D. [A] and [B] at k = 60 tie at 1/61 and are decided by source id. Duplicates
    raise. The result does not depend on input or hash order (checked with two `PYTHONHASHSEED`
    values).
- **Canary registry:**
  - `NEEDS_HAND_WRITE` becomes "read path → frozenset of the `[HAND-WRITE]` stubs it runs", marked
    through the existing `pending_hand_writes(*stubs)`. `tests/test_canary_checks.py:298-322` is
    updated to match, with a red proof: a path on a pending stub without its strict xfail fails.
  - `text_search_as_staff` → {`text_search`}.
  - `"rrf_fuse"` goes into `NEEDED_OUTSIDE_READ_PATHS` until PR 4 adds the hybrid paths.
  - `"text_search"` and `"rrf_fuse"` go into `PENDING_HAND_WRITE`.
- **Gate** (moved here from the draft's separate canary PR): `scripts/canary_gate.py` refuses an
  xfail under `tests.canary` that is not a `PENDING_HAND_WRITE` mark.
  - Red proof: `xfail(reason="flaky in CI")` on
    `test_no_read_path_shows_another_businesss_canary` fails the gate.
  - No allowlist [D10 (a)]: PR 1's `RESET` removes the only other xfail first.
- **Owner-connection clause checks** for `vector_search`, `text_search` and M2 PR 5b's retire are
  mirrored into `tests/canary` (M2's own clause tests stay where they are), so the gate covers the
  `WHERE` clause, not only RLS. The API-level red proof over a leaking full-text leg moves to PR 4,
  which closes the "after H1" item in `m2-close-out.md` (both decided 2026-10-24).
- **Decided 2026-10-24** (shesha accepted the team's proposals; ADR 0014 and ADR 0009 notes):
  - `ts_rank` in its two-argument form (no length normalisation).
  - `rrf_fuse` raises `ValueError` when `k` or `limit` is below 1.
  - `FusedHit` fields: `hit`, `rrf_score`, `ranks` (by input-list position, vector first).
  - `Question` moves to `retrieval/types.py`; `search.py` re-exports it.
  - M2's test helpers move into `tests/retrieval/conftest.py`, `corpus.py` and `recording.py`.
  - The gate accepts only the reason format built by `pending_reason` (read back by
    `pending_stubs`); a static scan refuses any `xfail` in `tests/canary` outside
    `pending_hand_writes`.
  - A NUL (U+0000) in a question is not H1's to handle; `Question` refuses it in PR 4 (or its
    own small PR).
- **Guide** `docs/learning/m3-hybrid.md`, covering:
  - tsvector, tsquery and text-search configs;
  - why OR, and why per-chunk configs;
  - why `ts_rank` has no corpus statistics, and why that is an isolation property;
  - RRF, and why scores are not averaged;
  - why the gate stays cosine;
  - the switches and the decisions;
  - explain-back questions, without printed answers.

## H1 — `m3/text-search` (`[HAND-WRITE]`, written by shesha [D5, 2026-10-24]; time-box 3 h)
- The body of `text_search`, about 17–18 lines of SQL (updated 2026-10-24; was about 15). Docker
  works again (2026-10-23), so `make test-integration` runs the Postgres cases locally.
- **Done when:**
  - every case goes `xfailed` → `XPASS(strict)` → (switches removed) `passed`;
  - the canary suite and gate pass in CI;
  - ananya-rag and zara-safety have reviewed it;
  - the explain-back (guide §6) is recorded in CLAUDE.md.
- If a team test fails in H1's PR, its owner fixes the test in a separate commit; H1 never changes
  to suit it [carried, M2 decision 2026-10-22].

## H2 — `m3/rrf` (`[HAND-WRITE]`, written by shesha [D5]; time-box 2 h)
- The body of `rrf_fuse`, about 20 lines, no database. Done when as H1 (ananya-rag and kenji-ml
  review).

## PR 4 — `m3/hybrid-search` (ananya; diego reviews the transaction, zara the gate and canary) · M
- **Settings,** platform defaults (rule 9), unmeasured: `rag_retrieval_mode` `vector | hybrid`
  (default `vector`), `rag_candidate_k` (20, decided D6/D15 (i); at least `rag_context_k` and 10, at most
  `MAX_K`), `rag_rrf_k` (60) [D6].
- **Hybrid search:**
  - The question is embedded once.
  - One business transaction holds the model check, `vector_search` and `text_search` at
    `candidate_k`, and `rrf_fuse` limited to `candidate_k`.
  - **One snapshot** [D8] (moved here from the draft's PR 1): `SET TRANSACTION ISOLATION LEVEL
    REPEATABLE READ READ ONLY` is the transaction's first statement, then `set_config` as today.
    Tested against Postgres: a commit between two statements is invisible to the second; a write
    inside is refused; the canary suite stays green.
  - `AnswerRun.hits` holds the fused list in fused order as plain `SearchHit`s [proposal], so the
    harness and M2's scoring stay unchanged.
  - The top `context_k` fused hits go to the answer.
  - **Vector mode is M2's path unchanged:** M2's tests pass untouched.
- **Evidence under hybrid** [D7]: fused hits whose cosine is at least `rag_min_score`; confidence v0
  unchanged.
  - zara S3: a keyword-stuffed approved chunk with a low cosine is never evidence
    (`test_the_evidence_gate_never_compares_a_cosine_threshold_with_a_fused_score`).
  - FTS-only hits are counted.
- **`rag_answer` gains optional, metadata-only fields:** the retrieval mode, hits per leg, FTS-only
  hits, and per-stage durations (embed, transaction, each leg, fuse) on a monotonic clock.
  - `record_version` stays 1 [proposal]: ADR 0007's rule "additive; a breaking change bumps it" is
    read as governing ADR 0011's sentence, and the ADR 0011 amendment records this. A test shows
    that M2's records still validate.
  - A canary string in the question, a chunk, a title and the answer appears in no record (this
    extends `tests/answer/test_answer.py`'s check).
- **[carried, `m1-pr-checks.md:531-534`]**
  - Route handlers still log only `type(exc).__name__` for gateway errors.
  - No route-level timeout is added around `complete()`. If one ever is, its expiry is recorded as
    `cancelled`, not `deadline_exceeded`; the ADR 0007 amendment says so.
- `GET /v1/kb/search` and `POST /v1/answer` follow the mode. Response shapes do not change (`score`
  stays the cosine).
- **NUL in a question** (decided 2026-10-24, shesha; ADR 0014 decision 5 amendment): `Question`
  refuses U+0000 with a 422 on both routes in both modes, since Postgres text cannot hold it. A
  test sends it to each route in each mode; the 422 echoes nothing, like every 422 today
  (`kb/errors.py`). It may land as its own small PR instead, never in H1.
- **Canary:**
  - New `READ_PATHS` keys `"hybrid_search"`, `"GET /v1/kb/search (hybrid)"` and
    `"POST /v1/answer (hybrid)"`, each mapped to {`vector_search`, `text_search`, `rrf_fuse`}.
  - `"rrf_fuse"` leaves `NEEDED_OUTSIDE_READ_PATHS`.
  - Red proof (moved here from PR 3, decided 2026-10-24): each hybrid API route, with a clean
    vector leg and a leaking `text_search` stand-in, is caught. It extends
    `test_red_proof_an_api_read_path_over_a_leaking_search_is_caught` and closes the "after H1"
    item in `m2-close-out.md`.
- **Snapshot test** (zara S8): an approval that commits between the legs (a hook in a stand-in leg)
  never puts the retired and the new version of one source into one fused list.
- Local tests use stand-ins for H1 and H2 (on the team side, as in M2). The real paths sit behind
  `PENDING_HAND_WRITE`.
- **Built (2026-10-25, ananya).** In review; nothing merged. No migration, no new dependency, no
  change to `text_search`, `rrf_fuse` or any M1/M2 hand-written body.
  - Settings `rag_retrieval_mode`, `rag_candidate_k` (10 to `MAX_K`, and at least
    `rag_context_k`, checked at startup in both modes) and `rag_rrf_k` (at least 1).
  - `db.business_transaction(..., snapshot=True)`: `SET TRANSACTION ISOLATION LEVEL REPEATABLE
    READ READ ONLY` first, then `set_config`. `retrieval/search.py`: `RetrievalConfig`,
    `SearchStats`, `fused_search` (both legs, then the fusion, vector list first) and the hybrid
    path of `SearchService`. The vector branch is M2's code byte for byte.
  - The answer: legs at `max(context_k, candidate_k)`, `AnswerRun.hits` the whole fused list, the
    top `context_k` fused hits then the cosine gate, confidence over those, FTS-only hits and
    FTS-only evidence counted. `rag_answer` gains one optional `retrieval` block (hybrid only,
    with `candidate_k`, `context_k` and `rrf_k`); vector records keep M2's keys; `record_version`
    1.
  - `Question` refuses U+0000 (a 422 on both routes in both modes, echoing nothing).
  - The eval harness refuses a `rag_retrieval_mode` other than `vector` (decision 6 below).
  - Canary: `hybrid_search` (also on the owner's connection, at `MAX_K` and 1) and both routes in
    hybrid mode, each on {`vector_search`, `text_search`, `rrf_fuse`}; `rrf_fuse` left
    `NEEDED_OUTSIDE_READ_PATHS`. The API red proof runs every route with each leg it runs
    leaking in turn (vector routes: the vector leg; hybrid routes: either leg, the other clean).
  - Tests: `tests/retrieval/test_hybrid_search.py` and `tests/answer/test_hybrid_answer.py` (no
    database; team-side stand-ins `StandInTextSearch` and `StandInFuse`, which are not full-text
    search or RRF), `tests/retrieval/test_hybrid_snapshot.py` (Postgres: the first statement, a
    commit between statements, writes refused, the approval between the legs; each with READ
    COMMITTED as its red proof; and both hybrid routes over the real legs, a strict xfail until
    H1 and H2).
  - Measured on the local stack (counts, not results): the M2 test files are unchanged and pass;
    targeted Postgres run (`tests/retrieval`, `tests/answer`, `tests/canary`, `tests/evalrun`)
    691 passed, 91 xfailed (after shesha's decisions of 2026-10-25; 682 before them); `make
    check` 2689 passed, 206 skipped, 42 xfailed, coverage gate passed; canary gate "54 canary
    tests passed; 16 xfailed on pending [HAND-WRITE] stubs (rrf_fuse, text_search)". Mutations
    checked by hand, each turning its tests red: a gate on the RRF score, the whole fused list
    sent on, `transaction_ms` read before the commit, a record `hits` cut at `context_k`, a
    `top_score` over the whole fused list, hybrid without the
    snapshot, `SET TRANSACTION` dropped. The nine that survived the first round now fail too
    (2026-10-25, each applied alone to the working file, then restored byte for byte, checked by
    sha256): each leg cut to `k` before the fusion (M09c); FTS-only hits counted over the legs,
    not the fused list (M12f); a wall clock as `SearchService`'s or `fused_search`'s default
    (M14a, M14b); RRF's k taken from the constant, in the fusion call or in the record (X07);
    the record's `candidate_k` taken from the setting when `k` is larger (X12); evidence
    re-sorted by cosine (X10); the canary `hybrid_search` path at READ COMMITTED (M17g); the
    harness running under a hybrid setting (X09: now refused, decision 6, in the CLI and in
    `check_run`). Also the block's `context_k` taken from `candidate_k`. With scratch reference versions of H1 and H2 (in the
    session's scratch directory only, deleted afterwards) and the pending set emptied, the
    hybrid canary paths, H1's and H2's tests and the real-legs test passed (gate: 70 canary
    tests passed); the one failure was H2's hash-seed test, whose fresh interpreters load the
    stub.
- **[decided 2026-10-25] (shesha)** Every recommendation of PR 4's review accepted (ADR 0014,
  "Built 2026-10-25"; decision 8, for PR 5, is in PR 5's section):
  1. **The record:** one optional nested `retrieval` block on `rag_answer`, hybrid only (a
     vector-mode record has exactly M2's keys), `record_version` 1. The block also carries
     `context_k` (`rag_context_k`): additive, so still version 1.
  2. **Evidence:** the top `context_k` fused hits, then the cosine gate (D7), in fused order.
  3. **Confidence v0:** the best cosine among those top `context_k` fused hits.
  4. **`GET /v1/kb/search`:** each leg fetches `max(k, candidate_k)`; the fused list is cut at `k`.
  5. **`rag_candidate_k >= rag_context_k`** is checked at startup in both modes. Side effect in
     vector mode, which never reads `rag_candidate_k`: a `rag_context_k` above `rag_candidate_k`
     (default 20) now stops startup there too, as does a `rag_candidate_k` outside 10 to `MAX_K`.
     Raising `rag_context_k` past 20 therefore means raising `rag_candidate_k` with it.
  6. **The eval harness refuses** a `rag_retrieval_mode` other than `vector` until PR 5 adds
     `--retrieval-mode`: `cli._check_settings` (dry and live runs, and `ingest`), and again
     `harness.check_run` (any caller), with one fixed message, `harness.VECTOR_MODE_ONLY`, that
     names the setting, never its value. Tests:
     `test_a_hybrid_retrieval_mode_is_refused_until_pr5_adds_retrieval_mode` (CLI: dry, live,
     ingest) and `test_execute_refuses_a_hybrid_retrieval_mode_for_any_caller` (harness), each
     also showing that the same Settings in vector mode get through.
  7. **No upper bound on `rag_rrf_k`** (at least 1).

## PR 5 — `m3/eval-hybrid` (noah; ananya and zara review) · M
- **`run.json`:**
  - `ConfigInfo` gains an optional `retrieval` block (mode, candidate k, RRF k, language-map
    version), omitted for vector mode.
    - "Omitted" means a None block is left out of the fingerprint payload (as
      `RagAnswerRecord`'s wrap serializer does for its `retrieval` block), not dumped as
      `"retrieval": null`; the vector fingerprint pin below proves it (review of PR 4, noah N3).
    - `--search-k` skips Settings' rule `rag_candidate_k >= rag_context_k`, so in hybrid mode
      either refuse a `--search-k` above `rag_candidate_k`, or record the effective candidate k
      (`max(rag_candidate_k, search_k)`) in the block, as `RetrievalRecord.candidate_k` already
      does (noah N3).
    - **[decided 2026-10-25] (shesha)** The language-map version is the sha256 of the canonical
      `TEXT_SEARCH_CONFIGS` plus `FALLBACK_CONFIG` (`ingestion/language_map.py`), not a
      hand-bumped constant. PR 5 builds it and fixes the canonical form; no code in PR 4.
  - The harness takes `--retrieval-mode`, like `--min-score`. It replaces PR 4's refusal of a
    hybrid `rag_retrieval_mode` (`harness.VECTOR_MODE_ONLY`, PR 4's decision 6; ADR 0014, "Not
    built here"): PR 5 removes the refusal and its two tests together.
  - **`compare` in step** (review of PR 6, ananya A4). When `ConfigInfo` gains `retrieval`,
    `compare`'s run table gains a "Retrieval" row (mode, candidate k, RRF k; "vector (no
    retrieval block)" when the block is absent), and the report points to each folder's
    summary.md for the FTS-only and headroom counts. Until then, PR 6 prints a missing block as
    `config.retrieval: … (absent: vector mode)` among the differing fields.
- **Fingerprint pin (D3's tripwire).** A test computes the fingerprint of a fixed vector config. It
  reads `catalog_version` from `gateway/models.toml` and `PROMPT_VERSION` from
  `answer/prompt.py`, never from literals, and compares the result with a pinned literal. A
  catalog or prompt bump therefore turns CI red. After the window, updating the literal is a
  deliberate, reviewed change.
- **Every list is recorded and checked.**
  - The recording seam wraps every leg and the fused list. `results.jsonl` gains optional component
    lists per question (chunk ids, ranks, matched groups; no text).
  - The business, status and audience check (ADR 0013 decision 2) runs on every list before the
    answer step: a leak that fusion drops is still a leak.
  - Tests: a stand-in full-text leg that returns another business's hit aborts with
    `CROSS-BUSINESS LEAK`, exit 4; a staff-only hit aborts with `AUDIENCE LEAK`.
  - **Scoring in hybrid mode** (review of PR 4, noah N2). Today `_score` scores `call.hits`, the
    recorder's vector list, and refuses a run whose `call.k` is not `search_k` or whose
    `run.hits` differ from `call.hits`: in hybrid mode both checks would raise on every question.
    In hybrid mode `_score` scores `run.hits` (the fused list) with H2, builds `hit_refs` and the
    evidence zip from it, and keeps `call.hits` as the vector component list. The checks become
    `call.k == max(candidate_k, search_k)` and `run.hits` == the recorded fused list.
  - **The recorder's own time** (noah N4). The recorder runs its document lookup inside the
    wrapped leg, so the leg's `vector_ms` (and `text_ms`, once that leg is wrapped too) includes
    the harness's own leak-check query. The recorder times its lookup on the same monotonic clock
    and writes it per question to `results.jsonl`; the per-stage rows either subtract it or are
    labelled "includes the harness leak-check lookup". Test: a ticking clock where the lookup
    takes a known number of steps.
- **`summary.md`:**
  - One retrieval row per list (vector, text, fused) at the metric k, each with its interval as in
    ADR 0013.
  - FTS-only evidence counts.
  - The **headroom count** [D13]: scorable questions where H2's per-question hit@10 is 1 and hit@5
    is 0 on the fused list, listed by id. This is harness counting over H2's values, not a new
    metric function [D5 boundary].
  - The safety rows by outcome (answered, dont_know, handoff) per business (zara S7). A rise in
    "answered" on safety rows is listed in the PR as a regression to explain.
  - Per-stage latency from the `rag_answer` durations, through M1's `latency_summary` (p95 only at
    n ≥ 20). It is labelled "local Docker on an 8 GB Mac, synthetic" and never compared with §6's
    p95 < 6 s.
- The plan the CLI prints shows that a hybrid run on an existing namespace makes no document
  embedding calls.
- **M3 spend so far** [D15 (iii)]: $5.00 for all of M3's paid runs, each still capped at $1.00.
  - Before a live run's cap prompt, the plan the CLI prints shows three figures. The first is the
    sum of `cost.total_usd` over the counted folders' `run.json` as committed in `HEAD`; the
    others are the ceiling and what is left. "Lower bound" is added when any counted folder has
    `llm_cost_complete` false.
  - Counted [proposal]: every committed live folder whose `run.json` has the `retrieval` block.
    Every run D15 lists is hybrid. A vector run made for M3 (D3's re-run after a price change) also
    counts. It has no `retrieval` block, so noah adds a way to name it and says how in the PR.
  - `--live` is refused when the typed cap exceeds what is left. The refusal names the ceiling,
    the sum and the folders.
  - Only committed folders count, so each live folder is committed before the next live run. A
    run that left no `run.json` (a kill, a failed write) is added by hand from its printed spend
    and record logs. shesha then types a cap within what is left.
  - Tests: planted committed folders give the sum and the lower-bound label, and a cap above what
    is left is refused before any call.
  - Like the typed cap, this only catches mistakes. The providers' own limits stay the real cap
    (ADR 0007).

## PR 6 — `m3/eval-compare` (noah; ananya reviews) · S — can start now
- `uv run python -m vectapilot_api.evalrun compare <run A> <run B>`:
  - It refuses unless both folders share dataset, version, `freeze_sha256`, split, embedding model
    and chunk config, and the refusal names the field.
  - It prints every other fingerprint field that differs, warns when more than one does, and prints
    both commits.
  - It warns when the later run's ingest wrote chunks (`IngestInfo.chunks_written > 0`) for an eval
    namespace the earlier run also used: the vectors were re-created (D3).
- Over the questions scorable under both:
  - hit@5 and MRR@5 per run through M2's H2, each with its interval as in ADR 0013;
  - discordant pairs by question id (A hit and B miss; B hit and A miss), also per business and
    language;
  - counts and intervals only: no p-values and no "better" wording.
- **Dev is for choosing.** On test [proposal, maya]: only two folders already in the ledger, counts
  only, labelled "held-out: reported, not used to choose".
- Dry or fake runs are labelled "not a result".
- Comparing two chunk configs stays manual, from the two folders' `results.jsonl` (ADR 0013) [D14].
- ADR 0013 amendment: its Consequences gain the command.
- **Built (2026-10-25, noah).** In review; nothing merged. No migration, no new dependency,
  `pyproject.toml` and `uv.lock` unchanged; no paid call, no database (run folders are files).
  - Files: `evalrun/compare.py` (new); `evalrun/ledger.py` (the ledger model and its reader,
    moved unchanged from `cli.py` so `compare` can read HEAD's ledger); `evalrun/cli.py` (the
    `compare` command); `evalrun/report.py` (the fingerprint payload as its own function, same
    hashes; `_metric_row` takes `k` and `counts_only`, defaults unchanged);
    `tests/evalrun/test_compare.py` (47 tests after the review fixes, TEST-ONLY folders in
    `tmp_path`); ADR 0013, "Amended 2026-10-25".
  - Checks: `make check` passed. The fingerprint refactor was compared with HEAD's code on 4
    configs: identical hashes. Mutation audit: 16 single changes to `compare.py`, each applied
    alone to the working file, then restored byte for byte (sha256 checked). 15 failed a test.
    The survivor (per-run rows at the run's metric k instead of 5) led to a stronger test, and
    then all 16 failed.
  - **Review fixes (2026-10-25; ananya A1, A2, A4, A5; kenji K1, K3, K5, K6).**
    - A1: the groups come from every selected question, not from the pairs, so a business or
      language whose questions are all left out keeps its rows with n = 0 ("none scorable under
      both", plus a line naming it) instead of vanishing and passing D15 (ii) unseen. The
      discordant table gains "Left out: one run only" and "Scorable in neither" (by reason) per
      group. The questions section lists by id the questions scorable in one run only, not scored
      (no evidence), unscorable in both, and not run in one or both runs.
    - A4: the run table's "Mode" row is now "Live or dry". An absent `config.retrieval` reads
      "(absent: vector mode)". PR 5's checklist has the "Retrieval" row.
    - K3: on test, the run table carries each run's test-run ledger row (`report._ledger_row`),
      and the banner names a held-out test RERUN with its reason.
    - K6: the interval caveat says each run's intervals are computed as summary.md computes them,
      over the paired n at k = 5, so they can differ from the run's own summary.md; and that
      overlapping intervals do not show that the runs agree.
    - K1, K5: run B's file is reversed in the two main tests (pairing by id, not by line). The
      fingerprint, stored-value and search-k refusals are tested on both sides. A split_group
      fixture is counted over paired questions only. The word guard matches word starts (it
      gained "higher", "lower", "win", "lost", "lose", "gain", "beat", "outperform", "adopt" and
      "superior"), and both table headers are asserted exactly (no difference column).
    - Fixture: a chunk id keeps its matched groups across runs (`<id>-relevant`), as a stored
      chunk does. Before, ids were named by rank, which A2's check would have flagged.
    - Mutation check of the fixes: 16 in-memory mutants of `compare.py` (each run in its own
      pytest process, no repo file written). These include the reviewers' K1 and K5 survivors,
      groups from pairs only, a dropped ledger row or rerun banner, a dropped "vector mode",
      and both A2 checks removed or comparing groups by rank. Each failed at least one test, and
      the unmutated control passed 47.
    - Checks after the fixes: ruff, format check and mypy clean; `make check` passed (2736
      passed, 206 skipped, 42 xfailed; gateway coverage gate passed).
- **[decided 2026-10-25] (shesha)** On PR 6's proposals and review findings:
  1. **The test split, as built:** only two folders already in the test-run ledger as committed
     in HEAD, counts only, "Held-out: reported, not used to choose" (maya's rule).
  2. **Prints only, no `--out`:** redirect stdout to keep the report; nothing is written under
     `evals/reports/` unless shesha chooses to.
  3. **Keep the second re-ingest warning:** a document id that differs between the runs for one
     eval business, source and version (it also catches a re-ingest by a third run).
  4. **Refuse a run.json** whose `config_fingerprint` is not the hash of its own fields.
  5. **A config block present in one run only** (PR 5's `retrieval`) counts as one differing field.
  6. **"Not a result"** for a dry run and for a run that is not complete (partial, failed, leak).
  7. **D15 (ii)'s safety half, as read in ananya's A5:** every `category == "safety"` question
     that ran in both runs (outcome not `not_run`), whatever its retrieval status, answered under
     one mode but not the other, as counts and ids per business, with no ranking. It is still not
     built (below).
  8. **Changed: the scoring-rule check refuses** (ananya A2), it does not warn. A question scored
     in both runs with a chunk whose matched groups differ, or a question scored in one run and
     unscorable in the other, is refused with one fixed message naming the cause and saying to
     re-run both runs on one commit (`compare.SCORING_RULE_REFUSAL`). `not_run` stays benign.
  9. **D15 (ii)'s hit half: a business with n = 0 paired questions gives no evidence and blocks
     adoption**, because the retrieval mode is platform-wide. `compare` shows it as
     "n = 0: no evidence (blocks adoption under D15)" (PR 9, step 4).
- **Decisions applied (2026-10-26, noah).** In review; nothing merged.
  - `compare.py`: `_check_scoring_rule` raises `CompareRefused` with `SCORING_RULE_REFUSAL`
    (`REGROUPED_CAUSE` or `SPLIT_CAUSE`), after the stored-value check and before any pairing;
    the two warnings are gone. A business row with n = 0 shows `NO_EVIDENCE_BLOCKS` as its n in
    the discordant table, and a line under the table says that it blocks adoption under D15 (ii).
    A language row with n = 0 does not block (D15 reads per business).
  - **[proposal]** Every business in either run.json's `namespaces` (the dataset manifest's
    businesses) now keeps its rows, with n = 0 when none of its questions was selected. Before,
    such a business had no row at all, so it would have passed D15 (ii) unseen.
  - With the refusal, a question scorable in one run only must have been not run in the other.
    So the main test fixture's x-01 and x-02 are `not_run` on one side, and a TEST-ONLY folder
    with an unrun question is written as partial (as `harness.final_status` would write it).
    That report is "not a result"; two complete live runs are tested as a result separately.
  - Tests for the mutation audit's gaps (`tests/evalrun/test_compare.py`, 68 passed):
    - M54: a spy shows `hit_at_k` asked at k = 5 for each paired question in both runs (both
      runs at metric k 6, so these calls are compare's own).
    - M57: a spy goes through `cli.run(["compare", A, B])`, so the CLI uses `io.metrics`.
    - M58: the bootstrap calls the passed `mean_reciprocal_rank` exactly
      `bootstrap_resamples` times per run (200 and 120), for "all" (n = 12) only, not for
      en (n = 9).
    - M33: the no-ranking test covers both folder orders and a 2-and-2 tie, on dev and test.
    - M34: every non-table line of the main fixture's report is pinned exactly.
    - M38: the "Live or dry" cell is checked for live/live, dry/live, live/dry and dry/dry.
    - M41: a later ingest into a namespace the earlier run never used gives no warning.
    - M47: a ledger line with matching fields but another run id is refused.
    - M26: the Wilson bounds 39 % to 86 % (8 of 12), 47 % to 91 % (9 of 12) and 44 % to 97 %
      (5 of 6), worked out by hand in the test.
    - Also: each scoring-rule refusal (both causes, both sides) and its literal text; an n = 0
      language that does not block; a manifest business with no selected question.
  - Mutation check: 21 in-memory mutants of `compare.py`, `cli.py` and `report.py`, each run in
    its own pytest process; no repo file was written, and the sha256 of all four files was the
    same before and after. Each was killed by the test written for it. For M54 (pair cell from
    the stored value in run B), M57, M58 (run B's rows read run A's info), M34, M41 and M47, the
    new test was the only one that failed. So these were real gaps. The unmutated control passed
    68.
  - Checks: ruff, format check and mypy clean; `make check` passed (2757 passed, 206 skipped,
    42 xfailed; gateway coverage gate passed). No database, no paid call; `pyproject.toml`,
    `uv.lock` and the migrations unchanged.
- **Not built:** D15 (ii)'s safety half (decision 7's reading) is a per-question outcome check
  that `compare` does not print yet. Until it is built, it is read from both folders'
  results.jsonl.
  - **Its range is not the pairs** (review of PR 6, ananya A5). A safety question must be
    handed off but need not have evidence, and a question without evidence is `not_scored`, so
    it never enters the pairs. Its test uses a safety question without evidence.
  - [open, for shesha] Build it in PR 5 or in a small follow-up.

## PR 7 — `m3/faithfulness-labels` (noah; zara reviews) · S — before M2 step 16 if possible
- **[proposal] `faithfulness_labels.jsonl` per run folder.** One line per shown answer (outcome
  `answered`): `faithful | unfaithful`, `labelled_by`, `labelled_on`. M2's `labels.jsonl` and
  summaries stay untouched.
  - The guide defines both labels. Faithful means every claim in the answer is supported by the
    text of the chunks it cites. Correct means the answer matches an approved source. Neither
    stands in for the other.
- **`label-sheet`** prints each cited chunk's text, rebuilt from the corpus at `run.json`'s chunk
  config after checking `freeze.json`'s corpus hashes. Terminal only, as today.
- **`summarize`** adds human faithfulness `x of n` with Wilson (harness counting like Wilson
  [D5 boundary]). A §6 ≥ 95 % verdict appears only under ADR 0013 decision 10: no verdict below
  n = 10, for unlabelled answers, or for anything but a complete live run.
- **Expected outcome, stated now.** This is an estimate from v0.1's manifest minimums, not a count:
  v0.1 likely has fewer than 10 shown answers on the test split, so "no verdict" is the expected
  result there. Dev's `x of n` is tuning information only.

## PR 8 — `m3/search-latency` (ananya; diego pairs) · S — $0, after H1
May fold into PR 4 if H1 has merged by then.
- **The measurement.** `EXPLAIN (ANALYZE, BUFFERS)` of M2 H1's and M3 H1's statements as
  `vectapilot_app`, inside a business transaction, at **pilot size only** [D14]. Never as the owner,
  which skips RLS locally.
  - It is read-only: it inserts and deletes nothing.
  - Repeated runs give the median and p95 through M1's `latency_summary` (p95 only at n ≥ 20).
- **The output** is committed with the commit, the PostgreSQL and pgvector versions, row counts and
  the Docker VM's memory, labelled "local Docker on an 8 GB Mac".
- It records whether the planner uses any index for `@@` or `<=>` under RLS (the GIN and HNSW
  questions).
- **ADR 0008 amendment:** exact search kept or not, the numbers, and D14's revisit trigger.

## PR 9 — `m3/close-out` (shesha's live runs; noah, ananya and maya help)
Agents never pass `--live` and never see a key. The commands follow M2's runbook
(`docs/learning/m2-eval-run.md`). D3's window and volume rule hold throughout. **Spend:** every
paid run of M3 counts towards one $5.00 ceiling [D15 (iii)], and each run stays within M1's $1.00
cap. Before each live run, read "M3 spend so far" in the plan the CLI prints (PR 5), and type a
cap within what is left. Commit each live folder before the next live run. Each step names its own
dependency, so steps 2–4 can overlap M2's steps 15–17.
1. **Faithfulness labels** on M2's live dev and test folders.
   *Needs:* PR 7, plus M2 step 14 (dev folder) and step 16 (test folder).
2. **Live hybrid dev runs** on the frozen v0.1: only D15 (i)'s variants, which are hybrid at M2's
   chosen `rag_min_score` with `candidate_k` 20, plus at most one `rag_min_score` variant. Each
   uses the embedding model, chunk config and prompt of M2's dev folder. Commit every folder,
   including the ones that lost. All of them stay within the $5.00 ceiling.
   *Needs:* M2 steps 10, 13 and 14 (a frozen v0.1 and M2's committed vector dev folder), PR 4,
   PR 5, H1 and H2.
3. **`compare`** M2's vector dev run with each hybrid dev variant, and read the headroom count.
   *Needs:* step 2 and PR 6.
4. **Choose on dev under D15 (ii)'s adoption rule.** Hybrid is adopted only if, for each business,
   "vector hit, hybrid miss" ≤ "hybrid hit, vector miss", counted over the questions scorable
   under both runs. **[decided 2026-10-25] (shesha)** A business with n = 0 gives no evidence and
   blocks adoption, because the retrieval mode is platform-wide. `compare` shows it as
   "n = 0: no evidence (blocks adoption under D15)". Adoption also needs that no safety row that
   vector did not answer is answered under hybrid, counted over every safety question that ran in
   both runs, whatever its retrieval status (review of PR 6, ananya A5: a safety question without
   evidence is never scorable; this reading decided 2026-10-25, PR 6's decision 7). If hybrid is adopted, a small PR flips `rag_retrieval_mode`'s default and
   carries the `compare` output.
   *Needs:* step 3.
5. **If hybrid is adopted:** one live test run of the hybrid config (ledger entry before the first
   paid call), within what is left of the $5.00, then correctness and faithfulness labels and
   `summarize`.
   **If vector stays:** M3 makes no test run, and M2's test folder is the result. A second vector
   test run would only be a ledgered RERUN.
   *Needs:* step 4 and the committed hybrid dev folder.
6. **Only if step 5 ran:** `compare` M2's vector test run with M3's hybrid test run, counts only,
   labelled held-out.
   *Needs:* M2 step 16 and step 5.
7. **The note** `docs/notes/m3-what-i-measured.md` (skeleton by noah):
   - every figure as printed, with dataset version, commit and config;
   - dev runs as tuning, including the variants that lost;
   - latency labelled local;
   - "not shown at 95 % confidence" or "no verdict" where the interval or n says so;
   - M3's total live spend, every counted folder by name, against the $5.00 ceiling.
   *Needs:* steps 1–6.
8. **The H1/H2 explain-backs,** the M3 study PDF (questions with answers) and the quiz.
- **Done when:** "How we will know M3 worked" is complete, the reported test folder's status line
  reads "may feed the note", and CLAUDE.md says M3 is complete.

## Stretch (only if a decision turns it on)
**Neither is built in M3** (D4 (b), D13 (c), decided 2026-10-23). Both designs are kept for when
their triggers fire: D4's 30 labels, or PR 5's headroom count.

**S1 — LLM judges** (D4 (a) or (c); noah, with diego for the alias and zara for the prompt) · M,
plus a `[HAND-WRITE]` for the agreement metrics (2 h)
- The first draft's judge stays as designed:
  - an offline `evalrun judge` command over a committed folder, with the fake provider in CI;
  - `--live --max-usd X` for shesha only, through M1's reservation;
  - a fingerprinted `JUDGE_PROMPT_VERSION`, with the answer and every cited chunk in boundary
    blocks from `choose_boundary`;
  - a closed verdict schema whose quotes go only to `judgments.jsonl`;
  - `judge_failed` counted against faithfulness;
  - zara's injection tests;
  - agreement metrics as exact `Fraction`s (`"judge_agreement"` in `NEEDED_OUTSIDE_READ_PATHS` and
    `PENDING_HAND_WRITE`);
  - ADR 0015.
- Added by review:
  - The `judge` alias merges **before M2's step 13**, so every M2 and M3 run shares one
    `catalog_version`, or after M3's last live answer run; never in between [D3].
  - The judge prompt is calibrated and revised only on dev folders plus constructed and adversarial
    rows. Their folder format, count and authorship are specified and sized in the PR, and shesha
    writes those rows. The frozen version then runs **once** on the test folders, and that agreement
    is reported separately as held-out.
  - "Calibrated" only from 30 labels on distinct shown answers.
  - With D4 (c), the correctness judge reuses the same machinery and protocol.

**S2 — Voyage rerank** (D13 (a) or (b); diego the adapter, ananya the wiring, noah the cap, zara
reviews) · M
- **First:** shesha checks and dates, with URLs, Voyage's rerank model id, multilingual and Hindi
  support, limits, price, billed-token rule and error statuses, and whether the training opt-out and
  zero-day retention cover rerank data.
- **The code:**
  - `rerank/` mirrors `embeddings/`: a protocol, a deterministic fake, and an adapter over `httpx2`
    with no new package.
  - `rerank/models.toml` holds a `Decimal` price, its source URL and the check date.
  - A `CappedReranker` and a metadata-only `rerank_call` record.
  - Covered by D9's opt-in; the evidence gate stays cosine.
- **The headroom pool** is the fused top 10, and N is set relative to the dev size before the run.
- **The runs:** a rerank dev run and, if it is adopted, a rerank test run, both inside D15's
  ceiling.
- **Hand-write boundary:** the adapter is `[DELEGATE]`. The reorder step in the search is retrieval
  logic, so it is a `[HAND-WRITE]` (about 1 h) [proposal, D5].

**Native structured output** (ADR 0006's conditional revisit, diego). This is still decided by M1's
live run. If it is triggered, the gateway PR merges only after M3's last live answer run (proposal:
M9), because the output mode is not part of the fingerprint [D14].

## ADRs
- **[ADR 0014 — Hybrid retrieval](../adr/0014-hybrid-retrieval.md),** accepted 2026-10-23 (PR 0).
  It covers:
  - the full-text column and the language map [D2];
  - OR semantics, `ts_rank`, RRF k = 60 with exact arithmetic, and `FusedHit` [D6];
  - the evidence gate under hybrid [D7] and one snapshot per search [D8];
  - `AnswerRun.hits` as the fused order;
  - the adoption rule [D15];
  - no GIN, BM25 or `pg_trgm`;
  - reranking and its headroom trigger [D13].
- **Amendments** dated 2026-10-23, in PR 0, listed there: ADRs 0006, 0007, 0008 (the ANN revisit,
  pending PR 8), 0009, 0011 and 0013.
- **Still to come:** ADR 0008's second amendment, with PR 8's numbers.
- **No ADR 0015** (LLM judges) in M3 [D4 (b)].

## Decided 2026-10-23
shesha accepted every recommendation for D1–D4, D6–D14, D15 (i) and (ii) and D16. shesha answered
D5 and D15 (iii) personally and left one question open, decided 2026-10-24 (below). Items
tagged **[proposal]** elsewhere in this file are not covered by these decisions.
- **D1 — Q6, translation:** (a) allowed now. There is no language filter, the answer is in the
  question's language, and the citation shows the source's language. Later (c): per business,
  once a config loader exists. zara may tighten it for safety content in M7.
- **D2 — Text-search config:** (b) per chunk, from `documents.language` through a platform map
  (`en` → `english`, `hi` → `hindi`, else `simple`), backfilled by migration 0005.
- **D3 — Measurement window:** (a) the three rules in "Order of work" 1–3, with PR 5's pin test as
  the tripwire:
  - no `models.toml`, `PROMPT_VERSION` or output-path change;
  - M2's test run on M2's dev code;
  - no `reset-db` or volume prune.
- **D4 — LLM judges:** (b) both deferred to M4 or later, until a dataset has at least 30 human
  labels on distinct shown answers. M3 reports human faithfulness labels only (a deviation).
- **D5 — Who writes:** shesha writes H2 `rrf_fuse`, and H1 `text_search` too (2026-10-24,
  below). The boundary proposals stand: harness counting over H2's values (`compare`'s pairs, the
  headroom count, faithfulness `x of n`) is `[DELEGATE]`.
- **D6 — H1/H2 spec:** (a) as in ADR 0014:
  - an OR of the question's lexemes in each chunk's own config, ranked by `ts_rank`;
  - `score` stays the cosine;
  - RRF with k = 60 as an exact `Fraction`, and `FusedHit` as a wrapper;
  - `candidate_k` 20 (provisional);
  - no GIN index and no BM25.
- **D7 — Evidence under hybrid:** (a) fused hits with a cosine ≥ `rag_min_score`, re-tuned on dev
  only; FTS-only rescues are counted.
- **D8 — One snapshot:** (a) REPEATABLE READ READ ONLY in hybrid mode (PR 4), amending ADR 0011
  decision 5.
- **D9 — Paid-call guard:** (a) `api_allow_paid_llm` covers every paid provider the API builds, and
  keeps its name (ADR 0006 amendment).
- **D10 — Pool-return `RESET`:** (a) in PR 1, and its strict xfail is removed (ADR 0009 amendment).
- **D11 — Citations:** (a) v0 is kept and native Citations are declined for M3. Per-claim
  citations return with M7's grounding check.
- **D12 — Tracing:** (a) the span, its exporter and `traceresponse` move to M9, whatever Q4b's
  answer. M3 adds per-stage durations to `rag_answer` (ADR 0007 amendment).
- **D13 — Reranking:** (c) none in M3, a deviation from the README's roadmap. PR 5's headroom count
  is the measured reason and the revisit trigger.
- **D14 — Other deferrals:** (a) all as listed:
  - query rewriting to M9 or v0.2;
  - prompt caching to M9;
  - native structured output after M3's last live answer run;
  - ANN search stays exact; PR 8 measures at pilot size only, and the synthetic scale moves to M9
    (ADR 0008 amendment);
  - the catalog check for tables without `business_id` to M7, or to the first PR that adds one;
  - comparing two chunk configs stays manual.
- **D15 — Live-run protocol** (fixed before any hybrid result is seen):
  - (i) the variants: hybrid at M2's chosen `rag_min_score` with `candidate_k` 20, plus at most one
    `rag_min_score` variant;
  - (ii) the adoption rule: per business, "vector hit, hybrid miss" ≤ "hybrid hit, vector miss",
    and no safety row that vector did not answer is answered under hybrid;
  - (iii) **$5.00 in total** for all of M3's paid runs, each run still ≤ $1.00. PR 5 counts the
    spend, and PR 9 follows it.
- **D16 — Dataset v0.2:** (c) decided after M2's dev run shows whether vector retrieval is at its
  ceiling. The default is (a), v0.1 only. A larger v0.2 is also D4's trigger.

## Decided 2026-10-24
No question is open now.
- **Who writes H1 `text_search`** (the rest of D5): (a) shesha, from the guide. D5 is fully
  decided: shesha writes both H1 and H2. The explain-back stays part of acceptance.
- **NUL in a question:** (a) `Question` refuses U+0000 with a 422 on both routes in both modes, in
  PR 4 or its own small PR, never in H1 (ADR 0014 decision 5 amendment).
- **PR 3's proposals,** all accepted: listed in PR 3's "Decided 2026-10-24", including the mirrored
  owner-connection checks and the API-level red proof moving to PR 4.

## Risks
- **Everything measured waits on M2's close-out.** Its critical path is the human-written dataset,
  and friends take days. M3 can be built well before it can be measured.
- **Small n and the v0.1 ceiling.** About 28 dev and 12 test questions, and 129 short chunks
  (ananya's count). A delta of 1–3 questions sits inside the intervals, so "no measurable
  difference" is likely and must be written as such. Faithfulness on test likely gets "no verdict".
- **Baseline drift.**
  - A `catalog_version` bump from a price re-check or a new alias, a prompt change, or a change to
    the output path would split the fingerprints of M2's and M3's runs, or refuse M2's test run.
  - A change to the vector path would break the paired comparison.
  - Guards: D3's window, PR 5's pin test reading the live catalog and prompt version, vector mode
    unchanged, and D11.
- **Local data loss.** `make reset-db` or a volume prune, tempting on a 93 % full disk, would force
  a paid re-embed, perhaps with different vectors. Guards: D3 and PR 6's re-ingest warning.
- **Forking paths.** Tuning many variants on about 28 dev questions until something wins. Guard:
  D15, fixed in writing on 2026-10-23, before the first hybrid run.
- **Spend.** The $5.00 ceiling is counted from committed folders only. An uncommitted folder, a
  killed run or a vector run made for M3 is invisible to the count unless it is committed or added
  by hand. Guards: commit before the next run (PR 9), PR 5's refusal, and the providers' own
  limits.
- **Score scales.** An RRF score (about 0.0125–0.033 at k = 60 and `candidate_k` 20; corrected
  2026-10-24) against the 0.3 cosine gate would make every
  answer `dont_know`, and a max of scores would let a keyword-stuffed chunk through. Guards:
  `FusedHit` and the gate test.
- **Isolation.** A full-text leg without its business clause passes the app-role canary, because
  RLS hides it. The owner-connection checks and the stricter gate land in PR 3, before H1. PR 1's
  `RESET` closes the known-gap xfail [D10 (a)].
- **Text search quality.** `hindi` and the fallback `simple` have no stop list, so OR queries match
  many chunks of those languages on function words (and `simple` chunks that contain English, e.g.
  a mis-tagged `eng` or `und` document, on English ones). `data/sample` has no fallback-language
  document today (17 `en`, 1 `hi`).
  There is no lexical match across scripts, and romanized Hinglish relies on English stemming.
  Guard: PR 3's guide puts one `simple` chunk that shares only stop words with a question into
  H1's mini corpus.
- **RLS and indexes.** Non-leakproof `@@` and `<=>` may keep any GIN or HNSW index unused by the app
  role. Measure as the app role, never as the superuser owner.
- **Telemetry.** FastAPI's spans, metrics and logs would record `?q=`, request bodies and stack
  traces, and accept a client's `traceparent`, as soon as any provider exists. PR 1 pins all three
  off.
- **Paid paths.** `GET /v1/kb/search` is already an ungated paid embedding path with Voyage
  configured (D9).
- **Migration under FORCE RLS.** With a non-superuser owner (M10), the backfill `UPDATE` would skip
  rows silently. The `SET NOT NULL` guard must be proven in PR 2.
- **Machine limits.** 8 GB RAM, a 4 GB Docker VM and a data volume 93 % used. Migration 0005
  rewrites `chunks` (small today), and M3 adds no synthetic data.
- **Scope creep.** The span (D12), the judges (D4) and reranking (D13) are decided out of M3.
  Reopening any of them adds an M-sized PR, human labelling, provider checks and paid runs at the
  end of M3, inside the $5.00.
- **Review bandwidth and learning debt.** About 12 merges, each reviewed by shesha, on top of M1's
  and M2's human steps. Four explain-backs are open (M1 H2/H3, M2 H1/H2), and M3 adds two
  hand-writes.

## If deadlines bite (cut in this order)
1. **PR 8 (latency).** ADR 0008 then records "not measured in M3; exact search kept at pilot size",
   as a deviation.
2. **PR 1's telemetry pin.** No provider exists before M9's span; ADR 0007's amendment records the
   pin as due with the span.
3. **PR 7's guide extras.** Keep the labels and `x of n`.

Never cut: canary coverage of the new paths, PR 3's stricter gate with the owner-connection checks,
PR 1's `RESET` [D10 (a)], D3's window and PR 5's pin test, PR 5's spend count [D15 (iii)], and
PR 6 (`compare`).
