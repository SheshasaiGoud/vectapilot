"""The Anthropic adapter (`providers/anthropic.py`) on httpx2's in-process `MockTransport`.

No test here reaches the network or uses a real key: the key is the fake "canary-key-7f3a" and every
HTTP exchange is answered by a handler in this file. The canaries stand in for a key, a prompt and a
completion; none may appear in any error text, log record or output (ADR 0007).
"""

import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from pydantic import SecretStr

from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    GatewayError,
    InvalidRequest,
    ProviderError,
    SpendLimitReached,
    Transient,
)
from vectapilot_api.gateway.providers.anthropic import (
    PINNED_LOGGERS,
    SDK_TIMEOUT_MARGIN_S,
    AnthropicProvider,
)
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.gateway.types import (
    STOP_REASONS,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    Usage,
)

CANARY_KEY: Final = "canary-key-7f3a"  # fake on purpose, and not shaped like a real key
CANARY_PROMPT: Final = "canary-prompt-7f3a"
CANARY_COMPLETION: Final = "canary-completion-7f3a"
CANARIES: Final = (CANARY_KEY, CANARY_PROMPT, CANARY_COMPLETION)
BASE_URL: Final = "https://anthropic.mock.invalid"
ATTEMPT_TIMEOUT_S: Final = 15.0
REQUEST_ID: Final = "req_011CanaryTest"
API_DIR: Final = Path(__file__).resolve().parents[2]


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────


class Handler:
    """A MockTransport handler: gives every request the same reply and records it."""

    def __init__(self, reply: httpx2.Response | Exception) -> None:
        self.reply = reply
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def provider_request(**overrides: Any) -> ProviderRequest:
    fields: dict[str, Any] = {
        "model": "model-requested",
        "effort": "low",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": CANARY_PROMPT}],
    }
    return ProviderRequest.model_validate(fields | overrides)


def message_body(**overrides: Any) -> dict[str, Any]:
    """A Messages API response body, as documented (citations and service tier included)."""
    body: dict[str, Any] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "model-answered",
        "content": [{"type": "text", "text": CANARY_COMPLETION, "citations": None}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "output_tokens": 34,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "inference_geo": "global",
            "service_tier": "standard",
        },
    }
    return body | overrides


def usage_body(**overrides: Any) -> dict[str, Any]:
    usage: dict[str, Any] = message_body()["usage"]
    return usage | overrides


def ok(body: Any = None, *, request_id: str = REQUEST_ID) -> httpx2.Response:
    return httpx2.Response(
        200, json=message_body() if body is None else body, headers={"request-id": request_id}
    )


