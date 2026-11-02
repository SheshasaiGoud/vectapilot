"""Search latency at pilot size (M3 PR 8; ADR 0008, ADR 0014 decision 9, D14):
`python -m vectapilot_api.evalrun latency`.

**What it measures**, for every sample question, in its business's eval namespace (`namespace.py`:
the sample corpus as `ingest` stored it, with the fake embedder's vectors):

1. `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` of M2 H1's `vector_search` statement and of M3 H1's
   `text_search` statement, each exactly as the function sends it: the function runs once against
   a stand-in connection that records its one statement and its values (`captured`), and that SQL
   is explained with those values. From the plan: server-side planning and execution time, the
   rows returned, the shared blocks read, and every plan node (scan type, relation, index).
2. The product's own hybrid search: `SearchService` in hybrid mode, with the fake embedder.
   `SearchStats` gives its transaction, vector-leg and text-leg durations (monotonic clock, as
   the answer's `rag_answer` record has them). The transaction is what ADR 0008's revisit
   trigger calls the search transaction: the snapshot, `set_config`, the model check, both legs,
   the fusion and the commit.

**As the app role, under RLS.** Everything goes through `db.Database`, whose role check refuses a
superuser, a BYPASSRLS role or a table owner (the owner skips RLS locally), and every EXPLAIN
transaction checks `current_user` again. Each question gets its own business transaction, opened
as a snapshot like the API's hybrid path (`snapshot=True`: REPEATABLE READ READ ONLY), so
Postgres refuses any write in it.

**Read-only, checked.** Every EXPLAIN transaction must report `transaction_read_only = on` and
`repeatable read`. Before and after, the chunk and document ids of each measured business are
read and hashed; if they differ, the measurement is refused and nothing is written.

**Samples.** The questions are the corpus's own section headings (the last heading of each section
of every approved, public document; distinct per business, in manifest and section order). They are
corpus text, never agent-written eval questions, and searched only in their own business. One
untimed warm-up pass (by default), then `repeats` timed passes: every timed execution counts once,
so n = questions x repeats per statement. M1's `latency_summary` gives the median and the p95
(only at n >= 20). Every EXPLAIN must return as many rows as the product's leg did for the same
question (`SearchStats`), else the measurement is refused.

**What the numbers are.** Local Docker on an 8 GB Mac, the synthetic sample corpus, fake
embeddings: never a production number, and never compared with business-context §6's targets as a
pass. Search cost does not depend on the vectors' values (exact search reads every candidate), so
fake vectors are fine for latency; no quality number comes from them (ADR 0008). EXPLAIN plans the
statement with this run's values each time; the app's own prepared statement may switch to a
generic plan after five executions (`plan_cache_mode = auto`), which the search-transaction
durations include and the EXPLAIN plans do not show.
"""

import hashlib
import inspect
import json
import math
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.config import Settings
from vectapilot_api.db import APP_ROLE, Database, create_api_engine
from vectapilot_api.embeddings.base import EmbeddingSpec
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.measure.latency import LatencySummary, latency_summary
from vectapilot_api.retrieval.search import (
    RetrievalConfig,
    SearchService,
    SearchStats,
    new_trace_id,
)
from vectapilot_api.retrieval.text import text_search
from vectapilot_api.retrieval.types import (
    VISIBLE_AUDIENCES,
    Reader,
    SearchHit,
    TextQuery,
    VectorQuery,
)
from vectapilot_api.retrieval.vector import vector_search

LABEL: Final = "local Docker on an 8 GB Mac, synthetic sample corpus, fake embeddings"
"""Every report's label (M3 plan PR 8, rule 3)."""

LATENCY_DIR: Final = Path("evals", "reports", "latency")
"""Where the reports go, relative to the repository root: committed, `<date>-local.json|md`."""

EXPLAIN: Final = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
OPERATION: Final = "search-latency"
"""The `embedding_call` operation of the fake embeddings this measurement makes."""

READER: Final[Reader] = "customer"
"""The routes' reader: approved public chunks only."""

DEFAULT_REPEATS: Final = 5
DEFAULT_WARMUP: Final = 1

Leg = Literal["vector", "text"]
LEGS: Final[tuple[Leg, ...]] = ("vector", "text")
OPERATOR: Final[Mapping[Leg, str]] = {"vector": "<=>", "text": "@@"}
"""The operator each leg's question about an index is about (HNSW for `<=>`, GIN for `@@`)."""
FUNCTION: Final[Mapping[Leg, str]] = {
    "vector": "retrieval.vector.vector_search (M2 H1)",
    "text": "retrieval.text.text_search (M3 H1)",
}
INDEX_KEYS: Final = ("Index Cond", "Order By")
"""The keys of an index node that hold what the index itself evaluates (a condition, an
ordering operator); a `Filter` is evaluated on the rows afterwards."""


