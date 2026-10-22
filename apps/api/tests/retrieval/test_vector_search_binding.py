"""H1 tests without a database: `vector_search` sends ONE statement with every value bound.

`vector_search` is [HAND-WRITE]: shesha writes it. Until then it raises NotImplementedError, so the
whole module is marked `xfail(strict=True, raises=NotImplementedError)`:

- not implemented yet          → "xfailed" (expected);
- implemented, wrong answer    → a normal failure (only NotImplementedError counts as expected);
- implemented, right answer    → "XPASS(strict)", a failure on purpose: that is your signal.

When no test here is xfailed any more, delete the xfail entry of `pytestmark` below (keep
`anyio`) and run again: every test must then simply pass. Do not edit the tests to make them
pass; change the code.

This module needs no Postgres, so it runs everywhere: the quickest signal while writing H1, but
only about the statement's shape and bindings. A recording stand-in for the connection records
every call: `execute` returns an empty result, so the function runs to its end and must return
`[]`; any other method stops it (`Stopped`) once recorded, and the assert on the calls names it.
No rows are checked here, and nothing here sends the SQL to Postgres, so a statement Postgres
rejects can still pass; `test_vector_search.py` checks both on real Postgres (CI).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, NoReturn, cast

import pytest
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ClauseElement

from tests.retrieval.vectors import vector
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.retrieval.types import Reader, SearchHit, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.xfail(
        strict=True,
        raises=NotImplementedError,
        reason="H1: shesha implements vector_search — see docs/learning/m2-retrieval.md",
    ),
]

BUSINESS: Final = "binding-check-clinic"
MODEL: Final = "binding-check-model"
K: Final = 7
EMBEDDING: Final = vector(0.6, 0.8)  # 0.36 + 0.64 = 1; "0.6" and "0.8" appear in no SQL keyword


class Stopped(Exception):
    """Raised by the stand-in for any method but `execute`, right after it recorded the call."""


@dataclass(frozen=True)
class Call:
    method: str
    args: tuple[object, ...]
    kwargs: dict[str, object]


class RecordingConnection:
    """Stands in for `AsyncConnection`. Every awaited method is recorded: `execute` then returns an
    empty result (no rows; `.mappings()`, `.all()`, `.scalars()` and iteration all work), any other
    method raises `Stopped`."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    async def execute(self, *args: object, **kwargs: object) -> IteratorResult[Any]:
        self.calls.append(Call("execute", args, kwargs))
        return IteratorResult(SimpleResultMetaData(list(SearchHit.model_fields)), iter(()))

    def __getattr__(self, name: str) -> Callable[..., Awaitable[NoReturn]]:
        if name.startswith("__"):
            raise AttributeError(name)

        async def record(*args: object, **kwargs: object) -> NoReturn:
            self.calls.append(Call(name, args, kwargs))
            raise Stopped

        return record


def statement_and_values(call: Call) -> tuple[str, list[object]]:
    """The SQL text of an `execute` call, and every value bound to it (parameters dict, plus any
    `.bindparams(...)` values carried by the statement itself)."""
    statement = call.args[0] if call.args else call.kwargs.get("statement")
    assert isinstance(statement, ClauseElement), "pass a text() statement to connection.execute"
    parameters = call.args[1] if len(call.args) > 1 else call.kwargs.get("parameters", {})
    assert isinstance(parameters, dict), "bind the values as one dict of parameters"
    binds = statement.compile().params
    unbound = sorted(set(parameters) - set(binds))
    assert not unbound, (
        f"{unbound} are in the parameters dict but are no parameter of the statement: in text(),"
        " ':name::vector' is not read as a parameter; write CAST(:name AS vector)"
    )
    values: list[object] = list(parameters.values())
    values.extend(v for v in binds.values() if v is not None)
    return str(statement), values


@pytest.mark.parametrize("reader", ["customer", "staff"])
async def test_one_statement_with_every_value_bound_and_none_in_the_sql(reader: Reader) -> None:
    connection = RecordingConnection()
    query = VectorQuery(
        business_id=BUSINESS, embedding=EMBEDDING, embedding_model=MODEL, k=K, reader=reader
    )

    hits: list[SearchHit] | None
    try:
        hits = await vector_search(cast(AsyncConnection, connection), query)
    except Stopped:
        hits = None  # it called a method other than `execute`: the next assert names it

    # Exactly one call, and it is `execute`: no begin/commit/rollback and no exec_driver_sql,
    # before or after the statement.
    assert [call.method for call in connection.calls] == ["execute"]
    sql, values = statement_and_values(connection.calls[0])
    # Nothing from the query is formatted into the SQL text...
    for literal in (BUSINESS, MODEL, "0.6", "0.8"):
        assert literal not in sql, f"{literal!r} is in the SQL text: bind it as a parameter"
    assert "set_config" not in sql.lower(), "the caller's transaction already set the business"
    # ...everything is bound instead.
    assert BUSINESS in values
    assert MODEL in values
    assert K in values
    assert vector_literal(EMBEDDING) in values, "bind kb.store.vector_literal(query.embedding)"
    assert any(isinstance(v, list | tuple) and tuple(v) == query.audiences for v in values), (
        "bind query.audiences (as a list) for the audience filter"
    )
    # No rows came back, so no hits (and no error).
    assert hits == []
