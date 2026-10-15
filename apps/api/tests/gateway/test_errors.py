"""Tests for the gateway's typed errors (`gateway/errors.py`). [DELEGATE]: these pass today."""

import traceback

import pytest

from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    DeadlineExceeded,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    OutputTruncated,
    ProviderError,
    SpendLimitReached,
    StructuredOutputInvalid,
    Transient,
)
from vectapilot_api.gateway.types import (
    STOP_REASONS,
    TRUNCATION_STOP_REASONS,
    CallOutcome,
    TruncationStopReason,
)

CANARY = "canary-7f3a-do-not-log"

# (error, the outcome it records, retryable by default). One row per class.
ERROR_TABLE = [
    pytest.param(SpendLimitReached(status_code=429), CallOutcome.SPEND_LIMIT, False, id="spend"),
    pytest.param(BillingError(status_code=402), CallOutcome.BILLING, False, id="billing"),
    pytest.param(Transient(status_code=529), CallOutcome.TRANSIENT_EXHAUSTED, True, id="transient"),
    pytest.param(AuthenticationFailed(status_code=401), CallOutcome.AUTH_FAILED, False, id="auth"),
    pytest.param(InvalidRequest(status_code=400), CallOutcome.INVALID_REQUEST, False, id="invalid"),
    pytest.param(ProviderError(status_code=418), CallOutcome.PROVIDER_ERROR, False, id="provider"),
    pytest.param(DeadlineExceeded(deadline_s=30), CallOutcome.DEADLINE_EXCEEDED, False, id="dead"),
    pytest.param(ModelRefused(), CallOutcome.REFUSAL, False, id="refusal"),
    pytest.param(
        OutputTruncated(stop_reason="max_tokens"), CallOutcome.TRUNCATED, False, id="truncated"
    ),
    pytest.param(
        StructuredOutputInvalid(problems=(("answer", "missing"),)),
        CallOutcome.VALIDATION_FAILED,
        False,
        id="validation",
    ),
]


@pytest.mark.parametrize(("error", "outcome", "retryable"), ERROR_TABLE)
def test_each_error_carries_its_outcome_and_retryable(
    error: GatewayError, outcome: CallOutcome, retryable: bool
) -> None:
    assert isinstance(error, GatewayError)
    assert isinstance(error, Exception)
    assert error.outcome is outcome
    assert error.retryable is retryable


@pytest.mark.parametrize(
    "cls",
    [
        SpendLimitReached,
        BillingError,
        Transient,
        AuthenticationFailed,
        InvalidRequest,
        ProviderError,
        ModelRefused,
    ],
)
def test_status_and_request_id_are_optional_and_kept(cls: type[GatewayError]) -> None:
    bare = cls()
    full = cls(status_code=503, request_id="req_011CVabc")

    assert (bare.status_code, bare.request_id) == (None, None)
    assert (full.status_code, full.request_id) == (503, "req_011CVabc")


def test_every_outcome_except_ok_cancel_and_internal_error_has_an_error_class() -> None:
    # `ok` is not an error; `cancelled` is asyncio.CancelledError and `internal_error` any other
    # exception that is not a GatewayError (a bug): both propagate unchanged, never wrapped.
    outcomes = {GatewayError.outcome} | {cls.outcome for cls in GatewayError.__subclasses__()}

    assert set(CallOutcome) - outcomes == {
        CallOutcome.OK,
        CallOutcome.CANCELLED,
        CallOutcome.INTERNAL_ERROR,
    }


# ── retryable and Retry-After ────────────────────────────────────────────────────────────────


def test_x_should_retry_can_flip_transient_and_provider_error_only() -> None:
    assert Transient(retryable=False).retryable is False
    assert ProviderError(retryable=True).retryable is True


@pytest.mark.parametrize(
    "cls", [SpendLimitReached, BillingError, AuthenticationFailed, InvalidRequest, ModelRefused]
)
def test_never_retried_classes_do_not_accept_retryable(cls: type[GatewayError]) -> None:
    with pytest.raises(TypeError):
        cls(retryable=True)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("error", "field"),
    [
        pytest.param(SpendLimitReached(), "retryable", id="retryable"),
        pytest.param(Transient(status_code=529), "status_code", id="status-code"),
        pytest.param(Transient(request_id="req_1"), "request_id", id="request-id"),
        pytest.param(Transient(retry_after=1.0), "retry_after", id="retry-after"),
        pytest.param(DeadlineExceeded(deadline_s=30), "deadline_s", id="deadline"),
        pytest.param(OutputTruncated(stop_reason="max_tokens"), "stop_reason", id="stop-reason"),
        pytest.param(StructuredOutputInvalid(problems=()), "problems", id="problems"),
    ],
)
def test_every_public_field_is_read_only(error: GatewayError, field: str) -> None:
    # Set once, checked once: assigning later would skip the constructor's checks.
    before = getattr(error, field)

    with pytest.raises(AttributeError):
        setattr(error, field, f"req {CANARY}")

    assert getattr(error, field) == before
    assert CANARY not in str(error)


def test_transient_carries_retry_after_in_seconds() -> None:
    assert Transient(status_code=429, retry_after=2.5).retry_after == 2.5
    assert Transient(status_code=429).retry_after is None
    assert Transient(retry_after=0).retry_after == 0


@pytest.mark.parametrize("retry_after", [-1.0, float("nan"), float("inf")])
def test_transient_rejects_an_impossible_retry_after(retry_after: float) -> None:
    with pytest.raises(ValueError, match="retry_after"):
        Transient(retry_after=retry_after)


# ── Messages are metadata only ───────────────────────────────────────────────────────────────