class LatencyRefused(Exception):
    """The measurement cannot run, or its result cannot be trusted. Ids and rules, never text."""


class CaptureError(LatencyRefused):
    """A leg did not send exactly one `SELECT` through `execute` with a dict of values."""


class PlanError(LatencyRefused):
    """EXPLAIN's output is not the JSON this module reads."""


# ── The sample questions (pure) ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SampleQuestion:
    """One section heading of the corpus, used as a question in its own business."""

    business_id: str
    """The dataset business (not the eval namespace)."""
    source_id: str
    text: str = field(repr=False)


def sample_questions(corpus: Corpus, business_ids: Sequence[str]) -> tuple[SampleQuestion, ...]:
    """The last heading of every section of each business's customer-visible documents (approved
    and public), stripped, distinct per business, in business, manifest and section order.
    Sections without a heading (CSV records, PDF pages) give none."""
    questions: list[SampleQuestion] = []
    for business in business_ids:
        seen: set[str] = set()
        for document in corpus.documents(business):
            if not document.source.answers_customers:
                continue
            for section in document.loaded.sections:
                heading = section.heading_path[-1].strip() if section.heading_path else ""
                if heading and heading not in seen:
                    seen.add(heading)
                    questions.append(SampleQuestion(business, document.source.source_id, heading))
    return tuple(questions)


def questions_sha256(questions: Sequence[SampleQuestion]) -> str:
    """sha256 of the questions as canonical JSON: which texts ran, without printing them."""
    payload = [[q.business_id, q.source_id, q.text] for q in questions]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── A leg's statement, exactly as its H1 function sends it ───────────────────────────────────


