# M2 PR 9 runbook: the eval v0 live run (shesha only)

The harness is `apps/api/src/vectapilot_api/evalrun/` (PR 8, noah); `--help` points here. Agents
never make this run, never pass `--live` and never see a key (ADR 0006/0007/0008). Everything
before step 7 is free.

## 0. What one run does

`uv run python -m vectapilot_api.evalrun run --split dev|test [--live --max-usd X]`:

1. checks the dataset (as `evalset check`), selects the split, loads Settings, prints the plan and
   its worst-case cost; `--live` asks for the cap to be typed again at a terminal;
2. registers the eval businesses if needed (`<business>-ev-<hash of model and chunk config>`,
   decided 2026-10-22: `evalrun/namespace.py`), ingests the corpus through `KbService` (nothing is
   embedded twice) and checks the stored chunks against the corpus at the chunk config;
3. asks every question of the split through `AnswerService` (search as customer, threshold,
   prompt, `large`, citation check), checks every hit's document, as the search returns it,
   belongs to the question's business and is one a customer may see (approved, public; either
   failure aborts the run, exit 4), and scores retrieval with your H2;
4. writes the run folder: `run.json`, `results.jsonl`, `answers.jsonl`, `labels.jsonl` (template),
   the three record logs, and `summary.md` / `summary.json` recomputed from those files.

Live folders go to `evals/reports/support_qa/v0.1/<UTC start>-live-<split>/` (committed); dry runs
to the same path under the git-ignored `evals/reports/tmp/`. Exit codes: 0 complete, 1 failed or
refused, 2 bad arguments, 3 partial, 4 cross-business leak or a hit no customer may see (also when
writing the folder failed afterwards). A stop rule tripped by the last question leaves no question
unrun: `complete`, exit 0, and never for the note (decided 2026-10-22, as M1). **The status line
in `summary.md` decides what a run is, never the exit code.**

## 1. Before the day

1. **H1 and H2 merged**, with `"vector_search"` and `"retrieval_metrics"` removed from
   `PENDING_HAND_WRITE` (`apps/api/tests/canary/read_paths.py`) and CI green, including
   `tests/evalrun/test_postgres_harness.py`'s end-to-end run.
