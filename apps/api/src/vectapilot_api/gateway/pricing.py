"""H1 — the cost of one model response. [HAND-WRITE] piece, written by Claude at shesha's request
(2026-10-15); shesha's explain-back is still part of its acceptance.

Guide: docs/learning/m1-gateway.md · Tests: tests/gateway/test_pricing.py.
"""

from decimal import Decimal

from vectapilot_api.gateway.types import ModelPricing, Usage

TOKENS_PER_MTOK = 1_000_000  # prices are per million tokens; an int, so the division stays Decimal


def cost_usd(usage: Usage, pricing: ModelPricing) -> Decimal:
    """Return the list-price cost, in US dollars, of one model response.

    Contract:

    - **Per class.** Each of the five token classes in `usage` is charged at its own price from
      the chosen tier. Prices are USD per million tokens (MTok), so one class costs its tokens
      times its price, divided by 1,000,000. The result is the sum over the five classes.
    - **Tier.** If the model has a long-prompt tier *and* the request's prompt is over that tier's
      threshold, use the long-prompt tier's prices; otherwise use the standard prices. The chosen
      tier prices every class of the request, output included. If the pricing page raises only
      some classes, the catalog's long-prompt tier repeats the standard price for the others, so
      this rule holds either way (PR 3 checks the page).
    - **Prompt length** counts all input tokens: the uncached input, the cache reads and both
      kinds of cache write. Output tokens do not count. "Over" is strictly greater: a prompt of
      exactly the threshold stays on the standard tier.
    - A model without a long-prompt tier never switches tier, however long the prompt.
    - **Exact.** The result is a `Decimal` (also for zero usage: `Decimal("0")`, not the int `0`),
      with no float anywhere and no rounding.

    One call to this function prices one response. Summing retries and repairs is the gateway's
    job (PR 6).

    Source, quoted from https://platform.claude.com/docs/en/about-claude/pricing, section "Long
    context pricing" (checked 2026-10-14): "Claude Haiku 5.5 is priced by prompt length: a request
    whose prompt is over 100,000 tokens pays higher prices. A request's prompt length counts all
    of its input tokens, including cache reads and cache writes. Each request is priced on its
    own: a request over the threshold pays the higher prices even when part of its prompt is a
    cache hit, and earlier requests keep the prices they were billed at."
    """
    # 1. Which price list? Prompt length counts every input-side class; output is not prompt.
    prompt_tokens = (
        usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_5m_tokens
        + usage.cache_write_1h_tokens
    )
    tier = pricing.long_prompt
    if tier is not None and prompt_tokens > tier.threshold_tokens:  # "over" = strictly greater
        prices = tier.prices
    else:
        prices = pricing.standard

    # 2. Each class at its own price. int x Decimal is exact; no float is ever involved.
    per_million = (
        usage.input_tokens * prices.input
        + usage.output_tokens * prices.output
        + usage.cache_read_tokens * prices.cache_read
        + usage.cache_write_5m_tokens * prices.cache_write_5m
        + usage.cache_write_1h_tokens * prices.cache_write_1h
    )
    return per_million / TOKENS_PER_MTOK
