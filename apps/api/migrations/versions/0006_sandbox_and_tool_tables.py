"""The sandbox backend and the tool tables, each under forced row-level security (ADR 0016, M5).

Decided D6 (a), 2026-11-10: the sandbox is Postgres, not an in-memory fake, so the tools of PR 2-5
run against the same isolation (ADR 0004, ADR 0009) as everything else. Six tables:

- `sandbox_slots`: a fake calendar (bookable slots). Read-only to the app: a booking takes a slot by
  inserting a booking row, never by changing the slot.
- `sandbox_bookings`: the calendar's bookings, seeded (`origin = 'seed'`) or written by a confirmed
  action (`origin = 'action'`).
- `sandbox_orders`: a fake order store (Paws & Co's pet shop). Read-only to the app.
- `sandbox_tickets`: tickets for staff, written only by a confirmed `create_ticket` action (W4).
- `pending_actions`: a write the model proposed, waiting for the confirm call (D3, W2, W5, W6).
- `tool_audit`: one metadata-only row per tool call (W12). Append-only.

**Isolation, as 0002.** Every table has a non-null `business_id`, RLS ENABLED and FORCED, and the
one `business_isolation` policy for every command. Every unique key starts with `business_id`
(ADR 0009, ADR 0010: Postgres checks uniqueness across all rows, ignoring RLS, so a key without it
would tell one business that another's id exists). Slot, booking and order ids are text chosen by
the seed (`S-17`, `1043`), unique per `(business_id, id)`, so both businesses may hold the same
id: the canary bait relies on that. `pending_actions` and `tool_audit` keep a single random uuid
key (the canary's one exception, `checks._random_uuid_key`); `pending_actions` adds
`UNIQUE (business_id, id, customer_ref, status)` as the target of the write tables' composite
foreign keys (below).

**Composite foreign keys.** Foreign-key checks ignore RLS too. Bookings point at
`(business_id, slot_id)`, so a booking can only take its own business's slot, whatever the code
does (`tests/canary/checks.foreign_key_problems`). Every action column (`action_id`,
`retired_by_action_id`) points at `(business_id, id, customer_ref, status)` of `pending_actions`
[PR 1 review, diego and zara], paired with the row's own business and customer and a stored
generated column that is `'executed'` whenever the action column is set. So a booking or ticket
can only ever exist for an action of its own business, for its own customer (W5, W6: an action
for cust-001 cannot retire cust-002's booking), that was executed, which the CHECKs below allow
only after a confirmation (W2). These keys are `DEFERRABLE INITIALLY DEFERRED`, checked at
commit, so PR 3's confirm can write the row, audit, then mark the action executed in one
transaction; MATCH SIMPLE skips seed rows (their action column is NULL). As a side effect an
executed action that wrote a row can never be reopened: its referenced key would change.
`tool_audit.action_id` has no key on purpose: a refused confirm of an unknown id is still audited
(W12). No `ON DELETE CASCADE`: only the owner's
sandbox reset deletes, children first (`vectapilot_api.tools.sandbox`).

**Exactly once, in the schema (W5).** Write rows carry `origin` (`seed` or `action`) with
`action_id` set exactly for `action` (diego's stricter CHECK), and `UNIQUE (business_id,
action_id)`: one action can create at most one booking and one ticket. A partial unique index on
active bookings per slot makes a double booking impossible. `reschedule` retires the old booking
(`status = 'rescheduled'`, `retired_by_action_id` set, the two together by CHECK) and inserts the
new one; a partial unique index on `retired_by_action_id` stops one action from retiring two
bookings. `pending_actions` refuses an `executed` or `failed` row that was never confirmed
(`confirmed_at` and `confirm_trace_id` set): the confirm route (PR 3) is the only writer of those,
so the database itself refuses an executed action that skipped confirmation, and, through the
foreign keys above, a write row for an action that was never executed (W2).

**Grants (ADR 0016 decision 6), least privilege.** `SELECT` on slots and orders; `SELECT, INSERT`
on tickets and `tool_audit`; `SELECT, INSERT, UPDATE` on bookings and pending actions. INSERT and
UPDATE are granted on named columns only (`GRANTS` below) [proposal, PR 1 review]: the app role
can insert only a `pending` action and an `active` booking, never picks a uuid key or a
`created_at`, and cannot rewrite a stored action's arguments after its summary was shown. No
`DELETE` and no `TRUNCATE` anywhere: the app never removes a sandbox row, and `tool_audit` is
append-only. A restrictive insert policy (`ACTION_ORIGIN_POLICY`) keeps the app's write rows
`origin = 'action'`.

**Checks mirror the tool core** (written out here: migrations are frozen in time; tests compare
them with `vectapilot_api.tools`): id and customer patterns, the tool names, bounded free text. The
free-text columns (a slot's label, an order's items and note, a ticket's summary) are untrusted
data for every reader (rule 5, W10); some seeded values carry instruction-like text on purpose.
Bookings have no free-text column: whether a booking may hold a reason for the visit is open (Q7).

**Expiry.** `expires_at` has no default: the proposal service (PR 3) writes `now()` plus its
configured lifetime in SQL, so the database clock sets it (W6). The CHECK bounds it to after
`created_at` and at most one hour later, so a configuration typo cannot make a proposal live for
days.

**The downgrade** drops the six tables (their policies, grants and indexes go with them), children
first. It deletes every sandbox row, pending action and audit row: tested only inside a rolled-back
transaction (`tests/test_migrations.py`), never run on a database whose rows matter.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-10
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE: Final = "vectapilot_app"
CURRENT_BUSINESS: Final = "nullif(current_setting('app.business_id', true), '')"

TABLES: Final = (
    "sandbox_slots",
    "sandbox_orders",
    "pending_actions",
    "sandbox_bookings",
    "sandbox_tickets",
    "tool_audit",
)
"""Parents before children (the creation order); the downgrade drops them in reverse."""

RECORD_ID: Final = "^[A-Z0-9]+(-[A-Z0-9]+)*$"
RECORD_ID_MAX: Final = 32
CUSTOMER_REF: Final = "^[a-z0-9]+(-[a-z0-9]+)*$"
CUSTOMER_REF_LENGTH: Final = (3, 40)
CODE: Final = "^[a-z][a-z0-9_]{1,39}$"
"""A service id, a ticket category, an outcome or a failure reason."""
TRACE_ID: Final = "^[0-9a-f]{32}$"
"""As `gateway.types.TraceId` (W3C Trace Context); all zeros is refused there, not here."""
TOOLS: Final = ("book", "check_slots", "create_ticket", "order_status", "reschedule")
WRITE_TOOLS: Final = ("book", "create_ticket", "reschedule")
ORDER_STATUSES: Final = ("placed", "packed", "shipped", "delivered", "cancelled")
ACTION_STATUSES: Final = ("pending", "executed", "failed")
BOOKING_STATUSES: Final = ("active", "rescheduled")
AUDIT_PHASES: Final = ("run", "propose", "confirm")
MAX_ACTION_LIFETIME: Final = "1 hour"
SLOT_DURATION_MINUTES: Final = (5, 480)
LABEL_MAX: Final = 200
ITEMS_MAX: Final = 300
NOTE_MAX: Final = 500
SUMMARY_MAX: Final = 500
"""Free-text and duration bounds, as `vectapilot_api.tools` (a test compares)."""
EXECUTED: Final = "executed"


def _one_of(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _record_id(column: str) -> str:
    return f"{column} ~ '{RECORD_ID}' AND length({column}) <= {RECORD_ID_MAX}"


def _customer_ref(column: str) -> str:
    low, high = CUSTOMER_REF_LENGTH
    return f"{column} ~ '{CUSTOMER_REF}' AND length({column}) BETWEEN {low} AND {high}"


def _trace_id(column: str) -> str:
    return f"{column} ~ '{TRACE_ID}'"


def _origin_checks() -> str:
    """A write row's origin: `seed` rows have no action, `action` rows exactly one (W5)."""
    return (
        "origin text NOT NULL CHECK (origin IN ('seed', 'action')),"
        " action_id uuid,"
        " CHECK ((origin = 'seed') = (action_id IS NULL))"
    )


