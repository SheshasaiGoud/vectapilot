"""The embedding runner: batches, per-batch retries (H2) and one metadata-only record per call."""

import asyncio
import logging
from typing import Any

import pytest

from tests.embeddings.scripted import (
    FAKE_SPEC,
    POLICY,
    FakeClock,
    ScriptedEmbedder,
    small_spec,
)
from vectapilot_api.embeddings.base import InputTooLarge, RateLimited, token_upper_bound
from vectapilot_api.embeddings.fake import FakeEmbedder, fake_vector
from vectapilot_api.embeddings.runner import (
    EMBEDDING_CALL,
    EmbeddingRunner,
    batch_deadline_s,
)
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    InvalidRequest,
    ProviderError,
    Transient,
)
from vectapilot_api.gateway.types import RetryPolicy

pytestmark = pytest.mark.anyio

BUSINESS = "clinic-a"
PLANTED_TEXT = "canary-text-7f3a never in a record"


def runner_for(
    embedder: ScriptedEmbedder,
    clock: FakeClock,
    policy: RetryPolicy = POLICY,
    deadline_s: float = 100.0,
) -> EmbeddingRunner:
    return EmbeddingRunner(
        embedder,
        embedder._fake._spec,
        policy,
        deadline_s=deadline_s,
        clock=clock,
        sleep=clock.sleep,
        rand=lambda: 0.5,
    )


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, EMBEDDING_CALL) for r in caplog.records if r.getMessage() == EMBEDDING_CALL]


async def embed(runner: EmbeddingRunner, texts: list[str]) -> Any:
    return await runner.embed(
        texts, input_type="document", business_id=BUSINESS, operation="kb-ingest"
    )


