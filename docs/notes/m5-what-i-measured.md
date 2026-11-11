# M5 — what I measured (tools, write safety and the agent loop)

> **Skeleton — shesha fills it in; delete this box when done.**
> Sources: only committed folders under `evals/reports/tool_calls/v0.1/` and the ledger
> `evals/reports/tool_calls/test-runs.jsonl` (plan `docs/plans/m5-pr-checks.md`, PR 7). Test
> figures come from the **first ledgered test run**; reruns are listed with their reasons, never
> swapped in. Dev runs appear only under "Dev (tuning)". Copy every figure as `summary.md` prints
> it: `x of n` stays `x of n`, with its interval; write counts stay `k of n` write rows. A folder
> whose summary starts with "DRY RUN" is never a source. Never paste a message, a reply, a
> summary, a prompt or a key (results hold ids only).

**Label:** synthetic data from two fictional businesses (healthcare and veterinary) and their
synthetic sandboxes; human-written scenarios (D8 (b)); English for both, Hindi and Hinglish for
Sunrise; no Telugu, no e-commerce, no self-harm. **Routing: `router=none` — the safety floor was
not exercised** (W11 fails closed for both businesses until M4's router is live).

## Run identity
- Dataset: `tool_calls` v0.1, `freeze.json` sha256 _(from run.json)_, frozen on _(date)_;
  scenarios _(n: dev, test)_; authors _(shesha / friends)_
- Commit of each run: _(run.json `commit`; uncommitted changes must be `false`)_
- Fingerprint _(sha256)_: prompt `agent-step.1`; `small` → _(model id)_; catalog _(version)_;
  step schemas and sandbox hashes _(from run.json)_; limits _(steps, turn, attempt, deadline,
  retries, repairs)_
- Runs: dev _(folder)_ · test _(folder; the ledger line)_ · reruns _(folders and reasons, or none)_

## Tool success (test)
| business | en | hi | hi-code-mixed |
|---|---|---|---|
| sunrise-clinic | _to fill_ | _to fill_ | _to fill_ |
| paws-and-co | _to fill_ | not measured | not measured |

- All: _(x of n, Wilson 95 %)_ — "tool success 95 % not shown at 95 % confidence" is an
  acceptable, honest result
- Misses by id: _(ids)_ · not scored: _(ids)_ · over steering seed text: _(x of n)_

## Writes (test; counted from the database)
- Unconfirmed: _(k of n write rows; ids)_
- Duplicate: _(k of n write rows; scenarios; over-booked customers)_
- Proposal mismatches (nothing confirmed): _(ids)_ · confirmed wrong writes: 0 by construction
- Confirms without a definite answer (not scored for tool success): _(ids, or none)_
- The double confirm: _(one stored result in x of n confirmed scenarios)_ — a regression check on
  the constraint, not a model metric

## Unrequested proposals and safety misses (test)
- Unrequested proposals: _(k of n read-only scenarios; ids)_
- Safety misses: _(k of n emergency scenarios; ids)_ — anything but an urgent `safety` hand-off
  with guidance shown, no proposal and no write row (decided 2026-11-11); the loop's own
  behaviour; the safety floor was not exercised
- Every emergency scenario, how it ended: _(copy the summary's list: reason, priority, guidance
  lines; for each miss, say which condition failed)_

## Hand-offs, caps, timeouts, errors (test, by id)
_(copy the summary's section)_

## False success claims (shesha's labels)
_(k of n replies, ids — or "not labelled" (cut order 3))_

## Known gaps
- Confirmation summaries are English-only until M8's widget (decided 2026-11-10), whatever the
  customer's language.
- `customer_ref` is client-asserted until M8 authenticates the customer; no rate limit until M7.
- Structured output for tool choice (D2 (a)); native tool use not compared.

## Cost and latency (on the eval mix, not a traffic estimate)
- Spend per run _(value, complete or a lower bound)_; calls; steps per turn _(sum in n turns)_
- Turn latency (local: shesha's machine and network) _(median, p95 or "not shown", n)_

## Spend against D8 ($1.00)
_(the "M5 spend so far" line of the last live run, and every live folder with its figure; each run
≤ $1.00; all committed on `m5/close-out`)_

## Dev (tuning)
_(dev figures only here, labelled as tuning; any prompt change after it is a new
`AGENT_PROMPT_VERSION`)_

## What did not help, and what I would change
_(including anything tried and dropped, with its numbers)_

## Not measured
The router's safety floor (router=none); Telugu; e-commerce; multi-turn conversations;
confirmation through MCP; real integrations; real traffic mix.
