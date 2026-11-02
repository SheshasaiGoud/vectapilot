# ADR 0008 — Embeddings

- **Status:** accepted · 2026-10-21 · chosen by shesha (M2 plan, decision 2: "yes to all
  recommendations"). Model `voyage-4` at 1024 dimensions (`voyage-4-lite` as the fallback) and the
  training opt-out before the first live call confirmed by shesha, 2026-10-21.

**Context.** M2 turns each business's documents into chunks and finds them by meaning. That needs an
embedding model for chunks and for questions, a vector column in Postgres (pgvector, ADR 0005) and
a way to run CI without paid calls. Businesses write in English, Hindi/Hinglish and (later) Telugu.
Anthropic offers no embedding model of its own and names Voyage AI as one provider
([platform.claude.com/docs/en/build-with-claude/embeddings], checked 2026-10-21).

**Options.** (a) Voyage AI, hosted, over plain REST with `httpx2`; (b) the same through the
`voyageai` SDK; (c) another hosted vendor; (d) a local open model (for example `voyage-4-nano`
open weights) on the 8 GB Mac.

**Decision.** (a). One small HTTP call needs no SDK, and `httpx2` is already locked (ADR 0006); it
becomes a direct runtime dependency because our code imports it, not a new package. (d) brings a
large ML runtime for a pilot; (c) was not researched.
- **Model (proposal).** `voyage-4`, 1024 dimensions, `float`, `input_type` = `document` for chunks
  and `query` for questions, `truncation=false` (an over-long chunk is an error, never silently
  cut). `voyage-4-lite` is the cheaper fallback if eval v0 shows no quality loss.
- **Model id per chunk.** Each chunk stores the model id and dimension it was embedded with. Search
  compares only vectors of one model id; changing the model means a full reindex of the business,
  never mixed vectors (we do not rely on Voyage's "4 series compatible" claim, unmeasured by us).
- **No ANN index yet.** Exact search with the `business_id` filter inside the query: perfect recall
  at pilot size and no filtered-index pitfalls (ADR 0004). Revisit in M3 with measured latency.
- **Fake embedder for CI.** Deterministic and offline (same text ⇒ same unit vector, configured
  dimension). `test`/`ci` ⇒ fake only, `production` ⇒ not fake (as ADR 0006). No quality number is
  ever reported from the fake.
- **Key.** `VECTAPILOT_VOYAGE_API_KEY` (`SecretStr`) follows ADR 0007: process environment only (in
  `.env` it stops startup), never logged, entered by shesha; agents never handle it. Paid embedding
  runs are human-only. Calls require a `BusinessId` and emit a metadata-only record.

**Facts** (all checked 2026-10-21): [emb] = https://docs.voyageai.com/docs/embeddings ·
[price] = https://docs.voyageai.com/docs/pricing · [faq] = https://docs.voyageai.com/docs/faq ·
[rate] = https://docs.voyageai.com/docs/rate-limits.
- `voyage-4-large`, `voyage-4`, `voyage-4-lite`: "general-purpose and multilingual retrieval",
  32,000-token context, 1024 dims by default (256/512/2048 optional) [emb].
- `voyage-multilingual-2` is now an "older model" (1024 dims, 32K) [emb].
- Per request: ≤ 1,000 texts; ≤ 320K tokens in total for `voyage-4` (1M for `-lite`, 120K for
  `-large`); `truncation` defaults to `true` [emb].
- Vectors are normalised to length 1, so cosine equals dot product [faq].
- Price per million tokens: `voyage-4` $0.06, `-lite` $0.02, `-large` $0.12; first 200M tokens of
  the 4 series free per account; Batch API 33 % cheaper, free tokens not applied [price].
- Data: by default Voyage may store API data and use it for training; opting out gives zero-day
  retention, needs a payment method and an organisation Admin, and cannot be undone in the
  dashboard [faq]. Rate-limit tier 1 needs a payment method [rate].
- **Unverified** (PR 4 checks them): Hindi or Telugu support (no page above names either; the
  `voyage-multilingual-2` post, blog.voyageai.com/2024/06/10/, evaluates 27 languages without
  listing them; the Voyage 4 post, blog.voyageai.com/2026/01/15/voyage-4/, names none); API region
  and data residency; a spend limit; limits without a payment method.

**Consequences.** One more vendor and key, and documents leave our machine (synthetic only until
the opt-out is done). Hindi quality is measured by eval v0, not assumed; Telugu stays unmeasured.
A model change costs a full reindex. Exact search grows linearly with chunks per business.

[platform.claude.com/docs/en/build-with-claude/embeddings]: https://platform.claude.com/docs/en/build-with-claude/embeddings

**Amended 2026-10-23 (shesha, M3 plan D14; `docs/plans/m3-pr-checks.md`): the ANN revisit is
pending PR 8's measurement.** Nothing is measured yet, so exact search stands.
- M3 PR 8 measures at pilot size only. It runs `EXPLAIN (ANALYZE, BUFFERS)` of the vector and
  full-text statements as `vectapilot_app` inside a business transaction, labelled local Docker on
  an 8 GB Mac. It also records whether the planner uses any index for `<=>` or `@@` under RLS.
  Its result amends this ADR again: exact search kept or not, with the numbers.
- The synthetic-scale measurement moves to M9. Its throwaway business then needs ADR 0013
  decision 3's delete guards (a reserved id form, checked at the delete, never in production) and
  a printed free-disk check.
- Revisit trigger: any business above 10,000 chunks, or the search transaction's measured p95
  above 300 ms (a target, not a measurement).

**Amended 2026-11-02 (M3 PR 8): exact search is kept.** Measured at pilot size by
`python -m vectapilot_api.evalrun latency` at commit `54a60c9`, labelled "local Docker on an 8 GB
Mac, synthetic sample corpus, fake embeddings". The report is
`evals/reports/latency/2026-11-02-local.md` (PostgreSQL 18.6, pgvector 0.8.7, Docker VM 3.83 GiB;
129 chunks, 47 and 64 customer-visible candidates per business; 71 questions × 5 passes, n = 355
per statement).
- Server-side execution (`EXPLAIN ANALYZE`, as `vectapilot_app` under RLS):
  - `vector_search`: median 0.248 ms, p95 0.321 ms.
  - `text_search`: median 0.209 ms, p95 0.413 ms.
- The product's hybrid search transaction, client-side (snapshot, `set_config`, model check, both
  legs, fusion, commit): median 6.883 ms, p95 9.915 ms.
- No index evaluates `<=>` or `@@`. The vector leg seq-scans `chunks`, and the text leg uses the
  `business_id` btree, then filters. Both operators are not leakproof, so under RLS an HNSW or GIN
  index could not be used ahead of the policy anyway.
- These are pilot-size numbers, never production ones, and never compared with business-context
  §6 as a pass. The revisit trigger above is unchanged: any business above 10,000 chunks, or the
  search transaction's measured p95 above 300 ms.
