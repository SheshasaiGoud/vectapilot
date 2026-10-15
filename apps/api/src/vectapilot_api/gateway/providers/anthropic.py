"""The Anthropic adapter: the only module that imports the Anthropic SDK (ADR 0006).

One `send` is one attempt (`providers/base.py`): no retries, no time limits of its own beyond a
backstop, no cost. What this module guarantees:

- **Client.** The key and the base URL are always passed explicitly, so the SDK never falls back
  to `ANTHROPIC_API_KEY` or `ANTHROPIC_BASE_URL` (`ANTHROPIC_CUSTOM_HEADERS` it reads regardless:
  `Settings` refuses it). `max_retries=0`: the gateway is the only retry layer, so retries never
  multiply. The SDK timeout sits `SDK_TIMEOUT_MARGIN_S` above the gateway's per-attempt limit: a
  backstop only, the gateway's `asyncio.timeout` fires first. Redirects are never followed: the
  SDK's default client follows them, and httpx2 strips only `Authorization` on a cross-origin one,
  so the `x-api-key` header and the prompt would go to the new host.
- **Request.** Model id, `max_tokens`, `output_config.effort`, the messages and the optional system
  prompt. Never `temperature`, `thinking` (omitted = adaptive) or `inference_geo` (the workspace
  default applies).
- **Errors.** ADR 0006's table, first match wins (`_status_error`). Only plain data (status,
  headers, body) leaves an `except` block; the gateway error is built and raised after it, so no SDK
  exception (its request holds the key header) is ever chained to it.
- **Response.** The SDK parses leniently: a missing field becomes None, an unknown content block a
  `TextBlock` with `type` set to the unknown value, a non-JSON body a plain `str`. So every value is
  validated into the gateway's types here; any mismatch is a `ProviderError` that does not echo the
  value, and the `ValidationError` is dropped (never logged, chained or turned into text).
- **Logging.** This module logs nothing, and pins the SDK's and the HTTP clients' loggers to WARNING
  right after import (ADR 0007).

Known limits: a traceback that captures local variables (pytest `-l`, an error tracker's default)
can still show the SDK `Message` in `send` and `_response`, so that capture stays off (CLAUDE.md,
Known issues). The import ban (ruff TID251, pyproject.toml) sees static imports only, not
`importlib.import_module("anthropic")`.

SDK facts this relies on were read in the installed anthropic 1.12.0 (file:line in the PR 5 notes).
"""

import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import anthropic
from anthropic.types import Message
from pydantic import SecretStr

from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    GatewayError,
    InvalidRequest,
    ProviderError,
    SpendLimitReached,
    Transient,
)
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.types import STOP_REASONS, check_opaque_id

if TYPE_CHECKING:
    import httpx2

PINNED_LOGGERS: Final = ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore")
"""Loggers that print request options, URLs and headers at DEBUG/INFO. Names as in the installed
packages: `anthropic` and `httpx2` (anthropic `_utils/_logs.py`, set by `ANTHROPIC_LOG`),
`httpcore2.*` (its connection traces). `httpx`/`httpcore` too, in case the classic client returns
(ADR 0007: Langfuse's SDK needs it)."""

# `import anthropic` above ran the SDK's `setup_logging()`: with ANTHROPIC_LOG=debug it has just set
# `anthropic` and `httpx2` to DEBUG. Pinned back to WARNING here, after that import, so neither
# ANTHROPIC_LOG nor a DEBUG root logger turns on transport traces. Child loggers
# (`anthropic._base_client`, `httpcore2.http11`, …) inherit the level.
for _name in PINNED_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

ANTHROPIC_API_URL: Final = "https://api.anthropic.com"
SDK_TIMEOUT_MARGIN_S: Final = 1.0
"""How far the SDK's own timeout sits above the gateway's per-attempt limit (backstop only). The SDK
applies it per phase (connect, read, write, pool), not as a total: only the gateway's
`asyncio.timeout` bounds a whole attempt."""

# /api/errors and /api/rate-limits (checked 2026-10-14): the two documented spend-limit shapes.
_SPEND_LIMIT_MESSAGE: Final = re.compile(
    r"You have reached your specified (?:workspace )?API usage limits"
)
_SPEND_LIMIT_CODE: Final = "enforced_spend_limit_reached"

