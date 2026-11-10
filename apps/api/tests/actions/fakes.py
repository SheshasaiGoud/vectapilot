"""Scripted stand-ins for the confirm service's and the store's unit tests: no database.

`ScriptedSource` is a `TransactionSource` whose first transaction is the main one (the confirm's
or the store's): each statement in it, but the server timeouts and `SET CONSTRAINTS`, returns the
next scripted result, rows or an exception. `constraints_error` is raised by `SET CONSTRAINTS ALL
IMMEDIATE` (a deferred key that does not hold); `exit_error` is raised when the main transaction
ends normally, as a failed or timed-out COMMIT would be. Later transactions (the audit after)
accept every statement, unless `after_error` is set: then their first statement raises it. Every
transaction (business, snapshot, read committed) and statement is recorded.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.actions.tokens import ConfirmToken, token_sha256

ACTION: Final = UUID("5f0c0b1e-1111-4222-8333-444455556666")
TOKEN: Final = "Q7zx9TOKENtokenTOKEN_-"  # noqa: S105 (a test token, not a secret)
OTHER_TOKEN: Final = "ZZzz9TOKENtokenTOKEN_-"  # noqa: S105 (a test token, not a secret)


def action_row(**changes: Any) -> SimpleNamespace:
    """A pending `book` action of `cust-001` with `TOKEN`, as `LOCK_ACTION` selects it."""
    row = {
        "business_id": "paws-and-co",
        "id": ACTION,
        "tool": "book",
        "arguments": {"slot_id": "S-09"},
        "customer_ref": "cust-001",
        "confirm_token_sha256": token_sha256(ConfirmToken(TOKEN)),
        "status": "pending",
        "result": None,
        "failure_reason": None,
        "expired": False,
    }
    return SimpleNamespace(**(row | changes))


@dataclass
class ScriptedResult:
    rows: list[Any]

    def keys(self) -> tuple[str, ...]:
        return tuple(vars(self.rows[0])) if self.rows else ("business_id",)

    def all(self) -> list[Any]:
        return self.rows

    def one(self) -> Any:
        [row] = self.rows
        return row

    @property
    def rowcount(self) -> int:
        return len(self.rows)


@dataclass
class ScriptedConnection:
    source: "ScriptedSource"
    main: bool

    async def execute(self, statement: object, params: Mapping[str, object] | None = None) -> Any:
        sql = str(statement)
        self.source.statements.append((sql, dict(params or {})))
        if not self.main:
            if self.source.after_error is not None:
                raise self.source.after_error
            return ScriptedResult([])
        if "statement_timeout" in sql:
            return ScriptedResult([])
        if sql.startswith("SET CONSTRAINTS"):
            if self.source.constraints_error is not None:
                raise self.source.constraints_error
            return ScriptedResult([])
        step = self.source.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return ScriptedResult(step)


@dataclass
class ScriptedSource:
    script: list[list[Any] | BaseException] = field(default_factory=list)
    exit_error: BaseException | None = None
    constraints_error: BaseException | None = None
    after_error: BaseException | None = None
    transactions: list[tuple[str, bool, bool]] = field(default_factory=list)
    statements: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    main_statements: int = 0

    @asynccontextmanager
    async def business_transaction(
        self, business_id: str, *, snapshot: bool = False, read_committed: bool = False
    ) -> AsyncIterator[AsyncConnection]:
        main = not self.transactions
        self.transactions.append((business_id, snapshot, read_committed))
        try:
            yield cast(AsyncConnection, ScriptedConnection(self, main))
        finally:
            if main:
                self.main_statements = len(self.statements)
        if main and self.exit_error is not None:
            raise self.exit_error

    def audits(self) -> list[dict[str, object]]:
        return [p for sql, p in self.statements if "INSERT INTO tool_audit" in sql]

    def main_audits(self) -> list[dict[str, object]]:
        """The audit rows inserted inside the main transaction (committed with it)."""
        main = self.statements[: self.main_statements]
        return [p for sql, p in main if "INSERT INTO tool_audit" in sql]

    def sent(self, fragment: str) -> bool:
        return any(fragment in sql for sql, _ in self.statements)

    def params_of(self, fragment: str) -> list[dict[str, object]]:
        return [p for sql, p in self.statements if fragment in sql]
