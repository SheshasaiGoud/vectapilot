"""One approved version per knowledge source: a partial unique index on `documents` (ADR 0010).

Only approved sources answer customers, and stale documents are the top cause of wrong answers
(business-context §3.4). With this index a business holds at most one approved version of each
source id; approving a new version retires the previous one in the same transaction
(`kb/store.py`), so search needs no version filter of its own (H1, docs/learning/m2-retrieval.md
§2.6). Drafts and retired versions are not in the index: any number of them may exist.

- `business_id` first (ADR 0009): Postgres checks uniqueness across all rows, ignoring RLS. With it
  first, the index only ever compares a business's rows with its own, so its error tells a
  business nothing about another. The canary key check (tests/canary/checks.py) reads partial
  indexes like any other unique index.
- **Rows that already break the rule stop the upgrade.** A local database may hold two approved
  versions of one source (PR 5 allowed it). Which one should answer customers is a decision, not
  something a migration may guess, so nothing is retired here (ADR 0010). Postgres' own error
  would print the duplicated key (a business id and a source id) into the migration log; the
  `DO` block replaces it with `DUPLICATES_FOUND`, which names no business, source or version.
  Building the index reads every row as the owner whatever RLS says, so the check needs no
  business set and no query of its own. To find the rows, as the owner (a superuser locally):
  `SELECT business_id, source_id FROM documents WHERE status = 'approved' GROUP BY 1, 2
  HAVING count(*) > 1`; set all but one to `retired`, or reset the local database.
- Not `CONCURRENTLY`: Alembic runs a migration in a transaction, which `CONCURRENTLY` refuses. The
  short lock on `documents` is fine at pilot size; revisit for a live deployment (M10).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX: Final = "documents_one_approved_version"
"""Written out here (migrations are frozen in time); a test checks `kb.store` uses the same name."""

CREATE_INDEX: Final = (
    f"CREATE UNIQUE INDEX {INDEX} ON documents (business_id, source_id) WHERE status = 'approved'"
)

DUPLICATES_FOUND: Final = (
    "migration 0004: a business holds two or more approved versions of one source. Retire all"
    " but one approved version per source (status retired), then upgrade again (ADR 0010)"
)
"""Fixed text: no business, source or version. No quote or percent sign (RAISE reads both)."""

UPGRADE: Final = (
    f"""
    DO $$
    BEGIN
        {CREATE_INDEX};
    EXCEPTION WHEN unique_violation THEN
        RAISE EXCEPTION '{DUPLICATES_FOUND}' USING ERRCODE = 'unique_violation';
    END
    $$
    """,
)
DOWNGRADE: Final = (f"DROP INDEX {INDEX}",)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
