"""G11: while `guard_answer` is off, `POST /v1/answer` and `GET /v1/kb/search` are byte-identical to
the base commit (M7 PR 8). Unit.

**The goldens** (`goldens/guard_answer_off.json`) were recorded on the untouched tree at 6572e29
(M7 PR 6 merged, before PR 8 edited any route code) by `tests/answer/goldens.py`, with
`uv run python -m tests.answer.goldens` from `apps/api/`: a fixed set of requests on the existing
stand-ins (no SQL, the scripted fake embedder, the fake provider behind the real gateway) covering
every answer outcome, every typed error of both routes, the 400 and the 422s, search, and a question
holding a synthetic phone number on each route; for each, the status, the headers, the body and the
`rag_answer`, `llm_call` and `embedding_call` lines as the app formats them. `new_trace_id` is
patched to a fixed sequence, and G11's closed list of volatile fields (`trace_id`, the `X-Trace-Id`
header, `time`, `latency_ms`, every `*_ms`) is replaced by a fixed token. **Re-record** only on a
reviewed change of the base behaviour, on that commit's untouched tree (the recorder refuses any
other): the file's diff is then part of that PR's review.

**Here** every request is replayed with `guard_answer = "off"`, the dead Redis URL of every unit
test and a limiter that fails the test if called: the result must equal the goldens, no `guard`
record may be written, and a second replay must equal the first, so a new record field that is
volatile but not on G11's list fails (`test_nothing_volatile_escapes_g11s_closed_list`, with its
red proof).
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from tests.answer import goldens
from tests.answer.goldens import (
    BASE_COMMIT,
    DEAD_REDIS,
    GOLDEN_FILE,
    PII_QUESTION,
    REPLACED,
    REQUESTS,
    VOLATILE_HEADERS,
    VOLATILE_KEYS,
    VOLATILE_SUFFIX,
    Recording,
    UnusedLimiter,
    _exchange,
    load_goldens,
    normalise,
    normalised_line,
    off_settings,
    record,
    recorded_on,
)
from tests.conftest import make_settings
from vectapilot_api.answer.router import ANSWER_ERRORS
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.db import Database
from vectapilot_api.embeddings.runner import EMBEDDING_CALL
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.guards.types import GUARD_RECORD
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import SEARCH_ERRORS, ErrorTable
from vectapilot_api.retrieval.search import SearchService


def replay() -> tuple[Recording, UnusedLimiter]:
    limiter = UnusedLimiter()
    return record(limiter=limiter, settings=off_settings()), limiter


def differences(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[str]:
    """`name: key` for every exchange field that differs (codes and names only)."""
    found = [f"count: {len(left)} != {len(right)}"] if len(left) != len(right) else []
    for one, other in zip(left, right, strict=False):
        found += [f"{one['name']}: {key}" for key in one if one[key] != other.get(key)]
    return found


# ── G11: byte-identical while off ────────────────────────────────────────────────────────────


def test_the_dead_redis_url_is_the_unit_tests_unreachable_one() -> None:
    assert ":1/" in DEAD_REDIS  # port 1: nothing listens (tests/conftest.py)
    settings = off_settings()  # both a recording and a replay
    assert settings.guard_answer == "off"
    assert str(settings.redis_url.get_secret_value()) == DEAD_REDIS


def test_off_is_byte_identical_to_the_base_commit_and_writes_no_guard_record() -> None:
    recording, limiter = replay()

    goldens = load_goldens()
    assert [e["name"] for e in recording.exchanges] == [g["name"] for g in goldens]
    assert differences(recording.exchanges, goldens) == []
    assert all(exchange["joined"] for exchange in recording.exchanges)
    assert limiter.calls == []
    assert GUARD_RECORD not in recording.messages


def _codes(table: ErrorTable) -> set[str]:
    """A route's error codes: its typed table's, and the request validation's."""
    return {code for _, code, _ in table.values()} | {"invalid_request"}


