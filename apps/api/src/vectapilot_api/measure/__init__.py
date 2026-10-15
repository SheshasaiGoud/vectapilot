"""Measurement helpers for the M1 measurement run (PR 8; ADR 0007, "Measurement").

Pure functions over numbers the gateway already recorded: no model calls, no I/O. Off the gateway's
critical path.

- `dataset` — the gateway_smoke case and manifest types, loaders and checks
- `schemas` — the structured-output schemas the dataset's cases name
- `records` — `llm_call` log lines back into `CallRecord`, paired with the calls made
- `summary` — one run's typed summary, rendered to Markdown and JSON

[HAND-WRITE] pieces (guide: docs/learning/m1-gateway.md, section 6):
- latency_summary (H3): implemented by Claude at shesha's request (2026-10-15); shesha's
  explain-back is pending
"""
