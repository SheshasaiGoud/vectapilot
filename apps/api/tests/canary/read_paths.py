"""EXTENSION POINT: every way a business can read data, for the canary suite (ADR 0009).

A read path acts as one business and returns what it produced, as text per label. The canary test
(`test_no_read_path_shows_another_businesss_canary`) runs every entry of `READ_PATHS` as each
business and fails if the other business's id or canary appears, or if its own never does.

The paths: plain SQL on every business table, H1's top-k vector search (`m2/retrieval`), and
PR 6's two routes that show chunk text (`GET /v1/kb/search`, `POST /v1/answer`, through the real
app with that business's `X-Business-Id`). Add yours here, with no other change.

**The bait.** A search path is only a test if the other business's data could be returned. So
`test_leakage.plant` gives both businesses the same `BAIT` chunk (same text, the configured
embedding model, the fake embedder's vector of `BAIT`), titled with that business's own canary,
and PR 6's API paths ask `BAIT`. The other business's bait is then a same-model candidate that
scores 1.0, above any answer threshold: a route that leaked across businesses would return it,
and its title, the other canary, would show in the hits, the prompt and the sources.

**A path that runs a [HAND-WRITE] stub** names it in `NEEDS_HAND_WRITE`, and a stub that still
raises NotImplementedError is listed in `PENDING_HAND_WRITE`. While it is, the canary test marks
the path `xfail(strict=True, raises=NotImplementedError)` (`read_path_params`, via
`pending_hand_write`), which `scripts/canary_gate.py` allows as a recorded gap: the PR stays green,
and the path is wired so that it runs the moment the stub is replaced. Then the path's canary
tests show XPASS(strict), a failure on purpose, or FAILED if it leaks: either way CI is red until
the stub's name is removed from `PENDING_HAND_WRITE`, and from then on the path is checked like
any other. **One switch:** PR 6's Postgres tests use the same `pending_hand_write`, so removing
"vector_search" (H1's step 3) turns on every check that runs H1 at once.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.answer.providers import CitingProvider
from tests.canary.catalog import business_tables
from tests.canary.checks import Planted
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.types import MAX_K, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

ReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
"""(the app role's engine, the business to act as) -> {label: everything the path returned}."""

CANARY_EMBEDDING: Final = (0.0,) * 1023 + (1.0,)
"""The direction `test_leakage.plant` stores every canary chunk with (its `VECTOR`)."""

BAIT: Final = "Bait for the canary suite: the same question and chunk text for every business."
"""The text of each business's `bait` chunk (`test_leakage.plant`) and the question PR 6's API
read paths ask. Shared on purpose; it holds no business id and no canary."""

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
    """H1's `vector_search` as `me`, once per embedding model id `me`'s chunks carry (`fake` for
    the canary chunk, the configured model for the bait), every hit as JSON. Staff is the widest
    reader (public and staff-only), `k` the most allowed, and the question points straight at the
    canary chunk, so a working search shows `me`'s canary (the bait's title is `me`'s canary too).

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


def _reindex(client: TestClient, me: Planted) -> None:
    """Re-embed `me`'s canary chunk with the configured (fake) model: `test_leakage.plant` stores
    it under model id `fake` next to the bait under the configured one, and search refuses mixed
    models (`reindex_required`). Afterwards all of `me`'s chunks carry the configured model; the
    canary chunk scores about 0 against `BAIT`, the bait 1.0."""
    response = client.post("/v1/kb/reindex", headers={BUSINESS_HEADER: me.business_id})
    assert response.is_success, response.text


async def search_through_the_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`GET /v1/kb/search` as `me` (reader customer: approved public chunks), asking `BAIT` with
    the largest `k`; the response body as text (`me`'s bait shows `me`'s canary as its title).
    Through the real app, as the app role: it proves the route's isolation through RLS and its own
    scoping together."""
    with TestClient(create_app(Settings())) as client:
        _reindex(client, me)
        response = client.get(
            "/v1/kb/search",
            params={"q": BAIT, "k": MAX_K},
            headers={BUSINESS_HEADER: me.business_id},
        )
    assert response.is_success, response.text
    return {"GET /v1/kb/search": response.text}


async def answer_through_the_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`POST /v1/answer` as `me`, asking `BAIT`, with a scripted model that cites every real block
    of its prompt; the response body (its `sources` show chunk text and titles) and every prompt
    the model got (business-context §5: a prompt only ever holds the current business's data).
    `me`'s bait scores 1.0, so it is evidence, and its title, `me`'s canary, is the positive
    control in both outputs."""
    provider = CitingProvider()
    with TestClient(create_app(Settings(), llm_provider=provider)) as client:
        _reindex(client, me)
        response = client.post(
            "/v1/answer",
            json={"question": BAIT},
            headers={BUSINESS_HEADER: me.business_id},
        )
    assert response.is_success, response.text
    prompts = [f"{call.system}\n{call.messages[-1].content}" for call in provider.calls]
    return {"POST /v1/answer": response.text, "POST /v1/answer (prompt)": "\n".join(prompts)}


READ_PATHS: Final[dict[str, ReadPath]] = {
    "sql": sql_on_every_business_table,
    "vector_search": vector_search_as_staff,
    "GET /v1/kb/search": search_through_the_api,
    "POST /v1/answer": answer_through_the_api,
}

NEEDS_HAND_WRITE: Final[Mapping[str, str]] = {
    "vector_search": "vector_search",
    "GET /v1/kb/search": "vector_search",
    "POST /v1/answer": "vector_search",
}
"""Read path → the [HAND-WRITE] stub it runs (H1's `vector_search` for all three)."""

NEEDED_OUTSIDE_READ_PATHS: Final[Mapping[str, str]] = {
    "retrieval_metrics": "tests/evalset/test_metrics.py (H2's own tests) and tests/evalrun (M2 PR"
    " 8's eval harness calls H2's metrics)",
}
"""[HAND-WRITE] stubs that no read path runs, but other tests wait for through this same switch:
stub → who waits. H2's `evalset/metrics.py` is one (the harness scores with it)."""

PENDING_HAND_WRITE: Final = frozenset({"vector_search", "retrieval_metrics"})
"""[HAND-WRITE] stubs still raising NotImplementedError, one switch each:

- H1: remove "vector_search" once `retrieval/vector.py` is written (its canary tests show
  XPASS(strict)). That one line turns on every check that runs it: the read paths above, PR 6's
  Postgres tests (`tests/answer/test_postgres_search_and_answer.py`) and PR 8's harness tests
  that search, which all ask `pending_hand_write` / `pending_hand_writes`.
- H2: remove "retrieval_metrics" once `evalset/metrics.py` is written (its own tests in
  `tests/evalset/test_metrics.py` show XPASS(strict)). That one line turns on H2's own tests and
  PR 8's harness tests that score with H2 (`tests/evalrun/`), which all ask `pending_hand_write` /
  `pending_hand_writes`."""


def pending_hand_write(stub: str) -> list[pytest.MarkDecorator]:
    """The marks for a test that runs `stub`: a strict xfail on NotImplementedError while the stub
    is pending, none once it is written (so the test must then simply pass)."""
    return pending_hand_writes(stub)


def pending_hand_writes(*stubs: str) -> list[pytest.MarkDecorator]:
    """The marks for a test that runs every stub in `stubs`: one strict xfail on
    NotImplementedError while any of them is pending, none once all are written."""
    pending = [stub for stub in stubs if stub in PENDING_HAND_WRITE]
    if not pending:
        return []
    return [
        pytest.mark.xfail(
            strict=True,
            raises=NotImplementedError,
            reason=f"[HAND-WRITE] {', '.join(pending)} not written yet"
            " (tests/canary/read_paths.py, PENDING_HAND_WRITE)",
        )
    ]


def read_path_params() -> list[object]:
    """`READ_PATHS`' names for `pytest.mark.parametrize`, a pending one marked strict xfail."""
    return [
        pytest.param(
            name,
            marks=pending_hand_write(NEEDS_HAND_WRITE[name]) if name in NEEDS_HAND_WRITE else [],
        )
        for name in sorted(READ_PATHS)
    ]
