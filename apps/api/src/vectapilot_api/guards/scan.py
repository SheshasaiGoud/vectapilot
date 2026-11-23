"""The KB scan (M7 PR 7; ADR 0018 part 10; G3, G7, G10): one business's approved chunks through the
chunk screen, listed by id and code for staff (`python -m vectapilot_api.guards scan`, `cli.py`).

**What it reads.** Every chunk of the business whose document is `approved`, public or staff-only,
in one business transaction as `vectapilot_app` (`Database.business_transaction`: the role check,
then RLS), and the statement names the business too (`c.business_id = :business_id`), so the
owner's connection, where RLS never applies, still sees one business (the canary suite checks
it). `ingestion/` is unchanged: the scan reads what the store already holds, read-only.

**What it runs** [proposal]: the screen a retrieved chunk meets at answer time
(`screens.screen_chunk`, G7), so a listed chunk is one the agent's answer path would drop before
the prompt: a chunk over the chunk cap (`over_cap`, the matcher never sees it, G3), or one with an
injection signal (`injection:phrase` through H1, `injection:invisible`). Red flags are not
listed: a business's own emergency guidance holds them on purpose.

**Codes, never text** (G10). A listed chunk is its id and its codes in `Finding` order; the scan
never prints, logs or returns a chunk's text, a term or a term id.

**Fail closed** (G3). Any exception while screening (H1's stub `NotImplementedError` included)
raises `ScanFailed` with the class name only, raised outside `except`: a partial list would read as
"the rest is clean", so nothing is listed. A database error propagates to the command, which
prints its type and SQLSTATE only.
"""

import uuid
from collections import Counter
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.guards.config import BusinessGuards
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.screens import screen_chunk
from vectapilot_api.guards.types import Finding

APPROVED_CHUNKS: Final = text(
    """
    SELECT c.id, c.content
    FROM chunks AS c
    JOIN documents AS d ON d.business_id = c.business_id AND d.id = c.document_id
    WHERE c.business_id = :business_id
      AND d.status = 'approved'
    ORDER BY d.source_id COLLATE "C", d.version COLLATE "C", c.chunk_index, c.id
    """
)
"""Every approved chunk of one business, both audiences, in a stable order: the business by its
own clause as well as by RLS (module docstring); the join uses both keys of the composite key."""

FINDING_ORDER: Final = {code: index for index, code in enumerate(Finding)}


class ScanFailed(Exception):
    """The screen raised (G3): nothing is listed. Carries the class name only; its message is
    fixed."""

    def __init__(self, error_type: str) -> None:
        super().__init__("the KB scan's screen failed")
        self.error_type = error_type


@dataclass(frozen=True)
class ChunkRow:
    """One approved chunk as the scan reads it."""

    chunk_id: uuid.UUID
    content: str

    def __repr__(self) -> str:
        return f"ChunkRow({self.chunk_id})"  # never the text


@dataclass(frozen=True)
class FlaggedChunk:
    chunk_id: uuid.UUID
    codes: tuple[Finding, ...]
    """Each code once, in `Finding` order."""


@dataclass(frozen=True)
class ScanReport:
    business_id: str
    screened: int
    flagged: tuple[FlaggedChunk, ...]
    """In the order the chunks were read (`APPROVED_CHUNKS`)."""

    def lines(self) -> list[str]:
        """What the command prints: a count line, then one line per flagged chunk (ids and codes,
        module docstring)."""
        head = (
            f"guards scan: {self.business_id}: {self.screened} approved chunk(s) screened,"
            f" {len(self.flagged)} flagged"
        )
        return [head] + [
            f"{chunk.chunk_id} {','.join(code.value for code in chunk.codes)}"
            for chunk in self.flagged
        ]


class ChunkSource(Protocol):
    """Where the approved chunks come from (the database, or a test's fake)."""

    async def approved_chunks(self, business_id: str) -> Sequence[ChunkRow]: ...


class BusinessTransactions(Protocol):
    """What the scan needs from `db.Database`: one business transaction (role check, then RLS)."""

    def business_transaction(
        self, business_id: str
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...


class DatabaseChunks:
    """`ChunkSource` over a business transaction (module docstring)."""

    def __init__(self, database: BusinessTransactions) -> None:
        self._database = database

    async def approved_chunks(self, business_id: str) -> Sequence[ChunkRow]:
        async with self._database.business_transaction(business_id) as connection:
            rows = await connection.execute(APPROVED_CHUNKS, {"business_id": business_id})
            return [ChunkRow(row.id, row.content) for row in rows]


def chunk_codes(content: str, guards: BusinessGuards, matcher: Matcher) -> tuple[Finding, ...]:
    """One chunk's codes under G7's chunk screen (module docstring), each once, in order."""
    _clean, found = screen_chunk(content, guards.injection, matcher)
    return tuple(sorted(Counter(found), key=FINDING_ORDER.__getitem__))


def screen_rows(
    business_id: str, rows: Sequence[ChunkRow], guards: BusinessGuards, matcher: Matcher
) -> ScanReport:
    """Every row through `chunk_codes`; `ScanFailed` if the screen raises (module docstring)."""
    flagged: list[FlaggedChunk] = []
    failure: str | None = None
    try:
        for row in rows:
            codes = chunk_codes(row.content, guards, matcher)
            if codes:
                flagged.append(FlaggedChunk(row.chunk_id, codes))
    except Exception as error:  # G3: fail closed; a cancel is not an Exception
        failure = type(error).__name__
    if failure is not None:
        raise ScanFailed(failure)  # outside `except`: nothing chained
    return ScanReport(business_id, len(rows), tuple(flagged))


async def scan_business(
    source: ChunkSource, business_id: str, guards: BusinessGuards, matcher: Matcher
) -> ScanReport:
    """Read the business's approved chunks, then screen them (module docstring)."""
    rows = await source.approved_chunks(business_id)
    return screen_rows(business_id, rows, guards, matcher)
