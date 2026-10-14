"""The provider interface (`providers/base.py`) and the scripted fake (`providers/fake.py`)."""

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from vectapilot_api.gateway.errors import SpendLimitReached, Transient
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import (
    HANG,
    INJECTION_TEXT,
    INVALID_JSON_TEXT,
    OTHER_MODEL,
    VERY_LONG_TEXT,
    FakeProvider,
    FakeReply,
)
from vectapilot_api.gateway.types import RedactedThinkingBlock, TextBlock, ThinkingBlock, Usage

CANARY = "canary-7f3a-do-not-log"  # stands in for prompt or completion text


def request(**overrides: Any) -> ProviderRequest:
    fields: dict[str, Any] = {
        "model": "model-a",
        "effort": "low",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "When do you open on Sunday?"}],
    }
    return ProviderRequest.model_validate(fields | overrides)


def test_the_fake_implements_the_provider_protocol() -> None:
    provider: Provider = FakeProvider()  # mypy checks this assignment against the Protocol

    assert provider.send is not None


def test_a_provider_request_has_no_temperature() -> None:
    # ADR 0006: temperature is never sent, so the request type has no such field.
    with pytest.raises(ValidationError):
        request(temperature=0.2)


@pytest.mark.anyio
async def test_replies_play_in_order_and_every_request_is_recorded() -> None:
    provider = FakeProvider([FakeReply(text="first"), FakeReply(text="second")])
    first, second = request(max_tokens=100), request(effort="medium", max_tokens=50)

    answers = [await provider.send(first), await provider.send(second)]

    assert [answer.content for answer in answers] == [
        (TextBlock(text="first"),),
        (TextBlock(text="second"),),
    ]
    assert provider.calls == [first, second]
    assert [(c.effort, c.max_tokens) for c in provider.calls] == [("low", 100), ("medium", 50)]
    assert [answer.request_id for answer in answers] == ["fake-req-1", "fake-req-2"]
    assert answers[0].model == "model-a"  # answers as the requested model by default
    assert (answers[0].stop_reason, answers[0].inference_geo) == ("end_turn", "global")


@pytest.mark.anyio
async def test_every_reply_field_passes_through() -> None:
    usage = Usage(input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_1h_tokens=4)
    reply = FakeReply(stop_reason="refusal", usage=usage, model=OTHER_MODEL, inference_geo="us")

    answer = await FakeProvider([reply]).send(request())

    assert isinstance(answer, ProviderResponse)
    assert (answer.stop_reason, answer.usage, answer.inference_geo) == ("refusal", usage, "us")
    assert answer.model == OTHER_MODEL != request().model


@pytest.mark.anyio
async def test_a_scripted_error_is_raised_unchanged_and_the_request_recorded() -> None:
    error = SpendLimitReached(status_code=429, request_id="req_1")
    provider = FakeProvider([error, Transient(status_code=529), FakeReply()])

    with pytest.raises(SpendLimitReached) as first:
        await provider.send(request())
    with pytest.raises(Transient):
        await provider.send(request())
    await provider.send(request())

    assert first.value is error
    assert first.value.__context__ is None
    assert len(provider.calls) == 3


@pytest.mark.anyio
async def test_a_hang_waits_until_it_is_cancelled() -> None:
    provider = FakeProvider([HANG, FakeReply(text="after the hang")])

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await provider.send(request())
    answer = await provider.send(request())

    assert answer.content == (TextBlock(text="after the hang"),)
    assert len(provider.calls) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text",
    [VERY_LONG_TEXT, INJECTION_TEXT, INVALID_JSON_TEXT, ""],
    ids=["very-long", "injection", "invalid-json", "empty"],
)
async def test_hostile_text_comes_back_unchanged(text: str) -> None:
    answer = await FakeProvider([FakeReply(text=text)]).send(request())

    assert answer.content == (TextBlock(text=text),)


@pytest.mark.anyio
async def test_thinking_blocks_come_before_the_text() -> None:
    answer = await FakeProvider([FakeReply(thinking=True)]).send(request())

    assert [type(block) for block in answer.content] == [
        ThinkingBlock,
        RedactedThinkingBlock,
        TextBlock,
    ]


@pytest.mark.anyio
async def test_a_call_beyond_the_script_fails_the_test() -> None:
    provider = FakeProvider()

    with pytest.raises(AssertionError, match="call 1 has no scripted step"):
        await provider.send(request())
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_request_and_response_text_stays_out_of_repr() -> None:
    sent = request(system=CANARY, messages=[{"role": "user", "content": CANARY}])
    reply = FakeReply(text=CANARY, thinking=True)
    answer = await FakeProvider([reply]).send(sent)

    assert CANARY not in repr(sent)
    assert CANARY not in repr(answer)
    assert CANARY not in repr(reply)  # a failing assert that prints the script shows no text
