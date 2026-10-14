"""H1 — the cost of one model response. [HAND-WRITE]: shesha writes the body.

Guide: docs/learning/m1-gateway.md · Tests: tests/gateway/test_pricing.py (marked
`xfail(strict=True)` until this works; then delete their `pytestmark` line).
"""

from decimal import Decimal

from vectapilot_api.gateway.types import ModelPricing, Usage


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
    raise NotImplementedError("H1: shesha implements cost_usd — see docs/learning/m1-gateway.md")
