# gateway_smoke v0.1

**Synthetic — written by Claude for M1; shesha reviews the hi/te and code-mixed prompts before the
live run.** Nothing here is real: the three businesses are the fictional examples of
[`docs/business-context.md` §7](../../../../docs/business-context.md), and no prompt names a real
person, clinic, phone number or health record. Medical and veterinary prompts stay generic
(booking, opening hours, fees, and how an assistant should word an escalation). No prompt mentions
self-harm or poisoning (ADR 0007, Data handling).

| | |
|---|---|
| Version | v0.1, created 2026-10-15 |
| Used by | M1 step 8: the LLM gateway measurement run (ADR 0007, "Measurement") |
| Cases | 24 distinct prompts: 12 on `small`, 12 on `large`; 13 plain, 11 structured (five non-forced on each alias, plus one forced repair on `small`) |
| Businesses | `sunrise-clinic` (en, hi) · `paws-and-co` (en, te) · `northwind-shop` (en): each only in the languages §7 lists for it |
| Languages | en 14 · hi 3 + 2 Hinglish · te 3 + 2 Tenglish (code-mixed cases are in Latin script); every non-English group is on both aliases (see "Comparing groups") |
| Review | `manifest.json` → `"review": "pending"` until shesha has reviewed the hi, te and code-mixed prompts |

What it measures is the gateway, not answer quality: latency, timeouts, refusals, truncation,
first-try validity of structured output, the one repair, tokens and cost. There are no expected
answers. The two emergency-wording cases (`sunrise-en-emergency-wording`,
`paws-en-emergency-wording`) are gateway probes like the rest: their outcomes are never evidence for
business-context §6's 100 % safety-escalation recall gate, which M7's red-team suite measures.

Two cases ask for a long non-English answer (`sunrise-hi-long-answer` on `large`,
`paws-te-long-answer` on `small`), to check the aliases' `max_output_tokens`. Under the rule fixed
before the live run, a truncation means: raise that alias's cap (new `catalog_version`) and re-run.
On `paws-te-long-answer` a truncation is likely (about 300 Telugu words against `small`'s 1024
output tokens; how many tokens Telugu needs per word is exactly what the run measures). That is a
useful result, not a failure: the PR 9 plan should allow for a second run after the cap is raised.

A run is partial only when it did not make every planned call: the cost cap, an account error
(`spend_limit`, `billing`, `auth_failed`), a call billed beyond its worst-case bound, an unexpected
error or an interrupt stopped it early (decided by shesha, 2026-10-15). A cost-incomplete call
(every attempt timeout is one) does not make a run partial: the summary reports "k/n
cost-incomplete" and labels the cost a lower bound. A partial run cannot feed the "what I measured"
note; neither can a dry run, nor a run that fails the record-count check (it gets no summary).

## Files

- `manifest.json` — one JSON object: `dataset`, `version`, `created`, `synthetic` (always `true`),
  `authored_by`, `review` (`pending` | `done`), and `businesses` (each business's languages).
- `cases.jsonl` — one case per line (JSON Lines, UTF-8). Loaded and checked by
  `vectapilot_api.measure.dataset` (`load_manifest`, `load_cases`, `check_dataset`).

## Case schema

| Field | Type | Meaning |
|---|---|---|
| `id` | slug | Unique; never reused for a different prompt in a later version |
| `business_id` | business slug | A business in `manifest.json` |
| `alias` | `small` \| `large` | The catalog alias to call |
| `language` | `en` \| `hi` \| `te` | One of the business's languages |
| `code_mixed` | bool, default `false` | Hindi or Telugu mixed with English (`hi` or `te` only) |
| `kind` | `plain` \| `structured` | `complete()` or `complete_structured()` |
| `schema_name` | string \| null | Structured cases only: a key of `vectapilot_api.measure.schemas.SCHEMAS` |
| `forced_repair` | bool, default `false` | The one structured case whose first answer is expected to fail validation |
| `prompt` | string | Sent as the only user message, with no system prompt |
| `fake_reply` | string | The fake provider's answer, **for dry runs only**: valid JSON for the schema on a structured case (on the forced repair, an answer that breaks the hidden rule) |
| `fake_repair_reply` | string | Forced repair only: the fake's valid answer to the repair request |

The summary groups languages as `en`, `hi`, `hi-code-mixed`, `te`, `te-code-mixed`, each per
alias.

## Comparing groups

An alias is one model, and the models differ in price, speed and perhaps tokenizer. So the summary
reports every language and every business per alias (one model per row), and languages are
compared only within an alias. The cases are spread so that language is not tied to model:

| Group | `small` | `large` |
|---|---|---|
| `hi` | `sunrise-hi-hours` (plain) | `sunrise-hi-booking` (structured), `sunrise-hi-long-answer` (plain) |
| `te` | `paws-te-long-answer` (plain) | `paws-te-hours` (plain), `paws-te-booking` (structured) |
| `hi-code-mixed` | `sunrise-hinglish-cancel-intent` (structured) | `sunrise-hinglish-fees` (plain) |
| `te-code-mixed` | `paws-tenglish-grooming` (plain) | `paws-tenglish-refund-intent` (structured) |

Limits of v0.1, to keep in mind when reading the per-language rows (n is 1 or 2 per row, so every
figure there is descriptive only):

- Each code-mixed group has one plain case, Hinglish on `large` and Tenglish on `small`, so their
  prompt tokens per character cannot be compared within one alias. A later version can add a
  plain case per code-mixed group on the other alias.
- The plain `hi` case on `small` is short and the plain `te` case there is the long answer; on
  `large` it is the other way round.
- Language and business are tied: each business has only the languages §7 lists for it.

## Structured cases and the forced repair

Ten structured cases (five per alias) use schemas whose every rule is in the JSON Schema the
gateway quotes: they measure first-try validity (k/n, Maya's rule). One case,
`northwind-en-bill-forced-repair` (`BillCheck`), is the forced repair: its after-validator requires
`total_inr` to equal the sum of quantity × unit price, which JSON Schema cannot express and which no
docstring or description mentions. The prompt asks for the bill's printed total, which does not
match the items, so the first answer is *expected* (not guaranteed) to fail and the gateway sends
its one repair. The summary reports that case on its own: "repair worked", "repair failed", or "not
exercised" when the first answer happened to be valid.

The prompt asks for the total "exactly as printed", which pits the customer's instruction against
the hidden rule, and the repair's feedback names only `total_inr: value_error`. So "repair failed"
is a plausible, legitimate result, not a model defect.

The result also settles an open question from the step 7 review (`docs/plans/m1-pr-checks.md`,
PR 7): a repair request ends with two consecutive `user` turns, which the API is documented to
merge. "repair worked" or "repair failed" confirms that the API accepts them; "repair did not
finish" with outcome `invalid_request` refutes it; "not exercised" leaves the question open for
PR 9. Since 2026-10-15 an `invalid_request` also stops the run at that case (the outcome is still
recorded, so the question is still answered).

## Adding a version

Never edit a version that a committed summary used: its numbers must stay reproducible.

1. Copy this folder to `v0.2/` (or `v1.0/` for an incompatible schema change).
2. Change the cases; keep ids stable for unchanged prompts and use new ids for changed ones.
3. Update `manifest.json` (`version`, `created`, and `review` back to `pending` if any hi, te or
   code-mixed prompt changed) and this README (counts, what changed and why).
4. Run the dataset test (`uv run pytest tests/measure/test_dataset.py` in `apps/api`), pointed at
   the new version.
5. Never tune prompts against a run's results to make a number look better; record why each change
   was made.