def failed(
    status: int,
    error_type: str = "api_error",
    message: str = f"An error about {CANARY_PROMPT}",  # an error body may echo request text
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    error: dict[str, Any] = {"type": error_type, "message": message}
    if details is not None:
        error["details"] = details
    body = {"type": "error", "error": error, "request_id": REQUEST_ID}
    return httpx2.Response(status, json=body, headers={"request-id": REQUEST_ID} | (headers or {}))


def make_provider(handler: Handler) -> AnthropicProvider:
    # Only the transport is swapped: the client is the one production uses.
    return AnthropicProvider(
        api_key=SecretStr(CANARY_KEY),
        attempt_timeout_s=ATTEMPT_TIMEOUT_S,
        base_url=BASE_URL,
        transport=httpx2.MockTransport(handler),
    )


async def answer(
    reply: httpx2.Response, request: ProviderRequest | None = None
) -> tuple[ProviderResponse, list[httpx2.Request]]:
    handler = Handler(reply)
    provider = make_provider(handler)
    try:
        return await provider.send(request or provider_request()), handler.requests
    finally:
        await provider.aclose()


async def error_for(reply: httpx2.Response | Exception) -> tuple[GatewayError, int]:
    """The gateway error one attempt raises, and how many HTTP requests it made."""
    handler = Handler(reply)
    provider = make_provider(handler)
    try:
        with pytest.raises(GatewayError) as caught:
            await provider.send(provider_request(system=CANARY_PROMPT))
    finally:
        await provider.aclose()
    return caught.value, len(handler.requests)


def assert_nothing_leaks(error: GatewayError) -> None:
    # Raised outside the `except` block: no SDK exception (its request holds the key header) is
    # chained, so a traceback shows only the gateway error and its metadata.
    assert error.__context__ is None
    assert error.__cause__ is None
    text = "".join(traceback.format_exception(error)) + repr(error)
    for canary in CANARIES:
        assert canary not in text


# ── Client and request body ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_adapter_implements_the_provider_protocol() -> None:
    adapter = make_provider(Handler(ok()))
    provider: Provider = adapter  # the check: mypy verifies this assignment against the Protocol
    try:
        response = await provider.send(provider_request())
    finally:
        await adapter.aclose()

    assert isinstance(response, ProviderResponse)


@pytest.mark.anyio
@pytest.mark.parametrize("alias", sorted(load_catalog().aliases))
async def test_request_body_per_alias(alias: str) -> None:
    entry = load_catalog().aliases[alias]
    request = provider_request(
        model=entry.model, effort=entry.effort, max_tokens=entry.max_output_tokens
    )

    _, requests = await answer(ok(), request)

    (sent,) = requests
    assert (sent.method, str(sent.url)) == ("POST", f"{BASE_URL}/v1/messages")
    body = json.loads(sent.content)
    # Exactly these keys: no temperature, no thinking (omitted = adaptive), no inference_geo.
    assert body == {
        "model": entry.model,
        "max_tokens": entry.max_output_tokens,
        "messages": [{"role": "user", "content": CANARY_PROMPT}],
        "output_config": {"effort": "low"},
    }
    assert not {"temperature", "thinking", "inference_geo", "system"} & body.keys()
    assert sent.headers["x-api-key"] == CANARY_KEY
    # The SDK timeout is the backstop, slightly above the gateway's per-attempt limit.
    backstop = ATTEMPT_TIMEOUT_S + SDK_TIMEOUT_MARGIN_S
    assert sent.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), backstop
    )


@pytest.mark.anyio
async def test_the_system_prompt_is_sent_when_set() -> None:
    _, requests = await answer(ok(), provider_request(system="You are a support assistant."))

    assert json.loads(requests[0].content)["system"] == "You are a support assistant."


@pytest.mark.anyio
async def test_the_explicit_key_and_url_win_over_the_sdk_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Settings refuses these with the anthropic provider; the adapter does not rely on that alone.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "env-token-must-not-be-used")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env-url.invalid")

    _, requests = await answer(ok())

    assert str(requests[0].url).startswith(f"{BASE_URL}/")
    assert requests[0].headers["x-api-key"] == CANARY_KEY
    assert "authorization" not in requests[0].headers


@pytest.mark.anyio
async def test_the_sdk_lets_anthropic_custom_headers_replace_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A record of SDK 1.12.0 behaviour, and the reason `Settings` refuses this variable with the
    # anthropic provider (ADR 0006): if an SDK upgrade changes it, this test says so.
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-api-key: env-key-wins\nanthropic-beta: b-1")

    _, requests = await answer(ok())

    assert requests[0].headers["x-api-key"] == "env-key-wins"
    assert requests[0].headers["anthropic-beta"] == "b-1"


@pytest.mark.anyio
async def test_the_production_client_never_follows_redirects_and_aclose_closes_it() -> None:
    # No transport: the client production builds. Nothing is sent, so nothing reaches the network.
    provider = AnthropicProvider(api_key=SecretStr(CANARY_KEY), attempt_timeout_s=ATTEMPT_TIMEOUT_S)
    sdk_client = provider._client

    assert sdk_client._client.follow_redirects is False
    await provider.aclose()
    assert sdk_client.is_closed()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_never_followed(status: int) -> None:
    # The SDK's default client would follow it, re-sending the x-api-key header (httpx2 strips
    # only Authorization) and, on a 307/308, the prompt, over plain HTTP to another host.
    reply = httpx2.Response(
        status, headers={"location": "http://collector.invalid/steal", "request-id": REQUEST_ID}
    )

    error, requests = await error_for(reply)

    assert type(error) is ProviderError  # row 6, never retried
    assert (error.status_code, error.request_id, error.retryable) == (status, REQUEST_ID, False)
    assert requests == 1  # the collector never gets a request
    assert_nothing_leaks(error)


