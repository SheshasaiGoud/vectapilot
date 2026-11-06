"""M4 PR 1: the training and eval row schemas, the manifest, and their loaders."""

import json
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.intenteval.helpers import (
    DATASET_DIR,
    eval_data,
    eval_row,
    manifest_data,
    train_data,
    train_row,
)
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.rows import (
    GROUPS_OF_LANGUAGE,
    TEXT_MAX_CHARS,
    EvalRow,
    TrainRow,
    dump_row,
    load_eval,
    load_manifest,
    load_train,
    repeated_ids,
)
from vectapilot_api.intents.taxonomy import Intent

MARKER: Final = "TEST-ONLY never-in-an-error 4f1c"
"""A text that must never appear in an error message."""


def test_a_training_row_and_an_eval_row_load() -> None:
    train = train_row()
    assert (train.author, train.verified, train.intent) == ("claude", False, Intent.QUESTION)
    row = eval_row(split_group="example-group", steering=True)
    assert (row.author, row.split, row.split_group, row.steering) == (
        "shesha",
        None,
        "example-group",
        True,
    )
    assert (eval_row().steering, eval_row().split_group) == (False, None)


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (TrainRow, {"author": "shesha"}),  # people write eval rows, not training rows
        (TrainRow, {"author": "friend"}),
        (EvalRow, {"author": "claude"}),  # agents never write an eval row (ADR 0012)
        (EvalRow, {"author": "kenji"}),
    ],
)
def test_the_author_decides_the_file(build: type[TrainRow | EvalRow], overrides: Any) -> None:
    data = (train_data if build is TrainRow else eval_data)(**overrides)
    with pytest.raises(ValidationError):
        build.model_validate(data)


def test_a_training_row_needs_verified_and_takes_no_eval_field() -> None:
    data = train_data()
    del data["verified"]
    with pytest.raises(ValidationError):
        TrainRow.model_validate(data)
    for field, value in (("split", "dev"), ("split_group", "example-g"), ("steering", True)):
        with pytest.raises(ValidationError):
            TrainRow.model_validate(train_data(**{field: value}))


def test_an_eval_row_takes_no_verified_field() -> None:
    with pytest.raises(ValidationError):
        EvalRow.model_validate(eval_data(verified=True))


@pytest.mark.parametrize(
    "overrides",
    [
        {"language": "te"},  # no Telugu rows in M4
        {"language": "hi-Latn"},  # Hinglish is hi + code_mixed
        {"language": "en", "code_mixed": True},
        {"intent": "safety"},  # support_qa's category, not an intent
        {"synthetic": False},
        {"text": ""},
        {"text": "   \n\t"},
        {"text": "x" * 501},
        {"business_id": "Sunrise Clinic"},
        {"id": "Row 1"},
        {"notes": "anything"},  # no free-text field beyond `text`
    ],
)
def test_a_row_breaking_a_rule_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        eval_row(**overrides)


def test_the_text_limit_is_inclusive_and_hinglish_is_hindi_code_mixed() -> None:
    # A literal, not the constant: a test built from TEXT_MAX_CHARS moves with it. The README
    # and the writers' brief state 500 (test_dataset_intents_v0_1 pins that too).
    assert TEXT_MAX_CHARS == 500
    assert len(eval_row(text="x" * 500).text) == 500
    assert len(train_row(text="x" * 500).text) == 500
    with pytest.raises(ValidationError):
        train_row(text="x" * 501)
    groups = [
        eval_row(language="en").language_group,
        eval_row(language="hi").language_group,
        eval_row(language="hi", code_mixed=True).language_group,
    ]
    assert groups == ["en", "hi", "hi-code-mixed"]
    assert dict(GROUPS_OF_LANGUAGE) == {"en": ("en",), "hi": ("hi", "hi-code-mixed")}


def test_loaders_skip_blank_lines_and_read_an_empty_file_as_no_rows() -> None:
    assert load_train("") == ()
    assert load_eval("\n\n") == ()
    line = json.dumps(eval_data())
    assert [r.id for r in load_eval(f"\n{line}\n\n")] == ["example-e1"]


def test_a_bad_line_is_named_by_file_line_and_field_never_by_its_text() -> None:
    good = json.dumps(train_data(id="example-t1"))
    bad = json.dumps(train_data(id="example-t2", text=MARKER, intent="safety"))
    with pytest.raises(DatasetError) as caught:
        load_train(f"{good}\n{bad}\nnot json at all\n")
    message = str(caught.value)
    assert "train.jsonl line 2 is not a valid row: intent" in message
    assert "train.jsonl line 3 is not a valid row" in message
    assert MARKER not in message


def test_dump_row_round_trips_and_keeps_hindi_readable() -> None:
    row = eval_row(language="hi", text="TEST-ONLY परीक्षण")
    line = dump_row(row)
    assert "परीक्षण" in line
    assert EvalRow.model_validate_json(line) == row


def test_ids_are_unique_across_both_files() -> None:
    train = (train_row(id="example-a"), train_row(id="example-b"))
    evals = (eval_row(id="example-b"), eval_row(id="example-c"), eval_row(id="example-c"))
    assert repeated_ids(train, evals) == ["example-b", "example-c"]


def test_the_committed_manifest_loads() -> None:
    loaded = load_manifest((DATASET_DIR / "manifest.json").read_text("utf-8"))
    assert loaded.dataset == "intents"


@pytest.mark.parametrize(
    "overrides",
    [
        {"businesses": ["sunrise-clinic", "sunrise-clinic"]},
        {"businesses": []},
        {"near_duplicate": {"method": "char-3gram-jaccard", "threshold_percent": 0}},
        {"near_duplicate": {"method": "char-3gram-jaccard", "threshold_percent": 101}},
        {"near_duplicate": {"method": "cosine", "threshold_percent": 70}},
        {"verification": {"percent": 0, "seed": "s"}},
        {"split_rule": {"rule": "stratified-systematic-v1", "seed": "s", "test_percent": 50}},
        {"stage": "draft"},
        {"synthetic": False},
        {"train_draft_sha256": "not-a-hash"},
    ],
)
def test_a_bad_manifest_is_refused_naming_its_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(DatasetError, match=r"manifest.json is not a valid intents manifest: "):
        load_manifest(json.dumps(manifest_data(**overrides)))
