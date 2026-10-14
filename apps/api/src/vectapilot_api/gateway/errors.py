"""Typed errors of the LLM gateway (ADR 0006, error table).

The provider adapter (PR 5) maps a response to one of these classes, first match wins:
spend limit → `SpendLimitReached`; 402 → `BillingError`; connection errors, timeouts, 408, 409,
429, 5xx, 529 → `Transient`; 401/403 → `AuthenticationFailed`; 400/404/413/422 →
`InvalidRequest`; anything else → `ProviderError`. The gateway adds `DeadlineExceeded`,
`ModelRefused` and `OutputTruncated`.

**Metadata only.** No constructor takes a free-text message: `str(error)` is built from the typed
fields below, and the request id must pass the opaque-id rule (`types.check_opaque_id`). So an
error's *own* message carries no prompt, completion, header or key text. A traceback also prints
any exception that was being handled when this one was raised (`__context__`), so these errors are
raised *outside* the `except` block (PR 5: `__context__ is None`); `test_errors.py` shows why.

Each class carries its `outcome` (the `CallRecord` outcome it maps to), an optional HTTP status,
an optional provider request id and `retryable`. Only `Transient` (default yes) and
`ProviderError` (default no) let the adapter set `retryable` — from the `x-should-retry` header.
Every field is a read-only property, set once by the constructor.

Known limit: `DeadlineExceeded` and `OutputTruncated` cannot be unpickled (`TypeError`: their
constructors have a required argument). M9's Redis queue must send an error's metadata, not the
exception object.
"""

import math
from typing import ClassVar

from vectapilot_api.gateway.types import (
    TRUNCATION_STOP_REASONS,
    CallOutcome,
    TruncationStopReason,
    check_opaque_id,
)


class GatewayError(Exception):
    """Base of every gateway error. Catch this to handle any of them."""

    outcome: ClassVar[CallOutcome] = CallOutcome.PROVIDER_ERROR

    def __init__(self, *, status_code: int | None = None, request_id: str | None = None) -> None:
        if status_code is not None and not 100 <= status_code <= 599:
            raise ValueError("status_code must be an HTTP status between 100 and 599")
        if request_id is not None:
            check_opaque_id(request_id, what="request_id")
        super().__init__()
        self._status_code = status_code
        self._request_id = request_id
        self._retryable = False

    @property
    def status_code(self) -> int | None:
        """The provider's HTTP status, if there was a response. Read-only."""
        return self._status_code

    @property
    def request_id(self) -> str | None:
        """The provider's request id (for support tickets and the Console). Read-only."""
        return self._request_id

    @property
    def retryable(self) -> bool:
        """True if retrying the same request may succeed. Read-only."""
        return self._retryable

    def _metadata(self) -> dict[str, object]:
        return {
            "status_code": self.status_code,
            "request_id": self.request_id,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:
        return ", ".join(f"{name}={value!r}" for name, value in self._metadata().items())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self})"


class SpendLimitReached(GatewayError):
    """A spend limit was reached (the documented 400 or 429 shapes). Never retried."""

    outcome = CallOutcome.SPEND_LIMIT


class BillingError(GatewayError):
    """402 `billing_error`: a payment or credit problem. Never retried."""

    outcome = CallOutcome.BILLING


class Transient(GatewayError):
    """A failure that may pass: connection error, timeout, 408, 409, 429, 5xx, 529.

    `retry_after` is the server's Retry-After in seconds, if it sent one. A `Transient` that
    escapes the gateway means a transient failure ended the call: the retries were used up, or none
    was allowed (`retryable` False, a Retry-After above the cap, or no time left before the
    deadline). Hence its outcome, `transient_exhausted`.
    """

    outcome = CallOutcome.TRANSIENT_EXHAUSTED

    def __init__(
        self,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        retryable: bool = True,
    ) -> None:
        if retry_after is not None and not (math.isfinite(retry_after) and retry_after >= 0):
            raise ValueError("retry_after must be a finite number of seconds >= 0")
        super().__init__(status_code=status_code, request_id=request_id)
        self._retry_after = retry_after
        self._retryable = retryable

    @property
    def retry_after(self) -> float | None:
        """The server's Retry-After, in seconds; None if it sent none. Read-only."""
        return self._retry_after

    def _metadata(self) -> dict[str, object]:
        return {**super()._metadata(), "retry_after": self.retry_after}


class AuthenticationFailed(GatewayError):
    """401/403: the key is missing, wrong or not allowed. Never retried."""

    outcome = CallOutcome.AUTH_FAILED


class InvalidRequest(GatewayError):
    """400/404/413/422: the same request would fail the same way. Never retried."""

    outcome = CallOutcome.INVALID_REQUEST


class ProviderError(GatewayError):
    """Any other provider failure. Not retried unless `x-should-retry: true` said so."""

    outcome = CallOutcome.PROVIDER_ERROR

    def __init__(
        self,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(status_code=status_code, request_id=request_id)
        self._retryable = retryable


class DeadlineExceeded(GatewayError):
    """The whole call (attempts, waits and repair) ran past its deadline of `deadline_s`."""

    outcome = CallOutcome.DEADLINE_EXCEEDED

    def __init__(
        self,
        *,
        deadline_s: float,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        if not (math.isfinite(deadline_s) and deadline_s > 0):
            raise ValueError("deadline_s must be a finite number of seconds > 0")
        super().__init__(status_code=status_code, request_id=request_id)
        self._deadline_s = deadline_s

    @property
    def deadline_s(self) -> float:
        """The deadline that ran out, in seconds. Read-only."""
        return self._deadline_s

    def _metadata(self) -> dict[str, object]:
        return {**super()._metadata(), "deadline_s": self.deadline_s}


class ModelRefused(GatewayError):
    """The model declined to answer (stop reason `refusal`). Never retried or repaired."""

    outcome = CallOutcome.REFUSAL


class OutputTruncated(GatewayError):
    """The output was cut off (stop reason `max_tokens` or `model_context_window_exceeded`).

    Never repaired. `stop_reason` accepts only `types.TruncationStopReason`, the values `LLMResult`
    refuses as truncations, so a truncation is always this error and never a result.
    """

    outcome = CallOutcome.TRUNCATED

    def __init__(
        self,
        *,
        stop_reason: TruncationStopReason,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        if stop_reason not in TRUNCATION_STOP_REASONS:
            # The message lists the allowed values only, never the given one.
            allowed = ", ".join(sorted(TRUNCATION_STOP_REASONS))
            raise ValueError(f"stop_reason must be a truncation stop reason: {allowed}")
        super().__init__(status_code=status_code, request_id=request_id)
        self._stop_reason = stop_reason

    @property
    def stop_reason(self) -> TruncationStopReason:
        """The provider's stop reason that cut the output off. Read-only."""
        return self._stop_reason

    def _metadata(self) -> dict[str, object]:
        return {**super()._metadata(), "stop_reason": self.stop_reason}
