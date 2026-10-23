"""Full-text columns on `chunks`: `ts_config` and a stored, generated `tsv` (ADR 0014, M3 PR 2).

`ts_config regconfig NOT NULL` is the Postgres text-search config of the chunk's document language
(D2 (b): per chunk, through the platform map `ingestion/language_map.py`). `tsv tsvector
GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED` is what H1's `text_search` matches.

- **`regconfig`, the two-argument `to_tsvector`.** `to_tsvector(regconfig, text)` is IMMUTABLE,
  which a generated column requires; the one-argument form reads `default_text_search_config`
  (STABLE) and is refused. A `regconfig` is checked when it is written: an unknown config name is
  refused (42704), never stored. Each chunk keeps its own config, so H1 can parse the question
  with it, and Hindi and English chunks of one business are each stemmed in their own language.
- **No default on `ts_config`**, as 0003 did for `content_kind`: every new row names its config
  (`kb/store.py` sets it from the document's language through the map). A default would hide a
  writer that forgot.
- **`STORED` is written out.** Postgres 18 makes a generated column VIRTUAL unless it says STORED;
  a virtual column is computed on every read and cannot be indexed, and PR 8 may index this one.
- **`GENERATED ALWAYS`**: Postgres refuses an INSERT or UPDATE that sets `tsv` (428C9), so it
  always matches `content` and `ts_config`.
- **Order.** `ts_config` is added nullable (a catalog change, no rewrite), backfilled, then made
  NOT NULL; `tsv` comes last, so the one table rewrite that adding a stored column needs computes
  every `tsv` from the backfilled config. Added first, every `tsv` would be computed twice.
- **The backfill** reads `documents.language` through the map as it stands at 0005, written out
  here (`LANGUAGE_MAP`, `FALLBACK`; migrations are frozen in time). The SQL rule is the module's:
  the primary language subtag, lower-cased (`lower(split_part(language, '-', 1))`); a test
  compares the two on a list of tags. The join names both keys, `business_id` and `id`, as the
  chunks' composite foreign key does, so the statement keeps each chunk with its own business's
  document (rule 9) even though the owner skips RLS here.
- **A later change to the map needs its own backfill migration.** A stored `tsv` does not follow
  the map, so a migration that writes the new map out and updates the rows it moves ships with
  the change (tests/test_migrations.py compares the newest written-out map with the module; it
  compares the maps, not the rows). Such a backfill rewrites values that are already set, so no
  NOT NULL check stops it when RLS hides rows: a non-superuser owner under FORCE RLS would update
  nothing and raise nothing. It must refuse to run unless the migrating role skips RLS, e.g. a
  `DO` block with fixed text, `IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user
  AND (rolsuper OR rolbypassrls)) THEN RAISE EXCEPTION 'map backfill needs a role that skips
  RLS'` (it raises for a NOBYPASSRLS role, and passes for a superuser and for BYPASSRLS), or run
  once per business.
- **NOT NULL under FORCE RLS (M10).** Validating NOT NULL reads every row, whatever RLS allows;
  the `UPDATE` sees only what RLS allows. Locally and in CI the owner is the bootstrap superuser,
  which skips RLS, so the `UPDATE` reaches every business (ADR 0009, "Limit"). With a non-superuser
  owner under FORCE RLS (M10), a row hidden from the `UPDATE` would stay NULL, and `SET NOT NULL`
  stops the migration on it (23502), instead of leaving that business without full-text search.
  With no business set, as a real migration runs, the `UPDATE` sees no rows at all, so the
  migration stops whenever any chunk exists. The fix then: run the backfill as a role with
  BYPASSRLS, or once per business (with the business ids supplied from outside the database:
  `businesses` is FORCE RLS too). A test proves both outcomes (tests/test_migrations.py).
- **Errors name no data.** Each statement's error names a column, a config or a size, never a
  row's values, so no `DO` block replaces them (0004 needed one: a unique violation prints the
  key).
- **No GIN index on `tsv`** (D6). `@@` is not leakproof, so under RLS the app role may never use
  one; PR 8's EXPLAIN decides. Not `CONCURRENTLY`: there is no index to build, and Alembic runs a
  migration in a transaction. Adding `tsv` rewrites `chunks` under an exclusive lock: fine at pilot
  size, revisit for a live deployment (M10).
- **`pg_upgrade` (M10).** It refuses a cluster with a `regconfig` column in a user table (its
  "reg* data types in user tables" check, PostgreSQL 18). `pg_dump` and restore are not affected:
  the dump writes `ts_config` by name and leaves `tsv` out, so the restore recomputes it. The
  column cannot just be converted first: Postgres refuses to retype a column that a generated
  column uses, and `to_tsvector(text::regconfig, ...)` is not immutable. Decided 2026-10-23
  (shesha; ADR 0014, "Built"): keep `regconfig`. At M10's first major upgrade, upgrade by dump and
  restore, or run this migration's `DOWNGRADE` before `pg_upgrade` and its `UPGRADE` after (a
  table rewrite, with the RLS caveat above).
- **Locale (M10).** Splitting non-ASCII text into words depends on the database's locale
  (`lc_ctype`: en_US.utf8 locally; CI runs the same image with no locale override). M10 checks the
  target's locale, or reruns test_an_hi_chunk_gets_hindi and the `te` case of
  test_an_unknown_language_gets_simple against it.
- **The downgrade** drops `tsv`, then `ts_config` (the generated column depends on it). Both hold
  derived values only: an upgrade rebuilds them from `documents.language` and `content`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from collections.abc import Mapping, Sequence
from typing import Final

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LANGUAGE_MAP: Final[Mapping[str, str]] = {"en": "english", "hi": "hindi"}
FALLBACK: Final = "simple"
"""`ingestion/language_map.py` as it stands at 0005, written out here (migrations are frozen in
time). Primary language subtag -> config; every other language gets `FALLBACK`."""


def config_for(language_sql: str) -> str:
    """SQL text: the config name (text) for the BCP 47 tag that `language_sql` evaluates to, by
    its primary subtag, lower-cased. S608: `language_sql` is a constant column reference here
    (or a test's), never input."""
    whens = " ".join(f"WHEN '{tag}' THEN '{config}'" for tag, config in LANGUAGE_MAP.items())
    return f"CASE lower(split_part({language_sql}, '-', 1)) {whens} ELSE '{FALLBACK}' END"


ADD_TS_CONFIG: Final = "ALTER TABLE chunks ADD COLUMN ts_config regconfig"
BACKFILL: Final = (
    f"UPDATE chunks AS c SET ts_config = CAST({config_for('d.language')} AS regconfig)"  # noqa: S608
    " FROM documents AS d"
    " WHERE d.business_id = c.business_id AND d.id = c.document_id"
)
SET_NOT_NULL: Final = "ALTER TABLE chunks ALTER COLUMN ts_config SET NOT NULL"
ADD_TSV: Final = (
    "ALTER TABLE chunks ADD COLUMN tsv tsvector"
    " GENERATED ALWAYS AS (to_tsvector(ts_config, content)) STORED"
)

UPGRADE: Final = (ADD_TS_CONFIG, BACKFILL, SET_NOT_NULL, ADD_TSV)
DOWNGRADE: Final = (
    "ALTER TABLE chunks DROP COLUMN tsv",
    "ALTER TABLE chunks DROP COLUMN ts_config",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
