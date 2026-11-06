"""The `intents` dataset tools (M4 PR 1): schema, checks, split, sample, freeze and report.

Pure modules over text and values, plus one I/O shell (`files.py`) and a CLI (`cli.py`, run as
`uv run python -m vectapilot_api.intenteval`). No model calls, no network, no database.

- `rows` — the training and eval row schemas, the dataset manifest, loaders
- `similarity` — the near-duplicate screen (character 3-gram Jaccard on normalised text)
- `split` — the eval split rule (`evalset/split.py`'s v2 rule over eval rows), content hashes,
  `freeze.json`
- `sample` — D3's seeded, stratified verification sample of the training rows
- `checks` — one stage-aware check, D3's minimums, the freeze
- `report` — the counts tables
- `files`, `cli` — reading and writing the files; the command line

Training rows are drafted by Claude (PR 2) and eval rows are written by people only (D3 (a),
ADR 0012): nothing here writes, completes or suggests a row. The dataset is
`evals/datasets/intents/<version>/`; its README documents every field and rule. PR 4 adds the
embedding, training and evaluation commands here.
"""
