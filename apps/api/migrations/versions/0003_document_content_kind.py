"""`documents.content_kind`: what a source is about (business-context §3.4), its own field.

Decided 2026-10-21 (M2 PR 3): `source_type` stays the file format (markdown, html, csv, pdf); the
content kind (FAQ, pricing, policy ...) is a separate, validated column so retrieval and review can
filter on it. The values are the §3.4 list without "PDFs" (a format, not a kind); `kb.types
.ContentKind` must list the same ones (a test compares them).

Rows that existed before this migration get `other` (a constant default adds the column without
rewriting the table and without RLS getting in the way); the default is then dropped, so every new
row must name its kind.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

from collections.abc import Sequence
from typing import Final

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONTENT_KINDS: Final = (
    "faq",
    "service_list",
    "product_catalogue",
    "pricing",
    "appointment_policy",
    "other",
)
"""Written out here (migrations are frozen in time); tests check it equals `ContentKind`."""

BACKFILL: Final = "other"


def upgrade() -> None:
    allowed = ", ".join(f"'{kind}'" for kind in CONTENT_KINDS)
    op.execute(
        f"ALTER TABLE documents ADD COLUMN content_kind text NOT NULL DEFAULT '{BACKFILL}'"
        f" CONSTRAINT documents_content_kind_check CHECK (content_kind IN ({allowed}))"
    )
    op.execute("ALTER TABLE documents ALTER COLUMN content_kind DROP DEFAULT")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN content_kind")
