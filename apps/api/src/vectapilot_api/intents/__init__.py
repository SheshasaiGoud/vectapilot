"""The intent router's platform modules (M4, ADR 0015). Pure values, no I/O, no model.

- `taxonomy` — the eight intents, their one-line definitions and the decision order
- `policy` — the closed set of business types, a route template per type, and the mandatory
  hand-off sets every template is checked against

The package is `intents`, not `router`: in this codebase `router.py` already means a FastAPI
router. The dataset tools live in `intenteval` (M4 PR 1); the classifier, calibration and the
fallback come in PR 3. The fallback decision itself (`route_message`) is shesha's H1
(`[HAND-WRITE]`).
"""
