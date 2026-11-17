"""M7 PR 2: the freeze and the rule that gate labels only tighten (`redteam/freeze.py`,
`checks.freeze_dataset`, `files.write_frozen`), on a TEST-ONLY dataset in tmp_path."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from tests.redteam.helpers import case, g_case, write_corpus, write_dataset
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.redteam.cases import Gates, Label
from vectapilot_api.redteam.checks import check_dataset, freeze_dataset, generator_of
from vectapilot_api.redteam.files import read_dataset, write_frozen
from vectapilot_api.redteam.freeze import file_sha256, loosening_problems, tightenings
from vectapilot_api.redteam.pii import DEFAULT_SEED, GENERATOR_VERSION, PiiGenerator, PiiType

DAY = date(2026, 11, 16)
CASES = [case(), case(id="l-002"), g_case(), g_case(id="g-002", obfuscation="leetspeak")]
LABELS = {"l-002": "soft", "g-002": "known_gap"}


def frozen_dataset(tmp_path: Path) -> tuple[Path, Path]:
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, CASES, labels=LABELS, max_over_escalated=1)
    write_frozen(dataset, freeze_dataset(read_dataset(dataset, corpus), frozen_on=DAY))
    return dataset, corpus


def test_freeze_pins_the_files_labels_generator_and_counts(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    freeze = json.loads((dataset / "freeze.json").read_text("utf-8"))
    assert freeze["cases_sha256"] == file_sha256((dataset / "cases.jsonl").read_bytes())
    assert freeze["gates_sha256"] == file_sha256((dataset / "gates.json").read_bytes())
    assert freeze["gates"]["labels"] == {"l-001": "hard", "g-001": "hard"} | LABELS
    assert freeze["pii"]["version"] == GENERATOR_VERSION
    assert freeze["pii"]["seed"] == DEFAULT_SEED
    assert set(freeze["pii"]["vectors"]) == {kind.value for kind in PiiType}
    manifest = json.loads((dataset / "manifest.json").read_text("utf-8"))
    assert manifest["stage"] == "frozen"
    assert manifest["counts"]["total"] == 4
    assert manifest["counts"]["categories"]["G"] == 2 and manifest["counts"]["categories"]["A"] == 0
    assert check_dataset(read_dataset(dataset, corpus)).ok


def test_freeze_json_holds_no_generated_value(tmp_path: Path) -> None:
    dataset, _ = frozen_dataset(tmp_path)
    text = (dataset / "freeze.json").read_text("utf-8")
    generator = PiiGenerator()
    for kind in PiiType:
        from vectapilot_api.redteam.pii import VECTOR_CASE_ID, Placeholder

        assert generator.value(VECTOR_CASE_ID, Placeholder(kind, 1)) not in text


def test_a_frozen_version_refuses_a_second_freeze_but_may_be_refrozen(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read_dataset(dataset, corpus), frozen_on=DAY)
    assert "already frozen: --refreeze" in refused.value.problems[0]
    with (dataset / "cases.jsonl").open("a", encoding="utf-8") as cases:
        cases.write(json.dumps(case(id="l-003")) + "\n")
    gates = json.loads((dataset / "gates.json").read_text("utf-8"))
    gates["labels"]["l-003"] = "hard"
    (dataset / "gates.json").write_text(json.dumps(gates), "utf-8")
    again = freeze_dataset(
        read_dataset(dataset, corpus), frozen_on=date(2026, 11, 17), refreeze=True
    )
    assert again.refrozen_from == DAY  # reported by the CLI
    write_frozen(dataset, again)
    assert check_dataset(read_dataset(dataset, corpus)).ok


@pytest.mark.parametrize(
    ("cases", "refreeze", "reason"),
    [
        ([], False, "cases.jsonl has no case"),
        ([case(language="fr")], False, "case l-001 language"),
        ([case()], True, "is not frozen: freeze it without --refreeze"),
    ],
)
def test_freeze_refusals_write_nothing(
    tmp_path: Path, cases: list[dict[str, Any]], refreeze: bool, reason: str
) -> None:
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, cases)
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read_dataset(dataset, corpus), frozen_on=DAY, refreeze=refreeze)
    assert any(p.startswith(reason) or reason in p for p in refused.value.problems)
    assert not (dataset / "freeze.json").exists()


def test_a_changed_case_after_the_freeze_is_a_new_version(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    text = (dataset / "cases.jsonl").read_text("utf-8").replace("Saturday", "Sunday", 1)
    (dataset / "cases.jsonl").write_text(text, "utf-8")
    problems = check_dataset(read_dataset(dataset, corpus)).problems
    assert problems == (
        "cases.jsonl changed after the freeze; v0.1 is frozen: make a new version instead (v0.2,"
        " with the reason recorded)",
    )


def test_a_changed_generator_or_count_after_the_freeze_is_a_problem(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    freeze = json.loads((dataset / "freeze.json").read_text("utf-8"))
    freeze["pii"]["vectors"]["aadhaar"] = "0" * 64
    (dataset / "freeze.json").write_text(json.dumps(freeze), "utf-8")
    manifest = json.loads((dataset / "manifest.json").read_text("utf-8"))
    manifest["counts"] = {"total": 5, "categories": {"G": 2, "L": 3}}
    (dataset / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    problems = check_dataset(read_dataset(dataset, corpus)).problems
    assert [p.split(";")[0] for p in problems] == [
        "the PII generator, its seed or its test vectors changed",
        "the cases' counts differ from the manifest's pinned counts",
    ]


def test_a_freeze_of_another_version_is_a_problem(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    freeze = json.loads((dataset / "freeze.json").read_text("utf-8"))
    freeze["version"] = "v0.2"
    (dataset / "freeze.json").write_text(json.dumps(freeze), "utf-8")
    assert check_dataset(read_dataset(dataset, corpus)).problems == (
        "freeze.json belongs to another dataset or version",
    )


def test_the_frozen_seed_is_the_runs_seed(tmp_path: Path) -> None:
    # Every other test freezes with DEFAULT_SEED, which would hide a reader that ignores the pin.
    corpus = write_corpus(tmp_path)
    dataset = write_dataset(tmp_path, [case()])
    frozen = freeze_dataset(read_dataset(dataset, corpus), frozen_on=DAY, generator=PiiGenerator(7))
    write_frozen(dataset, frozen)

    read = read_dataset(dataset, corpus)

    assert generator_of(read).seed == 7  # not DEFAULT_SEED
    assert check_dataset(read).ok


def test_a_freeze_file_while_collecting_is_a_problem(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    manifest = json.loads((dataset / "manifest.json").read_text("utf-8"))
    manifest["stage"], manifest["counts"] = "collecting", None
    (dataset / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    assert check_dataset(read_dataset(dataset, corpus)).problems == (
        "freeze.json exists while the manifest's stage is collecting",
    )


def test_gates_may_tighten_after_the_freeze_and_check_says_so(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    gates = json.loads((dataset / "gates.json").read_text("utf-8"))
    gates["labels"]["g-002"] = "hard"
    gates["max_over_escalated"] = 0
    (dataset / "gates.json").write_text(json.dumps(gates), "utf-8")
    result = check_dataset(read_dataset(dataset, corpus))
    assert result.ok
    assert result.warnings == (
        "tightened since the freeze: case g-002: known_gap -> hard",
        "tightened since the freeze: max_over_escalated lowered",
    )


def test_a_loosened_gate_after_the_freeze_is_refused(tmp_path: Path) -> None:
    dataset, corpus = frozen_dataset(tmp_path)
    gates = json.loads((dataset / "gates.json").read_text("utf-8"))
    gates["labels"]["l-001"] = "soft"
    gates["max_over_escalated"] = 2
    (dataset / "gates.json").write_text(json.dumps(gates), "utf-8")
    problems = check_dataset(read_dataset(dataset, corpus)).problems
    assert any("raises max_over_escalated above the frozen ceiling" in p for p in problems)
    assert "gates.json: case l-001 moved from hard to soft" in "\n".join(problems)


def gates(label: Label, ceiling: int = 1) -> Gates:
    return Gates(max_over_escalated=ceiling, labels={"a-001": label})


MOVES: list[tuple[Label, Label, bool]] = [
    ("known_gap", "hard", True),
    ("soft", "hard", True),
    ("hard", "hard", True),
    ("soft", "soft", True),
    ("known_gap", "known_gap", True),
    ("hard", "soft", False),
    ("hard", "known_gap", False),
    ("soft", "known_gap", False),
    ("known_gap", "soft", False),
]


@pytest.mark.parametrize(("before", "after", "allowed"), MOVES)
def test_only_moves_to_hard_are_allowed(before: Label, after: Label, allowed: bool) -> None:
    problems = loosening_problems(gates(before), gates(after))
    assert (problems == []) == allowed
    assert bool(tightenings(gates(before), gates(after))) == (allowed and before != after)


def test_the_ceiling_may_only_go_down_and_cases_may_not_come_or_go() -> None:
    assert loosening_problems(gates("hard", 1), gates("hard", 0)) == []
    assert loosening_problems(gates("hard", 1), gates("hard", 2))
    other = Gates(max_over_escalated=1, labels={"a-002": "hard"})
    assert loosening_problems(gates("hard"), other) == [
        "gates.json: case a-001's label was removed after the freeze",
        "gates.json: case a-002's label was added after the freeze",
    ]
