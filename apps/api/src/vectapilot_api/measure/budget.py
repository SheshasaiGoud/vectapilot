"""The `--live` cost cap: each call's worst-case cost, reserved before the call (PR 8; ADR 0006).

Pure: the catalog, the retry and repair limits and a case in; Decimal out. No I/O, no model call,
no API call to count tokens. The measurement CLI (`llm_cli.py`) reserves `Reservation.usd` before
each call and stops before any call whose reservation would cross `--max-usd` (`Ledger`).

**One call's worst case** sums every request the gateway could send for it, times every attempt
H2 could make of each request (retries apply to the first request and to the repair alike):

    usd = sum over requests r of
          (1 + max_retries) * (P_r * in_price_r + M * out_price_r) / 1,000,000

- **Requests:** the first one, plus the repair for a structured case when `max_repairs` >= 1.
- **M** is the alias's `max_output_tokens` (the CLI sets no smaller `max_tokens`): an attempt is
  never billed more output tokens than its `max_tokens`, thinking included.
- **P_r** bounds one attempt's prompt tokens from the text alone: the UTF-8 bytes of the prompt;
  for a structured case, plus the bytes of the schema's JSON Schema (`json.dumps` with its default
  separators, which is longer than the compact form the gateway sends) and
  `STRUCTURED_INSTRUCTION_BYTES`; for the repair, plus `REPAIR_FEEDBACK_BYTES`; then plus
  `REQUEST_OVERHEAD_TOKENS`.
- **in_price** is the highest input-side price of the tier (input, cache read, both cache writes),
  so the bound holds even if prompt tokens were billed as a cache write (M1 never asks for
  caching). **out_price** is the tier's output price.
- **Tier:** standard, unless the model has a long-prompt tier and P_r is over its threshold: then
  every class at the higher of the two tiers' prices (H1 picks the tier from the real prompt length,
  which P_r only bounds).

**Assumptions**, not documented facts:

- one token covers at least one UTF-8 byte of the text sent (true of a byte-level tokenizer);
- the API's own framing of a request (roles, turns) bills at most `REQUEST_OVERHEAD_TOKENS`. The
  token-counting page says "You are not billed for system-added tokens. Billing reflects only your
  content." (platform.claude.com/docs/en/build-with-claude/token-counting, checked 2026-10-14);
- global routing (ADR 0006; PR 9 checks the workspace default): a geo other than `global`, or
  none reported, makes the call cost-incomplete and it keeps its reservation (`Ledger`). `us` bills
  1.1x the list price, so the reservation covers such a call only while its cost at list prices is
  at most 1/1.1 (about 91 %) of its reservation;
- if an assumption fails, that one call may cross the cap. `bound_held` checks the first two
  against the billed tokens after every call, and the CLI stops the run at the first miss. The
  third cannot be checked in dollars (a cost-incomplete call's dollars are only a lower bound), so
  the CLI checks the geo instead: a `us` geo stops the run after that call (decided by shesha,
  2026-10-15: a setup error); a call with no geo reported keeps its reservation and the run
  carries on.

The two byte allowances are checked against what the gateway really sends, for every schema in
`measure.schemas.SCHEMAS` (tests/measure/test_budget.py).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from vectapilot_api.gateway.catalog import Catalog, PricedModel
from vectapilot_api.gateway.types import CallRecord, TokenPrices, Usd
from vectapilot_api.measure.dataset import SmokeCase

MAX_USD_CEILING: Final = Decimal("1.00")
"""`--max-usd` must be above 0 and at most this (decided in the M1 plan, D9: a hard ceiling)."""

REQUEST_OVERHEAD_TOKENS: Final = 200
"""Allowance per attempt for the API's framing of a request, which the text's bytes do not cover.
An assumption (see the module docstring), checked after every call by `bound_held`."""

STRUCTURED_INSTRUCTION_BYTES: Final = 256
"""Allowance for the gateway's one-sentence output instruction around the schema (81 bytes on
2026-10-15) and the blank line that joins it to a system prompt."""

REPAIR_FEEDBACK_BYTES: Final = 1024
"""Allowance for the repair's feedback message: at most 5 `location: error type` lines (the
largest possible for the dataset's schemas was 609 bytes on 2026-10-15)."""

TOKENS_PER_MTOK: Final = 1_000_000


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Reservation(_Closed):
    """One planned call's worst case, and the numbers it was computed from."""

    case_id: str
    alias: str
    model: str
    attempts_per_request: int = Field(ge=1)
    """`1 + max_retries`."""
    prompt_tokens: tuple[int, ...] = Field(min_length=1, max_length=2)
    """P_r for each request the call may send: the first, then the repair (if any)."""
    output_tokens: int = Field(ge=1)
    """M: the most output tokens one attempt can be billed."""
    usd: Usd

    @property
    def attempts(self) -> int:
        """The most attempts the call can make: every request, every retry."""
        return self.attempts_per_request * len(self.prompt_tokens)


