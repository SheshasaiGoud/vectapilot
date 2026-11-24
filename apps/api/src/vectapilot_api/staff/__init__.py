"""Staff access (M8; ADR 0019 decision 3, D4 (a)).

- `router`: `POST /v1/staff/login`, `POST /v1/staff/logout` and `GET /v1/staff/me` (PR 1). The
  queue and extraction review come with PR 3.
- `owner`: the owner role's statements on `staff_users` and `staff_login_links` (migration 0008).
- `cli`: `python -m vectapilot_api.staff add|issue-link|disable` (owner role; local, test and ci).
"""