@dataclass(frozen=True)
class Statement:
    """One leg's SQL and values, as its H1 function passed them to `execute`."""

    sql: str = field(repr=False)
    values: Mapping[str, object] = field(repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class _NoRows:
    def mappings(self) -> list[object]:
        return []


class _Capture:
    """Stands in for the business transaction's connection while an H1 function builds its
    statement: records every `execute` and returns no rows. It has no other method, so any
    other call fails (`CaptureError`)."""

    def __init__(self) -> None:
        self.executed: list[tuple[object, object]] = []

    async def execute(self, statement: object, parameters: object = None) -> _NoRows:
        self.executed.append((statement, parameters))
        return _NoRows()


async def captured[Q: (VectorQuery, TextQuery)](
    leg: Callable[[AsyncConnection, Q], Awaitable[list[SearchHit]]], query: Q
) -> Statement:
    """The one statement `leg` sends for `query`, and its values. Nothing reaches a database."""
    capture = _Capture()
    try:
        hits = await leg(cast(AsyncConnection, capture), query)
    except AttributeError:
        raise CaptureError("the leg called a connection method other than execute") from None
    if hits or len(capture.executed) != 1:
        raise CaptureError("a leg must send exactly one statement and return no hits for no rows")
    statement, values = capture.executed[0]
    if not isinstance(statement, TextClause) or not isinstance(values, dict):
        raise CaptureError("a leg must send a text() statement with a dict of values")
    if not statement.text.lstrip().upper().startswith("SELECT"):
        raise CaptureError("a leg's statement must be a SELECT")
    return Statement(sql=statement.text, values=dict(values))


# ── EXPLAIN's JSON (pure) ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanNode:
    node_type: str
    relation: str | None
    index: str | None
    conditions: tuple[str, ...]
    """What the index evaluates (`INDEX_KEYS`), for index nodes only."""

    @property
    def shape(self) -> tuple[str, str | None, str | None]:
        return (self.node_type, self.relation, self.index)


@dataclass(frozen=True)
class ExplainSample:
    """One `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` execution."""

    planning_ms: float
    execution_ms: float
    rows: int
    """Rows the statement returned (the top node's actual rows x loops)."""
    shared_read_blocks: int
    """Blocks read from outside Postgres' shared buffers (0: everything was cached)."""
    nodes: tuple[PlanNode, ...]
    """Every plan node, depth first."""


def _number(node: Mapping[str, object], key: str) -> float:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlanError(f"EXPLAIN output has no number {key!r}")
    if not math.isfinite(value) or value < 0:
        raise PlanError(f"EXPLAIN output's {key!r} is not a finite number >= 0")
    return float(value)


def _optional_text(node: Mapping[str, object], key: str) -> str | None:
    value = node.get(key)
    if value is None or isinstance(value, str):
        return value
    raise PlanError(f"EXPLAIN output's {key!r} is not text")


def _object(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PlanError(f"EXPLAIN output's {what} is not an object")
    return cast(Mapping[str, object], value)


def _walk(plan: Mapping[str, object]) -> Iterator[PlanNode]:
    node_type = plan.get("Node Type")
    if not isinstance(node_type, str):
        raise PlanError("an EXPLAIN plan node has no 'Node Type'")
    index = _optional_text(plan, "Index Name")
    conditions = tuple(
        value for key in INDEX_KEYS if index is not None and isinstance(value := plan.get(key), str)
    )
    yield PlanNode(node_type, _optional_text(plan, "Relation Name"), index, conditions)
    children = plan.get("Plans", [])
    if not isinstance(children, list):
        raise PlanError("an EXPLAIN plan node's 'Plans' is not a list")
    for child in children:
        yield from _walk(_object(child, "plan node"))


def parse_explain(raw: object) -> ExplainSample:
    """One `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` result (the JSON text, or already decoded)."""
    try:
        document = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        raise PlanError("EXPLAIN output is not JSON") from None
    if not isinstance(document, list) or len(document) != 1:
        raise PlanError("EXPLAIN output is not a list of one plan")
    top = _object(document[0], "result")
    plan = _object(top.get("Plan"), "Plan")
    rows = _number(plan, "Actual Rows") * _number(plan, "Actual Loops")
    return ExplainSample(
        planning_ms=_number(top, "Planning Time"),
        execution_ms=_number(top, "Execution Time"),
        rows=round(rows),
        shared_read_blocks=round(_number(plan, "Shared Read Blocks")),
        nodes=tuple(_walk(plan)),
    )


def index_used_for(nodes: Sequence[PlanNode], operator: str) -> bool:
    """Whether any index node evaluates `operator` itself (in its condition or its ordering)."""
    return any(operator in condition for node in nodes for condition in node.conditions)


# ── What the database says (typed) ───────────────────────────────────────────────────────────


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TableFacts(_Model):
    name: str
    rls: bool
    force_rls: bool
    live_rows_estimate: int
    """`pg_stat_user_tables.n_live_tup`: a statistics estimate over every business."""


class IndexFacts(_Model):
    table: str
    name: str
    definition: str


class OperatorFacts(_Model):
    operator: str
    left_type: str
    right_type: str
    function: str
    leakproof: bool


class DatabaseFacts(_Model):
    role: str
    postgres_version: str
    pgvector_version: str | None
    isolation: str
    read_only: bool
    row_security: str
    tables: tuple[TableFacts, ...]
    indexes: tuple[IndexFacts, ...]
    """Every index on chunks and documents (pg_indexes)."""
    operators: tuple[OperatorFacts, ...]
    """`@@`(tsvector, tsquery) and `<=>`(vector, vector), and whether their functions are
    leakproof (Postgres only pushes a leakproof qual below a table's RLS policy)."""


class BusinessRows(_Model):
    business_id: str
    eval_business_id: str
    chunks: int
    documents: int
    candidates: int
    """Chunks a customer's search may return: approved, public, this embedding model."""
    questions: int


# ── The measurement (Postgres) ───────────────────────────────────────────────────────────────

_SESSION: Final = text(
    "SELECT current_user AS role, current_setting('server_version') AS postgres_version,"
    " (SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector_version,"
    " current_setting('transaction_isolation') AS isolation,"
    " current_setting('transaction_read_only') AS read_only,"
    " current_setting('row_security') AS row_security"
)

_TABLES: Final = text(
    "SELECT c.relname AS name, c.relrowsecurity AS rls, c.relforcerowsecurity AS force_rls,"
    " s.n_live_tup AS live_rows"
    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " JOIN pg_stat_user_tables s ON s.relid = c.oid"
    " WHERE n.nspname = current_schema() AND c.relname IN ('chunks', 'documents')"
    " ORDER BY c.relname"
)

_INDEXES: Final = text(
    "SELECT tablename, indexname, indexdef FROM pg_indexes"
    " WHERE schemaname = current_schema() AND tablename IN ('chunks', 'documents')"
    " ORDER BY tablename, indexname"
)

_OPERATORS: Final = text(
    "SELECT o.oprname AS operator, format_type(o.oprleft, NULL) AS left_type,"
    " format_type(o.oprright, NULL) AS right_type, p.proname AS function,"
    " p.proleakproof AS leakproof"
    " FROM pg_operator o JOIN pg_proc p ON p.oid = o.oprcode"
    " WHERE (o.oprname = '@@' AND o.oprleft = 'tsvector'::regtype"
    "        AND o.oprright = 'tsquery'::regtype)"
    "    OR (o.oprname = '<=>' AND o.oprleft = 'vector'::regtype"
    "        AND o.oprright = 'vector'::regtype)"
    " ORDER BY o.oprname"
)

# The business is named in the query as well as by RLS (business-context §5). The ids' digests
# show an insert or a delete between two readings; the counts are what the report shows.
_ROWS: Final = text(
    "SELECT (SELECT count(*) FROM chunks WHERE business_id = :business_id) AS chunks,"
    " (SELECT count(*) FROM documents WHERE business_id = :business_id) AS documents,"
    " (SELECT count(*) FROM chunks c"
    "  JOIN documents d ON d.business_id = c.business_id AND d.id = c.document_id"
    "  WHERE c.business_id = :business_id AND c.embedding_model = :embedding_model"
    "    AND d.status = 'approved' AND d.audience = ANY(:audiences)) AS candidates,"
    " (SELECT encode(sha256(convert_to(coalesce(string_agg(id::text, ',' ORDER BY id), ''),"
    "  'UTF8')), 'hex') FROM chunks WHERE business_id = :business_id) AS chunk_ids,"
    " (SELECT encode(sha256(convert_to(coalesce(string_agg(id::text, ',' ORDER BY id), ''),"
    "  'UTF8')), 'hex') FROM documents WHERE business_id = :business_id) AS document_ids"
)


@dataclass(frozen=True)
class LatencyRequest:
    mapping: Mapping[str, str]
    """{dataset business: eval business}, in the dataset manifest's order."""
    questions: tuple[SampleQuestion, ...]
    expected_chunks: Mapping[str, int]
    """{dataset business: the chunks the corpus makes at the chunk config}."""
    spec: EmbeddingSpec
    """The fake embedder's model (the namespaces' model)."""
    k: int
    rrf_k: int
    repeats: int
    warmup: int

    def __post_init__(self) -> None:
        if self.repeats < 1 or self.warmup < 0:
            raise ValueError("repeats must be at least 1 and warmup at least 0")
        if not self.questions:
            raise ValueError("there must be at least one question")
        if any(q.business_id not in self.mapping for q in self.questions):
            raise ValueError("every question's business must be in the mapping")


@dataclass(frozen=True)
class Measured:
    """What one measurement read and timed (`build_report` turns it into the report)."""

    database: DatabaseFacts
    rows: tuple[BusinessRows, ...]
    statement_sha256: Mapping[Leg, str]
    explain: Mapping[Leg, tuple[ExplainSample, ...]]
    transaction_ms: tuple[float, ...]
    vector_ms: tuple[float, ...]
    text_ms: tuple[float, ...]


@dataclass(frozen=True)
class _Session:
    role: str
    postgres_version: str
    pgvector_version: str | None
    isolation: str
    read_only: bool
    row_security: str


async def _session(connection: AsyncConnection) -> _Session:
    row = (await connection.execute(_SESSION)).one()
    session = _Session(
        role=str(row.role),
        postgres_version=str(row.postgres_version),
        pgvector_version=None if row.pgvector_version is None else str(row.pgvector_version),
        isolation=str(row.isolation),
        read_only=row.read_only == "on",
        row_security=str(row.row_security),
    )
    if session.role != APP_ROLE:
        raise LatencyRefused(f"connected as {session.role}, not {APP_ROLE} (ADR 0009)")
    if not session.read_only or session.isolation != "repeatable read":
        raise LatencyRefused("a measured transaction must be REPEATABLE READ READ ONLY")
    return session


async def _catalog(
    connection: AsyncConnection,
) -> tuple[tuple[TableFacts, ...], tuple[IndexFacts, ...], tuple[OperatorFacts, ...]]:
    tables = tuple(
        TableFacts(
            name=r.name, rls=r.rls, force_rls=r.force_rls, live_rows_estimate=int(r.live_rows)
        )
        for r in (await connection.execute(_TABLES)).all()
    )
    indexes = tuple(
        IndexFacts(table=r.tablename, name=r.indexname, definition=r.indexdef)
        for r in (await connection.execute(_INDEXES)).all()
    )
    operators = tuple(
        OperatorFacts(
            operator=r.operator,
            left_type=r.left_type,
            right_type=r.right_type,
            function=r.function,
            leakproof=r.leakproof,
        )
        for r in (await connection.execute(_OPERATORS)).all()
    )
    return tables, indexes, operators


@dataclass(frozen=True)
class _Stored:
    chunks: int
    documents: int
    candidates: int
    chunk_ids: str
    document_ids: str


async def _stored(database: Database, request: LatencyRequest) -> dict[str, _Stored]:
    stored: dict[str, _Stored] = {}
    for eval_id in request.mapping.values():
        async with database.business_transaction(eval_id, snapshot=True) as connection:
            row = (
                await connection.execute(
                    _ROWS,
                    {
                        "business_id": eval_id,
                        "embedding_model": request.spec.model,
                        "audiences": list(VISIBLE_AUDIENCES[READER]),
                    },
                )
            ).one()
        stored[eval_id] = _Stored(
            int(row.chunks),
            int(row.documents),
            int(row.candidates),
            str(row.chunk_ids),
            str(row.document_ids),
        )
    return stored


async def explain(connection: AsyncConnection, statement: Statement) -> ExplainSample:
    """`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` of `statement`, with its own values bound."""
    result = await connection.execute(text(EXPLAIN + statement.sql), dict(statement.values))
    return parse_explain(result.scalar_one())


@dataclass(frozen=True)
class _Prepared:
    question: SampleQuestion
    eval_id: str
    statements: Mapping[Leg, Statement]


async def _prepare(runner: EmbeddingRunner, request: LatencyRequest) -> list[_Prepared]:
    prepared: list[_Prepared] = []
    for question in request.questions:
        eval_id = request.mapping[question.business_id]
        embedded = await runner.embed(
            [question.text],
            input_type="query",
            business_id=eval_id,
            operation=OPERATION,
            trace_id=new_trace_id(),
        )
        query = TextQuery(
            business_id=eval_id,
            question=question.text,
            embedding=embedded.vectors[0],
            embedding_model=embedded.model,
            k=request.k,
            reader=READER,
        )
        vector_query = VectorQuery(
            business_id=eval_id,
            embedding=embedded.vectors[0],
            embedding_model=embedded.model,
            k=request.k,
            reader=READER,
        )
        statements: dict[Leg, Statement] = {
            "vector": await captured(vector_search, vector_query),
            "text": await captured(text_search, query),
        }
        prepared.append(_Prepared(question, eval_id, statements))
    return prepared


def _refuse_unusable(stored: Mapping[str, _Stored], request: LatencyRequest) -> None:
    for business, eval_id in request.mapping.items():
        chunks = stored[eval_id].chunks
        if chunks == 0:
            raise LatencyRefused(
                f"{eval_id} holds no chunks: ingest the corpus first (`python -m"
                " vectapilot_api.evalrun ingest`, dry, fake embedder)"
            )
        if chunks != request.expected_chunks[business]:
            raise LatencyRefused(
                f"{eval_id} holds {chunks} chunks, the corpus makes"
                f" {request.expected_chunks[business]} at this chunk config: run `python -m"
                " vectapilot_api.evalrun ingest` (it checks the stored chunks)"
            )


async def measure(database: Database, runner: EmbeddingRunner, request: LatencyRequest) -> Measured:
    """The measurement (module docstring). Raises `LatencyRefused` for an empty or stale
    namespace, a wrong role or transaction, an EXPLAIN whose rows differ from the product's leg,
    or data that changed meanwhile."""
    before = await _stored(database, request)
    _refuse_unusable(before, request)
    prepared = await _prepare(runner, request)
    service = SearchService(
        database,
        runner,
        retrieval=RetrievalConfig(mode="hybrid", candidate_k=request.k, rrf_k=request.rrf_k),
    )
    samples: dict[Leg, list[ExplainSample]] = {leg: [] for leg in LEGS}
    durations: dict[str, list[float]] = {"transaction": [], "vector": [], "text": []}
    first: _Session | None = None
    for pass_number in range(request.warmup + request.repeats):
        timed = pass_number >= request.warmup
        for item in prepared:
            async with database.business_transaction(item.eval_id, snapshot=True) as connection:
                session = await _session(connection)
                explained = {leg: await explain(connection, item.statements[leg]) for leg in LEGS}
            first = first or session
            stats = SearchStats()
            await service.search(
                item.eval_id,
                item.question.text,
                k=request.k,
                reader=READER,
                trace_id=new_trace_id(),
                operation=OPERATION,
                stats=stats,
            )
            if (explained["vector"].rows, explained["text"].rows) != (
                stats.vector_hits,
                stats.text_hits,
            ):
                raise LatencyRefused(
                    "an EXPLAIN returned another number of rows than the product's leg for the"
                    f" same question ({item.question.business_id}/{item.question.source_id})"
                )
            if stats.transaction_ms is None or stats.vector_ms is None or stats.text_ms is None:
                raise LatencyRefused("the hybrid search did not record its durations")
            if timed:
                for leg in LEGS:
                    samples[leg].append(explained[leg])
                durations["transaction"].append(stats.transaction_ms)
                durations["vector"].append(stats.vector_ms)
                durations["text"].append(stats.text_ms)
    after = await _stored(database, request)
    if after != before:
        raise LatencyRefused(
            "the chunks or documents of a measured business changed during the measurement:"
            " nothing is written"
        )
    first_eval_id = next(iter(request.mapping.values()))
    async with database.business_transaction(first_eval_id, snapshot=True) as connection:
        tables, indexes, operators = await _catalog(connection)
    if first is None:  # unreachable: LatencyRequest asks for a pass and a question
        raise LatencyRefused("nothing was measured")
    per_business = Counter(q.business_id for q in request.questions)
    return Measured(
        database=DatabaseFacts(
            role=first.role,
            postgres_version=first.postgres_version,
            pgvector_version=first.pgvector_version,
            isolation=first.isolation,
            read_only=first.read_only,
            row_security=first.row_security,
            tables=tables,
            indexes=indexes,
            operators=operators,
        ),
        rows=tuple(
            BusinessRows(
                business_id=business,
                eval_business_id=eval_id,
                chunks=before[eval_id].chunks,
                documents=before[eval_id].documents,
                candidates=before[eval_id].candidates,
                questions=per_business[business],
            )
            for business, eval_id in request.mapping.items()
        ),
        statement_sha256={leg: prepared[0].statements[leg].sha256 for leg in LEGS},
        explain={leg: tuple(samples[leg]) for leg in LEGS},
        transaction_ms=tuple(durations["transaction"]),
        vector_ms=tuple(durations["vector"]),
        text_ms=tuple(durations["text"]),
    )


async def run_measurement(settings: Settings, request: LatencyRequest) -> Measured:
    """`measure` on the API's own kind of engine (`create_api_engine`: a pool, here of one
    connection, reset on return) as Settings' app role, with the fake embedder only."""
    engine = create_api_engine(
        str(settings.database_url.get_secret_value()), pool_size=1, max_overflow=0
    )
    embedder = FakeEmbedder(request.spec)
    try:
        policy = settings.llm_retry_policy()
        runner = EmbeddingRunner(
            embedder,
            request.spec,
            policy,
            deadline_s=batch_deadline_s(policy, settings.embeddings_timeout_seconds),
        )
        return await measure(Database(engine), runner, request)
    finally:
        await embedder.aclose()
        await engine.dispose()


# ── The report (pure) ────────────────────────────────────────────────────────────────────────


class Provenance(_Model):
    label: str = LABEL
    measured_at: datetime
    commit: str
    uncommitted_changes: bool
    """Any change outside `evals/reports/` when it ran (`llm_cli.read_git`)."""
    statements_at_commit: bool
    """Whether retrieval/vector.py and retrieval/text.py were byte for byte as in the commit."""
    docker_mem_total_bytes: int | None
    """`docker info`'s MemTotal: the Docker VM's memory (None when docker could not say)."""


class RunSettings(_Model):
    embedding_model: str
    max_chars: int
    overlap_chars: int
    k: int
    """Hits each leg is asked for (`rag_candidate_k`, as the hybrid path does)."""
    rrf_k: int
    reader: Reader
    warmup_passes: int
    repeats: int
    questions: int
    questions_sha256: str


class NodeCount(_Model):
    node_type: str
    relation: str | None
    index: str | None
    count: int


class StatementReport(_Model):
    leg: Leg
    function: str
    sql_sha256: str
    operator: str
    n: int
    execution_ms: LatencySummary
    planning_ms: LatencySummary
    rows_min: int
    rows_max: int
    shared_read_blocks: int
    """Summed over the timed executions."""
    distinct_plans: int
    """Distinct plan shapes (node types, relations, indexes) over the timed executions."""
    nodes: tuple[NodeCount, ...]
    index_for_operator: bool
    """Whether any execution's plan had an index evaluate `operator`."""


class SearchReport(_Model):
    transaction_ms: LatencySummary
    vector_ms: LatencySummary
    text_ms: LatencySummary


class LatencyReport(_Model):
    schema_version: Literal[1] = 1
    provenance: Provenance
    database: DatabaseFacts
    settings: RunSettings
    rows: tuple[BusinessRows, ...]
    statements: tuple[StatementReport, ...]
    search: SearchReport
    read_only_checked: Literal[True] = True
    """Always true in a written report: otherwise the measurement was refused."""


def statement_report(
    leg: Leg, sql_sha256: str, samples: Sequence[ExplainSample]
) -> StatementReport:
    if not samples:
        raise ValueError("a statement report needs at least one sample")
    counts = Counter(node.shape for sample in samples for node in sample.nodes)
    ordered = sorted(
        counts.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2] or "")
    )
    return StatementReport(
        leg=leg,
        function=FUNCTION[leg],
        sql_sha256=sql_sha256,
        operator=OPERATOR[leg],
        n=len(samples),
        execution_ms=latency_summary([s.execution_ms for s in samples]),
        planning_ms=latency_summary([s.planning_ms for s in samples]),
        rows_min=min(s.rows for s in samples),
        rows_max=max(s.rows for s in samples),
        shared_read_blocks=sum(s.shared_read_blocks for s in samples),
        distinct_plans=len({tuple(node.shape for node in s.nodes) for s in samples}),
        nodes=tuple(
            NodeCount(node_type=node_type, relation=relation, index=index, count=count)
            for (node_type, relation, index), count in ordered
        ),
        index_for_operator=any(index_used_for(s.nodes, OPERATOR[leg]) for s in samples),
    )