def test_the_goldens_cover_every_outcome_and_typed_error_of_both_routes() -> None:
    goldens = {g["name"]: g for g in load_goldens()}
    assert [request.name for request in REQUESTS] == list(goldens)  # one golden per request
    bodies = {g["name"]: json.loads(str(g["body"])) for g in goldens.values()}
    outcomes = {body.get("outcome") for body in bodies.values()} - {None}
    assert outcomes == {"answered", "dont_know", "handoff"}
    for path, table in (("/v1/answer", ANSWER_ERRORS), ("/v1/kb/search", SEARCH_ERRORS)):
        names = [request.name for request in REQUESTS if request.path == path]
        assert {bodies[name].get("error") for name in names} - {None} == _codes(table), path
    assert {g["status"] for g in goldens.values()} == {200, 400, 422, 502, 503}
    lines = [str(line) for g in goldens.values() for line in cast(list[str], g["records"])]
    hybrid = [line for line in lines if '"retrieval"' in line]
    assert hybrid and all(f'"embed_ms": "{REPLACED}"' in line for line in hybrid)
    # Search's one record, and the question masking would change on either route.
    assert {r.name for r in REQUESTS if PII_QUESTION in json.dumps([r.body, r.params])} == {
        "answered pii question",
        "search pii question",
    }
    for name in ("search hits", "answered pii question", "search pii question"):
        records = cast(list[str], goldens[name]["records"])
        assert any(f'"message": "{EMBEDDING_CALL}"' in line for line in records), name
    assert json.loads(GOLDEN_FILE.read_text("utf-8"))["recorded_on"] == BASE_COMMIT


# ── Where the goldens come from ──────────────────────────────────────────────────────────────


class FakeGit:
    """`git` answering `rev-parse HEAD` and the tracked-files status, every call recorded."""

    def __init__(self, head: str = BASE_COMMIT, changed: str = "") -> None:
        self.head, self.changed = head, changed
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        return f"{self.head}\n" if "rev-parse" in args else self.changed


def test_the_recorder_records_only_on_the_base_commits_untouched_tree() -> None:
    git = FakeGit()
    assert recorded_on(git) == BASE_COMMIT and len(BASE_COMMIT) == 40
    assert git.calls[1][-3:] == ["--untracked-files=no", "--", ":(top)"]
    with pytest.raises(SystemExit, match="this tree is at f00d"):
        recorded_on(FakeGit(head="f00d" * 10))
    with pytest.raises(SystemExit, match="a tracked file has changed"):
        recorded_on(FakeGit(changed=" M apps/api/src/vectapilot_api/answer/router.py\n"))


def test_the_recorder_refuses_before_it_records_or_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "guard_answer_off.json"  # never the real file, even if the refusal broke

    def recording(**_: object) -> Recording:
        raise AssertionError("recorded on a refused tree")

    monkeypatch.setattr(goldens, "GOLDEN_FILE", target)
    monkeypatch.setattr(goldens, "record", recording)
    with pytest.raises(SystemExit):
        goldens.main(FakeGit(changed=" M apps/api/tests/conftest.py\n"))

    assert not target.exists()


# ── G11's closed list of volatile fields ─────────────────────────────────────────────────────


def test_the_volatile_list_is_g11s_closed_list() -> None:
    assert {"trace_id", "time", "latency_ms"} == VOLATILE_KEYS
    assert VOLATILE_SUFFIX == "_ms"
    assert {"x-trace-id"} == VOLATILE_HEADERS


def test_nothing_volatile_escapes_g11s_closed_list() -> None:
    # Two replays in one process: a field that changes between them (a clock, a random id) and is
    # not on the list would make them differ here, before any golden is compared.
    first, _ = replay()
    second, _ = replay()

    assert differences(first.exchanges, second.exchanges) == []


