"""The run files' models (`extracteval/runinfo.py`): results.jsonl takes ids, codes, counts and type
names only, so a value cannot slip in through a free-text field; values.jsonl is apart."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.extracteval.runjson import extract_run_info
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval.runinfo import (
    RESULTS_FILE,
    DocumentLine,
    ExtractRunInfo,
    FieldLine,
    ValueLine,
    json_line,
    read_results,
)


def field(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "field": "patient_name",
        "type": "text",
        "status": "review",
        "level": "medium",
        "reasons": ("unsure",),
        "page": 1,
        "quote_hits": 1,
        "has_value": True,
        "gold_null": False,
        "correct": True,
        "routed_level": "medium",
        "proposal_correct": True,
    }
    return base | changes


def document(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "s-1",
        "business_id": "sunrise-clinic",
        "kind": "referral_letter",
        "split": "dev",
        "tags": [],
        "run": "scored",
        "status": "needs_review",
        "fields": [field()],
    }
    return base | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"reasons": ("Asha Rao",)},
        {"reasons": ("unsure", "bad_date")},
        {"reasons": ("unsure", "unsure")},
        {"status": None},
        {"status": None, "correct": None},  # not scored, yet a level, reasons and a value
        {"correct": None},  # scored, yet not scored as correct or not (mutation audit I04)
        {"page": 0},
        {"quote_hits": -1},
        {"type": "number"},
        {"value": "Asha Rao"},
        {"proposal_correct": None},  # scored, yet the proposal is not scored
        {"routed_level": "low"},  # a stored level is the routed level
        {"routed_level": "certain"},
    ],
)
def test_a_field_line_holds_codes_only(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FieldLine.model_validate(field(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"error": "ValueError: Asha Rao"},
        {"failure": "Model said: no"},
        {"status": None},
        {"run": "not_run"},
        {"fields": [field(), field()]},
        {"fields": []},
        {"id": "S 1"},
        {"cost_usd": "-0.01"},
        {"quote": "Patient name: Asha Rao"},
    ],
)
def test_a_document_line_holds_ids_codes_and_counts_only(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        DocumentLine.model_validate(document(**changes))


def test_a_document_that_is_not_scored_has_no_scored_field() -> None:
    blank = field(
        status=None,
        level=None,
        routed_level=None,
        reasons=(),
        has_value=False,
        correct=None,
        proposal_correct=None,
        page=None,
    )
    line = DocumentLine.model_validate(document(run="not_run", status=None, fields=[blank]))
    assert line.fields[0].correct is None
    with pytest.raises(ValidationError):  # not scored, yet a routed level
        FieldLine.model_validate(blank | {"routed_level": "low"})
    DocumentLine.model_validate(document(error="OutputTruncated", failure="truncated"))


def test_results_round_trip_and_name_a_bad_line_never_its_text(tmp_path: Path) -> None:
    line = DocumentLine.model_validate(document())
    (tmp_path / RESULTS_FILE).write_text(json_line(line) + '\n{"id": "Asha Rao"}\n', "utf-8")
    with pytest.raises(DatasetError) as bad:
        read_results(tmp_path)
    assert "results.jsonl line 3 is not valid" in str(bad.value)
    assert "Asha" not in str(bad.value)
    (tmp_path / RESULTS_FILE).write_text(json_line(line), "utf-8")
    assert read_results(tmp_path) == (line,)


def test_a_repeated_document_id_is_refused_naming_it(tmp_path: Path) -> None:
    # H2 counts one line per document: a repeat would double-count, silently when correct.
    line = DocumentLine.model_validate(document())
    (tmp_path / RESULTS_FILE).write_text(json_line(line) + "\n" + json_line(line), "utf-8")
    with pytest.raises(DatasetError) as repeated:
        read_results(tmp_path)
    assert str(repeated.value) == (
        f"{RESULTS_FILE} line 3 repeats document {line.id} (first on line 1)"
    )


def test_a_dropped_value_keeps_its_routed_level_and_its_proposals_score() -> None:
    # "No quote, no value" cleared the stored level and value; the routed level stays apart.
    dropped = field(
        level=None,
        has_value=False,
        page=None,
        gold_null=True,
        correct=True,
        routed_level="low",
        proposal_correct=False,
        unverified=True,
    )
    line = FieldLine.model_validate(dropped)
    assert (line.level, line.routed_level, line.correct, line.proposal_correct) == (
        None,
        "low",
        True,
        False,
    )


def test_values_live_in_values_jsonl_only() -> None:
    value = ValueLine(
        id="s-1",
        field="patient_name",
        value=None,
        proposed="Asha",
        quote="Patient: Asha",
        gold=None,
    )
    assert value.proposed == "Asha"
    assert "value" not in FieldLine.model_fields and "quote" not in DocumentLine.model_fields


def test_run_json_records_the_leak_flag_and_the_retries() -> None:
    info = extract_run_info("r")
    assert info.leak is False
    assert (info.fingerprint.limits.max_retries, info.fingerprint.limits.max_repairs) == (2, 1)


@pytest.mark.parametrize("reason", ["Asha Rao", "fix x", "a" * 81, ""])
def test_run_json_takes_a_slug_rerun_reason_only(reason: str) -> None:
    info = extract_run_info("r", split="test")
    data = {**info.model_dump(), "test_rerun_reason": reason}
    with pytest.raises(ValidationError):
        ExtractRunInfo.model_validate(data)
    assert ExtractRunInfo.model_validate({**data, "test_rerun_reason": "fix-x"}).test_rerun_reason
