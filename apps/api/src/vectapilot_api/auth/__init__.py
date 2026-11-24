"""Sessions and staff access (M8 PR 1, ADR 0019; D1 (a), D3 (a), D4 (a), D12 (b)). Standard library
only: no session, JWT or cookie-signing package (D8's "not proposed" list).

- `tokens`: a session or login-link token, `<business_id>.<secrets.token_urlsafe(32)>` (S3), its
  parse and its SHA-256, the only form ever stored.
- `store`: the statements on migration 0008's tables, each with its own `business_id` clause:
  the session lookups (customer and staff), creating and revoking a session, and S5's atomic use
  of a login link.
- `dependencies`: what a route depends on. `get_customer_session` (a bearer token, the widget,
  D3 (a)), `require_staff(role)` (the `__Host-vp_staff` cookie, D4 (a)), `require_same_origin`
  (S5's CSRF check on every non-GET staff route). Every authentication failure is one fixed 401;
  a lookup that cannot run is a typed 503 (S14).
- `cookies`: the staff cookie's name and attributes, set, cleared and read.
- `http`: `SessionRoute`, the route class of the widget and staff routers: `Cache-Control:
  no-store` on every answer and a 422 that names no unknown key, on those routes only (S9, S13).
- `origins`: the strict `scheme://host[:port]` form of `public_origin` (and, from the widget
  configuration, each business's embedding origins), written into headers and compared exactly.

This package imports nothing at package level, so `config` can import `origins` without a cycle.
"""
