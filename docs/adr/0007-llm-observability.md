# ADR 0007 — LLM observability and data handling

- **Status:** accepted · 2026-10-14 · chosen by shesha (M1 plan, Q2 and Q4)

**Context.** Every model call must be visible (cost, latency, outcome) without prompts, answers or
keys leaking into logs. The playbook's gateway rule includes a trace span per LLM call. Businesses
are in India and other countries (Q4b: which), and prompts may later hold health or personal data.

**Options.** (a) Langfuse Cloud now, via its SDK; (b) Langfuse Cloud later, via OpenTelemetry
(OTLP); (c) self-hosted Langfuse; (d) a structured JSON record in our own logs now, a span later.

**Decision.** (d) now; (b) is the leading candidate, decided when the span lands.
- **One `llm_call` record per logical call**, emitted once, also on error, deadline and cancel: a
  metadata-only contract versioned by `record_version` (additive; a breaking change bumps it).
  Fields: identity (business, alias, models, catalog, geo), outcome (closed enum), retries, repairs,
  tokens, cost, latency, request ids, optional `trace_id`, `operation` (exact list: `CallRecord`).
- **Never logged:** prompts, completions, headers or keys, in records, log lines, exception messages
  or chained causes. The adapter never logs; the SDK and HTTP-client loggers are pinned to WARNING
  after import, so neither a DEBUG root logger nor `ANTHROPIC_LOG` can turn on transport traces.
- **Measurement.** Per alias: n distinct prompts, median, min/max, timeout rate (timed-out calls
  counted, never dropped), refusals and first-try validity (k/n), cost/call; inclusive p95 only if
  n ≥ 20, not per language or business; cache off. Labelled "synthetic prompts, single home network"
  with commit, dataset version, model ids, catalog version. Per-call latency is not compared with
  §6's message-to-reply p95 target; no target is claimed as met.
  - **Where runs go (2026-10-15, shesha; `llm_cli.py`).** One directory per run. Live:
    `evals/reports/<dataset>/<version>/<UTC start>-live/`, committed. Dry:
    `evals/reports/tmp/<dataset>/<version>/<UTC start>-fake/`, git-ignored, never committed. Each
    holds `summary.md`, `summary.json` and the summary's two inputs, `calls_made.jsonl` and
    `llm_call_log.jsonl` (metadata only); a live summary is committed with both. The inputs are
    written and flushed line by line as the run goes, each call listed before it is sent, so a run
    cut short still shows every call that may have been billed.
  - **Partial runs and the note (2026-10-15, shesha).** A run is partial only if it did not make
    every planned call (stopped by the cost cap, `--max-calls`, an account error such as
    `spend_limit`, `billing` or `auth_failed`, a bound miss, an error, or a kill). Cost-incomplete
    calls do not make a run partial: they are reported as "k/n cost-incomplete" and the cost is
    labelled a lower bound. Only a complete live run feeds the "what I measured" note: never a dry
    run, a partial run, or a run that fails the record-count check (it writes no summary). As
    built, also not with unreviewed prompts, uncommitted changes, or a dataset whose files are not
    byte for byte those in the commit (outside the repository, untracked, git-ignored or changed).
    A kill (a closed terminal, a second Ctrl-C) writes no summary: a failed run, its two inputs
    kept as evidence only. One Ctrl-C (`interrupted`) is summarised, as partial if calls remained.
    Decided 2026-10-15 (shesha): a run stopped by its last planned call (an account
    error, a bound miss, an error or an interrupt on call n/n) made every call, so it is not
    partial and exits 0, but it does not feed the note either.
  - **Live-run stops (2026-10-15, shesha; `llm_cli.py`).** The `--max-usd` cap (≤ $1.00)
    holds call by call: each call's worst case is reserved first and the run stops, partial,
    before a call that would cross it, so the whole plan's worst case may exceed the cap. The worst
    case uses a conservative prompt-token bound (1 token ≥ 1 byte, ≤ 200 tokens of overhead, the
    highest input-side price), checked after every call; the first miss stops the run. The run
    also stops after a call that ends in `spend_limit`, `billing` or `auth_failed` (no further call
    can succeed) or `invalid_request` (our own request is wrong; the unverified prepaid-credit 400
    lands here too), or that was answered from the `us` geo (billed 1.1×: a setup error, the
    workspace default must be `global`; that one call can still cross the cap). A missing geo does
    not stop the run: the call is cost-incomplete and keeps its reservation. The stop detail is
    metadata only, never prompt or completion text. Full stop list: the PR 9 runbook in
    `docs/plans/m1-pr-checks.md`. The cap is typed again at a terminal: this guards against
    mistakes only. The real control is that the key never enters an agent's environment, and the
    Console's monthly limit is the only cap across runs.
- ⚠ **Trace span deferred to M3** (playbook deviation: due with the gateway); it may slip, at the
  latest to M9, only if the exporter or region (Q4b) is still open when M3 closes, recorded here.
  `trace_id` is the seam: a later exporter links records to spans without changing callers.
- **Langfuse facts.** Cloud Hobby: 50k units/month, 30 days of data, 2 users; regions US, EU, JP and
  HIPAA US, not India (langfuse.com/pricing, 2026-10-13). SDK 4.17.0 needs classic `httpx` plus the
  OpenTelemetry SDK and exporter (PyPI, 2026-10-14), so `httpx` and `httpx2` would both ship.
  Self-hosting is out (ADR 0005). If chosen, Cloud via OTLP gets metadata only, region after Q4b.
- **Data handling.** Synthetic data only until Q4b and Q4c (business-context §9) are answered and
  M7's privacy controls exist. The first-party Claude API has no India region: `inference_geo` is
  `global` or `us`, data at rest `us` only (/manage-claude/data-residency, 2026-10-14). We assume
  the longest documented retention: inputs and outputs 30 days, up to 2 years if flagged by trust
  and safety (classification scores up to 7 years), longer if the law requires (privacy.claude.com
  article 7996866, 2026-10-14). Red-flag messages (self-harm, poisoning) may hit the 2-year rule: an
  M7 item. Haiku and Sonnet 5.5 are not Covered Models (/manage-claude/api-and-data-retention).
- **Key handling.** `VECTAPILOT_ANTHROPIC_API_KEY` belongs to a dedicated workspace with a $10/month
  limit (not settable on the default workspace; /api/rate-limits, 2026-10-13). Never in the repo
  tree, a shell profile or history: shesha enters it with `read -s` in a separate terminal that
  never starts Claude Code or Cursor, then closes it; Keychain only if every read prompts. A
  convention, not a technical control (every agent has Bash): the key is never written where an
  agent can read it; agents never read or handle it, or pass `--live`. If it leaks: rotate first.
- **TLS and proxy variables (2026-10-15, shesha).** With the anthropic provider, startup refuses
  `SSL_CERT_FILE` and `SSL_CERT_DIR` (they replace the OS trust store) and `SSLKEYLOGFILE` (it
  writes TLS session keys to a file); messages name the variable, never its value. Proxies
  (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, the macOS system proxy) keep working.
- **No `.claude/settings.json` deny rule for the key** (decided 2026-10-15, shesha). Until the
  Claude Code docs say otherwise, assume a Read deny rule would not stop Bash `cat` or `env`; the
  control stays that the key never enters an agent's environment (Key handling).

**Consequences.** No dashboard in M1: stdout JSON lines and, per live run, one committed directory
(the summary and its two inputs). No Langfuse dependencies yet; OTLP stays vendor-neutral.
Summaries rely on the record contract; span due in M3.
