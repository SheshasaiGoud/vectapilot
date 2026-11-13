"""The extraction tables, under forced row-level security and hidden at expiry (ADR 0017, M6 PR 3).

Decided D6 (a), 2026-11-11: minimal storage. Two tables:

- `extractions`: one row per uploaded document. Metadata only: the kind, the template, prompt and
  rules versions, the status (`complete` exactly when no field is in review, D5 (a)), E3's failure
  if any, the file's SHA-256, its pages and the pages with an image (E4), the trace id and the
  retention that applied. **No column holds the file or its text** (E10): only the hash, as for
  KB documents.
- `extraction_fields`: one row per template field: its status, level and reasons, and only what
  D6 (a) keeps, the value, its page and the quote's label-to-value span (at most 40 characters),
  never the rest of a quote, which may hold text near the value the business must not keep.
  Values and spans are untrusted data for every reader (rule 5, E7).

**Isolation, as 0002 and 0006.** Both tables have a non-null `business_id`, RLS ENABLED and
FORCED, and the one `business_isolation` policy for every command. Every key starts with
`business_id` (ADR 0009: Postgres checks uniqueness across all rows, ignoring RLS). An extraction's
id is a uuid the database draws, unique per `(business_id, id)`, so the canary can plant the same
id in both businesses; the app role never chooses it (grants below). A field points at
`(business_id, extraction_id)` of its extraction (a composite foreign key: foreign-key checks
ignore RLS too, so a field can only belong to its own business's extraction), `ON DELETE CASCADE`
so the purge deletes an extraction with its fields in one statement.

**Hidden at expiry (E10).** `expires_at` is a STORED generated column, `created_at` plus the
business's `retention_days` (1-90, `config.RETENTION_DAYS`), computed in UTC so a day is always
24 hours (`date_add` with a time zone is immutable; `timestamptz + interval` alone is not, so it
could not be generated). The app inserts the retention it read from the business file and never
writes `created_at` or `expires_at`: the database clock sets both, as 0006's `pending_actions`
expiry. A restrictive `SELECT` policy for the app role, AND-ed with `business_isolation`, hides an
expired extraction, and a field whose extraction is expired (an `EXISTS` on the parent, so the two
can never disagree). The canary's `policy_problems` checks permissive policies only (they are
OR-ed and could open a table); a restrictive one can only narrow it. Expired rows stay until the
owner's manual purge (`python -m vectapilot_api.extraction purge`, one business at a time);
nothing schedules it until M9.

**Grants, least privilege (ADR 0017 decision 6).** `SELECT` on both tables, `INSERT` on named
columns only, as 0006: never an id, `created_at` or `expires_at`. No `UPDATE`, `DELETE` or
`TRUNCATE`: M6 stores the review queue, it does not work it (M8's review decisions get their own
migration), and only the owner's purge deletes.

**Checks mirror the extraction core** (written out here: migrations are frozen in time;
tests/test_migrations.py compares them with `vectapilot_api.extraction`): kinds and field names
are snake case, statuses, levels, reasons and failures are its closed vocabularies, a page is
within E12's page limit, a value within the longest field bound (`TEXT_MAX`: the service withholds
a longer one, `service.STORED_VALUE_MAX`), a span within `SPAN_MAX`. The service never returns an
outcome these refuse (`service.storable`), so a refusal is a bug, never a hostile value. Each
field row is one of `FieldDecision`'s shapes: `accepted` is `high` with a value and no reason,
`absent` has no level, reason, value, span or page, `review` has at least one reason, and only a
value has a level (`invariants.py`). A value is stored only with its page, and a page only with
its value: a value is stored only when E2 verified it, so its quote was found on a page (decided
2026-11-13, `service.verified`); a value without a page is the shape of an invented quote.

**The downgrade** drops both tables (their policies, grants and indexes go with them), children
first. It deletes every stored extraction: tested only inside a rolled-back transaction
(`tests/test_migrations.py`), never run on a database whose rows matter.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE: Final = "vectapilot_app"
CURRENT_BUSINESS: Final = "nullif(current_setting('app.business_id', true), '')"

TABLES: Final = ("extractions", "extraction_fields")
"""Parent before child (the creation order); the downgrade drops them in reverse."""

SNAKE: Final = "^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
"""`config.SNAKE_PATTERN` with plain groups: `(?:` would read as a bind parameter in `text()`."""
SNAKE_LENGTH: Final = (2, 40)
"""`config.Snake`'s length (`SNAKE_MAX`)."""
VERSION: Final = "^[a-z][a-z0-9]*(-[a-z0-9]+)*[.][1-9][0-9]*$"
"""A prompt or rules version, `extract-fields.1`, `extraction-rules.1`."""
VERSION_MAX: Final = 40
SHA256: Final = "^[0-9a-f]{64}$"
TRACE_ID: Final = "^[0-9a-f]{32}$"
"""As `gateway.types.TraceId` (W3C Trace Context); all zeros is refused there, not here."""
DOCUMENT_STATUSES: Final = ("complete", "needs_review")
FIELD_STATUSES: Final = ("accepted", "review", "absent")
LEVELS: Final = ("high", "medium", "low")
FAILURES: Final = ("model_unavailable", "invalid_output", "truncated", "timeout", "internal_error")
"""`routing.FAILURES`: E3's failures, the only document-level failure reasons."""
REASONS: Final = (
    "quote_not_found",
    "value_not_in_quote",
    "value_cut",
    "bad_date",
    "bad_pattern",
    "bad_enum",
    "too_long",
    "name_charset",
    "quote_too_long",
    "rule_failed",
    "unsure",
    "from_ocr",
    "label_not_in_quote",
    "shared_value",
    "conflicting_values",
    "date_ambiguous",
    "image_on_page",
    "always_review",
    "not_found",
    "label_present",
    "image_page",
    "missing_signal",
    "model_unavailable",
    "invalid_output",
    "truncated",
    "timeout",
    "internal_error",
    "routing_invariant",
)
"""`routing.Reason`, in its order."""
MAX_PAGES: Final = 5
"""`text.MAX_PAGES` (E12): a longer document is refused before anything is stored."""
VALUE_MAX: Final = 200
"""`service.STORED_VALUE_MAX`: the longest `FieldSpec.value_bound` (`config.TEXT_MAX`). A value
longer than its own field's bound is withheld before storage (in review, `too_long`)."""
SPAN_MAX: Final = 40
"""`signals.SPAN_MAX` (D6 (a))."""
RETENTION_DAYS: Final = (1, 90)
"""`config.RETENTION_DAYS`."""