_BLOCK_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "text": ("text",),
    "thinking": ("thinking", "signature"),
    "redacted_thinking": ("data",),
}
"""The content blocks M1 accepts, and the fields copied from each (a text block's `citations` is
not used in M1). Any other block type fails validation: M1 sends no tools."""


@dataclass(frozen=True)
class _Failure:
    """What a failed attempt left behind, as plain data (never the SDK exception)."""

    status: int | None = None
    """The HTTP status; None if there was no response (connection error or timeout)."""
    headers: Mapping[str, str] = field(default_factory=dict)
    body: object = field(default=None, repr=False)
    """The error body, which may echo the prompt: kept out of `repr` (and so out of local-variable
    dumps)."""
    unreadable: bool = False
    """The SDK could not read a response (for example a 200 whose body is not JSON)."""


class AnthropicProvider:
    """The `Provider` for the Anthropic Messages API. Build one per process; `aclose` at shutdown.

    `transport` is for tests: an `httpx2.MockTransport`, so no test ever reaches the network. Tests
    and production run the same client (the SDK's default, redirects off); only the transport
    differs. In production it stays None and the SDK sets up its own transport.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        attempt_timeout_s: float,
        base_url: str = ANTHROPIC_API_URL,
        transport: "httpx2.AsyncBaseTransport | None" = None,
    ) -> None:
        # Two calls, not `transport=None`: passing it at all skips the SDK's own transport setup
        # (TCP keepalive, `anthropic/_base_client.py:1632`).
        http_client = (
            anthropic.DefaultAsyncHttpxClient(follow_redirects=False)
            if transport is None
            else anthropic.DefaultAsyncHttpxClient(follow_redirects=False, transport=transport)
        )
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            max_retries=0,
            timeout=attempt_timeout_s + SDK_TIMEOUT_MARGIN_S,
            http_client=http_client,
        )

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        try:
            message = await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                # Built inline, so no local variable of this frame holds the prompt.
                messages=[{"role": m.role, "content": m.content} for m in request.messages],
                output_config={"effort": request.effort},
                system=anthropic.omit if request.system is None else request.system,
            )
        except anthropic.APIStatusError as error:
            failure = _Failure(error.status_code, error.response.headers, error.body)
        except anthropic.APIConnectionError:  # `APITimeoutError` too: the backstop fired
            failure = _Failure()
        except (anthropic.AnthropicError, ValueError, RecursionError):
            # A body the SDK could not decode. ValueError: `json.JSONDecodeError` (which keeps the
            # whole body in `.doc`) or `UnicodeDecodeError`. RecursionError: the JSON decoder on a
            # deeply nested body. None of them may escape.
            failure = _Failure(unreadable=True)
        else:
            return _response(message)
        # Outside every `except` block: nothing is chained (`__context__` and `__cause__` are None).
        raise _gateway_error(failure)

    async def aclose(self) -> None:
        """Close the HTTP client and its connections."""
        await self._client.close()


def _gateway_error(failure: _Failure) -> GatewayError:
    if failure.unreadable:
        return ProviderError()
    if failure.status is None:
        return Transient()
    return _status_error(failure.status, failure.headers, failure.body)


def _status_error(status: int, headers: Mapping[str, str], body: object) -> GatewayError:
    """ADR 0006's error table for an HTTP error response. First match wins."""
    request_id = _request_id(headers.get("request-id"))
    status_code = status if 100 <= status <= 599 else None  # the SDK passes any status through
    # (1) First, so a spend limit is never retried, whatever `x-should-retry` says.
    if _is_spend_limit(status, body):
        return SpendLimitReached(status_code=status_code, request_id=request_id)
    # (2) What prepaid-credit exhaustion returns is unverified: shesha records it (plan, PR 9).
    if status == 402:
        return BillingError(status_code=status_code, request_id=request_id)
    # `x-should-retry` counts only as exactly "true" or "false" (as in the SDK's `_should_retry`),
    # and only for `Transient` and `ProviderError` (ADR 0006).
    should_retry = {"true": True, "false": False}.get(headers.get("x-should-retry", ""))
    # (3) Connection errors and timeouts never get here: see `_gateway_error`.
    if status in (408, 409, 429) or 500 <= status <= 599:
        return Transient(
            status_code=status_code,
            request_id=request_id,
            retry_after=_retry_after(headers.get("retry-after")),
            retryable=should_retry is not False,
        )
    # (4)
    if status in (401, 403):
        return AuthenticationFailed(status_code=status_code, request_id=request_id)
    # (5)
    if status in (400, 404, 413, 422):
        return InvalidRequest(status_code=status_code, request_id=request_id)
    # (6)
    return ProviderError(
        status_code=status_code, request_id=request_id, retryable=should_retry is True
    )