def reserve(
    case: SmokeCase,
    schema: type[BaseModel] | None,
    *,
    catalog: Catalog,
    max_retries: int,
    max_repairs: int,
) -> Reservation:
    """The worst-case cost of one call of `case` (the module docstring has the formula).

    `schema` is the case's schema class, None for a plain case. The case's alias must be in the
    catalog (`dataset.check_dataset` checks it); a missing one raises `KeyError`.
    """
    return request_reservation(
        case_id=case.id,
        alias_name=case.alias,
        prompt_bytes=len(case.prompt.encode("utf-8")),
        schema=schema,
        catalog=catalog,
        max_retries=max_retries,
        max_repairs=max_repairs,
    )


def request_reservation(
    *,
    case_id: str,
    alias_name: str,
    prompt_bytes: int,
    schema: type[BaseModel] | None,
    catalog: Catalog,
    max_retries: int,
    max_repairs: int,
) -> Reservation:
    """The worst-case cost of one call whose prompt text (system prompt and messages together) is
    `prompt_bytes` UTF-8 bytes: the module docstring's formula, for any caller. `reserve` uses it
    for a smoke case; the eval harness (M2 PR 8) for a cited answer's request, with its system
    prompt. `case_id` only labels the reservation. A missing alias raises `KeyError`.
    """
    alias = catalog.aliases[alias_name]
    pricing = catalog.models[alias.model]
    first = prompt_bytes + REQUEST_OVERHEAD_TOKENS
    prompts = [first]
    if schema is not None:
        prompts[0] += _schema_bytes(schema) + STRUCTURED_INSTRUCTION_BYTES
        if max_repairs >= 1:
            prompts.append(prompts[0] + REPAIR_FEEDBACK_BYTES)
    attempts = 1 + max_retries
    output = alias.max_output_tokens
    per_million = sum(
        (
            attempts * (p * _input_price(pricing, p) + output * _output_price(pricing, p))
            for p in prompts
        ),
        Decimal(0),
    )
    return Reservation(
        case_id=case_id,
        alias=alias_name,
        model=alias.model,
        attempts_per_request=attempts,
        prompt_tokens=tuple(prompts),
        output_tokens=output,
        usd=per_million / TOKENS_PER_MTOK,
    )


def _schema_bytes(schema: type[BaseModel]) -> int:
    # Default separators (", " and ": "): longer than the compact text the gateway quotes.
    return len(json.dumps(schema.model_json_schema()).encode("utf-8"))


def _tiers(pricing: PricedModel, prompt_tokens: int) -> list[TokenPrices]:
    """The price lists the bound must cover for a prompt of at most `prompt_tokens` tokens."""
    tier = pricing.long_prompt
    if tier is not None and prompt_tokens > tier.threshold_tokens:  # "over" = strictly greater
        return [pricing.standard, tier.prices]
    return [pricing.standard]


def _input_price(pricing: PricedModel, prompt_tokens: int) -> Decimal:
    return max(
        max(t.input, t.cache_read, t.cache_write_5m, t.cache_write_1h)
        for t in _tiers(pricing, prompt_tokens)
    )


def _output_price(pricing: PricedModel, prompt_tokens: int) -> Decimal:
    return max(t.output for t in _tiers(pricing, prompt_tokens))


def total_usd(reservations: Sequence[Reservation]) -> Decimal:
    """The sum of the reservations' worst cases (exact)."""
    return sum((r.usd for r in reservations), Decimal(0))


def bound_held(reservation: Reservation, record: CallRecord) -> bool:
    """Whether a finished call stayed inside the assumptions its reservation was computed from.

    Checked against the record's billed tokens: at most the reserved attempts, at most the largest
    P_r and M per attempt, and (when the cost is complete) at most the reserved dollars. False means
    the bound was wrong: the CLI stops the run. Tokens of an attempt that timed out are unknown and
    not in the record; that call's cost is incomplete and keeps its reservation.
    """
    prompt = (
        record.input_tokens
        + record.cache_read_tokens
        + record.cache_write_5m_tokens
        + record.cache_write_1h_tokens
    )
    return (
        record.attempts <= reservation.attempts
        and prompt <= record.attempts * max(reservation.prompt_tokens)
        and record.output_tokens <= record.attempts * reservation.output_tokens
        and (not record.cost_complete or record.cost_usd <= reservation.usd)
    )


@dataclass
class Ledger:
    """What the run has spent, as the cap counts it, against `cap` (None: no cap, a dry run).

    A finished call counts its `cost_usd` when that is complete. A cost-incomplete call keeps its
    whole reservation (its `cost_usd` is only a lower bound: never counted as less than reserved),
    and so does a call whose record could not be read.
    """

    cap: Decimal | None
    counted: Decimal = field(default_factory=Decimal)

    def fits(self, reservation: Reservation) -> bool:
        """Whether reserving this call keeps the run at or under the cap."""
        return self.cap is None or self.counted + reservation.usd <= self.cap

    def settle(self, reservation: Reservation, record: CallRecord | None) -> Decimal:
        """Count a finished call; return the amount counted for it."""
        if record is not None and record.cost_complete:
            amount = record.cost_usd
        elif record is not None:
            amount = max(reservation.usd, record.cost_usd)
        else:
            amount = reservation.usd
        self.counted += amount
        return amount
