"""EXTENSION POINT: every way a business can read data, for the canary suite (ADR 0009).

A read path acts as one business and returns what it produced, as text per label. The canary test
(`test_no_read_path_shows_another_businesss_canary`) runs every entry of `READ_PATHS` as each
business and fails if the other business's id or canary appears, or if its own never does.

The paths: plain SQL on every business table, M2 H1's top-k vector search (`m2/retrieval`), M3
H1's full-text search (`text_search`, M3 PR 3), M3 PR 4's hybrid search (both legs and M3 H2's
`rrf_fuse` in one snapshot, `search.fused_search`), and PR 6's two routes that show chunk text
(`GET /v1/kb/search`, `POST /v1/answer`, through the real app with that business's
`X-Business-Id`), each in vector mode and in hybrid mode (M3 PR 4). Add yours here, with no other
change.

**The business clause itself (M3 PR 3, ADR 0009 amendment).** Every path above runs as the app
role, under RLS, so it would pass with a search whose SQL forgot `c.business_id = :business_id`:
RLS hides the other business's rows anyway. The read paths in `OWNER_CONNECTION_PATHS` therefore
also run on the owner's connection (a superuser locally and in CI), where RLS never applies and
only the statement's own clause keeps the other business out
(`test_no_search_leg_shows_another_businesss_canary_where_rls_cannot_help`), with `MAX_K` hits and
with one, so a clause applied after the `LIMIT` shows too. The API routes cannot run there: the app
refuses a role that skips RLS (`db.verify_role`).

**The bait.** A search path is only a test if the other business's data could be returned. So
`test_leakage.plant` gives both businesses the same `BAIT` chunk (same text, the configured
embedding model, the fake embedder's vector of `BAIT`), titled with that business's own canary,
and PR 6's API paths ask `BAIT`. The other business's bait is then a same-model candidate that
scores 1.0, above any answer threshold: a route that leaked across businesses would return it,
and its title, the other canary, would show in the hits, the prompt and the sources. Since M3 PR 2
(migration 0005) each bait chunk also carries a `ts_config` (its document's language through the
platform map) and `BAIT` holds `BAIT_WORD`, a word no other chunk has, so a full-text leg (M3
H1's `text_search`, the `text_search` path below) has the other business's bait to leak as well
(zara's S2). `BAIT` also holds the word "canary", which each business's canary chunk (its text is
its canary, `canary-<hex>`) shares, so a working full-text search finds that chunk too.

**A path that runs [HAND-WRITE] stubs** names them in `NEEDS_HAND_WRITE` (a set: PR 4's hybrid
paths run three), and a stub that still raises NotImplementedError is listed in
`PENDING_HAND_WRITE`. While any of a path's stubs is, the canary test marks the path
`xfail(strict=True, raises=NotImplementedError)` (`read_path_params`, via `pending_hand_writes`).
`scripts/canary_gate.py` allows that xfail, and only that one, as a recorded gap: the PR stays
green, and the path is wired so that it runs the moment the stub is replaced. Then the path's
canary tests show XPASS(strict), a failure on purpose, or FAILED if it leaks: either way CI is red
until the stub's name is removed from `PENDING_HAND_WRITE`, and from then on the path is checked
like any other. **One switch per stub:** the stub's own tests and every other test that runs it
use the same `pending_hand_write`, so removing its name turns on every check at once.

No other `xfail` belongs anywhere in tests/canary/: the gate refuses any other reason, and
`tests/test_canary_checks.py` refuses an `xfail` written outside `pending_hand_writes`.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Final, Protocol

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from scripts.canary_gate import pending_reason
from tests.answer.providers import CitingProvider
from tests.canary.catalog import business_tables
from tests.canary.checks import Planted
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.fusion import RRF_K, rrf_fuse
from vectapilot_api.retrieval.search import fused_search
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import MAX_K, RetrievalMode, TextQuery, VectorQuery
from vectapilot_api.retrieval.vector import vector_search

ReadPath = Callable[[AsyncEngine, Planted], Awaitable[Mapping[str, str]]]
"""(the app role's engine, the business to act as) -> {label: everything the path returned}."""


class SearchLegPath(Protocol):
    """A search leg's read path: a `ReadPath` that can also ask for fewer hits than `MAX_K`."""

    def __call__(
        self, engine: AsyncEngine, me: Planted, *, k: int = MAX_K
    ) -> Awaitable[Mapping[str, str]]: ...


CANARY_EMBEDDING: Final = (0.0,) * 1023 + (1.0,)
"""The direction `test_leakage.plant` stores every canary chunk with (its `VECTOR`)."""

BAIT_WORD: Final = "zq7bait3"
"""The rare word both bait chunks share. Letters and digits, so Postgres' parser reads it as a
`numword`, which `english`, `hindi` and `simple` all keep as this one unchanged lexeme: a full-text
query matches both bait chunks whatever config each is stored or parsed with. No file of the sample
corpus or the eval datasets holds it (tests/test_canary_checks.py), so with a missing business
clause the other business's bait is among the very few matches, not lost in a ranked list."""

BAIT: Final = (
    "Bait for the canary suite: the same question and chunk text for every business,"
    f" with one rare word, {BAIT_WORD}."
)
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


async def vector_search_as_staff(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """H1's `vector_search` as `me`, once per embedding model id `me`'s chunks carry (`fake` for
    the canary chunk, the configured model for the bait), every hit as JSON. Staff is the widest
    reader (public and staff-only), `k` the most allowed unless asked for fewer, and the question
    points straight at the canary chunk, so a working search shows `me`'s canary (the bait's title
    is `me`'s canary too), at any `k`.

    Under the app role it proves isolation through RLS, which would hide a missing
    `WHERE business_id` clause. So it is also in `OWNER_CONNECTION_PATHS`: on the owner's
    connection only the clause keeps the other business's canary chunk (the same vector) and bait
    out. M2's own clause tests stay in `tests/retrieval/test_vector_search.py`: mirrored here, not
    moved (decided by shesha, 2026-10-24)."""
    outputs: dict[str, str] = {}
    async with business_transaction(engine, me.business_id) as connection:
        models = (await connection.execute(_STORED_MODELS, {"b": me.business_id})).scalars()
        for model in list(models):
            query = VectorQuery(
                business_id=me.business_id,
                embedding=CANARY_EMBEDDING,
                embedding_model=model,
                k=k,
                reader="staff",
            )
            hits = await vector_search(connection, query)
            outputs[f"vector_search:{model}"] = "\n".join(hit.model_dump_json() for hit in hits)
    return outputs


async def text_search_as_staff(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """M3 H1's `text_search` as `me`, asking `BAIT`, once per embedding model id `me`'s chunks
    carry, every hit as JSON. As for `vector_search_as_staff`: staff, the largest `k` unless asked
    for fewer, and the canary direction as the question's vector (the score is the cosine to it;
    it never decides a match).

    A working search shows `me`'s canary in both outputs: the canary chunk (model `fake`) holds
    the word "canary" (`english` makes `canari` of it in the chunk and in `BAIT`), and the bait
    (the configured model) holds every word of `BAIT`, with `me`'s canary as its title. The other
    business's canary chunk and bait match the same words, so a search that leaks shows its
    canary. Also in `OWNER_CONNECTION_PATHS`, for the business clause itself."""
    outputs: dict[str, str] = {}
    async with business_transaction(engine, me.business_id) as connection:
        models = (await connection.execute(_STORED_MODELS, {"b": me.business_id})).scalars()
        for model in list(models):
            query = TextQuery(
                business_id=me.business_id,
                question=BAIT,
                embedding=CANARY_EMBEDDING,
                embedding_model=model,
                k=k,
                reader="staff",
            )
            hits = await text_search(connection, query)
            outputs[f"text_search:{model}"] = "\n".join(hit.model_dump_json() for hit in hits)
    return outputs


async def hybrid_search_as_staff(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """M3 PR 4's hybrid search as `me`, asking `BAIT`: the product's own `fused_search` (M2 H1's
    `vector_search`, M3 H1's `text_search`, then M3 H2's `rrf_fuse`) in one snapshot business
    transaction, as `SearchService` runs it, once per embedding model id `me`'s chunks carry,
    every fused hit as JSON. As for the two legs: staff, `k` hits from each leg and in the fused
    list, and the canary direction as the question's vector.

    A working search shows `me`'s canary in both outputs (each leg finds `me`'s canary chunk or
    bait, titled with `me`'s canary), and a leg that leaks puts the other business's canary into
    the fused list. Also in `OWNER_CONNECTION_PATHS`: both legs' business clauses, through the
    fusion. The legs and the fusion are looked up here, so a red proof that replaces one leg in
    this module replaces it for this path too."""
    outputs: dict[str, str] = {}
    async with business_transaction(engine, me.business_id, snapshot=True) as connection:
        models = (await connection.execute(_STORED_MODELS, {"b": me.business_id})).scalars()
        for model in list(models):
            query = TextQuery(
                business_id=me.business_id,
                question=BAIT,
                embedding=CANARY_EMBEDDING,
                embedding_model=model,
                k=k,
                reader="staff",
            )
            lists = await fused_search(
                connection,
                query,
                limit=k,
                rrf_k=RRF_K,
                vector=vector_search,
                text=text_search,
                fuse=rrf_fuse,
            )
            outputs[f"hybrid_search:{model}"] = "\n".join(
                item.hit.model_dump_json() for item in lists.fused
            )
    return outputs


def _reindex(client: TestClient, me: Planted) -> None:
    """Re-embed `me`'s canary chunk with the configured (fake) model: `test_leakage.plant` stores
    it under model id `fake` next to the bait under the configured one, and search refuses mixed
    models (`reindex_required`). Afterwards all of `me`'s chunks carry the configured model; the
    canary chunk scores about 0 against `BAIT`, the bait 1.0."""
    response = client.post("/v1/kb/reindex", headers={BUSINESS_HEADER: me.business_id})
    assert response.is_success, response.text


async def _search_through_the_api(me: Planted, mode: RetrievalMode, label: str) -> dict[str, str]:
    with TestClient(create_app(Settings(rag_retrieval_mode=mode))) as client:
        _reindex(client, me)
        response = client.get(
            "/v1/kb/search",
            params={"q": BAIT, "k": MAX_K},
            headers={BUSINESS_HEADER: me.business_id},
        )
    assert response.is_success, response.text
    return {label: response.text}


async def search_through_the_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`GET /v1/kb/search` as `me` (reader customer: approved public chunks), asking `BAIT` with
    the largest `k`; the response body as text (`me`'s bait shows `me`'s canary as its title).
    Through the real app, as the app role: it proves the route's isolation through RLS and its own
    scoping together."""
    return await _search_through_the_api(me, "vector", "GET /v1/kb/search")


async def search_through_the_api_hybrid(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`search_through_the_api` with `rag_retrieval_mode=hybrid` (M3 PR 4): the route runs both
    legs and the fusion. Both legs find `me`'s bait (same vector as `BAIT`, and `BAIT_WORD`), and
    the other business's bait is a candidate for both."""
    return await _search_through_the_api(me, "hybrid", "GET /v1/kb/search (hybrid)")


async def _answer_through_the_api(me: Planted, mode: RetrievalMode, label: str) -> dict[str, str]:
    provider = CitingProvider()
    settings = Settings(rag_retrieval_mode=mode)
    with TestClient(create_app(settings, llm_provider=provider)) as client:
        _reindex(client, me)
        response = client.post(
            "/v1/answer",
            json={"question": BAIT},
            headers={BUSINESS_HEADER: me.business_id},
        )
    assert response.is_success, response.text
    prompts = [f"{call.system}\n{call.messages[-1].content}" for call in provider.calls]
    return {label: response.text, f"{label} (prompt)": "\n".join(prompts)}


async def answer_through_the_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`POST /v1/answer` as `me`, asking `BAIT`, with a scripted model that cites every real block
    of its prompt; the response body (its `sources` show chunk text and titles) and every prompt
    the model got (business-context §5: a prompt only ever holds the current business's data).
    `me`'s bait scores 1.0, so it is evidence, and its title, `me`'s canary, is the positive
    control in both outputs."""
    return await _answer_through_the_api(me, "vector", "POST /v1/answer")


async def answer_through_the_api_hybrid(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`answer_through_the_api` with `rag_retrieval_mode=hybrid` (M3 PR 4): the prompt holds the
    top `rag_context_k` fused hits whose cosine passes the gate (`me`'s bait scores 1.0)."""
    return await _answer_through_the_api(me, "hybrid", "POST /v1/answer (hybrid)")


READ_PATHS: Final[dict[str, ReadPath]] = {
    "sql": sql_on_every_business_table,
    "vector_search": vector_search_as_staff,
    "text_search": text_search_as_staff,
    "hybrid_search": hybrid_search_as_staff,
    "GET /v1/kb/search": search_through_the_api,
    "POST /v1/answer": answer_through_the_api,
    "GET /v1/kb/search (hybrid)": search_through_the_api_hybrid,
    "POST /v1/answer (hybrid)": answer_through_the_api_hybrid,
}

OWNER_CONNECTION_PATHS: Final[Mapping[str, SearchLegPath]] = {
    "vector_search": vector_search_as_staff,
    "text_search": text_search_as_staff,
    "hybrid_search": hybrid_search_as_staff,
}
"""Read paths that also run on the owner's connection, where RLS never applies: each search leg's
own `WHERE c.business_id = :business_id` is then the only thing between `me` and the other
business (M3 PR 3, ADR 0009 amendment). The same functions as in `READ_PATHS`.

Each runs there twice: with `MAX_K` hits, and with one. With `MAX_K`, every candidate of both
businesses fits, so a leg that applies its business filter after its `LIMIT` (in a subquery, or in
Python) still returns exactly `me`'s rows. With one, the other business's equally good chunk can
take the only place before the filter, and `me` gets nothing: the positive control turns red.

Not "sql" (it reads whole tables on purpose, so it leaks there by design:
`test_red_proof_a_role_that_skips_rls_leaks_the_canary`), and not the API routes (the app refuses
such a role). `hybrid_search` (M3 PR 4) runs both legs' clauses through the fusion here."""

HYBRID: Final = frozenset({"vector_search", "text_search", "rrf_fuse"})
"""What every hybrid read path runs (M3 PR 4): both legs and the fusion."""

NEEDS_HAND_WRITE: Final[Mapping[str, frozenset[str]]] = {
    "vector_search": frozenset({"vector_search"}),
    "text_search": frozenset({"text_search"}),
    "hybrid_search": HYBRID,
    "GET /v1/kb/search": frozenset({"vector_search"}),
    "POST /v1/answer": frozenset({"vector_search"}),
    "GET /v1/kb/search (hybrid)": HYBRID,
    "POST /v1/answer (hybrid)": HYBRID,
}
"""Read path → the [HAND-WRITE] stubs it runs (M2 H1's `vector_search` for PR 6's routes, M3 H1's
`text_search` for its own path, and both legs and M3 H2's `rrf_fuse` for M3 PR 4's hybrid
paths)."""

NEEDED_OUTSIDE_READ_PATHS: Final[Mapping[str, str]] = {}
"""[HAND-WRITE] stubs that no read path runs yet, but other tests wait for through this same
switch: stub → who waits. Empty since M3 PR 4: M3 H2's `rrf_fuse` left when the hybrid read paths
came, which run it."""

PENDING_HAND_WRITE: Final[frozenset[str]] = frozenset()
"""[HAND-WRITE] stubs still raising NotImplementedError, one switch each. Remove a name once
every test that runs it shows XPASS(strict) (guide: docs/learning/m3-hybrid.md §1):

- None pending today. M3 H1 "text_search" and M3 H2 "rrf_fuse" were removed on 2026-10-28 (both
  written by Claude at shesha's request, for the deadline), so their own tests, the `text_search`
  and hybrid read paths, the owner-connection checks and PR 4/5's real-leg tests are plain tests.

For the record: M2 H1 "vector_search" and M2 H2 "retrieval_metrics" were removed on 2026-10-22
(both written by Claude at shesha's request), so every check that runs them is a plain test."""


def pending_hand_write(stub: str) -> list[pytest.MarkDecorator]:
    """The marks for a test that runs `stub`: a strict xfail on NotImplementedError while the stub
    is pending, none once it is written (so the test must then simply pass)."""
    return pending_hand_writes(stub)


def pending_hand_writes(*stubs: str) -> list[pytest.MarkDecorator]:
    """The marks for a test that runs every stub in `stubs`: one strict xfail on
    NotImplementedError while any of them is pending, none once all are written. Its reason is
    the one `scripts/canary_gate.py` accepts (`pending_reason`); this is the only place in
    tests/canary/ that may make an xfail (tests/test_canary_checks.py checks)."""
    pending = [stub for stub in stubs if stub in PENDING_HAND_WRITE]
    if not pending:
        return []
    return [
        pytest.mark.xfail(strict=True, raises=NotImplementedError, reason=pending_reason(pending))
    ]


def read_path_params(names: Iterable[str] = READ_PATHS) -> list[object]:
    """`names` (all of `READ_PATHS` by default) for `pytest.mark.parametrize`, sorted; a path on
    a pending stub is marked strict xfail."""
    return [
        pytest.param(name, marks=pending_hand_writes(*sorted(NEEDS_HAND_WRITE.get(name, ()))))
        for name in sorted(names)
    ]
