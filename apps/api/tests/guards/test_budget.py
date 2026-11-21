"""M7 PR 5 (G9): `BudgetedGateway` and its builders, over the fake provider and `FakeLimiter`.

Every model-facing test asserts what the fake provider received (non-vacuity): a refused call
must reach no provider, and an allowed one must.
"""

import asyncio
import inspect
import logging
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel

from tests.conftest import make_settings
from vectapilot_api.agent import llm as agent_llm
from vectapilot_api.evalrun.budget import prompt_bytes as evalrun_prompt_bytes
from vectapilot_api.gateway import gateway as plain_gateway
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import GatewayError, InvalidRequest
from vectapilot_api.gateway.gateway import LLM_CALL, LLMGateway
from vectapilot_api.gateway.providers.fake import HANG, FakeProvider, FakeReply
from vectapilot_api.gateway.types import LLMRequest, RetryPolicy, Usage
from vectapilot_api.guards.budget import (
    BudgetedGateway,
    BudgetTally,
    FallbackRefused,
    RefusalAsRouterFailure,
    build_budgeted_agent_gateway,
    build_budgeted_gateway,
    prompt_bytes,
    track_budget,
)
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import GuardRefused, RefusalKind, Reserved, micro_usd
from vectapilot_api.measure.budget import request_reservation

pytestmark = pytest.mark.anyio


class SCHEMA(BaseModel):
    """A short answer."""

    answer: str


def request(**overrides: Any) -> LLMRequest:
    fields: dict[str, Any] = {
        "business_id": "rt-shop",
        "alias": "small",
        "system": "You answer questions about a shop.",
        "messages": [{"role": "user", "content": "Where is my order?"}],
    }
    return LLMRequest.model_validate(fields | overrides)


def budgeted(provider: FakeProvider, limiter: FakeLimiter) -> BudgetedGateway:
    return BudgetedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay_s=0.01, max_delay_s=0.01),
        max_repairs=1,
        limiter=limiter,
        rand=lambda: 0.0,
    )


def worst_case(gateway: BudgetedGateway, req: LLMRequest, schema: Any = None) -> int:
    return micro_usd(
        request_reservation(
            case_id="x",
            alias_name=req.alias,
            prompt_bytes=prompt_bytes(req),
            schema=schema,
            catalog=load_catalog(),
            max_retries=2,
            max_repairs=1,
        ).usd
    )


def test_prompt_bytes_equals_the_eval_harness_copy() -> None:
    for req in (
        request(),
        request(system=None),
        request(system="सीने में दर्द " * 7, messages=[{"role": "user", "content": "€ 3 ✓"}]),
    ):
        assert prompt_bytes(req) == evalrun_prompt_bytes(req)


def test_the_budget_module_imports_nothing_from_evalrun() -> None:
    from vectapilot_api.guards import budget

    assert "evalrun" not in inspect.getsource(budget).replace("`evalrun", "")


async def test_a_call_reserves_its_worst_case_first_then_settles_its_cost() -> None:
    provider = FakeProvider([FakeReply(usage=Usage(input_tokens=100, output_tokens=50))])
    limiter = FakeLimiter()
    gateway = budgeted(provider, limiter)
    req = request()
    reserved = gateway.reservation_micro_usd(req, None)

    with track_budget() as tally:
        result = await gateway.complete(req)

    assert len(provider.calls) == 1
    assert reserved == worst_case(gateway, req) > micro_usd(result.cost_usd) > 0
    assert result.cost_complete
    assert tally == BudgetTally(reserved, micro_usd(result.cost_usd), 0)
    assert [c.operation for c in limiter.calls] == ["reserve", "settle"]
    assert limiter.spent["fake:rt-shop"] == micro_usd(result.cost_usd)


