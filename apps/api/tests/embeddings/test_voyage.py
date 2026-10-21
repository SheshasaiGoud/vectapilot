"""The Voyage adapter (`embeddings/voyage.py`) on httpx2's in-process `MockTransport`.

No test here reaches the network or uses a real key: the key is the fake "canary-key-7f3a" and every
HTTP exchange is answered by a handler in this file. The canaries stand in for a key and a document
text; neither may appear in any error text, traceback or log record (ADR 0007, ADR 0008).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from pydantic import SecretStr

from vectapilot_api.embeddings.base import Embedder, EmbeddingSpec, InputTooLarge, RateLimited
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.voyage import EMBEDDINGS_PATH, PINNED_LOGGERS, VoyageEmbedder
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    GatewayError,
    InvalidRequest,
    ProviderError,
    Transient,
)

CANARY_KEY: Final = "canary-key-7f3a"  # fake on purpose, and not shaped like a real key
CANARY_TEXT: Final = "canary-text-7f3a"
BASE_URL: Final = "https://voyage.mock.invalid"
BUSINESS: Final = "sunrise-clinic"
TIMEOUT_S: Final = 5.0
SRC: Final = Path(__file__).resolve().parents[2] / "src" / "vectapilot_api"
SMALL: Final = EmbeddingSpec(
    model="voyage-test",
    dimension=4,
    max_texts_per_request=2,
    max_tokens_per_request=1_000,
    context_tokens=100,
)


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────


def unit(i: int, dimension: int = 4) -> list[float]:
    """A distinct unit vector per i (one-hot), so tests can see the order."""
    return [1.0 if j == i % dimension else 0.0 for j in range(dimension)]


def body_for(
    count: int, *, model: str = "voyage-test-answered", dimension: int = 4
) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": unit(i, dimension), "index": i}
            for i in range(count)
        ],
        "model": model,
        "usage": {"total_tokens": 7 * count},
    }


class Handler:
    """A MockTransport handler: answers each request with the next reply and records it.

    A reply may be a response, an exception to raise, or "echo": a valid body for the request's
    number of inputs.
    """

    def __init__(self, *replies: httpx2.Response | Exception | str) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        if reply == "echo":
            return httpx2.Response(200, json=body_for(len(json.loads(request.content)["input"])))
        assert isinstance(reply, httpx2.Response)
        return reply

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


def make(handler: Any, spec: EmbeddingSpec = SMALL, timeout_s: float = TIMEOUT_S) -> VoyageEmbedder:
    # Only the transport is swapped: the client is the one production uses.
    return VoyageEmbedder(
        api_key=SecretStr(CANARY_KEY),
        spec=spec,
        timeout_s=timeout_s,
        base_url=BASE_URL,
        transport=httpx2.MockTransport(handler),
    )


async def error_for(
    *replies: httpx2.Response | Exception | str, texts: list[str] | None = None
) -> tuple[GatewayError, int]:
    """The error one call raises, and how many HTTP requests it made."""
    handler = Handler(*replies)
    embedder = make(handler)
    try:
        with pytest.raises(GatewayError) as caught:
            await embedder.embed(
                texts or [CANARY_TEXT], input_type="document", business_id=BUSINESS
            )
    finally:
        await embedder.aclose()
    assert_nothing_leaks(caught.value)
    return caught.value, len(handler.requests)


def assert_nothing_leaks(error: GatewayError) -> None:
    # Raised outside the `except` block: no httpx2 exception (its request holds the key header) is
    # chained, so a traceback shows only the typed error and its metadata.
    assert error.__context__ is None
    assert error.__cause__ is None
    text = "".join(traceback.format_exception(error)) + repr(error)
    for canary in (CANARY_KEY, CANARY_TEXT):
        assert canary not in text


def failed(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    # An error body may echo the input and the key: it must never reach the error.
    detail = f"bad input {CANARY_TEXT} for key {CANARY_KEY}"
    return httpx2.Response(status, json={"detail": detail}, headers=headers or {})


# ── Client and request ───────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_request_for_the_catalog_model() -> None:
    spec = load_embedding_catalog().resolve("voyage", None, 1024)
    handler = Handler(httpx2.Response(200, json=body_for(1, model=spec.model, dimension=1024)))
    embedder: Embedder = make(handler, spec)  # mypy checks the Protocol here

    result = await embedder.embed([CANARY_TEXT], input_type="query", business_id=BUSINESS)
    await embedder.aclose()

    (sent,) = handler.requests
    assert (sent.method, str(sent.url)) == ("POST", f"{BASE_URL}{EMBEDDINGS_PATH}")
    assert EMBEDDINGS_PATH == "/v1/embeddings"
    # Exactly these keys: input_type always set, truncation always off, dimension and dtype pinned.
    assert json.loads(sent.content) == {
        "input": [CANARY_TEXT],
        "model": spec.model,
        "input_type": "query",
        "truncation": False,
        "output_dimension": 1024,
        "output_dtype": "float",
    }
    assert sent.headers["authorization"] == f"Bearer {CANARY_KEY}"
    assert sent.headers["content-type"] == "application/json"
    assert sent.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), TIMEOUT_S
    )
    assert (result.model, result.dimension, len(result.vectors[0])) == (spec.model, 1024, 1024)


@pytest.mark.anyio
async def test_batches_are_sent_in_order_and_vectors_come_back_in_input_order() -> None:
    # 5 texts, at most 2 per request: 3 requests. The first reply lists its items out of order.
    shuffled = body_for(2)
    shuffled["data"].reverse()
    handler = Handler(httpx2.Response(200, json=shuffled), "echo")
    embedder = make(handler)

    result = await embedder.embed(
        ["t0", "t1", "t2", "t3", "t4"], input_type="document", business_id=BUSINESS
    )
    await embedder.aclose()

    assert [body["input"] for body in handler.bodies()] == [["t0", "t1"], ["t2", "t3"], ["t4"]]
    assert all(body["input_type"] == "document" for body in handler.bodies())
    assert result.vectors == tuple(tuple(unit(i)) for i in (0, 1, 0, 1, 0))
    assert (result.requests, result.total_tokens, result.input_type) == (3, 35, "document")


@pytest.mark.anyio
async def test_the_model_id_the_provider_returned_is_the_one_stored() -> None:
    # Requested `voyage-test`, answered `voyage-test-answered`: chunks store the answered id.
    embedder = make(Handler("echo"))
    result = await embedder.embed(["a"], input_type="document", business_id=BUSINESS)
    await embedder.aclose()

    assert (embedder.model, result.model) == ("voyage-test", "voyage-test-answered")


@pytest.mark.anyio
async def test_two_model_ids_in_one_call_are_a_provider_error() -> None:
    error, requests = await error_for(
        httpx2.Response(200, json=body_for(2, model="model-a")),
        httpx2.Response(200, json=body_for(1, model="model-b")),
        texts=["a", "b", "c"],
    )

    assert (type(error), requests) == (ProviderError, 2)


@pytest.mark.anyio
async def test_a_too_long_text_is_refused_before_any_request() -> None:
    handler = Handler("echo")
    embedder = make(handler)
    with pytest.raises(InputTooLarge) as caught:
        await embedder.embed(["ok", CANARY_TEXT * 10], input_type="document", business_id=BUSINESS)
    await embedder.aclose()

    assert (caught.value.index, caught.value.limit, handler.requests) == (1, 100, [])
    assert_nothing_leaks(caught.value)


@pytest.mark.anyio
async def test_the_production_client_never_follows_redirects_and_aclose_closes_it() -> None:
    # No transport: the client production builds. Nothing is sent, so nothing reaches the network.
    embedder = VoyageEmbedder(api_key=SecretStr(CANARY_KEY), spec=SMALL, timeout_s=TIMEOUT_S)
    client = embedder._client

    assert client.follow_redirects is False
    assert str(client.base_url) == "https://api.voyageai.com"
    assert CANARY_KEY not in repr(client.headers)  # httpx2 masks the Authorization header
    await embedder.aclose()
    assert client.is_closed


@pytest.mark.anyio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_never_followed(status: int) -> None:
    reply = httpx2.Response(status, headers={"location": "http://collector.invalid/steal"})

    error, requests = await error_for(reply)

    assert (type(error), error.status_code, error.retryable, requests) == (
        ProviderError,
        status,
        False,
        1,
    )


# ── Errors ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (401, AuthenticationFailed, False),
        (403, AuthenticationFailed, False),
        (429, RateLimited, True),
        (413, InputTooLarge, False),
        (400, InvalidRequest, False),
        (404, InvalidRequest, False),
        (422, InvalidRequest, False),
        (408, Transient, True),
        (409, Transient, True),
        (500, Transient, True),
        (502, Transient, True),
        (503, Transient, True),
        (529, Transient, True),
        (402, ProviderError, False),
        (418, ProviderError, False),
        (201, ProviderError, False),
    ],
)
async def test_the_error_table(status: int, kind: type[GatewayError], retryable: bool) -> None:
    error, requests = await error_for(failed(status))

    assert type(error) is kind
    assert (error.status_code, error.retryable, requests) == (status, retryable, 1)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("2", 2.0),
        ("0.5", 0.5),
        ("0", 0.0),
        ("-1", None),
        ("inf", None),
        ("nan", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        (None, None),
    ],
)
async def test_a_rate_limit_carries_retry_after_in_seconds(
    header: str | None, seconds: float | None
) -> None:
    error, _ = await error_for(failed(429, {} if header is None else {"retry-after": header}))

    assert isinstance(error, RateLimited)
    assert error.retry_after == seconds


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError(CANARY_TEXT),
        httpx2.ReadTimeout(CANARY_KEY),
        httpx2.RemoteProtocolError("x"),
    ],
    ids=["connect", "read-timeout", "protocol"],
)
async def test_connection_errors_and_timeouts_are_transient(failure: Exception) -> None:
    error, _ = await error_for(failure)

    assert (type(error), error.status_code, error.retryable) == (Transient, None, True)


@pytest.mark.anyio
async def test_a_hung_request_is_cut_off_by_the_whole_request_timeout() -> None:
    async def hang(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(30)
        raise AssertionError("not reached")

    embedder = make(hang, timeout_s=0.05)
    with pytest.raises(Transient) as caught:
        await embedder.embed(["a"], input_type="document", business_id=BUSINESS)
    await embedder.aclose()

    assert_nothing_leaks(caught.value)


@pytest.mark.anyio
async def test_an_undecodable_body_is_a_provider_error() -> None:
    # A stream, so the client decodes it (a `content=` body would be decoded here, in the test).
    stream = httpx2.ByteStream(b"not gzip")
    reply = httpx2.Response(200, stream=stream, headers={"content-encoding": "gzip"})

    error, _ = await error_for(reply)

    assert type(error) is ProviderError


def malformed_bodies() -> list[Any]:
    good = body_for(1)
    cases: dict[str, Any] = {
        "not-json": b"{not json",
        "plain-text": CANARY_TEXT.encode(),
        "a-list": b"[]",
        "no-data": {k: v for k, v in good.items() if k != "data"},
        "no-model": {k: v for k, v in good.items() if k != "model"},
        "no-usage": {k: v for k, v in good.items() if k != "usage"},
        "secret-shaped-model": good | {"model": "sk-canary-7f3a"},
        "text-as-model": good | {"model": f"{CANARY_TEXT} with spaces"},
        "negative-tokens": good | {"usage": {"total_tokens": -1}},
        "too-few-vectors": good | {"data": []},
        "too-many-vectors": body_for(2),
        "repeated-index": good | {"data": [{"embedding": unit(0), "index": 1}]},
        "string-floats": good | {"data": [{"embedding": ["1", "0", "0", "0"], "index": 0}]},
        "nan": b'{"data":[{"embedding":[NaN,0,0,0],"index":0}],'
        b'"model":"m","usage":{"total_tokens":1}}',
        "wrong-dimension": good | {"data": [{"embedding": [1.0], "index": 0}]},
        "not-unit": good | {"data": [{"embedding": [0.5, 0.5, 0.0, 0.0], "index": 0}]},
        "deeply-nested": b"[" * 100_000 + b"]" * 100_000,
    }
    return [
        pytest.param(v if isinstance(v, bytes) else json.dumps(v).encode(), id=k)
        for k, v in cases.items()
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("content", malformed_bodies())
async def test_a_malformed_response_is_a_provider_error_that_echoes_nothing(content: bytes) -> None:
    error, _ = await error_for(httpx2.Response(200, content=content))

    assert (type(error), error.status_code) == (ProviderError, None)


# ── Logging and the key (ADR 0007) ───────────────────────────────────────────────────────────


def test_the_http_client_loggers_are_pinned_at_warning() -> None:
    assert PINNED_LOGGERS == ("httpx2", "httpcore2", "httpx", "httpcore")
    for name in PINNED_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.anyio
async def test_the_adapter_emits_no_log_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("vectapilot_api.control").debug("control")  # proves DEBUG is captured

    embedder = make(Handler("echo"))
    await embedder.embed([CANARY_TEXT], input_type="document", business_id=BUSINESS)
    await embedder.aclose()
    await error_for(failed(400))
    await error_for(httpx2.ConnectError(CANARY_TEXT))

    assert [record.getMessage() for record in caplog.records] == ["control"]


CANARY_SCRIPT: Final = """
import asyncio, logging, sys, traceback

