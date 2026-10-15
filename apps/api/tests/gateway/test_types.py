"""Tests for the gateway's typed values (`gateway/types.py`). [DELEGATE]: these pass today."""

import copy
import pickle
from decimal import Decimal
from typing import Any, Final, get_args

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from vectapilot_api.gateway.types import (
    STOP_REASONS,
    TRUNCATION_STOP_REASONS,
    BusinessId,
    CallOutcome,
    CallRecord,
    GatewayModel,
    LLMRequest,
    LLMResult,
    LongPromptTier,
    Message,
    ModelPricing,
    RedactedThinkingBlock,
    RetryPolicy,
    StopReason,
    TextBlock,
    ThinkingBlock,
    TokenPrices,
    TraceId,
    TruncationStopReason,
    Usage,
)

CANARY = "canary-7f3a-do-not-log"  # stands in for prompt or completion text

# Shaped like secrets but fake; each carries CANARY so an echo would show.
SECRET_SHAPED = [
    pytest.param(f"sk-ant-api03-{CANARY}", id="anthropic-key"),
    pytest.param(f"SK-ANT-API03-{CANARY}", id="anthropic-key-uppercase"),
    pytest.param(f"sk-proj-{CANARY}", id="other-sk-key"),
    pytest.param(f"req_1:sk-ant-{CANARY}", id="key-inside-an-id"),
    pytest.param(f"eyJhbGciOiJIUzI1NiJ9.{CANARY}", id="jwt"),
]

SILENT = ConfigDict(hide_input_in_errors=True)
BUSINESS_ID: TypeAdapter[str] = TypeAdapter(BusinessId, config=SILENT)
TRACE_ID: TypeAdapter[str] = TypeAdapter(TraceId, config=SILENT)
VALID_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"  # the W3C Trace Context example

PRICES = TokenPrices(
    input=Decimal("3"),
    output=Decimal("7"),
    cache_read=Decimal("0.3"),
    cache_write_5m=Decimal("11"),
    cache_write_1h=Decimal("13"),
)


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "business_id": "sunrise-clinic",
        "alias": "small",
        "messages": [{"role": "user", "content": "When do you open on Sunday?"}],
    }
    return LLMRequest.model_validate(fields | overrides)


NO_RESPONSE: Final[dict[str, Any]] = {
    "model_answered": None,
    "inference_geo": None,
    "input_tokens": 0,
    "output_tokens": 0,
    "cost_usd": Decimal("0"),
}
"""What a call that never got a response leaves in its record (with `cost_complete` True)."""


def record(**overrides: Any) -> CallRecord:
    fields: dict[str, Any] = {
        "business_id": "paws-and-co",
        "alias": "small",
        "model_requested": "model-a",
        "model_answered": "model-a",
        "catalog_version": "2026-10-14.1",
        "inference_geo": "global",
        "outcome": "ok",
        "retries": 0,
        "repairs": 0,
        "attempt_timeouts": 0,
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": Decimal("0.000003"),
        "cost_complete": True,
        "cache_hit": False,
        "latency_ms": 812.5,
    }
    return CallRecord.model_validate(fields | overrides)


# ── BusinessId ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["sunrise-clinic", "paws-and-co", "northwind-shop", "abc", "a-b", "x" * 40, "clinic-2"],
)
def test_business_id_accepts_lowercase_slugs_of_3_to_40_characters(value: str) -> None:
    assert BUSINESS_ID.validate_python(value) == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("sunrise-clinic\n", id="trailing-newline"),
        pytest.param("Sunrise-Clinic", id="uppercase"),
        pytest.param("sunrise--clinic", id="double-hyphen"),
        pytest.param("-sunrise-clinic", id="leading-hyphen"),
        pytest.param("sunrise-clinic-", id="trailing-hyphen"),
        pytest.param("ab", id="2-characters"),
        pytest.param("a" * 41, id="41-characters"),
        pytest.param("", id="empty"),
        pytest.param("sunrise clinic", id="space"),
        pytest.param("sunrise_clinic", id="underscore"),
        pytest.param(" sunrise-clinic", id="leading-space"),
        pytest.param("sunrïse-clinic", id="non-ascii-letter"),
    ],
)
def test_business_id_rejects_anything_else(value: str) -> None:
    with pytest.raises(ValidationError):
        BUSINESS_ID.validate_python(value)


def test_business_id_must_be_a_string() -> None:
    with pytest.raises(ValidationError):
        BUSINESS_ID.validate_python(12345)


