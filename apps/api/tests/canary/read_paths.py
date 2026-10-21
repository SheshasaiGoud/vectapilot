"""EXTENSION POINT: every way a business can read data, for the canary suite (ADR 0009).

A read path acts as one business and returns what it produced, as text per label. The canary test
(`test_no_read_path_shows_another_businesss_canary`) runs every entry of `READ_PATHS` as each
business and fails if the other business's id or canary appears, or if its own never does.

Today the only path is plain SQL on every business table. Add yours here, with no other change:
- H1 (`m2/retrieval`): the top-k vector search, acting as the business, over the canary chunks.
- PR 5 / PR 6: each business route (`/v1/kb/...`) called with that business's `X-Business-Id`.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.catalog import business_tables
from tests.canary.checks import Planted
from vectapilot_api.db import business_transaction

ReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
"""(the app role's engine, the business to act as) -> {label: everything the path returned}."""


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


READ_PATHS: Final[dict[str, ReadPath]] = {
    "sql": sql_on_every_business_table,
    # "vector_search": ...,   H1
    # "GET /v1/kb/search": ..., PR 6
}
