"""EXTENSION POINT: every way a business can read data, for the canary suite (ADR 0009).

A read path acts as one business and returns what it produced, as text per label. The canary test
(`test_no_read_path_shows_another_businesss_canary`) runs every entry of `READ_PATHS` as each
business and fails if the other business's id or canary appears, or if its own never does.

The paths: plain SQL on every business table, M2 H1's top-k vector search (`m2/retrieval`), M3
H1's full-text search (`text_search`, M3 PR 3), M3 PR 4's hybrid search (both legs and M3 H2's
`rrf_fuse` in one snapshot, `search.fused_search`), and PR 6's two routes that show chunk text
(`GET /v1/kb/search`, `POST /v1/answer`, through the real app with that business's
`X-Business-Id`, with `guard_answer = "off"` pinned), each in vector mode and in hybrid mode (M3
PR 4), the same two routes with `guard_answer = "on"` in both modes (M7 PR 8's guarded pipeline,
a PR 10 precondition: "M7's guarded routes" below), and M5 PR 2's tools (`check_slots`,
`order_status` and the `book` and `reschedule` proposal checks, through the executor, over the
sandbox bait), and M7 PR 7's KB scan (`guards scan`). Add yours here, with no other change.

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

**M7's guarded routes** (`guard_answer = "on"`, M7 PR 8; decided 2026-11-23 as PR 10's
precondition). The real app builds the guarded pipeline itself, as production does: the red-flag
screen, the limits and the budget on the real Redis (each test's planted business has fresh
windows; the limits and the budget are pinned to the test suite's, `GUARDED_LIMITS`), masking, a
separate `AnswerService` over the chunk-screening search and its own `BudgetedGateway`, and H3's
output check, with the real term files (`data/safety/`), where a planted business is unresolved
and gets every term (G4). Two stand-ins, so the paths do not wait on a
pending [HAND-WRITE]: a no-hit matcher (`LookupMatcher`, not H1) and a no-finding output check
(`FixedCheck`, not H3). Both are pure and only ever tighten (a hit drops a chunk or hands off, a
finding withholds an answer), so neither can move a read; with their stubs every request would be
a `guard_error` hand-off or a 503 and nothing of either business would show. H1 itself still runs
in the startup self-test (G14), as for the guarded extraction write path. The masked `BAIT` is
`BAIT` (no PII), so the embedder still asks the bait's own vector. Besides the leak check, each
path asserts it went through the guards (non-vacuity): exactly one `guard` record, for `me`, its
route and the response's trace id, that let the question through after the clean, red-flag,
language, limits, injection and mask steps; the answer path also that it made exactly one model
call, reserved on the guarded route's own budget (M3's gateway reserves nothing), and that H3's
seam saw the shown answer with every cited chunk's clean text (only this request's chunk screen
supplies one). Every metadata record the request logged (`guard`, `embedding_call`, `llm_call`,
`rag_answer`) is an output too, so a record written for the other business would show its id.

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

import functools
import json
import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from scripts.canary_gate import pending_reason
from tests.answer.providers import CitingProvider
from tests.canary.catalog import business_tables
from tests.canary.checks import Planted
from tests.conftest import TEST_SETTINGS_FIELDS
from tests.guards.agent_helpers import FixedCheck, LookupMatcher
from tests.guards.helpers import SAFETY_DIR
from tests.tools.helpers import CORPUS_DIR
from tests.tools.sandbox_db import fresh_engine, owner_url
from vectapilot_api import main as api_main
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.guards.answer import AnswerGuard
from vectapilot_api.guards.config import load_guard_dir
from vectapilot_api.guards.output import OutputKind
from vectapilot_api.guards.scan import DatabaseChunks, scan_business
from vectapilot_api.guards.types import GUARD_RECORD, Check, Folded, RouteSlug, TermHit, TermSet
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.fusion import RRF_K, rrf_fuse
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import fused_search
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import MAX_K, RetrievalMode, TextQuery, VectorQuery
from vectapilot_api.retrieval.vector import vector_search
from vectapilot_api.tools.config import Service, TicketCategory, ToolName, ToolsConfig
from vectapilot_api.tools.executor import ObservationCaps, ToolExecutor
from vectapilot_api.tools.types import ToolContext, ToolKind

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

BAIT_SLOT_IDS: Final = ("S-BAIT-A", "S-BAIT-B")
BAIT_BOOKING_ID: Final = "B-BAIT"
BAIT_ORDER_ID: Final = "BAIT-1"
BAIT_CUSTOMER: Final = "cust-bait"
BAIT_SERVICE: Final = "canary"
"""The sandbox bait (M5 PR 1, ADR 0016 decision 6): `test_leakage.plant` gives both businesses
the same two slots, a booking and an order under these same ids, for the same customer, so a tool
read path (PR 2: `check_slots`, `order_status`, the proposal checks) has the other business's row
under the very id it asks for. Each business's rows carry its own canary: the slots' labels (and
so the booking, through its slot: bookings have no free-text column, Q7) and the order's note. A
path that leaked would show the other canary.

