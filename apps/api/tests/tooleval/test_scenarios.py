"""The scenario schema and the manifest (`tooleval/scenarios.py`). Unit; TEST-ONLY scenarios."""

import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from tests.tooleval.helpers import COMMITTED, PAWS, SUNRISE, parsed, scenario
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.tooleval.scenarios import (
    KEY_ARGUMENTS,
    MANIFEST_FILE,
    STRATA,
    WRITE_ROWS,
    dump_scenario,
    groups_for,
    load_manifest,
    load_scenarios,
)
from vectapilot_api.tools.book import BookInput
from vectapilot_api.tools.config import WRITE_TOOLS, ToolName
from vectapilot_api.tools.create_ticket import CreateTicketInput
from vectapilot_api.tools.reschedule import RescheduleInput

BOOK: dict[str, Any] = {"kind": "confirmation_required", "tool": "book"}


def test_key_arguments_are_fields_of_each_write_tools_input_and_never_free_text() -> None:
    inputs: dict[ToolName, type[BaseModel]] = {
        ToolName.BOOK: BookInput,
        ToolName.RESCHEDULE: RescheduleInput,
        ToolName.CREATE_TICKET: CreateTicketInput,
    }
    assert set(KEY_ARGUMENTS) == set(WRITE_TOOLS) == set(WRITE_ROWS)
    for tool, names in KEY_ARGUMENTS.items():
        assert set(names) <= set(inputs[tool].model_fields)
    assert "summary" not in KEY_ARGUMENTS[ToolName.CREATE_TICKET]
    assert WRITE_ROWS == {ToolName.BOOK: 1, ToolName.RESCHEDULE: 2, ToolName.CREATE_TICKET: 1}


def test_a_proposal_names_its_write_tool_and_exactly_its_key_arguments() -> None:
    good = parsed(scenario("s-1", kind=BOOK["kind"], tool="book", arguments={"slot_id": "S-09"}))
    assert good.expect.write_rows == 1
    assert good.expected_write_rows == 0  # not confirmed
    for bad in (
        scenario("s-2", kind="confirmation_required"),  # no tool
        scenario("s-3", kind="reply", tool="book", arguments={"slot_id": "S-09"}),
        scenario("s-4", kind="confirmation_required", tool="check_slots", arguments={}),
        scenario("s-5", kind="confirmation_required", tool="book", arguments={}),
        scenario(
            "s-6",
            kind="confirmation_required",
            tool="book",
            arguments={"slot_id": "S-09", "extra": "x"},
        ),
        scenario("s-7", kind="confirmation_required", tool="book", arguments={"slot_id": "s 9"}),
        scenario(
            "s-8",
            kind="confirmation_required",
            tool="create_ticket",
            arguments={"category": "Not A Code"},
        ),
    ):
        with pytest.raises(ValidationError):
            parsed(bad)
    # A proposal with its tool but no arguments at all.
    no_arguments = scenario("s-9", kind="confirmation_required")
    no_arguments["expect"] = {"kind": "confirmation_required", "tool": "book"}
    with pytest.raises(ValidationError):
        parsed(no_arguments)


def test_a_manifest_lists_each_business_once() -> None:
    manifest = json.loads((COMMITTED / MANIFEST_FILE).read_text("utf-8"))
    load_manifest(json.dumps(manifest))
    manifest["businesses"] = [PAWS, PAWS]
    with pytest.raises(DatasetError, match="each business is listed once"):
        load_manifest(json.dumps(manifest))


@pytest.mark.parametrize(
    "fields",
    [
        {"confirm": True},  # confirm without a proposal
        {"read_only": True, "kind": "confirmation_required", "tool": "book"},
        {"emergency": True, "kind": "reply"},
        {"language": "en", "code_mixed": True},
        {"message": "   "},
        {"message": "x" * 501},
        {"author": "claude"},
        {"synthetic": False},
        {"seed": "noisy"},
        {"customer_ref": "Cust 1"},
    ],
)
def test_inconsistent_scenarios_are_refused(fields: dict[str, Any]) -> None:
    kind = fields.pop("kind", "reply")
    tool = fields.pop("tool", None)
    arguments = {"slot_id": "S-09"} if tool else None
    with pytest.raises(ValidationError):
        parsed(scenario("s-1", kind=kind, tool=tool, arguments=arguments, **fields))


def test_an_emergency_expects_a_hand_off_and_a_confirmed_proposal_expects_its_rows() -> None:
    emergency = parsed(scenario("s-1", kind="handoff", emergency=True))
    confirmed = parsed(
        scenario(
            "s-2",
            kind="confirmation_required",
            tool="reschedule",
            arguments={"booking_id": "B-103", "new_slot_id": "S-10"},
            confirm=True,
        )
    )
    assert emergency.stratum == "emergency"
    assert confirmed.stratum == "confirm"
    assert confirmed.expected_write_rows == 2


def test_the_stratum_follows_the_fixed_order() -> None:
    steering = parsed(scenario("s-1", seed="steering", read_only=True))
    reading = parsed(scenario("s-2", read_only=True))
    handing = parsed(scenario("s-3", kind="handoff"))
    assert (steering.stratum, reading.stratum, handing.stratum) == (
        "steering",
        "read_only",
        "expect_handoff",
    )
    assert STRATA.index("emergency") == 0


def test_language_groups_follow_the_business_languages() -> None:
    assert groups_for(("en", "hi")) == ("en", "hi", "hi-code-mixed")
    assert groups_for(("en", "te")) == ("en",)
    hinglish = parsed(scenario("s-1", SUNRISE, language="hi", code_mixed=True))
    assert hinglish.language_group == "hi-code-mixed"


def test_a_bad_line_is_named_and_its_text_never_echoed() -> None:
    hidden = "TEST-ONLY a message that must never be echoed"
    good = json.dumps(scenario("s-1"))
    bad = json.dumps(scenario("s-2", message=hidden, confirm=True))
    with pytest.raises(DatasetError) as raised:
        load_scenarios(f"{good}\n\n{bad}\n")
    assert "line 3" in str(raised.value)
    assert hidden not in str(raised.value)


def test_a_scenario_round_trips_with_hindi_as_written() -> None:
    hindi = parsed(scenario("s-1", SUNRISE, language="hi", message="TEST-ONLY कल का स्लॉट"))
    line = dump_scenario(hindi)
    assert "कल" in line
    assert load_scenarios(line) == (hindi,)
    assert parsed(scenario("s-2", PAWS)).business_id == PAWS
