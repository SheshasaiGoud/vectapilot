"""Typed values of the LLM gateway (ADR 0006, ADR 0007).

Every model here is a frozen Pydantic v2 model (`GatewayModel`) that rejects unknown fields and is
validated on every path that builds or changes one (`model_copy(update=...)` included). What stays
out of sight, and what does not:

- **Hidden:** field *values* in `str()` and `repr()` of a `ValidationError`
  (`hide_input_in_errors`); fields that can hold prompt or completion text in the model's own
  `repr()` and `str()` (`Field(repr=False)`); the new value in a "frozen" error.
- **Not hidden:** an unknown *key* (it is the error's location) and an unknown union tag (it is in
  the message) still appear in `str(error)`, and `error.errors()` and `error.json()` include the
  input unless called with `include_input=False`. So a `ValidationError` from these models is never
  logged, chained or turned into a string: the gateway raises a typed `GatewayError` outside the
  `except` block instead (the same rule as for SDK exceptions, PR 5).
- **Carry the text on purpose:** `model_dump()`, `model_dump_json()`, `vars()` and `pickle`. They
  exist for the provider call only. The one gateway value that is ever logged is `CallRecord`, as
  `model_dump(mode="json")`.
- `hide_input_in_errors` is a model setting, not part of a type: the `Annotated` types below
  (`BusinessId`, `OpaqueId`, …) echo their input when validated on their own. Outside a
  `GatewayModel`, use `TypeAdapter(T, config=ConfigDict(hide_input_in_errors=True))`.

No provider SDK is imported and no model is named here: model ids live only in the catalog
(`models.toml`, PR 3) and reach these types as plain strings at runtime.
"""

import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictBool,
    model_validator,
)


class GatewayModel(BaseModel):
    """Base of every gateway value: immutable, closed, and validated however it is built.

    `model_copy(update=...)` validates the result, so a copy obeys the same rules as the original.
    `model_construct()` and the deprecated `copy()` are refused: both skip validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        """Return a copy; with `update`, a validated one (Pydantic's own skips validation)."""
        if not update:
            return super().model_copy(deep=deep)
        return self.model_validate(self.model_dump() | dict(update))

    def copy(self, *args: Any, **kwargs: Any) -> Self:
        """Refused: Pydantic's deprecated `copy(update=...)` skips validation."""
        raise TypeError(f"{type(self).__name__}.copy() skips validation; use model_copy()")

    # Hidden from mypy (checked with mypy 2026-10-14): the pydantic plugin types `model_construct`
    # per model, so an override clashes; and a `__setattr__` that mypy can see would make it accept
    # `x.typo = …` on any model. At runtime both are as written here.
    if not TYPE_CHECKING:

        @classmethod
        def model_construct(cls, _fields_set=None, **values):
            """Refused: it builds a model without validation."""
            raise TypeError(
                f"{cls.__name__}.model_construct() skips validation; use model_validate()"
            )

        # Pydantic's frozen error carries the new value (`input_value=…`) into its message, and so
        # into a logged traceback; `hide_input_in_errors` does not cover it. Name the field only.
        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError(f"{type(self).__name__} is frozen: cannot set {name!r}")

        def __delattr__(self, name: str) -> None:
            raise AttributeError(f"{type(self).__name__} is frozen: cannot delete {name!r}")


# ── Identifiers ──────────────────────────────────────────────────────────────────────────────

SLUG_PATTERN: Final = r"[a-z0-9]+(?:-[a-z0-9]+)*"
"""Lowercase letters and digits in groups joined by single hyphens; checked with `re.fullmatch`."""

_SLUG_RE: Final = re.compile(SLUG_PATTERN)
_TRACE_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
_OPAQUE_ID_RE: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def _check_slug(value: str) -> str:
    # `fullmatch`, not `match` with `$`: `$` also matches just before a trailing newline.
    # Messages never include the value: it may be caller input.
    if _SLUG_RE.fullmatch(value) is None:
        raise ValueError("must be a lowercase slug: a-z, 0-9 and single inner hyphens")
    return value


def _check_trace_id(value: str) -> str:
    if _TRACE_ID_RE.fullmatch(value) is None:
        raise ValueError("trace_id must be 32 lowercase hex characters (W3C Trace Context)")
    if value == "0" * 32:
        raise ValueError("trace_id must not be all zeros (W3C Trace Context)")
    return value


def _looks_like_a_secret(value: str) -> bool:
    # An Anthropic API key starts with `sk-ant-`, other vendors' keys often with `sk-`; a JWT
    # starts with `eyJ` (base64 of `{"`). Compared in lower case.
    lowered = value.lower()
    return lowered.startswith(("sk-", "eyj")) or "sk-ant-" in lowered


def check_opaque_id(value: str, *, what: str = "id") -> str:
    """Accept an opaque id (request id, model id, catalog version, inference geo), never text.

    1-128 characters from `A-Z a-z 0-9 . _ : -`: one line, no spaces, bounded length. That limits
    how much a slip can leak; it does not prove the value is harmless (personal data shaped like an
    id still fits). Secret-shaped values (`sk-…`, `…sk-ant-…`, `eyJ…`) are refused as well. The one
    rule for every `OpaqueId` field and for `errors.GatewayError.request_id`; `what` names the field
    in the message, which never contains the value.
    """
    if _OPAQUE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{what} must be 1-128 characters from A-Z a-z 0-9 . _ : -")
    if _looks_like_a_secret(value):
        raise ValueError(f"{what} must not look like an API key or a token")
    return value


StopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
    "other",
]
"""Why the model stopped: a closed set, plus `other` for any value the provider adds later.

The provider's values, checked 2026-10-14 on https://platform.claude.com/docs/en/api/messages
(`stop_reason`) and https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons:
`end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `pause_turn`, `refusal`,
`model_context_window_exceeded`. `other` is ours: the adapter (PR 5) maps any value outside that
list to it, so an unknown value is never stored as given. Closed, like `CallOutcome`.
"""