# ── Error table (ADR 0006), first match wins: one test per row ──────────────────────────────

SPEND_LIMIT_400: Final = "You have reached your specified API usage limits. You will regain access"
SPEND_LIMIT_400_WORKSPACE: Final = (
    "You have reached your specified workspace API usage limits. You will regain access"
)
SPEND_LIMIT_DETAILS: Final = {"error_code": "enforced_spend_limit_reached"}


def spend_limits(headers: dict[str, str] | None = None) -> list[Any]:
    return [
        pytest.param(failed(400, "invalid_request_error", SPEND_LIMIT_400, headers=headers), 400),
        pytest.param(
            failed(400, "invalid_request_error", SPEND_LIMIT_400_WORKSPACE, headers=headers), 400
        ),
        pytest.param(
            failed(429, "rate_limit_error", details=SPEND_LIMIT_DETAILS, headers=headers), 429
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "status"), spend_limits(), ids=["400", "400-workspace", "429-error-code"]
)
async def test_row_1_a_spend_limit_is_spend_limit_reached(
    reply: httpx2.Response, status: int
) -> None:
    error, requests = await error_for(reply)

    assert type(error) is SpendLimitReached
    assert (error.status_code, error.request_id, error.retryable) == (status, REQUEST_ID, False)
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize("should_retry", ["true", "false", None])
@pytest.mark.parametrize("shape", [0, 1, 2], ids=["400", "400-workspace", "429-error-code"])
async def test_row_1_x_should_retry_never_makes_a_spend_limit_retryable(
    should_retry: str | None, shape: int
) -> None:
    headers = None if should_retry is None else {"x-should-retry": should_retry}
    reply = spend_limits(headers)[shape].values[0]

    error, requests = await error_for(reply)

    assert type(error) is SpendLimitReached
    assert error.retryable is False
    assert requests == 1  # max_retries=0: the SDK never retries on its own


@pytest.mark.anyio
async def test_row_2_a_402_is_a_billing_error() -> None:
    error, requests = await error_for(failed(402, "billing_error"))

    assert type(error) is BillingError
    assert (error.status_code, error.request_id, error.retryable) == (402, REQUEST_ID, False)
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (408, "timeout_error"),
        (409, "api_error"),
        (429, "rate_limit_error"),  # a plain rate limit: no spend-limit error code
        (500, "api_error"),
        (503, "api_error"),
        (504, "timeout_error"),
        (529, "overloaded_error"),
    ],
)
async def test_row_3_transient_statuses(status: int, error_type: str) -> None:
    error, requests = await error_for(failed(status, error_type))

    assert type(error) is Transient
    assert (error.status_code, error.request_id, error.retryable) == (status, REQUEST_ID, True)
    assert error.retry_after is None
    assert requests == 1  # the SDK would retry all of these itself; max_retries=0 stops it
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError(f"connection refused while sending {CANARY_PROMPT}"),
        httpx2.ReadTimeout(f"read timed out ({CANARY_KEY})"),
    ],
    ids=["connection-error", "timeout"],
)
async def test_row_3_connection_errors_and_timeouts_are_transient(failure: Exception) -> None:
    error, requests = await error_for(failure)

    assert type(error) is Transient
    assert (error.status_code, error.request_id, error.retry_after) == (None, None, None)
    assert error.retryable is True
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type"), [(401, "authentication_error"), (403, "permission_error")]
)
async def test_row_4_auth_statuses_are_authentication_failed(status: int, error_type: str) -> None:
    error, requests = await error_for(failed(status, error_type))

    assert type(error) is AuthenticationFailed
    assert (error.status_code, error.request_id, error.retryable) == (status, REQUEST_ID, False)
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, "invalid_request_error"),  # its message echoes the prompt: never in the error
        (404, "not_found_error"),
        (413, "request_too_large"),
        (422, "invalid_request_error"),
    ],
)
async def test_row_5_request_statuses_are_invalid_request(status: int, error_type: str) -> None:
    error, requests = await error_for(failed(status, error_type))

    assert type(error) is InvalidRequest
    assert (error.status_code, error.request_id, error.retryable) == (status, REQUEST_ID, False)
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "status_code"), [(418, 418), (600, None)])
async def test_row_6_any_other_status_is_a_provider_error(
    status: int, status_code: int | None
) -> None:
    # 600 is outside HTTP's range: the SDK passes it on, the error then carries no status.
    error, requests = await error_for(failed(status))

    assert type(error) is ProviderError
    assert (error.status_code, error.request_id, error.retryable) == (
        status_code,
        REQUEST_ID,
        False,
    )
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "kind"),
    [
        (httpx2.Response(500, text=f"<html>{CANARY_PROMPT}</html>"), Transient),  # not JSON
        (httpx2.Response(400, json={"error": "not an object"}), InvalidRequest),
        (httpx2.Response(400, json={"error": {"message": 42}}), InvalidRequest),
        (httpx2.Response(429, json={"error": {"details": "not an object"}}), Transient),
        (failed(429, "rate_limit_error", details={"error_code": "other_code"}), Transient),
        (failed(500, "api_error", SPEND_LIMIT_400), Transient),  # the 400 text, on a 500
    ],
    ids=[
        "html-500",
        "error-not-object",
        "message-not-text",
        "details-not-object",
        "other-code",
        "500",
    ],
)
async def test_odd_error_bodies_are_never_a_spend_limit(
    reply: httpx2.Response, kind: type[GatewayError]
) -> None:
    error, _ = await error_for(reply)

    assert type(error) is kind
    assert_nothing_leaks(error)


