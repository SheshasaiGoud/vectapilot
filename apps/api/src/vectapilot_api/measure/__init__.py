"""Measurement helpers for the M1 measurement run (PR 8; ADR 0007, "Measurement").

Pure functions over numbers the gateway already recorded: no model calls, no I/O. Off the gateway's
critical path.

- `latency` — `latency_summary` (H3) and its result type `LatencySummary`

[HAND-WRITE] pieces (guide: docs/learning/m1-gateway.md, section 6):
- latency_summary (H3): implemented by Claude at shesha's request (2026-10-15); shesha's
  explain-back is pending
"""
