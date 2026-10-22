"""The eval run's cap over embeddings and the answer model (`evalrun/budget.py`), the embedding
prices it reserves with (`embeddings/models.toml`), and M1's reservation formula as the harness
calls it (`measure.budget.request_reservation`). No network, no key: fakes only."""

import io
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from tests.embeddings.scripted import FAKE_SPEC, ScriptedEmbedder
from vectapilot_api.answer.types import ModelAnswer
from vectapilot_api.embeddings.catalog import (
    EmbeddingCatalog,
    EmbeddingCatalogError,
    load_embedding_catalog,
)
from vectapilot_api.evalrun.budget import (
    CappedEmbedder,
    CappedGateway,
    CapReached,
    CostCap,
    embedding_reservation,
    prompt_bytes,
    record_for,
    usd,
)
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import BillingError, Transient
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply, FakeStep
from vectapilot_api.gateway.types import LLMRequest, Message, RetryPolicy, Usage
from vectapilot_api.llm_cli import LogCapture
from vectapilot_api.measure.budget import request_reservation, reserve
from vectapilot_api.measure.dataset import load_cases
from vectapilot_api.measure.schemas import SCHEMAS

SMOKE: Final = Path(__file__).parents[4] / "evals" / "datasets" / "gateway_smoke" / "v0.1"
TRACE: Final = "0123456789abcdef0123456789abcdef"
REPLY: Final = '{"outcome": "answered", "answer": "TEST-ONLY", "citations": []}'

# ── Embedding prices (ADR 0008's Facts, checked 2026-10-21) ──────────────────────────────────


def test_the_catalog_carries_adr_0008s_list_prices_as_decimal() -> None:
    catalog = load_embedding_catalog()

    assert catalog.price("voyage-4") == Decimal("0.06")
    assert catalog.price("voyage-4-lite") == Decimal("0.02")
    assert catalog.price("fake-embedding-1024") == Decimal(0)
    assert all(isinstance(m.usd_per_million_tokens, Decimal) for m in catalog.models.values())
    assert catalog.models["voyage-4"].price_source_url == "https://docs.voyageai.com/docs/pricing"


def test_a_paid_model_without_a_price_is_refused() -> None:
    data = load_embedding_catalog().model_dump(mode="json")
    data["models"]["voyage-4"]["usd_per_million_tokens"] = "0"

    with pytest.raises(ValueError, match="a paid model's usd_per_million_tokens must be > 0"):
        EmbeddingCatalog.model_validate(data)


def test_an_unknown_model_has_no_price() -> None:
    with pytest.raises(EmbeddingCatalogError, match=r"must be a model in models\.toml"):
        load_embedding_catalog().price("voyage-9")


# ── The cap ──────────────────────────────────────────────────────────────────────────────────


def test_the_cap_fits_up_to_and_including_its_amount() -> None:
    cap = CostCap(cap=Decimal("0.10"), embedding_usd=Decimal("0.04"), llm_usd=Decimal("0.05"))

    assert cap.fits(Decimal("0.01")) and not cap.fits(Decimal("0.011"))
    assert CostCap(cap=None).fits(Decimal(1000))
    with pytest.raises(CapReached, match=r"worst case \$0.02 on top of the \$0.09 counted"):
        cap.require(Decimal("0.02"), "an embedding request")


def test_the_first_stop_reason_wins() -> None:
    cap = CostCap(cap=None)
    cap.stop_after("first")
    cap.stop_after("second")

    assert cap.stop == "first"


def test_usd_is_fixed_point_text() -> None:
    assert (usd(Decimal("7.2E-7")), usd(Decimal(0)), usd(Decimal("0.50"))) == (
        "$0.00000072",
        "$0",
        "$0.5",
    )


# ── Embeddings ───────────────────────────────────────────────────────────────────────────────


def test_an_embedding_reservation_is_the_token_bound_times_the_price() -> None:
    # "abcd" is 4 UTF-8 bytes + 8 = 12 tokens; "हि" is 6 bytes + 8 = 14. 26 x $0.06 / 1M.
    tokens, amount = embedding_reservation(["abcd", "हि"], Decimal("0.06"))

    assert tokens == 26
    assert amount == Decimal("0.00000156")