def test_a_type_validated_on_its_own_needs_hide_input_in_errors_too() -> None:
    # hide_input_in_errors is a model (or TypeAdapter) setting, not part of the type: a bare
    # TypeAdapter echoes the input. The module docstring says so; this pins it.
    bad = f"{CANARY}\n"

    with pytest.raises(ValidationError) as bare:
        TypeAdapter(BusinessId).validate_python(bad)
    with pytest.raises(ValidationError) as silent:
        BUSINESS_ID.validate_python(bad)

    assert CANARY in str(bare.value)
    assert CANARY not in str(silent.value)


# ── Usage and prices ─────────────────────────────────────────────────────────────────────────


def test_usage_defaults_every_class_to_zero() -> None:
    assert Usage().model_dump() == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
    }


@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None])
def test_token_counts_are_strict_non_negative_ints(value: object) -> None:
    with pytest.raises(ValidationError):
        Usage.model_validate({"output_tokens": value})


@pytest.mark.parametrize("field", ["cache_creation_input_tokens", "inference_geo"])
def test_usage_rejects_unknown_fields(field: str) -> None:
    # A typo or a provider field name must not be silently dropped (the cost would be wrong), and
    # Usage is the five billed classes only: the geo travels next to it, not inside it.
    with pytest.raises(ValidationError):
        Usage.model_validate({field: 5})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(Decimal("0.10"), Decimal("0.10"), id="decimal"),
        pytest.param(3, Decimal("3"), id="whole-number-from-toml"),
        pytest.param("0.10", Decimal("0.10"), id="decimal-string"),
        pytest.param(0, Decimal("0"), id="free"),
    ],
)
def test_prices_are_exact_decimals(value: object, expected: Decimal) -> None:
    prices = TokenPrices.model_validate(PRICES.model_dump() | {"input": value})
    assert prices.input == expected
    assert type(prices.input) is Decimal


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0.1, id="float"),  # a catalog read without parse_float=Decimal
        pytest.param(True, id="bool"),
        pytest.param(Decimal("-0.01"), id="negative"),
        pytest.param(Decimal("NaN"), id="nan"),
        pytest.param("Infinity", id="infinity"),
        pytest.param("three", id="not-a-number"),
    ],
)
def test_prices_reject_floats_and_non_finite_or_negative_values(value: object) -> None:
    with pytest.raises(ValidationError):
        TokenPrices.model_validate(PRICES.model_dump() | {"output": value})


def test_model_pricing_is_single_tier_unless_a_long_prompt_tier_is_given() -> None:
    single = ModelPricing(standard=PRICES)
    tiered = ModelPricing(
        standard=PRICES, long_prompt=LongPromptTier(threshold_tokens=100, prices=PRICES)
    )

    assert single.long_prompt is None
    assert tiered.long_prompt is not None
    assert tiered.long_prompt.threshold_tokens == 100


@pytest.mark.parametrize("threshold", [0, -1, 100.0, "100"])
def test_long_prompt_threshold_is_a_positive_int(threshold: object) -> None:
    with pytest.raises(ValidationError):
        LongPromptTier.model_validate({"threshold_tokens": threshold, "prices": PRICES})


# ── RetryPolicy ──────────────────────────────────────────────────────────────────────────────


def test_retry_policy_accepts_the_adr_starting_values() -> None:
    policy = RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8)

    assert (policy.max_retries, policy.base_delay_s, policy.max_delay_s) == (2, 0.5, 8.0)


def test_retry_policy_accepts_seconds_as_strings_from_settings() -> None:
    policy = RetryPolicy.model_validate(
        {"max_retries": 2, "base_delay_s": "0.5", "max_delay_s": "8"}
    )

    assert (policy.base_delay_s, policy.max_delay_s) == (0.5, 8.0)


