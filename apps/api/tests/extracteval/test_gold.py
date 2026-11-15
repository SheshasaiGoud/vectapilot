"""M6 PR 4: the manifest, slot and gold-row models (`extracteval/gold.py`), shape only.

A canary in a value never reaches an error (`hide_input_in_errors`); the dataset's pure modules
never import `vectapilot_api.extraction`, so the eval's own comparison can stay independent of
the code it scores (scoring rules).
"""

import ast
import json
from pathlib import Path
from typing import Any, Final

import pytest

from tests.extracteval.helpers import COMMITTED, PAWS, SUNRISE, gold
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval import freeze as freeze_module
from vectapilot_api.extracteval import gold as gold_module
from vectapilot_api.extracteval import source as source_module
from vectapilot_api.extracteval.gold import (
    MANIFEST_FILE,
    dump_gold,
    load_gold,
    load_manifest,
    repeated_ids,
)

CANARY: Final = "CANARY-55e1"


def manifest(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((COMMITTED / MANIFEST_FILE).read_text("utf-8"))
    return data | changes


def manifest_problem(**changes: Any) -> str:
    with pytest.raises(DatasetError) as caught:
        load_manifest(json.dumps(manifest(**changes)))
    assert CANARY not in str(caught.value)
    return str(caught.value)


def test_the_committed_manifest_loads() -> None:
    loaded = load_manifest((COMMITTED / MANIFEST_FILE).read_text("utf-8"))
    assert loaded.stage in ("collecting", "frozen") and loaded.synthetic is True  # shesha freezes
    assert loaded.slot("sunrise-01") is not None and loaded.slot("nope") is None


def test_the_manifest_refuses_bad_slots_and_businesses() -> None:
    slots = manifest()["slots"]
    assert "each slot id is listed once" in manifest_problem(slots=[*slots, slots[0]])
    stranger = {**slots[0], "id": "x-1", "business_id": "other-business"}
    assert "one of the manifest's businesses" in manifest_problem(slots=[*slots, stranger])
    assert "each business is listed once" in manifest_problem(businesses=[SUNRISE, SUNRISE])
    doubled = {**slots[0], "tags": ["absent", "absent"]}
    assert "each tag is listed once" in manifest_problem(slots=[doubled])
    assert "slots.0.tags" in manifest_problem(slots=[{**slots[0], "tags": [CANARY]}])
    assert "synthetic" in manifest_problem(synthetic=False)
    assert "slots" in manifest_problem(slots=[])
    negative = {**manifest()["requirements"], "min_tag_per_business": {"absent": -1}}
    assert "a tag minimum is 0 or more" in manifest_problem(requirements=negative)


def test_a_gold_row_round_trips_and_keeps_its_field_order() -> None:
    row = gold("s-1")
    (loaded,) = load_gold(json.dumps(row) + "\n\n")
    assert json.loads(dump_gold(loaded)) == row
    assert list(loaded.fields) == list(row["fields"])
    assert CANARY not in repr(load_gold(json.dumps(gold("s-1", fields={"insurer": CANARY})))[0])


@pytest.mark.parametrize(
    ("changes", "where"),
    [
        ({"fields": {"insurer": f" {CANARY}"}}, "fields"),
        ({"fields": {"insurer": f"{CANARY}\nx"}}, "fields"),
        ({"fields": {"insurer": ""}}, "fields.insurer"),
        ({"fields": {}}, "fields"),
        ({"fields": {"Not Snake": CANARY}}, "fields.Not Snake"),
        ({"tags": ["absent", "absent"]}, "tags"),
        ({"tags": ["urgent"]}, "tags.0"),
        ({"author": "claude"}, "author"),
        ({"synthetic": False}, "synthetic"),
        ({"split": "train"}, "split"),
        ({"id": "Not_A_Slug"}, "id"),
        ({"extra": CANARY}, "extra"),
    ],
)
def test_a_bad_gold_row_is_named_by_line_and_field_never_by_value(
    changes: dict[str, Any], where: str
) -> None:
    text = json.dumps(gold("s-1")) + "\n" + json.dumps(gold("s-2", PAWS) | changes) + "\n"
    with pytest.raises(DatasetError) as caught:
        load_gold(text)
    message = str(caught.value)
    assert message.startswith("gold.jsonl line 2 is not a valid gold row: ")
    assert where in message
    assert CANARY not in message


def test_an_empty_gold_file_is_zero_rows_and_repeats_are_found() -> None:
    assert load_gold("") == ()
    rows = load_gold("\n".join(json.dumps(gold(i)) for i in ("a", "b", "a")))
    assert repeated_ids(rows) == ["a"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def test_the_pure_dataset_modules_never_import_the_extraction_code() -> None:
    for module in (gold_module, source_module, freeze_module):
        assert module.__file__ is not None
        imported = _imports(Path(module.__file__))
        assert not any(name.startswith("vectapilot_api.extraction") for name in imported), module