**The booking is asymmetric** (M5 PR 2, zara's review): business `a` books `S-BAIT-A` and `b`
books `S-BAIT-B` (`bait_slots`), so each business's free slot is the one the other business has
booked. A correlated booking check that lost its own business clause (`b.business_id =
s.business_id` in `check_slots`' NOT EXISTS or `book`'s EXISTS) then sees the other business's
booking of `me`'s free slot: `check_slots` lists nothing and the proposals are refused, so the
positive control turns red. With symmetric bait (both book the same id) that clause was
invisible: the other business's booking only ever hid a slot `me` had booked anyway."""


BAIT_CONFIRM_TOKEN: Final = "CanaryConfirmToken0001"  # noqa: S105 (the bait's token, synthetic)
"""The confirm token of every planted business's bait action (M5 PR 3), shared on purpose like the
bait ids: confirming another business's action id with it fails only because of that business's
clause (RLS, and the confirm statements' own `business_id = :business_id` on the owner's
connection), never because the token differs."""


BAIT_EXTRACTION_ID: Final = uuid5(NAMESPACE_URL, "vectapilot-canary-extraction")
"""The extraction id every planted business holds (M6 PR 3, migration 0007), shared on purpose
like the sandbox bait ids: the database draws an extraction's id and the app role cannot choose
one, so the owner plants it (`test_leakage.plant`). Its one field's value and span are the
business's own canary, so a path that reads an extraction by id without its business clause shows
the other canary."""


def bait_action_id(business_id: str) -> UUID:
    """The id of `business_id`'s planted, confirmable `book` action (`test_leakage.plant`): derived
    from the business id, so a write path can name it without reading the database. A planted
    business id is random per run, so the id is too."""
    return uuid5(NAMESPACE_URL, f"vectapilot-canary-action:{business_id}")


def bait_slots(business_id: str) -> tuple[str, str]:
    """`(booked, free)`: the bait slot `test_leakage.plant` books for a planted business, and its
    other, free one. A planted business id is `canary-<letter>-<hex>`; `a` books the first."""
    letter = business_id.split("-")[1]
    booked = {"a": 0, "b": 1}[letter]
    return BAIT_SLOT_IDS[booked], BAIT_SLOT_IDS[1 - booked]


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
            # Sorted: without ORDER BY, Postgres may return the same rows in another order after an
            # UPDATE, and the "nothing changed" checks compare these strings (flaked in CI on
            # 2026-11-11). Sorting keeps every row in the comparison; only the order is ignored.
            outputs[f"sql:{table}"] = "\n".join(sorted(row[0] for row in rows))
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
    settings = Settings(rag_retrieval_mode=mode, guard_answer="off")  # M3's route: module docstring
    with TestClient(create_app(settings)) as client:
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
    scoping together. `guard_answer = "off"` explicitly, so M3's unguarded route (still reachable
    outside production) keeps this canary whatever a `.env` or a later default says."""
    return await _search_through_the_api(me, "vector", "GET /v1/kb/search")


async def search_through_the_api_hybrid(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`search_through_the_api` with `rag_retrieval_mode=hybrid` (M3 PR 4): the route runs both
    legs and the fusion. Both legs find `me`'s bait (same vector as `BAIT`, and `BAIT_WORD`), and
    the other business's bait is a candidate for both."""
    return await _search_through_the_api(me, "hybrid", "GET /v1/kb/search (hybrid)")