def test_no_error_takes_a_free_text_message() -> None:
    with pytest.raises(TypeError):
        Transient(CANARY)  # type: ignore[call-arg, arg-type]
    with pytest.raises(TypeError):
        InvalidRequest(message=CANARY)  # type: ignore[call-arg]


def test_str_and_repr_show_only_metadata() -> None:
    error = Transient(status_code=529, request_id="req_1", retry_after=2.0)

    assert str(error) == "status_code=529, request_id='req_1', retryable=True, retry_after=2.0"
    assert repr(error) == f"Transient({error})"
    assert str(DeadlineExceeded(deadline_s=30.0)) == (
        "status_code=None, request_id=None, retryable=False, deadline_s=30.0"
    )
    assert str(OutputTruncated(stop_reason="max_tokens", request_id="req_1")) == (
        "status_code=None, request_id='req_1', retryable=False, stop_reason='max_tokens'"
    )
    invalid = StructuredOutputInvalid(problems=(("citations.url", "string_type"),))
    assert str(invalid) == (
        "status_code=None, request_id=None, retryable=False,"
        " problems=(('citations.url', 'string_type'),)"
    )


def test_a_traceback_shows_only_metadata() -> None:
    try:
        raise InvalidRequest(status_code=400, request_id="req_9")
    except InvalidRequest as error:
        text = "".join(traceback.format_exception(error))

    assert text.endswith("InvalidRequest: status_code=400, request_id='req_9', retryable=False\n")


def test_raising_inside_an_except_block_chains_the_caught_text() -> None:
    # Why PR 5 raises gateway errors *outside* the `except` block (`__context__ is None`): inside
    # it, Python chains the exception being handled, and a traceback prints that one too.
    sdk_error = RuntimeError(f"an SDK error that echoes the prompt: {CANARY}")

    with pytest.raises(InvalidRequest) as inside:
        try:
            raise sdk_error
        except RuntimeError:
            raise InvalidRequest(status_code=400)  # noqa: B904

    handled = False
    try:
        raise sdk_error
    except RuntimeError:
        handled = True
    with pytest.raises(InvalidRequest) as outside:
        raise InvalidRequest(status_code=400)

    assert handled
    assert inside.value.__context__ is sdk_error
    assert CANARY in "".join(traceback.format_exception(inside.value))
    assert outside.value.__context__ is None
    assert CANARY not in "".join(traceback.format_exception(outside.value))


@pytest.mark.parametrize(
    "request_id",
    [
        pytest.param(f"{CANARY} with words", id="spaces"),
        pytest.param(f"req_1\n{CANARY}", id="newline"),
        pytest.param("x" * 129, id="too-long"),
        pytest.param("", id="empty"),
        pytest.param(f"Bearer {CANARY}", id="header-like"),
        pytest.param(f"sk-ant-api03-{CANARY}", id="anthropic-key"),
        pytest.param(f"SK-ANT-API03-{CANARY}", id="anthropic-key-uppercase"),
        pytest.param(f"sk-proj-{CANARY}", id="other-sk-key"),
        pytest.param(f"req_1:sk-ant-{CANARY}", id="key-inside-an-id"),
        pytest.param(f"eyJhbGciOiJIUzI1NiJ9.{CANARY}", id="jwt"),
    ],
)
def test_request_id_must_look_like_an_id_and_is_never_echoed(request_id: str) -> None:
    with pytest.raises(ValueError, match="request_id") as caught:
        Transient(request_id=request_id)

    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("status_code", [99, 600, 0, -1])
def test_status_code_must_be_an_http_status(status_code: int) -> None:
    with pytest.raises(ValueError, match="status_code"):
        ProviderError(status_code=status_code)


@pytest.mark.parametrize(
    "stop_reason", ["", "Max Tokens", f"max_tokens {CANARY}", "max_tokens\n", "context_overflow"]
)
def test_truncation_stop_reason_must_be_a_truncation_stop_reason(stop_reason: str) -> None:
    # mypy already rejects a plain str here; the runtime check catches what reaches it anyway.
    with pytest.raises(ValueError, match="stop_reason") as caught:
        OutputTruncated(stop_reason=stop_reason)  # type: ignore[arg-type]

    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("stop_reason", sorted(STOP_REASONS - TRUNCATION_STOP_REASONS))
def test_truncation_rejects_every_other_stop_reason(stop_reason: str) -> None:
    # `end_turn`, `refusal`, `other`, …: valid stop reasons, but not a cut-off output. LLMResult
    # refuses every truncation value (test_types.py), so a truncation is only ever this error.
    with pytest.raises(ValueError, match="truncation stop reason") as caught:
        OutputTruncated(stop_reason=stop_reason)  # type: ignore[arg-type]

    assert stop_reason not in str(caught.value)


@pytest.mark.parametrize("stop_reason", sorted(TRUNCATION_STOP_REASONS))
def test_each_truncation_stop_reason_builds_an_output_truncated(
    stop_reason: TruncationStopReason,
) -> None:
    error = OutputTruncated(stop_reason=stop_reason)

    assert (error.outcome, error.stop_reason) == (CallOutcome.TRUNCATED, stop_reason)


@pytest.mark.parametrize("deadline_s", [0.0, -1.0, float("inf"), float("nan")])
def test_deadline_must_be_a_positive_finite_number_of_seconds(deadline_s: float) -> None:
    with pytest.raises(ValueError, match="deadline_s"):
        DeadlineExceeded(deadline_s=deadline_s)


def test_one_except_clause_catches_every_gateway_error() -> None:
    caught = []
    for error in (SpendLimitReached(), Transient(), ModelRefused(), DeadlineExceeded(deadline_s=1)):
        try:
            raise error
        except GatewayError as gateway_error:
            caught.append(gateway_error)

    assert len(caught) == 4
