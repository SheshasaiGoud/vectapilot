"""The eval harness (M2 PR 8): ingest eval v0's corpus, answer and score its questions, report.

Separate from `evalset` on purpose: `evalset` is pure (no model, network or database) and only
defines the data; this package runs the product against it. Pure modules plus I/O shells:

- `intervals` — Wilson score interval for `x of n`, a seeded percentile bootstrap for means (pure)
- `namespace` — the eval business id per (business, embedding model, chunk config) (pure)
- `scoring` — the leak check, the unscorable check, one question's record; calls H2 (pure)
- `budget` — the `--max-usd` cap over embeddings and the answer model (M1's pattern, extended)
- `plan` — the whole run's worst-case cost, printed before anything is sent (pure)
- `store` — the harness's own SQL: document businesses, stored chunks, registry, scoped delete
- `ingest` — the corpus through `KbService`, then the stored chunks checked against the corpus
- `dryrun` — the dry run's canned answer model
- `harness` — the run: ingest, check, answer and score each question through `AnswerService`
- `report` — run.json, the labels files, and the summary recomputed from the run folder
- `cited` — the label sheet's cited chunk text, rebuilt from the run's frozen corpus (M3 PR 7)
- `ledger` — the test-run ledger, `evals/reports/<dataset>/test-runs.jsonl`
- `compare` — two runs of one chunk config, paired by question; prints only (M3 PR 6)
- `cli` — `uv run python -m vectapilot_api.evalrun run|ingest|summarize|label-sheet|`
  `faithfulness-template|compare`

The retrieval metrics are shesha's H2 (`evalset/metrics.py`); the search is shesha's H1
(`retrieval/vector.py`). Both are called, never re-implemented here. Runbook for the live run:
docs/learning/m2-eval-run.md.
"""