async def test_an_incomplete_cost_keeps_at_least_the_reservation() -> None:
    provider = FakeProvider([FakeReply(inference_geo=None)])  # no geo: the cost is a lower bound
    limiter = FakeLimiter()
    gateway = budgeted(provider, limiter)

    with track_budget() as tally:
        result = await gateway.complete(request())

    assert not result.cost_complete
    assert tally.settled_micro_usd == tally.reserved_micro_usd > micro_usd(result.cost_usd)
    assert limiter.spent["fake:rt-shop"] == tally.reserved_micro_usd


async def test_a_structured_call_reserves_the_repair_too() -> None:
    provider = FakeProvider([FakeReply(text="not json"), FakeReply(text="still not json")])
    limiter = FakeLimiter()
    gateway = budgeted(provider, limiter)
    req = request()

    with track_budget() as tally, pytest.raises(GatewayError):
        await gateway.complete_structured(req, SCHEMA)

    assert len(provider.calls) == 2  # the first request and its one repair
    assert tally.reserved_micro_usd == worst_case(gateway, req, SCHEMA)
    assert tally.reserved_micro_usd > worst_case(gateway, req)
    # A GatewayError carries no cost: the whole reservation is kept, with no settle call.
    assert tally.settled_micro_usd == tally.reserved_micro_usd
    assert [c.operation for c in limiter.calls] == ["reserve"]


async def test_a_structured_call_settles_like_a_plain_one() -> None:
    provider = FakeProvider([FakeReply(text='{"answer": "Tuesday at 10."}')])
    limiter = FakeLimiter()

    with track_budget() as tally:
        structured = await budgeted(provider, limiter).complete_structured(request(), SCHEMA)

    assert len(provider.calls) == 1
    assert tally.settled_micro_usd == micro_usd(structured.result.cost_usd)
    assert [c.operation for c in limiter.calls] == ["reserve", "settle"]


async def test_a_refused_reservation_never_reaches_the_provider_or_writes_a_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    provider = FakeProvider([FakeReply()])
    gateway = budgeted(provider, FakeLimiter(budget="refuse"))

    with track_budget() as tally:
        with pytest.raises(GuardRefused) as plain:
            await gateway.complete(request())
        with pytest.raises(GuardRefused) as structured:
            await gateway.complete_structured(request(), SCHEMA)

    assert plain.value.kind is structured.value.kind is RefusalKind.BUDGET
    assert provider.calls == []
    assert tally == BudgetTally(0, 0, 2)
    assert [r for r in caplog.records if r.getMessage() == LLM_CALL] == []


async def test_redis_down_refuses_the_call_as_unavailable() -> None:
    provider = FakeProvider([FakeReply()])
    with pytest.raises(GuardRefused) as caught:
        await budgeted(provider, FakeLimiter(budget="raise")).complete(request())
    assert (caught.value.kind, caught.value.error_type) == (
        RefusalKind.UNAVAILABLE,
        "ConnectionError",
    )
    assert provider.calls == []


async def test_a_failed_call_keeps_its_whole_reservation_and_its_own_exception() -> None:
    provider = FakeProvider([InvalidRequest(status_code=400)])
    limiter = FakeLimiter()

    with track_budget() as tally, pytest.raises(InvalidRequest):
        await budgeted(provider, limiter).complete(request())

    assert len(provider.calls) == 1
    assert tally.settled_micro_usd == tally.reserved_micro_usd > 0
    assert limiter.spent["fake:rt-shop"] == tally.reserved_micro_usd


class SettleFails(FakeLimiter):
    async def settle(self, reserved: Reserved, spent_micro_usd: int) -> None:
        raise GuardRefused(RefusalKind.UNAVAILABLE, error_type="TimeoutError")


async def test_a_failed_settle_never_replaces_the_result_and_keeps_the_reservation() -> None:
    provider = FakeProvider([FakeReply()])

    with track_budget() as tally:
        result = await budgeted(provider, SettleFails()).complete(request())

    assert result.text == "A scripted fake reply."
    assert tally.settled_micro_usd == tally.reserved_micro_usd


