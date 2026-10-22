"""`GET /v1/kb/search` and `SearchService` without Postgres (M2 PR 6).

H1's `vector_search` is replaced by a stand-in that returns prepared hits and runs no SQL
(`standins.py`); everything around it is real: the `X-Business-Id` dependency, the embedding
runner and its record, the model check, the error mapping. The same route against real Postgres:
tests/answer/test_postgres_search_and_answer.py and the canary suite (both wait for H1).
"""

import logging
from typing import Final

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from tests.answer.apps import A, B, get_search, headers, serve
from tests.embeddings.scripted import ScriptedEmbedder
from tests.retrieval.standins import Connection, build_search, hit
from vectapilot_api.business import UNKNOWN_BUSINESS
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.fake import fake_vector
from vectapilot_api.embeddings.runner import EMBEDDING_CALL
from vectapilot_api.gateway.errors import AuthenticationFailed, Transient
from vectapilot_api.log import JsonFormatter
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import (
    MAX_QUESTION_CHARS,
    check_question,
    new_trace_id,
    top_score,
)
from vectapilot_api.retrieval.types import MAX_K
from vectapilot_api.retrieval.vector import vector_search

CANARY: Final = "canary-question-5e1d"
"""Planted in questions: never in an error body or a log line."""
QUESTION: Final = f"When do you open on Sundays? {CANARY}"


def logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record as the JSON line it would be, `extra` fields included."""
    return "\n".join(JsonFormatter().format(record) for record in caplog.records)


def embedding_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [getattr(r, EMBEDDING_CALL) for r in caplog.records if r.getMessage() == EMBEDDING_CALL]


def assert_error(response: httpx2.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["error"] == code
    assert CANARY not in response.text


def assert_traced_error(
    response: httpx2.Response, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A typed error from the service step: its `X-Trace-Id` is its `embedding_call`'s."""
    assert_error(response, status, code)
    [record] = embedding_records(caplog)
    assert response.headers[TRACE_HEADER] == record["trace_id"]


# ── The service: embed, one transaction, model check, H1 as the customer ─────────────────────


@pytest.mark.anyio
async def test_the_question_is_embedded_as_a_query_then_searched_in_one_business_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    search = build_search({A: [hit("We open at 9.", 0.9)]})
    trace_id = new_trace_id()

    result = await search.service.search(A, QUESTION, k=7, reader="customer", trace_id=trace_id)

    assert search.embedder.batches == [[QUESTION]]
    [record] = embedding_records(caplog)
    assert (record["input_type"], record["operation"], record["trace_id"]) == (
        "query",
        "kb-search",
        trace_id,
    )
    assert search.database.opened == [A]
    [(model_connection, business)] = search.stored.calls
    [(search_connection, query)] = search.standin.calls
    assert business == A
    assert isinstance(search_connection, Connection) and search_connection.business_id == A
    assert model_connection is search_connection  # the check and the search share a transaction
    assert (query.business_id, query.reader, query.k) == (A, "customer", 7)
    assert query.embedding_model == search.embedder.model == result.embedding_model
    assert query.embedding == fake_vector(QUESTION, 1024)
    assert [h.content for h in result.hits] == ["We open at 9."]


@pytest.mark.parametrize(
    "value", ["", "   ", "\n\t", "ok \ud800 not"], ids=["empty", "spaces", "blank", "surrogate"]
)
def test_a_question_without_valid_text_is_refused(value: str) -> None:
    with pytest.raises(ValueError) as raised:
        check_question(value)

    assert value.strip() == "" or "not" not in str(raised.value)


def test_a_question_is_stripped() -> None:
    assert check_question("  Hours?\n") == "Hours?"


def test_top_score_is_the_maximum_whatever_the_order() -> None:
    # Not hits[0]: H1 is hand-written, and the answer's confidence must not depend on its order.
    assert top_score([hit("a", 0.4), hit("b", 0.9), hit("c", 0.6)]) == 0.9
    assert top_score([hit("a", -0.3), hit("b", -0.1)]) == -0.1  # cosine can be negative
    assert top_score([]) is None


def test_trace_ids_are_new_w3c_ids() -> None:
    ids = {new_trace_id() for _ in range(50)}

    assert len(ids) == 50
    assert all(len(i) == 32 and set(i) <= set("0123456789abcdef") for i in ids)


# ── The route ──────────────────────────────────────────────────────────────────────────────


