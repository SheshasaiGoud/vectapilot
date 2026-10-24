"""H1 tests without a database: `vector_search` sends ONE statement with every value bound.

`vector_search` is [HAND-WRITE], written by Claude at shesha's request (2026-10-22). The module's
strict xfail was removed with it, so these are plain tests. Do not edit a test to make it pass;
change the code.

This module needs no Postgres, so it runs everywhere: the quickest signal while writing H1, but
only about the statement's shape and bindings. A recording stand-in for the connection records
every call: `execute` returns an empty result, so the function runs to its end and must return
`[]`; any other method stops it (`Stopped`) once recorded, and the assert on the calls names it.
No rows are checked here, and nothing here sends the SQL to Postgres, so a statement Postgres
rejects can still pass; `test_vector_search.py` checks both on real Postgres (CI). The stand-in is
in tests/retrieval/recording.py (moved there unchanged in M3 PR 3, shared with `text_search`).
"""

from typing import Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.retrieval.recording import RecordingConnection, Stopped, statement_and_values
from tests.retrieval.vectors import vector
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.retrieval.types import Reader, SearchHit, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

pytestmark = [
    pytest.mark.anyio,
]

BUSINESS: Final = "binding-check-clinic"
MODEL: Final = "binding-check-model"
K: Final = 7
EMBEDDING: Final = vector(0.6, 0.8)  # 0.36 + 0.64 = 1; "0.6" and "0.8" appear in no SQL keyword


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
