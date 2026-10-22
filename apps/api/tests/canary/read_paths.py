"""EXTENSION POINT: every way a business can read data, for the canary suite (ADR 0009).

A read path acts as one business and returns what it produced, as text per label. The canary test
(`test_no_read_path_shows_another_businesss_canary`) runs every entry of `READ_PATHS` as each
business and fails if the other business's id or canary appears, or if its own never does.

The paths: plain SQL on every business table, and H1's top-k vector search (`m2/retrieval`).
Add yours here, with no other change:
- PR 6: each business route (`/v1/kb/...`) called with that business's `X-Business-Id`.

**A path whose code is still a [HAND-WRITE] stub** is listed in `PENDING_HAND_WRITE` too. The canary
test then marks it `xfail(strict=True, raises=NotImplementedError)` (`read_path_params`), which
`scripts/canary_gate.py` allows as a recorded gap: the scaffold PR stays green, and the path is
wired so that it runs the moment the stub is replaced. Then the path's canary tests show
XPASS(strict), a failure on purpose, or FAILED if it leaks: either way CI is red until the name is
removed from `PENDING_HAND_WRITE`, and from then on the path is checked like any other.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.catalog import business_tables
from tests.canary.checks import Planted
from vectapilot_api.db import business_transaction
from vectapilot_api.retrieval.types import MAX_K, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

ReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
"""(the app role's engine, the business to act as) -> {label: everything the path returned}."""

CANARY_EMBEDDING: Final = (0.0,) * 1023 + (1.0,)
"""The direction `test_leakage.plant` stores every canary chunk with (its `VECTOR`)."""

_STORED_MODELS: Final = text(
    "SELECT DISTINCT embedding_model FROM chunks WHERE business_id = :b ORDER BY 1"
)


async def sql_on_every_business_table(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """Each business table, read in full as `me`, every row as JSON."""
    outputs: dict[str, str] = {}
    async with business_transaction(engine, me.business_id) as connection:
        for table in await business_tables(connection):
            # S608: `table` comes from the catalog as `oid::regclass::text` (quoted), not input.
            rows = await connection.exec_driver_sql(
                f"SELECT row_to_json(t)::text FROM {table} AS t"  # noqa: S608
            )
            outputs[f"sql:{table}"] = "\n".join(row[0] for row in rows)
    return outputs


async def vector_search_as_staff(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """H1's `vector_search` as `me`, once per embedding model id `me`'s chunks carry, every hit as
    JSON. Staff is the widest reader (public and staff-only), `k` the most allowed, and the question
    points straight at the canary chunk, so a working search shows `me`'s canary.

    Like every read path here it runs as the app role, under RLS, so it proves isolation through
    RLS, not `vector_search`'s own `WHERE business_id` clause: a search without that clause would
    pass here too. The clause is proven by `tests/retrieval/test_vector_search.py` (the
    owner-connection test and the other-business-transaction test), which `scripts/canary_gate.py`
    does not cover. A proposal for zara and shesha once H1 lands: mirror those two checks into
    tests/canary, or make the gate require those test ids to pass."""
    outputs: dict[str, str] = {}
    async with business_transaction(engine, me.business_id) as connection:
        models = (await connection.execute(_STORED_MODELS, {"b": me.business_id})).scalars()
        for model in list(models):
            query = VectorQuery(
                business_id=me.business_id,
                embedding=CANARY_EMBEDDING,
                embedding_model=model,
                k=MAX_K,
                reader="staff",
            )
            hits = await vector_search(connection, query)
            outputs[f"vector_search:{model}"] = "\n".join(hit.model_dump_json() for hit in hits)
    return outputs


READ_PATHS: Final[dict[str, ReadPath]] = {
    "sql": sql_on_every_business_table,
    "vector_search": vector_search_as_staff,
    # "GET /v1/kb/search": ..., PR 6
}

PENDING_HAND_WRITE: Final = frozenset({"vector_search"})
"""Read paths still raising NotImplementedError (a [HAND-WRITE] stub). H1: remove
"vector_search" once `retrieval/vector.py` is written (its canary tests show XPASS(strict))."""


def read_path_params() -> list[object]:
    """`READ_PATHS`' names for `pytest.mark.parametrize`, a pending one marked strict xfail."""
    pending = pytest.mark.xfail(
        strict=True,
        raises=NotImplementedError,
        reason="[HAND-WRITE] stub not written yet (tests/canary/read_paths.py, PENDING_HAND_WRITE)",
    )
    return [
        pytest.param(name, marks=[pending] if name in PENDING_HAND_WRITE else [])
        for name in sorted(READ_PATHS)
    ]
