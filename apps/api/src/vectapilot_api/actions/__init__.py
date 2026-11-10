"""Pending actions and their confirmation (ADR 0016 decisions 2-3, M5 PR 3).

A write tool is only ever proposed by the loop (W1). This package holds what happens next:

- `pending.py`: `PendingActionStore` stores a `WriteProposal` as a pending action with a DB-set
  expiry and the hash of a one-time confirm token (`tokens.py`); PR 4's `/v1/agent` calls it.
- `confirm.py`: `ConfirmService` executes a pending action exactly once, in one transaction with
  its audit row, and is the only importer of `tools.execute` (W1).
- `router.py`: `POST /v1/agent/actions/{action_id}/confirm`, the only route that writes.
- `audit.py`: the metadata-only audit rows and `tool_call` records of both (W12).
"""
