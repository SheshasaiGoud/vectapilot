"""G11's goldens for `POST /v1/answer` and `GET /v1/kb/search` (M7 PR 8): what both routes return
and log while `guard_answer` is off, recorded on the base commit and replayed by
tests/answer/test_answer_goldens.py.

**What is recorded.** A fixed set of requests (`REQUESTS`) through the real app (`create_app`),
each on the existing stand-ins (tests/retrieval/standins.py: no SQL; the scripted fake embedder;
the fake provider behind the real `LLMGateway`, tests/answer/apps.py): every answer outcome
(answered in vector and hybrid mode, every abstain reason, the configured hand-off), every typed
error of both routes' tables, the 400 and the 422s, search with hits, without hits and in hybrid
mode, and one question per route holding a synthetic phone number (masking it would change the
embedder's and the model's input, so an off path that masked would differ). For each one: the
status, every response header in order, the body's exact text, and the `rag_answer`, `llm_call`
and `embedding_call` lines exactly as the app's own log formatter writes them (`embedding_call` is
the one record search writes).

**Normalisation (G11's closed list).** `new_trace_id` is patched to a fixed sequence in both route
modules, and only these are replaced by `REPLACED`: the `X-Trace-Id` header, and in a log line every
non-null value under a key named `trace_id`, `time` or `latency_ms`, or ending in `_ms`. A body is
kept byte for byte, its trace id (the patched value) replaced by `REPLACED` as text. Each exchange
also records whether its header, its body's `trace_id` and every record's `trace_id` are the same
patched id (`joined`), so the join survives the token. Nothing else is touched: a new field that
changes from run to run (a clock, a random id) is caught by recording twice
(`test_answer_goldens.py`), and any other new or changed byte by the comparison with the file.

**Recorded** on the untouched tree at `BASE_COMMIT` (6572e29: M7 PR 6 merged, before PR 8 edited
any route code): a checkout of that commit (a local clone or a `git worktree`) with this file, and
nothing else, copied in, then `uv run python -m tests.answer.goldens` from its `apps/api/` (with
`PYTHONPATH=src` when the virtualenv is another tree's). `main` refuses any other tree: HEAD must
be `BASE_COMMIT`, with no tracked file changed (this file is untracked there), and it writes the
full commit it read. **Re-record** only on a reviewed change of the base behaviour (a change to
M3's answer path that its own PR and review allow, after M3's measurement window), with
`BASE_COMMIT` moved to that commit: a diff of `goldens/guard_answer_off.json` is a change to what
customers get while `guard_answer` is off, and belongs in that PR's review.
"""

import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from tests.answer.apps import CONFIG, A, gateway_for
from tests.answer.providers import reply
from tests.conftest import TEST_SETTINGS_FIELDS, make_settings
from tests.embeddings.scripted import ScriptedEmbedder
from tests.retrieval.standins import FakeDatabase, build_hybrid_search, build_search
from vectapilot_api.answer import router as answer_router
from vectapilot_api.answer.router import get_answer_service
from vectapilot_api.answer.service import RAG_ANSWER, AnswerService, RagConfig
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.embeddings.base import InputTooLarge
from vectapilot_api.embeddings.runner import EMBEDDING_CALL
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    DeadlineExceeded,
    ModelRefused,
    Transient,
)
from vectapilot_api.gateway.gateway import LLM_CALL
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.guards.limits import Limiter
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.llm_cli import _git
from vectapilot_api.main import create_app
from vectapilot_api.retrieval import router as search_router
from vectapilot_api.retrieval.router import get_search_service
from vectapilot_api.retrieval.search import SearchService
from vectapilot_api.retrieval.types import SearchHit