@pytest.mark.anyio
@pytest.mark.parametrize("request_id", ["has spaces", "line\nbreak", "x" * 129])
async def test_a_malformed_request_id_never_changes_the_error_class(request_id: str) -> None:
    reply = failed(429, "rate_limit_error", details=SPEND_LIMIT_DETAILS)
    reply.headers["request-id"] = request_id

    error, _ = await error_for(reply)

    assert type(error) is SpendLimitReached
    assert error.request_id is None
    assert_nothing_leaks(error)


# ── x-should-retry and Retry-After ──────────────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "header", "retryable"),
    [
        (529, "false", False),  # Transient: the header may turn retrying off …
        (529, "true", True),
        (529, None, True),
        (529, "TRUE", True),  # only exactly "true"/"false" count, as in the SDK
        (418, "true", True),  # ProviderError: … or on
        (418, "false", False),
        (418, None, False),
        (400, "true", False),  # never for any other class
        (401, "true", False),
        (402, "true", False),
    ],
)
async def test_x_should_retry_flips_only_transient_and_provider_error(
    status: int, header: str | None, retryable: bool
) -> None:
    headers = None if header is None else {"x-should-retry": header}

    error, requests = await error_for(failed(status, headers=headers))

    assert error.retryable is retryable
    assert requests == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("7", 7.0),
        ("0.5", 0.5),
        ("0", 0.0),
        ("-1", None),
        ("nan", None),
        ("inf", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # the API sends seconds; a date is not used
        ("soon", None),
    ],
)
async def test_retry_after_is_read_in_seconds(header: str, seconds: float | None) -> None:
    error, _ = await error_for(failed(429, "rate_limit_error", headers={"retry-after": header}))

    assert isinstance(error, Transient)
    assert error.retry_after == seconds


# ── Response mapping ─────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_every_response_field_is_mapped() -> None:
    body = message_body(
        content=[
            {"type": "thinking", "thinking": "Let me think.", "signature": "sig-1"},
            {"type": "redacted_thinking", "data": "opaque-data"},
            {"type": "text", "text": CANARY_COMPLETION, "citations": None},
        ],
        usage=usage_body(
            input_tokens=11,
            output_tokens=22,
            cache_read_input_tokens=33,
            cache_creation_input_tokens=99,
            cache_creation={"ephemeral_5m_input_tokens": 44, "ephemeral_1h_input_tokens": 55},
            inference_geo="us",
        ),
    )

    response, _ = await answer(ok(body))

    assert response == ProviderResponse(
        content=(
            ThinkingBlock(thinking="Let me think.", signature="sig-1"),
            RedactedThinkingBlock(data="opaque-data"),
            TextBlock(text=CANARY_COMPLETION),
        ),
        stop_reason="end_turn",
        usage=Usage(
            input_tokens=11,
            output_tokens=22,
            cache_read_tokens=33,
            cache_write_5m_tokens=44,
            cache_write_1h_tokens=55,
        ),
        model="model-answered",  # the answering model, not the requested one
        request_id=REQUEST_ID,
        inference_geo="us",
    )