def test_search_returns_the_hits_with_citation_fields_and_scores_best_first(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    first = hit("We open at 9.", 0.9, page=3, source_id="hours", version="2026-10")
    second = hit("Closed on Sundays.", 0.4, title="Sundays", chunk_index=2, language="hi")
    search = build_search({A: [first, second]})
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        response = get_search(client, q=QUESTION)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embedding_model"] == search.embedder.model
    assert body["hits"] == [
        {
            "chunk_id": str(first.chunk_id),
            "document_id": str(first.document_id),
            "source_id": "hours",
            "version": "2026-10",
            "title": "Opening hours",
            "page": 3,
            "chunk_index": 0,
            "language": "en",
            "content_kind": "faq",
            "content": "We open at 9.",
            "score": 0.9,
        },
        {
            "chunk_id": str(second.chunk_id),
            "document_id": str(second.document_id),
            "source_id": "faq",
            "version": "1",
            "title": "Sundays",
            "page": None,
            "chunk_index": 2,
            "language": "hi",
            "content_kind": "faq",
            "content": "Closed on Sundays.",
            "score": 0.4,
        },
    ]
    [record] = embedding_records(caplog)
    assert record["trace_id"] == body["trace_id"] and len(body["trace_id"]) == 32
    assert response.headers[TRACE_HEADER] == body["trace_id"]
    [(_, query)] = search.standin.calls
    assert (query.business_id, query.reader, query.k) == (A, "customer", 5)  # the default k


def test_search_returns_only_this_businesss_hits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = build_search(
        {A: [hit("A's hours.", 0.9)], B: [hit("B's secret price list.", 0.99, source_id="b")]}
    )
    with serve(settings, monkeypatch, search) as client:
        mine, theirs = get_search(client, A, q="hours"), get_search(client, B, q="hours")

    assert [h["content"] for h in mine.json()["hits"]] == ["A's hours."]
    assert "secret" not in mine.text
    assert [h["content"] for h in theirs.json()["hits"]] == ["B's secret price list."]
    assert [q.business_id for _, q in search.standin.calls] == [A, B]
    assert {q.reader for _, q in search.standin.calls} == {"customer"}
    assert search.database.opened == [A, B]


def test_the_model_check_is_made_for_each_request_and_its_own_business(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One service, two businesses: A's single model must not carry over to B, whose chunks carry
    # two (a check cached per process, or made for the wrong business, would let B search).
    search = build_search(
        {A: [hit("A's hours.", 0.9)], B: [hit("never returned", 0.9)]},
        stored={
            A: frozenset({"fake-embedding-1024"}),
            B: frozenset({"fake-embedding-1024", "old"}),
        },
    )
    with serve(settings, monkeypatch, search) as client:
        mine, theirs = get_search(client, A, q="hours"), get_search(client, B, q="hours")

    assert mine.status_code == 200 and [h["content"] for h in mine.json()["hits"]] == ["A's hours."]
    assert_error(theirs, 503, "reindex_required")
    assert [business for _, business in search.stored.calls] == [A, B]
    assert [connection.business_id for connection, _ in search.stored.calls] == [A, B]
    assert [q.business_id for _, q in search.standin.calls] == [A]


@pytest.mark.parametrize("k", [1, 2, MAX_K])
def test_k_is_passed_to_the_search(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, k: int
) -> None:
    search = build_search({A: [hit(f"Chunk {i}.", 0.9 - i / 100) for i in range(3)]})
    with serve(settings, monkeypatch, search) as client:
        response = get_search(client, q="hours", k=k)

    assert response.status_code == 200
    assert search.standin.calls[0][1].k == k
    assert len(response.json()["hits"]) == min(k, 3)


def test_the_question_is_stripped_before_it_is_embedded(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = build_search({A: []})
    with serve(settings, monkeypatch, search) as client:
        get_search(client, q="  hours?  ")

    assert search.embedder.batches == [["hours?"]]


def test_an_empty_knowledge_base_has_no_hits(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = build_search(stored={})
    with serve(settings, monkeypatch, search) as client:
        response = get_search(client, q="hours")

    assert response.status_code == 200 and response.json()["hits"] == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "   "},
        {"q": "x" * (MAX_QUESTION_CHARS + 1)},
        {"q": CANARY, "k": 0},
        {"q": CANARY, "k": MAX_K + 1},
        {"q": CANARY, "k": CANARY},
        {"q": CANARY, "unexpected": CANARY},
    ],
    ids=["no q", "empty", "spaces", "too long", "k 0", "k too big", "k not a number", "extra"],
)
def test_invalid_parameters_are_a_422_that_echoes_nothing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, params: dict[str, str | int]
) -> None:
    search = build_search()
    with serve(settings, monkeypatch, search) as client:
        response = client.get("/v1/kb/search", params=params, headers=headers(A))

    assert_error(response, 422, "invalid_request")
    assert all(set(p) == {"loc", "type"} for p in response.json()["problems"])
    assert TRACE_HEADER not in response.headers  # refused before the request had a trace id
    assert search.embedder.batches == [] and search.standin.calls == []


def test_a_question_at_the_length_limit_is_accepted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve(settings, monkeypatch) as client:
        assert get_search(client, q="x" * MAX_QUESTION_CHARS).status_code == 200


