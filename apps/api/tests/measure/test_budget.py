"""PR 8: the `--live` cost cap (`measure/budget.py`): worst-case reservations, the bound check and
the ledger.

The arithmetic tests use made-up round prices, worked out by hand in the comments. One test pins
the real catalog's worst case for one dataset case, the example the step 8 hand-off quotes. The
allowance tests run every dataset case through the real gateway on the fake provider and check the
bound against the exact requests the gateway sends (`FakeProvider.calls`).
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel, ConfigDict

from vectapilot_api.gateway import gateway as gateway_module
from vectapilot_api.gateway.catalog import Catalog, load_catalog, parse_catalog
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.gateway import MAX_PROBLEMS, LLMGateway
from vectapilot_api.gateway.providers.base import ProviderRequest
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import CallRecord, LLMRequest, Message, RetryPolicy
from vectapilot_api.measure.budget import (
    MAX_USD_CEILING,
    REPAIR_FEEDBACK_BYTES,
    REQUEST_OVERHEAD_TOKENS,
    STRUCTURED_INSTRUCTION_BYTES,
    Ledger,
    Reservation,
    bound_held,
    reserve,
    total_usd,
)
from vectapilot_api.measure.dataset import SmokeCase, load_cases
from vectapilot_api.measure.schemas import SCHEMAS

DATASET: Final = Path(__file__).parents[4] / "evals" / "datasets" / "gateway_smoke" / "v0.1"
CASES: Final = load_cases((DATASET / "cases.jsonl").read_text("utf-8"))

# Made-up prices, per million tokens. The highest input-side price is cache_write_1h: 3 standard,
# 5 on the long-prompt tier (over 300 prompt tokens). Output: 2 standard, 4 long.
PRICES: Final[Catalog] = parse_catalog(
    """
catalog_version = "budget-test-1"

[models.m-tiered]
source_url = "https://example.invalid/prices"
checked = 2026-10-15
[models.m-tiered.standard]
input = 1
output = 2
cache_read = 0.5
cache_write_5m = 1.25
cache_write_1h = 3
[models.m-tiered.long_prompt]
threshold_tokens = 300
[models.m-tiered.long_prompt.prices]
input = 2
output = 4
cache_read = 1
cache_write_5m = 2.5
cache_write_1h = 5

[models.m-flat]
source_url = "https://example.invalid/prices"
checked = 2026-10-15
[models.m-flat.standard]
input = 1
output = 10
cache_read = 0.1
cache_write_5m = 1.25
cache_write_1h = 2

[aliases.tiered]
model = "m-tiered"
max_output_tokens = 100
effort = "low"

