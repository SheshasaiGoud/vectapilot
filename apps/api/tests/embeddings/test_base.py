"""Batching, the token bound, the response checks and the typed errors (`embeddings/base.py`).

The batching examples are small enough to check by hand: each text's bound is its UTF-8 bytes
plus `TOKEN_OVERHEAD_PER_TEXT` (8), so "aa" counts 10 and "aaaaaaaaaaaa" (12 bytes) counts 20.
"""

import math
import traceback

import pytest

from vectapilot_api.embeddings.base import (
    TOKEN_OVERHEAD_PER_TEXT,
    UNIT_NORM_TOLERANCE,
    EmbeddingSpec,
    InputTooLarge,
    RateLimited,
    check_business_id,
    check_vectors,
    plan_batches,
    token_upper_bound,
)
from vectapilot_api.gateway.errors import InvalidRequest, ProviderError, Transient


def spec(**overrides: int) -> EmbeddingSpec:
    fields: dict[str, object] = {
        "model": "test-model",
        "dimension": 2,
        "max_texts_per_request": 3,
        "max_tokens_per_request": 30,
        "context_tokens": 20,
    }
    return EmbeddingSpec.model_validate(fields | overrides)


# ── Token bound ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "utf8_bytes"),
    [("aa", 2), ("नमस्ते", 18), ("é", 2), ("🙂", 4), ("\udc80", 3)],
    ids=["ascii", "devanagari", "latin", "emoji", "lone-surrogate"],
)
def test_the_token_bound_is_utf8_bytes_plus_the_overhead(text: str, utf8_bytes: int) -> None:
    assert TOKEN_OVERHEAD_PER_TEXT == 8
    assert token_upper_bound(text) == utf8_bytes + TOKEN_OVERHEAD_PER_TEXT


# ── Batching ─────────────────────────────────────────────────────────────────────────────────


def test_batches_never_exceed_the_text_count() -> None:
    # Seven 10-token texts, at most 3 per request (30 tokens would allow 3 as well).
    assert plan_batches(["aa"] * 7, spec()) == [range(0, 3), range(3, 6), range(6, 7)]


def test_batches_never_exceed_the_token_total() -> None:
    # 20 + 10 = 30 fits; a third text would make 40 > 30. Then 20 + 10 again.
    texts = ["a" * 12, "aa", "a" * 12, "aa"]

    assert plan_batches(texts, spec()) == [range(0, 2), range(2, 4)]


def test_one_batch_when_everything_fits() -> None:
    assert plan_batches(["aa", "bb"], spec()) == [range(0, 2)]


def test_the_voyage_request_limit_of_1000_texts_holds() -> None:
    batches = plan_batches(
        ["aa"] * 2_001, spec(max_texts_per_request=1_000, max_tokens_per_request=10**6)
    )

    assert [len(batch) for batch in batches] == [1_000, 1_000, 1]


def test_a_text_over_the_context_is_refused_before_anything_is_planned() -> None:
    with pytest.raises(InputTooLarge) as caught:
        plan_batches(["aa", "a" * 13], spec())  # 13 + 8 = 21 > 20

    error = caught.value
    assert (error.index, error.limit, error.status_code, error.retryable) == (1, 20, None, False)
    assert isinstance(error, InvalidRequest)
    assert str(error) == ("status_code=None, request_id=None, retryable=False, index=1, limit=20")


def test_a_text_exactly_at_the_context_is_allowed() -> None:
    assert plan_batches(["a" * 12], spec()) == [range(0, 1)]  # 12 + 8 = 20


@pytest.mark.parametrize("texts", [[], "a bare string", ["ok", ""], ["ok", 3]])
def test_no_texts_a_bare_string_or_an_empty_text_is_invalid(texts: object) -> None:
    with pytest.raises(InvalidRequest):
        plan_batches(texts, spec())  # type: ignore[arg-type]


def test_a_spec_whose_context_exceeds_one_request_is_refused() -> None:
    with pytest.raises(ValueError, match="context_tokens must be <= max_tokens_per_request"):
        spec(context_tokens=31)


# ── Response checks ──────────────────────────────────────────────────────────────────────────

UNIT = (0.6, 0.8)


def test_valid_vectors_pass_and_ints_become_floats() -> None:
    assert check_vectors([UNIT, (1, 0)], count=2, dimension=2) == (UNIT, (1.0, 0.0))


@pytest.mark.parametrize(
    "vectors",
    [
        [UNIT],  # one vector too few
        [UNIT, UNIT, UNIT],  # one too many
        [UNIT, (1.0,)],  # wrong dimension
        [UNIT, (math.nan, 1.0)],
        [UNIT, (math.inf, 0.0)],
        [UNIT, (1e200, 1e200)],  # the length overflows to infinity
        [UNIT, (0.5, 0.5)],  # length 0.707
        [UNIT, (0.0, 0.0)],
        [UNIT, (True, False)],  # a bool is not a number here
        [UNIT, ("0.6", "0.8")],
    ],
    ids=[
        "too-few",
        "too-many",
        "dimension",
        "nan",
        "inf",
        "overflow",
        "not-unit",
        "zero",
        "bool",
        "strings",
    ],
)
def test_bad_vectors_are_a_provider_error_that_echoes_nothing(vectors: list[object]) -> None:
    with pytest.raises(ProviderError) as caught:
        check_vectors(vectors, count=2, dimension=2)  # type: ignore[arg-type]

    assert str(caught.value) == "status_code=None, request_id=None, retryable=False"


def test_the_unit_length_tolerance() -> None:
    assert UNIT_NORM_TOLERANCE == 1e-3
    inside, outside = 1 + 0.9 * UNIT_NORM_TOLERANCE, 1 + 1.1 * UNIT_NORM_TOLERANCE
    assert check_vectors([(inside, 0.0)], count=1, dimension=2)
    with pytest.raises(ProviderError):
        check_vectors([(outside, 0.0)], count=1, dimension=2)


# ── Business id and errors ───────────────────────────────────────────────────────────────────


def test_a_business_id_is_checked_and_never_echoed() -> None:
    assert check_business_id("sunrise-clinic") == "sunrise-clinic"
    bad = "Canary Business 7f3a"  # a variable: the traceback quotes source lines
    with pytest.raises(InvalidRequest) as caught:
        check_business_id(bad)

    error = caught.value
    assert error.__context__ is None
    assert "Canary" not in "".join(traceback.format_exception(error))


def test_rate_limited_is_a_retryable_transient_with_retry_after() -> None:
    error = RateLimited(status_code=429, retry_after=1.5)

    assert isinstance(error, Transient)
    assert (error.retryable, error.retry_after) == (True, 1.5)
