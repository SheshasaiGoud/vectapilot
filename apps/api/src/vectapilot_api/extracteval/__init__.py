"""The `doc_extraction` eval (M6 PR 4, D8 (b)): the document dataset's tools and the extraction
harness.

Dataset commands (no model, no network, no database; `cli.py`, run as
`uv run python -m vectapilot_api.extracteval`):
- `gold` — the manifest with its slots (fixed before any document), D8's minimums and the gold
  rows; shape only, never imports `vectapilot_api.extraction`
- `source` — a document's text source, `<id>.pdf.txt`: page markers and the image, hidden-text
  and column options the fixture writer draws
- `checks` — one stage-aware check: the slots, each gold row against its slot and its business's
  template, each document against E12 (the core's own reader) and its source, the freeze
- `freeze` — what `freeze.json` pins
- `report` — slots, counts and minimums, then each gold row beside its page text
- `files` — reading the dataset and the extraction configuration, writing a freeze

The harness (`run`, `summarize`; no database: the core runs in-process):
- `harness` — the outside world (`Io`), loading (live: the frozen dataset; dry: the fixtures),
  the settings rules, H1's probe, the test discipline and M6's spend lines
- `dryrun` — the dry run's fixture documents from `tests/` and its scripted model
- `runs` — `run` and `summarize`: the folder, run.json, results.jsonl, values.jsonl
- `compare` — the eval's own comparison of a value with its gold (imports nothing of the core)
- `metrics` — H2 (`[HAND-WRITE]`, D7 (b)): five metric functions, written 2026-11-15
- `summary` — summary.md through H2, the subsets and intervals around it
- `runinfo` (folder names and the run files) and `spend` (M6's live runs against D8's $1.00)

Documents and gold values are written by people only (shesha and friends), never by an agent
(ADR 0012); nothing here writes, completes or suggests one. The dataset is
`evals/datasets/doc_extraction/<version>/`; its README holds the scoring rules and the writers'
brief. Values and quotes reach `values.jsonl` only, never a log, run.json, results.jsonl or a
summary.
"""
