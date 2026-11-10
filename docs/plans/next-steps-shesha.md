# Next steps for shesha: one ordered checklist (M1–M4)

> As of 2026-11-10 (main at `76dad45`, M4 H2 merged as #56). Prepared by maya so shesha can act
> without reading four plans. The detail stays in the sources: `docs/plans/m1-pr-checks.md` PR 9,
> `docs/plans/m2-close-out.md`, `docs/plans/m3-pr-checks.md` PR 9, `docs/plans/m4-pr-checks.md`
> PR 5 and H1, runbook `docs/learning/m2-eval-run.md`. If this file and a source disagree, the
> source wins. Every time below is an **estimate**, never a measurement.

**Where things stand:** no Voyage or Anthropic key; no live run in any milestone; `support_qa`
v0.1 has 0 questions (stage `collecting`); `faq-hi` is `review: pending`; `intents` v0.1 has 0 of
≥ 160 eval rows; M4 H1 (`route_message`) not started; 8 explain-backs open (M1 H2/H3, M2 H1/H2,
M3 H1/H2, M4 H1/H2). Done already: Docker works, M1's smoke-prompt review (`"review": "done"`).

`uv run` commands run inside `apps/api/`; `make` runs from the repository root.

## Rules for every step
- **R1** Agents never pass `--live`, never run a test split, never see a key. Every live and
  every test step below is typed by shesha.
- **R2** Keys: entered with `read -s` in a separate terminal that never runs Claude Code or
  Cursor; never in the repo tree, `apps/api/.env`, a shell profile or history. Close the terminal
  after the run.
- **R3** Never open `evals/datasets/intents/v0.1/train.jsonl` (or its diff, PR or an editor search)
  before the M4 eval rows are committed.
- **R4** Commit each live folder, on that milestone's close-out branch, before the next live run.
  Never delete a live folder.
- **R5** M3 measurement window, from M2's live dev run (step 11) to M3's last live answer run
  (step 17): nothing merges that changes `gateway/models.toml`, `PROMPT_VERSION` or the gateway's
  output path; no `make reset-db`, no Docker volume prune (free disk outside Docker's volumes).
- **R6** Caps: each run ≤ $1.00 · M3 ≤ $5.00 total · M4 ≤ $2.00 total · Anthropic workspace
  $10/month. Arithmetic, not a forecast: if M1–M4 all fall in one calendar month, M3's and M4's
  ceilings leave $3.00 for M1 and M2 together (Voyage bills separately).
- **R7** `summary.md`'s status line decides whether a run counts, never the exit code.

## The live terminal (L): used by every paid step
A fresh terminal without Claude Code or Cursor, on a clean commit. Enter only the key(s) the step
pays for. Step by step:
```bash
cd apps/api
```
```bash
git status
```
```bash
read -s VECTAPILOT_ANTHROPIC_API_KEY && export VECTAPILOT_ANTHROPIC_API_KEY
```
```bash
read -s VECTAPILOT_VOYAGE_API_KEY && export VECTAPILOT_VOYAGE_API_KEY
```
```bash
export VECTAPILOT_LLM_PROVIDER=anthropic VECTAPILOT_EMBEDDINGS_PROVIDER=voyage
```
```bash
env | cut -d= -f1 | grep -E '^(ANTHROPIC_|SSL)'
```
`git status` must show nothing to commit; the `env` check must print nothing. Export only the
provider you pay for (an M4 run without `--live` refuses a non-fake provider). Type the cap again
when asked; close the terminal when the run ends.

## Order at a glance
```
Now, in parallel ($0):  1 friends' briefs · 2 accounts + keys · 3 M4 eval rows · 4 M2 questions
                        · 5 M4 H1 · 6 explain-backs (any time)
After 2:                7 M1 live run
M2 → M3 (one track):    8 import → 9 freeze → 10 dry run → 11 live dev (opens R5) → 12 live test
                        → 13 labels → 14 faithfulness → 15 hybrid dev → 16 compare → 17 decide
M4 (own track, after 2, 3, 5; parallel with 8–17):
                        18 sample check → 19 freeze → 20 dry run → 21 live embed → 22 train
                        → 23 dev runs → 24 test runs
Last:                   25 notes · 26 study PDFs and quizzes
```
Friends (1) take days and M2 needs ≥ 10 `friend_blind` questions, so send first. Keys (2) unlock
every paid step in M1–M4.

## Now, in parallel
**1. Send both briefs to friends.** est. 15 min of yours, days of theirs. Unlocks 8 (and optional
M4 friend rows). M2: `evals/datasets/support_qa/v0.1/friends-brief.md` + `friends-template.csv`.
M4: `evals/datasets/intents/v0.1/writers-brief.md` (friends never see `train.jsonl`, R3). Keep
returned CSVs outside the repository.

**2. Accounts and keys.** est. 1–2 h. Unlocks 7, 11, 12, 15, 21, 23, 24. Anthropic: personal
organisation, dedicated workspace, $10/month spend limit, default inference geo `global`, key in
that workspace; prices re-checked against `apps/api/src/vectapilot_api/gateway/models.toml`.
Voyage: account, payment method, training opt-out (cannot be undone: read the FAQ first), dated
checks (Hindi, region, spend limit, model id and price against
`apps/api/src/vectapilot_api/embeddings/models.toml`), key. A changed price lands through a PR
**before** step 11 (R5). Detail: `m2-close-out.md` step 11. Rule R2.

**3. Write the M4 eval rows (≥ 160) in `evals/datasets/intents/v0.1/eval.jsonl`.** est. 4–6 h.
Unlocks 18. Minimums (D3): ≥ 20 per intent; per business ≥ 10 `emergency` and ≥ 10
`professional_judgement`; Sunrise ≥ 6 safety rows in Hindi or Hinglish, 3 of them `emergency`; no
self-harm rows (D5). Guide: the writers' brief. Branch: proposal `m4/eval-rows` → PR. Rule R3.
From the repository root:
```bash
make intent-check
```

**4. M2 corpus review, own questions and labels.** est. 30 min + 3–4 h + 2–3 h. Unlocks 9.
Review `data/sample/sunrise-clinic/faq-hi.md` against `faq.md`, then set `"review": "done"` in
`data/sample/sunrise-clinic/manifest.json` (corrections go to noah first). Write questions in
`evals/datasets/support_qa/v0.1/questions.jsonl` (guide `docs/learning/m2-eval-questions.md` §2–3;
needs ≥ 40 total, ≥ 10 Hindi/Hinglish, ≥ 15 per business, ≥ 3 safety per business, 8 with no
evidence). Branch: proposal `m2/eval-v0-questions`. Anchor helpers (`evalset lookup`, `show`):
`m2-close-out.md` step 7. From the root; done when it prints `check (collecting, N questions): ok`:
```bash
make eval-check
```

**5. M4 H1: write `route_message`** (`[HAND-WRITE]`, time-box 2 h, decided). Unlocks 20 and the
hybrid rows in 23–24. Guide `docs/learning/m4-router.md` §1; reviews kenji-ml and zara-safety.
From `apps/api`:
```bash
git switch -c m4/fallback
```
```bash
uv run pytest tests/intents/test_decide.py tests/intents/test_fallback.py -v
```
Done when every case shows `XPASS(strict)`, the switches (guide §1) are removed and CI is green.

**6. Explain-backs** (part of acceptance; est. 20–30 min each; any time, M4 H1 after step 5).

| Item | Guide section | Ask |
|---|---|---|
| M1 H2 retry · M1 H3 latency | `docs/learning/m1-gateway.md` §5 · §6.3 | diego-backend |
| M2 H1 `vector_search` · M2 H2 metrics | `docs/learning/m2-retrieval.md` §6 · `m2-metrics.md` §6 | ananya-rag · noah-evals |
| M3 H1 `text_search` · H2 `rrf_fuse` | `docs/learning/m3-hybrid.md` §6 | ananya-rag |
| M4 H1 `route_message` · M4 H2 metrics | `docs/learning/m4-router.md` §6 · `m4-metrics.md` §6 | kenji-ml · noah-evals |

## M1
**7. M1 live run.** est. 30 min + 1 h note. Cap $1.00. Needs 2. Unlocks the M1 note and quiz,
and rehearses key entry on the smallest run. Checklist: `m1-pr-checks.md` PR 9, steps 1–18.
Rehearse from the root, then in L (Anthropic key only):
```bash
make llm-demo
```
```bash
uv run vectapilot-llm --live --max-usd 1.00
```
Commit `evals/reports/gateway_smoke/v0.1/<UTC start>-live/` through a PR; compare tokens with the
Console (PR 9 step 14); fill `docs/notes/m1-what-i-measured.md`.

## M2 → M3 (one track)
**8. Import friends' questions** (after 1, 4). est. 1 h per friend. From `apps/api`:
```bash
uv run python -m vectapilot_api.evalset import-friends ~/Downloads/friend-a.csv > /tmp/friend-a.jsonl
```
Complete each draft, append to `questions.jsonl`, update the README's "Left out at import" line.

**9. Ready, then freeze once.** est. 30 min. Unlocks 11. Done when `ready` ends `ready: yes`;
commit `questions.jsonl`, `freeze.json` and `manifest.json` together through a PR.
```bash
uv run python -m vectapilot_api.evalset ready
```
```bash
uv run python -m vectapilot_api.evalset freeze
```

**10. Dry run ($0).** est. 15 min. From the root; done when the status line starts `DRY RUN`.
```bash
make up
```
```bash
make eval-dry-run
```

**11. M2 live dev run** (opens R5). est. 30 min. Cap ≤ $1.00 (R6). Needs 2, 9, 10, `make up`.
Create `m2/close-out` from `main` (no other change), then in L (both keys):
```bash
uv run python -m vectapilot_api.evalrun run --split dev --live --max-usd X
```
Optional dev tuning (`--min-score`), each a new paid folder. Commit the dev folder(s) on
`m2/close-out` (R4): step 12 runs on exactly this commit plus the dev folder. Detail: runbook §2, `m2-close-out.md` steps 13–14.

**12. M2 live test run, once.** est. 30 min. Cap ≤ $1.00. On `m2/close-out` (the dev folder in
HEAD), in a fresh L. Done when it is in `evals/reports/support_qa/test-runs.jsonl`.
```bash
uv run python -m vectapilot_api.evalrun run --split test --live --max-usd X
```

**13. Label M2's answers, summarize, commit** with the ledger, one commit, PR. est. 1–2 h.
```bash
uv run python -m vectapilot_api.evalrun label-sheet ../../evals/reports/support_qa/v0.1/<run>
```
```bash
uv run python -m vectapilot_api.evalrun summarize ../../evals/reports/support_qa/v0.1/<run>
```
Done when the status line reads `Complete live held-out test run: may feed the note`. Merge
`m2/close-out`, then start `m3/close-out` from `main`: M3's runs need M2's folders in HEAD.

**14. M3 faithfulness labels on M2's dev and test folders** (on `m3/close-out`; do it in the same
reading as 13 if you can). est. 1 h. Per folder: the template, then step 13's `label-sheet` and
`summarize` on the same folder; commit each folder on `m3/close-out`.
```bash
uv run python -m vectapilot_api.evalrun faithfulness-template ../../evals/reports/support_qa/v0.1/<run>
```

**15. M3 live hybrid dev runs** (D15 (i): M2's `--min-score`, plus at most one variant). est.
30 min each. Each ≤ $1.00; all M3 runs ≤ $5.00 (read "M3 spend so far" first). In L (both keys),
on `m3/close-out`, `make up` running; add M2's `--max-chars`/`--overlap`/`--search-k` if not the
defaults. Commit each folder, losers too (R4):
```bash
uv run python -m vectapilot_api.evalrun run --split dev --retrieval-mode hybrid --min-score <M2's> --live --max-usd X
```

**16. Compare** M2's vector dev folder with each hybrid one ($0, prints only). est. 30 min.
```bash
uv run python -m vectapilot_api.evalrun compare ../../evals/reports/support_qa/v0.1/<vector-dev> ../../evals/reports/support_qa/v0.1/<hybrid-dev>
```

**17. Decide under D15 (ii)** (rule: `m3-pr-checks.md` PR 9 step 4). Vector stays: no test run,
M2's test folder is the result, R5 ends here. Hybrid adopted: the team flips the default in a
small PR; one hybrid test run in L (≤ $1.00, within the $5.00), labels as 13–14, then `compare`
with M2's test folder; R5 ends after it.
```bash
uv run python -m vectapilot_api.evalrun run --split test --retrieval-mode hybrid --min-score <M2's> --live --max-usd X
```

## M4 (own track; parallel with 8–17 once 2, 3 and 5 are done)
**18. Training sample check** (only after step 3 is committed: R3 lifts now). est. 1 h. Check
each id in `evals/datasets/intents/v0.1/verification-sample.txt` in `train.jsonl`, correct
wrong labels, set `verified: true`; note the changed labels as `x of n`.
```bash
uv run python -m vectapilot_api.intenteval report
```

**19. Freeze `intents` v0.1 once**, commit the three files through a PR. est. 15 min.
```bash
uv run python -m vectapilot_api.intenteval freeze
```

**20. Dry run ($0)** (needs 5 and 19 merged). est. 15 min. From the root:
```bash
make intent-dry-run
```

**21. Live embed** (train + eval). est. 15 min. Cap ≤ $1.00; counts towards M4's $2.00. Start
`m4/close-out` from `main`; in L (Voyage key and provider only). Commit the `-live-embed` folder.
```bash
uv run python -m vectapilot_api.intenteval embed --live --max-usd X
```

**22. Train, calibrate, thresholds ($0, any terminal, no key).** est. 15 min. Commit the
`-local-model` folder (artifact and dev curve) on `m4/close-out`.
```bash
uv run python -m vectapilot_api.intenteval train --embed ../../evals/reports/intents/v0.1/<UTC>-live-embed
```
```bash
uv run python -m vectapilot_api.intenteval calibrate ../../evals/reports/intents/v0.1/<UTC>-local-model
```
```bash
uv run python -m vectapilot_api.intenteval thresholds ../../evals/reports/intents/v0.1/<UTC>-local-model
```

**23. Dev runs.** est. 30 min. Classifier ($0), then LLM in L (Anthropic key and provider only;
≤ $1.00, counts towards $2.00), then the replayed hybrid ($0). Commit each folder.
```bash
uv run python -m vectapilot_api.intenteval run --split dev --mode classifier --model ../../evals/reports/intents/v0.1/<UTC>-local-model
```
```bash
uv run python -m vectapilot_api.intenteval run --split dev --mode llm --live --max-usd X
```
```bash
uv run python -m vectapilot_api.intenteval summarize --classifier ../../evals/reports/intents/v0.1/<UTC>-local-classifier-dev --llm ../../evals/reports/intents/v0.1/<UTC>-live-llm-dev
```

**24. Test, once per mode** (typed confirmations at a terminal). est. 45 min. Pin first (clean
tree; commit `evals/reports/intents/test-runs.jsonl`), then LLM in L (≤ $1.00, within what is left
of $2.00), classifier ($0), hybrid. Commit each folder.
```bash
uv run python -m vectapilot_api.intenteval pin ../../evals/reports/intents/v0.1/<UTC>-local-model
```
```bash
uv run python -m vectapilot_api.intenteval run --split test --mode llm --live --max-usd X
```
```bash
uv run python -m vectapilot_api.intenteval run --split test --mode classifier --model ../../evals/reports/intents/v0.1/<UTC>-local-model
```
```bash
uv run python -m vectapilot_api.intenteval summarize --classifier ../../evals/reports/intents/v0.1/<UTC>-local-classifier-test --llm ../../evals/reports/intents/v0.1/<UTC>-live-llm-test
```

## Last
**25. The notes**, each figure as printed, with dataset version, commit and config: est. 1–2 h
each. `docs/notes/m2-what-i-measured.md` (after 13), `docs/notes/m3-what-i-measured.md` (after 17;
noah writes the skeleton first, it does not exist yet), `docs/notes/m4-what-i-measured.md` (after 24). noah checks figures, maya reviews.

**26. Study PDFs and quizzes, M1–M4.** est. 30 min each. Then maya marks each milestone complete
in CLAUDE.md.
