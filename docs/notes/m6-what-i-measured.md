# M6 — what I measured (document extraction)

> **Skeleton — shesha fills it in; delete this box when done.**
> Sources: only committed folders under `evals/reports/doc_extraction/v0.1/` and the ledger
> `evals/reports/doc_extraction/test-runs.jsonl` (plan `docs/plans/m6-pr-checks.md`, PR 5). Test
> figures come from the **first ledgered test run**; reruns are listed with their reasons, never
> swapped in. Dev runs appear only under "Dev (tuning)". Copy every figure as `summary.md` prints
> it: `x of n` stays `x of n`, with its interval, and every count says "n scored, m not scored".
> A folder whose summary starts with "DRY RUN" is never a source. Never paste a value, a quote, a
> line of a document, a prompt or a key: `values.jsonl` is for reading misses, not for the note.

**Label:** typed English PDFs only, synthetic, written by shesha (and friends) for two fictional
businesses (a clinic's referral letters, a veterinary clinic's vaccination records); no OCR (D1
(a): pages with an image go to review), no real layouts, no scans. Levels are ordinal checks in
code, never calibrated probabilities.

## Run identity
- Dataset: `doc_extraction` v0.1, `freeze.json` sha256 _(from run.json)_, frozen on _(date)_;
  documents _(n: dev, test; per business)_; authors _(shesha / friends)_
- Commit of each run: _(run.json `commit`; uncommitted changes must be `false`)_
- Fingerprint _(sha256)_: prompt `extract-fields.1`; rules _(EXTRACTION_RULES_VERSION)_;
  `small` → _(model id)_; catalog _(version)_; template and business-file hashes _(from
  run.json; the freeze does not pin them)_; limits _(bytes, pages, characters, attempt,
  deadline, retries, repairs)_; dataset and configuration files at the commit _(yes)_
- Runs: dev _(folders, each with its versions)_ · test _(folder; the ledger line)_ · reruns
  _(folders and reasons, or none)_

## Silent errors (test; the headline)
| | fields | documents with at least one (Wilson 95 %) |
|---|---|---|
| all | _to fill_ | _to fill_ |
| sunrise-clinic | _to fill_ | _to fill_ |
| paws-and-co | _to fill_ | _to fill_ |

- By id: _(document/field)_ — for each, what happened (from `values.jsonl`, in words, no value)
- "0 of 12" still allows about 24 % (0 of 6: about 39 %): the claim is the count

## Field accuracy (test)
- All _(k of n, bootstrap over documents)_ · per business _(k of n each)_
- By level, as routed (before "no quote, no value" dropped a guess; each proposal against its
  gold): high _(k of n)_ · medium _(k of n)_ · low _(k of n)_ · no value proposed _(k of n)_
- Accepted precision _(k of n)_ · per field _(copy the table)_

## Review (test)
- Review rate _(k of n fields, bootstrap)_ — the staff work; by tag _(k of n each; every v0.1
  document carries a stress tag, so this reads high for everyday documents)_
- Review recall _(k of n wrong-or-missing fields)_; misses by id _(must equal the silent errors)_
- Review precision _(wrong of n reviewed with a value)_; without a value _(n, by reason, how many
  with a null gold)_

## Straight-through documents (test)
_(x of n, Wilson; per business; per tag, stress documents only)_

## Also by id (test)
_(copy the summary's section: failures by reason, truncations, `routing_invariant` and
`internal_error` fields, withheld and unverified values, signal errors, `injection` documents with
an accepted value not in gold)_

## Latency and cost per document (on the eval mix, not a traffic estimate)
- Latency (local: shesha's machine and network) _(n, median, max; no p95 below 20)_
- Tokens _(input, output: n, median, max)_ · cost per document _(n, median, max; complete or a
  lower bound)_

## Spend against D8 ($1.00)
_(the "M6 spend so far" line of the last live run, and every live folder with its figure; each run
≤ $1.00; all committed on `m6/close-out`)_

## Dev (tuning)
_(dev figures only here, labelled as tuning; a prompt change is a new `EXTRACT_PROMPT_VERSION`, a
rule change a new `EXTRACTION_RULES_VERSION`; truncations counted, not fixed: M3's window)_

## What did not help, and what I would change
_(including anything tried and dropped, with its numbers)_

## Known gaps and not measured
- OCR and scans (D1 (a)); image uploads; DOCX; Hindi and Telugu documents; real layouts.
- A single hidden copy of a value (pypdf reads invisible text; only a conflicting copy is caught).
- Planted text that is really in the document can be quoted (the `injection` documents count it).
- Gold corrections after the freeze: _(none, or v0.2 with the corrected ids)_
