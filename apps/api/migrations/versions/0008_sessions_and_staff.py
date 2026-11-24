"""Sessions and staff, under forced row-level security (ADR 0019, M8 PR 1; D1 (a), D4 (a)).

Decided D1 (a), 2026-11-24: sessions are rows in Postgres under RLS, never in Redis or in a signed
client token. Three tables:

- `staff_users`: one row per staff member of one business (S5, D4 (a)): a synthetic display name,
  a role (`reviewer` or `admin`, P2) and `disabled_at`. Written only by the owner CLI (rule 5:
  staff rows live in the database only); the app role reads them on every staff request, so a
  disabled member or a changed role takes effect at once.
- `staff_login_links`: a one-time login link (D4 (a)): its token's SHA-256, the staff member it
  logs in, `expires_at` (15 minutes by default, P1; at most an hour by CHECK) and `used_at`. Issued
  by the owner CLI; the app role only marks one used, in S5's one atomic statement.
- `sessions`: one row per customer or staff session (S3): its kind, the staff member exactly for
  a staff session, the token's SHA-256, the channel and the UI language at session start (§6's
  breakdown, carried by every session row), and `created_at`, `expires_at` and `revoked_at`.

**Tokens are never stored** (S3): only the SHA-256 of `<business_id>.<secret>`
(`vectapilot_api.auth.tokens`), 64 lowercase hex digits, as `pending_actions.confirm_token_sha256`.
`UNIQUE (business_id, token_sha256)`: the lookup's key, `business_id` first.

**Isolation, as 0006 and 0007.** Every table has a non-null `business_id` leading its primary key
(the canary can plant the same id in two businesses), RLS ENABLED and FORCED, and the one
`business_isolation` policy for every command. A uuid id is drawn by the database and never
chosen by the app role (column grants below). A session's or link's staff member is a composite
foreign key `(business_id, staff_id)` into `staff_users` (foreign-key checks ignore RLS, so a
key on `staff_id` alone would let one business point at, and probe for, another's staff); MATCH
SIMPLE skips a customer session's NULL `staff_id`. No `ON DELETE CASCADE`: nothing here is ever
deleted by the app, and the owner deletes children first.

**Hidden at expiry: a restrictive policy for expiry, the lookup's own clause for the rest**
(decided here, PR 1, "verified against Postgres": `tests/test_migrations.py`). A restrictive
`SELECT` policy for the app role, AND-ed with `business_isolation` as 0007's `hidden_at_expiry`,
hides a session or a link once `expires_at` has passed. So any statement of the app role, a later
one that forgets its clause included, can never authenticate with an expired row. Every lookup
still writes `expires_at > now()` itself: on the owner's connection (the canary's
`OWNER_CONNECTION_PATHS`) no policy applies, and only the statement's own clause stands.
For reading, revocation (`revoked_at IS NULL`) and use (`used_at IS NULL`) are **only** in the
lookup's clause, never in a `SELECT` policy: Postgres also checks the `SELECT` policies against
the *new* row of an UPDATE that reads the table (a WHERE on its columns or a RETURNING:
`rowsecurity.c`, "SELECT rights are also required", added as WITH CHECK options), so a `SELECT`
policy `revoked_at IS NULL` would make S5's
`UPDATE ... SET revoked_at = now() ... RETURNING` fail with SQLSTATE 42501 ("new row violates
row-level security policy") instead of revoking, and the same for `used_at`. An expiry-only
policy passes there: the update changes neither `expires_at` nor `now()` (one transaction, one
`now()`). `tests/test_migrations.py` runs both, the refusal included.

**Revocation and use are one-way: restrictive `UPDATE` policies** (zara's review, PR 1). The
column grants let the app role write any value into `revoked_at` and `used_at`, NULL included, so
without more a single statement could revive a logged-out token (`SET revoked_at = NULL`) or a
used link (`SET used_at = NULL`). `revoke_once` and `use_once`, RESTRICTIVE `FOR UPDATE` for the
app role, AND-ed with `business_isolation` as 0006's `app_writes_are_actions`: `USING` lets an
UPDATE reach only a row that is still unrevoked (unused), and `WITH CHECK` lets it leave only a
revoked (used) one. These are `UPDATE` policies, not `SELECT` ones, so the trap above does not
apply: `USING` silently filters the old row (an un-revoke updates nothing) and `WITH CHECK` checks
the new row alone (clearing the column on a live row is SQLSTATE 42501). The logout's and S5's
statements already carry the same predicates and pass unchanged.

**Grants (S5, S12), least privilege.** `staff_users`: `SELECT` only. `staff_login_links`:
`SELECT` and `UPDATE (used_at)`, used only by S5's statement. `sessions`: `SELECT`, `INSERT` on
named columns (never `id`, `created_at` or `revoked_at`: the database draws the id and its clock
sets `created_at`; the app writes `expires_at` as `now()` plus a configured lifetime in SQL, as
0006's `pending_actions`) and `UPDATE (revoked_at)`. No `DELETE` and no `TRUNCATE` anywhere: logout
revokes, it never deletes.

**Checks mirror the auth core** (written out here: migrations are frozen in time;
tests/test_migrations.py compares them with `vectapilot_api.auth`): the kinds, roles and channels,
the hash's form, the UI language's pattern and length, the display name's length, and the
lifetimes' upper bounds (a session at most 24 hours, P1's 12 h and 8 h inside it; a link at most
an hour, P1's 15 minutes inside it), so a configuration typo cannot make a credential live for
days. A staff session has its staff member and a customer session has none; a customer session
has a UI language and a staff session has none [proposal: the staff dashboard has no language
choice in M8]. A link is never used after its expiry (`used_at < expires_at`: S5's statement sets
`used_at = now()` only where `expires_at > now()`).

**The downgrade** drops the three tables (their policies, grants and indexes go with them),
children first. It deletes every session, link and staff member: tested only inside a rolled-back
transaction (`tests/test_migrations.py`), never run on a database whose rows matter.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-24
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE: Final = "vectapilot_app"
CURRENT_BUSINESS: Final = "nullif(current_setting('app.business_id', true), '')"

TABLES: Final = ("staff_users", "staff_login_links", "sessions")
"""Parent before children (the creation order); the downgrade drops them in reverse."""

SHA256: Final = "^[0-9a-f]{64}$"
SESSION_KINDS: Final = ("customer", "staff")
STAFF_ROLES: Final = ("reviewer", "admin")
CHANNELS: Final = ("web",)
UI_LANGUAGE: Final = "^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
"""`auth.store.UI_LANGUAGE_PATTERN`: a language tag (`en`, `hi`, `te`); the widget configuration's
list of languages is checked in code."""
UI_LANGUAGE_MAX: Final = 35
DISPLAY_NAME_MAX: Final = 80
MAX_SESSION_LIFETIME: Final = "24 hours"
MAX_LINK_LIFETIME: Final = "1 hour"


def _one_of(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _sha256(column: str) -> str:
    return f"{column} ~ '{SHA256}'"


def _lifetime(maximum: str) -> str:
    """`expires_at` after `created_at` and at most `maximum` later."""
    return f"expires_at > created_at AND expires_at <= created_at + interval '{maximum}'"


# One statement per op.execute: asyncpg sends each through the extended protocol, which takes one.
CREATE_TABLES: Final = (
    f"""
    CREATE TABLE staff_users (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        display_name text NOT NULL
            CHECK (length(display_name) BETWEEN 1 AND {DISPLAY_NAME_MAX}),
        role text NOT NULL CHECK (role IN ({_one_of(STAFF_ROLES)})),
        created_at timestamptz NOT NULL DEFAULT now(),
        disabled_at timestamptz,
        PRIMARY KEY (business_id, id)
    )
    """,
    f"""
    CREATE TABLE staff_login_links (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        staff_id uuid NOT NULL,
        token_sha256 text NOT NULL CHECK ({_sha256("token_sha256")}),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        used_at timestamptz,
        PRIMARY KEY (business_id, id),
        UNIQUE (business_id, token_sha256),
        FOREIGN KEY (business_id, staff_id) REFERENCES staff_users (business_id, id),
        CHECK ({_lifetime(MAX_LINK_LIFETIME)}),
        CHECK (used_at IS NULL OR used_at < expires_at)
    )
    """,
    f"""
    CREATE TABLE sessions (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        kind text NOT NULL CHECK (kind IN ({_one_of(SESSION_KINDS)})),
        staff_id uuid,
        token_sha256 text NOT NULL CHECK ({_sha256("token_sha256")}),
        channel text NOT NULL CHECK (channel IN ({_one_of(CHANNELS)})),
        ui_language text CHECK (ui_language ~ '{UI_LANGUAGE}'
                                AND length(ui_language) <= {UI_LANGUAGE_MAX}),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz,
        PRIMARY KEY (business_id, id),
        UNIQUE (business_id, token_sha256),
        FOREIGN KEY (business_id, staff_id) REFERENCES staff_users (business_id, id),
        CHECK ((kind = 'staff') = (staff_id IS NOT NULL)),
        CHECK ((kind = 'customer') = (ui_language IS NOT NULL)),
        CHECK ({_lifetime(MAX_SESSION_LIFETIME)})
    )
    """,
)

# Column lists, not whole tables (as 0006 and 0007): the app role never chooses a session's id,
# its `created_at` or `revoked_at` at insert, and changes only `revoked_at` afterwards.
SESSION_INSERT_COLUMNS: Final = (
    "business_id",
    "kind",
    "staff_id",
    "token_sha256",
    "channel",
    "ui_language",
    "expires_at",
)
SESSION_UPDATE_COLUMNS: Final = ("revoked_at",)
LINK_UPDATE_COLUMNS: Final = ("used_at",)


def _columns(columns: Sequence[str]) -> str:
    return ", ".join(columns)


GRANTS: Final = (
    f"GRANT SELECT ON {', '.join(TABLES)} TO {APP_ROLE}",
    f"GRANT UPDATE ({_columns(LINK_UPDATE_COLUMNS)}) ON staff_login_links TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(SESSION_INSERT_COLUMNS)}) ON sessions TO {APP_ROLE}",
    f"GRANT UPDATE ({_columns(SESSION_UPDATE_COLUMNS)}) ON sessions TO {APP_ROLE}",
)

EXPIRING_TABLES: Final = ("staff_login_links", "sessions")
EXPIRY_POLICY: Final = "hidden_at_expiry"
"""A RESTRICTIVE `SELECT` policy for the app role, AND-ed with `business_isolation` (module
docstring): expiry only, never revocation or use."""


def expiry_policy(table: str) -> str:
    return (
        f"CREATE POLICY {EXPIRY_POLICY} ON {table} AS RESTRICTIVE FOR SELECT TO {APP_ROLE}"
        " USING (expires_at > now())"
    )


ONE_WAY_POLICIES: Final = (
    ("sessions", "revoke_once", "revoked_at"),
    ("staff_login_links", "use_once", "used_at"),
)
"""(table, policy, column): RESTRICTIVE `UPDATE` policies for the app role (module docstring):
the column can only go from NULL to a time, never back and never twice."""


def one_way_policy(table: str, name: str, column: str) -> str:
    return (
        f"CREATE POLICY {name} ON {table} AS RESTRICTIVE FOR UPDATE TO {APP_ROLE}"
        f" USING ({column} IS NULL) WITH CHECK ({column} IS NOT NULL)"
    )


def row_security(table: str) -> tuple[str, ...]:
    """0002's three statements for one table: RLS on, forced, and the one business policy."""
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY business_isolation ON {table}"
        f" USING (business_id = {CURRENT_BUSINESS})"
        f" WITH CHECK (business_id = {CURRENT_BUSINESS})",
    )


UPGRADE: Final = (
    *CREATE_TABLES,
    *(statement for table in TABLES for statement in row_security(table)),
    *(expiry_policy(table) for table in EXPIRING_TABLES),
    *(one_way_policy(*policy) for policy in ONE_WAY_POLICIES),
    *GRANTS,
)
DOWNGRADE: Final = (f"DROP TABLE {', '.join(reversed(TABLES))}",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