def build_report(
    measured: Measured,
    provenance: Provenance,
    request: LatencyRequest,
    *,
    max_chars: int,
    overlap_chars: int,
) -> LatencyReport:
    return LatencyReport(
        provenance=provenance,
        database=measured.database,
        settings=RunSettings(
            embedding_model=request.spec.model,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            k=request.k,
            rrf_k=request.rrf_k,
            reader=READER,
            warmup_passes=request.warmup,
            repeats=request.repeats,
            questions=len(request.questions),
            questions_sha256=questions_sha256(request.questions),
        ),
        rows=measured.rows,
        statements=tuple(
            statement_report(leg, measured.statement_sha256[leg], measured.explain[leg])
            for leg in LEGS
        ),
        search=SearchReport(
            transaction_ms=latency_summary(measured.transaction_ms),
            vector_ms=latency_summary(measured.vector_ms),
            text_ms=latency_summary(measured.text_ms),
        ),
    )


def _ms(value: float | None) -> str:
    return "n < 20" if value is None else f"{value:.3f}"


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def _memory(total: int | None) -> str:
    if total is None:
        return "unknown (docker info did not answer)"
    return f"{total / 2**30:.2f} GiB ({total} bytes, `docker info` MemTotal)"


_USING: Final = re.compile(r" USING (.*)$")


