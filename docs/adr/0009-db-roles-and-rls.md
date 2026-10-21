# ADR 0009 — Database roles and row-level security

- **Status:** accepted · 2026-10-21 · chosen by shesha (M2 plan, decision 3: "yes to all
  recommendations"). Implements [ADR 0004](0004-multi-business-isolation.md).

**Context.** ADR 0004 chose shared tables + `business_id` + Postgres row-level security (RLS). M2
adds the first business-owned tables (documents, chunks). RLS is only as strong as the role that
queries: superusers and `BYPASSRLS` roles always skip it, and a table's owner skips it unless the
table has `FORCE ROW LEVEL SECURITY`. Today the API connects as the stack's bootstrap user, a
superuser (`infra/compose.yaml`). Connections are pooled, and there is no authentication until M8.

**Options.** Roles: (a) one role for everything; (b) an owner role for migrations plus a separate
app role. Setting the business: (i) `SET app.business_id` per connection; (ii) `set_config(...,
true)` per transaction; (iii) one pool per business. Naming the business before M8: a header, a
path segment, or waiting for auth.

**Decision.** (b) + (ii) + a header.
- **Roles.** The owner role owns the schema and runs Alembic; the API never connects as it.
  `vectapilot_app` logs in with `NOSUPERUSER NOBYPASSRLS`, owns nothing and gets only the grants it
  needs. Startup fails if the app connection's role is a superuser or has `BYPASSRLS`.
- **FORCE RLS** on every business-owned table, so even owner sessions (migrations, scripts) see no
  rows unless they name a business. Policy: `business_id = nullif(current_setting(
  'app.business_id', true), '')`, for both `USING` and `WITH CHECK`.
- **Once per transaction.** Each unit of work opens a transaction and first runs
  `select set_config('app.business_id', :business_id, true)`. Why not `SET`: it lasts for the
  session, and a pooled connection carries it into the next request, possibly another business's.
  `true` (local) ends it at commit or rollback, and a function takes a bind parameter where `SET`
  would need string-built SQL. One business per transaction.
- **No business set ⇒ 0 rows.** `current_setting(..., true)` returns NULL when never set and `''`
  on a connection where a local value has ended; `nullif` turns both into NULL, which matches no
  row, so reads return nothing and writes fail `WITH CHECK`.
- **`X-Business-Id` until M8.** Validated as a `BusinessId` (`re.fullmatch`); unknown and malformed
  ids get the same error. It names a business; it does not authenticate. Refused in `production`
  (400), so business routes cannot be served there before M8 auth replaces it.
- **Canary leakage suite = hard gate.** At least two fictional businesses, each seeded with a
  unique canary string. Acting as one, no read path (SQL, vector search, API route) may return the
  other's canary; no business ⇒ 0 rows; a reused pooled connection keeps no business; the app
  role's attributes are checked; a catalog test fails when any table with `business_id` lacks
  RLS or `FORCE`. It runs in CI against real Postgres and blocks merges (business-context §6).

**Consequences.** Isolation holds even when code forgets a filter. The price: two roles and two
database URLs; every database access, background jobs included, needs a transaction with the
business set; owner scripts must name a business deliberately; CI gains a Postgres service and
minutes. RLS covers Postgres only: caches, files, prompts and logs are scoped in code, and the
canary suite grows to cover them as they arrive.