[aliases.flat]
model = "m-flat"
max_output_tokens = 1000
effort = "low"
"""
)


class Tiny(BaseModel):
    model_config = ConfigDict(extra="forbid")

    a: int


TINY_SCHEMA_JSON: Final = (
    '{"additionalProperties": false, "properties": {"a": {"title": "A", "type": "integer"}},'
    ' "required": ["a"], "title": "Tiny", "type": "object"}'
)


def case(**overrides: Any) -> SmokeCase:
    fields: dict[str, Any] = {
        "id": "budget-case",
        "business_id": "test-clinic",
        "alias": "tiered",
        "language": "en",
        "kind": "plain",
        "prompt": "hello",
        "fake_reply": "[dry run] hi",
    }
    return SmokeCase.model_validate(fields | overrides)


def record(**overrides: Any) -> CallRecord:
    """A valid `ok` record of one attempt, priced complete."""
    fields: dict[str, Any] = {
        "business_id": "test-clinic",
        "alias": "tiered",
        "model_requested": "m-tiered",
        "model_answered": "m-tiered",
        "catalog_version": "budget-test-1",
        "inference_geo": "global",
        "outcome": "ok",
        "stop_reason": "end_turn",
        "attempts": 1,
        "retries": 0,
        "repairs": 0,
        "attempt_timeouts": 0,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_5m_tokens": 0,
        "cache_write_1h_tokens": 0,
        "cost_usd": "0.00002",
        "cost_complete": True,
        "cache_hit": False,
        "latency_ms": 1.0,
    }
    return CallRecord.model_validate(fields | overrides)


# ── The worst case, by hand ──────────────────────────────────────────────────────────────────


def test_a_plain_call_by_hand() -> None:
    # "héllo wörld": 11 characters, 13 UTF-8 bytes (é and ö take 2 each).
    # P = 13 + 200 (overhead) = 213 tokens: not over 300, so the standard tier.
    # Per attempt: 213 x 3 (highest input-side price) + 100 x 2 (output) = 639 + 200 = 839.
    # 2 retries: 3 attempts x 839 = 2517 per million, so $0.002517.
    reservation = reserve(
        case(prompt="héllo wörld"), None, catalog=PRICES, max_retries=2, max_repairs=1
    )

    assert reservation == Reservation(
        case_id="budget-case",
        alias="tiered",
        model="m-tiered",
        attempts_per_request=3,
        prompt_tokens=(213,),
        output_tokens=100,
        usd=Decimal("0.002517"),
    )
    assert reservation.attempts == 3


def test_the_long_prompt_tier_counts_once_the_bound_is_over_its_threshold() -> None:
    # 100 bytes: P = 300, not over 300 -> standard: 300 x 3 + 100 x 2 = 1100 -> $0.0011.
    # 101 bytes: P = 301, over 300 -> each class at the higher tier: input-side max(3, 5) = 5,
    # output max(2, 4) = 4: 301 x 5 + 100 x 4 = 1505 + 400 = 1905 -> $0.001905.
    at = reserve(case(prompt="x" * 100), None, catalog=PRICES, max_retries=0, max_repairs=0)
    over = reserve(case(prompt="x" * 101), None, catalog=PRICES, max_retries=0, max_repairs=0)

    assert (at.prompt_tokens, at.usd) == ((300,), Decimal("0.0011"))
    assert (over.prompt_tokens, over.usd) == ((301,), Decimal("0.001905"))


def test_a_structured_call_reserves_the_schema_and_the_repair_by_hand() -> None:
    # The schema as `json.dumps` writes it with default separators, counted by hand: 32 up to
    # `false, `, then 14 + 6 + 15 + 18 + 3 for `properties` (88), 19 for `required` (107), 17 for
    # `title` (124) and 17 for `type` = 141 bytes.
    assert json.dumps(Tiny.model_json_schema()) == TINY_SCHEMA_JSON
    assert len(TINY_SCHEMA_JSON) == 141
    # First request: 5 (prompt) + 141 (schema) + 256 (instruction) + 200 (overhead) = 602.
    # Repair: 602 + 1024 (feedback) = 1626. Model m-flat (no long tier): input-side max 2,
    # output 10, max_output_tokens 1000.
    # First: 602 x 2 + 1000 x 10 = 11204; repair: 1626 x 2 + 10000 = 13252.
    # 1 retry: 2 attempts each: 2 x (11204 + 13252) = 48912 per million, so $0.048912.
    structured = case(alias="flat", kind="structured", schema_name="IntentLabel")

    reservation = reserve(structured, Tiny, catalog=PRICES, max_retries=1, max_repairs=1)

    assert reservation.prompt_tokens == (602, 1626)
    assert reservation.usd == Decimal("0.048912")
    assert reservation.attempts == 4  # 2 requests x 2 attempts


def test_no_repair_is_reserved_when_repairs_are_off() -> None:
    # As above without the repair: 2 x 11204 = 22408 -> $0.022408.
    structured = case(alias="flat", kind="structured", schema_name="IntentLabel")

    reservation = reserve(structured, Tiny, catalog=PRICES, max_retries=1, max_repairs=0)

    assert reservation.prompt_tokens == (602,)
    assert reservation.usd == Decimal("0.022408")


def test_the_real_catalog_worst_case_of_one_dataset_case() -> None:
    # The step 8 hand-off's example. sunrise-en-hours: alias small (claude-haiku-5-5, 1024 output
    # tokens), plain, a 78-byte English prompt. models.toml (checked 2026-10-14): standard tier,
    # input-side prices 0.10 / 0.01 / 0.125 / 0.20 -> 0.20; output 0.50; threshold 100,000.
    # P = 78 + 200 = 278. Per attempt: 278 x 0.20 + 1024 x 0.50 = 55.6 + 512 = 567.6.
    # Settings default 2 retries: 3 x 567.6 = 1702.8 per million -> $0.0017028.
    [hours] = [c for c in CASES if c.id == "sunrise-en-hours"]
    assert len(hours.prompt.encode("utf-8")) == 78

    reservation = reserve(hours, None, catalog=load_catalog(), max_retries=2, max_repairs=1)

    assert reservation.usd == Decimal("0.0017028")


def test_total_usd_is_the_exact_sum() -> None:
    plain = reserve(case(), None, catalog=PRICES, max_retries=0, max_repairs=0)

    assert total_usd([plain, plain, plain]) == plain.usd * 3
    assert total_usd([]) == Decimal(0)


def test_the_ceiling_is_one_dollar() -> None:
    assert Decimal("1.00") == MAX_USD_CEILING


# ── The byte allowances, against what the gateway really sends ───────────────────────────────


def _sent_bytes(request: ProviderRequest) -> int:
    texts = [request.system or "", *(message.content for message in request.messages)]
    return sum(len(text.encode("utf-8")) for text in texts)


@pytest.mark.anyio
@pytest.mark.parametrize("dataset_case", CASES, ids=[c.id for c in CASES])
async def test_the_bound_covers_every_request_the_gateway_sends(dataset_case: SmokeCase) -> None:
    # A structured case's first answer is made invalid on purpose (`{}`), so every structured case
    # sends a real repair with real feedback; then its canned (valid) reply.
    schema = None if dataset_case.schema_name is None else SCHEMAS[dataset_case.schema_name]
    if schema is None:
        replies = [dataset_case.fake_reply]
    else:
        replies = ["{}", dataset_case.fake_repair_reply or dataset_case.fake_reply]
    fake = FakeProvider([FakeReply(text=text) for text in replies])
    gateway = LLMGateway(
        fake,
        load_catalog(),
        attempt_timeout_s=15.0,
        deadline_s=30.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
    )
    request = LLMRequest(
        business_id=dataset_case.business_id,
        alias=dataset_case.alias,
        messages=(Message(role="user", content=dataset_case.prompt),),
    )
    try:
        if schema is None:
            await gateway.complete(request)
        else:
            await gateway.complete_structured(request, schema)
    except GatewayError:
        pass  # the bound is about what was sent, whatever the outcome

    reservation = reserve(
        dataset_case, schema, catalog=load_catalog(), max_retries=2, max_repairs=1
    )

    assert len(fake.calls) == len(reservation.prompt_tokens)  # first request, then the repair
    for sent, bound in zip(fake.calls, reservation.prompt_tokens, strict=True):
        assert _sent_bytes(sent) <= bound - REQUEST_OVERHEAD_TOKENS
        assert sent.max_tokens == reservation.output_tokens


def test_the_instruction_allowance_covers_the_gateways_instruction() -> None:
    for schema in SCHEMAS.values():
        spec = gateway_module._prepare(schema)
        instruction = len(spec.instruct(None).encode("utf-8")) - len(spec.text.encode("utf-8"))
        joined = len(spec.instruct("s").encode("utf-8")) - len(spec.text.encode("utf-8")) - 1

        assert max(instruction, joined) <= STRUCTURED_INSTRUCTION_BYTES


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_the_feedback_allowance_covers_the_largest_feedback_possible(name: str) -> None:
    # Larger than any real feedback: MAX_PROBLEMS problems, each located at every property name of
    # the schema joined (longer than any real path) with the longest error type there is.
    spec = gateway_module._prepare(SCHEMAS[name])
    location = ".".join(sorted(spec.names))
    longest_type = max(gateway_module._KNOWN_ERROR_TYPES, key=len)
    problems = tuple((f"{location}.{n}", longest_type) for n in range(MAX_PROBLEMS))

    feedback = gateway_module._feedback(problems)

    assert len(feedback.encode("utf-8")) <= REPAIR_FEEDBACK_BYTES


# ── The bound check after a call ─────────────────────────────────────────────────────────────


def test_a_call_inside_its_bound_holds() -> None:
    plain = reserve(case(), None, catalog=PRICES, max_retries=2, max_repairs=1)  # P 205, M 100

    assert bound_held(plain, record())
    assert bound_held(plain, record(attempts=3, retries=2, input_tokens=615, output_tokens=300))


@pytest.mark.parametrize(
    "overrides",
    [
        {"attempts": 4, "retries": 3},  # more attempts than reserved
        {"input_tokens": 206},  # prompt above P for its one attempt
        {"cache_write_1h_tokens": 206, "input_tokens": 0},  # cache tokens count as prompt
        {"output_tokens": 101},  # output above M
        {"cost_usd": "1"},  # billed above the reservation
    ],
)
def test_a_call_beyond_its_bound_does_not_hold(overrides: dict[str, Any]) -> None:
    plain = reserve(case(), None, catalog=PRICES, max_retries=2, max_repairs=1)

    assert not bound_held(plain, record(**overrides))


def test_an_incomplete_cost_is_not_compared_with_the_reservation() -> None:
    # Its cost is only a lower bound; its tokens are still checked.
    plain = reserve(case(), None, catalog=PRICES, max_retries=2, max_repairs=1)

    assert bound_held(plain, record(cost_usd="1", cost_complete=False, inference_geo=None))


# ── The ledger ───────────────────────────────────────────────────────────────────────────────


def _reservation(usd: str) -> Reservation:
    return reserve(case(), None, catalog=PRICES, max_retries=0, max_repairs=0).model_copy(
        update={"usd": Decimal(usd)}
    )


def test_a_reservation_that_reaches_the_cap_fits_and_one_that_crosses_it_does_not() -> None:
    ledger = Ledger(cap=Decimal("0.10"), counted=Decimal("0.04"))

    assert ledger.fits(_reservation("0.06"))  # exactly the cap: does not cross it
    assert not ledger.fits(_reservation("0.0600000001"))


def test_without_a_cap_everything_fits() -> None:
    assert Ledger(cap=None, counted=Decimal(5)).fits(_reservation("1"))


def test_a_complete_call_counts_its_cost() -> None:
    ledger = Ledger(cap=Decimal("1"))

    counted = ledger.settle(_reservation("0.5"), record(cost_usd="0.00002"))

    assert counted == ledger.counted == Decimal("0.00002")


def test_a_cost_incomplete_call_keeps_its_reservation() -> None:
    ledger = Ledger(cap=Decimal("1"))
    incomplete = record(cost_usd="0.00002", cost_complete=False, inference_geo=None)

    counted = ledger.settle(_reservation("0.5"), incomplete)

    assert counted == ledger.counted == Decimal("0.5")  # never counted as its lower bound


def test_an_incomplete_cost_above_the_reservation_counts_in_full() -> None:
    ledger = Ledger(cap=Decimal("1"))
    incomplete = record(cost_usd="0.7", cost_complete=False, inference_geo=None)

    assert ledger.settle(_reservation("0.5"), incomplete) == Decimal("0.7")


def test_a_call_without_its_record_keeps_its_reservation() -> None:
    ledger = Ledger(cap=Decimal("1"), counted=Decimal("0.1"))

    assert ledger.settle(_reservation("0.5"), None) == Decimal("0.5")
    assert ledger.counted == Decimal("0.6")
