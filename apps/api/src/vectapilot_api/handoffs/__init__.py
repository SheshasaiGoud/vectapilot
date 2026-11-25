"""Hand-offs and widget turns at rest (M8 PR 2; migration 0009; D6 (a), S11, S17).

- `at_rest`: S11's one helper for customer text that rests in a table: cleaned as the guards
  clean it, masked by H2's `mask_pii`, and None (stored as NULL) whenever that cannot be done
  within the column's bound. Never raw, never truncated.
- `store`: the statements on migration 0009's tables, each with its own `business_id` clause:
  storing or raising a session's open hand-off (S17), "talk to a person" (idempotent per
  session), a widget turn's metadata row, a session's read of its own hand-off's status, and (PR
  2b) a session's one rating of one of its own turns.

The widget routes (`widget.messages`, `widget.feedback`) call both outside the measured services
(S13, G13); the staff routes (PR 3) read the same rows.
"""