@pytest.mark.anyio
async def test_a_request_that_fits_is_sent_and_counted_at_the_reported_tokens() -> None:
    cap = CostCap(cap=Decimal("1.00"))
    inner = ScriptedEmbedder(reported_tokens=5)
    embedder = CappedEmbedder(inner, Decimal("0.06"), cap)

    result = await embedder.embed(["abcd"], input_type="query", business_id="test-only-a")

    assert len(result.vectors) == 1 and inner.batches == [["abcd"]]
    assert cap.embedding_usd == Decimal("0.0000003")  # 5 x 0.06 / 1M
    assert (cap.embedding_requests, cap.stop) == (1, None)
    assert (embedder.model, embedder.dimension) == (FAKE_SPEC.model, FAKE_SPEC.dimension)


@pytest.mark.anyio
async def test_a_request_that_would_cross_the_cap_is_never_sent() -> None:
    cap = CostCap(cap=Decimal("0.0000007"))  # the reservation is 0.00000072
    inner = ScriptedEmbedder()

    with pytest.raises(CapReached, match="an embedding request not sent"):
        await CappedEmbedder(inner, Decimal("0.06"), cap).embed(
            ["abcd"], input_type="query", business_id="test-only-a"
        )
    assert inner.batches == [] and cap.total == 0 and cap.embedding_requests == 0


@pytest.mark.anyio
async def test_a_failed_request_keeps_its_whole_reservation() -> None:
    cap = CostCap(cap=Decimal("1.00"))
    inner = ScriptedEmbedder(script=[Transient()])

    with pytest.raises(Transient):
        await CappedEmbedder(inner, Decimal("0.06"), cap).embed(
            ["abcd"], input_type="document", business_id="test-only-a"
        )
    assert cap.embedding_usd == Decimal("0.00000072")


@pytest.mark.anyio
async def test_tokens_above_the_bound_stop_the_run_after_the_request() -> None:
    cap = CostCap(cap=Decimal("1.00"))
    embedder = CappedEmbedder(ScriptedEmbedder(reported_tokens=13), Decimal("0.06"), cap)

    await embedder.embed(["abcd"], input_type="query", business_id="test-only-a")

    assert cap.stop is not None and "beyond its token bound" in cap.stop


@pytest.mark.anyio
async def test_closing_the_capped_embedder_closes_the_inner_one() -> None:
    inner = ScriptedEmbedder()
    await CappedEmbedder(inner, Decimal(0), CostCap(cap=None)).aclose()

    assert inner.closed


# ── The answer model ─────────────────────────────────────────────────────────────────────────


def test_request_reservation_is_reserve_for_a_smoke_case() -> None:
    # The refactor kept M1's formula: a smoke case reserves exactly as before.
    catalog = load_catalog()
    for case in load_cases((SMOKE / "cases.jsonl").read_text("utf-8")):
        schema = None if case.schema_name is None else SCHEMAS[case.schema_name]
        assert reserve(
            case, schema, catalog=catalog, max_retries=2, max_repairs=1
        ) == request_reservation(
            case_id=case.id,
            alias_name=case.alias,
            prompt_bytes=len(case.prompt.encode("utf-8")),
            schema=schema,
            catalog=catalog,
            max_retries=2,
            max_repairs=1,
        )


def request(system: str = "TEST-ONLY system", user: str = "TEST-ONLY user") -> LLMRequest:
    return LLMRequest(
        business_id="test-only-a",
        alias="large",
        system=system,
        messages=(Message(role="user", content=user),),
        trace_id=TRACE,
        operation="rag-answer",
    )


def test_prompt_bytes_counts_the_system_prompt_and_every_message() -> None:
    assert prompt_bytes(request("ab", "हि")) == 2 + 6


@pytest.fixture
def capture() -> Iterator[LogCapture]:
    handler = LogCapture(io.StringIO())
    gateway_logger.addHandler(handler)
    try:
        yield handler
    finally:
        gateway_logger.removeHandler(handler)