def test_retry_policy_accepts_the_cap_of_10_retries() -> None:
    # The cap itself is allowed (decided 2026-10-14); 11 is rejected below.
    assert RetryPolicy(max_retries=10, base_delay_s=0.5, max_delay_s=8).max_retries == 10


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"max_delay_s": 0.25}, id="cap-below-base"),
        pytest.param({"base_delay_s": 0}, id="zero-base"),
        pytest.param({"base_delay_s": -0.5}, id="negative-base"),
        pytest.param({"base_delay_s": True}, id="bool-seconds"),
        pytest.param({"max_delay_s": float("inf")}, id="infinite-cap"),
        pytest.param({"max_delay_s": float("nan")}, id="nan-cap"),
        pytest.param({"max_retries": -1}, id="negative-retries"),
        pytest.param({"max_retries": 11}, id="more-than-10-retries"),
        pytest.param({"max_retries": True}, id="bool-retries"),
        pytest.param({"max_retries": 2.0}, id="float-retries"),
    ],
)
def test_retry_policy_rejects_unsafe_values(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RetryPolicy.model_validate(
            {"max_retries": 2, "base_delay_s": 0.5, "max_delay_s": 8.0} | fields
        )


# ── LLMRequest ───────────────────────────────────────────────────────────────────────────────


def test_a_minimal_request_is_valid_and_immutable() -> None:
    req = request()

    assert req.business_id == "sunrise-clinic"
    assert req.messages == (Message(role="user", content="When do you open on Sunday?"),)
    assert (req.system, req.max_tokens, req.trace_id, req.operation) == (None, None, None, None)
    with pytest.raises(AttributeError, match="frozen"):
        req.alias = "large"  # type: ignore[misc]


def test_a_request_without_business_id_is_rejected() -> None:
    fields = request().model_dump(exclude={"business_id"})

    with pytest.raises(ValidationError):
        LLMRequest.model_validate(fields)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"business_id": "Sunrise-Clinic"}, id="bad-business-id"),
        pytest.param({"alias": ""}, id="empty-alias"),
        pytest.param({"alias": "Small"}, id="uppercase-alias"),
        pytest.param({"messages": []}, id="no-messages"),
        pytest.param({"messages": [{"role": "system", "content": "x"}]}, id="system-role"),
        pytest.param({"messages": [{"role": "user", "content": ""}]}, id="empty-content"),
        pytest.param({"system": ""}, id="empty-system"),
        pytest.param({"max_tokens": 0}, id="zero-max-tokens"),
        pytest.param({"max_tokens": 1.5}, id="float-max-tokens"),
        pytest.param({"operation": "RAG answer"}, id="operation-not-a-slug"),
        pytest.param({"temperature": 0.2}, id="temperature-is-never-sent"),
    ],
)
def test_invalid_requests_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


def test_trace_id_accepts_a_w3c_trace_id() -> None:
    assert request(trace_id=VALID_TRACE_ID).trace_id == VALID_TRACE_ID


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(VALID_TRACE_ID.upper(), id="uppercase"),
        pytest.param(VALID_TRACE_ID[:-1], id="31-characters"),
        pytest.param(VALID_TRACE_ID + "0", id="33-characters"),
        pytest.param("0" * 32, id="all-zeros"),
        pytest.param(VALID_TRACE_ID + "\n", id="trailing-newline"),
        pytest.param("g" + VALID_TRACE_ID[1:], id="not-hex"),
    ],
)
def test_trace_id_rejects_anything_else(value: str) -> None:
    with pytest.raises(ValidationError):
        TRACE_ID.validate_python(value)


# ── LLMResult ────────────────────────────────────────────────────────────────────────────────


def result(*content: dict[str, str]) -> LLMResult:
    return LLMResult.model_validate(
        {
            "content": list(content),
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model_answered": "model-a",
            "cost_usd": Decimal("0.000065"),
            "cost_complete": True,
        }
    )


def test_result_keeps_content_blocks_in_order_and_text_skips_thinking() -> None:
    res = result(
        {"type": "thinking", "thinking": "Sunday hours are in the FAQ.", "signature": "sig"},
        {"type": "text", "text": "We open "},
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": "at 10:00."},
    )

    assert [type(block) for block in res.content] == [
        ThinkingBlock,
        TextBlock,
        RedactedThinkingBlock,
        TextBlock,
    ]
    assert res.text == "We open at 10:00."


def test_result_text_is_empty_without_text_blocks() -> None:
    assert result({"type": "thinking", "thinking": "", "signature": "sig"}).text == ""