STOP_REASONS: Final[frozenset[str]] = frozenset(get_args(StopReason))
"""`LLMResult` and `CallRecord` validate the same `Literal` (`LLMResult` then refuses the error
stop reasons), so the set is defined once."""

TruncationStopReason = Literal["max_tokens", "model_context_window_exceeded"]
"""The output was cut off: the only values `errors.OutputTruncated` takes. A subset of `StopReason`.

`max_tokens` is ADR 0006. `model_context_window_exceeded` is proposed (Diego, 2026-10-14), awaiting
shesha: the stop-reasons page says of it "Treat the response as truncated" (checked 2026-10-14).
If shesha reverses it, remove it here and from its tests.
"""

TRUNCATION_STOP_REASONS: Final[frozenset[str]] = frozenset(get_args(TruncationStopReason))


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""How much the model may think and write (`output_config.effort`), set per alias in the catalog.

The provider's levels, checked 2026-10-14 on
https://platform.claude.com/docs/en/build-with-claude/effort. Which levels a model supports, and
its default, differ per model; ADR 0006 sends `low` on both aliases, always.
"""


BusinessId = Annotated[str, Field(min_length=3, max_length=40), AfterValidator(_check_slug)]
"""The business a call belongs to: a lowercase slug, 3-40 characters (business-context §3.1)."""

Slug = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_check_slug)]
"""A model alias (`small`, `large`) or an operation name (`rag-answer`): a slug, 1-64 characters."""

TraceId = Annotated[str, AfterValidator(_check_trace_id)]
"""A W3C Trace Context trace id: 32 lowercase hex characters, not all zeros."""

OpaqueId = Annotated[str, AfterValidator(check_opaque_id)]
"""A model id, catalog version, inference geo or request id, as `check_opaque_id` accepts it.

