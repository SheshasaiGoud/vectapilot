"""M7 PR 1: the guards' closed codes, terms, `GuardDecision` and the metadata-only `guard`
record (G2, G10)."""

import json
import logging
from typing import Any
from uuid import UUID

import pytest

from vectapilot_api.guards.types import (
    GUARD_RECORD,
    OUTPUT_CATEGORIES,
    RED_FLAG_CATEGORIES,
    UNCHANGED,
    Check,
    DecidedBy,
    Finding,
    GuardAction,
    GuardDecision,
    GuardRecord,
    NearTerm,
    PhraseTerm,
    RouteSlug,
    TermCategory,
    TermKind,
    TermSet,
    emit_guard_record,
    red_flag_intent,
)
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.log import configure_logging

TRACE = "0123456789abcdef0123456789abcdef"
SHA = "ab" * 32
HANDOFF = GuardDecision(GuardAction.HANDOFF, DecidedBy.RED_FLAG_EMERGENCY, Priority.URGENT, True)


def record(**changes: Any) -> GuardRecord:
    fields: dict[str, Any] = {
        "business_id": "rt-shop",
        "trace_id": TRACE,
        "route": RouteSlug.AGENT,
        "rules_version": SHA,
        "decision": HANDOFF,
        "checks": (Check.CLEAN, Check.RED_FLAG),
        "findings": {Finding.RED_FLAG_EMERGENCY: 2},
        "durations_ms": {Check.RED_FLAG: 0.4},
    }
    return GuardRecord(**(fields | changes))


# ── codes ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("codes", [TermKind, TermCategory, Finding, DecidedBy, GuardAction, Check])
def test_codes_are_unique_lower_case_and_contain_no_space(codes: Any) -> None:
    values = [code.value for code in codes]
    assert len(set(values)) == len(values)
    assert all(v == v.lower() and " " not in v for v in values)


def test_every_red_flag_category_is_m4_s_intent_finding_and_decision() -> None:
    assert {red_flag_intent(c) for c in RED_FLAG_CATEGORIES} == {
        Intent.EMERGENCY,
        Intent.PROFESSIONAL_JUDGEMENT,
    }
    for category in RED_FLAG_CATEGORIES:
        Finding(f"red_flag:{category}")
        DecidedBy(f"red_flag:{category}")


def test_every_output_category_has_its_finding_and_decision() -> None:
    for category in OUTPUT_CATEGORIES:
        Finding(f"output:{category}")
        DecidedBy(f"output:{category}")


def test_screens_categories_do_not_overlap() -> None:
    assert not RED_FLAG_CATEGORIES & OUTPUT_CATEGORIES
    assert TermCategory.INJECTION not in RED_FLAG_CATEGORIES | OUTPUT_CATEGORIES


@pytest.mark.parametrize("category", sorted(set(TermCategory) - RED_FLAG_CATEGORIES))
def test_only_a_red_flag_category_maps_to_an_intent(category: TermCategory) -> None:
    with pytest.raises(ValueError, match="red-flag"):
        red_flag_intent(category)


# ── terms ────────────────────────────────────────────────────────────────────────────────────


def test_a_phrase_term_is_its_words() -> None:
    term = PhraseTerm("t1", TermCategory.EMERGENCY, ("zorbly", "fizz"))
    assert term.kind is TermKind.PHRASE
    assert term.text == "zorbly fizz"


@pytest.mark.parametrize("words", [(), ("",), (" zorbly",), ("zorbly fizz",)])
def test_a_phrase_term_needs_non_empty_single_words(words: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="single words"):
        PhraseTerm("t1", TermCategory.EMERGENCY, words)


def test_a_near_term_has_two_sets_and_a_window_that_holds_them() -> None:
    term = NearTerm("n1", TermCategory.EMERGENCY, (("zorbly",), ("fizz", "fizzes")), 4)
    assert term.kind is TermKind.NEAR
    with pytest.raises(ValueError, match="two word sets"):
        NearTerm("n1", TermCategory.EMERGENCY, (("zorbly",),), 4)
    with pytest.raises(ValueError, match="window"):
        NearTerm("n1", TermCategory.EMERGENCY, (("a",), ("b",), ("c",)), 2)
    with pytest.raises(ValueError, match="single words"):
        NearTerm("n1", TermCategory.EMERGENCY, (("zorbly",), ()), 4)


def test_a_term_set_lists_its_categories() -> None:
    terms = TermSet(
        (
            PhraseTerm("a", TermCategory.EMERGENCY, ("zorbly",)),
            PhraseTerm("b", TermCategory.INJECTION, ("quuxify",)),
        )
    )
    assert terms.categories() == {TermCategory.EMERGENCY, TermCategory.INJECTION}


# ── GuardDecision ────────────────────────────────────────────────────────────────────────────


