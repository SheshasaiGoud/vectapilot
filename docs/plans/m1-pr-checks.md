# M1 — acceptance checks per PR

ADRs 0006 (LLM gateway) and 0007 (LLM observability) record only decisions, reasons, playbook
deviations and dated sources. This file holds how each M1 PR must implement or prove those
decisions; each PR copies its section into its own acceptance check and ticks it off.
"PR A, 2–9" are the M1 plan's step numbers (order in CLAUDE.md, Current status), not GitHub
pull-request numbers. Tags: **[0006]** / **[0007]** = moved out of that ADR · **[plan]** = from the
approved M1 plan (2026-10-14), never in an ADR · **[review]** = added by the ADR review
(2026-10-14) · **[note]** = Maya's note, to be resolved in that PR (not a decision).
**[step A review]** / **[step 2 review]** = from that step's review (2026-10-14); the step 2 lines
are as shesha decided them.
When M1 closes, this file is either kept as the record of what was checked or deleted in the
close-out PR (decided then).

## PR A — `m1/fix-config-secrets`
- Nothing moved here from the ADRs. The plan's check stands: a short wrong-scheme DSN canary is red
  before `hide_input_in_errors=True` and green after; neither the canary nor `input_value` appears.

## PR 2 — `m1/gateway-types-and-stubs`
- **[0006]** `errors.py`: every gateway error carries its class, HTTP status, provider request id
  and `retryable`. Classes from the error table: `SpendLimitReached`, `BillingError`, `Transient`,
  `AuthenticationFailed`, `InvalidRequest`, `ProviderError`, plus `DeadlineExceeded` and typed
  refusal and truncation errors.
- **[review]** The H2 stub signature changes (option A, approved): `call_with_retries` also receives
  the deadline and a clock, next to `sleep` and `rand`.
- **[0007]** `CallRecord` in `types.py` follows the exact `llm_call` field list under PR 6.
- **[0006]** The guide covers temperature and streaming as concepts only (both approved playbook
  deviations), and omitted `thinking` = adaptive thinking, billed as output tokens.

## PR 3 — `m1/gateway-foundation`
- **[0006]** The catalog file is `vectapilot_api/gateway/models.toml`, shipped inside the package.
  `create_app` loads and validates it; this is the only app wiring in M1. The api-image job proves
  the file ships.
- **[0006]** Per model: five token classes (input, output, cache read, 5-minute cache write, 1-hour
  cache write), the long-prompt tier (**[plan]** as worded on the pricing page), source URL,
  checked date. Per alias: model id, `max_output_tokens`, effort.
- **[0006]** Prices are read with `tomllib` and `parse_float=Decimal`, then validated, because whole
  numbers stay `int`. **[review]** Tests: a whole-number price (for example `3`) becomes
  `Decimal("3")`, and `0.10` equals `Decimal("0.10")` exactly.