These come from the catalog or the provider's response and end up in the `llm_call` record
(ADR 0007), so they must be shaped like ids: single-line and bounded.
"""

RequestId = OpaqueId
"""A provider request id (opaque, for support tickets and the Console): the `OpaqueId` rule."""


# ── Tokens and prices ────────────────────────────────────────────────────────────────────────

Count = Annotated[int, Field(ge=0, strict=True)]
"""A non-negative int (tokens, retries). Strict: `True`, `1.0` and `"1"` are rejected."""


class Usage(GatewayModel):
    """Tokens billed for one model response, split into the five classes that are priced apart.

    `input_tokens` is only the uncached part of the prompt: the prompt's total is input + cache
    reads + both cache writes. A class that did not occur is 0.

    Exactly the five billed classes, nothing else, so H1 prices tokens and only tokens. The
    provider's `usage.inference_geo` is not part of `Usage`: it travels next to it in the
    per-attempt provider response (`providers.base.ProviderResponse`, shared by the fake provider
    and the adapter) and ends up in `CallRecord.inference_geo`.
    """

    input_tokens: Count = 0
    output_tokens: Count = 0
    """Everything the model generated, thinking included."""
    cache_read_tokens: Count = 0
    cache_write_5m_tokens: Count = 0
    cache_write_1h_tokens: Count = 0


def _reject_float(value: object) -> object:
    # Pydantic would turn the float 0.1 into Decimal("0.1") without a word; a float price means
    # the catalog was read without `parse_float=Decimal`, which must fail loudly instead.
    if isinstance(value, float | bool):
        raise ValueError("money must be a Decimal, an int or a decimal string, never a float")
    return value


def _usd_to_json(value: Decimal) -> str:
    # Fixed-point, never scientific: Decimal("3E-7") is written "0.0000003", and -0 as "0", so cost
    # lines in the logs compare and grep as text. The value itself stays exact.
    return "0" if value.is_zero() else format(value, "f")


Usd = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(ge=0),
    PlainSerializer(_usd_to_json, return_type=str, when_used="json"),
]
"""A non-negative, finite amount of US dollars. Never a float; written to JSON as fixed-point."""


class TokenPrices(GatewayModel):
    """List prices in USD per million tokens (MTok), one per token class."""

    input: Usd
    output: Usd
    cache_read: Usd
    cache_write_5m: Usd
    cache_write_1h: Usd


class LongPromptTier(GatewayModel):
    """Higher prices for a request whose prompt is over `threshold_tokens` tokens.

    The exact rule (which tokens count, and that "over" is strictly greater) is quoted from the
    pricing page in `pricing.cost_usd`. `prices` is a full set: for a class the page does not raise,
    the catalog repeats the standard price.
    """

    threshold_tokens: int = Field(gt=0, strict=True)
    prices: TokenPrices


class ModelPricing(GatewayModel):
    """The prices of one model id. `long_prompt` is None for a model with a single tier."""

    standard: TokenPrices
    long_prompt: LongPromptTier | None = None


# ── Retries ──────────────────────────────────────────────────────────────────────────────────


def _reject_bool(value: object) -> object:
    # `True` would quietly become 1.0 s. Not `strict=True`: settings (PR 3) arrive as strings.
    if isinstance(value, bool):
        raise ValueError("must be a number of seconds, not a bool")
    return value


Seconds = Annotated[float, BeforeValidator(_reject_bool), Field(gt=0, allow_inf_nan=False)]
"""A positive, finite number of seconds. An infinite timeout would be no timeout."""


class RetryPolicy(GatewayModel):
    """How often and how long H2 (`retry.call_with_retries`) may retry.

    Values come from settings (PR 3); ADR 0006's starting values are 2 retries and a backoff of
    0.5-8 s — safety bounds, not latency targets.
    """

    max_retries: int = Field(ge=0, le=10, strict=True)
    """Retries after the first attempt, so at most `1 + max_retries` calls.

    The upper bound of 10 is a sanity cap (decided 2026-10-14; not in ADR 0006).
    """
    base_delay_s: Seconds
    """The backoff bound before the first retry; it doubles for each further retry."""
    max_delay_s: Seconds
    """The cap: no backoff wait is longer, and a longer Retry-After is not waited for."""

    @model_validator(mode="after")
    def _cap_not_below_base(self) -> Self:
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be >= base_delay_s")
        return self


# ── Requests and results ─────────────────────────────────────────────────────────────────────


class Message(GatewayModel):
    """One conversation turn. The system prompt is `LLMRequest.system`, not a message."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, repr=False)