@pytest.mark.anyio
async def test_without_the_ttl_split_cache_writes_count_as_5_minute_writes() -> None:
    usage = usage_body(cache_creation_input_tokens=7, cache_creation=None)

    response, _ = await answer(ok(message_body(usage=usage)))

    assert (response.usage.cache_write_5m_tokens, response.usage.cache_write_1h_tokens) == (7, 0)


@pytest.mark.anyio
async def test_absent_optional_usage_fields_are_zero_and_the_geo_unknown() -> None:
    response, _ = await answer(ok(message_body(usage={"input_tokens": 1, "output_tokens": 2})))

    assert response.usage == Usage(input_tokens=1, output_tokens=2)
    assert response.inference_geo is None


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", sorted(STOP_REASONS - {"other"}))
async def test_known_stop_reasons_pass_through(stop_reason: str) -> None:
    response, _ = await answer(ok(message_body(stop_reason=stop_reason)))

    assert response.stop_reason == stop_reason


@pytest.mark.anyio
@pytest.mark.parametrize("stop_reason", ["brand_new_reason", None, 7, ["end_turn"]])
async def test_an_unknown_stop_reason_becomes_other(stop_reason: object) -> None:
    response, _ = await answer(ok(message_body(stop_reason=stop_reason)))

    assert response.stop_reason == "other"


MALFORMED: Final = [
    pytest.param(
        {"content": [{"type": "tool_use", "id": "t1", "name": CANARY_PROMPT, "input": {}}]},
        id="unknown-block",
    ),
    pytest.param({"content": [{"type": ["text"], "text": CANARY_COMPLETION}]}, id="odd-block-type"),
    pytest.param({"content": [{"type": "text"}]}, id="text-missing"),
    pytest.param({"content": [{"type": "thinking", "thinking": "t"}]}, id="signature-missing"),
    pytest.param({"content": None}, id="content-missing"),
    pytest.param({"usage": None}, id="usage-missing"),
    pytest.param({"usage": usage_body(input_tokens="12")}, id="tokens-as-text"),
    pytest.param({"usage": usage_body(output_tokens=-1)}, id="negative-tokens"),
    pytest.param({"usage": usage_body(inference_geo=f"us {CANARY_COMPLETION}")}, id="odd-geo"),
    pytest.param({"model": f"model {CANARY_COMPLETION}"}, id="odd-model-id"),
    pytest.param({"model": None}, id="model-missing"),
    # Optional counts: the SDK coerces what pydantic's lax mode accepts (false -> 0, "7" -> 7) and
    # passes anything else on as given; once a falsy value was read as 0, now it is refused.
    pytest.param({"usage": usage_body(cache_read_input_tokens="")}, id="cache-read-empty-text"),
    pytest.param(
        {"usage": usage_body(cache_creation_input_tokens=[], cache_creation=None)},
        id="cache-write-empty-list",
    ),
    # The cache-write total must equal its 5-minute + 1-hour split, or billed tokens go uncounted.
    pytest.param(
        {"usage": usage_body(cache_creation_input_tokens=100)}, id="cache-total-not-split"
    ),
    pytest.param(
        {
            "usage": usage_body(
                cache_creation_input_tokens=300,
                cache_creation={
                    "ephemeral_5m_input_tokens": 100,
                    "ephemeral_1h_input_tokens": 100,
                    "ephemeral_24h_input_tokens": 100,
                },
            )
        },
        id="unknown-cache-ttl",
    ),
]


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", MALFORMED)
async def test_a_malformed_response_is_a_provider_error_that_echoes_nothing(
    overrides: dict[str, Any],
) -> None:
    error, requests = await error_for(ok(message_body(**overrides)))

    assert type(error) is ProviderError
    assert (error.status_code, error.request_id, error.retryable) == (None, REQUEST_ID, False)
    assert requests == 1
    assert_nothing_leaks(error)