import httpx2

from vectapilot_api.config import Settings
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.voyage import VoyageEmbedder
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.log import configure_logging

TEXT = sys.argv[1]
BUSINESS = "sunrise-clinic"
assert "anthropic" not in sys.modules  # the pins below must come from the Voyage adapter alone
configure_logging("DEBUG")
settings = Settings(_env_file=None)  # the fake providers (test); it still reads the Voyage key
logging.getLogger("vectapilot_api.canary").debug("debug logging is on")
spec = load_embedding_catalog().resolve("voyage", None, 1024)


def ok(request):
    vector = [1.0] + [0.0] * 1023
    data = [{"embedding": vector, "index": 0}]
    usage = {"total_tokens": 3}
    return httpx2.Response(200, json={"data": data, "model": spec.model, "usage": usage})


def rejected(request):
    return httpx2.Response(400, json={"detail": f"bad: {TEXT} {request.headers['authorization']}"})


def down(request):
    raise httpx2.ConnectError(f"refused: {TEXT} {request.headers['authorization']}")


async def main():
    for handler in (ok, rejected, down):
        embedder = VoyageEmbedder(
            api_key=settings.voyage_api_key, spec=spec, timeout_s=5.0,
            base_url="https://voyage.mock.invalid", transport=httpx2.MockTransport(handler),
        )
        try:
            result = await embedder.embed([TEXT], input_type="document", business_id=BUSINESS)
            print(repr(result))
        except GatewayError as error:
            print("".join(traceback.format_exception(error)), repr(error))
        finally:
            await embedder.aclose()
    print("canary: done")