async def _answer_through_the_api(me: Planted, mode: RetrievalMode, label: str) -> dict[str, str]:
    provider = CitingProvider()
    settings = Settings(rag_retrieval_mode=mode, guard_answer="off")  # M3's route: module docstring
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
    control in both outputs. `guard_answer = "off"` explicitly, as `search_through_the_api`: each
    M3 entry and its guarded twin are off and on by construction."""
    return await _answer_through_the_api(me, "vector", "POST /v1/answer")


async def answer_through_the_api_hybrid(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`answer_through_the_api` with `rag_retrieval_mode=hybrid` (M3 PR 4): the prompt holds the
    top `rag_context_k` fused hits whose cosine passes the gate (`me`'s bait scores 1.0)."""
    return await _answer_through_the_api(me, "hybrid", "POST /v1/answer (hybrid)")


# ── M7's guarded routes: `guard_answer = "on"` (module docstring) ─────────────────────────────

GUARDED_CHECKS: Final = frozenset(
    {
        Check.CLEAN,
        Check.RED_FLAG,
        Check.NOT_COVERED_LANGUAGE,
        Check.LIMITS,
        Check.INJECTION,
        Check.MASK,
    }
)
"""G1's steps both guarded routes run on a question every guard lets through (`guards.answer`);
the answer route also runs H3's output check (`Check.OUTPUT`)."""

GUARDED_LIMITS: Final[dict[str, Any]] = {
    name: TEST_SETTINGS_FIELDS[name]
    for name in (
        "guard_limit_business_per_minute",
        "guard_limit_customer_per_minute",
        "guard_budget_usd_per_business_per_day",
    )
}
"""The guarded routes' limits and budget, pinned to the test suite's (`TEST_SETTINGS_FIELDS`,
far above one request: CI's integration job shares one Redis database) on the real Redis."""


class MetadataRecords(logging.Handler):
    """Every metadata record logged under `vectapilot_api` while attached: the `guard`,
    `embedding_call`, `llm_call` and `rag_answer` records each carry their fields as the `extra`
    named by their message (metadata only, G10: ids, codes and counts, never text)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, dict[str, Any]]] = []

    def emit(self, record: logging.LogRecord) -> None:
        name = record.getMessage()
        fields = getattr(record, name, None)
        if isinstance(fields, dict):
            self.records.append((name, fields))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [fields for logged, fields in self.records if logged == name]

    def text(self) -> str:
        return "\n".join(
            json.dumps({name: fields}, sort_keys=True, default=str) for name, fields in self.records
        )


@contextmanager
def recording() -> Iterator[MetadataRecords]:
    """The metadata records logged inside this block (the app logs from its own thread too)."""
    handler = MetadataRecords()
    logger = logging.getLogger("vectapilot_api")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


@contextmanager
def served_guarded(
    mode: RetrievalMode, provider: CitingProvider, check: FixedCheck
) -> Iterator[TestClient]:
    """The real app, as the app role, with `guard_answer = "on"`, the real term files and the
    real Redis limiter, `provider` as the answer model, and the two stand-ins (module docstring):
    `LookupMatcher` through `create_app`'s test seam and `check` as `AnswerGuard`'s output check,
    patched in before the lifespan builds the guard (as tests/guards/test_answer_guard_db.py).
    The limits and the budget are the test suite's (`GUARDED_LIMITS`), never a `.env`'s: a small
    budget would turn this isolation gate red with a `budget` hand-off that says nothing about
    isolation (Redis keeps its own canaries, tests/canary/test_redis_isolation.py)."""
    settings = Settings(
        rag_retrieval_mode=mode,
        guard_answer="on",
        guard_config_dir=SAFETY_DIR,
        corpus_dir=CORPUS_DIR,
        tools_config_dir=None,  # a developer's .env may name the sample folders: not here
        extraction_config_dir=None,
        **GUARDED_LIMITS,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api_main, "AnswerGuard", functools.partial(AnswerGuard, check=check))
        app = create_app(settings, llm_provider=provider, matcher=LookupMatcher())
        with TestClient(app) as client:
            yield client


def the_guard_record(
    records: MetadataRecords, me: Planted, route: RouteSlug, response: httpx2.Response
) -> dict[str, Any]:
    """The request's one `guard` record (non-vacuity): written for `me`, on `route`, under the
    response's trace id, after every step of `GUARDED_CHECKS`, and letting the response pass."""
    guards = records.named(GUARD_RECORD)
    assert len(guards) == 1, f"{len(guards)} guard records: the guarded route did not run once"
    [record] = guards
    assert (record["business_id"], record["route"]) == (me.business_id, route.value), record
    assert record["trace_id"] == response.headers[TRACE_HEADER], record
    assert (record["action"], record["decided_by"]) == ("pass", "none"), record
    assert {check.value for check in GUARDED_CHECKS} <= set(record["checks"]), record
    return record


