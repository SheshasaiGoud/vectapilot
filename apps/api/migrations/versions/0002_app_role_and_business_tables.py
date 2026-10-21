"""The app role and the first business-owned tables, each under forced row-level security.

ADR 0009 (roles and RLS), ADR 0004 (isolation), ADR 0008 (one embedding model id per chunk).

- `vectapilot_app` is the role the API logs in as: NOSUPERUSER NOBYPASSRLS, owns nothing, and gets
  only the grants below. This migration creates it NOLOGIN and without a password, so no secret is
  ever part of a migration; `vectapilot-db-provision` (run after `alembic upgrade head`) turns
  LOGIN on and sets the password from the API's own URL (ADR 0009, "Role and password").
- Every table here has a non-null `business_id`, RLS ENABLED and FORCED (so the owner is filtered
  too, unless it is a superuser), and one policy for every command:
  `business_id = nullif(current_setting('app.business_id', true), '')` in USING and WITH CHECK.
  No business set (NULL or '') matches no row: reads return nothing and writes are refused.
- `businesses` is the registry: its key *is* the business id, so it is filtered like the rest and
  the app can only see the business it named.

Roles belong to the whole Postgres server, not one database: the downgrade drops the role, which
fails loudly if another database on the same server still grants it anything.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE: Final = "vectapilot_app"
"""Written out here (migrations are frozen in time); tests check it equals `db.APP_ROLE`."""

BUSINESS_TABLES: Final = ("businesses", "documents", "chunks", "llm_calls")
CURRENT_BUSINESS: Final = "nullif(current_setting('app.business_id', true), '')"

# One statement per op.execute: asyncpg sends each through the extended protocol, which takes one.
CREATE_TABLES: Final = (
    # The registry. The CHECK mirrors `BusinessId` (business-context §3.1); Postgres' `$` matches
    # only at the very end of the string, so a trailing newline is refused here too.
    """
    CREATE TABLE businesses (
        business_id text PRIMARY KEY
            CHECK (business_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
                   AND length(business_id) BETWEEN 3 AND 40),
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    # One version of one knowledge source (business-context §3.4). UNIQUE (business_id, id) is the
    # target of the chunks' composite foreign key; (business_id, source_id, version) lets PR 5
    # make re-ingesting the same version idempotent.
    """
    CREATE TABLE documents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id text NOT NULL REFERENCES businesses (business_id),
        source_id text NOT NULL,
        version text NOT NULL,
        title text NOT NULL,
        source_type text NOT NULL,
        language text NOT NULL,
        status text NOT NULL CHECK (status IN ('draft', 'approved', 'retired')),
        audience text NOT NULL CHECK (audience IN ('public', 'staff_only')),
        content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        review_by date,
        metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (business_id, id),
        UNIQUE (business_id, source_id, version)
    )
    """,
    # The composite foreign key makes a chunk's business the same as its document's: a chunk can
    # never point at another business's document, whatever the code does.
    """
    CREATE TABLE chunks (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id text NOT NULL,
        document_id uuid NOT NULL,
        chunk_index integer NOT NULL CHECK (chunk_index >= 0),
        content text NOT NULL,
        page integer CHECK (page >= 1),
        metadata jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(metadata) = 'object'),
        embedding vector(1024) NOT NULL,
        embedding_model text NOT NULL,
        embedding_dim integer NOT NULL CHECK (embedding_dim = vector_dims(embedding)),
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (business_id, document_id)
            REFERENCES documents (business_id, id) ON DELETE CASCADE,
        UNIQUE (document_id, chunk_index)
    )
    """,
    # Search filters by business and model id (ADR 0008); exact search, no ANN index yet.
    "CREATE INDEX chunks_business_model ON chunks (business_id, embedding_model)",
    # One row per gateway `CallRecord` (ADR 0006/0007): metadata only, never prompt or answer text.
    # Column names are the record's field names (a test compares them).
    """
    CREATE TABLE llm_calls (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        business_id text NOT NULL REFERENCES businesses (business_id),
        record_version smallint NOT NULL CHECK (record_version >= 1),
        alias text NOT NULL,
        model_requested text,
        model_answered text,
        catalog_version text NOT NULL,
        inference_geo text,
        outcome text NOT NULL,
        stop_reason text,
        attempts integer NOT NULL CHECK (attempts >= 0),
        retries integer NOT NULL CHECK (retries >= 0),
        repairs integer NOT NULL CHECK (repairs BETWEEN 0 AND 1),
        attempt_timeouts integer NOT NULL CHECK (attempt_timeouts >= 0),
        first_try_valid boolean,
        input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
        output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
        cache_read_tokens bigint NOT NULL CHECK (cache_read_tokens >= 0),
        cache_write_5m_tokens bigint NOT NULL CHECK (cache_write_5m_tokens >= 0),
        cache_write_1h_tokens bigint NOT NULL CHECK (cache_write_1h_tokens >= 0),
        cost_usd numeric NOT NULL CHECK (cost_usd >= 0),
        cost_complete boolean NOT NULL,
        cache_hit boolean NOT NULL,
        latency_ms double precision NOT NULL CHECK (latency_ms >= 0),
        request_ids text[] NOT NULL DEFAULT '{}',
        trace_id text,
        operation text,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX llm_calls_business_created ON llm_calls (business_id, created_at)",
)

# Only what the API needs. No sequences (uuid keys), no CREATE anywhere, no TRUNCATE (which RLS
# does not filter). The registry is read-only: businesses are added by the owner (config via PRs).
# `llm_calls` is append-only.
GRANTS: Final = (
    f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}",
    f"GRANT SELECT ON businesses TO {APP_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents, chunks TO {APP_ROLE}",
    f"GRANT SELECT, INSERT ON llm_calls TO {APP_ROLE}",
)


def upgrade() -> None:
    # Created only if missing (roles are server-wide); an existing one must already be safe: this
    # refuses instead of altering it, since only a superuser may change SUPERUSER or BYPASSRLS.
    # S608: the only value formatted in is the constant APP_ROLE, never input.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
                    NOREPLICATION NOINHERIT;
            END IF;
            IF EXISTS (SELECT FROM pg_catalog.pg_roles
                       WHERE rolname = '{APP_ROLE}' AND (rolsuper OR rolbypassrls)) THEN
                RAISE EXCEPTION 'role {APP_ROLE} exists with SUPERUSER or BYPASSRLS';
            END IF;
        END
        $$
        """  # noqa: S608
    )
    for statement in CREATE_TABLES:
        op.execute(statement)
    for table in BUSINESS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY business_isolation ON {table}"
            f" USING (business_id = {CURRENT_BUSINESS})"
            f" WITH CHECK (business_id = {CURRENT_BUSINESS})"
        )
    for statement in GRANTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE llm_calls, chunks, documents, businesses")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