class LLMRequest(GatewayModel):
    """One logical call to a model, as callers build it.

    There is no `temperature` field: it is never sent (ADR 0006), so passing one is an error.
    """

    business_id: BusinessId
    alias: Slug
    """A catalog alias (`small`, `large`); never a model id."""
    messages: tuple[Message, ...] = Field(min_length=1)
    system: Annotated[str, Field(min_length=1)] | None = Field(default=None, repr=False)
    max_tokens: Annotated[int, Field(gt=0, strict=True)] | None = None
    """Output cap for this call; None means the alias's `max_output_tokens` from the catalog.

    A value above that maximum is rejected with a typed error before any call, never clamped
    (decided 2026-10-14). This type cannot see the catalog, so the gateway enforces the rule
    (PR 6), and PR 8's `--live` cap reserves the effective value.
    """
    trace_id: TraceId | None = None
    operation: Slug | None = None
    """The caller's step name (for example `rag-answer`), for M9's per-step view."""


class TextBlock(GatewayModel):
    type: Literal["text"] = "text"
    text: str = Field(repr=False)


class ThinkingBlock(GatewayModel):
    """Kept unchanged so it can be replayed in a later turn (signature included)."""

    type: Literal["thinking"] = "thinking"
    thinking: str = Field(repr=False)
    signature: str = Field(repr=False)


class RedactedThinkingBlock(GatewayModel):
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str = Field(repr=False)


ContentBlock = Annotated[
    TextBlock | ThinkingBlock | RedactedThinkingBlock, Field(discriminator="type")
]

_ERROR_STOP_REASONS: Final = frozenset({"refusal"}) | TRUNCATION_STOP_REASONS
"""Stop reasons that become typed errors (`ModelRefused`, `OutputTruncated`), never a result."""


class LLMResult(GatewayModel):
    """A successful call: the content blocks as returned, plus usage and cost (ADR 0006, Cost).

    Refusals and truncations never become an `LLMResult`; they are typed errors (`errors.py`), and
    building a result with one of their stop reasons fails.
    """

    content: tuple[ContentBlock, ...]
    stop_reason: StopReason
    usage: Usage
    model_answered: OpaqueId
    """The model id that actually answered; cost is priced by it, not by the alias."""
    cost_usd: Usd
    """Every billed attempt, repairs included; a lower bound when `cost_complete` is False."""
    cost_complete: StrictBool

    @model_validator(mode="after")
    def _not_a_refusal_or_truncation(self) -> Self:
        if self.stop_reason in _ERROR_STOP_REASONS:
            raise ValueError("a refusal or a truncation is a typed error, never an LLMResult")
        return self

    @property
    def text(self) -> str:
        """The text blocks joined in order; thinking blocks are left out."""
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))


# ── The `llm_call` record (ADR 0007) ─────────────────────────────────────────────────────────


class CallOutcome(StrEnum):
    """How a logical call ended. Closed: a new value is a contract change (`record_version`)."""

    OK = "ok"
    REFUSAL = "refusal"
    TRUNCATED = "truncated"
    VALIDATION_FAILED = "validation_failed"
    TRANSIENT_EXHAUSTED = "transient_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CANCELLED = "cancelled"
    SPEND_LIMIT = "spend_limit"
    BILLING = "billing"
    AUTH_FAILED = "auth_failed"
    INVALID_REQUEST = "invalid_request"
    PROVIDER_ERROR = "provider_error"