async def _search_through_the_guarded_api(
    me: Planted, mode: RetrievalMode, label: str
) -> dict[str, str]:
    with served_guarded(mode, CitingProvider(), FixedCheck()) as client:
        _reindex(client, me)
        with recording() as records:
            response = client.get(
                "/v1/kb/search",
                params={"q": BAIT, "k": MAX_K},
                headers={BUSINESS_HEADER: me.business_id},
            )
    assert response.is_success, response.text
    the_guard_record(records, me, RouteSlug.SEARCH, response)
    return {label: response.text, f"{label} (records)": records.text()}


async def search_through_the_guarded_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`search_through_the_api` with `guard_answer = "on"` (module docstring): the guards run
    before the search, which embeds the clean, masked question; its hits are not screened (M8).
    The response body (`me`'s bait shows `me`'s canary as its title) and the request's metadata
    records."""
    return await _search_through_the_guarded_api(me, "vector", "GET /v1/kb/search (guarded)")


async def search_through_the_guarded_api_hybrid(
    engine: AsyncEngine, me: Planted
) -> Mapping[str, str]:
    """`search_through_the_guarded_api` with `rag_retrieval_mode=hybrid`: both legs and the
    fusion get the masked question."""
    return await _search_through_the_guarded_api(
        me, "hybrid", "GET /v1/kb/search (hybrid, guarded)"
    )


async def _answer_through_the_guarded_api(
    me: Planted, mode: RetrievalMode, label: str
) -> dict[str, str]:
    provider, check = CitingProvider(), FixedCheck()
    with served_guarded(mode, provider, check) as client:
        _reindex(client, me)
        with recording() as records:
            response = client.post(
                "/v1/answer",
                json={"question": BAIT},
                headers={BUSINESS_HEADER: me.business_id},
            )
    assert response.is_success, response.text
    record = the_guard_record(records, me, RouteSlug.ANSWER, response)
    assert Check.OUTPUT.value in record["checks"], record
    # Exactly one model call, reserved on the guarded route's own `BudgetedGateway`: M3's gateway,
    # which serves while the guards are off, reserves nothing, and a miswire that also answered
    # through M3's service would call the model twice.
    assert len(provider.calls) == 1, provider.calls
    reserved = record["reserved_micro_usd"]
    assert isinstance(reserved, int) and reserved > 0, record
    # H3's seam saw the shown answer once, with the clean text of every chunk it cites: only this
    # request's chunk screen supplies those (`guards.search`; any other citation is a KeyError).
    body = response.json()
    asked = [(reply, context.kind, len(context.cited)) for reply, context in check.asked]
    assert asked == [(body["answer"], OutputKind.ANSWER, len(body["sources"]))], asked
    prompts = [f"{call.system}\n{call.messages[-1].content}" for call in provider.calls]
    return {
        label: response.text,
        f"{label} (prompt)": "\n".join(prompts),
        f"{label} (records)": records.text(),
    }


async def answer_through_the_guarded_api(engine: AsyncEngine, me: Planted) -> Mapping[str, str]:
    """`answer_through_the_api` with `guard_answer = "on"` (module docstring): the masked `BAIT`,
    the chunk screen before the prompt, the guarded route's own budgeted model call and H3's
    seam. The response body (its `sources` show titles), every prompt the model got and the
    request's metadata records; `me`'s bait scores 1.0 and passes the screen, so its title, `me`'s
    canary, is the positive control in the body and the prompt (the records show `me`'s id)."""
    return await _answer_through_the_guarded_api(me, "vector", "POST /v1/answer (guarded)")


async def answer_through_the_guarded_api_hybrid(
    engine: AsyncEngine, me: Planted
) -> Mapping[str, str]:
    """`answer_through_the_guarded_api` with `rag_retrieval_mode=hybrid`: the chunk screen keeps
    only the fused hits it let through (`guards.search`)."""
    return await _answer_through_the_guarded_api(me, "hybrid", "POST /v1/answer (hybrid, guarded)")


class EngineTransactions:
    """`Database.business_transaction` on a bare engine, without the app's role check: the app
    role's engine (under RLS) or the owner's (a superuser, where RLS never applies and only each
    tool statement's own `business_id = :business_id` keeps the other business out)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    def business_transaction(
        self, business_id: str, *, snapshot: bool = False, read_committed: bool = False
    ) -> AbstractAsyncContextManager[AsyncConnection]:
        return business_transaction(
            self.engine, business_id, snapshot=snapshot, read_committed=read_committed
        )


def bait_tools(business_id: str) -> ToolsConfig:
    """A planted business's tool configuration: every tool, the bait's one service, UTC, and
    today (UTC) as the sandbox date, so the bait slots (`now() + 1 day`) are in the future and in
    `check_slots`' range. Built here: a planted business has no `tools.json`."""
    return ToolsConfig(
        business_id=business_id,
        synthetic=True,
        business_type=BusinessType.VETERINARY,
        timezone="UTC",
        sandbox_date=datetime.now(UTC).date(),
        enabled_tools=tuple(ToolName),
        services=(Service(id=BAIT_SERVICE, name="Canary service"),),
        ticket_categories=(TicketCategory(id="other", name="Something else"),),
        emergency_guidance=("Call the canary line.",),
    )


async def _through_the_executor(
    engine: AsyncEngine, me: Planted, label: str, tool: ToolName, arguments: dict[str, str], k: int
) -> dict[str, str]:
    """`tool` as `me`'s bait customer through `ToolExecutor`: its observation, then the business
    ids of every row it read (the envelope, ADR 0016 decision 1). The observation shows `me`'s
    canary where the tool shows free text (a slot label, an order note); a proposal's summary
    never does (W3), so for those the rows' ids are the positive control, and a statement that
    lost its business clause would add the other business's id. `k` caps the rows shown
    (`ObservationCaps.max_rows`): at 1, a filter applied after the limit shows nothing of `me`.

    Every bait proposal should succeed. A refused one keeps the other businesses' ids (a leak
    still shows) but drops `me`'s, so the positive control turns red instead of the path
    raising: that is how a lost booking clause (`bait_slots`) is caught."""
    tools = ToolExecutor(
        EngineTransactions(engine),
        bait_tools(me.business_id),
        caps=ObservationCaps(max_rows=min(k, 20)),
    )
    context = ToolContext(business_id=me.business_id, customer_ref=BAIT_CUSTOMER)
    if tools.kind(tool) is ToolKind.READ:
        envelope = await tools.run(tool, arguments, context)
        rows = ",".join(sorted(envelope.business_ids))
        return {label: f"{envelope.observation}\nrows: {rows}"}
    envelope = await tools.propose(tool, arguments, context)
    if envelope.proposal is not None:
        rows = ",".join(sorted(envelope.business_ids))
        return {label: f"{envelope.observation}\nrows: {rows}"}
    foreign = ",".join(sorted(envelope.business_ids - {me.business_id}))
    return {label: f"{envelope.observation}\nno proposal; rows of other businesses: {foreign}"}


async def check_slots_as_the_bait_customer(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """`check_slots` over today and the next 13 days: `me`'s free bait slot, labelled with `me`'s
    canary (the booked one is not free). The other business's free bait slot is in the same time
    window, so a statement without its outer business clause would list it too; one without its
    booking check's business clause would hide `me`'s (the other business booked that id)."""
    today = datetime.now(UTC).date()
    arguments = {
        "date_from": today.isoformat(),
        "date_to": (today + timedelta(days=13)).isoformat(),
    }
    return await _through_the_executor(
        engine, me, "tool check_slots", ToolName.CHECK_SLOTS, arguments, k
    )


async def order_status_as_the_bait_customer(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """`order_status` for the shared bait order id: `me`'s order, its note `me`'s canary."""
    arguments = {"order_id": BAIT_ORDER_ID}
    return await _through_the_executor(
        engine, me, "tool order_status", ToolName.ORDER_STATUS, arguments, k
    )


async def book_proposal_as_the_bait_customer(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """`propose(book)` onto `me`'s free bait slot: the proposal checks read the slot and its
    bookings by id, so a missing business clause would read the other business's slot (a leak)
    or its booking of that id (a refusal: the positive control turns red)."""
    arguments = {"slot_id": bait_slots(me.business_id)[1]}
    return await _through_the_executor(
        engine, me, "tool book (propose)", ToolName.BOOK, arguments, k
    )


async def reschedule_proposal_as_the_bait_customer(
    engine: AsyncEngine, me: Planted, *, k: int = MAX_K
) -> Mapping[str, str]:
    """`propose(reschedule)` of the shared bait booking onto `me`'s free bait slot: the booking
    (by business, customer and id, joined to its slot) and the slot are both read by shared ids."""
    arguments = {"booking_id": BAIT_BOOKING_ID, "new_slot_id": bait_slots(me.business_id)[1]}
    return await _through_the_executor(
        engine, me, "tool reschedule (propose)", ToolName.RESCHEDULE, arguments, k
    )


TOOL_PATHS: Final[Mapping[str, SearchLegPath]] = {
    "tool check_slots": check_slots_as_the_bait_customer,
    "tool order_status": order_status_as_the_bait_customer,
    "tool book (propose)": book_proposal_as_the_bait_customer,
    "tool reschedule (propose)": reschedule_proposal_as_the_bait_customer,
}
"""M5 PR 2's tool read paths: both reads and the proposal checks that read rows. Not
`create_ticket`: its proposal reads no row, so there is nothing of either business to show."""


def _every_text_hits(text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
    """NOT H1: a hit for the first term it is given, on every text, so the scan lists every chunk
    it reads (H1 is pure: it cannot move a read, so the path never waits for it)."""
    first = terms.terms[0] if terms.terms else None
    return () if first is None else (TermHit(first.id, first.category, 0),)


_CHUNK_OWNERS: Final = text(
    "SELECT c.id::text, c.business_id, d.title FROM chunks c"
    " JOIN documents d ON d.business_id = c.business_id AND d.id = c.document_id"
    " WHERE c.id = ANY(CAST(:ids AS uuid[]))"
)


async def scan_as_staff(engine: AsyncEngine, me: Planted, *, k: int = MAX_K) -> Mapping[str, str]:
    """M7 PR 7's KB scan (`guards.scan`, the `guards scan` command's reader) as `me`, listing
    every approved chunk it reads (`_every_text_hits`). It prints chunk ids, which hold no canary,
    so each listed id is looked up as the owner (a superuser sees every business) and shown as
    its business id and its document's title (the planted canary): `me`'s for a working scan,
    the other business's if the scan read across. Also in `OWNER_CONNECTION_PATHS`: there only
    the statement's own business clause keeps the other business out. `k` caps the chunks
    shown."""
    guards = load_guard_dir(SAFETY_DIR, CORPUS_DIR).for_business(me.business_id)
    report = await scan_business(
        DatabaseChunks(EngineTransactions(engine)), me.business_id, guards, _every_text_hits
    )
    ids = [str(chunk.chunk_id) for chunk in report.flagged[:k]]
    owner = fresh_engine(owner_url())
    try:
        async with owner.connect() as connection:
            rows = (await connection.execute(_CHUNK_OWNERS, {"ids": ids})).all()
    finally:
        await owner.dispose()
    found = {row[0]: f"{row[1]} {row[2]}" for row in rows}
    assert set(found) == set(ids), "a listed chunk the owner cannot find"
    return {"guards scan": "\n".join(f"{i} {found[i]}" for i in ids)}


READ_PATHS: Final[dict[str, ReadPath]] = {
    "sql": sql_on_every_business_table,
    "vector_search": vector_search_as_staff,
    "text_search": text_search_as_staff,
    "hybrid_search": hybrid_search_as_staff,
    "GET /v1/kb/search": search_through_the_api,
    "POST /v1/answer": answer_through_the_api,
    "GET /v1/kb/search (hybrid)": search_through_the_api_hybrid,
    "POST /v1/answer (hybrid)": answer_through_the_api_hybrid,
    "GET /v1/kb/search (guarded)": search_through_the_guarded_api,
    "POST /v1/answer (guarded)": answer_through_the_guarded_api,
    "GET /v1/kb/search (hybrid, guarded)": search_through_the_guarded_api_hybrid,
    "POST /v1/answer (hybrid, guarded)": answer_through_the_guarded_api_hybrid,
    **TOOL_PATHS,
    "guards scan": scan_as_staff,
}

OWNER_CONNECTION_PATHS: Final[Mapping[str, SearchLegPath]] = {
    "vector_search": vector_search_as_staff,
    "text_search": text_search_as_staff,
    "hybrid_search": hybrid_search_as_staff,
    **TOOL_PATHS,
    "guards scan": scan_as_staff,
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
such a role). `hybrid_search` (M3 PR 4) runs both legs' clauses through the fusion here. M5 PR 2's
tool paths (`TOOL_PATHS`) run every tool statement's business clause here, through the executor;
for them `k` caps the rows shown."""

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
    "GET /v1/kb/search (guarded)": frozenset({"vector_search"}),
    "POST /v1/answer (guarded)": frozenset({"vector_search"}),
    "GET /v1/kb/search (hybrid, guarded)": HYBRID,
    "POST /v1/answer (hybrid, guarded)": HYBRID,
}
"""Read path → the [HAND-WRITE] stubs it runs (M2 H1's `vector_search` for PR 6's routes, M3 H1's
`text_search` for its own path, and both legs and M3 H2's `rrf_fuse` for M3 PR 4's hybrid
paths). M7's guarded routes run the same legs; they run M7's H1 and H3 as stand-ins (module
docstring), so they wait on neither."""

NEEDED_OUTSIDE_READ_PATHS: Final[Mapping[str, str]] = {
    "run_turn": "M5 H1's tests, the fake-provider turns and the POST /v1/agent write path",
    "route_fields": "M6 H1's tests and the POST /v1/extractions routing test",
    "match_terms": "M7 H1's tests and the guarded POST /v1/agent write path",
}
"""[HAND-WRITE] stubs that no read path runs, but other tests wait for through this same
switch: stub → who waits. M3 H2's `rrf_fuse` left in M3 PR 4, when the hybrid read paths came,
which run it. M5 H1's `run_turn` runs on no read path (the agent's reads are the tool paths,
checked without the loop); `write_paths.WRITE_NEEDS_HAND_WRITE` names the write path. M6 H1's
`route_fields` runs on no read path either: its own tests (tests/extraction/test_routing.py) and
the route's H1 test (tests/extraction/test_route.py) wait for it. The `POST /v1/extractions` write
path runs it too but is not marked: the stub fails closed into a stored all-review extraction, so
the write it checks happens either way (`write_paths.py`). M7 H3's `check_reply` left on
2026-11-23, when it was written."""

PENDING_HAND_WRITE: Final[frozenset[str]] = frozenset({"run_turn", "route_fields", "match_terms"})
"""[HAND-WRITE] stubs still raising NotImplementedError, one switch each. Remove a name once
every test that runs it shows XPASS(strict) (guide: docs/learning/m3-hybrid.md §1):

- "run_turn" (M5 H1, the agent loop, shesha's; guide: docs/learning/m5-agent-loop.md §1): its
  own tests (tests/agent/test_loop.py), the fake-provider turns through the real step model and
  tools (tests/agent/test_turns_db.py) and the `POST /v1/agent` write path
  (`write_paths.WRITE_NEEDS_HAND_WRITE`).
- "route_fields" (M6 H1, review routing per field, shesha's; guide: docs/learning/m6-routing.md
  §1): its own tests (tests/extraction/test_routing.py) and the route's H1 test
  (tests/extraction/test_route.py), moved to this switch by M6 PR 3 when the route came.
- "match_terms" (M7 H1, the term matcher, shesha's), added by M7 PR 6 with "check_reply" (M7
  H3): the guarded `POST /v1/agent` write path runs both (`write_paths.GUARD_STUBS`). Each
  hand-write's PR removes its name here once its own switch's tests show XPASS(strict); the
  path's strict xfail then waits on the names still listed. M7 H2's `mask_pii` was written on
  2026-11-20 (by Claude at shesha's request) before PR 6, so it never had an entry: the guarded
  path runs it as plain code. M7 H3's "check_reply" was removed on 2026-11-23 (written by Claude
  at shesha's request, D5 (b)): its own tests are plain tests, and the guarded write path waits
  on "match_terms" and "run_turn" alone.
- M3 H1 "text_search" and M3 H2 "rrf_fuse" were removed on 2026-10-28 (both
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
