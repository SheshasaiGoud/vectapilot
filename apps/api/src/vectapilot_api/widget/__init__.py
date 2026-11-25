"""The customer widget's API (M8; ADR 0019, D2 (a), D3 (a)).

- `config`: the widget configuration folder (`VECTAPILOT_WIDGET_CONFIG_DIR`), validated at
  startup (rule 9, S8).
- `models`: the routes' request and response bodies.
- `router`: `GET /v1/widget/{business_id}/config`, `POST /v1/widget/sessions` and
  `DELETE /v1/widget/session` (PR 1).
- `messages`: `POST /v1/widget/messages`, `POST /v1/widget/handoffs` and
  `GET /v1/widget/handoffs/{handoff_id}` (PR 2a).
- `confirm`: `POST /v1/widget/actions/{action_id}/confirm` (PR 2b): the confirm route's own
  service and limits, the customer from the session, no unguarded branch.
- `feedback`: `POST /v1/widget/feedback` (PR 2b): one rating of one of the session's own turns.

The widget page itself is served by `ui.pages` (PR 2b).
"""
