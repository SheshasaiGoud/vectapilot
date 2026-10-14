"""The LLM gateway: the only path to a model (ADR 0006).

Retries with backoff, time limits, cost per call, structured output, a cache hook and a fake
provider all live here, scoped by `business_id`. Built across M1 (docs/plans/m1-pr-checks.md).

[HAND-WRITE] pieces, written by the human lead (guide: docs/learning/m1-gateway.md):
- `pricing.cost_usd` (H1)
- `retry.backoff_delay` and `retry.call_with_retries` (H2)
"""
