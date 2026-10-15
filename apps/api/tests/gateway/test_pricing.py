"""H1 tests: `cost_usd` (guide: docs/learning/m1-gateway.md, section H1).

`cost_usd` is implemented, so the module's `xfail(strict=True)` marker is gone and every test must
simply pass. Do not edit the tests to make them pass; change the code.

All prices are made up (never real list prices, so these tests never go stale). Every expected
value is worked out by hand; the working is in the comment next to it.
"""

from decimal import Decimal

import pytest

from vectapilot_api.gateway.pricing import cost_usd
from vectapilot_api.gateway.types import LongPromptTier, ModelPricing, TokenPrices, Usage

STANDARD = TokenPrices(  # USD per million tokens
    input=Decimal("3"),
    output=Decimal("7"),
    cache_read=Decimal("0.3"),
    cache_write_5m=Decimal("11"),
    cache_write_1h=Decimal("13"),
)
LONG_PROMPT = TokenPrices(  # ten times STANDARD, so a wrong tier is easy to spot
    input=Decimal("30"),
    output=Decimal("70"),
    cache_read=Decimal("3"),
    cache_write_5m=Decimal("110"),
    cache_write_1h=Decimal("130"),
)
SINGLE_TIER = ModelPricing(standard=STANDARD)
TIERED = ModelPricing(
    standard=STANDARD,
    long_prompt=LongPromptTier(threshold_tokens=100, prices=LONG_PROMPT),
)


def test_zero_usage_costs_exactly_zero() -> None:
    assert cost_usd(Usage(), TIERED) == Decimal("0")


@pytest.mark.parametrize(
    ("token_class", "expected"),
    [
        ("input_tokens", Decimal("3")),  # 1,000,000 * $3 / 1,000,000
        ("output_tokens", Decimal("7")),
        ("cache_read_tokens", Decimal("0.3")),
        ("cache_write_5m_tokens", Decimal("11")),
        ("cache_write_1h_tokens", Decimal("13")),
    ],
)
def test_each_class_is_charged_at_its_own_price(token_class: str, expected: Decimal) -> None:
    usage = Usage.model_validate({token_class: 1_000_000})

    assert cost_usd(usage, SINGLE_TIER) == expected


def test_one_token_at_3_dollars_per_mtok_is_exact() -> None:
    # 1 * $3 / 1,000,000 = $0.000003 exactly: the smallest amount comes out as itself.
    assert cost_usd(Usage(input_tokens=1), SINGLE_TIER) == Decimal("0.000003")


def test_all_five_classes_are_added_up() -> None:
    usage = Usage(
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=30,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=15,
    )
    # Prompt 10 + 30 + 40 + 15 = 95, not over 100 → standard prices:
    # 10*3 + 20*7 + 30*0.3 + 40*11 + 15*13 = 30 + 140 + 9 + 440 + 195 = 814 → $0.000814
    assert cost_usd(usage, TIERED) == Decimal("0.000814")


def test_realistic_counts_with_a_fractional_price_stay_exact() -> None:
    usage = Usage(input_tokens=1234, output_tokens=567, cache_read_tokens=8901)
    # 1234*3 + 567*7 + 8901*0.3 = 3702 + 3969 + 2670.3 = 10341.3 → $0.0103413
    assert cost_usd(usage, SINGLE_TIER) == Decimal("0.0103413")


def test_a_price_with_more_digits_than_a_float_holds_stays_exact() -> None:
    # A float holds about 15-17 significant digits; this answer has 18, so a float anywhere on
    # the way loses the last ones, even if the result is turned back into a Decimal at the end.
    precise = ModelPricing(standard=STANDARD.model_copy(update={"input": Decimal("1.23456789123")}))
    # 987,654 * 1.23456789123 = 1,219,325.91604487442 → / 1,000,000 = $1.21932591604487442
    assert cost_usd(Usage(input_tokens=987_654), precise) == Decimal("1.21932591604487442")


# ── The long-prompt tier ("over 100,000 tokens" on the pricing page; 100 here) ───────────────