GOLDEN_FILE: Final = Path(__file__).parent / "goldens" / "guard_answer_off.json"
API_DIR: Final = Path(__file__).resolve().parents[2]
"""`apps/api` of the tree this file runs in (`main`'s provenance check)."""
BASE_COMMIT: Final = "6572e29da269f55a21bb1b7fd8c5262b9ff2c8d9"
DEAD_REDIS: Final = TEST_SETTINGS_FIELDS["redis_url"]
REPLACED: Final = "<volatile>"
VOLATILE_KEYS: Final = frozenset({"trace_id", "time", "latency_ms"})
"""G11's closed list in a log line, with every key ending in `VOLATILE_SUFFIX`."""
VOLATILE_SUFFIX: Final = "_ms"
VOLATILE_HEADERS: Final = frozenset({"x-trace-id"})
RECORDED: Final = frozenset({RAG_ANSWER, LLM_CALL, EMBEDDING_CALL})
"""The log lines a golden holds (G11)."""
QUESTION: Final = "When do you open on Sundays?"
PII_QUESTION: Final = "My phone is +91 00000 20777, when do you open on Sundays?"
"""TEST-ONLY: a synthetic number (tests/guards' `SYNTHETIC_PHONE`) H2 masks as `[PHONE]`."""
NAMESPACE: Final = uuid.UUID("6f1c2d4e-0b9b-4c1e-8d6f-4f7e9a0a3f6f")


def fixed_hit(name: str, content: str, score: float, **fields: Any) -> SearchHit:
    """A `SearchHit` whose ids derive from `name`, so a body is the same on every run."""
    values: dict[str, Any] = {
        "source_id": "faq",
        "version": "1",
        "title": "Opening hours",
        "language": "en",
        "content_kind": ContentKind.FAQ,
        "page": None,
        "chunk_index": 0,
        "embedding_model": "fake-embedding-1024",
    } | fields
    return SearchHit(
        chunk_id=uuid.uuid5(NAMESPACE, f"chunk:{name}"),
        document_id=uuid.uuid5(NAMESPACE, f"document:{name}"),
        content=content,
        score=score,
        **values,
    )


SUNDAYS: Final = fixed_hit("sundays", "On Sundays we open at 10.", 0.82, page=1)
WEEKDAYS: Final = fixed_hit("weekdays", "On weekdays we open at 9.", 0.64, chunk_index=1)
FEES: Final = fixed_hit("fees", "A visit costs 500.", 0.31, source_id="fees", title="Fees")
WEAK: Final = fixed_hit("weak", "Parking is behind the building.", 0.12, title="Parking")
FORGED: Final = "0b9b3c1e-7d6f-4f7e-9a0a-3f6f1c2d4e5f"
HANDOFF: Final = RagConfig(min_score=0.3, context_k=5, weak_evidence_outcome="handoff")


# ── The requests ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Arrangement:
    """The services one request runs on: a search, and an answer service over it (answer only)."""

    search: SearchService
    answer: AnswerService | None = None


def _answer(
    hits: Sequence[SearchHit] = (SUNDAYS, WEEKDAYS, FEES, WEAK),
    steps: Sequence[Any] = (),
    *,
    config: RagConfig = CONFIG,
    embedder: Sequence[BaseException] = (),
    stored: frozenset[str] | None = None,
    database_fails: Exception | None = None,
) -> Callable[[], Arrangement]:
    """An answer on stand-ins; `embedder` is the scripted embedder's failures, built afresh on
    every call (a script is consumed as it runs)."""

    def build() -> Arrangement:
        search = build_search(
            {A: list(hits)},
            embedder=ScriptedEmbedder(script=list(embedder)),
            stored=None if stored is None else {A: stored},
        )
        if database_fails is not None:
            search.service._database = FakeDatabase(fail=database_fails)
        gateway = gateway_for(FakeProvider(list(steps)))
        return Arrangement(search.service, AnswerService(search.service, gateway, config))

    return build


def _hybrid_answer(steps: Sequence[Any]) -> Callable[[], Arrangement]:
    def build() -> Arrangement:
        search = build_hybrid_search({A: [SUNDAYS, WEEKDAYS]}, {A: [WEEKDAYS, FEES]})
        gateway = gateway_for(FakeProvider(list(steps)))
        return Arrangement(search.service, AnswerService(search.service, gateway, CONFIG))

    return build