def gateway(
    script: list[FakeStep], cap: CostCap, capture: LogCapture, *, max_retries: int = 0
) -> tuple[CappedGateway, FakeProvider]:
    provider = FakeProvider(script)
    capped = CappedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=max_retries, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
        cap=cap,
        records=lambda trace_id: record_for(capture.lines, trace_id),
    )
    return capped, provider


@pytest.mark.anyio
async def test_the_answer_call_reserves_m1s_worst_case_for_its_exact_request(
    capture: LogCapture,
) -> None:
    cap = CostCap(cap=Decimal("1.00"))
    capped, provider = gateway([FakeReply(text=REPLY)], cap, capture, max_retries=2)
    expected = request_reservation(
        case_id=TRACE,
        alias_name="large",
        prompt_bytes=prompt_bytes(request()),
        schema=ModelAnswer,
        catalog=load_catalog(),
        max_retries=2,
        max_repairs=1,
    )

    assert capped.reservation(request(), ModelAnswer) == expected
    result = await capped.complete_structured(request(), ModelAnswer)

    assert result.value.outcome == "answered" and len(provider.calls) == 1
    record = record_for(capture.lines, TRACE)
    assert record is not None and record.cost_complete
    assert (cap.llm_usd, cap.llm_calls, cap.stop) == (record.cost_usd, 1, None)


@pytest.mark.anyio
async def test_an_answer_call_that_would_cross_the_cap_is_never_made(
    capture: LogCapture,
) -> None:
    cap = CostCap(cap=Decimal("0.001"))  # far below a `large` call's worst case
    capped, provider = gateway([FakeReply(text=REPLY)], cap, capture)

    with pytest.raises(CapReached, match="an answer-model call not sent"):
        await capped.complete_structured(request(), ModelAnswer)
    assert provider.calls == [] and cap.total == 0 and capture.lines == []


@pytest.mark.anyio
async def test_an_account_outcome_stops_the_run_after_the_call(capture: LogCapture) -> None:
    cap = CostCap(cap=Decimal("1.00"))
    capped, _ = gateway([BillingError()], cap, capture)

    with pytest.raises(BillingError):
        await capped.complete_structured(request(), ModelAnswer)
    assert cap.stop is not None and "billing" in cap.stop


@pytest.mark.anyio
async def test_the_us_geo_stops_the_run_and_the_call_keeps_its_reservation(
    capture: LogCapture,
) -> None:
    cap = CostCap(cap=Decimal("1.00"))
    capped, _ = gateway([FakeReply(text=REPLY, inference_geo="us")], cap, capture)

    await capped.complete_structured(request(), ModelAnswer)

    assert cap.stop is not None and "us geo" in cap.stop
    assert not cap.llm_cost_complete
    assert cap.llm_usd == capped.reservation(request(), ModelAnswer).usd


@pytest.mark.anyio
async def test_a_call_billed_beyond_its_bound_stops_the_run(capture: LogCapture) -> None:
    cap = CostCap(cap=Decimal("1.00"))
    huge = Usage(input_tokens=10_000_000, output_tokens=5)
    capped, _ = gateway([FakeReply(text=REPLY, usage=huge)], cap, capture)

    await capped.complete_structured(request(), ModelAnswer)

    assert cap.stop is not None and "beyond its worst-case bound" in cap.stop


@pytest.mark.anyio
async def test_a_call_without_its_record_keeps_its_reservation_and_stops(
    capture: LogCapture,
) -> None:
    cap = CostCap(cap=Decimal("1.00"))
    provider = FakeProvider([FakeReply(text=REPLY)])
    capped = CappedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=0, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
        cap=cap,
        records=lambda trace_id: None,
    )

    await capped.complete(request())

    assert cap.llm_usd == capped.reservation(request(), None).usd
    assert cap.stop is not None and "no single llm_call record" in cap.stop


def test_record_for_finds_exactly_one_record_or_none() -> None:
    assert record_for(["not json"], TRACE) is None
    assert record_for([], None) is None