def _index_text(index: IndexFacts) -> str:
    using = _USING.search(index.definition)
    return f"`{index.name}` {using.group(1) if using else index.definition}"


def _nodes_text(statement: StatementReport) -> str:
    parts = []
    for node in statement.nodes:
        where = f" on {node.relation}" if node.relation else ""
        using = f" using {node.index}" if node.index else ""
        parts.append(f"{node.node_type}{where}{using} x{node.count}")
    return "; ".join(parts)


def render_markdown(report: LatencyReport) -> str:
    """The committed summary of one report (every number is in the JSON next to it)."""
    p, d, s = report.provenance, report.database, report.settings
    statement_n = report.statements[0].n
    forced = [t.name for t in d.tables if t.rls and t.force_rls]
    businesses = ", ".join(f"{r.questions} {r.business_id}" for r in report.rows)
    lines = [
        f"# Search latency at pilot size ({p.measured_at.date().isoformat()}, local)",
        "",
        f"**{p.label}.** Pilot size only (M3 plan D14). Never a production number, and never"
        " compared with the targets in `docs/business-context.md` §6 as a pass.",
        "",
        "## Setup",
        "",
        f"- Commit `{p.commit}`. Uncommitted changes outside `evals/reports/`:"
        f" {_yes(p.uncommitted_changes)}. `retrieval/vector.py` and `retrieval/text.py` byte for"
        f" byte as in that commit: {_yes(p.statements_at_commit)}.",
        f"- PostgreSQL {d.postgres_version}, pgvector {d.pgvector_version or 'not installed'}."
        f" Docker VM memory {_memory(p.docker_mem_total_bytes)}.",
        f"- Role `{d.role}`. Every EXPLAIN transaction: one business (`set_config`),"
        f" {d.isolation}, read only: {_yes(d.read_only)}. `row_security` {d.row_security}; RLS"
        f" enabled and forced on: {', '.join(forced) or 'none'}.",
        f"- Embedding model `{s.embedding_model}` (fake: latency only, never a quality number);"
        f" chunks {s.max_chars}/{s.overlap_chars}; k {s.k} per leg; RRF k {s.rrf_k}; reader"
        f" {s.reader}.",
        f"- Questions: {s.questions} section headings of the customer-visible corpus documents"
        f" ({businesses}), sha256 `{s.questions_sha256}`. {s.warmup_passes} warm-up pass(es), not"
        f" counted, then {s.repeats} timed passes: n = {statement_n} per statement, every timed"
        " execution counted once.",
        "",
        "## Rows",
        "",
        "| business | eval namespace | chunks | documents | customer candidates | questions |",
        "|---|---|--:|--:|--:|--:|",
        *(
            f"| {r.business_id} | `{r.eval_business_id}` | {r.chunks} | {r.documents} |"
            f" {r.candidates} | {r.questions} |"
            for r in report.rows
        ),
        "",
        "Whole tables, every business (statistics estimates, `n_live_tup`): "
        + ", ".join(f"{t.name} {t.live_rows_estimate}" for t in d.tables)
        + ".",
        "",
        "## EXPLAIN (ANALYZE, BUFFERS): server-side time in ms",
        "",
        "| statement | n | execution median | execution p95 | execution max | planning median |"
        " planning p95 | rows returned | shared blocks read |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|",
        *(
            f"| `{st.function}` | {st.n} | {_ms(st.execution_ms.median_ms)} |"
            f" {_ms(st.execution_ms.p95_ms)} | {_ms(st.execution_ms.max_ms)} |"
            f" {_ms(st.planning_ms.median_ms)} | {_ms(st.planning_ms.p95_ms)} |"
            f" {st.rows_min}-{st.rows_max} | {st.shared_read_blocks} |"
            for st in report.statements
        ),
        "",
        "## Plans and indexes",
        "",
        *(
            f"- `{st.function}`: {st.distinct_plans} plan shape(s). Nodes: {_nodes_text(st)}."
            f" An index evaluated `{st.operator}`: {_yes(st.index_for_operator)}."
            for st in report.statements
        ),
        "- Indexes on chunks and documents (`pg_indexes`): "
        + "; ".join(_index_text(i) for i in d.indexes)
        + ".",
        "- Operators: "
        + "; ".join(
            f"`{o.operator}`({o.left_type}, {o.right_type}) is `{o.function}`, leakproof"
            f" {str(o.leakproof).lower()}"
            for o in d.operators
        )
        + ".",
        "",
        "## The product's hybrid search transaction, client-side, in ms",
        "",
        "`SearchService` in hybrid mode, `SearchStats` on the monotonic clock: the transaction is"
        " the snapshot, `set_config`, the model check, both legs, the fusion and the commit.",
        "",
        "| part | n | median | p95 | max |",
        "|---|--:|--:|--:|--:|",
        *(
            f"| {name} | {summary.n} | {_ms(summary.median_ms)} | {_ms(summary.p95_ms)} |"
            f" {_ms(summary.max_ms)} |"
            for name, summary in (
                ("search transaction", report.search.transaction_ms),
                ("vector leg", report.search.vector_ms),
                ("text leg", report.search.text_ms),
            )
        ),
        "",
        "## Read-only",
        "",
        "Every EXPLAIN transaction reported REPEATABLE READ and `transaction_read_only` on; the"
        " search transactions are the API's own snapshot transactions. Each measured business's"
        " chunk and document ids hashed the same before and after. Every EXPLAIN returned as many"
        " rows as the product's leg for the same question.",
        "",
    ]
    return "\n".join(lines)