def test_unchanged_is_a_pass_decided_by_no_rule() -> None:
    assert (UNCHANGED.action, UNCHANGED.decided_by, UNCHANGED.priority, UNCHANGED.guidance) == (
        GuardAction.PASS,
        DecidedBy.NONE,
        None,
        False,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        (GuardAction.HANDOFF, DecidedBy.GUARD_ERROR, None, False),  # a hand-off without priority
        (
            GuardAction.DROP,
            DecidedBy.INJECTION_CHUNK,
            Priority.HIGH,
            False,
        ),  # priority, no hand-off
        (GuardAction.PASS, DecidedBy.GUARD_ERROR, None, False),  # a pass decided by a rule
        (GuardAction.HANDOFF, DecidedBy.NONE, Priority.HIGH, False),  # a hand-off by no rule
        (GuardAction.WITHHOLD, DecidedBy.INJECTION_TOOL_RESULT, None, True),  # guidance, no refusal
        (GuardAction.PASS, DecidedBy.NONE, None, True),
    ],
)
def test_an_inconsistent_decision_is_refused(arguments: Any) -> None:
    with pytest.raises(ValueError):
        GuardDecision(*arguments)


def test_guidance_goes_with_a_hand_off_or_a_429() -> None:
    GuardDecision(GuardAction.LIMIT, DecidedBy.LIMIT, guidance=True)
    GuardDecision(GuardAction.HANDOFF, DecidedBy.GUARD_ERROR, Priority.URGENT, guidance=True)


@pytest.mark.parametrize(
    "action",
    [GuardAction.DROP, GuardAction.WITHHOLD, GuardAction.READ_ONLY, GuardAction.UNAVAILABLE],
)
def test_guidance_goes_only_with_a_handoff_or_a_429(action: GuardAction) -> None:
    # Mutant t02 (a 503 allowed to carry guidance): every other refusal is pinned by name.
    with pytest.raises(ValueError, match="guidance goes only with a hand-off or a 429"):
        GuardDecision(action, DecidedBy.GUARD_ERROR, guidance=True)


# ── the `guard` record (G10) ─────────────────────────────────────────────────────────────────


def test_a_record_is_json_ready_in_a_fixed_key_order() -> None:
    action = UUID("12345678-1234-5678-1234-567812345678")
    extra = record(
        error_type="RuntimeError", reserved_micro_usd=1200, withheld_action_id=action
    ).as_extra()
    assert list(extra) == [
        "business_id", "trace_id", "route", "rules_version", "action", "decided_by", "priority",
        "guidance", "checks", "findings", "invisible", "error_type", "reserved_micro_usd",
        "settled_micro_usd", "budget_refused", "withheld_action_id", "durations_ms",
    ]  # fmt: skip
    assert json.loads(json.dumps(extra)) == extra
    assert extra["decided_by"] == "red_flag:emergency"
    assert extra["priority"] == "urgent"
    assert extra["findings"] == {"red_flag:emergency": 2}
    assert extra["withheld_action_id"] == str(action)


def test_a_record_is_frozen_including_its_mappings() -> None:
    findings = {Finding.INJECTION_PHRASE: 1}
    built = record(findings=findings)
    findings[Finding.INJECTION_PHRASE] = 99  # the caller's dict is copied
    assert built.findings[Finding.INJECTION_PHRASE] == 1
    with pytest.raises(TypeError):
        built.findings[Finding.OVER_CAP] = 1  # type: ignore[index]


def test_a_record_without_configured_guards_has_no_rules_version() -> None:
    decision = GuardDecision(GuardAction.HANDOFF, DecidedBy.GUARDS_NOT_CONFIGURED, Priority.HIGH)
    assert record(rules_version=None, decision=decision).as_extra()["rules_version"] is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"business_id": "Rt Shop"}, "business_id"),
        ({"business_id": "ab"}, "business_id"),
        ({"trace_id": "0" * 32}, "trace_id"),
        ({"trace_id": TRACE.upper()}, "trace_id"),
        ({"rules_version": "not-a-hash"}, "rules_version"),
        ({"checks": (Check.CLEAN, Check.CLEAN)}, "checks"),
        ({"checks": ("my chest hurts",)}, "checks"),
        ({"findings": {Finding.OVER_CAP: 0}}, "findings"),
        ({"findings": {"red_flag:chest_pain": 1}}, "findings"),
        ({"error_type": "ValueError: my chest hurts"}, "error_type"),
        ({"reserved_micro_usd": -1}, "micro_usd"),
        ({"settled_micro_usd": -5}, "micro_usd"),
        ({"invisible": -1}, "counts"),
        ({"budget_refused": -1}, "counts"),
        ({"durations_ms": {Check.CLEAN: float("nan")}}, "durations_ms"),
        ({"durations_ms": {Check.CLEAN: -1.0}}, "durations_ms"),
        ({"durations_ms": {"chest": 1.0}}, "durations_ms"),
    ],
)
def test_a_record_refuses_anything_but_codes_counts_and_checked_ids(
    changes: dict[str, Any], field: str
) -> None:
    with pytest.raises(ValueError) as raised:
        record(**changes)
    message = str(raised.value)
    assert message == f"invalid guard record: {field}"  # names the field, never the value
    assert "chest" not in message


def test_emit_writes_one_record_even_under_a_quieter_log_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, root.handlers[:]
    try:
        configure_logging("WARNING")  # as the app does at startup: JSON lines to stdout
        emit_guard_record(record())
    finally:
        root.setLevel(saved_level)
        root.handlers[:] = saved_handlers

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["message"] for line in lines] == [GUARD_RECORD]
    assert lines[0][GUARD_RECORD] == record().as_extra()
