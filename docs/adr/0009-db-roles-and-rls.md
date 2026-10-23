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
  needs. Startup fails if the app connection's role is a superuser, has `BYPASSRLS`, owns a
  relation (an owner can turn `FORCE` off), or is a member of a role that does any of these.
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
  ids get the same error. It names a business; it does not authenticate. Accepted only in `local`,
  `test` and `ci` (an allowlist; `production` and any later environment get a 400), so business
  routes cannot be served there before M8 auth replaces it.
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

**Role and password (settled in M2 PR 1, 2026-10-21; Diego's choice, open to shesha's review).**
- Migration 0002 creates `vectapilot_app` **NOLOGIN, without a password**, plus its grants, so a
  migration never holds a secret. If the role already exists with SUPERUSER or BYPASSRLS, the
  migration stops instead of altering it.
- `vectapilot-db-provision`, run as the owner right after `alembic upgrade head`, turns on LOGIN
  and sets the password taken from the API's own URL (`VECTAPILOT_DATABASE_URL`, whose user must
  be `vectapilot_app`). So there is one source for the password; it is idempotent, and running it
  again after a change rotates it. Postgres quotes the literal (`format('%L')`) and stores only a
  SCRAM hash. The `migrate` compose job and CI run it; the API never gets the owner's URL.
- Why not a Postgres init script: it runs only on an empty data volume (existing local volumes
  would never get the role) and CI's service containers cannot mount one.
- Local stack: `VECTAPILOT_APP_DB_PASSWORD` in `infra/.env` (default `vectapilot-app-dev`, like
  ADR 0005's `POSTGRES_PASSWORD`). CI: a throwaway `vectapilot-app-ci`. Production: M10.
- Limit: locally and in CI the owner is the bootstrap superuser, which skips RLS even with FORCE.
  A non-superuser owner, and an owner allowed to create roles on managed Postgres, are M10 work.

**Canary suite (M2 PR 2, 2026-10-21; Zara; the key exception is a proposal for shesha).**
- `apps/api/tests/canary/`: the business tables are read from `pg_catalog`, never listed. Beyond
  RLS + FORCE it checks what RLS does not cover: unique/exclusion keys and foreign keys are
  enforced across all rows, so they must lead with or pair `business_id`; a view must use
  `security_invoker`; a materialized view cannot be protected at all.
- Proposal: a primary key on one `uuid` column defaulting to `gen_random_uuid()` is allowed
  without `business_id` (no caller can guess a value to collide with), only while no API accepts
  a client-chosen id.
- Each check breaks its protection once per CI run in a rolled-back transaction and must turn
  red; `scripts/canary_gate.py` fails CI when the suite did not run.

**Amended 2026-10-23 (shesha, M3 plan D10; `docs/plans/m3-pr-checks.md`).**
- **Pool-return reset** (D10 (a), built in M3 PR 1 by diego). A pool hook resets `app.business_id`
  whenever a connection returns to the pool, so a session-level `SET` cannot reach the next
  request. This is defence in depth: the transaction-local `set_config` stays the rule. On
  2026-10-21 shesha decided the reset waits until it can be proven against Postgres. Docker works
  again (2026-10-23) and CI has Postgres, so it can be proven now.
  `test_a_session_level_business_does_not_survive_the_pool` then XPASSes and its strict xfail is
  removed. The red proof: without the hook, the test fails. diego documents which hook he picks
  and why.
  - Done 2026-10-23 (M3 PR 1, diego). The hook is SQLAlchemy's pool `reset` event
    (`db.reset_business_on_return`, installed by `db.create_api_engine`, which `create_app`
    uses). It rolls back any open transaction, then sends `RESET app.business_id` outside a
    transaction. The rollback comes first because SQLAlchemy runs the event before its own
    rollback, and Postgres undoes a `RESET` with the transaction it ran in. Why not `checkin`: a
    failing `checkin` listener loses the pool slot and the error reaches the request. A failing
    `reset` is logged by SQLAlchemy, and the connection is invalidated (closed, never pooled
    again), so it fails closed. asyncpg's own reset query runs only in asyncpg's pool, which
    SQLAlchemy does not use. Proven against Postgres in `tests/canary/test_leakage.py`, with two
    red proofs (no hook; the hook without its rollback) and a failed-reset check.
  - 2026-10-23 (shesha): `verify_role` also refuses a new connection whose `app.business_id` is
    already set (a default from the role, the database or the connection options), because the
    `RESET` would restore it. The refusal is fixed text and never contains the value.
- **A stricter canary gate** (M3 PR 3). `scripts/canary_gate.py` refuses any xfail under
  `tests.canary` that is not a pending `[HAND-WRITE]` mark (`PENDING_HAND_WRITE`), with a red
  proof. There is no allowlist of known gaps (that was D10 (b)).
- **Owner-connection clause checks in the suite** (M3 PR 3, before H1 `text_search`). The checks
  that run `vector_search`, `text_search` and the retire on approve (ADR 0010) on the owner's
  connection, where RLS cannot help, move into `tests/canary`. The gate then covers each
  statement's business clause, not only RLS.
