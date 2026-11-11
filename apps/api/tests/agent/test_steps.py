"""`build_agent_step`: one business's `AgentStep` schema (D2 (a), W7; M5 PR 4)."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from tests.agent.fakes import EXECUTOR, NO_DATABASE, PAWS, STEP, SUNRISE, as_call, call, step
from vectapilot_api.agent.steps import TOOL_DESCRIPTIONS, build_agent_step, variant_name
from vectapilot_api.agent.types import AgentStep, CallTool, Handoff, ModelHandoffReason, Reply
from vectapilot_api.gateway.gateway import _prepare
from vectapilot_api.tools.config import READ_TOOLS, WRITE_TOOLS, ToolName
from vectapilot_api.tools.executor import ToolExecutor

CONTEXT_FIELDS = {"business_id", "customer_ref", "trace_id"}


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for child in node.values():
            yield from walk(child)
    elif isinstance(node, list):
        for child in node:
            yield from walk(child)


def variants(model: type[AgentStep]) -> list[str]:
    return sorted(
        name
        for name in schema_of(model).get("$defs", {})
        if name.startswith("Call") and name != "CallTool"
    )


def test_every_platform_tool_has_a_description_that_says_read_or_write() -> None:
    assert set(TOOL_DESCRIPTIONS) == set(ToolName)
    for tool in READ_TOOLS:
        assert TOOL_DESCRIPTIONS[tool].startswith("Read: ")
    for tool in WRITE_TOOLS:
        assert TOOL_DESCRIPTIONS[tool].startswith("Write: propose ")
        assert "only proposed" in TOOL_DESCRIPTIONS[tool]


def test_one_call_variant_per_enabled_tool_and_none_for_a_disabled_one() -> None:
    sunrise = build_agent_step(ToolExecutor(NO_DATABASE, SUNRISE))  # no order_status

    assert variants(STEP) == sorted(variant_name(tool) for tool in PAWS.enabled_tools)
    assert variants(sunrise) == sorted(variant_name(tool) for tool in SUNRISE.enabled_tools)
    assert "CallOrderStatus" not in variants(sunrise)
    with pytest.raises(ValidationError):
        sunrise.model_validate(
            {
                "step": {
                    "action": "call_tool",
                    "tool": "order_status",
                    "arguments": {"order_id": "1"},
                }
            }
        )


def test_the_schema_is_stable_whatever_order_the_configuration_lists_the_tools_in() -> None:
    reordered = PAWS.model_copy(update={"enabled_tools": tuple(reversed(PAWS.enabled_tools))})
    other = build_agent_step(ToolExecutor(NO_DATABASE, reordered))

    # The gateway's cache identity: module, qualified name and schema text.
    assert _prepare(other).identity == _prepare(STEP).identity
    assert other.__name__ == STEP.__name__ == "AgentStep"


def test_the_arguments_are_the_businesss_own_input_model_with_its_enums() -> None:
    schema = json.dumps(schema_of(STEP))
    sunrise = json.dumps(schema_of(build_agent_step(ToolExecutor(NO_DATABASE, SUNRISE))))

    assert '"vet_consultation"' in schema and '"vet_consultation"' not in sunrise
    assert '"physiotherapy"' in sunrise and '"physiotherapy"' not in schema
    with pytest.raises(ValidationError):  # a service another business has
        call(ToolName.CHECK_SLOTS, date_from="2026-11-16", date_to="2026-11-17", service="physio")


def test_no_schema_has_a_context_field_and_every_object_is_closed() -> None:
    for model in (STEP, build_agent_step(ToolExecutor(NO_DATABASE, SUNRISE))):
        for node in walk(schema_of(model)):
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert not CONTEXT_FIELDS & set(properties), properties
                assert node.get("additionalProperties") is False, node.get("title")


@pytest.mark.parametrize("field", sorted(CONTEXT_FIELDS))
def test_a_context_field_in_the_arguments_is_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        call(ToolName.ORDER_STATUS, order_id="1042", **{field: "sunrise-clinic"})


def test_the_three_actions_validate_into_their_types() -> None:
    called = call(ToolName.BOOK, slot_id="S-17")
    replied = step({"action": "reply", "text": "Hello"})
    handed = step({"action": "handoff", "reason": "safety"})

    assert isinstance(called.step, CallTool)
    assert as_call(called).tool_name is ToolName.BOOK
    assert as_call(called).argument_values() == {"slot_id": "S-17"}
    assert isinstance(replied.step, Reply) and replied.step.text == "Hello"
    assert isinstance(handed.step, Handoff) and handed.step.reason is ModelHandoffReason.SAFETY


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "handoff", "reason": "step_cap"},  # a loop reason is not the model's
        {"action": "reply", "text": ""},
        {"action": "reply", "text": "x" * 1501},
        {"action": "execute", "tool": "book"},
        {"action": "call_tool", "tool": "book", "arguments": {"slot_id": "S-17"}, "extra": 1},
        {"action": "call_tool", "tool": "book", "arguments": {}},
        {
            "action": "call_tool",
            "tool": "check_slots",
            "arguments": {"date_from": "2026-11-01", "date_to": "2026-11-30"},
        },  # more than 14 days: the input model's own rule
    ],
    ids=[
        "loop reason",
        "empty reply",
        "long reply",
        "unknown action",
        "extra key",
        "no slot",
        "range",
    ],
)
def test_anything_else_is_a_schema_error(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        step(payload)


def test_a_business_with_one_tool_or_none_still_gets_a_valid_schema() -> None:
    one = build_agent_step(
        ToolExecutor(
            NO_DATABASE, PAWS.model_copy(update={"enabled_tools": (ToolName.ORDER_STATUS,)})
        )
    )
    none = build_agent_step(
        ToolExecutor(
            NO_DATABASE,
            PAWS.model_copy(update={"enabled_tools": (), "ticket_categories": ()}),
        )
    )

    assert isinstance(
        one.model_validate(
            {
                "step": {
                    "action": "call_tool",
                    "tool": "order_status",
                    "arguments": {"order_id": "1"},
                }
            }
        ).step,
        CallTool,
    )
    assert isinstance(none.model_validate({"step": {"action": "reply", "text": "Hi"}}).step, Reply)
    with pytest.raises(ValidationError):
        none.model_validate(
            {
                "step": {
                    "action": "call_tool",
                    "tool": "order_status",
                    "arguments": {"order_id": "1"},
                }
            }
        )


def test_the_variants_docstrings_are_the_tool_descriptions() -> None:
    defs = schema_of(STEP)["$defs"]

    for tool in PAWS.enabled_tools:
        assert defs[variant_name(tool)]["description"] == TOOL_DESCRIPTIONS[tool]


def test_the_executor_used_here_is_the_real_one() -> None:
    # The fakes' schema is built from the committed configuration through the real executor.
    assert EXECUTOR.enabled == tuple(sorted(PAWS.enabled_tools))


def test_one_tool_is_a_direct_variant_never_a_nested_union() -> None:
    # ST3 (PR 4 mutation review): the fingerprint pins only the canonical multi-tool business, so
    # a one-tool business's schema (prompt text) is pinned here: a direct $ref, no "tool"
    # discriminator nested inside the "action" one.
    one = build_agent_step(
        ToolExecutor(
            NO_DATABASE, PAWS.model_copy(update={"enabled_tools": (ToolName.ORDER_STATUS,)})
        )
    )

    step = one.model_json_schema()["properties"]["step"]

    assert step["oneOf"] == [
        {"$ref": "#/$defs/CallOrderStatus"},
        {"$ref": "#/$defs/Reply"},
        {"$ref": "#/$defs/Handoff"},
    ]
    assert step["discriminator"] == {
        "mapping": {
            "call_tool": "#/$defs/CallOrderStatus",
            "handoff": "#/$defs/Handoff",
            "reply": "#/$defs/Reply",
        },
        "propertyName": "action",
    }