async def test_an_unknown_alias_reserves_nothing_and_the_gateway_rejects_it_before_any_call() -> (
    None
):
    provider = FakeProvider([FakeReply()])
    limiter = FakeLimiter()
    gateway = budgeted(provider, limiter)

    with track_budget() as tally:
        with pytest.raises(GatewayError):
            await gateway.complete(request(alias="no-such-alias"))
        with pytest.raises(GatewayError):
            await gateway.complete_structured(request(alias="no-such-alias"), SCHEMA)

    assert provider.calls == [] and limiter.calls == []
    assert tally == BudgetTally()


async def test_outside_a_tracked_request_calls_still_reserve_and_settle() -> None:
    limiter = FakeLimiter()
    await budgeted(FakeProvider([FakeReply()]), limiter).complete(request())
    assert [c.operation for c in limiter.calls] == ["reserve", "settle"]


async def test_one_tally_sums_a_turn_across_the_small_and_large_gateways() -> None:
    limiter = FakeLimiter()
    small = budgeted(FakeProvider([FakeReply(), FakeReply()]), limiter)
    large = budgeted(FakeProvider([FakeReply()]), limiter)
    step, answer = request(), request(alias="large")

    with track_budget() as tally:
        await small.complete(step)
        await large.complete(answer)
        await small.complete(step)
    with track_budget() as other:
        pass

    expected = 2 * small.reservation_micro_usd(step, None) + large.reservation_micro_usd(
        answer, None
    )
    assert tally.reserved_micro_usd == expected
    assert large.reservation_micro_usd(answer, None) > small.reservation_micro_usd(step, None)
    assert other == BudgetTally()  # each request starts at zero


# ── Builders (G9: mirror the plain builders argument for argument) ───────────────────────────


def _parameters(function: Any) -> list[tuple[str, Any, Any]]:
    signature = inspect.signature(inspect.unwrap(function))
    return [(p.name, p.kind, p.default) for p in signature.parameters.values()]


@pytest.mark.parametrize(
    ("budgeted_builder", "plain_builder"),
    [
        (build_budgeted_agent_gateway, agent_llm.build_agent_gateway),
        (build_budgeted_gateway, plain_gateway.build_gateway),
    ],
)
def test_each_builder_takes_the_plain_builders_arguments_plus_the_limiter(
    budgeted_builder: Any, plain_builder: Any
) -> None:
    budgeted_params = _parameters(budgeted_builder)
    assert budgeted_params[:-1] == _parameters(plain_builder)
    assert budgeted_params[-1][:2] == ("limiter", inspect.Parameter.KEYWORD_ONLY)


def _limits(gateway: LLMGateway) -> tuple[object, ...]:
    return (
        gateway._attempt_timeout_s,  # private, on purpose: the mirror is the point of the test
        gateway._deadline_s,
        gateway._retry_policy,
        gateway._max_repairs,
        gateway._catalog,
    )


@pytest.mark.parametrize(
    ("budgeted_builder", "plain_builder"),
    [
        (build_budgeted_agent_gateway, agent_llm.build_agent_gateway),
        (build_budgeted_gateway, plain_gateway.build_gateway),
    ],
)
async def test_each_builder_builds_the_plain_gateways_limits_and_closes_its_provider(
    budgeted_builder: Any, plain_builder: Any
) -> None:
    settings = make_settings(
        agent_llm_attempt_timeout_seconds=5.0, llm_attempt_timeout_seconds=12.0
    )
    limiter = FakeLimiter()
    provider, plain_provider = FakeProvider(), FakeProvider()
    async with (
        budgeted_builder(settings, provider=provider, limiter=limiter) as gateway,
        plain_builder(settings, provider=plain_provider) as plain,
    ):
        assert isinstance(gateway, BudgetedGateway)
        assert _limits(gateway) == _limits(plain)
        assert gateway._limiter is limiter
    assert provider.closed and plain_provider.closed