def test_a_prompt_of_exactly_the_threshold_uses_standard_prices() -> None:
    # 100 is not over 100 → standard: 100*3 = 300 → $0.0003
    assert cost_usd(Usage(input_tokens=100), TIERED) == Decimal("0.0003")


def test_a_prompt_one_token_over_the_threshold_uses_long_prompt_prices() -> None:
    # 101 is over 100 → long prompt: 101*30 = 3030 → $0.00303
    assert cost_usd(Usage(input_tokens=101), TIERED) == Decimal("0.00303")


def test_the_threshold_is_read_from_the_pricing() -> None:
    # The same tiers with a threshold of 50: 51 is over 50 → long prompt: 51*30 = 1530 → $0.00153
    tiered_at_50 = ModelPricing(
        standard=STANDARD, long_prompt=LongPromptTier(threshold_tokens=50, prices=LONG_PROMPT)
    )
    assert cost_usd(Usage(input_tokens=51), tiered_at_50) == Decimal("0.00153")


def test_cache_tokens_count_toward_the_prompt_at_the_threshold() -> None:
    usage = Usage(
        input_tokens=40,
        cache_read_tokens=30,
        cache_write_5m_tokens=20,
        cache_write_1h_tokens=10,
    )
    # Prompt 40 + 30 + 20 + 10 = 100, not over → standard:
    # 40*3 + 30*0.3 + 20*11 + 10*13 = 120 + 9 + 220 + 130 = 479 → $0.000479
    assert cost_usd(usage, TIERED) == Decimal("0.000479")


@pytest.mark.parametrize(
    ("cache_class", "expected"),
    [
        ("cache_read_tokens", Decimal("0.003003")),  # 100*30 + 1*3 = 3003
        ("cache_write_5m_tokens", Decimal("0.00311")),  # 100*30 + 1*110 = 3110
        ("cache_write_1h_tokens", Decimal("0.00313")),  # 100*30 + 1*130 = 3130
    ],
)
def test_one_cache_token_pushes_the_prompt_over_the_threshold(
    cache_class: str, expected: Decimal
) -> None:
    # Prompt 100 + 1 = 101, over 100 → long-prompt prices for every class.
    usage = Usage.model_validate({"input_tokens": 100, cache_class: 1})

    assert cost_usd(usage, TIERED) == expected


def test_output_tokens_do_not_count_toward_the_prompt() -> None:
    usage = Usage(input_tokens=100, output_tokens=1000)
    # Prompt is 100 (output is not prompt) → standard: 100*3 + 1000*7 = 7300 → $0.0073
    assert cost_usd(usage, TIERED) == Decimal("0.0073")


def test_the_long_prompt_tier_prices_output_too() -> None:
    usage = Usage(input_tokens=101, output_tokens=10)
    # Over 100 → the long-prompt tier for every class, output included, at whatever output price
    # that tier holds (here 70; PR 3 copies the real ones from the pricing page):
    # 101*30 + 10*70 = 3030 + 700 = 3730 → $0.00373
    assert cost_usd(usage, TIERED) == Decimal("0.00373")


def test_a_single_tier_model_never_switches() -> None:
    usage = Usage(input_tokens=9_000_000, cache_read_tokens=1_000_000, output_tokens=500_000)
    # No long-prompt tier, so standard: 9,000,000*3 + 1,000,000*0.3 + 500,000*7
    # = 27,000,000 + 300,000 + 3,500,000 = 30,800,000 → $30.8
    assert cost_usd(usage, SINGLE_TIER) == Decimal("30.8")


@pytest.mark.parametrize(
    "usage",
    [
        pytest.param(Usage(), id="zero-usage"),
        pytest.param(Usage(input_tokens=1, output_tokens=1), id="some-usage"),
    ],
)
def test_the_result_is_a_decimal(usage: Usage) -> None:
    # Decimal("0") == 0 is True, so equality alone would not catch an int or a float.
    assert type(cost_usd(usage, TIERED)) is Decimal