def _search(
    hits: Sequence[SearchHit] = (SUNDAYS, WEEKDAYS, FEES),
    *,
    embedder: Sequence[BaseException] = (),
    stored: frozenset[str] | None = None,
    database_fails: Exception | None = None,
    hybrid: bool = False,
) -> Callable[[], Arrangement]:
    def build() -> Arrangement:
        if hybrid:
            return Arrangement(build_hybrid_search({A: list(hits)}, {A: list(hits[1:])}).service)
        search = build_search(
            {A: list(hits)},
            embedder=ScriptedEmbedder(script=list(embedder)),
            stored=None if stored is None else {A: stored},
        )
        if database_fails is not None:
            search.service._database = FakeDatabase(fail=database_fails)
        return Arrangement(search.service)

    return build


def _cites(*hits: SearchHit, answer: str = "We open at 10 on Sundays.") -> FakeReply:
    return FakeReply(text=reply("answered", answer, [str(h.chunk_id) for h in hits]))


@dataclass(frozen=True)
class GoldenRequest:
    """One request of the set: its name, its services and what is sent."""

    name: str
    arrange: Callable[[], Arrangement]
    method: str = "POST"
    path: str = "/v1/answer"
    body: object = field(default_factory=lambda: {"question": QUESTION})
    params: dict[str, Any] | None = None
    business: str | None = A


def _get(name: str, arrange: Callable[[], Arrangement], **params: Any) -> GoldenRequest:
    sent = {"q": QUESTION, "k": 5} | params
    return GoldenRequest(name, arrange, method="GET", path="/v1/kb/search", params=sent, body=None)


ANSWER_REQUESTS: Final[tuple[GoldenRequest, ...]] = (
    GoldenRequest("answered", _answer(steps=[_cites(SUNDAYS, SUNDAYS)])),
    GoldenRequest("answered citing two", _answer(steps=[_cites(WEEKDAYS, SUNDAYS)])),
    GoldenRequest("answered hybrid", _hybrid_answer([_cites(WEEKDAYS)])),
    GoldenRequest(
        "answered pii question",
        _answer(steps=[_cites(SUNDAYS)]),
        body={"question": PII_QUESTION},
    ),
    GoldenRequest("dont_know no hits", _answer(hits=())),
    GoldenRequest("dont_know below threshold", _answer(hits=(WEAK,))),
    GoldenRequest("handoff weak evidence", _answer(hits=(WEAK,), config=HANDOFF)),
    GoldenRequest("dont_know model", _answer(steps=[FakeReply(text=reply("dont_know", "", []))])),
    GoldenRequest(
        "dont_know empty answer",
        _answer(steps=[FakeReply(text=reply("answered", "  ", [str(SUNDAYS.chunk_id)]))]),
    ),
    GoldenRequest(
        "dont_know no citation", _answer(steps=[FakeReply(text=reply("answered", "Yes.", []))])
    ),
    GoldenRequest(
        "dont_know citation unverified",
        _answer(steps=[FakeReply(text=reply("answered", "Free.", [FORGED]))]),
    ),
    GoldenRequest(
        "handoff citation unverified",
        _answer(steps=[FakeReply(text=reply("answered", "Free.", [FORGED]))], config=HANDOFF),
    ),
    GoldenRequest("model_unavailable retries", _answer(steps=[Transient()] * 3)),
    GoldenRequest("model_unavailable deadline", _answer(steps=[DeadlineExceeded(deadline_s=10.0)])),
    GoldenRequest("answer_failed refused", _answer(steps=[ModelRefused()])),
    GoldenRequest("answer_failed invalid twice", _answer(steps=[FakeReply(text="not json")] * 2)),
    GoldenRequest("reindex_required", _answer(stored=frozenset({"old-model"}))),
    GoldenRequest(
        "question_too_long",
        _answer(embedder=[InputTooLarge(status_code=413)]),
    ),
    GoldenRequest("embedding_unavailable", _answer(embedder=[Transient()] * 3)),
    GoldenRequest(
        "embedding_failed",
        _answer(embedder=[AuthenticationFailed(status_code=401)]),
    ),
    GoldenRequest("database_unavailable os", _answer(database_fails=OSError("down"))),
    GoldenRequest("database_unavailable sql", _answer(database_fails=SQLAlchemyError("down"))),
    GoldenRequest("no business", _answer(), business=None),
    GoldenRequest("invalid body empty", _answer(), body={"question": "   "}),
    GoldenRequest("invalid body extra key", _answer(), body={"question": QUESTION, "x": 1}),
)
SEARCH_REQUESTS: Final[tuple[GoldenRequest, ...]] = (
    _get("search hits", _search()),
    _get("search k one", _search(), k=1),
    _get("search no hits", _search(hits=())),
    _get("search hybrid", _search(hybrid=True)),
    _get("search pii question", _search(), q=PII_QUESTION),
    _get("search reindex_required", _search(stored=frozenset({"old-model"}))),
    _get(
        "search question_too_long",
        _search(embedder=[InputTooLarge(status_code=413)]),
    ),
    _get(
        "search embedding_unavailable",
        _search(embedder=[Transient()] * 3),
    ),
    _get(
        "search embedding_failed",
        _search(embedder=[AuthenticationFailed(status_code=401)]),
    ),
    _get("search database_unavailable os", _search(database_fails=OSError("down"))),
    _get("search database_unavailable sql", _search(database_fails=SQLAlchemyError("down"))),
    GoldenRequest(
        "search no business",
        _search(),
        method="GET",
        path="/v1/kb/search",
        params={"q": QUESTION},
        body=None,
        business=None,
    ),
    _get("search invalid k", _search(), k=0),
)
REQUESTS: Final = ANSWER_REQUESTS + SEARCH_REQUESTS