async def test_the_agent_builder_refuses_a_catalog_without_the_agent_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vectapilot_api.guards import budget

    catalog = load_catalog()
    without = catalog.model_copy(
        update={"aliases": {k: v for k, v in catalog.aliases.items() if k != "small"}}
    )
    monkeypatch.setattr(budget, "load_catalog", lambda: without)
    with pytest.raises(ValueError, match="no 'small' alias"):
        async with build_budgeted_agent_gateway(make_settings(), limiter=FakeLimiter()):
            pass


# ── Mutation-check probes (PR 5 review) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("structured", [False, True])
async def test_a_cancelled_call_keeps_its_whole_reservation(structured: bool) -> None:
    # A turn timeout or a client disconnect cancels the call after the provider may have billed
    # it: the reservation stays counted (`except BaseException`, not `Exception`).
    provider = FakeProvider([HANG])
    limiter = FakeLimiter()
    gateway = budgeted(provider, limiter)
    with track_budget() as tally:
        call = (
            gateway.complete_structured(request(), SCHEMA)
            if structured
            else gateway.complete(request())
        )
        task = asyncio.create_task(call)
        await asyncio.sleep(0.05)
        assert len(provider.calls) == 1  # reserved, sent, and hanging
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert tally.settled_micro_usd == tally.reserved_micro_usd > 0
    assert [c.operation for c in limiter.calls] == ["reserve"]


async def test_the_reservation_is_keyed_by_the_requests_business() -> None:
    # G12: every other test uses 'rt-shop'; a call never charges another business's budget.
    limiter = FakeLimiter()
    await budgeted(FakeProvider([FakeReply()]), limiter).complete(
        request(business_id="paws-and-co")
    )
    assert {c.business_id for c in limiter.calls} == {"paws-and-co"}
    assert set(limiter.spent) == {"fake:paws-and-co"}


async def test_the_tally_ends_with_its_block() -> None:
    # G10: a later untracked call never adds to an earlier request's guard record.
    limiter = FakeLimiter()
    with track_budget() as tally:
        pass
    await budgeted(FakeProvider([FakeReply()]), limiter).complete(request())
    assert tally == BudgetTally()
    assert [c.operation for c in limiter.calls] == ["reserve", "settle"]  # the call did reserve


async def test_a_settle_follows_the_budget_script_without_consuming_it() -> None:
    fake = FakeLimiter(budget=["allow", "allow", "refuse"])
    reserved = await fake.reserve("rt-shop", 10)
    await fake.settle(reserved, 4)
    await fake.reserve("rt-shop", 10)  # the second "allow": a settle consumed nothing
    with pytest.raises(GuardRefused):
        await fake.reserve("rt-shop", 10)


# ── The fake limiter itself ──────────────────────────────────────────────────────────────────


async def test_the_fake_follows_its_script_then_repeats_the_last_mode() -> None:
    from vectapilot_api.guards.types import RouteSlug

    fake = FakeLimiter(limit=["allow", "refuse", "raise", "allow"], retry_after_s=7)
    await fake.admit("rt-shop", RouteSlug.AGENT, "cust-001")
    with pytest.raises(GuardRefused) as limit:
        await fake.admit("rt-shop", RouteSlug.AGENT)
    with pytest.raises(GuardRefused) as down:
        await fake.admit("rt-shop", RouteSlug.AGENT)
    await fake.admit("rt-shop", RouteSlug.AGENT)
    await fake.admit("rt-shop", RouteSlug.AGENT)
    assert (limit.value.kind, limit.value.retry_after_s) == (RefusalKind.LIMIT, 7)
    assert down.value.kind is RefusalKind.UNAVAILABLE
    assert fake.calls[0] == LimiterCall("admit", "rt-shop", RouteSlug.AGENT, customered=True)
    assert fake.calls[1].customered is False
    with pytest.raises(ValueError, match="at least one mode"):
        FakeLimiter(limit=[])


