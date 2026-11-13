"""The purge of expired extractions, one business at a time, as the owner (ADR 0017 decision 6,
decided D6 (a); E10; M6 PR 3).

Stored extractions are hidden from the app role when they expire (migration 0007's restrictive
`hidden_at_expiry` policy), but they stay in the table until this purge deletes them. Nothing
schedules it until M9: it runs by hand, through `python -m vectapilot_api.extraction purge
--business <id>` (`cli.py`), only in `PURGE_ENVIRONMENTS`.

**One statement, scoped by its own clause.** `DELETE FROM extractions WHERE business_id =
:business_id AND expires_at < now()`: the fields go with their extraction (`ON DELETE CASCADE`,
the composite foreign key, so only the same business's fields). It runs as the owner, which skips
row-level security (a superuser locally and in CI, ADR 0009 "Limit"), so RLS cannot keep another
business out: the statement's `business_id = :business_id` does, and the tests prove on the
owner's connection that purging one business leaves every other row of every business table the
same (and that the same check turns red without the clause). It also runs inside
`business_transaction` for that business, so under a non-superuser owner (M10) `FORCE` RLS adds a
second fence; the expiry policy binds the app role only, so the owner sees the expired rows it
must delete. `purge_expired` refuses a connection set to any other business, or none.

`now()` is the transaction's start: a row that expires during the purge waits for the next one.
Only expired rows: an unexpired extraction is never deleted here, whatever its age.
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

PURGE_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
"""Where the purge may run: never `production`, nor any environment added later (until M9 decides
how and where the purge is scheduled)."""

PURGE_REFUSED: Final = "the purge runs inside business_transaction for the named business"
_CURRENT_BUSINESS: Final = text("SELECT current_setting('app.business_id', true)")
PURGE: Final = text(
    "DELETE FROM extractions WHERE business_id = :business_id AND expires_at < now()"
)
"""The one statement a purge sends; its fields go with it (`ON DELETE CASCADE`)."""


class PurgeRefused(RuntimeError):
    """The purge would run where it must not. Fixed text (`PURGE_REFUSED`), never a value."""


async def purge_expired(connection: AsyncConnection, business_id: str) -> int:
    """Delete `business_id`'s expired extractions (and their fields) on `connection`, an owner
    connection inside `business_transaction(engine, business_id)` (the caller commits). The
    number of extractions deleted. Refuses a connection set to another business, or to none,
    before it sends anything else."""
    if await connection.scalar(_CURRENT_BUSINESS) != business_id:
        raise PurgeRefused(PURGE_REFUSED)
    result = await connection.execute(PURGE, {"business_id": business_id})
    return int(result.rowcount)