async def test_batches_are_embedded_in_order_with_one_record_each(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    texts = ["one", "two", "three", PLANTED_TEXT, "five"]
    embedder = ScriptedEmbedder(small_spec(max_texts_per_request=2), reported_tokens=11)
    result = await embed(runner_for(embedder, FakeClock()), texts)

    assert embedder.batches == [texts[0:2], texts[2:4], texts[4:5]]
    assert result.vectors == tuple(fake_vector(t, 1024) for t in texts)
    assert (result.model, result.dimension, result.calls) == (FAKE_SPEC.model, 1024, 3)
    assert result.total_tokens == 33
    found = records(caplog)
    assert [r["inputs"] for r in found] == [2, 2, 1]
    assert found[1] == {
        "business_id": BUSINESS,
        "operation": "kb-ingest",
        "input_type": "document",
        "model_requested": FAKE_SPEC.model,
        "model_answered": FAKE_SPEC.model,
        "outcome": "ok",
        "inputs": 2,
        "token_estimate": token_upper_bound("three") + token_upper_bound(PLANTED_TEXT),
        "reported_tokens": 11,
        "attempts": 1,
        "retries": 0,
        "latency_ms": 0.0,
        "trace_id": None,
    }
    assert PLANTED_TEXT not in caplog.text  # metadata only


async def test_a_trace_id_goes_into_every_record_of_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder(small_spec(max_texts_per_request=1))
    trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"

    await runner_for(embedder, FakeClock()).embed(
        ["one", "two"],
        input_type="query",
        business_id=BUSINESS,
        operation="kb-search",
        trace_id=trace_id,
    )

    assert [(r["trace_id"], r["input_type"]) for r in records(caplog)] == [
        (trace_id, "query"),
        (trace_id, "query"),
    ]


@pytest.mark.parametrize(
    "trace_id", ["", "0" * 32, "4BF92F3577B34DA6A3CE929D0E0E4736", PLANTED_TEXT]
)
async def test_an_invalid_trace_id_is_refused_before_any_call(
    caplog: pytest.LogCaptureFixture, trace_id: str
) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder()

    with pytest.raises(InvalidRequest) as raised:
        await runner_for(embedder, FakeClock()).embed(
            ["one"],
            input_type="query",
            business_id=BUSINESS,
            operation="kb-search",
            trace_id=trace_id,
        )

    assert embedder.batches == [] and records(caplog) == []
    assert PLANTED_TEXT not in str(raised.value)


async def test_a_transient_failure_is_retried_with_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    clock = FakeClock()
    embedder = ScriptedEmbedder(script=[Transient(status_code=503), None])
    result = await embed(runner_for(embedder, clock), ["a text"])

    assert result.vectors == (fake_vector("a text", 1024),)
    assert clock.slept == [0.25]  # full jitter: rand 0.5 x the first bound 0.5 s
    [record] = records(caplog)
    assert (record["outcome"], record["attempts"], record["retries"]) == ("ok", 2, 1)
    assert record["latency_ms"] == 250.0


async def test_retry_after_is_waited_exactly() -> None:
    clock = FakeClock()
    embedder = ScriptedEmbedder(script=[RateLimited(status_code=429, retry_after=3.0), None])
    await embed(runner_for(embedder, clock), ["a text"])

    assert clock.slept == [3.0]


async def test_a_failed_batch_never_repeats_the_batches_before_it() -> None:
    embedder = ScriptedEmbedder(small_spec(max_texts_per_request=1), script=[None, Transient()])
    await embed(runner_for(embedder, FakeClock()), ["first", "second"])

    assert embedder.batches == [["first"], ["second"], ["second"]]


async def test_a_non_retryable_error_is_raised_after_one_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    error = AuthenticationFailed(status_code=401)
    embedder = ScriptedEmbedder(script=[error])
    with pytest.raises(AuthenticationFailed) as raised:
        await embed(runner_for(embedder, FakeClock()), ["a text"])

    assert raised.value is error
    assert len(embedder.batches) == 1
    [record] = records(caplog)
    assert record["outcome"] == "auth_failed"
    assert (record["model_answered"], record["reported_tokens"]) == (None, None)


async def test_retries_run_out(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder(script=[Transient(), Transient(), Transient()])
    with pytest.raises(Transient):
        await embed(runner_for(embedder, FakeClock()), ["a text"])

    [record] = records(caplog)
    assert (record["outcome"], record["attempts"], record["retries"]) == (
        "transient_exhausted",
        3,
        2,
    )


async def test_a_wait_past_the_deadline_is_not_slept() -> None:
    clock = FakeClock()
    embedder = ScriptedEmbedder(script=[RateLimited(retry_after=5.0), None])
    with pytest.raises(RateLimited):
        await embed(runner_for(embedder, clock, deadline_s=4.0), ["a text"])

    assert clock.slept == []


async def test_two_model_ids_in_one_call_are_refused() -> None:
    embedder = ScriptedEmbedder(
        small_spec(max_texts_per_request=1), answer_models=["model-a", "model-b"]
    )
    with pytest.raises(ProviderError):
        await embed(runner_for(embedder, FakeClock()), ["first", "second"])


@pytest.mark.parametrize(
    ("texts", "error"),
    [
        pytest.param([], InvalidRequest, id="no texts"),
        pytest.param(["x" * 40_000], InputTooLarge, id="a text over the context"),
    ],
)
async def test_a_text_that_cannot_be_sent_fails_before_any_call(
    caplog: pytest.LogCaptureFixture, texts: list[str], error: type[Exception]
) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder()
    with pytest.raises(error):
        await embed(runner_for(embedder, FakeClock()), texts)

    assert embedder.batches == []
    assert records(caplog) == []


async def test_an_invalid_business_is_refused_before_any_call() -> None:
    embedder = ScriptedEmbedder()
    with pytest.raises(InvalidRequest):
        await runner_for(embedder, FakeClock()).embed(
            ["a text"], input_type="query", business_id="Not A Slug", operation="kb-ingest"
        )

    assert embedder.batches == []


async def test_a_bug_in_the_embedder_is_recorded_as_internal_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder(script=[KeyError("boom")])
    with pytest.raises(KeyError):
        await embed(runner_for(embedder, FakeClock()), ["a text"])

    assert [r["outcome"] for r in records(caplog)] == ["internal_error"]


async def test_cancellation_is_recorded_and_propagates(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    embedder = ScriptedEmbedder(script=[asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await embed(runner_for(embedder, FakeClock()), ["a text"])

    assert [r["outcome"] for r in records(caplog)] == ["cancelled"]


def test_records_stay_on_when_logging_is_quieter() -> None:
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.ERROR)  # a quieter `log_level`
    try:
        assert logging.getLogger("vectapilot_api.embeddings").isEnabledFor(logging.INFO)
    finally:
        root.setLevel(before)


def test_the_runner_refuses_an_embedder_of_another_model() -> None:
    with pytest.raises(ValueError, match="same model"):
        EmbeddingRunner(
            FakeEmbedder(small_spec(dimension=1024)),
            FAKE_SPEC.model_copy(update={"model": "other-model"}),
            POLICY,
            deadline_s=1.0,
        )


def test_the_runner_exposes_its_model_and_dimension() -> None:
    runner = runner_for(ScriptedEmbedder(), FakeClock())

    assert (runner.model, runner.dimension) == (FAKE_SPEC.model, 1024)


def test_the_batch_deadline_covers_every_attempt_and_wait() -> None:
    # 3 attempts of 30 s, plus 2 waits of at most 8 s.
    assert batch_deadline_s(POLICY, request_timeout_s=30.0) == 106.0
