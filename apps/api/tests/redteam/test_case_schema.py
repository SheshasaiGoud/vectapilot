"""M7 PR 2: the case schema (`redteam/cases.py`) against the scoring rules
(`docs/plans/m7-pr-checks.md`, "Scoring rules for the red-team suite").

Every vocabulary is pinned to the plan's list, so widening one is a plan change, not an edit. A
line that does not validate names its line, case id and fields, never the offending value.
"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from tests.redteam.helpers import case, g_case
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.redteam.cases import (
    G_IN,
    G_OUT,
    PLAIN_EXPECTS,
    Case,
    Category,
    Counts,
    Obfuscation,
    Outage,
    Route,
    Surface,
    dump_case,
    load_manifest,
    masked_labels,
    parse_cases,
)
from vectapilot_api.redteam.pii import MaskLabel

LEAK = "SECRET-VALUE-7Q"
"""A distinctive bad value: no problem text may ever contain it."""


def one(values: dict[str, Any]) -> tuple[tuple[Case, ...], tuple[str, ...]]:
    return parse_cases(json.dumps(values) + "\n")


def test_the_vocabularies_are_the_plans() -> None:
    assert [c.value for c in Category] == list("ABCDEFGHIJKL")
    assert {s.value for s in Surface} == {
        "message",
        "chunk",
        "tool_output",
        "ticket_text",
        "document",
        "reply",
        "redis",
        "log",
    }
    assert {r.value for r in Route} == {
        "agent",
        "confirm",
        "answer",
        "search",
        "extraction",
        "limiter",
        "scan",
    }
    assert {
        "urgent_handoff",
        "high_handoff",
        "handoff",
        "withheld",
        "dropped",
        "write_tools_removed",
        "allowed",
        "blocked_429",
        "no_leak",
    } == PLAIN_EXPECTS
    assert {o.value for o in G_IN} == {
        "none",
        "case",
        "zero_width",
        "joiner",
        "homoglyph",
        "accent",
        "punctuation",
        "whitespace",
        "word_order",
    }
    assert {o.value for o in G_OUT} == {
        "intra_word_space",
        "typo",
        "leetspeak",
        "paraphrase",
        "transliteration",
    }
    assert set(Obfuscation) == G_IN | G_OUT and not G_IN & G_OUT
    # The plan's "Design points left to their PRs": the closed J outage key.
    assert {o.value for o in Outage} == {
        "redis_down",
        "budget_exhausted",
        "provider_failing",
        "guard_raising",
        "burst",
    }
    assert {m.value for m in MaskLabel} == {
        "phone",
        "email",
        "aadhaar",
        "pan",
        "card",
        "upi",
        "ifsc",
        "passport",
        "id_number",
    }


def test_a_valid_case_round_trips() -> None:
    cases, problems = one(case())
    assert problems == () and len(cases) == 1
    assert json.loads(dump_case(cases[0]))["id"] == "l-001"
    assert cases[0].fixture.scripted_model == ()


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"category": LEAK}, "category"),
        ({"surface": LEAK}, "surface"),
        ({"route": LEAK}, "route"),
        ({"expect": LEAK}, "expect"),
        ({"author": LEAK}, "author"),
        ({"reviewed_by": [LEAK]}, "reviewed_by"),
        ({"synthetic": False}, "synthetic"),
        ({"fixture": {"outage": LEAK}}, "fixture.outage"),
        ({"fixture": {"chunks": [{"id": "c1", "text": "x", "audience": LEAK}]}}, "fixture"),
        ({"fixture": {"scripted_model": [{"action": LEAK}]}}, "fixture.scripted_model"),
        ({"surplus": LEAK}, "surplus"),
    ],
)
def test_an_unknown_value_is_refused_naming_line_case_and_field_never_the_value(
    changes: dict[str, Any], field: str
) -> None:
    cases, problems = one(case(**changes))
    assert cases == ()
    assert len(problems) == 1
    assert problems[0].startswith("cases.jsonl line 1 (case l-001) is not a valid case: ")
    assert field in problems[0]
    assert LEAK not in problems[0]


def test_g_obfuscation_must_be_one_of_the_closed_classes() -> None:
    _, problems = one(g_case(obfuscation=LEAK))
    assert len(problems) == 1 and "obfuscation" in problems[0] and LEAK not in problems[0]


def test_a_malformed_line_names_only_its_line() -> None:
    _, problems = parse_cases('{"id": "' + LEAK + '"\n\n{not json\n')
    assert [p.split(" is not")[0] for p in problems] == ["cases.jsonl line 1", "cases.jsonl line 3"]
    assert all(LEAK not in p for p in problems)


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"id": "a-001"}, "starts with its category's letter"),
        ({"id": "g-001", "category": "G"}, "a G case has an obfuscation class"),
        ({"obfuscation": "none"}, "a G case has an obfuscation class"),
        ({"code_mixed": True}, "hi mixed with English"),
        ({"fixture": {"outage": "redis_down"}}, "only a J case names an outage"),
        ({"reviewed_by": ["shesha", "shesha"]}, "each reviewer is listed once"),
        ({"fixture": {"context": {"cited": ["c9"]}}}, "context.cited names chunks"),
        (
            {"fixture": {"chunks": [{"id": "c1", "text": "x", "audience": "public"}] * 2}},
            "each chunk id is listed once",
        ),
        ({"expect": "masked:phone,email"}, "each label once, sorted"),
        ({"expect": "masked:phone,phone"}, "each label once, sorted"),
        ({"expect": "masked:name"}, "G6 labels only"),
        ({"expect": "masked:"}, "closed outcomes or masked"),
    ],
)
def test_model_rules(changes: dict[str, Any], rule: str) -> None:
    _, problems = one(case(**changes))
    assert len(problems) == 1 and rule in problems[0], problems


def test_a_j_case_may_name_its_outage_and_the_masked_labels_parse() -> None:
    cases, problems = one(
        case(
            id="j-001",
            category="J",
            fixture={"outage": "redis_down"},
            expect="urgent_handoff",
        )
    )
    assert problems == () and cases[0].fixture.outage is Outage.REDIS_DOWN
    assert masked_labels("masked:email,phone") == (MaskLabel.EMAIL, MaskLabel.PHONE)
    assert masked_labels("allowed") is None


def test_tool_fields_and_arguments_are_named_as_the_tools_name_them() -> None:
    """`order_id`, `slot_id`, `date_from`: the tools' own names (B2's fix; a slug has no `_`)."""
    fixture = {
        "tool_results": [{"tool": "order_status", "fields": {"order_id": "RT-0204", "note": "x"}}],
        "scripted_model": [
            {"action": "call_tool", "tool": "order_status", "arguments": {"order_id": "RT-0204"}}
        ],
    }
    values = case(id="c-001", category="C", surface="tool_output", fixture=fixture)
    cases, problems = one(values)
    assert problems == () and "order_id" in cases[0].fixture.tool_results[0].fields
    for bad in ("Order_id", "order-id", "1st", "_x"):
        broken = json.loads(json.dumps(values))
        broken["fixture"]["scripted_model"][0]["arguments"] = {bad: "x"}
        _, problems = one(broken)
        assert len(problems) == 1 and "scripted_model" in problems[0], bad


@pytest.mark.parametrize(
    ("values", "subgroup"),
    [
        (g_case(), "G-in"),
        (g_case(obfuscation="paraphrase"), "G-out"),
        (case(id="h-001", category="H", expect="high_handoff"), "H-req"),
        (case(id="h-002", category="H", surface="reply", expect="handoff"), "H-out"),
        (case(), "L"),
    ],
)
def test_subgroups(values: dict[str, Any], subgroup: str) -> None:
    cases, problems = one(values)
    assert problems == () and cases[0].subgroup == subgroup


def test_code_mixed_hindi_is_its_own_language_row() -> None:
    cases, _ = one(case(language="hi", code_mixed=True))
    assert cases[0].language_row == "hi-code-mixed"
    cases, _ = one(case(language="hi"))
    assert cases[0].language_row == "hi"


def test_counts_add_up_and_are_not_negative() -> None:
    with pytest.raises(ValidationError):
        Counts.model_validate({"total": 2, "categories": {"A": 1}})
    with pytest.raises(ValidationError):
        Counts.model_validate({"total": 1, "categories": {"A": 2, "B": -1}})


def manifest(stage: str, counts: dict[str, Any] | None) -> str:
    return json.dumps(
        {
            "dataset": "redteam",
            "version": "v0.1",
            "created": "2026-11-16",
            "synthetic": True,
            "authored_by": "TEST-ONLY",
            "stage": stage,
            "corpus": "corpus",
            "counts": counts,
        }
    )


def test_only_a_frozen_manifest_pins_counts() -> None:
    counts = {"total": 1, "categories": {"A": 1}}
    with pytest.raises(DatasetError):
        load_manifest(manifest("frozen", None))
    with pytest.raises(DatasetError):
        load_manifest(manifest("collecting", counts))
    assert load_manifest(manifest("frozen", counts)).counts is not None
