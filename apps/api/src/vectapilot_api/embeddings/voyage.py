"""The Voyage AI adapter (ADR 0008): plain REST over `httpx2`, no SDK. The only module that knows
the Voyage URL.

One `embed` call plans its batches first (`base.plan_batches`: at most the model's texts and tokens
per request, a text over the context refused before anything is sent), then sends one request per
batch, in order, one attempt each: **no retries here** (PR 5 may wrap a batch in H2's
`call_with_retries`; every error below carries `retryable` and `retry_after` for it). What this
module guarantees, as the Anthropic adapter does (ADR 0006/0007):

- **Client.** The key comes only from the caller (Settings: process environment only), in the
  `Authorization` header. Redirects are never followed, so the key and the texts never go to
  another host. A timeout bounds each request twice: httpx2's per-phase timeout, and an
  `asyncio.timeout` over the whole request (a slow-drip body included).
- **Request.** `input`, `model`, `input_type`, `truncation: false`, `output_dimension`,
  `output_dtype: "float"`. Nothing else.
- **Errors.** Typed and metadata only (`base.py`), first match wins: 401/403 → AuthenticationFailed;
  429 → RateLimited (Retry-After in seconds); 413 → InputTooLarge; 400/404/422 → InvalidRequest;
  408/409/5xx, connection errors and timeouts → Transient; anything else (a redirect included) →
  ProviderError. Only plain data leaves an `except` block; the error is raised after it, so no
  httpx2 exception (its request holds the key header) is ever chained.
- **Response.** Exactly one vector per input, `index` values 0..n-1, each `dimension` finite
  floats of unit length, one model id for the whole call (the id the provider returned is the one
  stored per chunk), a token count. Any mismatch is a ProviderError that echoes nothing.
- **Logging.** This module logs nothing, and pins the HTTP clients' loggers to WARNING.

Unverified until the first live call (docs/plans/m2-pr-checks.md, PR 4): the error statuses Voyage
really sends, a request-id header (none is read), and the token bound (`base.py`).
"""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Final

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from vectapilot_api.embeddings.base import (
    Embeddings,
    EmbeddingSpec,
    InputTooLarge,
    InputType,
    RateLimited,
    check_business_id,
    check_vectors,
    plan_batches,
)
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    GatewayError,
    InvalidRequest,
    ProviderError,
    Transient,
)
from vectapilot_api.gateway.types import BusinessId, Count, OpaqueId

PINNED_LOGGERS: Final = ("httpx2", "httpcore2", "httpx", "httpcore")
"""HTTP client loggers that print URLs and connection traces at DEBUG/INFO (ADR 0007)."""
for _name in PINNED_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

VOYAGE_API_URL: Final = "https://api.voyageai.com"
EMBEDDINGS_PATH: Final = "/v1/embeddings"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class _Item(_Wire):
    index: int = Field(ge=0, strict=True)
    embedding: list[Annotated[float, Field(strict=True, allow_inf_nan=False)]]


class _Usage(_Wire):
    total_tokens: Count


class _Body(_Wire):
    data: list[_Item]
    model: OpaqueId
    usage: _Usage


@dataclass(frozen=True)
class _Reply:
    """One checked response (a dataclass: the vectors were validated once, in `_reply`)."""

    model: str
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    total_tokens: int


class VoyageEmbedder:
    """The `Embedder` for Voyage AI. Build one per process; `aclose` at shutdown.

    `transport` is for tests (an `httpx2.MockTransport`, so no test reaches the network); in
    production it stays None and httpx2 builds its own. Proxies from the environment keep working
    (as for Anthropic); Settings refuses the TLS variables with this provider.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr,
        spec: EmbeddingSpec,
        timeout_s: float,
        base_url: str = VOYAGE_API_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._spec = spec
        self._timeout_s = timeout_s
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            headers={"authorization": f"Bearer {api_key.get_secret_value()}"},
            timeout=timeout_s,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def model(self) -> str:
        return self._spec.model

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: BusinessId
    ) -> Embeddings:
        business = check_business_id(business_id)
        batches = plan_batches(texts, self._spec)
        replies = [
            await self._send(list(texts[batch.start : batch.stop]), input_type) for batch in batches
        ]
        models = {reply.model for reply in replies}
        if len(models) != 1:
            raise ProviderError()  # one call, one model id: never mixed vectors
        (model,) = models
        return Embeddings(
            business_id=business,
            input_type=input_type,
            model=model,
            dimension=self._spec.dimension,
            vectors=tuple(vector for reply in replies for vector in reply.vectors),
            total_tokens=sum(reply.total_tokens for reply in replies),
            requests=len(batches),
        )

    async def _send(self, batch: list[str], input_type: InputType) -> _Reply:
        """One request, one attempt."""
        try:
            async with asyncio.timeout(self._timeout_s):
                response: httpx2.Response | None = await self._client.post(
                    EMBEDDINGS_PATH,
                    json={
                        "input": batch,
                        "model": self._spec.model,
                        "input_type": input_type,
                        "truncation": False,
                        "output_dimension": self._spec.dimension,
                        "output_dtype": "float",
                    },
                )
            unreadable = False
        except (TimeoutError, httpx2.TransportError):  # connection errors and timeouts
            response, unreadable = None, False
        except httpx2.HTTPError:  # a body httpx2 could not decode (DecodingError)
            response, unreadable = None, True
        # Outside every `except` block: nothing is chained (`__context__` and `__cause__` are None).
        if response is None:
            raise ProviderError() if unreadable else Transient()
        if response.status_code != 200:
            raise _status_error(response.status_code, response.headers)
        return _reply(response.content, count=len(batch), dimension=self._spec.dimension)

    async def aclose(self) -> None:
        """Close the HTTP client and its connections."""
        await self._client.aclose()


def _status_error(status: int, headers: Mapping[str, str]) -> GatewayError:
    """The error for a non-200 response. First match wins (module doc)."""
    code = status if 100 <= status <= 599 else None
    if status in (401, 403):
        return AuthenticationFailed(status_code=code)
    if status == 429:
        return RateLimited(status_code=code, retry_after=_retry_after(headers.get("retry-after")))
    if status == 413:
        return InputTooLarge(status_code=code)
    if status in (400, 404, 422):
        return InvalidRequest(status_code=code)
    if status in (408, 409) or 500 <= status <= 599:
        return Transient(status_code=code)
    return ProviderError(status_code=code)


def _retry_after(value: str | None) -> float | None:
    """Retry-After in seconds; None if absent or not a finite number >= 0 (an HTTP date too)."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _reply(content: bytes, *, count: int, dimension: int) -> _Reply:
    """Validate a 200 body into vectors in input order, or raise a ProviderError."""
    try:
        body: _Body | None = _Body.model_validate_json(content)
    except (ValidationError, RecursionError):
        # The error may hold values from the body: dropped here, never logged or chained.
        body = None
    if body is None or len(body.data) != count:
        raise ProviderError()
    items = sorted(body.data, key=lambda item: item.index)
    if [item.index for item in items] != list(range(count)):
        raise ProviderError()  # a missing or repeated index
    vectors = check_vectors([item.embedding for item in items], count=count, dimension=dimension)
    return _Reply(model=body.model, vectors=vectors, total_tokens=body.usage.total_tokens)