async def test_the_fakes_budget_modes() -> None:
    assert await FakeLimiter(remaining_micro_usd=42).remaining("rt-shop") == 42
    assert await FakeLimiter(budget="refuse").remaining("rt-shop") == 0
    with pytest.raises(GuardRefused):
        await FakeLimiter(budget="raise").remaining("rt-shop")
    with pytest.raises(GuardRefused):
        await FakeLimiter(budget="raise").settle(Reserved("fake:rt-shop", 1), 1)
    fake = FakeLimiter()
    reserved = await fake.reserve("rt-shop", 10)
    await fake.settle(reserved, 4)
    assert fake.spent == {"fake:rt-shop": 4}


def test_the_fakes_cost_numbers_are_decimal_safe() -> None:
    assert micro_usd(Decimal("0.0000005")) == 1


def test_every_model_calling_method_of_the_gateway_is_overridden() -> None:
    # G13: a new entry point on `LLMGateway` must not skip the budget. `aclose` calls no model.
    public = {
        name
        for name, member in inspect.getmembers(LLMGateway, inspect.iscoroutinefunction)
        if not name.startswith("_")
    }
    assert public == {"complete", "complete_structured", "aclose"}
    for name in public - {"aclose"}:
        assert getattr(BudgetedGateway, name) is not getattr(LLMGateway, name)


# ── The router's fallback (kenji's PR 6 review, G2) ──────────────────────────────────────────


class StubFallback:
    """NOT M4's fallback: answers `intent`, or raises `error`."""

    def __init__(self, intent: Any = None, error: BaseException | None = None) -> None:
        self.intent, self.error = intent, error

    async def __call__(self, message: str, *, business_id: str, trace_id: str) -> Any:
        if self.error is not None:
            raise self.error
        return self.intent


@pytest.mark.parametrize("kind", [RefusalKind.BUDGET, RefusalKind.UNAVAILABLE])
async def test_a_refused_fallback_is_raised_as_a_router_failure_with_nothing_chained(
    kind: RefusalKind,
) -> None:
    error_type = "ConnectionError" if kind is RefusalKind.UNAVAILABLE else None
    refused = GuardRefused(kind, error_type=error_type)
    fallback = RefusalAsRouterFailure(StubFallback(error=refused))

    with pytest.raises(FallbackRefused) as raised:
        await fallback("Is it open?", business_id="rt-shop", trace_id="0" * 32)

    # Neither a guard refusal the route answers nor a model failure M4 enumerates.
    assert not isinstance(raised.value, GuardRefused | GatewayError)
    assert (raised.value.__context__, raised.value.__cause__) == (None, None)
    assert str(raised.value) == "the router's fallback call was refused"


async def test_every_other_answer_of_the_fallback_passes_unchanged() -> None:
    from vectapilot_api.intents.taxonomy import Intent

    assert (
        await RefusalAsRouterFailure(StubFallback(Intent.QUESTION))(
            "Is it open?", business_id="rt-shop", trace_id="0" * 32
        )
        is Intent.QUESTION
    )
    for error in (InvalidRequest(), ValueError("a caller bug"), asyncio.CancelledError()):
        with pytest.raises(type(error)) as raised:
            await RefusalAsRouterFailure(StubFallback(error=error))(
                "Is it open?", business_id="rt-shop", trace_id="0" * 32
            )
        assert raised.value is error


def test_create_app_wraps_the_router_fallback_only_while_the_guards_are_on() -> None:
    from vectapilot_api.intents.fallback import LLMIntentFallback
    from vectapilot_api.main import _router_fallback

    gateway = budgeted(FakeProvider(), FakeLimiter())

    guarded = _router_fallback(gateway, guarded=True)
    plain = _router_fallback(gateway, guarded=False)

    assert type(guarded) is RefusalAsRouterFailure
    assert type(guarded._fallback) is LLMIntentFallback
    assert guarded._fallback._gateway is gateway
    assert type(plain) is LLMIntentFallback
    assert plain._gateway is gateway