def test_a_thinking_block_round_trips_unchanged() -> None:
    res = result({"type": "thinking", "thinking": "step 1", "signature": "abc=="})

    assert LLMResult.model_validate(res.model_dump()) == res


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"cost_usd": 0.000065}, id="float-cost"),
        pytest.param({"cost_complete": 1}, id="int-as-bool"),
        pytest.param({"content": [{"type": "image", "data": "x"}]}, id="unknown-block-type"),
        pytest.param({"model_answered": ""}, id="empty-model"),
        pytest.param({"model_answered": f"model-a {CANARY}"}, id="model-with-text"),
        pytest.param({"model_answered": f"sk-ant-api03-{CANARY}"}, id="model-shaped-like-a-key"),
        pytest.param({"stop_reason": "End Turn"}, id="stop-reason-with-a-space"),
        pytest.param({"stop_reason": "end_turn\n"}, id="stop-reason-trailing-newline"),
        pytest.param({"stop_reason": "context_overflow"}, id="stop-reason-not-in-the-set"),
        pytest.param({"stop_reason": "refusal"}, id="refusal-is-an-error"),
        pytest.param({"stop_reason": "max_tokens"}, id="truncation-is-an-error"),
        pytest.param(
            {"stop_reason": "model_context_window_exceeded"}, id="context-window-is-an-error"
        ),
    ],
)
def test_result_rejects_invalid_fields(fields: dict[str, object]) -> None:
    valid = result({"type": "text", "text": "ok"}).model_dump()

    with pytest.raises(ValidationError) as caught:
        LLMResult.model_validate(valid | fields)

    assert CANARY not in str(caught.value)


# ── CallRecord (the `llm_call` contract, ADR 0007) ───────────────────────────────────────────


def test_call_record_has_exactly_the_contract_fields() -> None:
    # Adding, removing or renaming a field is a contract change: update docs/plans/m1-pr-checks.md
    # (PR 6) and, if it breaks readers, bump record_version.
    assert list(CallRecord.model_fields) == [
        "record_version",
        "business_id",
        "alias",
        "model_requested",
        "model_answered",
        "catalog_version",
        "inference_geo",
        "outcome",
        "stop_reason",
        "retries",
        "repairs",
        "attempt_timeouts",
        "first_try_valid",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "cost_usd",
        "cost_complete",
        "cache_hit",
        "latency_ms",
        "request_ids",
        "trace_id",
        "operation",
    ]


def test_call_record_token_fields_match_usage() -> None:
    usage = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3)

    rec = record(**usage.model_dump())

    assert rec.cache_read_tokens == 3
    assert set(Usage.model_fields) <= set(CallRecord.model_fields)


def test_call_outcome_is_the_closed_list_from_the_contract() -> None:
    assert [outcome.value for outcome in CallOutcome] == [
        "ok",
        "refusal",
        "truncated",
        "validation_failed",
        "transient_exhausted",
        "deadline_exceeded",
        "cancelled",
        "spend_limit",
        "billing",
        "auth_failed",
        "invalid_request",
        "provider_error",
    ]


def test_stop_reason_is_the_documented_closed_set_plus_other() -> None:
    # The provider's list, checked 2026-10-14 on /api/messages and
    # /build-with-claude/handling-stop-reasons; `other` is ours (the adapter maps unknown values).
    assert get_args(StopReason) == (
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "model_context_window_exceeded",
        "other",
    )
    assert frozenset(get_args(StopReason)) == STOP_REASONS


def test_truncation_stop_reasons_are_the_two_documented_ones() -> None:
    # `model_context_window_exceeded` is a truncation: decided by shesha, 2026-10-15.
    assert get_args(TruncationStopReason) == ("max_tokens", "model_context_window_exceeded")
    assert frozenset(get_args(TruncationStopReason)) == TRUNCATION_STOP_REASONS
    assert TRUNCATION_STOP_REASONS < STOP_REASONS


@pytest.mark.parametrize("value", sorted(TRUNCATION_STOP_REASONS | {"refusal"}))
def test_result_refuses_every_refusal_and_truncation_stop_reason(value: str) -> None:
    # Derived from the set, so a truncation value added later is refused too (errors.py's
    # OutputTruncated takes exactly these truncation values).
    valid = result({"type": "text", "text": "ok"}).model_dump()

    with pytest.raises(ValidationError, match="typed error"):
        LLMResult.model_validate(valid | {"stop_reason": value})


@pytest.mark.parametrize("value", sorted(STOP_REASONS))
def test_every_stop_reason_and_other_can_be_recorded(value: str) -> None:
    assert record(stop_reason=value).stop_reason == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("context_overflow", id="well-shaped-but-unknown"),
        pytest.param("END_TURN", id="uppercase"),
        pytest.param("other\n", id="trailing-newline"),
        pytest.param("", id="empty"),
        pytest.param(f"end_turn {CANARY}", id="with-text"),
    ],
)
def test_a_stop_reason_outside_the_set_is_rejected_by_record_and_result(value: str) -> None:
    valid = result({"type": "text", "text": "ok"}).model_dump()

    with pytest.raises(ValidationError) as as_record:
        record(stop_reason=value)
    with pytest.raises(ValidationError) as as_result:
        LLMResult.model_validate(valid | {"stop_reason": value})

    assert CANARY not in str(as_record.value)
    assert CANARY not in str(as_result.value)


