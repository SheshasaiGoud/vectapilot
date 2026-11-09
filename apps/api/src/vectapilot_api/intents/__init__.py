"""The intent router's platform modules (M4, ADR 0015). No database, no retrieval, no business data.

- `taxonomy` — the eight intents, their one-line definitions and the decision order
- `policy` — the closed set of business types, a route template per type, and the mandatory
  hand-off sets every template is checked against
- `classifier` — the committed classifier artifact and its standard-library scorer (PR 3)
- `calibration` — one temperature, fitted on dev by negative log-likelihood (PR 3)
- `fallback` — the versioned router prompt and the one LLM call below τ (PR 3)
- `decide` — `route_message`: the safety floor, the threshold and the fallback; its body is
  shesha's H1 (`[HAND-WRITE]`, guide: docs/learning/m4-router.md)

The package is `intents`, not `router`: in this codebase `router.py` already means a FastAPI
router. The dataset tools live in `intenteval` (M4 PR 1; PR 4 adds embedding, training and
evaluation).
"""