class CallRecord(GatewayModel):
    """One `llm_call` record per logical call, emitted once — metadata only, never text.

    Fields follow docs/plans/m1-pr-checks.md, PR 6. New fields are additive; a breaking change
    bumps `record_version`. The token fields have the same names as `Usage`'s.

    Build it only from values that were already validated at the adapter boundary (PR 5): the
    record is built in `finally`, and a validation error there must never replace the call's real
    result or exception (PR 6).
    """

    # Version 1 is the first release of the contract: `attempt_timeouts` was added (2026-10-14)
    # before any record was emitted, so it did not need a bump.
    record_version: Literal[1] = 1
    business_id: BusinessId
    alias: Slug
    model_requested: OpaqueId
    model_answered: OpaqueId | None = None
    """None when no attempt got an answer (for example the first call hit the spend limit)."""
    catalog_version: OpaqueId
    inference_geo: OpaqueId | None = None
    """As reported in the response's usage (`global`, `us`); None when there was no response."""
    outcome: CallOutcome
    stop_reason: StopReason | None = None
    """None when there was no response; an unknown provider value is recorded as `other` (PR 5)."""
    retries: Count
    """Retried attempts over the whole call, a repair's retries included.

    So a call that reached the provider made `1 + repairs + retries` attempts (decided 2026-10-14).
    Calls with no model call (a cache hit, a pre-call `max_tokens` rejection): open, PR 6/7.
    """
    repairs: Count
    attempt_timeouts: Count
    """Attempts that hit the per-attempt timeout (not the call's deadline).

    At most the attempts; for an `ok` call at most `repairs + retries` (its last attempt answered);
    0 on a cache hit. PR 8's timeout rate counts a call whose `attempt_timeouts` > 0 or whose
    outcome is `deadline_exceeded`.
    """
    first_try_valid: StrictBool | None = None
    """None when the call was not structured."""
    input_tokens: Count
    output_tokens: Count
    cache_read_tokens: Count
    cache_write_5m_tokens: Count
    cache_write_1h_tokens: Count
    cost_usd: Usd
    """Every billed attempt, repairs included; a lower bound when `cost_complete` is False."""
    cost_complete: StrictBool
    cache_hit: StrictBool
    latency_ms: float = Field(ge=0, allow_inf_nan=False, strict=True)
    """Monotonic clock, the whole call: backoff waits and repair included."""
    request_ids: tuple[RequestId, ...] = ()
    """One per attempt that got a provider request id, in order."""
    trace_id: TraceId | None = None
    operation: Slug | None = None

    @model_validator(mode="after")
    def _cost_rules(self) -> Self:
        # ADR 0006, Caching: a response-cache hit makes no model call, so it costs $0.
        if self.cache_hit and not self.cost_usd.is_zero():
            raise ValueError("a cache hit costs 0 (ADR 0006)")
        # ADR 0006, Cost: list prices assume global routing, so any other geo is incomplete.
        if self.inference_geo not in (None, "global") and self.cost_complete:
            raise ValueError("cost_complete must be False when inference_geo is not global")
        return self

    @model_validator(mode="after")
    def _timeouts_within_attempts(self) -> Self:
        if self.attempt_timeouts > 1 + self.repairs + self.retries:
            raise ValueError("attempt_timeouts must not exceed the attempts, 1 + repairs + retries")
        # An `ok` call's last attempt got the answer, so that attempt did not time out.
        if self.outcome == CallOutcome.OK and self.attempt_timeouts > self.repairs + self.retries:
            raise ValueError("attempt_timeouts must be <= repairs + retries for an ok call")
        # ADR 0006, Caching: a response-cache hit makes no model call, so nothing can time out.
        if self.cache_hit and self.attempt_timeouts > 0:
            raise ValueError("a cache hit makes no model call: attempt_timeouts must be 0")
        return self
