# ADR 0006 — LLM gateway

- **Status:** accepted · 2026-10-14 · chosen by shesha (M1 plan Q1–Q5 and the review changes,
  2026-10-14)

**Context.** M1 builds the one path to a model. RAG, router, tools and guardrails (M3–M7) will call
it, so retries, time limits, structured output, cost, caching and `business_id` scoping live in one
place. Playbook: "95%+ branch coverage on gateway; cost shown per call". Live calls cost money.

**Options.** SDK: (a) `anthropic` 1.x, which requires `httpx2`; (b) pin 0.125.x, the last
`httpx`-based release (2026-08-19), until M3; (c) our own client (~+5 h, estimate). Retries: the
SDK's, ours, or both. Structured output: native `output_config.format`, or Pydantic plus a repair.

**Decision.** (a); `vectapilot_api.gateway` is the only path to a model. ⚠ = playbook deviation.
- **One entry point.** Only `gateway/providers/anthropic.py` imports `anthropic` (enforced in `src/`
  and `tests/`); SDK types and exceptions never leave it, not even chained (an SDK request holds the
  key header). Model ids live only in `models.toml`; callers use aliases.
- **Dependencies (rule 11, approved 2026-10-14).** `anthropic` 1.x (Anthropic, MIT), the newest
  release ≥ 7 days old on install day, `uv.lock` diff reviewed per package. It needs `httpx2`
  (Pydantic organisation; PyPI Trusted Publishing from github.com/pydantic/httpx2), which brings
  `httpcore2` and `truststore`. Dev: `coverage`; `httpx2` declared for `MockTransport`. **Reverses**
  the 2026-10-12 "httpx2: No" (ADR 0003). PyPI, 2026-10-14: `anthropic` 1.13.0 requires
  `httpx2>=2,<3`.
- **Retries: the gateway only** (SDK `max_retries=0`, so retries never multiply); full-jitter
  backoff is H2 (`[HAND-WRITE]`). Status and body set the class, first match wins: (1) the
  documented spend-limit 400 or 429 → `SpendLimitReached` (first, so it is never retried); (2) 402
  `billing_error` → `BillingError`; (3) connection errors, timeouts, 408, 409, 429, 5xx, 529 →
  `Transient`; (4) 401/403 → `AuthenticationFailed`; (5) 400/404/413/422 → `InvalidRequest`; (6)
  else `ProviderError`. Only `Transient` is retried (Retry-After, else backoff; a Retry-After above
  the backoff cap is not retried, decided 2026-10-14); `x-should-retry` can flip that for
  `Transient` and `ProviderError` only. Sources (2026-10-14): /api/errors,
  /api/rate-limits; for the header and 408/409, the SDK's `_base_client._should_retry` (v1.13.0).
- **Time limits.** Each attempt has a timeout (expiry: `Transient`); one deadline caps the call,
  retries and repair included (`DeadlineExceeded`). `CancelledError` always propagates. H2 gets the
  deadline and a clock, never sleeps past it, and raises the last error instead. Defaults (attempt
  15 s, deadline 30 s, 2 retries, backoff 0.5–8 s) are **safety bounds, not latency targets**.
- **Structured output.** Pydantic validation; at most one repair, inside the deadline: a fresh
  request whose feedback names only schema fields and error types. Refusals and truncation are typed
  errors, never repaired. ⚠ Native `output_config.format` is deferred, revisited in M3 if any
  non-forced case fails first try in the live run; it cannot be combined with Citations
  (/build-with-claude/structured-outputs, 2026-10-13).
- **Cost.** Prices are versioned data in `models.toml` (five token classes, long-prompt tier, source
  URL, checked date), read as `Decimal`. Cost sums every billed attempt, repairs included, priced by
  the answering model's exact id. `cost_complete=false` (and `cost_usd` is a lower bound) if that
  model is unpriced, an attempt was cancelled or timed out after sending, or `usage.inference_geo`
  is not `global`, since prices assume global routing (`us` is 1.1×, /manage-claude/data-residency,
  2026-10-14). List prices also assume the standard service tier: no request sets `service_tier`
  and the adapter drops `usage.service_tier`; carry it only if a priority-tier organisation
  becomes possible (PR 6, 2026-10-15). An unpriced configured model stops startup (never a
  silent $0). `cost_usd` is H1 (`[HAND-WRITE]`).
- **Caching.** A response-cache hook, off by default. Key = `business_id` prefix + sha256 over
  canonical JSON of (business_id, model id, catalog version, system, messages, schema, output
  params); the value carries `business_id`, checked on read, so two businesses never share a key or
  a hit. Fails open (an error is a miss). Refusals, truncations and errors are never cached; a hit
  costs $0 and is re-validated. The Redis store (M9) needs a TTL, a per-business purge, and short
  I/O timeouts (the gateway treats a cache error as a miss, but a hung `get` uses up the call's
  deadline and a hung `set` holds the answer until it); M9 also decides an additive `cache_error`
  record field (proposed), since cache errors are invisible in M1, and whether `set` gets its own
  short bound or runs after the answer is returned.