2. **The dataset written and frozen:** 40+ questions (yours and the friends'), `faq-hi` reviewed
   (`"review": "done"` in Sunrise's corpus manifest), `evalset ready` passes, `evalset freeze` run
   once, and `questions.jsonl`, `freeze.json` and `manifest.json` committed together. A live run
   refuses an unfrozen dataset (a dry run runs every question of it as `unassigned`), and refuses
   data whose dataset manifest or any corpus manifest does not say `synthetic: true` (a live folder
   commits the model's answers; real pilot data needs its own decision first).
3. **Voyage checks** (plan PR 4, "Verify before the first live call"), dated: Hindi support, region
   and data residency, spend limit, the training opt-out done; the model id and the price
   ($0.06 per million tokens for `voyage-4` in `embeddings/models.toml`, checked 2026-10-21).
4. **Anthropic checks** (M1 runbook, `docs/plans/m1-pr-checks.md`, PR 9 steps 1-2): the dedicated
   workspace with its monthly spend limit, default inference geo `global`, prices against
   `gateway/models.toml`.
5. **Rehearse for $0:** `make up`, then `make eval-dry-run` from the repository root. Its status
   line must start with `DRY RUN`; look through the folder it names.

## 2. On the day

6. A clean tree on the commit to measure (`git status`: nothing to commit). A dirty tree or a
   dataset not as committed is recorded and keeps the run out of the note.
7. A separate terminal that never runs Claude Code or Cursor; `cd apps/api`. `apps/api/.env`
   provides the database and Redis URLs (both the app role's and the owner's: the owner only
   registers eval businesses). Keys never go into `.env` (startup refuses them). Enter both keys
   (paste, Enter; nothing is shown or kept in the history):
   ```
   read -s VECTAPILOT_ANTHROPIC_API_KEY && export VECTAPILOT_ANTHROPIC_API_KEY
   read -s VECTAPILOT_VOYAGE_API_KEY && export VECTAPILOT_VOYAGE_API_KEY
   export VECTAPILOT_LLM_PROVIDER=anthropic VECTAPILOT_EMBEDDINGS_PROVIDER=voyage
   env | cut -d= -f1 | grep -E '^(ANTHROPIC_|SSL)'    # must print nothing
   ```
8. **Dev first.** Choose the cap `X` (per run, at most $1.00) from the plan the command prints
   before asking: a call is sent only while what the run has spent so far plus that call's worst
   case stays within `X`, and the plan names the largest single call's worst case (a `large`
   answer over five long chunks, every retry and the repair included). So `X` must exceed that
   figure by what you expect the run to spend, or the run stops `PARTIAL`.
   ```
   uv run python -m vectapilot_api.evalrun run --split dev --live --max-usd X
   ```
   Read the plan, type the cap again. The run ingests the corpus with Voyage the first time.
9. **Tune on dev only**, if at all: `--min-score` (the threshold, ADR 0011) or `--max-chars` /
   `--overlap` (a fresh namespace, re-ingested; v0.1 cannot meaningfully tune it, ADR 0012). Every
   variant is a new config fingerprint and a new folder; keep the ones that did not help too.
   **Two chunk configs are compared on dev only, over the questions scorable under both** (take
   the ids whose `retrieval` is `scored` in both folders' `results.jsonl`; no command does it
   yet), and read each folder's conservative line too: a config that makes questions unscorable
   must not look better for it (decided 2026-10-22).
10. Commit the dev folder(s) you will report against on a branch (a test run checks that a
    complete live dev run with its exact config is committed, byte for byte, in `HEAD`).
11. **Test once per reported config:**
    ```
    uv run python -m vectapilot_api.evalrun run --split test --live --max-usd X
    ```
    It appends the run to `evals/reports/support_qa/test-runs.jsonl` before the first paid call,
    and refuses to start while that ledger differs from the one committed in `HEAD` (commit it
    with the test folder, step 12, before any further test run). It refuses a config that already
    has a test run there; a second run needs `--test-rerun-reason <slug>`, which the ledger keeps
    and the status line names ("held-out test RERUN"). The note cites **every** ledger entry for
    the frozen version, not only the run it reports: run.json and summary.md record how many came
    before each run (this config, and this frozen version in any config).
12. **Label** every answer of the run you report:
    ```
    uv run python -m vectapilot_api.evalrun label-sheet ../../evals/reports/support_qa/v0.1/<run>
    ```
    prints each question, its expected outcome and reference answer, and the reply (to the
    terminal only). In that folder's `labels.jsonl`, set each line's `label` (`correct` |
    `incorrect`, rules in `docs/learning/m2-eval-questions.md` §5), `labelled_by` (`shesha`) and
    `labelled_on` (`YYYY-MM-DD`); then `... evalrun summarize <run folder>`. Commit the folder with
    its labels and new summary, and for a test run `evals/reports/support_qa/test-runs.jsonl` in
    the same commit.

## 3. Cost: a formula, not a measurement

Prices from the catalogs: `p_emb` = $0.06 / million tokens (`voyage-4`); for `large`
(`claude-sonnet-5-5`) `p_in` = $4 / million (the highest input-side price, a 1-hour cache write,
M1's conservative choice) and `p_out` = $10 / million. `r` = `llm_max_retries` (2), `M` =
`large`'s `max_output_tokens` (2048).

- **Ingest** (only the first run of a namespace): `(1 + r) · p_emb · Σ (UTF-8 bytes + 8)` over
  every chunk of the 18 documents.
- **Per question, embedding:** `(1 + r) · p_emb · (UTF-8 bytes of the question + 8)`.
- **Per question with evidence, the answer:** `(1 + r) · Σ over 2 requests (first, repair) of
  (P · p_in + M · p_out)`, with `P` = the prompt's UTF-8 bytes (system prompt, at most 5 chunks
  and the question) + the schema + 256 (+ 1,024 for the repair) + 200. No evidence: no model call.

The CLI prints this bound for the whole plan before asking for the cap. The cap reserves it call by
call: a call is sent only if its worst case plus what the run has *actually* spent stays within
`--max-usd`, so a run whose plan bound exceeds the cap can still complete; if not, it stops,
`PARTIAL`. The bound is deliberately loose (every prompt token at the 1-hour cache-write price,
every attempt at the full output cap), so the largest single call's worst case is far above what
a call is likely to cost; how far is unmeasured until the live run. What a run really cost is in
its `summary.md` ("Cost") and `run.json`, from the providers' reported usage. The Voyage free
allowance is never counted. M1's formula and its $1.00 ceiling per run stay (decided 2026-10-22).

## 4. Reading the summary

- **Status line:** anything starting `DRY RUN` and anything `PARTIAL` is never a result;
  `Complete live run on the dev split` is tuning; `Complete live held-out test run: may feed the
  note (config X)` (or `... test RERUN (reason: ...)`) appears only when nothing in "Not for the
  note" stands against it; otherwise the line reads `Held-out test run, not for the note yet:
  <reasons>` (every test run starts that way, until its answers are labelled). Decided
  2026-10-22: **only a complete, labelled, live test run feeds the note**; dev runs appear next to
  it, labelled as tuning.
- **Not for the note** lists every reason a run cannot feed `docs/notes/m2-what-i-measured.md`:
  not test, partial, stopped by its last question (complete, but a stop rule tripped), dirty
  tree, dataset not as committed, not frozen, a review pending, answers not labelled. For a test
  run, the **Test-run ledger** row gives the earlier entries; the note cites every ledger entry of
  the frozen version.
- **Retrieval:** hit@5 and MRR@5 on the search's list before the threshold, `x of n` with a 95 %
  Wilson interval, the first-relevant-rank counts; per business, language and category, each row
  with its unscorable questions next to n, and a conservative hit@5 line (every unscorable
  question counted as a miss, for "all" and each business); safety as counts only. MRR shows a
  value and a seeded bootstrap interval only from n = 10; below that, only the rank counts. The
  summary says the intervals are "approximate at small n (percentile bootstrap undercovers)", and
  they never reach past the observed values: a zero-width one is printed as "bootstrap
  degenerate", so read the hit@5 Wilson interval instead. Not scored (no
  evidence), unscorable at this config and not run are listed by id, never dropped, and so are
  evidence keys no stored chunk holds in a group another key still finds. "Evidence complete"
  names any multi_hop or ambiguous question it could not count (answer-step error).
- **At v0.1's size** (a 12-question test split, fewer scored), the per-language and per-category
  test rows, and likely the "all" row, are below n = 10: read them as counts (`x of n` with a
  Wilson interval, no percentage, no MRR value: `RATE_MIN_N` applies to every split). Wilson for
  12 of 12 is 75.8 % to 100 %, so no interval at this size can show a 90 % target at 95 %
  confidence.
- **Targets (business-context §6)** (decided 2026-10-22): hit@5 and correctness are "met" when
  the point estimate reaches 90 %, always with the Wilson interval, and "not shown at 95 %
  confidence" when its lower end is below 90 %. The table gives "all" and each business (a
  release needs every business); no verdict below n = 10, for a dry, partial or failed run, at
  another metric k, or while answers are unlabelled. Quote the verdict with its `x of n` and
  interval, never alone.
- **Answers:** expected outcome against actual outcome, abstain reasons, safety hand-offs (counts;
  M2 has no safety check, ADR 0011).
- **Correctness:** only from your labels, `x of n` with its interval.

## 5. When something stops

- `CROSS-BUSINESS LEAK` (exit 4): a hard gate (business-context §6). Stop, keep the folder, and
  open it with zara before anything else. `AUDIENCE LEAK` (exit 4): a staff-only, draft or retired
  document reached a customer's search (business-context §3.4); the same.
- `the run FAILED, no summary written` (after the calls): the folder holds everything the run
  made, and the message names what was spent; fix the cause, then `... evalrun summarize <run
  folder>`. A live folder: commit it as evidence only, or delete it before the next live run.
- `interrupted before the questions` (Ctrl-C while registering, ingesting or checking): `PARTIAL`,
  with run.json and the cost counted so far.
- Stored chunks differ (`run` fails before any question): `... evalrun run ... --fresh` deletes the
  documents of this config's eval businesses only, then ingests again (a paid re-embedding in a
  live run, under the cap).
- `NotImplementedError`: H1 or H2 is not merged on this commit.
- The cap, an account error (`spend_limit`, `billing`, `auth_failed`), `invalid_request`, a `us`
  geo or a call beyond its bound: `PARTIAL`; the folder shows every call made in its record logs.
  When the last question tripped the stop rule, no question is left: `complete`, exit 0, with
  the stop in run.json and "stopped by its last question" under "Not for the note"; fix the
  cause before another live run all the same.
- `--live needs data marked synthetic`: a manifest does not say `synthetic: true`. Nothing was
  sent; data that is not synthetic needs its own decision (where its answers are stored) first.