def report_paths(directory: Path, measured_on: datetime) -> tuple[Path, Path]:
    """`<directory>/<date>-local.json` and `.md`."""
    stem = f"{measured_on.date().isoformat()}-local"
    return directory / f"{stem}.json", directory / f"{stem}.md"


def write_report(paths: tuple[Path, Path], report: LatencyReport) -> None:
    """Write both files; a file that exists is never overwritten (`FileExistsError`)."""
    json_path, md_path = paths
    json_path.parent.mkdir(parents=True, exist_ok=True)
    contents = (
        (json_path, report.model_dump_json(indent=2) + "\n"),
        (md_path, render_markdown(report)),
    )
    for path, content in contents:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)


# ── The outside world ────────────────────────────────────────────────────────────────────────


def statement_sources() -> dict[Path, bytes]:
    """The files holding both H1 statements, with their bytes (for `dataset_at_commit`)."""
    sources: dict[Path, bytes] = {}
    for function in (vector_search, text_search):
        name = inspect.getsourcefile(function)
        if name is None:
            raise LatencyRefused(f"the source file of {function.__name__} cannot be found")
        sources[Path(name)] = Path(name).read_bytes()
    return sources


_DIGITS: Final = re.compile(r"[0-9]+")


def parse_mem_total(output: str) -> int | None:
    """`docker info --format {{.MemTotal}}`'s output as bytes, or None if it is not a number."""
    value = output.strip()
    return int(value) if _DIGITS.fullmatch(value) and int(value) > 0 else None


def docker_mem_total() -> int | None:
    """The Docker VM's memory in bytes (`docker info`), or None when docker cannot say."""
    executable = shutil.which("docker")
    if executable is None:
        return None
    try:
        # Fixed arguments, no shell (S603: nothing here is parsed by a shell).
        done = subprocess.run(  # noqa: S603
            [executable, "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_mem_total(done.stdout) if done.returncode == 0 else None
