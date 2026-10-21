# M1 — what I measured (LLM gateway, live smoke run)

> **Skeleton — shesha fills it in; delete this box when done.**
> Source: exactly one committed run directory, `evals/reports/gateway_smoke/v0.1/<UTC start>-live/`,
> whose `summary.md` status line reads `complete live run: may feed the "what I measured" note`
> (runbook step 17 in `docs/plans/m1-pr-checks.md`). Exit 0 alone is not enough.
> Copy every figure as `summary.md` prints it (k/n stays k/n; no re-rounding). No figure that is
> not in that directory or the Console. Never paste prompt or completion text, request bodies or
> the key (ADR 0007).

**Label:** synthetic prompts, single home network.
_(Keep this line exactly: it is the label `summary.md` prints for a live run.)_

## Run identity
Copy from `summary.md`, table "Run" (row names in brackets).
- Run directory: _(path, and the PR that committed it)_
- Commit: _(row "Commit"; it must say "uncommitted changes: no")_
- Dataset and version: _(row "Dataset"; it must say prompt review `done`, files as committed `yes`)_
- Catalog version: _(row "Catalog version")_
- Model ids: _(row "Models (catalog)", alias → model id; and row "Models that answered")_
- Gateway limits: _(row "Limits": retries, repairs, attempt timeout, deadline, backoff,
  `max_output_tokens` per alias)_
- Calls: _(row "Calls": made of planned, distinct prompts, repeats, cache hits)_
- Cost: _(rows "Cost of every call" and "Cost cap"; say if it is a lower bound, k/n cost-incomplete)_
- Started (UTC): _(row "Started")_
- Prices re-checked on the pricing page on: _(date, runbook step 2)_

## Per alias
One row per alias, from `summary.md`, "Per alias". Distinct prompts only; repeats go below.

| alias | model that answered | n (distinct prompts) | latency median / min / max, ms | p95 (inclusive) or "not reported" | timeout rate (k/n) | deadline exceeded | refusals | truncated (k/n) | output tokens median / max of cap | first-try validity (k/n) + not validated, by outcome | forced repair | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

Repeats (section "Repeats"): _(one sentence per alias; never a p95.)_
Per language and per business (those two tables): _(describe only; they are not comparisons.)_

## The rules each figure follows (fixed before the run)
- **Latency** is per gateway call (`latency_ms`: whole call, retries and repair included), not
  message to reply. It is never compared with the business-context §6 target. No target is
  claimed as met.
- **p95**: inclusive method, per alias only, and only when the distinct prompts reach
  `P95_MIN_DISTINCT_PROMPTS` (ADR 0007). Never per language, per business or for repeats.
- **n** counts distinct prompts. Repeats are reported on their own; the cache is off in a live run.
- **Timed-out calls are kept.** Timeout rate = calls with outcome `deadline_exceeded` or any
  attempt timeout, as k/n (decided in the step 2 review).
- **First-try validity**: k/n over the non-forced structured cases whose first answer was
  validated; the rest are listed by outcome beside it. Any failure ⇒ revisit native
  `output_config.format` in M3. The forced-repair case is reported on its own.
- **Truncation**: any truncation ⇒ raise that alias's `max_output_tokens` (new `catalog_version`)
  and re-run. The note then uses the re-run, not this one.
- **Cost**: `Decimal`, priced by the answering model's catalog entry; a cost-incomplete call makes
  the total a lower bound.

## Cost accuracy against the Console (runbook step 14)
Per model: local token sums (the step 14 script) next to the Console's usage for the run's time
window.

| model | local tokens, per class | Console tokens | match? |
|---|---|---|---|

_(An exact match is expected only at zero cost-incomplete calls. A Console figure below the local
sum is a gateway problem: say so and open an issue.)_

## Adaptive thinking at effort `low`
_(What the output tokens show for each alias. If the records cannot tell thinking tokens from
answer tokens, write that instead of estimating.)_

## Rules that fired, stops and open items
- Truncation rule / first-try rule: _(fired or not, and what you will do)_
- Stops: _(row "Stopped" in `summary.md`)_
- Credit-exhausted response (PR 9, step 5 review item): _(only if observed: outcome, request ids,
  Console balance; never the key or a request body)_

## Limitations
_(Keep each one that applies; add any you noticed.)_
- Synthetic prompts for fictional pilot businesses; small n; no real customer traffic.
- One home network, one time window, global routing only: latency includes that network.
- Per-call latency only; nothing end to end.
- Valid for this commit, catalog version and prices on the check date only.

## In my own words
_(Three to five sentences: what surprised you, what you would measure next, and what you would
say about these numbers in an interview.)_
