"""The customer widget's API (M8; ADR 0019, D2 (a), D3 (a)).

- `config`: the widget configuration folder (`VECTAPILOT_WIDGET_CONFIG_DIR`), validated at
  startup (rule 9, S8).
- `models`: the routes' request and response bodies.
- `router`: `GET /v1/widget/{business_id}/config`, `POST /v1/widget/sessions` and
  `DELETE /v1/widget/session` (PR 1). The message, hand-off, confirm and feedback routes come with
  PR 2.
"""
