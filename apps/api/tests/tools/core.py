"""Shared fixtures for the tool-core tests (M5 PR 2): the committed configurations, a fake
transaction source for the executor's unit tests, and contexts.

`FakeSource` stands in for `Database` without a database: it records every transaction it opens
(the business and whether it is the read-only snapshot) and every statement sent, and can make the
audit insert fail. The real tools never run on it; unit tests replace a tool's `run` or `validate`
through the executor's `registry`.

`RowsConnection` is for the real tools' own logic without a database: each statement returns the
next of its scripted results (rows as `SimpleNamespace`), through the real `ToolScope.fetch`.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, cast

from sqlalchemy.ext.asyncio import AsyncConnection

from tests.tools.helpers import SANDBOX_DIR
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.files import BusinessTools, read_business_tools
from vectapilot_api.tools.types import ToolContext

PAWS: Final[BusinessTools] = read_business_tools(SANDBOX_DIR / "paws-and-co")
SUNRISE: Final[BusinessTools] = read_business_tools(SANDBOX_DIR / "sunrise-clinic")
TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"


def config_for(business_id: str, tools: BusinessTools = PAWS, **changes: Any) -> ToolsConfig:
    """`tools`' configuration for another business id (and any other changes), validated."""
    data = tools.config.model_dump(mode="json") | {"business_id": business_id} | changes
    return ToolsConfig.model_validate(data)


def context_for(business_id: str, customer_ref: str = "cust-001") -> ToolContext:
    return ToolContext(business_id=business_id, customer_ref=customer_ref, trace_id=TRACE)


@dataclass
class FakeConnection:
    statements: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    audit_error: BaseException | None = None

    async def execute(self, statement: object, params: Mapping[str, object] | None = None) -> None:
        sql = str(statement)
        self.statements.append((sql, dict(params or {})))
        if self.audit_error is not None and "INSERT INTO tool_audit" in sql:
            raise self.audit_error


@dataclass
class FakeResult:
    columns: tuple[str, ...]
    rows: list[Any]

    def keys(self) -> tuple[str, ...]:
        return self.columns

    def all(self) -> list[Any]:
        return self.rows


@dataclass
class RowsConnection:
    """Each `execute` returns the next scripted result; its columns are the first row's fields
    (`business_id` alone for an empty result) unless `columns` says otherwise."""

    results: list[list[Any]] = field(default_factory=list)
    columns: tuple[str, ...] | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def execute(self, statement: object, params: Mapping[str, object] | None = None) -> Any:
        self.calls.append((str(statement), dict(params or {})))
        rows = self.results.pop(0)
        columns = self.columns or (tuple(vars(rows[0])) if rows else ("business_id",))
        return FakeResult(columns, rows)

    def connection(self) -> AsyncConnection:
        return cast(AsyncConnection, self)


@dataclass
class FakeSource:
    connection: FakeConnection = field(default_factory=FakeConnection)
    transactions: list[tuple[str, bool]] = field(default_factory=list)

    @asynccontextmanager
    async def business_transaction(
        self, business_id: str, *, snapshot: bool = False
    ) -> AsyncIterator[AsyncConnection]:
        self.transactions.append((business_id, snapshot))
        yield cast(AsyncConnection, self.connection)

    def audit_rows(self) -> list[dict[str, object]]:
        return [p for sql, p in self.connection.statements if "INSERT INTO tool_audit" in sql]

    def timeouts(self) -> list[dict[str, object]]:
        return [p for sql, p in self.connection.statements if "statement_timeout" in sql]


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)