@pytest.mark.parametrize(
    ("retries", "repairs", "attempt_timeouts"),
    [
        pytest.param(0, 0, 0, id="none"),
        pytest.param(0, 0, 1, id="the-only-attempt"),
        pytest.param(2, 0, 3, id="every-attempt"),
        pytest.param(1, 1, 3, id="every-attempt-with-a-repair"),
    ],
)
def test_attempt_timeouts_may_reach_the_number_of_attempts(
    retries: int, repairs: int, attempt_timeouts: int
) -> None:
    # attempts = 1 + repairs + retries (decided 2026-10-14)
    rec = record(
        outcome="transient_exhausted",
        retries=retries,
        repairs=repairs,
        attempt_timeouts=attempt_timeouts,
    )

    assert rec.attempt_timeouts == attempt_timeouts


@pytest.mark.parametrize(
    ("retries", "repairs", "attempt_timeouts"),
    [
        pytest.param(0, 0, 2, id="one-attempt"),
        pytest.param(2, 0, 4, id="three-attempts"),
        pytest.param(1, 1, 4, id="three-attempts-with-a-repair"),
    ],
)
def test_attempt_timeouts_cannot_exceed_the_number_of_attempts(
    retries: int, repairs: int, attempt_timeouts: int
) -> None:
    with pytest.raises(ValidationError, match="exceed the attempts"):
        record(
            outcome="transient_exhausted",
            retries=retries,
            repairs=repairs,
            attempt_timeouts=attempt_timeouts,
        )


@pytest.mark.parametrize(
    ("retries", "repairs", "attempt_timeouts"),
    [
        pytest.param(1, 0, 1, id="one-timeout-then-the-answer"),
        pytest.param(2, 1, 3, id="every-attempt-but-the-last"),
    ],
)
def test_an_ok_call_may_have_timeouts_before_its_last_attempt(
    retries: int, repairs: int, attempt_timeouts: int
) -> None:
    rec = record(retries=retries, repairs=repairs, attempt_timeouts=attempt_timeouts)

    assert (rec.outcome, rec.attempt_timeouts) == (CallOutcome.OK, attempt_timeouts)


@pytest.mark.parametrize(
    ("retries", "repairs", "attempt_timeouts"),
    [
        pytest.param(0, 0, 1, id="the-only-attempt"),
        pytest.param(2, 0, 3, id="every-attempt"),
        pytest.param(1, 1, 3, id="every-attempt-with-a-repair"),
    ],
)
def test_an_ok_call_cannot_have_timed_out_on_its_last_attempt(
    retries: int, repairs: int, attempt_timeouts: int
) -> None:
    # `ok` means the last attempt answered; the same counts are valid for a failed call (above).
    with pytest.raises(ValidationError, match="ok call"):
        record(retries=retries, repairs=repairs, attempt_timeouts=attempt_timeouts)


def test_a_cache_hit_cannot_have_attempt_timeouts() -> None:
    # retries=1 keeps the ok rule satisfied, so only the cache-hit rule can reject this record.
    with pytest.raises(ValidationError, match="cache hit"):
        record(cache_hit=True, cost_usd=Decimal("0"), retries=1, attempt_timeouts=1)


def test_call_record_serialises_to_json_with_an_exact_cost() -> None:
    rec = record(trace_id=VALID_TRACE_ID, operation="rag-answer", request_ids=["req_011abc"])

    dumped = rec.model_dump(mode="json")

    assert dumped["record_version"] == 1
    assert dumped["outcome"] == "ok"
    assert dumped["cost_usd"] == "0.000003"  # a string, so no float ever touches the money
    assert dumped["request_ids"] == ["req_011abc"]
    assert dumped["first_try_valid"] is None