@pytest.mark.anyio
async def test_a_malformed_request_id_in_a_response_is_a_provider_error() -> None:
    error, _ = await error_for(ok(request_id=f"req {CANARY_COMPLETION}"))

    assert type(error) is ProviderError
    assert error.request_id is None
    assert_nothing_leaks(error)


JSON_200: Final = {"content-type": "application/json", "request-id": REQUEST_ID}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reply",
    [
        # The SDK hands back a plain str for a non-JSON body, and the decoded value for a JSON
        # list; for broken JSON it raises `json.JSONDecodeError`, whose `.doc` holds the body; for
        # a deeply nested body the JSON decoder raises `RecursionError`.
        httpx2.Response(
            200,
            text=CANARY_COMPLETION,
            headers={"content-type": "text/plain", "request-id": REQUEST_ID},
        ),
        httpx2.Response(200, json=[CANARY_COMPLETION], headers={"request-id": REQUEST_ID}),
        httpx2.Response(200, content=f'{{"text": "{CANARY_COMPLETION}'.encode(), headers=JSON_200),
        httpx2.Response(200, content=b"[" * 50_000 + b"]" * 50_000, headers=JSON_200),
    ],
    ids=["plain-text", "json-list", "broken-json", "deeply-nested"],
)
async def test_an_unreadable_200_is_a_provider_error(reply: httpx2.Response) -> None:
    error, requests = await error_for(reply)

    assert type(error) is ProviderError
    # Known limit (`_response`): the SDK keeps no request id for these, though one was sent.
    assert (error.status_code, error.request_id, error.retryable) == (None, None, False)
    assert requests == 1
    assert_nothing_leaks(error)


# ── Logging (ADR 0007) ───────────────────────────────────────────────────────────────────────


def test_the_sdk_and_http_client_loggers_are_pinned_at_warning() -> None:
    assert PINNED_LOGGERS == ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore")
    for name in PINNED_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.anyio