- **Not in M1.** ⚠ Hands-on streaming moves to M8, concept only. ⚠ Temperature is never sent,
  concept only (approved 2026-10-14): SDK 1.x removed it and current models reject non-default
  values (migration guide). Cross-model fallback is M4 router work (`[HAND-WRITE]`); server-side fallbacks stay off.
- **Models and region.** `small` = `claude-haiku-5-5`, `large` = `claude-sonnet-5-5`, no Opus.
  Effort `low` on both, always sent as the defaults differ (Haiku 5.5 `medium`, Sonnet 5.5 `high`).
  Omitted `thinking` means adaptive, billed as output (decided; measured live). `inference_geo` is
  never sent; shesha checks the workspace default is `global` (organisations with the legacy
  global-routing opt-out default to `us`). One workspace for all businesses until M7. Calls need a
  `BusinessId` (business-context §3.1). A `max_tokens` above the alias's maximum is rejected before
  any call, never clamped (decided 2026-10-14).
- **Settings.** Flat `VECTAPILOT_LLM_*`; the key is `VECTAPILOT_ANTHROPIC_API_KEY` (`SecretStr`),
  from the process environment only (in `.env` it stops startup). Fail fast: `test`/`ci` ⇒ fake
  only; `production` ⇒ not fake; anthropic ⇒ key required and none of `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_LOG`, `ANTHROPIC_CUSTOM_HEADERS` set
  (the SDK reads them; the last one even overrides an explicit key, added in PR 5); deadline
  ≥ attempt timeout; bad catalog ⇒ stop.
- **Exposure.** No HTTP route in M1. Paid calls are human-only: the CLI is fake unless `--live`,
  which needs `--max-usd` (≤ $1.00) and a typed confirmation; it stops before any call whose worst
  case could cross the cap. **Amended 2026-10-22 (shesha):** M2 PR 6's `POST /v1/answer` makes
  one uncapped call to `large` per answer with evidence, so until M7 adds budgets the API server
  refuses a paid `llm_provider` (any but `fake`) unless `VECTAPILOT_API_ALLOW_PAID_LLM` is true
  (default false): `create_app` stops at startup with a fixed `ValidationError` that names the
  setting, never a value. Only the API checks it, not `_llm_rules`, so the CLI's `--live` path is
  unchanged. Live evals in PR 8/9 go through the harness under M1's reservation cap, not the API.
- **Coverage gate** (CI `api` job and `make check`) on `gateway/`, unimported modules counted:
  coverage.py's combined total ≥ 95.00 % and, as the playbook says "branch", branches alone ≥ 95 %.
  No `pragma: no cover` or `exclude_also` in `gateway/` without an ADR note.

**Consequences.** One place to test, measure, trace and budget; the fake provider keeps tests free
and offline. Cost is deterministic and reproducible from recorded usage and versioned prices; the
live run checks it against the Console's token counts. The price: seven new runtime packages whose
changelogs we must read; retry and timeout code we own; two hung attempts use up the deadline; one
extra paid request per repair; prices go stale unless rechecked; one business can exhaust the shared
limit. Deferred: `llm_calls` table + RLS, registry check (M2); prompt caching, Citations, trace span
(M3); model choice (M4); budgets, PII masking, pre-flight limits (M7); Redis cache (M9).

**Amended 2026-10-23 (shesha, M3 plan D3, D9, D11, D14; `docs/plans/m3-pr-checks.md`).** The text
above stands; these lines supersede it where they differ.
- **Exposure** (D9 (a), built in M3 PR 1). The API's paid opt-in covers every paid provider the API
  builds, not only `llm_provider`. Until PR 1, `create_app` builds the configured embedder without
  it, so `GET /v1/kb/search` with `embeddings_provider = voyage` is an ungated paid path. From PR 1
  the server refuses `embeddings_provider = voyage`, and any later paid reranker, unless
  `VECTAPILOT_API_ALLOW_PAID_LLM` is true. The setting keeps its name, and the error names the
  setting, never a value. The harness is unaffected (ADR 0013 decision 4).
- **Deferred to M3, now decided** (the Consequences' list):
  - Prompt caching moves to M9's cost work (D14): it changes cost, not answers. Anthropic's minimum
    cacheable prompt length is unverified; check it, with URL and date, before any caching work.
  - Native Citations are declined for M3 (D11 (a); ADR 0011 amendment).
  - Native structured output is still decided by M1's live run. If triggered, the change merges
    only after M3's last live answer run (proposal: M9), because the output mode is not part of
    the eval fingerprint (D14).
  - The trace span moves to M9 (ADR 0007 amendment).
- **Catalog freeze** (D3 (a); ADR 0013 amendment). From M2's first live dev run to M3's last live
  answer run, nothing merges that changes `models.toml` or the gateway's output path. If a price
  changes inside that window, shesha chooses then: keep the old catalog (the note marks those
  costs stale), or bump `catalog_version` and re-run M2's vector dev run (paid).