@pytest.mark.parametrize(
    ("cost", "written"),
    [
        pytest.param(Decimal("3E-7"), "0.0000003", id="no-scientific-notation"),
        pytest.param(Decimal("0.00030"), "0.00030", id="digits-kept"),
        pytest.param(Decimal("-0"), "0", id="negative-zero"),
        pytest.param(Decimal("12.5"), "12.5", id="plain"),
    ],
)
def test_cost_is_written_to_json_in_fixed_point(cost: Decimal, written: str) -> None:
    rec = record(cost_usd=cost)

    assert rec.model_dump(mode="json")["cost_usd"] == written
    assert rec.model_dump()["cost_usd"] == cost  # Python mode keeps the exact Decimal


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"record_version": 2}, id="other-record-version"),
        pytest.param({"outcome": "timeout"}, id="outcome-not-in-the-enum"),
        pytest.param({"business_id": "Paws"}, id="bad-business-id"),
        pytest.param({"trace_id": "0" * 32}, id="bad-trace-id"),
        pytest.param({"request_ids": ["req 1"]}, id="request-id-with-space"),
        pytest.param({"request_ids": ["x" * 129]}, id="request-id-too-long"),
        pytest.param({"latency_ms": -1}, id="negative-latency"),
        pytest.param({"latency_ms": True}, id="bool-latency"),
        pytest.param({"latency_ms": "12.5"}, id="string-latency"),
        pytest.param({"retries": -1}, id="negative-retries"),
        pytest.param({"attempt_timeouts": -1}, id="negative-attempt-timeouts"),
        pytest.param({"attempt_timeouts": True}, id="bool-attempt-timeouts"),
        pytest.param({"attempt_timeouts": 1.0}, id="float-attempt-timeouts"),
        pytest.param({"attempt_timeouts": "1"}, id="string-attempt-timeouts"),
        pytest.param({"attempt_timeouts": 2}, id="more-timeouts-than-attempts"),
        pytest.param({"stop_reason": "context_overflow"}, id="stop-reason-not-in-the-set"),
        pytest.param({"cost_usd": 0.1}, id="float-cost"),
        pytest.param({"cache_hit": "yes"}, id="string-as-bool"),
        pytest.param({"cost_complete": 1}, id="int-as-bool"),
        pytest.param({"first_try_valid": "true"}, id="string-first-try-valid"),
        pytest.param({"prompt": "hello"}, id="no-text-fields"),
        pytest.param({"stop_reason": f"max_tokens {CANARY}"}, id="stop-reason-with-text"),
        pytest.param({"model_answered": f"model-a\n{CANARY}"}, id="model-answered-with-text"),
        pytest.param({"model_requested": ""}, id="empty-model-requested"),
        pytest.param({"catalog_version": "v1 (draft)"}, id="catalog-version-with-spaces"),
        pytest.param({"inference_geo": "x" * 129}, id="inference-geo-too-long"),
        pytest.param({"cache_hit": True}, id="cache-hit-with-a-cost"),
        pytest.param({"inference_geo": "us"}, id="non-global-geo-marked-complete"),
        pytest.param({"inference_geo": None}, id="unknown-geo-marked-complete"),
        pytest.param({"model_answered": None, "inference_geo": "us"}, id="a-geo-means-a-response"),
        # Only a record of no response at all is exempt from the geo rule (step 6 review):
        pytest.param(
            {**NO_RESPONSE, "input_tokens": 1}, id="no-response-but-tokens-marked-complete"
        ),
        pytest.param(
            {**NO_RESPONSE, "cost_usd": Decimal("0.000003")},
            id="no-response-but-a-cost-marked-complete",
        ),
        pytest.param(
            {**NO_RESPONSE, "stop_reason": "end_turn"},
            id="no-response-but-a-stop-reason-marked-complete",
        ),
        # model_requested is None only for an unknown alias (invalid_request, no response):
        pytest.param({"model_requested": None}, id="no-model-requested-on-a-response"),
        pytest.param(
            {**NO_RESPONSE, "model_requested": None, "outcome": "ok"}, id="no-model-requested-ok"
        ),
        pytest.param(
            {"model_requested": None, "outcome": "invalid_request"},
            id="no-model-requested-but-a-response",
        ),
    ],
)
def test_call_record_rejects_invalid_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as caught:
        record(**fields)

    assert CANARY not in str(caught.value)


def test_call_record_accepts_metadata_shaped_values() -> None:
    rec = record(
        model_requested="model-a-20261001",
        model_answered="model-a-20261001",
        catalog_version="2026-10-14.1",
        inference_geo="global",
        outcome="truncated",
        stop_reason="max_tokens",
        request_ids=["req_011abc", "req_011abd"],
        latency_ms=812,
    )

    assert (rec.inference_geo, rec.stop_reason) == ("global", "max_tokens")
    assert rec.outcome is CallOutcome.TRUNCATED
    assert rec.latency_ms == 812.0


def test_call_record_accepts_the_cost_rules_of_adr_0006() -> None:
    hit = record(cache_hit=True, cost_usd=Decimal("0"))
    us = record(inference_geo="us", cost_complete=False)

    assert (hit.cache_hit, hit.cost_usd) == (True, Decimal("0"))
    assert (us.inference_geo, us.cost_complete) == ("us", False)