- **[0006]** No `claude-` literal in `src/**/*.py`; model ids live only in `models.toml`.
- **[0006]** Settings validator, one test per rule:
  - `test`/`ci` ⇒ fake provider only; `production` ⇒ not fake.
  - anthropic ⇒ `VECTAPILOT_ANTHROPIC_API_KEY` (`SecretStr`) required.
  - anthropic plus any of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` or
    `ANTHROPIC_LOG` set ⇒ startup error (the SDK reads them).
  - deadline < attempt timeout ⇒ startup error.
  - invalid catalog, or a configured model without a price ⇒ startup error.
- **[step A review]** `hide_input_in_errors` does not hide text a validator writes into its own
  message. Every new Settings validator gets a canary test: the value (e.g. the key) is absent from
  both `str(error)` and `errors(include_input=False)`; messages name fields and rules, never values.
- **[review]** The key comes from the process environment only; a key found in `.env` stops
  startup. So the tmp `.env` test now expects an error (the plan said it "still resolves to test +
  fake").
- **[review]** `conftest.py`: before any app import, set `VECTAPILOT_ENVIRONMENT=test` and
  `VECTAPILOT_LLM_PROVIDER=fake` in `os.environ`, and remove `VECTAPILOT_ANTHROPIC_API_KEY`,
  `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL`.
- **[step A review]** `conftest.py:12` builds `UNREACHABLE_SETTINGS` at import time without
  `_env_file=None`, so an exported `VECTAPILOT_*` variable (e.g. `VECTAPILOT_LOG_LEVEL=debug`) or a
  value in `apps/api/.env` can break or change the whole test run. Pass `_env_file=None` and every
  field explicitly (or build it in a fixture); test: the suite passes with junk `VECTAPILOT_*`
  values exported. Moved to this step by shesha (2026-10-14).
- **[review]** `BusinessId` (rule in business-context §3.1) is checked with `re.fullmatch`, because
  `re.match` with `$` also accepts a trailing newline. Tests reject: a trailing newline, uppercase,
  `--`, a leading or trailing `-`; **[note]** proposed: also the 3/40-character bounds (2 and 41
  rejected).
- **[step 2 review]** One per-attempt provider response type (content, stop reason, `Usage`, model
  id, request id, `inference_geo`), shared by the fake provider and the adapter. The price check
  confirms which classes the long-prompt tier raises.

## PR 4 — `ci/coverage-gate`
- **[0006]** One committed script runs the gate, used by both the CI `api` job and `make check`
  (**[plan]** via `make coverage`).
- **[0006]** `[tool.coverage.run]`: `branch = true`, `source_pkgs = ["vectapilot_api"]` (so a module
  no test imports counts as 0 %); `[tool.coverage.report]`: `precision = 2`.
- **[0006]** Both checks use the same `--include='*/vectapilot_api/gateway/*'`:
  1. combined total: `coverage report --include=… --fail-under=95` (statements and branches
     together, ≥ 95.00 %);
  2. branch-only: from `coverage json` with the same include,
     `100 × covered_branches ≥ 95 × num_branches` as an integer comparison; 0 branches fails.
- **[0006]** No `pragma: no cover` or `exclude_also` for `gateway/` unless an ADR note allows it.
- **[review]** The hand-off reports statements k/m and branches k/m separately, plus a branch
  checklist mapped to named tests.
- **[review]** Local failure proof: add an untested branch on purpose and `make coverage` exits
  non-zero. The PR's own green run shows the step executes.
- **[review]** Scope grew with the branch-only check; PR 4 will take longer than the plan's 0.5 h.

## PR 5 — `m1/anthropic-adapter`
- **[0006]** Dependencies (approved 2026-10-14): `anthropic` 1.x, the newest release at least 7
  days old **on install day**; review the `uv.lock` diff package by package: `anthropic`, `httpx2`,
  `httpcore2`, `jiter`, `truststore`, `sniffio`, `docstring-parser`. Publisher check for `httpx2`:
  PyPI project of the Pydantic organisation, Trusted Publishing from github.com/pydantic/httpx2
  (author metadata: Tom Christie). Declare `httpx2` as a dev dependency (tests import
  `httpx2.MockTransport`). Remove the TestClient warning filter (ADR 0003 update); **[plan]**
  remove the `httpx` dev dependency if unused.
- **[0006]** Import ban: only `gateway/providers/anthropic.py` imports `anthropic`, in `src/` and in
  `tests/`. Enforce it with ruff TID251 or a test, whichever is shown to catch
  `from anthropic.types import …`.
- **[0006]** Client: `AsyncAnthropic(api_key=…, base_url=…, max_retries=0, timeout=…)`. `api_key=`
  and `base_url=` are always passed explicitly. The SDK timeout sits slightly above the per-attempt
  limit, as a backstop only.
- **[0006]** Spend-limit shapes (first row of the error table): a 400 whose message is "You have
  reached your specified [workspace] API usage limits…", or a 429 with
  `error.details.error_code` = `enforced_spend_limit_reached`. Sources: /api/errors,
  /api/rate-limits (2026-10-14).
- **[review]** One MockTransport test per error-table row. A spend-limit response with
  `x-should-retry` true, false or absent gives `SpendLimitReached` after exactly 1 request.
  `x-should-retry` may change retrying only for `Transient` and `ProviderError`.
- **[0006]** Re-read `_base_client._should_retry` in the installed SDK (v1.13.0 on 2026-10-14) for
  the header and the 408/409 rules; the ADR's source rests on a reading of v1.13.0 on GitHub.
- **[review]** 402: what happens when prepaid credit runs out is unverified. Record what the new
  org actually returns. **[note]** Agents never make live calls, so this observation comes from
  shesha (it may land with the PR 9 live run).
- **[0006]** Gateway errors are raised outside the `except` block, so no SDK exception (its request
  holds the key header) stays chained. **[review]** The canary asserts `err.__context__ is None`.
  **[note]** Proposed: also assert `err.__cause__ is None`.
- **[0007]** Loggers pinned to WARNING after import (in the adapter): `anthropic` and `httpx2`
  (`ANTHROPIC_LOG` sets these at import), plus `httpcore2`, `httpx` and `httpcore`. Re-read the
  names in the installed SDK.
- **[0007]** Canary: runs through the real adapter on MockTransport, in a subprocess with
  `ANTHROPIC_LOG=debug` and `VECTAPILOT_LOG_LEVEL=DEBUG` set before import. It finds no prompt,
  completion or key in caplog, stdout/stderr or exception text, and is shown red with the logger pin
  removed. The adapter emits no log records at all.
- **[step 4 review]** The adapter's coverage comes from the in-process MockTransport tests (one per
  error-table row); the subprocess canary is not measured by the coverage gate.
- **[note]** Settings refuse `ANTHROPIC_LOG` with the anthropic provider, so the canary must build
  the adapter directly, not through `Settings`. Confirm in review.
- **[step 3 review]** Keep the `pydantic_settings` logger pin that step 3 added in `config.py`
  (`PYDANTIC_SETTINGS_DEBUG=1` otherwise logs every raw setting, key and DSN passwords included);
  set `PYDANTIC_SETTINGS_DEBUG=1` in the subprocess canary too. Also review proxy and TLS variables
  the HTTP client reads (`HTTPS_PROXY`, `SSL_CERT_FILE`, …).
- **[review]** Request body per alias: model id, effort `low` in `output_config`, `max_tokens`; no
  `temperature`, no `thinking`, no `inference_geo`.
- **[0006]** Usage mapping covers all five token classes (5-minute/1-hour cache-write split) and
  `usage.inference_geo`.
- **[step 2 review]** The `usage.inference_geo` above is the provider's field, carried in PR 3's
  per-attempt response type (not in `Usage`). The adapter validates every provider string into the
  shared types at its boundary: a mismatch becomes a `ProviderError` that does not echo the value,
  and an unknown stop reason maps to `other`. A `ValidationError` from a gateway model is never
  logged, chained or turned into a string.

## PR 6 — `m1/gateway-complete`
- **[0006]** Nested time limits: each attempt under `asyncio.timeout(attempt)`, the whole call under
  `asyncio.timeout(deadline)`.
- **[0006]** `CancelledError` is caught only to record, then re-raised. Inside the scope a firing
  deadline looks like `CancelledError`, so the outcome is set after the scope exits, from
  `.expired()`. **[review]** Test: a deadline that fires mid-attempt is recorded as
  `deadline_exceeded`, not `cancelled`. An external cancel propagates and still writes exactly one
  `llm_call` line.
- **[0006]** `cost_complete=false` when the answering model is unpriced, an attempt was cancelled or
  timed out after sending, or `usage.inference_geo` is not `global`. Cost is priced by the answering
  model's exact id and sums every billed attempt, repairs included.
- **[step 3 review]** An `inference_geo` of `None` (the provider did not say) also counts as "not
  `global`" ⇒ `cost_complete=false` (fail closed; decided 2026-10-14). Test:
  `FakeReply(inference_geo=None)` gives `cost_complete=False`.
- **[step 4 review]** Enforce that rule in `CallRecord._cost_rules` too: reject
  `not cache_hit and inference_geo != "global" and cost_complete` (today `inference_geo=None` with
  `cost_complete=True` is accepted), with a test for `None`.
- **[0007]** Exact `llm_call` record, emitted once in `finally` (also on error, deadline and
  cancel), metadata only:
  - `record_version`, `business_id`, alias, model requested and model answered, `catalog_version`,
    `inference_geo`
  - `outcome`, a closed enum: `ok`, `refusal`, `truncated`, `validation_failed`,
    `transient_exhausted`, `deadline_exceeded`, `cancelled`, `spend_limit`, `billing`,
    `auth_failed`, `invalid_request`, `provider_error`
  - `stop_reason`, `retries`, `repairs`, `attempt_timeouts` (**[step 2 review]**),
    `first_try_valid` (null if unstructured)
  - five token classes, `cost_usd`, `cost_complete`, `cache_hit`
  - `latency_ms`: monotonic clock, the whole call, backoff and repair included
  - provider request ids
  - optional caller fields: `trace_id` (W3C: 32 lowercase hex, else rejected) and `operation` (a
    slug, for M9's per-step view)
  - New fields are additive; a breaking change bumps `record_version`.
- **[0007]** The gateway never adds eval-case metadata; the measurement CLI does (PR 8).
- **[step 2 review]**
  - Per-attempt accounting lives in the `call` closure; the H2 signature is frozen once merged.
  - Building or emitting the record cannot mask the outcome. Test: an odd stop reason or model id
    through the fake gives exactly one record with the right outcome.
  - A request `max_tokens` above the alias's maximum ⇒ typed error before any call, never clamped.
  - For a call that reached the provider, attempts = 1 + `repairs` + `retries`; a repair's retries
    count in `retries`.
  - `attempt_timeouts` counts the attempts that hit the per-attempt timeout, at most the attempts.
- **[note]** Proposed by Diego (step 2), pending shesha: `model_context_window_exceeded` is a
  truncation ⇒ `OutputTruncated` (ADR 0006: truncation is a typed error; the stop-reasons page says
  "Treat the response as truncated", checked 2026-10-14). If reversed, remove it from
  `TruncationStopReason` and its tests.
- **[note]** Open, shesha decides: do `other` and the stop reasons M1 never asks for (`tool_use`,
  `pause_turn`, `stop_sequence`) become a result or a typed error? `LLMResult` accepts them today.
  Maya's proposal: fail closed, a typed error that is never cached.
- **[note]** Open: calls with no model call (a pre-call `max_tokens` rejection; a cache hit, PR 7).
  Is a record emitted, and what do `retries`, `repairs` and `attempt_timeouts` hold?

## PR 7 — `m1/gateway-structured-and-cache`
- **[0006]** The repair is a fresh request: the original messages plus feedback; it replays no
  assistant or thinking blocks. Feedback names only schema fields (locations) and error types.
- **[0007]** Structured-output errors carry only locations and error types, never the invalid
  input.
- **[review]** The canary sits in a JSON key as well as a value of the invalid output; it never
  reaches the feedback, logs or exception text.
- **[0006]** Cache "fails open": a cache error is a miss (the model is called; nothing is served or
  written).
- **[0006]** Tests: two businesses never share a key or a hit (two businesses → two keys; **[plan]**
  two schemas → two keys; a stored value whose `business_id` differs is never served). Refusals,
  truncations and errors are never cached. A hit records $0 and `cache_hit=True` and is
  re-validated.
- **[note]** A cache hit makes no model call: see PR 6's open note on records for calls with no
  model call (`CallRecord` already requires `attempt_timeouts` = 0 on a hit).

## PR 8 — `m1/measure`
- **[0006]** `--live` cap mechanics: before each call, reserve its worst-case cost
  (`max_output_tokens`, every retry and repair); stop before any call whose reservation would cross
  `--max-usd`. A cost-incomplete call keeps its reservation (never counted as $0) and marks the run
  partial; **[plan]** partial runs cannot feed the note.
- **[review]** The summary prints cache hits separately and "k/n cost-incomplete"; an incomplete
  `cost_usd` is labelled a lower bound.
- **[0006]** First-try validity is k/n over the non-forced structured cases only; the forced-repair
  case is reported on its own ("repair worked"). The rule (any non-forced first-try failure ⇒
  revisit native `output_config.format` in M3) is fixed before the live run.
- **[0007]** Measurement rules: p95 uses the inclusive method and appears only when n ≥ 20 distinct
  prompts; no p95 per language or business; timed-out calls count in the timeout rate and are never
  dropped; cache off in the live run; per-call latency is never compared with the §6 target (p95 <
  6 s, message to reply).
- **[0007]** The summary file is committed so the numbers last; this PR sets its location.
- **[0007]** The CLI adds case metadata to its own output; the gateway never does.
- **[step 2 review]** Timeout rate (decided 2026-10-14) = calls whose outcome is
  `deadline_exceeded` or whose `attempt_timeouts` > 0, as k/n; `deadline_exceeded` is also reported
  on its own. The worst-case reservation uses the effective `max_tokens` (the request's, else the
  alias's).
- **[step 3 review]** The aliases' `max_output_tokens` (starting values decided 2026-10-14: small
  1024, large 2048) are only confirmed by measurement: report `truncated` as k/n per alias plus the median and max
  `output_tokens` per alias, include at least one long non-English answer, and report latency
  against `output_tokens`. Rule fixed before the live run: any truncation ⇒ raise that alias's cap
  (new `catalog_version`) and re-run.

## PR 9 — close-out (shesha's live run)
- **[0007]** Key entry, in a separate terminal that never starts Claude Code or Cursor:
  `read -s VECTAPILOT_ANTHROPIC_API_KEY && export VECTAPILOT_ANTHROPIC_API_KEY`; close that terminal
  after the run. Keychain only if `security find-generic-password … -w` prompts on every read; if it
  returns the key silently, any agent could read it too.
- **[review]** Before the run: the workspace default inference geo is `global`; the shell has none
  of `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_LOG`, and the key
  is not in `.env` (startup refuses otherwise).
- **[0006]** Measure the output-token cost of adaptive thinking at effort `low`, and check cost
  accuracy against the Console's token counts per model.

## H2 — retry with backoff (shesha, `[HAND-WRITE]`)
- **[0006]** H2 receives the deadline and a clock (see PR 2). Tests: a backoff or Retry-After delay
  that would end past the deadline is not slept; the last error is raised instead.
  `SpendLimitReached` and other non-retryable errors ⇒ exactly 1 call.
- **[step 2 review]** Retry-After above `max_delay_s` ⇒ no retry, the error is raised (decided
  2026-10-14; alternative considered: wait it if it fits before the deadline).

H1 and H3: nothing moved from the ADRs.