asyncio.run(main())
"""


def test_canary_no_key_or_text_in_logs_output_or_errors(tmp_path: Path) -> None:
    env = os.environ | {
        "VECTAPILOT_LOG_LEVEL": "DEBUG",
        "PYDANTIC_SETTINGS_DEBUG": "1",
        "VECTAPILOT_VOYAGE_API_KEY": CANARY_KEY,
        "VECTAPILOT_ENVIRONMENT": "test",
        "VECTAPILOT_LLM_PROVIDER": "fake",
        "VECTAPILOT_EMBEDDINGS_PROVIDER": "fake",
        "VECTAPILOT_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
        "VECTAPILOT_REDIS_URL": "redis://127.0.0.1:1/0",
    }
    command = [sys.executable, "-I", "-c", CANARY_SCRIPT, CANARY_TEXT]
    # S603: a fixed command (this interpreter and the script above), no shell.
    result = subprocess.run(  # noqa: S603
        command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, "the canary script failed"
    assert "canary: done" in result.stdout
    assert '"level": "DEBUG"' in result.stdout  # DEBUG logging was on
    assert "InvalidRequest(status_code=400" in result.stdout  # the error paths ran
    assert "Transient(status_code=None" in result.stdout
    for canary in (CANARY_KEY, CANARY_TEXT):
        assert canary not in output


# ── Only the adapter knows the URL; model ids live in the catalog ────────────────────────────


def test_only_the_voyage_adapter_knows_the_url() -> None:
    hits = sorted(
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if "voyageai.com" in p.read_text()
    )

    assert hits == ["embeddings/voyage.py"]


def test_no_embedding_model_id_is_written_in_code() -> None:
    catalog = load_embedding_catalog()
    ids = re.compile("|".join(re.escape(model) for model in catalog.models))
    hits = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if ids.search(p.read_text()))

    assert hits == []