@pytest.mark.parametrize("geo", [None, "us"], ids=["unknown", "us"])
def test_a_response_whose_geo_is_not_global_cannot_be_cost_complete(geo: str | None) -> None:
    # ADR 0006, Cost; None fails closed (decided 2026-10-14).
    with pytest.raises(ValidationError, match="inference_geo is not global"):
        record(inference_geo=geo, cost_complete=True)

    assert record(inference_geo=geo, cost_complete=False).inference_geo == geo


def test_a_call_with_no_response_may_be_cost_complete() -> None:
    # Decided 2026-10-15: a pre-call rejection writes one record with zeros and a complete $0.
    rec = record(**NO_RESPONSE, outcome="invalid_request")

    assert (rec.model_answered, rec.inference_geo, rec.cost_complete) == (None, None, True)


def test_an_unknown_alias_is_recorded_without_a_model_requested() -> None:
    # Proposed after the step 6 review (shesha to confirm): every call writes one record.
    rec = record(**NO_RESPONSE, outcome="invalid_request", alias="medium", model_requested=None)

    assert (rec.alias, rec.model_requested, rec.cost_complete) == ("medium", None, True)


def test_a_cache_hit_may_be_cost_complete_without_a_geo() -> None:
    # The rule is `not cache_hit and inference_geo != "global" and cost_complete`: a hit makes no
    # model call (PR 7 sets its fields).
    rec = record(cache_hit=True, cost_usd=Decimal("0"), inference_geo=None, cost_complete=True)

    assert (rec.cache_hit, rec.cost_complete) == (True, True)


@pytest.mark.parametrize("field", list(CallRecord.model_fields))
def test_no_call_record_field_accepts_free_text(field: str) -> None:
    # The record is metadata only (ADR 0007). A future free-text field would make this fail.
    with pytest.raises(ValidationError) as caught:
        record(**{field: f"a {CANARY}"})

    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("value", SECRET_SHAPED)
@pytest.mark.parametrize(
    "field", ["model_requested", "model_answered", "catalog_version", "inference_geo"]
)
def test_secret_shaped_values_are_rejected_in_every_id_field(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as as_field:
        record(**{field: value})
    with pytest.raises(ValidationError) as as_request_id:
        record(request_ids=[value])

    assert CANARY not in str(as_field.value)
    assert CANARY not in str(as_request_id.value)


# ── Every way to build or change a value is validated ────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "update"),
    [
        pytest.param(request(), {"prompt": CANARY}, id="unknown-field"),
        pytest.param(request(), {"business_id": f"Other Biz\n{CANARY}"}, id="bad-business-id"),
        pytest.param(record(), {"stop_reason": f"max_tokens {CANARY}"}, id="text-in-a-record"),
        pytest.param(record(), {"request_ids": (f"sk-ant-api03-{CANARY}",)}, id="key-as-id"),
        pytest.param(record(), {"cache_hit": True}, id="breaks-a-cost-rule"),
        pytest.param(record(), {"attempt_timeouts": 2}, id="breaks-the-attempts-rule"),
    ],
)
def test_model_copy_with_an_update_is_validated(
    model: GatewayModel, update: dict[str, object]
) -> None:
    # Pydantic's own model_copy(update=...) stores the update unchecked; ours validates it.
    with pytest.raises(ValidationError) as caught:
        model.model_copy(update=update)

    assert CANARY not in str(caught.value)


def test_model_copy_still_works_for_valid_updates_and_plain_copies() -> None:
    # The block's `type` is a default that was never set explicitly: the copy must still find it.
    res = LLMResult(
        content=(TextBlock(text="We open at 10:00."),),
        stop_reason="end_turn",
        usage=Usage(output_tokens=5),
        model_answered="model-a",
        cost_usd=Decimal("0"),
        cost_complete=True,
    )

    changed = res.model_copy(update={"cost_usd": Decimal("0.000035")})

    assert changed.content == res.content
    assert changed.cost_usd == Decimal("0.000035")
    assert res.cost_usd == Decimal("0")
    assert res.model_copy() == res
    assert res.model_copy(deep=True) == res


@pytest.mark.parametrize("model", [LLMRequest, CallRecord, Message])
def test_model_construct_is_refused(model: type[GatewayModel]) -> None:
    values: dict[str, Any] = {"stop_reason": f"x {CANARY}"}

    with pytest.raises(TypeError, match="model_construct"):
        model.model_construct(**values)


