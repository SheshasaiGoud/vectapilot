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
- `report` — run.json, the labels file, and the summary recomputed from the run folder
- `cli` — `uv run python -m vectapilot_api.evalrun run|ingest|summarize|label-sheet`

The retrieval metrics are shesha's H2 (`evalset/metrics.py`); the search is shesha's H1
(`retrieval/vector.py`). Both are called, never re-implemented here. Runbook for the live run:
docs/learning/m2-eval-run.md.
"""
