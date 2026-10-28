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
   (with every RR equal it has zero width, while Wilson for the same 10 of 10 hits is 72.2 % to
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
scorable under both is done from the two folders' results.jsonl (no command does it yet). Two runs
of one chunk config are compared by `evalrun compare <run A> <run B>`, paired by question id
(amended 2026-10-25, below); it refuses two chunk configs.

**Amended 2026-10-23 (shesha, M3 plan D3, D4, D14, D15; `docs/plans/m3-pr-checks.md`).** Built in
M3 PR 5–7: `compare` (PR 6, 2026-10-25), the retrieval block, the list checks and the spend ceiling
(PR 5, 2026-10-27), and the faithfulness labels (PR 7, 2026-10-28), each in its amendment below.
- **The measurement window** (D3 (a)), from M2's first live dev run to M3's last live answer run:
  - No change to `gateway/models.toml`, `PROMPT_VERSION` or the gateway's output path. A catalog
    or prompt bump changes every fingerprint and refuses the test run, and the output mode is not
    hashed at all.
  - M2's test run runs on M2's dev code: the branch that adds only the dev folder on top of the
    dev run's commit.
  - No `make reset-db`, volume prune or other deletion of the eval namespaces. M3's hybrid runs
    reuse M2's chunks and embeddings, and a re-ingest is a paid re-embed.
  - Tripwire: a fingerprint pin test that reads the live catalog and prompt versions (PR 5).
- **The retrieval block** (PR 5). `ConfigInfo` gains an optional `retrieval` block (mode, candidate
  k, RRF k, language-map version), omitted in vector mode so M2's fingerprints do not change.
  `--retrieval-mode` works like `--min-score`.
- **Every list is checked** (PR 5). Decision 2's business, status and audience check runs on each
  leg and on the fused list before the answer step: a leak that fusion drops is still a leak.
  `results.jsonl` gains optional component lists per question (chunk ids, ranks, matched groups;
  no text).
- **`compare`** (PR 6). `evalrun compare <run A> <run B>` takes two runs of one dataset, version,
  freeze, split, embedding model and chunk config. It reports discordant pairs by question id over
  the questions scorable under both, as counts and intervals only. Test folders come only from the
  ledger and are labelled held-out. Comparing two chunk configs stays manual (D14), so the
  Consequences' last sentence stays true.
- **Faithfulness labels** (PR 7; the file format is a proposal). A human `faithful | unfaithful`
  label per shown answer, kept apart from correctness. `summarize` reports `x of n` with Wilson,
  and a §6 ≥ 95 % verdict only under decision 10.
- **No LLM judge in M3** (D4 (b)). Both judges wait for the first dataset with at least 30 human
  labels on distinct shown answers (v0.2 or a pilot). M3 reports human labels only, a deviation
  from M2's decision 4 and from §6's correctness method.
- **Spend across M3's runs** (D15 (iii)). Every run keeps decision 4's $1.00 cap, and all of M3's
  paid runs together stay within $5.00. PR 5 sums `cost.total_usd` over M3's committed live
  folders, shows it before the cap prompt, and refuses a cap above what is left (details: the
  plan, PR 5). Like the typed cap, this only catches mistakes. The providers' own limits stay the
  real cap across runs (ADR 0007; Voyage's spend limit is unverified, ADR 0008).

**Amended 2026-10-25 (M3 PR 6, noah; in review, ananya reviews; shesha's decisions of 2026-10-25
applied 2026-10-26).** `compare` is built as the 2026-10-23 amendment says (`evalrun/compare.py`;
plan, PR 6, "[decided 2026-10-25] (shesha)"):
- It reads run.json and results.jsonl only, prints Markdown and writes nothing: redirect stdout
  to keep the report (decided).
- Refused, naming the field: another dataset, version, freeze, split or embedding model; two
  chunk configs, with a pointer to decision 8 and D14. Also refused: a run.json whose
  `config_fingerprint` is not the hash of its own fields (decided), two question sets, a search k
  below 5, stored values H2 does not give again, and the same folder twice.
- **A scoring rule that differs between the runs is refused** (D3; decided: refuse, not warn): a
  question scored in both runs with a chunk whose matched groups differ, or a question scored in
  one run and unscorable in the other. One freeze and chunk config store the same chunks, so
  either means the code deciding relevant or unscorable changed, and the fingerprint does not
  cover that code. The message is fixed: it names the cause and says to re-run both runs on one
  commit. A question not run in one run stays benign.
- Printed: both commits, every other fingerprint field that differs (a block present in one run
  only, such as PR 5's `retrieval`, is one field, decided; an absent `retrieval` reads "absent:
  vector mode"), and a warning when more than one does. Two re-ingest warnings (D3): the later
  run's ingest wrote chunks into a namespace the earlier run used; and a document has another id
  in each run for the same eval business, source and version (decided; it also sees a re-ingest
  made by a third run).
- Over the questions scorable under both: hit@5 and MRR@5 per run through H2, with decision 7's
  intervals computed over the paired n at k = 5 (so they can differ from each run's own
  summary.md), for "all", each business and each language; and the discordant pairs at hit@5 by
  question id, per business and per language. Counts and intervals only.
- Left out, as decision 8 asks (outside every row's n, shown next to it per row): the groups
  come from every selected question, so a group with nothing paired keeps an n = 0 row; so does
  every business in either run.json's `namespaces` with no selected question [proposal]. Per
  group, the questions scorable in one run only and those scorable in neither are counted. The
  questions themselves are listed by id: scorable in one run only, not scored (no evidence),
  unscorable in both, and not run in one or both.
- **A business with n = 0 gives no evidence and blocks adoption under D15 (ii)** (decided), as
  the retrieval mode is platform-wide. Its discordant row reads "n = 0: no evidence (blocks
  adoption under D15)". A language with n = 0 does not block.
- "Not a result" for a dry run, a run that is not complete, or an unfrozen dataset (decided).
  "Dev is for choosing" on dev. On test, only folders already in the ledger as committed in
  HEAD, counts only, "Held-out: reported, not used to choose" (maya's rule, decided), with each
  run's test-run ledger row and any RERUN named with its reason (decision 5).
- Not built yet: D15 (ii)'s safety half. Its reading is decided: every safety question that ran
  in both runs, whatever its retrieval status, answered under one mode but not the other, as
  counts and ids per business. Whether it lands in PR 5 or a follow-up is open.

**Amended 2026-10-27 (M3 PR 5, noah; in review, ananya and zara review).** The 2026-10-23
amendment's retrieval block, "every list is checked" and the spend ceiling are built, and so is
D15 (ii)'s safety half in `compare` (decided 2026-10-27, shesha). Plan: `docs/plans/m3-pr-checks.md`,
PR 5, "Built (2026-10-27, noah)". The items once marked [proposal] were decided by shesha on
2026-10-27, all as proposed or recommended (plan, PR 5, "[decided 2026-10-27] (shesha)").
- **The retrieval mode is the run's own:** `--retrieval-mode vector|hybrid`, like `--min-score`,
  defaults to Settings' `rag_retrieval_mode`, and the harness builds `SearchService` in that mode
  whatever Settings say. PR 4's refusal of a hybrid setting (`VECTOR_MODE_ONLY`) is removed with
  its two tests. `ingest` runs in either mode: the namespaces depend on the embedding model and
  chunk config only, so a hybrid run reuses the vector runs' namespaces and embeds no document
  once they are ingested. The plan the CLI prints checks this against HEAD (review of PR 5,
  ananya A1): "no document embedding expected" only when a complete live vector dev run committed
  in HEAD has the same `namespaces`; otherwise a WARNING with the ingest's worst case.
- **run.json's `config.retrieval`** (hybrid only): mode, `candidate_k`, `rrf_k` and
  `language_map_sha256`, the sha256 of the map's fixed canonical form (compact sorted JSON of
  `{"fallback": FALLBACK_CONFIG, "map": TEXT_SEARCH_CONFIGS}`; decided 2026-10-25). It lives in
  `evalrun/report.py`, not `ingestion/`, whose import allowlist excludes `json` (decided
  2026-10-27, shesha); the map itself stays in `ingestion/language_map.py`. A None block is
  left out of every dump (a wrap serializer, as `RagAnswerRecord`'s), and so is `m3_spend` when
  false (review of PR 5, ananya A5), so a vector run's run.json has exactly M2's keys and its
  fingerprint is M2's: a pin test computes a fixed vector config's fingerprint from the live
  `catalog_version` and `PROMPT_VERSION` and compares it with the literal computed on PR 6's
  merged code (D3's tripwire). A hybrid `--search-k` above `rag_candidate_k` is refused (the API
  refuses such Settings at startup), never recorded as `max(rag_candidate_k, search_k)`
  (decided 2026-10-27, shesha).
- **Every list is recorded and checked** before the answer step: `RecordingSearch` wraps the
  vector leg, the text leg and the fusion. Each leg's rows are read on the search's connection;
  the fused list is checked against the rows its legs read (document level: a hit whose document
  no leg returned fails closed), then at chunk level (review of PR 5, ananya A2): every fused hit
  must equal (`==`) a hit with its chunk id in one of the legs' lists, with no chunk id twice, or
  the run stops before the answer step (`FusedListMismatch`, partial). A hybrid leak line names its list ("rank 2 of the text leg"); vector lines are M2's.
  In hybrid mode `_score` scores the fused list (`AnswerRun.hits`) with H2 and checks the legs at
  `max(candidate_k, search_k)`, the fusion's input lists, RRF k and limit (review of PR 4, N2).
  `results.jsonl` gains `components` per question (each leg's chunk ids, ranks and matched
  groups; no text), absent in vector mode.
- **The recorder's own time** (N4; decided 2026-10-27, shesha: subtracted, not labelled): the recorder times its lookup
  and check inside each stage on the search's monotonic clock and writes it per question
  (`components.harness`); the summary subtracts it from the `rag_answer` stage durations and
  shows it as its own row. A difference below 0 beyond a nanosecond refuses the summary.
- **summary.md** gains, for hybrid runs: each list's rows (vector leg, text leg, fused) for "all"
  and each business at the metric k, with decision 7's intervals; the FTS-only counts; the
  headroom count (D13; harness counting over H2's hit@10 and hit@5, D5), listed by id; and
  per-stage latency through M1's `latency_summary` (p95 only at n ≥ 20), labelled "local Docker
  on an 8 GB Mac, synthetic", never compared with §6. Every run gains the safety questions by
  outcome per business (zara S7), counts only.
- **`compare`** gains a "Retrieval" row ("vector (no retrieval block)" when absent), a pointer to
  each hybrid folder's summary.md for the FTS-only and headroom counts, and D15 (ii)'s safety
  half: every safety question that ran in both runs, whatever its retrieval status, answered in
  one run but not the other, as counts and ids per business (every business of either run's
  namespaces keeps a row). An `error` outcome shows no behaviour, so it is never "not answered":
  it has its own columns (error in A only, in B only, in both) and is listed by id (review of
  PR 5, zara Z1). Two things block adoption under D15 (ii) whichever run is which (decided
  2026-10-27, shesha): a business with no safety question that ran in both runs (its n cell is
  "n = 0: no evidence (blocks adoption under D15)", as in the hit half), and a safety question
  whose outcome is `error` in either run (behaviour unknown; the business is named). The
  2026-10-25 amendment's "not built yet" no longer holds.
- **M3's spend** (D15 (iii); `evalrun/spend.py`): before the cap prompt of a live run, the plan
  prints the sum of `cost.total_usd` over the counted folders' run.json as committed in HEAD, the
  $5.00 ceiling and what is left, "lower bound" when a counted folder's answer-model cost was
  incomplete. It names the branch and commit whose HEAD it read (diego D1): HEAD is one
  branch's, so every M3 live run is made and committed on `m3/close-out`, and the plan says so
  (decided 2026-10-27, shesha). Counted (decided 2026-10-27, shesha): a committed live folder
  whose run.json has the `retrieval` block, or a vector run named with `--m3-spend` (run.json
  `m3_spend: true`, outside the fingerprint; D3's re-run); M2's runs never count. A live folder
  without run.json (a kill, a failed write, a run still going) counts at M1's $1.00 per-run
  ceiling until shesha records its real spend in that folder's `recorded-spend.json` (total as a
  decimal string, at most $1.00; whether it was M3's; who, when, and where the figure was read)
  and commits it; "left" subtracts it either way (zara Z4, decided 2026-10-27). A live run of M3
  is refused when its cap exceeds what is left (the refusal names the ceiling, the sum and the
  folders), and while one of M3's live folders on disk is not committed as it is (its run.json
  or its recorded-spend.json). Like the typed cap, this only catches mistakes (ADR 0007).

**Amended 2026-10-28 (M3 PR 7, noah; in review, zara reviews).** The 2026-10-23 amendment's
faithfulness labels are built (plan: `docs/plans/m3-pr-checks.md`, PR 7, "Built (2026-10-28,
noah)"). The items once marked [proposal] were decided 2026-10-28 by shesha, every recommendation
accepted (plan, PR 7, "[decided 2026-10-28] (shesha)"), and two changes were built with them.
- **The file** (decided 2026-10-28, shesha): `faithfulness_labels.jsonl` in the run folder, one
  line per shown answer (outcome `answered`): `question_id`, `run_id`, `label` (`faithful` |
  `unfaithful`), `labelled_by`, `labelled_on`, optional `note`
  (`evalset.questions.FaithfulnessLabel`, closed).
  `summarize` refuses, by line number and never echoing a value: a line that is not JSON, an id
  that is not a question of the run, a question whose answer was not shown (`dont_know`,
  `handoff`, `error`, `not_run`), a second line for one answer, another run's id, or an invalid
  filled line. A `label: null` line or a missing line or file leaves that answer unlabelled.
  `labels.jsonl`, its parser and `CorrectnessLabel` are unchanged, and neither file is read as the
  other.
- **Who writes it:** `evalrun faithfulness-template <run>` writes the template (ids and nulls,
  one line per shown answer) and refuses when the file exists; `run` does not write it, so a run
  folder holds the files of decision 6 (decided 2026-10-28, shesha). The labels are shesha's
  alone; no agent and no model writes or suggests one (D4 (b)), and an answer gets one label, not
  one per claim (D11).
- **The two labels:** faithful means every claim in the answer is supported by the text of the
  chunks it cites; correct means the reply does what `expected_outcome` says, and every claim
  matches an approved source (`CorrectnessLabel`). Neither stands in for the other
  (`docs/learning/m2-eval-questions.md` §5 gives an example of each without the other).
- **`label-sheet`** prints, under each shown answer, the text of every chunk it cites, rebuilt
  with the real loader and chunker at run.json's chunk config (`evalrun/cited.py`). Before the
  first line it refuses, naming ids and hashes only: a dataset folder of another dataset or
  version; freeze.json missing or not the run's (sha256 against run.json's `freeze_sha256`); a
  question, corpus file or corpus manifest that differs from that freeze.json; and, per cited
  chunk, a citation of a chunk the prompt did not hold (results.jsonl's `evidence_chunk_ids`, ADR
  0011), a citation that is not one of its question's hits in results.jsonl with the same
  source, version, chunk index and page, a citation of a document no customer may see (draft,
  retired or staff-only: not `answers_customers`), or a rebuilt chunk with another page or other
  evidence groups than the run recorded. The audience refusal repeats the run's own check (a run
  stops on such a hit, `AudienceLeak`), so it means an edited folder or a harness bug: a leak
  signal under the hard gate, refused rather than shown as evidence to label (decided 2026-10-28,
  shesha: the whole sheet is refused, not only flagged). The page and groups check is a tripwire
  for a loader or chunker changed since the run, not a proof: a change that keeps every anchor in
  its chunk passes it (a test pins this limit). The stronger guard is upstream: a run's
  `verify_stored` refuses stored chunks the current chunker would not make, so a chunker change
  during M3 already stops the next run on M2's namespaces (decided 2026-10-28, shesha: the
  tripwire stays, with no extra refusal on a git diff of the loader or chunker). A run without a
  freeze (only a dry run) shows "text not shown" and why; the check that each citation is a chunk
  the prompt held needs no corpus and runs for it too (both decided 2026-10-28, shesha; the
  second built then).
  Terminal only: nothing is written or logged. Every printed value from a run or dataset file
  (question, reference, answer, chunk, ids, reason, error, and the values a refusal names) shows
  control characters and bidi controls as escapes; one-line values (ids, reason, error) escape
  newline and tab too. The reply's lines sit behind a `>` gutter and a chunk's behind `|`, so
  neither can start a line the sheet's own structure uses (a question header, a Sources line, a
  cited-chunks block). Escapes and both gutters kept (decided 2026-10-28, shesha).
- **`summarize`** ends summary.md with a Faithfulness section, after the Targets table, so every
  section above it renders as before (checked byte for byte against PR 5's merged `report.py` on
  four TEST-ONLY folders; summary.json only gains the `faithfulness` key). It gives the shown
  answers, the labelled ones, the unlabelled and unfaithful ones by id, `x of n` faithful with
  decision 7's Wilson interval for "all" and each business, language and category (safety as
  counts only), and §6's ≥ 95 % target (exactly 19/20) for "all" and each business under decision
  10: met when the point estimate reaches it, always with its interval, "not shown at 95 %
  confidence" when the lower end is below it; no verdict for a dry, partial, failed or aborted
  run, below `RATE_MIN_N`, while any shown answer is unlabelled, or for a business with no shown
  answer, which reads "no verdict: no shown answer" ahead of the run-wide unlabelled reason
  (decided and built 2026-10-28, shesha; a dry or incomplete run still reads so first). A target
  row's Measured cell reads "no shown answer" for a group with none, and "none
  labelled (k shown)" while k shown answers wait for a label. Faithfulness never enters the status
  line or "Not for the note": correctness alone decides those, as in M2 (decided 2026-10-28,
  shesha).
- **M2's folders** get their faithfulness labels in M3 PR 9 step 1, on `m3/close-out`: `summarize`
  re-summarizes them there, changing only the Faithfulness section and summary.json's
  `faithfulness` key, and each folder is committed there (decided 2026-10-28, shesha).
- **Expected outcome on v0.1, an estimate from the manifest minimums, not a count.** At 40
  questions the test split holds 12. At least 17 of the 40 expect no shown answer (6 `safety` and
  3 `handoff_request` expect a hand-off; 8 expect `dont_know` with no evidence), so at most 23
  expect an answer, about 7 of them on test. The test split likely has fewer than 10 shown
  answers, so "no verdict: n < 10" is the expected faithfulness result there, for "all" and for
  each business. Dev's `x of n` is tuning information only.
- **Limits.** One labeller, so no agreement figure; per-answer labels cannot say which claim
  failed; the judges that would scale this wait for D4's 30 labelled answers.
