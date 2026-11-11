"""Builders of made-up result lines for the H2 and summary tests. No number here is a result."""

from typing import Any

from vectapilot_api.tooleval.runinfo import ActiveBookings, ResultLine, WriteRow


def result(scenario_id: str, **fields: Any) -> ResultLine:
    """A result line with quiet defaults: an English Paws & Co scenario on dev expecting a reply,
    which replied."""
    data: dict[str, Any] = {
        "id": scenario_id,
        "business_id": "paws-and-co",
        "language_group": "en",
        "split": "dev",
        "unit": f"id:{scenario_id}",
        "read_only": False,
        "emergency": False,
        "confirm": False,
        "steering": False,
        "expected_kind": "reply",
        "expected_tool": None,
        "expected_arguments": None,
        "expected_write_rows": 0,
        "outcome": "reply",
    }
    return ResultLine.model_validate(data | fields)


def row(
    kind: str,
    row_id: str,
    action_id: str | None,
    *,
    status: str | None = "executed",
    confirmed: bool = True,
    turn_ours: bool = True,
    ours: bool = True,
    audit: bool = True,
    slot: str | None = None,
    category: str | None = None,
) -> WriteRow:
    return WriteRow(
        kind=kind,
        row_id=row_id,
        action_id=action_id,
        slot_id=slot,
        category=category,
        action_status=status,
        confirmed_at_set=confirmed,
        action_trace_ours=turn_ours,
        confirm_trace_ours=ours,
        executed_audit=audit,
    )


def active(customer: str, actual: int, expected: int) -> ActiveBookings:
    return ActiveBookings(customer_ref=customer, actual=actual, expected=expected)