def _is_spend_limit(status: int, body: object) -> bool:
    """A 400 whose message is "You have reached your specified [workspace] API usage limits…", or
    a 429 whose `error.details.error_code` is `enforced_spend_limit_reached`."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    if status == 400:
        message = error.get("message")
        return isinstance(message, str) and _SPEND_LIMIT_MESSAGE.match(message) is not None
    if status == 429:
        details = error.get("details")
        return isinstance(details, dict) and details.get("error_code") == _SPEND_LIMIT_CODE
    return False


def _retry_after(value: str | None) -> float | None:
    """The Retry-After header in seconds (/api/rate-limits: "the number of seconds to wait").

    None if it is absent, or not a finite number >= 0 (an HTTP date included: the API sends
    seconds).
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _request_id(value: object) -> str | None:
    """The provider's request id if it passes the opaque-id rule, else None (never echoed).

    For errors: a malformed request id is dropped, never allowed to change the error's class (a
    spend limit must stay `SpendLimitReached`). In a successful response it is validated like every
    other value, so there it makes the response a `ProviderError` (`_response`).
    """
    if not isinstance(value, str):
        return None
    try:
        return check_opaque_id(value, what="request_id")
    except ValueError:
        return None


def _block(block: object) -> dict[str, object]:
    # Dispatch on `type`, not on the SDK class: an unknown block arrives as a `TextBlock`. An
    # unknown or non-string type is passed on as given and fails validation (unknown union tag).
    kind = getattr(block, "type", None)
    names = _BLOCK_FIELDS.get(kind, ()) if isinstance(kind, str) else ()
    return {"type": kind, **{name: getattr(block, name, None) for name in names}}


def _response(message: Message) -> ProviderResponse:
    """Validate the SDK's message into a `ProviderResponse`, or raise a `ProviderError`.

    Known limit: a 200 the SDK could not read as an object (a plain-text or non-object JSON body,
    or broken JSON in `send`) gives a `ProviderError` without its `request-id`, which the SDK does
    not keep for those. Rare; revisit if PR 6 or the live run needs it.
    """
    raw_request_id = getattr(message, "_request_id", None)
    try:
        usage = message.usage
        # Absent optional counts are 0; any other value goes through the strict `Count` check.
        total = usage.cache_creation_input_tokens
        cache = usage.cache_creation
        if cache is None:
            # Without the per-TTL split, the total counts as 5-minute writes (the API's default
            # TTL). M1 sets no `cache_control`, so cache writes should stay 0 until M3.
            write_5m, write_1h = (0 if total is None else total), 0
        else:
            write_5m, write_1h = cache.ephemeral_5m_input_tokens, cache.ephemeral_1h_input_tokens
            if total is not None and total != write_5m + write_1h:
                # The split does not explain the total (an unknown TTL class, say): billed
                # tokens would go uncounted, so the response is malformed.
                raise ValueError("cache-write total does not match its split")
        cache_read = usage.cache_read_input_tokens
        stop_reason = message.stop_reason
        response: ProviderResponse | None = ProviderResponse.model_validate(
            {
                "content": [_block(block) for block in message.content],
                # The closed set; anything else (None included) is recorded as `other`.
                "stop_reason": (
                    stop_reason
                    if isinstance(stop_reason, str) and stop_reason in STOP_REASONS
                    else "other"
                ),
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": 0 if cache_read is None else cache_read,
                    "cache_write_5m_tokens": write_5m,
                    "cache_write_1h_tokens": write_1h,
                },
                "model": message.model,
                "request_id": raw_request_id,
                "inference_geo": usage.inference_geo,
            }
        )
    except (AttributeError, TypeError, ValueError):
        # A malformed response: a missing object (AttributeError), an odd shape (TypeError) or a
        # value our types refuse (pydantic's ValidationError is a ValueError). It may hold the
        # value, so it is dropped here and the ProviderError below is raised outside this block.
        response = None
    if response is None:
        raise ProviderError(request_id=_request_id(raw_request_id))
    return response