def test_the_length_limit_counts_before_stripping(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One character over the limit, all of it whitespace: still refused (MAX_QUESTION_CHARS says
    # "counted before stripping"), so the limit bounds what the client sent, not what is left.
    search = build_search()
    with serve(settings, monkeypatch, search) as client:
        response = get_search(client, q=" " + "x" * MAX_QUESTION_CHARS)

    assert_error(response, 422, "invalid_request")
    assert TRACE_HEADER not in response.headers  # refused before the request had a trace id
    assert search.embedder.batches == []


@pytest.mark.parametrize("registered", [True, False], ids=["no header", "unregistered"])
def test_no_business_is_refused_before_anything_else(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, registered: bool
) -> None:
    search = build_search()
    with serve(settings, monkeypatch, search, registered=registered) as client:
        response = get_search(client, None if registered else "nobody-here", k=0)

    assert response.status_code == 400 and response.json() == {"detail": UNKNOWN_BUSINESS}
    assert TRACE_HEADER not in response.headers
    assert search.embedder.batches == [] and search.database.opened == []


@pytest.mark.parametrize(
    "stored",
    [frozenset({"fake-embedding-1024", "old-model"}), frozenset({"old-model"})],
    ids=["mixed", "another model"],
)
def test_chunks_of_another_model_mean_reindex_required_and_no_search(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stored: frozenset[str],
) -> None:
    caplog.set_level(logging.INFO)
    search = build_search({A: [hit("never returned", 0.9)]}, stored={A: stored})
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        response = get_search(client, q=QUESTION)

    assert_traced_error(response, 503, "reindex_required", caplog)
    assert search.standin.calls == []
    assert "old-model" not in response.text


@pytest.mark.parametrize(
    ("embedder", "status", "code"),
    [
        (ScriptedEmbedder(script=[Transient()] * 3), 503, "embedding_unavailable"),
        (ScriptedEmbedder(script=[AuthenticationFailed(status_code=401)]), 502, "embedding_failed"),
    ],
    ids=["retries used up", "not retryable"],
)
def test_embedding_failures_are_typed_errors_and_search_nothing(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    embedder: ScriptedEmbedder,
    status: int,
    code: str,
) -> None:
    caplog.set_level(logging.INFO)
    search = build_search({A: [hit("x", 0.9)]}, embedder=embedder)
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        response = get_search(client, q=QUESTION)

    assert_traced_error(response, status, code, caplog)
    assert search.database.opened == [] and search.standin.calls == []


@pytest.mark.parametrize(
    "outage", [OSError(), OperationalError("SELECT 1", {}, Exception())], ids=["os", "sqlalchemy"]
)
def test_a_database_outage_is_a_typed_503(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outage: Exception,
) -> None:
    caplog.set_level(logging.INFO)
    search = build_search({A: [hit("x", 0.9)]})
    search.database.fail = outage
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        response = get_search(client, q=QUESTION)

    assert_traced_error(response, 503, "database_unavailable", caplog)


def test_an_unmapped_error_is_never_typed_so_h1s_stub_reaches_the_caller(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H1's stub raises NotImplementedError: the route must let it through (a 500), so the canary
    # read path through the API is a strict xfail until H1 lands (tests/canary/read_paths.py).
    search = build_search({A: [hit("x", 0.9)]}, raises=NotImplementedError("H1"))
    with serve(settings, monkeypatch, search) as client, pytest.raises(NotImplementedError):
        get_search(client, q=QUESTION)


def test_no_question_or_chunk_text_reaches_the_logs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    search = build_search({A: [hit(f"chunk text {CANARY}", 0.9, title=f"title {CANARY}")]})
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        caplog.set_level(logging.DEBUG)  # create_app set the root logger back to INFO
        assert logging.getLogger().isEnabledFor(logging.DEBUG)
        assert get_search(client, q=QUESTION).status_code == 200
        assert get_search(client, q=QUESTION, k=0).status_code == 422

    assert CANARY not in logged(caplog)
    assert embedding_records(caplog)  # the records are there, without the text


def test_every_search_gets_its_own_trace_id(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    search = build_search({A: [hit("We open at 9.", 0.9)]})
    with serve(settings, monkeypatch, search, caplog=caplog) as client:
        responses = [get_search(client, q="hours") for _ in range(3)]

    ids = [response.json()["trace_id"] for response in responses]
    assert len(set(ids)) == 3
    assert [response.headers[TRACE_HEADER] for response in responses] == ids
    assert [record["trace_id"] for record in embedding_records(caplog)] == ids


def test_the_app_searches_with_h1s_vector_search_and_serves_both_routes(
    settings: Settings,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert app.state.search_service.vector_search is vector_search
        paths = client.get("/openapi.json").json()["paths"]

    assert {"/v1/kb/search", "/v1/answer"} <= set(paths)
    assert set(paths["/v1/kb/search"]) == {"get"} and set(paths["/v1/answer"]) == {"post"}
    for operation in (paths["/v1/kb/search"]["get"], paths["/v1/answer"]["post"]):
        documented = {
            code
            for code, doc in operation["responses"].items()
            if TRACE_HEADER in doc.get("headers", {})
        }
        assert documented == {"200", "422", "502", "503"}  # not 400: refused before a trace id
