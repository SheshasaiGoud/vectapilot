"""The LLM gateway: the only path to a model (ADR 0006).

Retries with backoff, time limits, cost per call, structured output, a cache hook and a fake
provider all live here, scoped by `business_id`. Built across M1 (docs/plans/m1-pr-checks.md).

[HAND-WRITE] pieces (guide: docs/learning/m1-gateway.md):
- `pricing.cost_usd` (H1) and `retry.*` (H2): implemented by Claude at shesha's request
  (2026-10-15); shesha's explain-backs are pending
"""