def _one_of(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _snake(column: str) -> str:
    low, high = SNAKE_LENGTH
    return f"{column} ~ '{SNAKE}' AND length({column}) BETWEEN {low} AND {high}"


def _version(column: str) -> str:
    return f"{column} ~ '{VERSION}' AND length({column}) <= {VERSION_MAX}"


def _one_dimension(column: str) -> str:
    """A flat array (an empty one has no dimensions)."""
    return f"coalesce(array_ndims({column}), 1) = 1"


# `IS TRUE`: `x <= ALL (array)` is NULL when the array holds a NULL, and a CHECK passes on NULL.
IMAGE_PAGES_CHECK: Final = (
    f"{_one_dimension('image_pages')} AND cardinality(image_pages) <= pages"
    " AND (1 <= ALL (image_pages)) IS TRUE AND (pages >= ALL (image_pages)) IS TRUE"
)
# Containment never matches a NULL element, so a NULL reason fails `<@`.
REASONS_CHECK: Final = (
    f"{_one_dimension('reasons')} AND (reasons <@ ARRAY[{_one_of(REASONS)}]) IS TRUE"
)
# `FieldDecision`'s shapes, each branch a non-null boolean (a NULL would pass the CHECK).
FIELD_SHAPE_CHECK: Final = (
    "CASE status"
    " WHEN 'accepted' THEN level IS NOT DISTINCT FROM 'high' AND cardinality(reasons) = 0"
    " AND value IS NOT NULL"
    " WHEN 'absent' THEN level IS NULL AND cardinality(reasons) = 0 AND value IS NULL"
    " AND span IS NULL AND page IS NULL"
    " ELSE cardinality(reasons) > 0 END"
)
EXPIRES_AT: Final = "date_add(created_at, make_interval(days => retention_days), 'UTC')"
RETENTION_RANGE: Final = " AND ".join(map(str, RETENTION_DAYS))

# One statement per op.execute: asyncpg sends each through the extended protocol, which takes one.
CREATE_TABLES: Final = (
    f"""
    CREATE TABLE extractions (
        business_id text NOT NULL REFERENCES businesses (business_id),
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        kind text NOT NULL CHECK ({_snake("kind")}),
        template_version integer NOT NULL CHECK (template_version >= 1),
        prompt_version text NOT NULL CHECK ({_version("prompt_version")}),
        rules_version text NOT NULL CHECK ({_version("rules_version")}),
        status text NOT NULL CHECK (status IN ({_one_of(DOCUMENT_STATUSES)})),
        failure_reason text CHECK (failure_reason IN ({_one_of(FAILURES)})),
        file_sha256 text NOT NULL CHECK (file_sha256 ~ '{SHA256}'),
        pages integer NOT NULL CHECK (pages BETWEEN 0 AND {MAX_PAGES}),
        image_pages integer[] NOT NULL CHECK ({IMAGE_PAGES_CHECK}),
        trace_id text NOT NULL CHECK (trace_id ~ '{TRACE_ID}'),
        retention_days integer NOT NULL CHECK (retention_days BETWEEN {RETENTION_RANGE}),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL GENERATED ALWAYS AS ({EXPIRES_AT}) STORED,
        PRIMARY KEY (business_id, id),
        CHECK (failure_reason IS NULL OR status = 'needs_review'),
        CHECK (pages > 0 OR failure_reason IS NOT NULL)
    )
    """,
    # The purge's `expires_at < now()` and the policy's `expires_at > now()`, per business.
    "CREATE INDEX extractions_business_expires ON extractions (business_id, expires_at)",
    f"""
    CREATE TABLE extraction_fields (
        business_id text NOT NULL REFERENCES businesses (business_id),
        extraction_id uuid NOT NULL,
        field text NOT NULL CHECK ({_snake("field")}),
        status text NOT NULL CHECK (status IN ({_one_of(FIELD_STATUSES)})),
        level text CHECK (level IN ({_one_of(LEVELS)})),
        reasons text[] NOT NULL CHECK ({REASONS_CHECK}),
        value jsonb CHECK (jsonb_typeof(value) = 'string'
                           AND length(value #>> '{{}}') BETWEEN 1 AND {VALUE_MAX}),
        span text CHECK (length(span) BETWEEN 1 AND {SPAN_MAX}),
        page integer CHECK (page BETWEEN 1 AND {MAX_PAGES}),
        PRIMARY KEY (business_id, extraction_id, field),
        FOREIGN KEY (business_id, extraction_id) REFERENCES extractions (business_id, id)
            ON DELETE CASCADE,
        CHECK ({FIELD_SHAPE_CHECK}),
        CHECK (level IS NULL OR value IS NOT NULL),
        CHECK ((page IS NULL) = (value IS NULL)),
        CHECK (span IS NULL OR page IS NOT NULL)
    )
    """,
)

# Column lists, not whole tables (as 0006): the app role never chooses an extraction's id, its
# `created_at` or (generated) `expires_at`.
EXTRACTION_INSERT_COLUMNS: Final = (
    "business_id",
    "kind",
    "template_version",
    "prompt_version",
    "rules_version",
    "status",
    "failure_reason",
    "file_sha256",
    "pages",
    "image_pages",
    "trace_id",
    "retention_days",
)
FIELD_INSERT_COLUMNS: Final = (
    "business_id",
    "extraction_id",
    "field",
    "status",
    "level",
    "reasons",
    "value",
    "span",
    "page",
)


def _columns(columns: Sequence[str]) -> str:
    return ", ".join(columns)


GRANTS: Final = (
    f"GRANT SELECT ON {', '.join(TABLES)} TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(EXTRACTION_INSERT_COLUMNS)}) ON extractions TO {APP_ROLE}",
    f"GRANT INSERT ({_columns(FIELD_INSERT_COLUMNS)}) ON extraction_fields TO {APP_ROLE}",
)

EXPIRY_POLICY: Final = "hidden_at_expiry"
"""A RESTRICTIVE `SELECT` policy for the app role, AND-ed with `business_isolation` (E10)."""
PARENT_NOT_EXPIRED: Final = (
    "EXISTS (SELECT 1 FROM extractions e WHERE e.business_id = extraction_fields.business_id"
    " AND e.id = extraction_fields.extraction_id AND e.expires_at > now())"
)
"""A field is visible only while its extraction is: one expiry, never two that could disagree."""
EXPIRY_POLICIES: Final = (
    f"CREATE POLICY {EXPIRY_POLICY} ON extractions AS RESTRICTIVE FOR SELECT TO {APP_ROLE}"
    " USING (expires_at > now())",
    f"CREATE POLICY {EXPIRY_POLICY} ON extraction_fields AS RESTRICTIVE FOR SELECT TO {APP_ROLE}"
    f" USING ({PARENT_NOT_EXPIRED})",
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
    *EXPIRY_POLICIES,
    *GRANTS,
)
DOWNGRADE: Final = (f"DROP TABLE {', '.join(reversed(TABLES))}",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
