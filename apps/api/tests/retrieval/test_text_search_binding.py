"""H1 (M3) tests without a database: `text_search` sends ONE statement with every value bound.

`text_search` is [HAND-WRITE], written by shesha (D5, decided 2026-10-24). Until then it raises
NotImplementedError, so this module is a strict xfail on it: "xfailed" while it is a stub, a normal
failure for a wrong answer, "XPASS(strict)" (a failure on purpose) once this passes. Do not edit a
test to make it pass; change the code. The mark comes from the one switch,
`pending_hand_write("text_search")` (tests/canary/read_paths.py, `PENDING_HAND_WRITE`).

This module needs no Postgres, so it runs everywhere: the quickest signal while writing H1, but
only about the statement's shape and bindings (tests/retrieval/recording.py, as for M2's
`vector_search`). Whether Postgres accepts the statement, which rows come back and how a hostile
question behaves: `test_text_search.py`, on real Postgres.
"""

from typing import Final, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.canary.read_paths import pending_hand_write
from tests.retrieval.recording import RecordingConnection, Stopped, statement_and_values
from tests.retrieval.vectors import vector
from vectapilot_api.kb.store import vector_literal
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import Reader, SearchHit, TextQuery

# A strict xfail while "text_search" is in PENDING_HAND_WRITE: one switch with the canary paths.
pytestmark = [pytest.mark.anyio, *pending_hand_write("text_search")]

BUSINESS: Final = "binding-check-clinic"
MODEL: Final = "binding-check-model"
K: Final = 7
EMBEDDING: Final = vector(0.6, 0.8)  # 0.36 + 0.64 = 1; "0.6" and "0.8" appear in no SQL keyword
QUESTION: Final = "zq5bind & opening | hours"
"""Bound exactly as given (it has nothing to strip); `zq5bind` and the operators must never reach
the SQL text."""


@pytest.mark.parametrize("reader", ["customer", "staff"])
async def test_one_statement_with_every_value_bound_and_none_in_the_sql(reader: Reader) -> None:
    connection = RecordingConnection()
    query = TextQuery(
        business_id=BUSINESS,
        question=QUESTION,
        embedding=EMBEDDING,
        embedding_model=MODEL,
        k=K,
        reader=reader,
    )

    hits: list[SearchHit] | None
    try:
        hits = await text_search(cast(AsyncConnection, connection), query)
    except Stopped:
        hits = None  # it called a method other than `execute`: the next assert names it

    # Exactly one call, and it is `execute`: no begin/commit/rollback and no exec_driver_sql,
    # before or after the statement.
    assert [call.method for call in connection.calls] == ["execute"]
    sql, values = statement_and_values(connection.calls[0])
    # Nothing from the query is formatted into the SQL text...
    for literal in (BUSINESS, MODEL, "0.6", "0.8", "zq5bind", QUESTION):
        assert literal not in sql, f"{literal!r} is in the SQL text: bind it as a parameter"
    assert "set_config" not in sql.lower(), "the caller's transaction already set the business"
    # ...everything is bound instead, the question unchanged: it is parsed in SQL, with each
    # chunk's own config, never split, escaped or rewritten in Python.
    assert BUSINESS in values
    assert MODEL in values
    assert K in values
    assert QUESTION in values, "bind query.question exactly as given"
    assert vector_literal(EMBEDDING) in values, "bind kb.store.vector_literal(query.embedding)"
    assert any(isinstance(v, list | tuple) and tuple(v) == query.audiences for v in values), (
        "bind query.audiences (as a list) for the audience filter"
    )
    # No rows came back, so no hits (and no error).
    assert hits == []
