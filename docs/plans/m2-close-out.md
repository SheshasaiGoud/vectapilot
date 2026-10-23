# M2 close-out: shesha's remaining steps (PR 9)

> **Status (2026-10-22):** the team side of M2 is merged (plan PR 0–8, PR 5b and the H1 and H2
> scaffolds; GitHub #23–#34).
> Everything below is shesha's. Today: H1 and H2 are written (by Claude at shesha's request,
> 2026-10-22: H2 #36, H1 on `m2/retrieval`; `PENDING_HAND_WRITE` is empty), so steps 3 and 4
> leave only their explain-backs (step 18); `support_qa` v0.1 has no
> questions (`stage: collecting`); `faq-hi` is `review: pending`; Docker Desktop is unreachable on
> the Mac; no Voyage or Anthropic account or key is known to be set up. Prepared by maya on
> `m2/close-out-prep`. Nothing in this file is a result, and no cost in it is a figure: the cost of
> a run is the runbook's formula (`docs/learning/m2-eval-run.md` §3), printed by the CLI per run.

Each step says what to do, the exact commands (checked against the `Makefile`, `pyproject.toml`
and the two CLIs on 2026-10-22), **how you know it is done** (the signal to look for), who can
help, and what it needs first. Commands with `uv run` run inside `apps/api/`; `make` runs from the
repository root. Agents never pass `--live`, never see a key, and write no question, reference
answer or label.

## Order of work

```
Now, in parallel:  1 friends' brief · 2 Docker · 3 H2 · 5 faq-hi review · 6 own questions · 11 accounts
Then:              4 H1 (after 2, recommended) · 7 labels (after 5, 6) · 8 friends' rows (after 1, 5)
Dataset:           9 ready → 10 freeze (after 5–8)
Runs:              12 dry run (after 2–4) → 13 live dev (after 10–12) → 14 tune, commit dev
                   → 15 live test → 16 label, summarize → 17 note → 19 quiz
After 3 and 4 merge: 18 explain-backs
```

The critical path is the dataset (1, 5–10: friends take days) and Docker: the dry and live runs
need local Postgres; CI cannot stand in for them. Step 12 can run before the freeze (every question
as `unassigned`); steps 13 and 15 cannot.

> **Team items found in maya's review of this prep: both fixed in this PR (2026-10-22).**
> - **T1 (fixed).** `tests/test_canary_checks.py` asserted exactly one mark from
>   `pending_hand_writes(*PENDING_HAND_WRITE)`, which fails once both stubs are written (the set is
>   empty), and an empty `frozenset()` needed a type for mypy. Now the test expects
>   `min(1, len(PENDING_HAND_WRITE))` marks and the set is typed `Final[frozenset[str]]`; checked
>   by emptying the set temporarily (64 canary checks pass, mypy clean). H1 and H2 never change for it.
> - **T2 (fixed).** `tests/retrieval/test_vector_search_binding.py`'s docstring now says to delete
>   only the `xfail` entry of `pytestmark` and keep `anyio`, matching the guide.

## 1. Send the friends' brief (first: it takes them days)
- **Do:** send `evals/datasets/support_qa/v0.1/friends-brief.md` and `friends-template.csv` to one
  or two friends who have not seen the documents. Do not answer their "what does it cost?"
  questions while they write. Keep the returned CSVs **outside the repository** (they may hold
  comments; the dataset is public).
- **Done when:** the CSVs (or messages) are back.
- **Help:** noah-evals (brief, template). **Needs:** nothing.

## 2. Get Docker Desktop working
- **Do:** quit Docker Desktop and open it again (or use its own Restart menu item), then wait until
  it reports that the engine is running. Agents make no system changes here: anything beyond a
  restart (Troubleshoot actions that clean or reset data, a reinstall, a local Postgres) is your
  call with an explicit yes, and option (c) in `docs/learning/m2-retrieval.md` §1 is global software
  (rules 1 and 11).
- **Commands:**
  ```bash
  docker info          # prints a "Server:" section; "Cannot connect to the Docker daemon" = still down
  make up              # Postgres + pgvector, Redis, migrations, API; returns when healthy
  make ps              # every service running/healthy; the migrate job exited 0
  make test-integration
  ```
- **Done when:** `make up` returns without error and `make test-integration` runs the Postgres
  tests (they show `passed` or `xfailed`, no longer `skipped`).
- **Help:** sofia-frontend (Docker), diego-backend (the stack, migrations, roles). **Needs:** nothing.
- **Open (rule 10):** the H1 guide's question (a) Docker first, (b) CI only with a longer time-box,
  or (c) a local Postgres is still yours to answer; this step is option (a).

## 3. H2: the retrieval metrics, on `m2/metrics` (`[HAND-WRITE]`, time-box 2 h)
> **Done (2026-10-22):** written by Claude at shesha's request; 142 of 142 cases pass and
> "retrieval_metrics" is out of `PENDING_HAND_WRITE`. The explain-back (step 18) remains.

- **Do:** write the five bodies in `apps/api/src/vectapilot_api/evalset/metrics.py`; guide
  `docs/learning/m2-metrics.md` (§1 workflow, §5 decisions as taken). No Docker needed.
- **Commands:**
  ```bash
  cd apps/api
  git switch main && git pull
  git switch -c m2/metrics
  uv run pytest tests/evalset/test_ranking.py tests/evalset/test_metrics.py -v   # metrics: 142 xfailed
  # write the bodies, then:
  uv run pytest tests/evalset/test_metrics.py -v
  ```
- **The switch:** when every case shows `XPASS(strict)`, edit `apps/api/tests/canary/read_paths.py`
  and remove `"retrieval_metrics"` from `PENDING_HAND_WRITE`:
  - if H1 is not merged yet: `PENDING_HAND_WRITE: Final = frozenset({"vector_search"})`;
  - if H1 is already merged (the set becomes empty, and mypy strict needs the type, checked):
    `PENDING_HAND_WRITE: Final[frozenset[str]] = frozenset()`.

  Then `uv run pytest -q && uv run mypy && uv run ruff check . && uv run ruff format --check .`
  (or `make check` from the root, as CI).
- **Done when:** the cases went `xfailed` → `XPASS(strict)` → (switch removed) `passed`; the PR's
  three CI checks are green (`api (lint, types, tests)`, `api image (docker build)`,
  `web (lint, build)`); noah-evals reviewed; merged.
- **If a PR 8 test fails** once the switch is off (`tests/evalrun/`, team code): ask noah-evals.
  Proposal, to confirm: the rule decided for H1 and PR 6 applies here too (the team fixes its own
  test in a separate commit; H2 answers only to `test_metrics.py`).
- **Help:** noah-evals (review), kenji-ml (the definitions). **Needs:** nothing.

## 4. H1: `vector_search`, on `m2/retrieval` (`[HAND-WRITE]`, time-box 3 h)
> **Done (2026-10-22):** written by Claude at shesha's request; the binding test passes locally,
> the three switches are removed, and the Postgres cases (H1's, PR 6's, PR 8's, the canary paths)
> first run in this PR's CI. The explain-back (step 18) remains.

- **Do:** write the body in `apps/api/src/vectapilot_api/retrieval/vector.py`; guide
  `docs/learning/m2-retrieval.md` (§1 workflow, §5 decisions as taken).
- **Commands:**
  ```bash
  cd apps/api
  git switch main && git pull
  git switch -c m2/retrieval
  uv run pytest tests/retrieval -v      # types pass · binding: 2 xfailed · Postgres: skipped without Docker
  uv run pytest tests/retrieval/test_vector_search_binding.py -v
  make test-integration                  # from the root, once step 2 works; else push and read CI
  ```
  Without Docker, open a draft PR early: CI's `api (lint, types, tests)` log shows the Postgres
  cases (`FAILED` with a diff = a wrong answer; `XPASS(strict)` = that case passes).
- **The switch (three markers, guide §1):** when every case shows `XPASS(strict)`, remove
  1. the `xfail` entry of `pytestmark` in `tests/retrieval/test_vector_search_binding.py` (keep
     `anyio`; see T2);
  2. the `xfail` entry of `pytestmark` in `tests/retrieval/test_vector_search.py` (keep
     `integration` and `anyio`);
  3. `"vector_search"` from `PENDING_HAND_WRITE` in `tests/canary/read_paths.py`: as in step 3, the
     line becomes `frozenset({"retrieval_metrics"})` if H2 is not merged yet, or
     `Final[frozenset[str]] = frozenset()` if it is.

  This one line also turns on PR 6's Postgres tests (`tests/answer/test_postgres_search_and_answer.py`),
  the canary paths `GET /v1/kb/search` and `POST /v1/answer`, and PR 8's searching harness tests.
  **If a PR 6 test fails in your H1 PR, ananya fixes her test in a separate commit; H1 never
  changes to suit it** (decided 2026-10-22).
- **Done when:** after the switch every H1 test, the canary suite and the canary gate pass in CI;
  ananya-rag and zara-safety (the business filter) reviewed; merged.
- **Help:** ananya-rag (review, PR 6 tests), zara-safety (canary). **Needs:** Docker (step 2)
  recommended, CI otherwise.

## 5. Review `faq-hi`
- **Do:** read `data/sample/sunrise-clinic/faq-hi.md` against `faq.md` (same facts, natural Hindi,
  the emergency section matching the English one). Corrections go to noah-evals (the corpus owner)
  **before** you label evidence: an anchor quotes the document verbatim, so a later edit breaks it.
  When it is right, set `"review": "done"` in faq-hi's entry of
  `data/sample/sunrise-clinic/manifest.json`.
- **Command:** `uv run python -m vectapilot_api.evalset ready`
- **Done when:** `ready` prints no `REVIEW` line.
- **Help:** noah-evals. **Needs:** nothing.

## 6. Write your own questions
- **Do:** guide `docs/learning/m2-eval-questions.md` §2: a customer's voice, some written before
  you reread the documents, about 10 Hindi or Hinglish to Sunrise, at least 8 that nothing public
  answers, at least 3 safety questions per business (the sensitive ones are yours alone). Rows go
  in `evals/datasets/support_qa/v0.1/questions.jsonl` on a branch of yours (suggested name, not
  decided: `m2/eval-v0-questions`).
- **Commands:**
  ```bash
  make eval-check                                      # check, then the coverage counts
  uv run python -m vectapilot_api.evalset report
  ```
- **Done when:** `check` prints `check (collecting, N questions): ok` and `report` shows your share
  of the requirements.
- **Help:** noah-evals (schema, checks); zara-safety if you want a second view of the safety rows.
  **Needs:** nothing.

## 7. Label category, outcome and evidence
- **Do:** guide §3, in its order: `category` (precedence safety > handoff_request > adversarial >
  out_of_scope > ambiguous > multi_hop > policy > faq), `subtype`, `expected_outcome`,
  `expected_evidence` as groups of `(source_id, version, anchor)` keys, `split_group` for
  translations and twins, `reference_answer`, `id`. `split` stays `null`.
- **Commands:**
  ```bash
  uv run python -m vectapilot_api.evalset lookup sunrise-clinic "<words to find>"
  uv run python -m vectapilot_api.evalset show sunrise-clinic <source_id> [--version <version>]
  uv run python -m vectapilot_api.evalset check
  ```
- **Done when:** `check` is `ok` with every row labelled.
- **Help:** noah-evals, ananya-rag (what a chunk holds). **Needs:** step 5 (corpus final), step 6.

## 8. Import the friends' questions
- **Do:** optionally add a last column `category` to each CSV first (your triage: `action` and
  `document` rows are dropped and counted). Then import, complete each draft (`id`, `category`,
  `expected_outcome`, `expected_evidence`, …; never "fix" their wording), append to
  `questions.jsonl`, read their comments in the CSV (they are not copied), and add the dropped count
  to the README's "Left out at import" line.
- **Command:**
  ```bash
  uv run python -m vectapilot_api.evalset import-friends ~/Downloads/friend-a.csv > /tmp/friend-a.jsonl
  ```
- **Done when:** stderr prints `N draft row(s); … dropped … skipped … had a comment`, and after you
  complete and append the rows, `check` is `ok`. A personal-data refusal names the row: ask the
  friend to invent the detail.
- **Help:** noah-evals. **Needs:** steps 1 and 5.

## 9. `ready`
- **Command:** `uv run python -m vectapilot_api.evalset ready`
- **Done when:** its last line is `ready: yes` (no `MISSING`, `REVIEW` or `BALANCE` line). On
  `BALANCE`: add or remove **one** question that belongs anyway, record it in the README, run again;
  never change the seed or the percentage (README, "If freeze refuses").
- **Help:** noah-evals. **Needs:** steps 5–8.

## 10. `freeze` once, and commit the frozen dataset
- **Command:** `uv run python -m vectapilot_api.evalset freeze`
- **Done when:** it prints `frozen support_qa v0.1: … dev, … test. Review the diff and commit
  questions.jsonl, freeze.json and manifest.json together.`; `freeze.json` exists and the manifest
  says `"stage": "frozen"`. Commit those three files together (with Sunrise's corpus manifest and the
  README line if not yet committed), open a PR; CI is green, where `tests/evalset/test_dataset_v0_1.py`
  now runs the frozen checks. From here on, a change is a new version (README, "Adding a version").
- **Help:** noah-evals (review). **Needs:** step 9.

## 11. Accounts and keys (any time before step 13)
- **Voyage** (ADR 0008; plan PR 4, "Verify before the first live call"): an account and
  organisation; a **payment method** (the training opt-out and rate-limit tier 1 need one); the
  **training opt-out** (needs an organisation Admin and cannot be undone in the dashboard: read
  Voyage's FAQ first; ADR 0008 requires it before the first live call); then check and **date**:
  Hindi (and Telugu) support, API region and data residency, whether a spend limit exists (set it if
  so), limits without a payment method, and on the day the model id and price against
  `apps/api/src/vectapilot_api/embeddings/models.toml`. Create the API key.
- **Anthropic** (ADR 0007; `docs/plans/m1-pr-checks.md` PR 9, "Before the day" steps 1–2): a
  personal organisation with a **dedicated workspace**, its **monthly spend limit** and default
  inference geo `global`; a key created in that workspace; prices of the `large` alias's model
  re-checked against `apps/api/src/vectapilot_api/gateway/models.toml` (a changed price lands
  through a PR, new `catalog_version`, before the run). Done already if M1's live run came first.
- **Key handling (ADR 0007):** keys live where no agent can read them: never in the repository
  tree, `apps/api/.env` (startup refuses them there), a shell profile or the history. On the day you
  enter them with `read -s` in a separate terminal that never runs Claude Code or Cursor (step 13),
  and close it afterwards. Keychain only if every read prompts.
- **Done when:** both dashboards show those settings, and every check has its date, ready to copy
  into the note's "Provider checks" section.
- **Help:** ananya-rag (Voyage adapter, ADR 0008), diego-backend (gateway, ADR 0006/0007). Nobody
  but you sees a key. **Needs:** nothing.

## 12. Rehearse for $0 (dry run)
- **Commands:** `make up`, then `make eval-dry-run` (fake embedder, canned answers, into the
  git-ignored `evals/reports/tmp/`). Optionally rehearse labelling:
  `uv run python -m vectapilot_api.evalrun label-sheet <the folder it names>`.
- **Done when:** the summary's status line starts `DRY RUN`, and you have looked through the folder.
  `NotImplementedError` means H1 or H2 is not on this commit.
- **Help:** noah-evals. **Needs:** steps 2, 3, 4 merged, and questions in the dataset (before the
  freeze they run as `unassigned`).

## 13. Live dev run (runbook steps 6–8)
- **Do:** `docs/learning/m2-eval-run.md` §2. A clean tree on the commit to measure; `make up`; a
  separate terminal without Claude Code or Cursor; `cd apps/api` (`.env` provides the database and
  Redis URLs, never keys).
- **Commands:**
  ```bash
  git status                                   # nothing to commit
  read -s VECTAPILOT_ANTHROPIC_API_KEY && export VECTAPILOT_ANTHROPIC_API_KEY
  read -s VECTAPILOT_VOYAGE_API_KEY && export VECTAPILOT_VOYAGE_API_KEY
  export VECTAPILOT_LLM_PROVIDER=anthropic VECTAPILOT_EMBEDDINGS_PROVIDER=voyage
  env | cut -d= -f1 | grep -E '^(ANTHROPIC_|SSL)'    # must print nothing
  uv run python -m vectapilot_api.evalrun run --split dev --live --max-usd X
  ```
  Choose `X` (at most 1.00) from the plan the command prints (runbook step 8: the cap must exceed
  the largest single call's worst case by what you expect the run to spend), type it again, and
  close the terminal when it ends.
- **Done when:** `summary.md`'s status line reads `Complete live run on the dev split: tuning
  numbers, not the held-out result.` The status line decides, never the exit code. `PARTIAL`,
  `FAILED` or a leak: runbook §5 (a `CROSS-BUSINESS LEAK` or `AUDIENCE LEAK` goes to zara first).
- **Help:** noah-evals (harness), zara-safety (any leak). **Needs:** steps 2–4, 10, 11, 12.

## 14. Tune on dev only (optional), then commit the dev folder(s)
- **Do:** runbook steps 9–10. Each variant (`--min-score`, or `--max-chars`/`--overlap` with a fresh
  namespace) is a new folder; keep the ones that did not help. Compare chunk configs only over the
  questions scorable under both, and read each conservative line. Commit the dev folder(s) you
  will report against on a branch (suggested, not decided: `m2/close-out`).
- **Done when:** the complete live dev folder of the config you will test is committed (HEAD):
  the test run refuses otherwise.
- **Help:** noah-evals, ananya-rag (threshold, ADR 0011). **Needs:** step 13.

## 15. Live test run, once per reported config (runbook step 11)
- **Do:** the same terminal setup as step 13 (a fresh terminal, keys again), on the clean commit
  that holds the dev folder.
- **Command:** `uv run python -m vectapilot_api.evalrun run --split test --live --max-usd X`
- **Done when:** it appended its entry to `evals/reports/support_qa/test-runs.jsonl` and the status
  line reads `Held-out test run, not for the note yet:` with only the unlabelled-answers reason
  left. A second run of the same config needs `--test-rerun-reason <slug>` and stays visible in the
  ledger.
- **Help:** noah-evals. **Needs:** step 14.

## 16. Label every answer, summarize, commit (runbook step 12)
- **Commands:**
  ```bash
  uv run python -m vectapilot_api.evalrun label-sheet ../../evals/reports/support_qa/v0.1/<run>
  # in that folder's labels.jsonl: label correct|incorrect, labelled_by shesha, labelled_on YYYY-MM-DD
  uv run python -m vectapilot_api.evalrun summarize ../../evals/reports/support_qa/v0.1/<run>
  ```
  The labels are yours (guide §5; a "prices are fictional" caveat is ignored).
- **Done when:** the status line reads `Complete live held-out test run: may feed the note (config
  …)` and the "Not for the note" row reads `nothing: it may feed it`. Commit the folder (labels and
  new summary) with `evals/reports/support_qa/test-runs.jsonl` in one commit; PR; CI green.
- **Help:** noah-evals. **Needs:** step 15.

## 17. The "what I measured" note
- **Do:** fill `docs/notes/m2-what-i-measured.md` from that test folder's `summary.md` only, every
  figure as printed (`x of n` with its interval); dev runs only in its tuning section; cite every
  ledger entry of the frozen version.
- **Done when:** no placeholder is left, noah-evals checked each figure against `summary.md`, maya
  reviewed, CI green.
- **Help:** noah-evals, maya-tech-lead. **Needs:** step 16.

## 18. Explain-backs (part of acceptance; right after each merge)
- **H2:** `docs/learning/m2-metrics.md` §6. **H1:** `docs/learning/m2-retrieval.md` §6.
- **Done when:** each is recorded as passed in CLAUDE.md's "Current status" (as M1's H1 was).
- **Help:** noah-evals and kenji-ml (H2), ananya-rag and zara-safety (H1). **Needs:** steps 3, 4.

## 19. M2 close-out quiz, and closing M2
- **Do:** the M2 study PDF (questions with answers, your chosen format) and the close-out quiz.
- **Done when:** the quiz is recorded as passed, PR 9's checks in `docs/plans/m2-pr-checks.md` are
  ticked, and CLAUDE.md says M2 is complete (maya updates both).
- **Help:** maya-tech-lead. **Needs:** steps 16–18.

## Not blockers for the close-out (recorded, owners named)
- Pool-return `RESET app.business_id`: waits until it can be proven against Postgres (plan PR 2,
  pinned by a strict xfail); possible once step 2 works.
- After H1 (zara, ananya, you): mirror the owner-connection checks into `tests/canary` or make the
  gate require them; a Postgres red proof that an API path turns red.
- Redis and `compose.yaml` images pinned by digest; `db_provision.py` SCRAM verifier (by M10).

## M1 leftovers (also shesha's)
- **Live run:** `docs/plans/m1-pr-checks.md`, PR 9, "Live run (shesha only)", steps 1–18. Rehearse
  with `make llm-demo`; the run itself, from `apps/api` in a separate terminal after `read -s`, is
  `uv run vectapilot-llm --live --max-usd 1.00`. **Done when:** `summary.md`'s status line reads
  `complete live run: may feed the "what I measured" note`, and the run folder is committed through
  a PR. Proposal: do it before M2's step 13, since it uses the same Anthropic workspace and key
  entry on a smaller run.
- **Note:** `docs/notes/m1-what-i-measured.md` (skeleton ready).
- **Explain-backs:** M1 H2 retry (`docs/learning/m1-gateway.md` §5) and H3 `latency_summary` (§6.3).
- **M1 close-out quiz.**
- **Help:** diego-backend (gateway), noah-evals (measurement).