async def test_the_adapter_emits_no_log_records(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # the root logger at DEBUG, as VECTAPILOT_LOG_LEVEL=DEBUG
    logging.getLogger("vectapilot_api.control").debug("control")  # proves DEBUG is captured

    await answer(ok())
    await error_for(failed(400, "invalid_request_error"))
    await error_for(httpx2.ConnectError(CANARY_PROMPT))

    assert [record.getMessage() for record in caplog.records] == ["control"]


# ── Import ban (ADR 0006, One entry point) ───────────────────────────────────────────────────


def one_import(module: str, name: str) -> str:
    """A module that imports one name and is clean under every other ruff rule."""
    return f'from {module} import {name}\n\n__all__ = ["{name}"]\n'


SDK_IMPORT: Final = one_import("anthropic.types", "Usage")
ADAPTER: Final = "vectapilot_api.gateway.providers.anthropic"
MAIN: Final = "src/vectapilot_api/main.py"


@pytest.mark.parametrize(
    ("path", "source", "banned"),
    [
        ("src/vectapilot_api/gateway/providers/fake.py", SDK_IMPORT, True),
        ("src/vectapilot_api/gateway/types.py", SDK_IMPORT, True),
        (MAIN, SDK_IMPORT, True),
        ("tests/gateway/test_anthropic_provider.py", SDK_IMPORT, True),
        ("scripts/coverage_gate.py", SDK_IMPORT, True),
        ("src/vectapilot_api/gateway/providers/anthropic.py", SDK_IMPORT, False),
        # The SDK re-exported through the adapter is banned too; the adapter's own API is not.
        (MAIN, one_import(ADAPTER, "anthropic"), True),
        (MAIN, one_import(ADAPTER, "Message"), True),
        (MAIN, one_import(ADAPTER, "AnthropicProvider"), False),
    ],
)
def test_only_the_adapter_may_import_the_sdk(path: str, source: str, banned: bool) -> None:
    # Runs the project's own ruff config (every selected rule, so TID251 must be switched on) on
    # a module that is clean under every other rule, as if it were saved at `path`.
    command = [sys.executable, "-m", "ruff", "check", "--no-cache", "--stdin-filename", path, "-"]
    # S603: a fixed command, no shell; the input is one of the constants above.
    result = subprocess.run(  # noqa: S603
        command, input=source, capture_output=True, text=True, cwd=API_DIR, check=False
    )

    assert ("TID251" in result.stdout) is banned
    assert result.returncode == (1 if banned else 0)


# ── Canary (ADR 0007): a fresh process with every debug switch on ───────────────────────────

CANARY_SCRIPT: Final = """
import asyncio, logging, sys, traceback

import httpx2

from vectapilot_api.config import Settings
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.providers.anthropic import AnthropicProvider
from vectapilot_api.gateway.providers.base import ProviderRequest
from vectapilot_api.log import configure_logging

PROMPT, COMPLETION = sys.argv[1:3]

# DEBUG logging to stdout first, so pydantic-settings' debug dump (PYDANTIC_SETTINGS_DEBUG=1) of the
# raw key and DSN would be printed if `config.py`'s logger pin were missing. Then Settings with the
# fake provider (it refuses ANTHROPIC_LOG with the anthropic one, so the adapter is built directly
# below); it still reads the key from the environment.
configure_logging("DEBUG")
settings = Settings(_env_file=None)
assert settings.log_level == "DEBUG"
logging.getLogger("vectapilot_api.canary").debug("debug logging is on")


def ok(request):
    body = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "canary-model",
        "content": [{"type": "text", "text": COMPLETION}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1, "inference_geo": "global"},
    }
    return httpx2.Response(200, json=body, headers={"request-id": "req_canary"})


def rejected(request):
    error = {"type": "invalid_request_error", "message": f"bad request: {PROMPT} {COMPLETION}"}
    return httpx2.Response(400, json={"type": "error", "error": error})


async def main():
    request = ProviderRequest(
        model="canary-model", effort="low", max_tokens=16, system=PROMPT,
        messages=[{"role": "user", "content": PROMPT}],
    )
    for handler in (ok, rejected):
        provider = AnthropicProvider(
            api_key=settings.anthropic_api_key,
            attempt_timeout_s=settings.llm_attempt_timeout_seconds,
            base_url="https://anthropic.mock.invalid",
            transport=httpx2.MockTransport(handler),
        )
        try:
            response = await provider.send(request)
            assert response.content[0].text == COMPLETION  # checked here, never printed
        except GatewayError as error:
            print("".join(traceback.format_exception(error)), repr(error))
        finally:
            await provider.aclose()
    print("canary: done")


asyncio.run(main())
"""


DSN_CANARY: Final = "not-a-real-secret"  # the DSN password: fake, checked like the canaries


def test_canary_no_key_prompt_or_completion_in_logs_output_or_errors(tmp_path: Path) -> None:
    env = os.environ | {
        # Every switch that could print request or response text, set before any import.
        "ANTHROPIC_LOG": "debug",
        "VECTAPILOT_LOG_LEVEL": "DEBUG",
        "PYDANTIC_SETTINGS_DEBUG": "1",
        "VECTAPILOT_ANTHROPIC_API_KEY": CANARY_KEY,
        "VECTAPILOT_ENVIRONMENT": "test",
        "VECTAPILOT_LLM_PROVIDER": "fake",
        "VECTAPILOT_DATABASE_URL": f"postgresql+asyncpg://u:{DSN_CANARY}@127.0.0.1:1/db",
        "VECTAPILOT_REDIS_URL": "redis://127.0.0.1:1/0",
    }
    command = [sys.executable, "-I", "-c", CANARY_SCRIPT, CANARY_PROMPT, CANARY_COMPLETION]
    # S603: a fixed command (this interpreter and the script above), no shell.
    result = subprocess.run(  # noqa: S603
        command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120, check=False
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, "the canary script failed"
    assert "canary: done" in result.stdout
    # DEBUG logging was on, so the canary check below means something.
    assert '"level": "DEBUG"' in result.stdout
    assert "InvalidRequest(status_code=400" in result.stdout  # the error path ran
    for canary in (*CANARIES, DSN_CANARY):
        assert canary not in output
