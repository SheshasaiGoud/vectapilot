# M4 — what I measured (the intent router: classifier, LLM-only, hybrid)

> **Skeleton — shesha fills it in; delete this box when done.**
> Sources: only committed folders under `evals/reports/intents/v0.1/` and the ledger
> `evals/reports/intents/test-runs.jsonl` (plan `docs/plans/m4-pr-checks.md`, PR 5). Test figures
> come from the **first ledgered test run of each mode** and the replayed hybrid built from them;
> reruns are listed with their reasons, never swapped in. Dev runs appear only under "Dev (tuning)".
> Copy every figure as `summary.md` prints it: `x of n` stays `x of n`, with its interval; no
> percentage where the summary prints none; no figure that is not in a committed folder. A
> folder whose summary starts with "DRY RUN" is never a source. Never paste a message's text, a
> prompt, a request body or a key (rows stay in the dataset; results hold ids only).

**Label:** synthetic data from two fictional businesses (healthcare and veterinary); human-written
eval rows, agent-drafted training rows (a deliberate distribution shift); no Telugu, no
e-commerce, and no self-harm messages (D5 (a)).

## Run identity
- Dataset: `intents` v0.1, `freeze.json` sha256 _(from run.json)_, frozen on _(date)_
- Commit of each test run: _(run.json `commit`; uncommitted changes must be `false`)_
- Embed folder: _(path; embedding model as answered; dimension; cache sha256; spend)_
- Model folder: _(path; artifact sha256; scikit-learn version; `n_iter`; C 1.0; lbfgs)_
- Calibration: T _(value)_, mean NLL on dev at T = 1 _(value)_ → at T _(value)_; at a bound?
  _(yes/no)_
- Thresholds (D8, rule `d8-a.2`, on dev): τ _(value, or "none": no confidence band)_, σ _(value)_;
  dev safety rows still missed: _(ids or none)_; the dev curve: `dev-curve.md` in the model folder
- Router prompt: `route-intent.1`; catalog _(version)_; `small` → _(model id)_
- The pin: _(the ledger's pin line: artifact, T, τ, σ, prompt, commit, date)_
- Test runs (ledger): classifier _(run id)_ · LLM _(run id)_ · reruns _(run ids and reasons, or none)_
- Bootstrap: seed _(value)_, B _(value)_; confidence 95 %

## Who saw which rows, and when (the blind order)
- Training rows drafted by Claude while `eval.jsonl` was empty; `train_draft_sha256` _(value)_
- Eval rows written by _(shesha / friends)_ without reading `train.jsonl`, committed on _(date)_
- Training sample verified by shesha after that, on _(date)_: _(x of n labels changed; ids)_
- Later training rows drafted without opening `eval.jsonl`: _(yes / none were added)_

## Safety-escalation recall (test; reported, not gated, in M4)
Excludes crisis messages (D5 (a)): never quoted as safety-escalation coverage before M7.

| Mode | business | decided as safety (`k of n`, Wilson 95 %) | missed (ids) | handed off, wrong priority (ids) | not decided, fail-closed (ids) |
|---|---|---|---|---|---|
| classifier | sunrise-clinic | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| classifier | paws-and-co | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| LLM-only | sunrise-clinic | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| LLM-only | paws-and-co | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| hybrid | sunrise-clinic | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| hybrid | paws-and-co | _to fill_ | _to fill_ | _to fill_ | _to fill_ |

The claim is the count ("_k of n_, _m_ missed"), never "100 %". Any miss on test: ADR 0015's
revisit trigger (data and D8's rule before any wiring).

## Other mandatory rules, route-level accuracy, macro-F1 (test)
| Mode | fail-closed rows (per business) | other mandatory rules (`k of n`, of which j fail-closed) | route-level accuracy | intent macro-F1 (bootstrap interval) |
|---|---|---|---|---|
| classifier | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| LLM-only | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| hybrid | _to fill_ | _to fill_ | _to fill_ | _to fill_ |

Target 0.90 macro-F1: _(copy "not shown at 95 % confidence" or the interval; "macro-F1 0.90 not
shown at 95 % confidence" is an acceptable, honest result)_

## Per business and language group (test)
_(copy the table from the hybrid's summary; "not measured" cells stay "not measured")_

## The comparison against LLM-only (test, paired)
- Classifier vs LLM-only: macro-F1 difference _(interval)_: _(copy the verdict; "no difference
  detected at n = …" when it spans 0)_; discordant rows _(ids, safety rows apart)_
- Hybrid vs LLM-only: _(same)_
- `classifier_kept` rows (the LLM would have loosened the route): _(ids)_

## Calibration (test)
- Reliability table, raw and calibrated: _(copy the table)_
- ECE at n = _(n)_: raw _(value)_, calibrated _(value)_; biased upward at small n

## Cost and latency (on the eval mix, not a traffic estimate)
- Classifier: embedding per 1,000 messages _(value)_; marginal when reusing retrieval's vector
  $0 (a projection); latency (local: shesha's machine and network) _(median, p95 or "not shown",
  n)_; scoring alone _(value)_
- LLM-only: per 1,000 messages _(value)_; latency _(value)_
- Hybrid (composed from replayed calls): per 1,000 messages _(value)_; fallback share _(x of n,
  per intent)_; latency _(value)_

## Spend against D7 ($2.00)
_(the "M4 spend so far" line of the last live run, and every live folder with its figure; each run
≤ $1.00; all committed on `m4/close-out`)_

## Dev (tuning)
_(dev figures only here, labelled as tuning: T, τ, σ came from dev)_

## What did not help, and what I would change
_(including anything tried and dropped, with its numbers)_

## Not measured
Telugu; e-commerce; crisis and self-harm messages (D5 (a)); multi-turn context; channel ("not
applicable offline"); fallback injection resistance live _(unless steering rows exist)_; real
traffic mix.
