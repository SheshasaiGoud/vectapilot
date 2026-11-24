"""Scripted stand-ins for the auth unit tests: no database.

`ScriptedConnection` answers each statement with the next scripted row list (or raises a scripted
exception) and records every statement with its parameters. `ScriptedDatabase` is the part of
`db.Database` the dependencies use: `business_transaction` records the business and yields the
connection, or fails or hangs on purpose (S14).
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import TextClause
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.auth.tokens import token_sha256

BUSINESS: Final = "paws-and-co"
OTHER: Final = "sunrise-clinic"
SECRET: Final = "A" * 21 + "b" * 21 + "_"  # 43 base64url characters, synthetic
TOKEN: Final = f"{BUSINESS}.{SECRET}"
OTHER_TOKEN: Final = f"{OTHER}.{SECRET}"
SESSION_ID: Final = UUID("0f0e0d0c-0b0a-4908-8706-050403020100")
STAFF_ID: Final = UUID("11111111-2222-4333-8444-555555555555")


def customer_row(**changes: Any) -> SimpleNamespace:
    """A live customer session of `BUSINESS` with `TOKEN`, as `CUSTOMER_SESSION` selects it."""
    row = {
        "business_id": BUSINESS,
        "id": SESSION_ID,
        "ui_language": "en",
        "token_sha256": token_sha256(TOKEN),
    }
    return SimpleNamespace(**(row | changes))


def staff_row(**changes: Any) -> SimpleNamespace:
    """A live staff session of an enabled reviewer of `BUSINESS`, as `STAFF_SESSION` selects it."""
    row = {
        "business_id": BUSINESS,
        "id": SESSION_ID,
        "staff_id": STAFF_ID,
        "token_sha256": token_sha256(TOKEN),
        "role": "reviewer",
        "display_name": "Synthetic Reviewer",
    }
    return SimpleNamespace(**(row | changes))


@dataclass
class ScriptedResult:
    rows: list[Any]

    def one(self) -> Any:
        [row] = self.rows
        return row

    def one_or_none(self) -> Any:
        if len(self.rows) > 1:
            raise MultipleResultsFound("more than one row")
        return self.rows[0] if self.rows else None


@dataclass
class ScriptedConnection:
    """Each `execute` takes the next item of `script`: a list of rows, or an exception to raise."""

    script: list[list[Any] | BaseException] = field(default_factory=list)
    executed: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)

    async def execute(
        self, statement: TextClause, params: Mapping[str, Any] | None = None
    ) -> ScriptedResult:
        self.executed.append((statement.text, dict(params or {})))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ScriptedResult(item)


def as_connection(connection: ScriptedConnection) -> AsyncConnection:
    return cast(AsyncConnection, connection)


@dataclass
class ScriptedDatabase:
    """`Database.business_transaction` over one `ScriptedConnection`; `fail` raises on opening,
    `hang` sleeps forever there (a database that never answers)."""

    connection: ScriptedConnection = field(default_factory=ScriptedConnection)
    fail: BaseException | None = None
    hang: bool = False
    opened: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def business_transaction(self, business_id: str) -> AsyncIterator[AsyncConnection]:
        self.opened.append(business_id)
        if self.hang:
            await asyncio.Event().wait()
        if self.fail is not None:
            raise self.fail
        yield as_connection(self.connection)