def test_red_proof_a_new_volatile_field_is_caught() -> None:
    def line(started_at: int, queued_ms: float) -> str:
        record = {"trace_id": "a" * 32, "latency_ms": 1.5, "started_at": started_at}
        payload = {
            "time": "t",
            "message": "llm_call",
            "llm_call": record | {"queued_ms": queued_ms},
        }
        return json.dumps(payload)

    # On the list (`*_ms`): normalised away. Not on it (`started_at`): the two lines differ.
    assert normalised_line(line(1, 1.0)) == normalised_line(line(1, 2.0))
    assert normalised_line(line(1, 1.0)) != normalised_line(line(2, 1.0))
    assert f'"time": "{REPLACED}"' in normalised_line(line(1, 1.0))


def test_a_null_volatile_value_stays_null() -> None:
    payload = {"time": "t", "rag_answer": {"retrieval": {"text_ms": None, "fuse_ms": 2.0}}}

    normalised = json.loads(normalised_line(json.dumps(payload)))

    assert normalised["rag_answer"]["retrieval"] == {"text_ms": None, "fuse_ms": REPLACED}


def test_a_line_that_does_not_round_trip_is_refused() -> None:
    with pytest.raises(AssertionError, match="round-trip"):
        normalised_line('{"time":"t"}')  # compact separators: not what the app writes


def test_a_volatile_field_inside_a_list_is_normalised() -> None:
    value = {"calls": [{"wait_ms": 1.5, "n": 1}]}

    assert normalise(value) == {"calls": [{"wait_ms": REPLACED, "n": 1}]}


class _Headers:
    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def get(self, name: str, default: object = None) -> object:
        return next((value for key, value in self._items if key == name), default)

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._items)


class _Response:
    """The parts of a response `_exchange` reads."""

    def __init__(self, header: str, body: str) -> None:
        self.status_code = 200
        self.text = body
        self.headers = _Headers([("x-trace-id", header)])


def test_joined_is_false_when_the_header_the_body_or_a_record_names_another_trace_id() -> None:
    a, b = "a" * 32, "b" * 32

    def line(message: str, trace: str) -> str:
        return json.dumps({"time": "t", "message": message, message: {"trace_id": trace}})

    def joined(body_trace: str, lines: list[str]) -> object:
        response = _Response(a, json.dumps({"trace_id": body_trace}))
        return _exchange(REQUESTS[0], response, lines)["joined"]

    assert joined(a, [line("rag_answer", a), line(EMBEDDING_CALL, a)]) is True
    assert joined(b, [line("rag_answer", a)]) is False
    for message in ("rag_answer", "llm_call", EMBEDDING_CALL):
        assert joined(a, [line(message, b)]) is False, message


# ── While off, M3's services serve ───────────────────────────────────────────────────────────


def test_while_off_m3s_answer_and_search_services_are_the_ones_that_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", business_exists)
    limiter = UnusedLimiter()
    app = create_app(make_settings(guard_answer="off"), limiter=limiter)
    served: list[object] = []

    with TestClient(app) as client:
        assert app.state.answer_guard is None
        assert app.state.guarded_answer_service is None
        m3 = app.state.answer_service
        assert type(m3) is AnswerService
        assert type(m3._search) is SearchService and type(m3._gateway) is LLMGateway
        assert m3._search is app.state.search_service

        async def answer(business_id: str, question: str, *, trace_id: str) -> Any:
            served.append(("answer", question))
            raise OSError("down")

        async def search(business_id: str, question: str, **_: Any) -> Any:
            served.append(("search", question))
            raise OSError("down")

        monkeypatch.setattr(m3, "answer", answer)
        monkeypatch.setattr(app.state.search_service, "search", search)
        headers = {BUSINESS_HEADER: "clinic-a"}
        posted = client.post("/v1/answer", json={"question": "Open?"}, headers=headers)
        got = client.get("/v1/kb/search", params={"q": "Open?"}, headers=headers)

    assert (posted.status_code, got.status_code) == (503, 503)
    assert served == [("answer", "Open?"), ("search", "Open?")]
    assert limiter.calls == []
