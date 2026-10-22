# ADR 0010 — One approved version per knowledge source

- **Status:** accepted · 2026-10-22 · chosen by shesha (option c, recorded in
  `docs/plans/m2-pr-checks.md`, H1, "decided 2026-10-22"). Built in M2 PR 5b (Diego); its
  details were confirmed by shesha the same day (PR 5b, "decided 2026-10-22").

**Context.** A knowledge source (an FAQ, a price list ...) is stored once per version
(`documents`: `UNIQUE (business_id, source_id, version)`, migration 0002), and only approved
documents may answer customers (business-context §3.4). §3.4 also says why versions matter: stale
documents are the top cause of wrong answers. Until PR 5b nothing stopped a business from holding
two approved versions of one source: uploading version 2 as approved left version 1 approved too,
so search (H1) could quote last year's prices next to this year's. `version` is free text, so
"newest" has no order the database can compute.

**Options** (as shesha saw them on 2026-10-22):
- (a) All approved versions: superseded versions stay searchable until someone retires them (and
  nothing can retire a document yet). The assistant may cite two contradicting versions; the
  stale-document risk §3.4 names stays.
- (b) Newest version only: the search SQL picks the newest approved version per source. That needs
  a definition of "newest": `version` is free text and does not sort, and `created_at` is upload
  order, not the business's intended order (re-ingesting older files after newer ones makes the
  older win). It also makes every read path more complex: each one (H1 now, PR 6's search, M3's
  hybrid search, the M8 admin screen) must repeat the rule, and one that forgets it quotes the
  stale version.
- (c) At most one approved version per source, enforced by a partial unique index; approving a
  version retires the previous one in the same transaction.

**Decision.** (c).
- **Migration 0004:** `CREATE UNIQUE INDEX documents_one_approved_version ON documents
  (business_id, source_id) WHERE status = 'approved'`. A partial index: drafts and retired
  versions are not in it, any number may exist. `business_id` first (ADR 0009): uniqueness is
  checked across all rows, ignoring RLS, so it only ever compares a business with itself. The
  canary key check reads partial indexes like any other (and shows their `WHERE`).
- **Retire on approve** (`kb/store.py`): saving an approved document runs `UPDATE documents SET
  status = 'retired' WHERE business_id = ... AND source_id = ... AND status = 'approved' AND
  version <> ...` and then the insert, in one business transaction. The order matters: the index
  is checked row by row, so the old row must leave it before the new one enters. Any later failure
  rolls the retire back with the rest; the old version stays approved. The response's
  `retired_version` names what was retired. Draft and retired uploads retire nothing.
- **Concurrent approvals** of two versions of one source: the index lets one commit; the other
  gets `409 approval_conflict` (a fixed message, never the database's text, which holds input).
- **Search needs no version filter** of its own (`status = 'approved'` is enough; H1 guide §2.6).

**Consequences.** One rule in one place, enforced even when code forgets it; H1 and every later
read path stay simple. The price: approving is a write with a lock on the previous version (fine
at pilot size); rolling back to an older version means uploading it again as a new version; a
database that already broke the rule cannot take migration 0004 until someone chooses which
version stays approved. No new privilege: the app role had `UPDATE` on `documents` since 0002.

**Details** (confirmed by shesha, 2026-10-22):
- *Migration 0004 on existing duplicates:* it stops with a fixed message naming no business,
  source or version, instead of retiring all but the newest by `created_at` (which version keeps
  answering customers is a decision, and a guessed one would be silent).
- *Same bytes, other status* (for example re-approving a retired version, or approving a stored
  draft by uploading it again): `409 status_conflict`; an upload never changes a stored version's
  status. Rejected for now: letting an upload promote a stored draft in place; revisit with M8's
  approve step.
- *Response:* `retired_version` stays (empty when nothing was retired).
- *Approving without an upload* (a review step, `approved by` / `approved on` from §3.4) is not
  built; it belongs with M8's auth and admin screen and would use the same retire.
