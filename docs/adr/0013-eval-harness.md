# ADR 0013 — The eval harness: run folders, test-split discipline, eval businesses

- **Status:** accepted · 2026-10-22 (shesha: every recommended option, one PR) · drafted by
  noah-evals in M2 PR 8 (`m2/eval-harness`). Builds on ADR 0006/0007 (live-run cap, records), 0011
  (`AnswerService`) and 0012 (dataset v0.1, frozen splits).

**Context.** PR 9's live run must turn eval v0 into hit@5, MRR@5 and correctness with every number
traceable to its dataset version, commit and config (rule 3), at a capped cost, with the held-out
test split run once per reported config (ADR 0012, 8), and with each scored run doubling as a leak
check. Three things are new: embedding calls are paid too; re-ingesting a stored version does not
re-chunk it; and nothing yet records which configs ran on test.

**Decision.**
1. **A package of its own,** `vectapilot_api/evalrun/`, run as `python -m vectapilot_api.evalrun`
   (no new console script: `pyproject.toml` unchanged). `evalset` stays pure (no model, network or
   database). The harness calls the product (`KbService`, `AnswerService`) and shesha's H1/H2;
   it re-implements neither.
2. **A hit's business from its document row,** read with its status and audience on the search's
   own connection inside the same business transaction (a wrapper around H1 through
   `SearchService`'s existing `search` seam), and checked **before the hits go back to the answer
   step**, so a later cap refusal, Ctrl-C or error cannot hide it; `SearchHit` (H1's contract) is
   unchanged. A hit of another business, or one not visible in the question's, aborts the run
   (`CROSS-BUSINESS LEAK`, exit 4); so does a hit a customer may never see (not `approved`, or an
   audience outside `VISIBLE_AUDIENCES["customer"]`: `AUDIENCE LEAK`, exit 4, business-context
   §3.4). Considered: a `business_id` on `SearchHit` (changes H1's decided contract); a second
   query in a separate transaction (another snapshot); checking only after the answer step (a
   stop after the search skipped the check).
3. **Eval businesses per (business, embedding model, chunk config):**
   `<business>-ev-<10 hex of sha256>`, registered by the owner, ingested through `KbService`, never
   deleted unless `--fresh` (only ids of the derived form, checked at the delete itself, and never
   in production). The stored chunks are
   checked against the corpus at the config before any question. Considered: the dataset's own ids
   plus a delete before every new config (a delete as the default path); a chunk-config column on
   documents (a migration, and still one config at a time).
4. **One cap for embeddings and the answer model,** M1's reservation pattern per paid request
   (M1's formula and its `--max-usd` ceiling of $1.00, typed again at a terminal), embedding
   prices in `embeddings/models.toml` from ADR 0008's Facts. The API's `api_allow_paid_llm` does
   not apply. `--live` is refused unless the dataset manifest and every corpus manifest say
   `synthetic: true` (a guard for the first real pilot: a live folder commits the answers).
5. **Test-split discipline:** live runs need a frozen dataset; dry runs on an unfrozen one run
   every question as `unassigned`; dry runs are refused on test; a test
   run needs a complete live dev run with the same config fingerprint committed in `HEAD`, and the
   ledger `evals/reports/<dataset>/test-runs.jsonl` committed as it is on disk (so a discarded
   entry cannot make a rerun look like a first run); it is appended to the ledger before its first
   paid call, and run.json records how many entries came before it (this config, this frozen
   version). A second test run of one fingerprint needs `--test-rerun-reason`, recorded in the
   ledger and named in the status line ("held-out test RERUN"). The ledger is committed with the
   test folder, and the note cites every ledger entry of the frozen version. Considered: no ledger
   (unauditable); refusing every rerun (a cap stop would burn a config forever). Only a complete,
   labelled, live test run feeds the "what I measured" note; dev runs appear next to it, labelled
   as tuning.
6. **Run folders:** run.json, results.jsonl (no text), answers.jsonl (the model's answers, for
   labelling: synthetic data, committed with a live run; questions by id), labels.jsonl
   (template), the three metadata-only record logs, and a summary recomputed from those files
   through H2 (stored
   per-question values must match; H2's two means must agree with its per-question values; every
   first relevant rank lies in 1..k). The summary's "synthetic" sentence is built from the two
   manifest flags ("NOT synthetic" when either is false). Question text stays in the dataset
   only. A failure while writing the folder after the calls says "the run FAILED, no summary
   written", where the folder is and what was spent, never "nothing was sent".
7. **Intervals:** 95 % Wilson for every `x of n`; a percentile bootstrap over questions for MRR
   (2,000 resamples, seed 20261022, indexes from `random.Random(seed).random()` only); an MRR
   value and its interval only from n = 10 (below it, the first-relevant-rank counts only); a
   percentage from n = 10, on every split; safety as counts only. Questions sharing a
   `split_group` are counted, since the intervals assume independence. **Limits of the
   bootstrap:** at small n it covers less than 95 %, and it never reaches past the observed values
   (with every RR equal it has zero width, while Wilson for the same 10 of 10 hits is 72.3 % to
   100 %); summary.md says "approximate at small n (percentile bootstrap undercovers)", and a
   zero-width interval is printed as degenerate, pointing to the hit@k Wilson interval, never as
   a 95 % interval.
8. **Unscorable is decided per group** (any key finds a group, ADR 0012): a question with a group
   no stored customer-visible chunk holds any key of is unscorable at that config, outside every
   row's n and shown next to it per row. A single key no chunk holds, in a group another key still
   finds, is kept per result (`unheld_keys`, indexes) and counted in the summary. A conservative
   hit@k counts every unscorable question as a miss (`hit.x` of `n + unscorable`, per row, shown
   for "all" and each business), and two chunk configs are compared on dev only, over the
   questions scorable under both. Considered: per key (a question with any unheld key
   unscorable: drops findable questions).
9. **Stops and exit codes, as M1.** A stop rule tripped by the last question leaves no question
   unrun: status `complete`, exit 0, the stop in run.json and a "not for the note" reason
   ("stopped by its last question"). A stop with questions left is `partial`, exit 3. A leak
   always exits 4, also when writing the folder fails afterwards.
10. **When a §6 target is "met"** (hit@5 and correctness ≥ 90 %): when the point estimate reaches
    it, always shown with its Wilson interval, and "not shown at 95 % confidence" when the
    interval's lower end is below it (v0.1's test split cannot show more: 12 of 12 gives 75.8 %
    to 100 %). No verdict below n = 10 on any split, for a dry, partial or failed run, or while
    answers are unlabelled; per business too. summary.md has a "Targets" table.
11. **The test stand-ins for H2** stay on the team's side of the [HAND-WRITE] line: per-question
    values and both means are hand-written literals, a bootstrap resample gets a stand-in
    statistic that is deliberately not a mean (position-weighted prepared values, so the seed,
    the resampling unit and the resample count all matter), and the report cross-checks H2's
    means against H2's own per-question values.

**Decided 2026-10-22 (shesha), every recommended option:** one derived eval business per
(business, embedding model, chunk config); answers.jsonl committed, questions by id; the test-split
discipline of decision 5; dry runs on an unfrozen dataset allowed (`unassigned`), live runs need
the freeze; Wilson 95 % everywhere and the seeded bootstrap from n = 10 with its small-n note;
M1's formula and $1.00 ceiling; only a complete, labelled, live test run feeds the note;
unscorable per group with the conservative line and the dev-only comparison rule; "met" as in
decision 10, with `RATE_MIN_N` on every split; stops as in decision 9, and `--live` refused for
data not marked synthetic; the H2 stand-ins of decision 11; this ADR accepted, and PR 8 kept as
one PR.

**Consequences.** One command gives a reproducible, auditable folder; every eval config costs one
ingest of its own (bounded before the run by the formula in `docs/learning/m2-eval-run.md` §3 and
printed with the plan; not measured until PR 9) and leaves its namespace in the local database; a
leak can never be averaged away. The price: more code to read; namespaces accumulate locally until
`--fresh` or a reset; the test-run ledger is honest only if test runs go through the harness and
the ledger is committed with each test folder; comparing two chunk configs over the questions
scorable under both is done from the two folders' results.jsonl (no command does it yet).