def _executed_when_set(column: str, action_column: str) -> str:
    """`column` is `'executed'` exactly when `action_column` is set: the status the referenced
    action must have. STORED (Postgres 18 makes a generated column virtual by default, and a
    virtual one cannot be part of a foreign key)."""
    return (
        f"{column} text GENERATED ALWAYS AS"
        f" (CASE WHEN {action_column} IS NOT NULL THEN '{EXECUTED}' END) STORED"
    )


def _executed_action_key(name: str, action_column: str, status_column: str) -> str:
    """A write row's action: same business, same customer, executed; checked at commit."""
    return (
        f"CONSTRAINT {name} FOREIGN KEY (business_id, {action_column}, customer_ref,"
        f" {status_column}) REFERENCES pending_actions (business_id, id, customer_ref, status)"
        " DEFERRABLE INITIALLY DEFERRED"
    )


DURATION_RANGE: Final = " AND ".join(map(str, SLOT_DURATION_MINUTES))
BOOKING_ACTION_KEY: Final = _executed_action_key(
    "sandbox_bookings_action_executed", "action_id", "action_status"
)
BOOKING_RETIRED_BY_KEY: Final = _executed_action_key(
    "sandbox_bookings_retired_by_executed", "retired_by_action_id", "retired_by_action_status"
)
TICKET_ACTION_KEY: Final = _executed_action_key(
    "sandbox_tickets_action_executed", "action_id", "action_status"
)

