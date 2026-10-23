# M2 — what I measured (retrieval and cited answers, eval v0)

> **Skeleton — shesha fills it in; delete this box when done.**
> Source: exactly one committed live **test** run folder,
> `evals/reports/support_qa/v0.1/<UTC start>-live-test/`, whose `summary.md` status line reads
> `Complete live held-out test run: may feed the note (config <fingerprint>)` and whose "Not for
> the note" row reads `nothing: it may feed it` (runbook `docs/learning/m2-eval-run.md` §4; ADR 0013,
> decision 5). Exit 0 alone is not enough. Dev runs appear only under "Dev runs (tuning)".
> Copy every figure as `summary.md` prints it: `x of n` stays `x of n`, each with its interval, no
> re-rounding, no percentage where the summary prints none. No figure that is not in that folder,
> its `run.json` or a provider dashboard. Never paste question text, answers, prompts, request
> bodies or a key (questions stay in the dataset; answers in the run's `answers.jsonl`).

**Label:** _(copy the "Synthetic data: …" sentence under the status line of `summary.md`, exactly)_

## Run identity
Copy from `summary.md`, table "Run" (row names in brackets).
- Test run folder: _to fill from the live test run (path, and the PR that committed it)_
- Status line: _to fill from the live test run_
- Dataset and version: _(row "Dataset": must say stage frozen, files as committed `yes`, synthetic `yes`)_
- `freeze.json` sha256: _(row "Dataset")_
- Commit: _(row "Commit": must say uncommitted changes `no`)_
- Embedding model: _(row "Embeddings": configured and answered)_
- Answer model: _(row "Answer model": alias → model id, answered, catalog version, prompt version)_
- Config: _(row "Config": chunks, search k, metric k, `rag_min_score`, weak evidence, retries, repairs)_
- Config fingerprint: _(row "Config fingerprint")_
- Eval businesses: _(row "Eval businesses")_
- Intervals: _(row "Intervals": method, resamples, seed, the n from which a value is shown)_
- Test-run ledger: _(row "Test-run ledger"; then every entry of `evals/reports/support_qa/test-runs.jsonl`
  for this frozen version, by run id, rerun reason if any)_
- Started / ended (UTC): _(row "Started / ended")_

## Provider checks before the run (dated)
- Voyage (ADR 0008; plan PR 4): Hindi / Telugu support _(what the docs say, date)_ · region and
  data residency _(date)_ · spend limit _(exists? set?, date)_ · limits without a payment method
  _(date)_ · training opt-out done on _(date)_ · model id and price re-checked against
  `embeddings/models.toml` on _(date)_
- Anthropic (ADR 0007): workspace spend limit set _(date)_ · default inference geo `global`
  _(date)_ · prices re-checked against `gateway/models.toml` on _(date)_
- First live call (plan PR 4): do Voyage's statuses match `embeddings/voyage.py`'s table? a
  request-id header? reported tokens against `token_upper_bound`? _(what you saw, or "not
  observable from the records")_

## Retrieval: hit@5 and MRR@5 (held-out test split)
From "Retrieval": on the search's ranked list, before the score threshold.
Counts line: _(copy: selected · scored · not scored · unscorable at this config · not run)_

| Group | n | Unscorable | hit@5 (`x of n`, Wilson 95 %) | MRR@5 (value and bootstrap interval, or rank counts only) | First relevant rank |
|---|---|---|---|---|---|
| all | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| business sunrise-clinic | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| business paws-and-co | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| language en | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| language hi | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |
| language hi-code-mixed | _to fill_ | _to fill_ | _to fill_ | _to fill_ | _to fill_ |

Per category: _(describe the category rows as counts; they are not comparisons)_
- Conservative hit@5 (unscorable counted as misses): _(copy the line)_
- Every group in the top 5 (questions with 2+ groups): _(copy)_
- Evidence complete, `multi_hop` / `ambiguous` (not hit@k; depends on `rag_min_score`): _(copy)_
- Not scored / unscorable / evidence keys not held / not run: _(copy the ids)_
- Shared `split_group` (intervals too narrow by an unknown amount): _(copy the count)_
- MRR intervals: _(copy the "approximate at small n" note, or "bootstrap degenerate" if printed)_

## Answers and correctness (human labels)
- Labelled by shesha on: _(date)_ · labelled / not yet: _to fill from the live test run_
- Expected outcome against actual outcome: _(copy the "Answers" table)_
- Why answers were not shown: _(copy)_ · answer-step errors: _(copy the ids)_
- Safety hand-offs (counts only, never a rate; M2 has no safety check, ADR 0011): _(copy)_

| Group | Correct (`x of n`, Wilson 95 %) |
|---|---|
| all | _to fill from the live test run_ |
| business sunrise-clinic | _to fill_ |
| business paws-and-co | _to fill_ |
| language en | _to fill_ |
| language hi | _to fill_ |
| language hi-code-mixed | _to fill_ |

## Against the business-context §6 targets
The rule (ADR 0013, decision 10; fixed before the run): a target is **met** when the point
estimate reaches it, always quoted with its `x of n` and Wilson interval, and **"not shown at 95 %
confidence"** when the interval's lower end is below it. No verdict below `RATE_MIN_N` (row
"Intervals"), for a dry, partial or failed run, at another metric k, or while answers are
unlabelled. A release needs every business to meet it. Quote a verdict only with its measurement.

| Metric | Group | §6 target | Measured (`x of n`, interval) | Verdict (as `summary.md` prints it) |
|---|---|---|---|---|
| hit@5 | all | _(copy)_ | _to fill from the live test run_ | _to fill_ |
| hit@5 | business sunrise-clinic | _(copy)_ | _to fill_ | _to fill_ |
| hit@5 | business paws-and-co | _(copy)_ | _to fill_ | _to fill_ |
| correctness | all | _(copy)_ | _to fill_ | _to fill_ |
| correctness | business sunrise-clinic | _(copy)_ | _to fill_ | _to fill_ |
| correctness | business paws-and-co | _(copy)_ | _to fill_ | _to fill_ |

Not measured in M2 (keep the sentence): faithfulness (human faithfulness labels from M3's PR 7; LLM judges deferred, M3 D4 (b)), safety-escalation recall
(router M4, gated from M7). Cross-business isolation in this run: _(the run's leak check: aborted
or not; the canary suite runs in CI)_

## Dev runs (tuning, not results)
Numbers chosen on the dev split; none of them is a held-out result.

| Run folder | Config fingerprint | What changed | hit@5 (`x of n`) | Conservative hit@5 | Status line |
|---|---|---|---|---|---|
| _to fill from each committed dev run_ | | | | | |

Chunk configs compared over the questions scorable under both only: _(ids used, or "not compared")_

## Cost
- Before each run, the bound (runbook §3's formula, printed with the plan; not a measurement):
  _(copy "whole plan" and "largest single call" per run)_ · cap typed: _(per run)_
- Spent, from `summary.md` "Cost (list prices)" and `run.json`: dev run(s) _to fill_, test run
  _to fill_; say if a lower bound (an incomplete call cost). Voyage's free allowance is not counted.
- Against the dashboards (Voyage usage, Anthropic Console) for the runs' time windows: _(optional)_

## Rules that fired, stops and open items
- Stops: _(row "Status" of each run; a stop by the last question keeps a run out of the note)_
- Leak or audience aborts: _(none, or the folder and zara's finding)_
- Threshold (`rag_min_score`, ADR 0011): _(the value used and why, from the dev runs)_

## Limitations
_(Keep each one that applies; add any you noticed.)_
- Synthetic, human-written questions about two fictional businesses; only the friends' questions
  were written without seeing the documents.
- Small n: per-language and per-category rows are counts; questions sharing a `split_group` make
  the intervals too narrow.
- One embedding model, one answer model, one chunk config; v0.1 cannot meaningfully tune the chunk
  size (ADR 0012).
- Correctness labelled by one person (shesha), who also wrote the questions and knows the system.
- Valid for this commit, freeze hash, catalog and prompt version, and the prices on the check date
  only.

## What surprised me
_(shesha's own words)_

## What I'd change
_(the next measurement, dataset v0.2, what M3 should fix first, and what you would say about these
numbers in an interview)_
