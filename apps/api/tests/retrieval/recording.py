"""A recording stand-in for `AsyncConnection`, for the binding tests of both search legs.

Moved unchanged from tests/retrieval/test_vector_search_binding.py (M3 PR 3), so
`test_text_search_binding.py` checks `text_search` the same way. Every awaited method is recorded:
`execute` returns an empty result (no rows; `.mappings()`, `.all()`, `.scalars()` and iteration all
work), so the function under test runs to its end and must return `[]`; any other method stops it
(`Stopped`) once recorded, and the assert on the calls names it. Nothing here sends the SQL to
Postgres, so a statement Postgres rejects can still pass; the Postgres tests check that.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData
from sqlalchemy.sql.elements import ClauseElement

from vectapilot_api.retrieval.types import SearchHit


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
