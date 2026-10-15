"""Structured-output schemas the gateway_smoke dataset names (PR 8; dataset README, "Schema").

Each case with `kind: structured` names one of these by its key in `SCHEMAS`; the measurement CLI
passes that class to `LLMGateway.complete_structured`.

Read before editing (gateway.py, `complete_structured`; the M3 hand-over in
docs/plans/m1-pr-checks.md):

- A class docstring and every `Field(description=...)` end up in the JSON Schema that the gateway
  quotes in the system prompt, so they are prompt text written for the model, and part of the
  cache key. Notes for maintainers go in `#` comments, which the model never sees.
- Validators raise only `ValueError` (or `AssertionError`, `PydanticCustomError`); anything else
  propagates as `internal_error`. Their messages never contain the input.
- Every schema is closed (`extra="forbid"`, so `additionalProperties: false`): an unexpected key
  is a validation error, and first-try validity stays honest.

Synthetic and business-neutral: no business, phone number or order format is named here.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class IntentLabel(BaseModel):
    """The main intent of one customer-support message, for routing it."""

    model_config = ConfigDict(extra="forbid")

    intent: Literal[
        "booking",
        "cancellation",
        "opening_hours",
        "fees",
        "order_status",
        "refund",
        "return",
        "other",
    ] = Field(description="What the customer mainly wants.")
    language: Literal["en", "hi", "te"] = Field(
        description="The message's language. A message that mixes English with Hindi or Telugu"
        " counts as hi or te."
    )
    needs_human: bool = Field(description="True if a staff member should handle this message.")


class BookingRequest(BaseModel):
    """An appointment request, as the customer described it."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(
        min_length=1,
        max_length=80,
        description="What the visit is for, in English, in a few words.",
    )
    preferred_day: Weekday | None = Field(
        description="The weekday the customer asked for, or null if they named none."
    )
    preferred_time_of_day: Literal["morning", "afternoon", "evening"] | None = Field(
        description="The part of the day the customer asked for, or null if they named none."
    )
    for_pet: bool = Field(description="True if the appointment is for an animal.")


class OrderQuery(BaseModel):
    """A customer's question about an online order."""

    model_config = ConfigDict(extra="forbid")

    order_ref: str | None = Field(
        max_length=40,
        description="The order reference exactly as the customer wrote it, or null if none.",
    )
    topic: Literal["status", "delay", "damaged", "return", "refund", "other"] = Field(
        description="What the question is about."
    )
    wants: Literal["information", "replacement", "refund", "other"] = Field(
        description="What the customer asks the shop to do."
    )


class LineItem(BaseModel):
    """One product line on a bill."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60, description="The product, in a few words.")
    quantity: int = Field(ge=1, le=100, description="How many were bought.")
    unit_price_inr: int = Field(ge=0, description="The price of one, in whole rupees.")


class BillCheck(BaseModel):
    """The items and the total of a customer's bill."""

    model_config = ConfigDict(extra="forbid")

    items: list[LineItem] = Field(min_length=1, max_length=20, description="Every product line.")
    total_inr: int = Field(ge=0, description="The bill's total, in whole rupees.")

    # The forced-repair schema (exactly one dataset case uses it). The rule below is an
    # after-validator that the JSON Schema cannot express: JSON Schema has no way to say "this
    # number equals a sum computed from other fields". It is deliberately written in no docstring
    # or description, so the model does not see it on the first try; the case's prompt asks for a
    # total that does not match the items. The first answer is therefore *expected* (not
    # guaranteed) to fail, which makes the gateway send its one repair. The repair's feedback names
    # only `total_inr: value_error` (gateway.py, `_problems`). The summary reports every result,
    # including "the first answer was valid" (repair not exercised).
    @field_validator("total_inr", mode="after")
    @classmethod
    def _total_matches_the_items(cls, total: int, info: ValidationInfo) -> int:
        items = info.data.get("items")  # absent when `items` itself failed validation
        if items is not None and total != sum(
            item.quantity * item.unit_price_inr for item in items
        ):
            raise ValueError("total_inr must equal the sum of quantity x unit_price_inr")
        return total


SCHEMAS: Final[Mapping[str, type[BaseModel]]] = MappingProxyType(
    {
        "IntentLabel": IntentLabel,
        "BookingRequest": BookingRequest,
        "OrderQuery": OrderQuery,
        "BillCheck": BillCheck,
    }
)
"""Every schema a dataset case may name (`schema_name`), by its class name. Read-only."""
