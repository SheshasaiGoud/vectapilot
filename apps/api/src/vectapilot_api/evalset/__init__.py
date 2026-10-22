"""Eval v0 (M2 PR 7): the sample corpus, the question dataset, its checks, split and freeze.

Pure modules over text and values, plus one I/O shell (`files.py`) and a CLI (`cli.py`, run as
`uv run python -m vectapilot_api.evalset`). No model calls, no network, no database.

- `corpus` — `data/sample/<business_id>/manifest.json`, documents loaded and chunked by the real
  ingestion code, anchors resolved to chunk indexes, `lookup` for humans
- `questions` — the question schema (evidence groups), the dataset manifest, row checks,
  readiness, the coverage report, the correctness-label schema
- `relevance` — when a retrieved chunk is evidence for a question (by content; PR 8 scores with
  it)
- `ranking` — one question's ranked groups (`RankedRelevance`), built from `relevance`, and which
  questions are scored for retrieval at all
- `metrics` — hit@k and MRR on those ranked groups: shesha's H2 (`[HAND-WRITE]`), stubs until then
- `split` — the test-split rule, its balance check, content hashes, `freeze.json`
- `checks` — one stage-aware check of a version, the split preview, and the freeze
- `personal_data` — the fail-closed screen for phone numbers, Aadhaar numbers and e-mail addresses
- `files`, `cli` — reading and writing the files; the command line

The questions themselves are human-written (decided 2026-10-21); nothing here writes one. The
retrieval metrics (hit@k, MRR) are shesha's H2 (`[HAND-WRITE]`, `metrics.py`): they take the
ranked groups `relevance.matched_groups` yields, as `ranking.RankedRelevance` records.
"""