# One statement per op.execute: asyncpg sends each through the extended protocol, which takes one.
CREATE_TABLES: Final = (
    f"""
    CREATE TABLE sandbox_slots (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id text NOT NULL CHECK ({_record_id("id")}),
        service text NOT NULL CHECK (service ~ '{CODE}'),
        starts_at timestamptz NOT NULL,
        duration_minutes integer NOT NULL CHECK (duration_minutes BETWEEN {DURATION_RANGE}),
        label text NOT NULL CHECK (length(label) BETWEEN 1 AND {LABEL_MAX}),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id)
    )
    """,
    "CREATE INDEX sandbox_slots_business_starts ON sandbox_slots (business_id, starts_at)",
    f"""
    CREATE TABLE sandbox_orders (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id text NOT NULL CHECK ({_record_id("id")}),
        customer_ref text NOT NULL CHECK ({_customer_ref("customer_ref")}),
        status text NOT NULL CHECK (status IN ({_one_of(ORDER_STATUSES)})),
        items text NOT NULL CHECK (length(items) BETWEEN 1 AND {ITEMS_MAX}),
        note text CHECK (length(note) BETWEEN 1 AND {NOTE_MAX}),
        placed_at timestamptz NOT NULL,
        expected_delivery date,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id)
    )
    """,
    # A proposed write (D3). The single random uuid key is the action's id and its idempotency key
    # (D4); UNIQUE (business_id, id, customer_ref, status) is the write rows' foreign-key target.
    # Arguments are the validated tool input; customer_ref and business_id are context, never
    # arguments (W7).
    f"""
    CREATE TABLE pending_actions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id text NOT NULL REFERENCES businesses (business_id),
        tool text NOT NULL CHECK (tool IN ({_one_of(WRITE_TOOLS)})),
        arguments jsonb NOT NULL CHECK (jsonb_typeof(arguments) = 'object'),
        customer_ref text NOT NULL CHECK ({_customer_ref("customer_ref")}),
        confirm_token_sha256 text NOT NULL CHECK (confirm_token_sha256 ~ '^[0-9a-f]{{64}}$'),
        status text NOT NULL CHECK (status IN ({_one_of(ACTION_STATUSES)})),
        trace_id text CHECK ({_trace_id("trace_id")}),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        confirmed_at timestamptz,
        confirm_trace_id text CHECK ({_trace_id("confirm_trace_id")}),
        finished_at timestamptz,
        result jsonb CHECK (jsonb_typeof(result) = 'object'),
        failure_reason text CHECK (failure_reason ~ '{CODE}'),
        UNIQUE (business_id, id, customer_ref, status),
        CHECK (expires_at > created_at
               AND expires_at <= created_at + interval '{MAX_ACTION_LIFETIME}'),
        CHECK ((confirmed_at IS NULL) = (confirm_trace_id IS NULL)),
        CHECK ((status = 'pending') = (finished_at IS NULL)),
        CHECK ((status = 'pending') = (result IS NULL)),
        CHECK (status = 'pending' OR confirmed_at IS NOT NULL),
        CHECK ((status = 'failed') = (failure_reason IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE sandbox_bookings (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id text NOT NULL CHECK ({_record_id("id")}),
        slot_id text NOT NULL,
        customer_ref text NOT NULL CHECK ({_customer_ref("customer_ref")}),
        status text NOT NULL CHECK (status IN ({_one_of(BOOKING_STATUSES)})),
        {_origin_checks()},
        {_executed_when_set("action_status", "action_id")},
        retired_by_action_id uuid,
        {_executed_when_set("retired_by_action_status", "retired_by_action_id")},
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id),
        FOREIGN KEY (business_id, slot_id) REFERENCES sandbox_slots (business_id, id),
        {BOOKING_ACTION_KEY},
        {BOOKING_RETIRED_BY_KEY},
        UNIQUE (business_id, action_id),
        CHECK ((status = 'rescheduled') = (retired_by_action_id IS NOT NULL))
    )
    """,
    # No double booking: at most one active booking per slot, so slots stay read-only.
    "CREATE UNIQUE INDEX sandbox_bookings_one_active_per_slot"
    " ON sandbox_bookings (business_id, slot_id) WHERE status = 'active'",
    # One reschedule retires at most one booking.
    "CREATE UNIQUE INDEX sandbox_bookings_retired_once"
    " ON sandbox_bookings (business_id, retired_by_action_id)"
    " WHERE retired_by_action_id IS NOT NULL",
    "CREATE INDEX sandbox_bookings_business_customer"
    " ON sandbox_bookings (business_id, customer_ref)",
    f"""
    CREATE TABLE sandbox_tickets (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        customer_ref text NOT NULL CHECK ({_customer_ref("customer_ref")}),
        category text NOT NULL CHECK (category ~ '{CODE}'),
        summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND {SUMMARY_MAX}),
        {_origin_checks()},
        {_executed_when_set("action_status", "action_id")},
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id),
        {TICKET_ACTION_KEY},
        UNIQUE (business_id, action_id)
    )
    """,
    # Metadata only (W12, ADR 0007): never arguments, results, text or the token. action_id has
    # no foreign key: refusals of unknown ids are audited too.
    f"""
    CREATE TABLE tool_audit (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id text NOT NULL REFERENCES businesses (business_id),
        tool text NOT NULL CHECK (tool IN ({_one_of(TOOLS)})),
        kind text NOT NULL CHECK (kind IN ('read', 'write')),
        phase text NOT NULL CHECK (phase IN ({_one_of(AUDIT_PHASES)})),
        outcome text NOT NULL CHECK (outcome ~ '{CODE}'),
        duration_ms double precision NOT NULL CHECK (duration_ms >= 0),
        trace_id text CHECK ({_trace_id("trace_id")}),
        action_id uuid,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX tool_audit_business_created ON tool_audit (business_id, created_at)",
)

# Column lists, not whole tables: least privilege (ADR 0016 decision 6) [proposal, PR 1 review].
# INSERT leaves out every column the database fills (ids, created_at) and every column only a
# confirm may set, so the app role can only ever insert a `pending` action (the CHECKs refuse an
# `executed` or `failed` row without `confirmed_at`) and an `active` booking, and never chooses a
# uuid key. UPDATE names only what a confirm changes, so a stored action's arguments, customer,
# token hash and expiry, and a booking's slot and customer, cannot be rewritten after the summary
# was shown.
ACTION_INSERT_COLUMNS: Final = (
    "business_id",
    "tool",
    "arguments",
    "customer_ref",
    "confirm_token_sha256",
    "status",
    "trace_id",
    "expires_at",
)
ACTION_UPDATE_COLUMNS: Final = (
    "status",
    "confirmed_at",
    "confirm_trace_id",
    "finished_at",
    "result",
    "failure_reason",
)
BOOKING_INSERT_COLUMNS: Final = (
    "business_id",
    "id",
    "slot_id",
    "customer_ref",
    "status",
    "origin",
    "action_id",
)
BOOKING_UPDATE_COLUMNS: Final = ("status", "retired_by_action_id")
TICKET_INSERT_COLUMNS: Final = (
    "business_id",
    "customer_ref",
    "category",
    "summary",
    "origin",
    "action_id",
)
AUDIT_INSERT_COLUMNS: Final = (
    "business_id",
    "tool",
    "kind",
    "phase",
    "outcome",
    "duration_ms",
    "trace_id",
    "action_id",
)


def _columns(columns: Sequence[str]) -> str:
    return ", ".join(columns)


GRANTS: Final = (
    f"GRANT SELECT ON {', '.join(TABLES)} TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(ACTION_INSERT_COLUMNS)}) ON pending_actions TO {APP_ROLE}",
    f"GRANT UPDATE ({_columns(ACTION_UPDATE_COLUMNS)}) ON pending_actions TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(BOOKING_INSERT_COLUMNS)}) ON sandbox_bookings TO {APP_ROLE}",
    f"GRANT UPDATE ({_columns(BOOKING_UPDATE_COLUMNS)}) ON sandbox_bookings TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(TICKET_INSERT_COLUMNS)}) ON sandbox_tickets TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(AUDIT_INSERT_COLUMNS)}) ON tool_audit TO {APP_ROLE}",
)

ACTION_ORIGIN_TABLES: Final = ("sandbox_bookings", "sandbox_tickets")
ACTION_ORIGIN_POLICY: Final = "app_writes_are_actions"
"""A RESTRICTIVE policy, AND-ed with `business_isolation` [proposal, PR 1 review]: a row the app
role inserts into a write table is `origin = 'action'`, never `seed`. Seed rows come only from the
owner's reset, so an app-side insert cannot pass as seed data and escape the eval's count of
action rows (ADR 0016 decision 10). The canary's `policy_problems` checks permissive policies
only (they are OR-ed and could open a table); a restrictive one can only narrow it."""


def action_origin_policy(table: str) -> str:
    return (
        f"CREATE POLICY {ACTION_ORIGIN_POLICY} ON {table} AS RESTRICTIVE FOR INSERT"
        f" TO {APP_ROLE} WITH CHECK (origin = 'action')"
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
    *(action_origin_policy(table) for table in ACTION_ORIGIN_TABLES),
    *GRANTS,
)
DOWNGRADE: Final = (f"DROP TABLE {', '.join(reversed(TABLES))}",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
