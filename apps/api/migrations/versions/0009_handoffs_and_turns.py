"""Hand-offs, widget turns and turn feedback, under forced row-level security (ADR 0019, M8 PR 2;
D6 (a), D7 (a), S11, S12, S17).

Decided D6 (a), 2026-11-24: one hand-off row per session's open hand-off, a metadata-only row per
widget turn, and feedback bound to the session's own turns. Three tables:

- `handoffs`: the first place customer text rests (S11). One row per session's open hand-off
  (S17): its origin (`turn`, a message the guards or the service handed off; `button`, "talk to a
  person"), reason, priority, intent, `count` (how many hand-offs the session asked for while the
  row was open), the guidance lines the customer was shown (as sent, never re-read later), the
  message masked by S11's helper (`handoffs.at_rest`, NULL when masking failed or the masked text
  is over `MESSAGE_MAX`: never raw, never truncated), and the staff side, worked by PR 3: the
  status, who claimed and resolved it, the escalation label and one reply (D7 (a)). The
  session's channel and UI language at its start are copied in (§6's breakdown, never the
  detected message language). `pressed_at` records the session's first "talk to a person" while
  the row was open, and survives a later raise (noah's PR 2 review: "the customer asked for a
  person" is M11's signal for a missed escalation).
- `widget_turns`: one row per widget turn, no text (D6 (a)): its trace id, the response's kind, a
  hand-off's reason, priority and intent, an answer's outcome (`answered`, `dont_know`,
  `handoff`: an answer and an abstention are told apart, noah's PR 2 review), and the server's
  latency in milliseconds, from when the route started (before the session lookup) to the
  response built, the store write and serialization excluded. The denominator §6's "resolved" and
  thumbs-up rate need, and M9's per-conversation key; a lower bound of the turns served (a store
  that fails writes no row, `widget_store_failed`).
- `turn_feedback`: one rating (`up`, `down`) per turn, only for the rating session's own turn
  (PR 2b's route; the database pairs the session too, below).

**One open hand-off per session (S17).** A red-flag message skips the limiter (M7 G1) and "talk
to a person" has none, so a partial unique index on `(business_id, session_id) WHERE status <>
'resolved'` bounds the queue by sessions, and sessions by the creation window. A later hand-off
on that session increments `count`; when it ranks higher (priority, then reason class: safety >
decision > system; S17 amended 2026-11-25, shesha, Z3 (a)) it replaces the row's reason, origin,
intent, trace id, message and guidance and sets `raised_at` (the rank only ever rises; a claimed
row stays claimed and shows the raise). One statement does both under concurrency
(`INSERT ... ON CONFLICT ... DO UPDATE`, `handoffs.store`). A resolved row leaves the index, so the
session's next hand-off opens a new row.

**Isolation, as 0006-0008.** Every table has a non-null `business_id` leading its primary key and
every unique key, RLS ENABLED and FORCED, and the one `business_isolation` policy. Every id is a
uuid the database draws. Every reference is a composite foreign key that pairs the business
(foreign-key checks ignore RLS): a hand-off's and a turn's session, a hand-off's staff members,
and a rating's turn **and session**, `(business_id, session_id, trace_id)` into `widget_turns`,
so a session can only ever rate its own turn, by the database's own rule [proposal: the plan
names the turn and the business; the session joins the key]. No `ON DELETE CASCADE`: the app
deletes nothing, and the owner deletes children first.

**Hidden at expiry (S11, P5).** `handoffs.expires_at` is a STORED generated column, `created_at`
plus the business's `retention_days` (1-90, the widget file's `handoff_retention_days`), computed
in UTC as 0007's. A restrictive `SELECT` policy for the app role hides an expired hand-off. A
hand-off can never expire while its session lives: a session lives at most 24 hours (0008's
CHECK) and a hand-off at least a day, created after its session. The owner's purge (by hand, PR
3) NULLs the text columns (`message_masked`, `guidance_shown`, `reply`) and keeps the metadata, so
labels outlive retention for M11. Turns and ratings hold no text and do not expire in M8.

**A resolved hand-off is final for the app role** [proposal]: a RESTRICTIVE `UPDATE` policy
`resolved_is_final` (`USING (status <> 'resolved') WITH CHECK (true)`) lets no statement of the app
role reach a resolved row, so its label and reply, which outlive retention for M11, can never be
rewritten or the row reopened; the resolve itself passes (its old row is not resolved, and the
explicit `WITH CHECK` keeps Postgres from applying `USING` to the new row). The S17 statement never
reaches a resolved row (its conflict target excludes them).

**Grants, least privilege (S12).** `SELECT` on all three. `handoffs`: `INSERT` on the columns a
new hand-off names, `UPDATE` on S17's raise columns, the press's `pressed_at` and PR 3's staff
columns (status, claim, resolve, label, reply), never `business_id`, `session_id`, `channel`,
`ui_language`, `retention_days`, `id` or `created_at`. `widget_turns` and `turn_feedback`:
`INSERT` on named columns only. No `DELETE` and no `TRUNCATE` anywhere.

**Checks mirror the code** (written out here: migrations are frozen in time;
tests/test_migrations.py compares them with `vectapilot_api`): the reasons are
`agent.responses.HandoffReason`'s, the system reasons `tooleval.metrics`'s
`INFRASTRUCTURE_HANDOFF_REASONS`, the priorities `intents.policy.Priority`'s, the intents
`intents.taxonomy.Intent`'s, the kinds `/v1/agent`'s four, the answer outcomes
`answer.types.AnswerOutcome`'s, the trace id W3C's form, the UI language 0008's pattern, the
bounds the code's. A button hand-off is `customer_asked` and has its press time; a label is set
only at resolve and only on a decision hand-off a turn made (never on a system reason or a button
press, PR 3's metric). A turn has a reason and a priority exactly when it is a hand-off, an intent
only then, and an answer outcome exactly when it is an answer.

**The downgrade** drops the three tables (their policies, grants and indexes go with them),
children first. It deletes every hand-off, turn and rating: tested only inside a rolled-back
transaction (`tests/test_migrations.py`), never run on a database whose rows matter.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-24
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE: Final = "vectapilot_app"
CURRENT_BUSINESS: Final = "nullif(current_setting('app.business_id', true), '')"

TABLES: Final = ("handoffs", "widget_turns", "turn_feedback")
"""Parent before children (the creation order); the downgrade drops them in reverse."""

TRACE_ID: Final = "^[0-9a-f]{32}$"
"""As `gateway.types.TraceId` (W3C Trace Context); all zeros is refused there, not here."""
ORIGINS: Final = ("turn", "button")
REASONS: Final = (
    "customer_asked",
    "safety",
    "other",
    "step_cap",
    "tool_failure",
    "invalid_model_output",
    "model_unavailable",
    "turn_timeout",
    "safety_floor_off",
    "agent_error",
    "proposal_not_stored",
    "not_configured",
    "routed",
    "router_failed",
    "guard_error",
    "guard_output",
    "budget",
    "guards_not_configured",
    "not_covered_language",
)
"""`agent.responses.HandoffReason`, in its order."""
SYSTEM_REASONS: Final = (
    "agent_error",
    "budget",
    "guard_error",
    "guard_output",
    "guards_not_configured",
    "invalid_model_output",
    "model_unavailable",
    "not_configured",
    "not_covered_language",
    "proposal_not_stored",
    "router_failed",
    "safety_floor_off",
    "step_cap",
    "tool_failure",
    "turn_timeout",
)
"""`tooleval.metrics.INFRASTRUCTURE_HANDOFF_REASONS`, sorted: the system fell back, nobody decided
about the message. Never labelled (PR 3's escalation-precision metric counts decisions only)."""
BUTTON_REASON: Final = "customer_asked"
PRIORITIES: Final = ("urgent", "high", "normal")
"""`intents.policy.Priority`, most urgent first."""
RISING: Final = ("normal", "high", "urgent")
"""The same priorities, least urgent first: `array_position` over it ranks a priority (S17)."""
INTENTS: Final = (
    "emergency",
    "professional_judgement",
    "complaint",
    "refund_or_payment",
    "human_request",
    "action_request",
    "question",
    "out_of_scope",
)
"""`intents.taxonomy.Intent`, in its order."""
STATUSES: Final = ("open", "claimed", "resolved")
LABELS: Final = ("needed", "not_needed", "unsure")
"""PR 3's escalation label: "staff agree it was needed" (§6)."""
KINDS: Final = ("reply", "confirmation_required", "handoff", "answer")
"""`/v1/agent`'s four response kinds (`agent.responses.AgentResponse`)."""
ANSWER_OUTCOMES: Final = ("answered", "dont_know", "handoff")
"""`answer.types.AnswerOutcome`, in its order: an `answer` turn's outcome."""
RATINGS: Final = ("up", "down")
CHANNELS: Final = ("web",)
"""Migration 0008's `sessions.channel`."""
UI_LANGUAGE: Final = "^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$"
"""Migration 0008's `UI_LANGUAGE` (`auth.store.UI_LANGUAGE_PATTERN`)."""
UI_LANGUAGE_MAX: Final = 35
MESSAGE_MAX: Final = 2_000
"""`retrieval.types.MAX_QUESTION_CHARS`: the masked message's bound (`handoffs.at_rest`); a longer
masked text is stored as NULL, never truncated (S11)."""
GUIDANCE_LINES_MAX: Final = 10
"""`agent.responses.GUIDANCE_LINES_MAX`."""
REPLY_MAX: Final = 2_000
"""One staff reply's bound (D7 (a)) [proposal: the message's own bound]."""
RETENTION_DAYS: Final = (1, 90)
"""P5, `widget.config.RETENTION_DAYS`."""
LATENCY_MAX_MS: Final = 600_000
"""Ten minutes [proposal]: far above the route's outer bound (`widget_turn_bound_seconds`, at most
120 s) plus the session lookup's (at most 10 s), so a value past it is a bug, never a slow turn."""


def _one_of(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


EXPIRES_AT: Final = "date_add(created_at, make_interval(days => retention_days), 'UTC')"
RETENTION_RANGE: Final = " AND ".join(map(str, RETENTION_DAYS))
# A flat array of at most GUIDANCE_LINES_MAX lines, none NULL and none empty. `IS TRUE`: a CHECK
# passes on NULL, and `x <> ALL (array)` is NULL when the array holds a NULL.
GUIDANCE_CHECK: Final = (
    "guidance_shown IS NULL OR (coalesce(array_ndims(guidance_shown), 1) = 1"
    f" AND cardinality(guidance_shown) <= {GUIDANCE_LINES_MAX}"
    " AND array_position(guidance_shown, NULL) IS NULL"
    " AND ('' <> ALL (guidance_shown)) IS TRUE)"
)
# A label only at resolve, and only on a hand-off someone decided on through a turn (PR 3).
LABEL_CHECK: Final = (
    "staff_label IS NULL OR (status = 'resolved' AND origin = 'turn'"
    f" AND reason NOT IN ({_one_of(SYSTEM_REASONS)}))"
)

# One statement per op.execute: asyncpg sends each through the extended protocol, which takes one.
CREATE_TABLES: Final = (
    f"""
    CREATE TABLE handoffs (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL,
        trace_id text NOT NULL CHECK (trace_id ~ '{TRACE_ID}'),
        origin text NOT NULL CHECK (origin IN ({_one_of(ORIGINS)})),
        reason text NOT NULL CHECK (reason IN ({_one_of(REASONS)})),
        priority text NOT NULL CHECK (priority IN ({_one_of(PRIORITIES)})),
        count integer NOT NULL DEFAULT 1 CHECK (count >= 1),
        intent text CHECK (intent IN ({_one_of(INTENTS)})),
        channel text NOT NULL CHECK (channel IN ({_one_of(CHANNELS)})),
        ui_language text NOT NULL CHECK (ui_language ~ '{UI_LANGUAGE}'
                                         AND length(ui_language) <= {UI_LANGUAGE_MAX}),
        guidance_shown text[],
        message_masked text CHECK (length(message_masked) BETWEEN 1 AND {MESSAGE_MAX}),
        status text NOT NULL DEFAULT 'open' CHECK (status IN ({_one_of(STATUSES)})),
        claimed_by uuid,
        claimed_at timestamptz,
        resolved_by uuid,
        resolved_at timestamptz,
        staff_label text CHECK (staff_label IN ({_one_of(LABELS)})),
        reply text CHECK (length(reply) BETWEEN 1 AND {REPLY_MAX}),
        replied_at timestamptz,
        raised_at timestamptz,
        pressed_at timestamptz,
        retention_days integer NOT NULL CHECK (retention_days BETWEEN {RETENTION_RANGE}),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL GENERATED ALWAYS AS ({EXPIRES_AT}) STORED,
        PRIMARY KEY (business_id, id),
        FOREIGN KEY (business_id, session_id) REFERENCES sessions (business_id, id),
        FOREIGN KEY (business_id, claimed_by) REFERENCES staff_users (business_id, id),
        FOREIGN KEY (business_id, resolved_by) REFERENCES staff_users (business_id, id),
        CHECK ({GUIDANCE_CHECK}),
        CHECK (origin <> 'button' OR reason = '{BUTTON_REASON}'),
        CHECK (origin <> 'button' OR pressed_at IS NOT NULL),
        CHECK ({LABEL_CHECK}),
        CHECK ((claimed_by IS NULL) = (claimed_at IS NULL)),
        CHECK ((resolved_by IS NULL) = (resolved_at IS NULL)),
        CHECK ((status = 'resolved') = (resolved_at IS NOT NULL)),
        CHECK (status <> 'claimed' OR claimed_at IS NOT NULL),
        CHECK (status <> 'open' OR claimed_at IS NULL),
        CHECK ((reply IS NULL) = (replied_at IS NULL)),
        CHECK (raised_at IS NULL OR raised_at >= created_at),
        CHECK (pressed_at IS NULL OR pressed_at >= created_at)
    )
    """,
    # S17: one open hand-off per session; `business_id` first (ADR 0009: uniqueness ignores RLS).
    "CREATE UNIQUE INDEX handoffs_one_open_per_session ON handoffs (business_id, session_id)"
    " WHERE status <> 'resolved'",
    # PR 3's queue (status, then priority and age) and the purge's expiry, per business.
    "CREATE INDEX handoffs_queue ON handoffs (business_id, status, priority, created_at)",
    "CREATE INDEX handoffs_business_expires ON handoffs (business_id, expires_at)",
    f"""
    CREATE TABLE widget_turns (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL,
        trace_id text NOT NULL CHECK (trace_id ~ '{TRACE_ID}'),
        kind text NOT NULL CHECK (kind IN ({_one_of(KINDS)})),
        reason text CHECK (reason IN ({_one_of(REASONS)})),
        priority text CHECK (priority IN ({_one_of(PRIORITIES)})),
        intent text CHECK (intent IN ({_one_of(INTENTS)})),
        answer_outcome text CHECK (answer_outcome IN ({_one_of(ANSWER_OUTCOMES)})),
        channel text NOT NULL CHECK (channel IN ({_one_of(CHANNELS)})),
        ui_language text NOT NULL CHECK (ui_language ~ '{UI_LANGUAGE}'
                                         AND length(ui_language) <= {UI_LANGUAGE_MAX}),
        server_latency_ms integer NOT NULL
            CHECK (server_latency_ms BETWEEN 0 AND {LATENCY_MAX_MS}),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id),
        UNIQUE (business_id, trace_id),
        UNIQUE (business_id, session_id, trace_id),
        FOREIGN KEY (business_id, session_id) REFERENCES sessions (business_id, id),
        CHECK ((kind = 'handoff') = (reason IS NOT NULL)),
        CHECK ((kind = 'handoff') = (priority IS NOT NULL)),
        CHECK (kind = 'handoff' OR intent IS NULL),
        CHECK ((kind = 'answer') = (answer_outcome IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE turn_feedback (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        session_id uuid NOT NULL,
        trace_id text NOT NULL CHECK (trace_id ~ '{TRACE_ID}'),
        rating text NOT NULL CHECK (rating IN ({_one_of(RATINGS)})),
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (business_id, id),
        UNIQUE (business_id, trace_id),
        FOREIGN KEY (business_id, session_id, trace_id)
            REFERENCES widget_turns (business_id, session_id, trace_id)
    )
    """,
)

# Column lists, not whole tables (as 0006-0008): the app role never chooses an id, `created_at`,
# `expires_at` (generated) or, at insert, any staff column.
HANDOFF_INSERT_COLUMNS: Final = (
    "business_id",
    "session_id",
    "trace_id",
    "origin",
    "reason",
    "priority",
    "intent",
    "channel",
    "ui_language",
    "guidance_shown",
    "message_masked",
    "retention_days",
    "pressed_at",
)
HANDOFF_RAISE_COLUMNS: Final = (
    "count",
    "priority",
    "reason",
    "origin",
    "intent",
    "trace_id",
    "guidance_shown",
    "message_masked",
    "raised_at",
)
"""S17: what a later hand-off on the session's open row may change."""
HANDOFF_PRESS_COLUMNS: Final = ("pressed_at",)
""""Talk to a person" on an open row: its first press's time, nothing else."""
HANDOFF_STAFF_COLUMNS: Final = (
    "status",
    "claimed_by",
    "claimed_at",
    "resolved_by",
    "resolved_at",
    "staff_label",
    "reply",
    "replied_at",
)
"""PR 3: claim, resolve, the label and the reply (D7 (a))."""
TURN_INSERT_COLUMNS: Final = (
    "business_id",
    "session_id",
    "trace_id",
    "kind",
    "reason",
    "priority",
    "intent",
    "answer_outcome",
    "channel",
    "ui_language",
    "server_latency_ms",
)
FEEDBACK_INSERT_COLUMNS: Final = ("business_id", "session_id", "trace_id", "rating")


def _columns(columns: Sequence[str]) -> str:
    return ", ".join(columns)


GRANTS: Final = (
    f"GRANT SELECT ON {', '.join(TABLES)} TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(HANDOFF_INSERT_COLUMNS)}) ON handoffs TO {APP_ROLE}",
    f"GRANT UPDATE"
    f" ({_columns((*HANDOFF_RAISE_COLUMNS, *HANDOFF_PRESS_COLUMNS, *HANDOFF_STAFF_COLUMNS))})"
    f" ON handoffs TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(TURN_INSERT_COLUMNS)}) ON widget_turns TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(FEEDBACK_INSERT_COLUMNS)}) ON turn_feedback TO {APP_ROLE}",
)

EXPIRY_POLICY: Final = (
    f"CREATE POLICY hidden_at_expiry ON handoffs AS RESTRICTIVE FOR SELECT TO {APP_ROLE}"
    " USING (expires_at > now())"
)
"""A RESTRICTIVE `SELECT` policy for the app role, AND-ed with `business_isolation` (S11). An
UPDATE never changes `expires_at` (generated from columns the app cannot update), so S17's and PR
3's `UPDATE ... RETURNING` pass it (migration 0008's note on SELECT policies and new rows)."""
FINAL_POLICY: Final = (
    f"CREATE POLICY resolved_is_final ON handoffs AS RESTRICTIVE FOR UPDATE TO {APP_ROLE}"
    " USING (status <> 'resolved') WITH CHECK (true)"
)
"""[proposal] The app role's UPDATE never reaches a resolved hand-off (module docstring). The
explicit `WITH CHECK (true)` matters: an UPDATE policy without one applies its USING to the new
row too, which would refuse the resolve itself (its new row is resolved)."""


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
    EXPIRY_POLICY,
    FINAL_POLICY,
    *GRANTS,
)
DOWNGRADE: Final = (f"DROP TABLE {', '.join(reversed(TABLES))}",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
