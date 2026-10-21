"""What every embedder shares (ADR 0008): the protocol, the result, batching and response checks.

Provider-neutral: no URL and no model id here (those live in `voyage.py` and `models.toml`).

**Errors** are the gateway's typed, metadata-only errors (`gateway/errors.py`: no free text, so no
document text or key can reach a message), plus two subclasses below. So `retryable` and
`retry_after` mean what they mean for H2's `call_with_retries`, which PR 5 can wrap around one
batch if it retries (this PR makes one attempt per batch and retries nothing).

**Token limits without a tokenizer.** We send `truncation=false` (an over-long text is an error,
never silently cut), so each request must stay under the model's limits. With no SDK and no
tokenizer, `token_upper_bound` counts UTF-8 bytes plus a small allowance per text. That is an upper
bound if every token covers at least one byte (true for byte-level and byte-fallback BPE
tokenizers); Voyage's tokenizer is not verified to be one. The API is the backstop: an over-limit
request is refused (`truncation=false`), never truncated. PR 9's live run compares the reported
`total_tokens` with this bound.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol, Self

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from vectapilot_api.gateway.errors import InvalidRequest, ProviderError, Transient
from vectapilot_api.gateway.types import BusinessId, GatewayModel, OpaqueId

STORED_DIMENSION: Final = 1024
"""`chunks.embedding` is `vector(1024)` (migration 0002). Another dimension needs a migration and a
full reindex; `tests/embeddings/test_catalog_and_factory.py` pins this against the migration."""

UNIT_NORM_TOLERANCE: Final = 1e-3
"""How far a vector's length may be from 1. Voyage normalises its vectors (ADR 0008, [faq]); float
rounding stays far below this. Cosine equals the dot product only for unit vectors."""

TOKEN_OVERHEAD_PER_TEXT: Final = 8
"""Allowance per text for special tokens a tokenizer may add (an assumption, see the module doc)."""

InputType = Literal["document", "query"]
"""Voyage's `input_type`: `document` for chunks, `query` for questions (ADR 0008)."""


class InputTooLarge(InvalidRequest):
    """A text is over the model's context, or the provider said the request is too large (413).

    Never retried and never truncated. Raised before any request when `token_upper_bound` says a
    text cannot fit: `index` is its position in the call, `limit` the model's context in tokens. For
    a 413, both are None. Metadata only: never the text.
    """

    def __init__(
        self, *, index: int | None = None, limit: int | None = None, status_code: int | None = None
    ) -> None:
        super().__init__(status_code=status_code)
        self._index = index
        self._limit = limit

    @property
    def index(self) -> int | None:
        """The position of the text that is too long, if it was found before sending. Read-only."""
        return self._index

    @property
    def limit(self) -> int | None:
        """The model's context limit in tokens, if it was found before sending. Read-only."""
        return self._limit

    def _metadata(self) -> dict[str, object]:
        return {**super()._metadata(), "index": self.index, "limit": self.limit}


class RateLimited(Transient):
    """HTTP 429. `retry_after` holds the server's Retry-After in seconds, if it sent one."""


class EmbeddingSpec(GatewayModel):
    """One model as an embedder uses it: the id to request, the dimension and the request limits.

    Built by `catalog.EmbeddingCatalog.resolve`; tests build small ones to exercise batching.
    """

    model: OpaqueId
    dimension: int = Field(gt=0, strict=True)
    max_texts_per_request: int = Field(gt=0, strict=True)
    max_tokens_per_request: int = Field(gt=0, strict=True)
    context_tokens: int = Field(gt=0, strict=True)
    """The most tokens one text may have."""

    @model_validator(mode="after")
    def _one_text_fits_one_request(self) -> Self:
        # So every text that passes the context check fits a request on its own.
        if self.context_tokens > self.max_tokens_per_request:
            raise ValueError("context_tokens must be <= max_tokens_per_request")
        return self


@dataclass(frozen=True)
class Embeddings:
    """The vectors for one call, in input order, and what a record of the call needs.

    `model` is the id the provider *returned*: PR 5 stores it (and `dimension`) on every chunk, and
    the guard compares it (`guard.py`). The vectors stay out of `repr`.
    """

    business_id: BusinessId
    input_type: InputType
    model: str
    dimension: int
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    total_tokens: int
    """Tokens the provider reported (summed over the requests); 0 for the fake."""
    requests: int
    """How many requests (batches) the call took."""


class Embedder(Protocol):
    """Turns texts into unit vectors. One instance per process; `aclose` at shutdown."""

    @property
    def model(self) -> str:
        """The model id this embedder requests (from the catalog)."""
        ...

    @property
    def dimension(self) -> int:
        """The vector length it returns."""
        ...

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: BusinessId
    ) -> Embeddings:
        """Embed `texts` (in order) for one business. Raises the typed errors above."""
        ...

    async def aclose(self) -> None:
        """Release connections."""
        ...


_BUSINESS_ID: Final = TypeAdapter[str](BusinessId)


def check_business_id(value: str) -> BusinessId:
    """The `BusinessId` rule, or `InvalidRequest` (a caller bug; the value is not echoed)."""
    try:
        checked: str | None = _BUSINESS_ID.validate_python(value)
    except ValidationError:
        checked = None
    if checked is None:
        raise InvalidRequest()
    return checked


def token_upper_bound(text: str) -> int:
    """An upper bound on the tokens of `text` (see the module doc for the assumption)."""
    return len(text.encode("utf-8", "surrogatepass")) + TOKEN_OVERHEAD_PER_TEXT


def plan_batches(texts: Sequence[str], spec: EmbeddingSpec) -> list[range]:
    """Split `texts` into consecutive batches that each fit one request.

    Each batch has at most `max_texts_per_request` texts and at most `max_tokens_per_request`
    tokens by `token_upper_bound`. Raises `InvalidRequest` for no texts, a bare `str` (it would be
    split into characters) or an empty text, and `InputTooLarge` for a text over `context_tokens`.
    Nothing is sent before the whole plan is known, so a bad text fails the call before any cost.
    """
    if isinstance(texts, str) or not texts:
        raise InvalidRequest()
    batches: list[range] = []
    start = tokens = 0
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text:
            raise InvalidRequest()
        bound = token_upper_bound(text)
        if bound > spec.context_tokens:
            raise InputTooLarge(index=index, limit=spec.context_tokens)
        full = index - start == spec.max_texts_per_request
        if full or tokens + bound > spec.max_tokens_per_request:
            batches.append(range(start, index))
            start, tokens = index, 0
        tokens += bound
    batches.append(range(start, len(texts)))
    return batches


def check_vectors(
    vectors: Sequence[Sequence[float]], *, count: int, dimension: int
) -> tuple[tuple[float, ...], ...]:
    """Exactly `count` vectors, each `dimension` finite numbers of unit length; else ProviderError.

    The error echoes nothing. Every embedder (the fake too) returns only what passed this check.
    """
    if len(vectors) != count:
        raise ProviderError()
    checked: list[tuple[float, ...]] = []
    for vector in vectors:
        if len(vector) != dimension:
            raise ProviderError()
        if not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vector):
            raise ProviderError()
        values = tuple(float(x) for x in vector)
        if not all(math.isfinite(x) for x in values):
            raise ProviderError()
        if abs(math.sqrt(math.sumprod(values, values)) - 1.0) > UNIT_NORM_TOLERANCE:
            raise ProviderError()
        checked.append(values)
    return tuple(checked)
