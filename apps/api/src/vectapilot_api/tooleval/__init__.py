"""The `tool_calls` eval (M5 PR 6, D8): the scenario dataset's tools and the agent's harness.

Dataset commands (no model, no network, no database; `cli.py`, run as
`uv run python -m vectapilot_api.tooleval`):
- `scenarios` — the scenario schema, the dataset manifest (D8's minimums, the split rule), loaders
- `split` — the 50/50 split rule fixed before any scenario, content hashes, `freeze.json`
- `checks` — one stage-aware check, D8's minimums, the freeze
- `report` — the counts tables
- `files` — reading and writing the dataset and the sandbox configuration

The harness (`run`, `summarize`, `label-sheet`):
- `runinfo` — run folders, `run.json`, `results.jsonl` (ids only), replies and labels
- `plans` — the dry run's scripted fake model: fixture plans from `tests/`, never the dataset
- `turns` — one scenario: reset, the agent service with `router=none`, the leak check, the confirm
  of a matching proposal (twice, once concurrently), and the counts from the database
- `writes` — those counts: read-only SQL on the owner's connection, by business and trace id
- `metrics` — **H2 [HAND-WRITE]**: the five metric functions (stubs until written; guide
  docs/learning/m5-metrics.md)
- `summary` — `summary.md`, from the results through H2
- `ledger`, `spend` — the test-run ledger, and M5's spend against D8's $1.00
- `harness`, `runs` — what every harness command shares, and the commands

Scenarios are written by people only (shesha and friends), never by an agent (ADR 0012); nothing
here writes, completes or suggests one. The dataset is `evals/datasets/tool_calls/<version>/`; its
README holds the scoring rules and the writers' brief.
"""