# ── Recording ────────────────────────────────────────────────────────────────────────────────


class Collected(logging.Handler):
    """Every record the app logs during a recording (the root logger's records, as caplog's)."""

    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def trace_ids() -> Iterator[str]:
    """The fixed sequence `new_trace_id` is patched to (never all zeros)."""
    n = 0
    while True:
        n += 1
        yield f"{0x7ACE0000 + n:032x}"


def normalise(value: object) -> object:
    """G11's closed list replaced by `REPLACED` (module docstring); null values stay null."""
    if isinstance(value, dict):
        return {
            key: (
                REPLACED
                if item is not None and (key in VOLATILE_KEYS or key.endswith(VOLATILE_SUFFIX))
                else normalise(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalise(item) for item in value]
    return value


def normalised_line(line: str) -> str:
    """A log line with G11's volatile values replaced, otherwise byte for byte: the app's
    formatters write `json.dumps(payload, default=str)`, which the round trip reproduces exactly
    (checked here, so a formatter change cannot hide in the round trip)."""
    payload = json.loads(line)
    if json.dumps(payload) != line:
        raise AssertionError("a log line does not round-trip through json; record it differently")
    return json.dumps(normalise(payload))


@dataclass
class Recording:
    """One pass over `REQUESTS`: the normalised exchanges, and every log message seen (codes:
    the off test asserts no `guard` record was written)."""

    exchanges: list[dict[str, object]]
    messages: list[str]


def _trace_of(value: object) -> object:
    return value.get("trace_id") if isinstance(value, dict) else None


@contextmanager
def _patched(ids: Iterator[str]) -> Iterator[None]:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    def next_id() -> str:
        return next(ids)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Database, "business_exists", business_exists)
        patch.setattr(answer_router, "new_trace_id", next_id)
        patch.setattr(search_router, "new_trace_id", next_id)
        yield


def record(*, limiter: Limiter, settings: Settings | None = None) -> Recording:
    """Run every request of `REQUESTS` through one app and normalise what it returned and logged
    (module docstring). `limiter` is the app's limiter (G11: one that fails if called)."""
    exchanges: list[dict[str, object]] = []
    collected = Collected()
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    ids = trace_ids()
    try:
        with _patched(ids):
            app = create_app(settings or make_settings(), limiter=limiter)
            formatter = root.handlers[0].formatter
            if formatter is None:  # pragma: no cover (configure_logging always sets one)
                raise AssertionError("the app's log handler has no formatter")
            root.addHandler(collected)
            with TestClient(app) as client:
                for request in REQUESTS:
                    arranged = request.arrange()
                    _override(app, arranged)
                    start = len(collected.records)
                    headers = (
                        {} if request.business is None else {BUSINESS_HEADER: request.business}
                    )
                    response = client.request(
                        request.method,
                        request.path,
                        params=request.params,
                        json=request.body,
                        headers=headers,
                    )
                    lines = [
                        formatter.format(r)
                        for r in collected.records[start:]
                        if r.getMessage() in RECORDED
                    ]
                    exchanges.append(_exchange(request, response, lines))
    finally:
        root.setLevel(saved_level)
        root.handlers[:] = saved_handlers
    return Recording(exchanges, [r.getMessage() for r in collected.records])


def _override(app: Any, arranged: Arrangement) -> None:
    """Point both routes' service dependencies at `arranged` (zero-argument callables: FastAPI
    would read a default argument as a query parameter)."""

    def search() -> SearchService:
        return arranged.search

    def answer() -> AnswerService | None:
        return arranged.answer

    app.dependency_overrides[get_search_service] = search
    app.dependency_overrides[get_answer_service] = answer


def _exchange(request: GoldenRequest, response: Any, lines: list[str]) -> dict[str, object]:
    trace = response.headers.get("x-trace-id")
    body = response.text
    payloads = [json.loads(line) for line in lines]
    traces = {trace, _trace_of(_json_or_none(body))}
    traces.update(_trace_of(p.get(message)) for p in payloads for message in RECORDED)
    traces.discard(None)
    return {
        "name": request.name,
        "status": response.status_code,
        "headers": [
            [name, REPLACED if name in VOLATILE_HEADERS else value]
            for name, value in response.headers.multi_items()
        ],
        "body": body if trace is None else body.replace(trace, REPLACED),
        "records": [normalised_line(line) for line in lines],
        "joined": len(traces) <= 1,
    }


def _json_or_none(text: str) -> object:
    try:
        return json.loads(text)
    except ValueError:
        return None


def load_goldens() -> list[dict[str, object]]:
    data = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    exchanges: list[dict[str, object]] = data["exchanges"]
    return exchanges


@dataclass
class UnusedLimiter:
    """A limiter nothing may call while `guard_answer` is off (G11): every call is recorded, then
    raises, so the test fails on the record even if a route turned the error into a response."""

    calls: list[str] = field(default_factory=list)

    def _called(self, operation: str) -> AssertionError:
        self.calls.append(operation)
        return AssertionError(f"the limiter was called: {operation}")

    async def admit(self, *args: object, **kwargs: object) -> None:
        raise self._called("admit")

    async def reserve(self, *args: object, **kwargs: object) -> Any:
        raise self._called("reserve")

    async def settle(self, *args: object, **kwargs: object) -> None:
        raise self._called("settle")

    async def remaining(self, *args: object, **kwargs: object) -> int:
        raise self._called("remaining")


def off_settings() -> Settings:
    """The settings both a recording and a replay run with: `guard_answer` off, a dead Redis."""
    return make_settings(guard_answer="off", redis_url=DEAD_REDIS)


TRACKED_CHANGES: Final = ("--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
"""`git status` of tracked files only: at the base commit this file is the one untracked file."""


def recorded_on(git: Callable[[Sequence[str]], str] = _git) -> str:
    """The full commit a recording runs on, read from git (read-only): HEAD must be `BASE_COMMIT`
    and no tracked file of the tree may differ from it, or the recording is refused."""
    where = ["-C", str(API_DIR)]
    head = git([*where, "rev-parse", "HEAD"]).strip()
    if head != BASE_COMMIT:
        raise SystemExit(f"goldens are recorded on {BASE_COMMIT} only; this tree is at {head}")
    if git([*where, *TRACKED_CHANGES, "--", ":(top)"]).strip():
        raise SystemExit("goldens are recorded on an untouched tree: a tracked file has changed")
    return head


def main(git: Callable[[Sequence[str]], str] = _git) -> None:
    """Re-record `GOLDEN_FILE` (module docstring: only on a reviewed base change, on that commit's
    untouched tree; `recorded_on` refuses any other before anything runs)."""
    commit = recorded_on(git)
    recording = record(limiter=UnusedLimiter(), settings=off_settings())
    GOLDEN_FILE.parent.mkdir(exist_ok=True)
    document = {
        "recorded_on": commit,
        "how": "uv run python -m tests.answer.goldens (apps/api); see tests/answer/goldens.py",
        "exchanges": recording.exchanges,
    }
    GOLDEN_FILE.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", "utf-8")


if __name__ == "__main__":
    main()