def test_the_deprecated_copy_is_refused() -> None:
    with pytest.raises(TypeError, match="model_copy"):
        record().copy(update={"stop_reason": CANARY})


@pytest.mark.parametrize(
    ("model", "field"),
    [
        pytest.param(request(system="Be brief."), "system", id="request-system"),
        pytest.param(Message(role="user", content="Hi"), "content", id="message-content"),
        pytest.param(TextBlock(text="Hello"), "text", id="text-block"),
        pytest.param(record(), "stop_reason", id="record"),
    ],
)
def test_setting_or_deleting_a_field_is_refused_without_echoing_the_value(
    model: BaseModel, field: str
) -> None:
    before = getattr(model, field)

    with pytest.raises(AttributeError) as set_error:
        setattr(model, field, CANARY)
    with pytest.raises(AttributeError) as del_error:
        delattr(model, field)

    for error in (set_error.value, del_error.value):
        assert field in str(error)
        assert CANARY not in str(error)
        assert CANARY not in repr(error)
    assert getattr(model, field) == before


def test_copy_deepcopy_hash_and_pickle_still_work() -> None:
    req = request(system="Be brief.", trace_id=VALID_TRACE_ID)

    assert copy.copy(req) == req
    assert copy.deepcopy(req) == req
    assert hash(copy.copy(req)) == hash(req)
    assert pickle.loads(pickle.dumps(req)) == req  # noqa: S301 (our own bytes, just made)


# ── What is hidden, and what is not ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(Message(role="user", content=CANARY), id="message"),
        pytest.param(
            request(system=CANARY, messages=[{"role": "user", "content": CANARY}]), id="request"
        ),
        pytest.param(TextBlock(text=CANARY), id="text-block"),
        pytest.param(ThinkingBlock(thinking=CANARY, signature=CANARY), id="thinking-block"),
        pytest.param(RedactedThinkingBlock(data=CANARY), id="redacted-thinking-block"),
        pytest.param(
            result(
                {"type": "text", "text": CANARY},
                {"type": "thinking", "thinking": CANARY, "signature": CANARY},
            ),
            id="result",
        ),
    ],
)
def test_text_fields_are_hidden_from_repr_and_str(model: BaseModel) -> None:
    assert CANARY not in repr(model)
    assert CANARY not in str(model)


def test_validation_errors_do_not_echo_input_values() -> None:
    bad = {"business_id": CANARY + "\n", "messages": [{"role": "system", "content": CANARY}]}

    with pytest.raises(ValidationError) as caught:
        request(**bad)

    error = caught.value
    assert {loc[0] for loc in (e["loc"] for e in error.errors())} == {"business_id", "messages"}
    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    # `hide_input_in_errors` covers str() and repr() only: `.errors()` and `.json()` still carry
    # the input unless asked not to.
    assert CANARY in str(error.errors())
    assert CANARY not in str(error.errors(include_input=False))
    assert CANARY in error.json()
    assert CANARY not in error.json(include_input=False)


def test_unknown_keys_and_union_tags_are_still_echoed() -> None:
    # The limits of hide_input_in_errors, pinned so nobody relies on more: an unknown key is the
    # error's location and a union tag is quoted in the message, so both reach str(error). Hence
    # a ValidationError from a gateway model is never logged or chained (module docstring).
    valid = result({"type": "text", "text": "ok"}).model_dump()

    with pytest.raises(ValidationError) as key_error:
        LLMRequest.model_validate(request().model_dump() | {f"Patient {CANARY}": 1})
    with pytest.raises(ValidationError) as tag_error:
        LLMResult.model_validate(valid | {"content": [{"type": CANARY}]})

    assert CANARY in str(key_error.value)
    assert CANARY in str(tag_error.value)


def test_dumps_carry_the_text_for_the_provider_call() -> None:
    # The other side of the boundary: dumps are how the adapter sends the text, so they hold it.
    # That is why the only gateway value ever logged is CallRecord.
    assert CANARY in request(system=CANARY).model_dump_json()
    assert CANARY in result({"type": "text", "text": CANARY}).model_dump_json()


def test_every_gateway_model_is_frozen_closed_and_silent() -> None:
    models = GatewayModel.__subclasses__()

    assert len(models) >= 12
    for model in models:
        assert model.model_config.get("frozen") is True, model
        assert model.model_config.get("extra") == "forbid", model
        assert model.model_config.get("hide_input_in_errors") is True, model
