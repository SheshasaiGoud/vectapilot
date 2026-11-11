"""The stage-aware check, D8's minimums and the freeze (`tooleval/checks.py`, `split.py`). Unit; a
throwaway repository per test, TEST-ONLY scenarios."""

import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

import pytest

from tests.tooleval.helpers import (
    LOW_MINIMUMS,
    PAWS,
    SUNRISE,
    TODAY,
    frozen_repo,
    letters,
    make_repo,
    parsed,
    read,
    scenario,
    write_scenarios,
)
from tests.tools.helpers import read_json, write_json
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.evalset.questions import Split
from vectapilot_api.tooleval import checks
from vectapilot_api.tooleval.checks import check_dataset, freeze_dataset, minimums
from vectapilot_api.tooleval.cli import WAY_OUT, main
from vectapilot_api.tooleval.scenarios import FREEZE_FILE, MANIFEST_FILE, SCENARIOS_FILE, Scenario
from vectapilot_api.tooleval.split import HINDI_GROUP, balance_problems


def problems_of(dataset_dir: Path, *, allow_placeholders: bool = True) -> list[str]:
    return list(check_dataset(read(dataset_dir), allow_placeholders=allow_placeholders).problems)


def book(scenario_id: str, slot: str = "S-09", **fields: Any) -> dict[str, Any]:
    return scenario(
        scenario_id,
        kind="confirmation_required",
        tool="book",
        arguments={"slot_id": slot},
        **fields,
    )


def test_an_empty_collecting_version_passes(tmp_path: Path) -> None:
    assert problems_of(make_repo(tmp_path)) == []


def test_valid_scenarios_pass_with_placeholders_allowed_and_fail_without(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(
        dataset_dir,
        [
            book("s-1", confirm=True),
            scenario(
                "s-2",
                SUNRISE,
                kind="confirmation_required",
                tool="reschedule",
                arguments={"booking_id": "B-103", "new_slot_id": "S-10"},
            ),
            scenario(
                "s-3",
                kind="confirmation_required",
                tool="create_ticket",
                arguments={"category": "order"},
            ),
            scenario(
                "s-4", SUNRISE, language="hi", code_mixed=True, kind="handoff", emergency=True
            ),
        ],
    )
    assert problems_of(dataset_dir) == []
    refused = problems_of(dataset_dir, allow_placeholders=False)
    assert len(refused) == 4
    assert all("placeholder" in p for p in refused)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (scenario("s-1", customer_ref="cust-999"), "seeded customers"),
        (book("s-1", slot="S-99"), "not a seeded slot"),
        (
            scenario(
                "s-1",
                kind="confirmation_required",
                tool="reschedule",
                arguments={"booking_id": "B-101", "new_slot_id": "S-99"},
            ),
            "new_slot_id that is not a seeded slot",
        ),
        (
            scenario(
                "s-1",
                kind="confirmation_required",
                tool="reschedule",
                arguments={"booking_id": "B-102", "new_slot_id": "S-10"},
            ),
            "customer's seeded bookings",  # B-102 is cust-002's
        ),
        (
            scenario(
                "s-1",
                SUNRISE,
                kind="confirmation_required",
                tool="create_ticket",
                arguments={"category": "order"},  # a Paws & Co category only
            ),
            "ticket category",
        ),
        (scenario("s-1", kind="answer"), "router=none never produces"),
        (scenario("s-1", PAWS, language="hi"), "does not list hi"),
        (scenario("s-1", split="dev"), "split is set by `freeze`"),
        (scenario("s-1", message="TEST-ONLY call me on 98765 43210"), "personal data"),
        (scenario("s-1", "another-business"), "not in the dataset manifest"),
    ],
)
def test_each_problem_names_the_scenario_and_its_rule(
    tmp_path: Path, data: dict[str, Any], fragment: str
) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(dataset_dir, [data])
    found = problems_of(dataset_dir)
    assert any("s-1" in p and fragment in p for p in found), found
    assert all("98765" not in p for p in found)


@pytest.mark.parametrize("scenario_id", ["example", "example-1"])
def test_an_example_id_is_a_placeholder_even_with_an_unmarked_message(
    tmp_path: Path, scenario_id: str
) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(dataset_dir, [scenario(scenario_id, message=letters(scenario_id))])
    found = problems_of(dataset_dir, allow_placeholders=False)
    assert any(scenario_id in p and "placeholder" in p for p in found), found


def test_a_tool_the_business_does_not_enable_is_refused(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    tools_path = dataset_dir.parents[3] / "data" / "sandbox" / PAWS / "tools.json"
    tools = read_json(tools_path)
    tools["enabled_tools"] = [t for t in tools["enabled_tools"] if t != "book"]
    write_json(tools_path, tools)
    write_scenarios(dataset_dir, [book("s-1")])
    assert any("does not enable" in p for p in problems_of(dataset_dir))


def test_repeated_ids_and_unpaired_near_duplicates_are_problems(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    same = "TEST-ONLY could you show me the free slots for next week please"
    write_scenarios(
        dataset_dir,
        [
            scenario("s-1", message=same),
            scenario("s-1", message="TEST-ONLY something entirely different here"),
            scenario("s-2", message=same + "!"),
            scenario("s-3", message=same, split_group="pair"),
            scenario("s-4", message=same + "?", split_group="pair"),
            scenario("s-5", message=same + ".", split_group="one"),
            scenario("s-6", message=same + "..", split_group="two"),
        ],
    )
    found = problems_of(dataset_dir)
    assert "scenario id s-1 is used more than once" in found
    assert any("s-2" in p and "near-duplicates" in p for p in found)
    assert not any("s-3 and s-4" in p for p in found)
    # Near-duplicates in two different groups could straddle the split: a problem too.
    assert any("s-5 and s-6" in p and "near-duplicates" in p for p in found), found


def test_minimums_follow_the_manifest_and_each_business_languages(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(
        dataset_dir,
        [
            book("s-1", confirm=True),
            scenario("s-2", SUNRISE, language="hi", read_only=True, seed="steering"),
            scenario("s-3", SUNRISE, kind="handoff", emergency=True),
            scenario("s-4", PAWS, kind="handoff", emergency=True),
        ],
    )
    found = {m.what: (m.have, m.need) for m in minimums(read(dataset_dir))}
    assert found["scenarios"] == (4, 40)
    assert found[f"business {PAWS}: scenarios"] == (2, 15)
    assert found[f"business {SUNRISE}: scenarios"] == (2, 15)
    assert found[f"business {SUNRISE}: scenarios in hi"] == (1, 2)
    assert found[f"business {SUNRISE}: scenarios in hi-code-mixed"] == (0, 2)
    assert f"business {PAWS}: scenarios in hi" not in found
    assert found["confirming scenarios"] == (1, 6)
    assert found["scenarios over steering seed text"] == (1, 6)
    assert found["read-only scenarios"] == (1, 6)
    assert found["emergency plus booking scenarios"] == (2, 4)  # read-only is 1: counted apart
    # Decided 2026-11-11: each business's emergencies (2), and, for a business that allows hi,
    # its emergencies in Hindi or Hinglish (1). s-3 is Sunrise's, in English.
    assert found[f"business {SUNRISE}: emergency scenarios"] == (1, 2)
    assert found[f"business {PAWS}: emergency scenarios"] == (1, 2)
    assert found[f"business {SUNRISE}: emergency scenarios in {HINDI_GROUP}"] == (0, 1)
    assert f"business {PAWS}: emergency scenarios in {HINDI_GROUP}" not in found


def test_a_hindi_or_hinglish_emergency_counts_for_its_business_only(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(
        dataset_dir,
        [
            scenario("s-hi", SUNRISE, language="hi", kind="handoff", emergency=True),
            scenario(
                "s-mix", SUNRISE, language="hi", code_mixed=True, kind="handoff", emergency=True
            ),
            scenario("s-calm", SUNRISE, language="hi"),  # Hindi, not an emergency
        ],
    )
    found = {m.what: (m.have, m.need) for m in minimums(read(dataset_dir))}
    # By hand: s-hi and s-mix are Sunrise emergencies in hi (one Hinglish) → 2; Paws has none.
    assert found[f"business {SUNRISE}: emergency scenarios"] == (2, 2)
    assert found[f"business {SUNRISE}: emergency scenarios in {HINDI_GROUP}"] == (2, 1)
    assert found[f"business {PAWS}: emergency scenarios"] == (0, 2)


def test_freeze_is_refused_below_the_minimums_and_writes_nothing(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)
    write_scenarios(dataset_dir, [scenario("s-1")])
    before = (dataset_dir / SCENARIOS_FILE).read_bytes()
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    assert any("scenarios: 1 of at least 40" in p for p in refused.value.problems)
    assert (dataset_dir / SCENARIOS_FILE).read_bytes() == before


def test_freeze_is_refused_below_the_emergency_minimums(tmp_path: Path) -> None:
    # Everything else at its lowest; twelve() has 2 English emergencies per business and no Hindi
    # one, so only the Hindi minimum is short. Dropping one Paws emergency makes Paws short too.
    need = {**LOW_MINIMUMS, "min_emergency_per_business": 2, "min_emergency_hindi_per_business": 1}
    dataset_dir = make_repo(tmp_path, requirements=need)
    write_scenarios(dataset_dir, [s for s in twelve() if s["id"] != "paw-sos-2"])
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    assert sorted(refused.value.problems) == [
        f"business {PAWS}: emergency scenarios: 1 of at least 2",
        f"business {SUNRISE}: emergency scenarios in {HINDI_GROUP}: 0 of at least 1",
    ]
    assert refused.value.unbalanced is False
    assert not (dataset_dir / FREEZE_FILE).exists()


def twelve() -> list[dict[str, Any]]:
    """Twelve TEST-ONLY scenarios over both businesses and every flag."""
    out: list[dict[str, Any]] = []
    for business in (PAWS, SUNRISE):
        short = business[:3]
        out += [
            book(f"{short}-book-1", confirm=True),
            book(f"{short}-book-2", slot="S-10", confirm=True),
            scenario(f"{short}-read-1", business, read_only=True),
            scenario(f"{short}-read-2", business, read_only=True, seed="steering"),
            scenario(f"{short}-sos-1", business, kind="handoff", emergency=True),
            scenario(f"{short}-sos-2", business, kind="handoff", emergency=True),
        ]
    for item in out[6:8]:
        item["business_id"] = SUNRISE
    return out


def test_freeze_splits_half_and_half_and_pins_scenarios_and_sandbox(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    dataset = read(dataset_dir)
    splits = [s.split for s in dataset.scenarios]
    assert splits.count("test") == 6 and splits.count("dev") == 6
    assert dataset.manifest.stage == "frozen"
    assert dataset.freeze is not None
    assert set(dataset.freeze.sandbox) == {SUNRISE, PAWS}
    assert check_dataset(dataset, allow_placeholders=True).ok
    for flag in ("confirm", "read_only", "emergency"):
        flagged = {s.split for s in dataset.scenarios if getattr(s, flag)}
        assert flagged == {"dev", "test"}, flag


def test_a_frozen_version_fails_when_a_scenario_or_a_seed_changes(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    lines = (dataset_dir / SCENARIOS_FILE).read_text("utf-8").splitlines()
    first = json.loads(lines[0])
    first["message"] += " edited"
    (dataset_dir / SCENARIOS_FILE).write_text(
        "\n".join([json.dumps(first), *lines[1:]]) + "\n", "utf-8"
    )
    assert any("changed after the freeze" in p for p in problems_of(dataset_dir))

    other = frozen_repo(tmp_path / "second", twelve())
    seed_path = other.parents[3] / "data" / "sandbox" / PAWS / "seed.json"
    seed = read_json(seed_path)
    seed["slots"][0]["label"] = seed["slots"][0]["label"] + " (changed)"
    write_json(seed_path, seed)
    assert any("seed.json changed after the freeze" in p for p in problems_of(other))


FrozenEdit = Callable[[Path], None]


def _edit_lines(dataset_dir: Path, change: Callable[[list[dict[str, Any]]], None]) -> None:
    path = dataset_dir / SCENARIOS_FILE
    lines = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    change(lines)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), "utf-8")


def _flip_split(dataset_dir: Path) -> None:
    def change(lines: list[dict[str, Any]]) -> None:
        lines[0]["split"] = "dev" if lines[0]["split"] == "test" else "test"

    _edit_lines(dataset_dir, change)


def _drop_first(dataset_dir: Path) -> None:
    def change(lines: list[dict[str, Any]]) -> None:
        del lines[0]

    _edit_lines(dataset_dir, change)


def _add_one(dataset_dir: Path) -> None:
    _edit_lines(dataset_dir, lambda lines: lines.append(scenario("new-1", split="dev")))


def _change_seed(dataset_dir: Path) -> None:
    manifest = read_json(dataset_dir / MANIFEST_FILE)
    manifest["split_rule"]["seed"] = "tool_calls_2"
    write_json(dataset_dir / MANIFEST_FILE, manifest)


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        (_flip_split, "moved from"),
        (_drop_first, "was frozen and is gone"),
        (_add_one, "scenario new-1 was added after the freeze"),
        (_change_seed, "the split rule changed after the freeze"),
    ],
    ids=["moved", "gone", "added", "seed"],
)
def test_every_frozen_pin_fails_on_its_own_change(
    tmp_path: Path, edit: FrozenEdit, fragment: str
) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    assert problems_of(dataset_dir) == []
    edit(dataset_dir)
    found = problems_of(dataset_dir)
    assert any(fragment in p for p in found), found


def test_a_frozen_version_is_held_to_its_minimums(tmp_path: Path) -> None:
    # freeze.json does not pin `requirements`: raising one after the freeze shows up at once
    # (lowering one is caught by the committed version's own test, which pins D8's values).
    dataset_dir = frozen_repo(tmp_path, twelve())
    manifest = read_json(dataset_dir / MANIFEST_FILE)
    manifest["requirements"]["min_scenarios"] = 40
    write_json(dataset_dir / MANIFEST_FILE, manifest)
    assert "scenarios: 12 of at least 40" in problems_of(dataset_dir)


def test_a_frozen_version_is_held_to_the_emergency_minimums(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    manifest = read_json(dataset_dir / MANIFEST_FILE)
    manifest["requirements"] |= {
        "min_emergency_per_business": 3,
        "min_emergency_hindi_per_business": 1,
    }
    write_json(dataset_dir / MANIFEST_FILE, manifest)
    found = problems_of(dataset_dir)
    # By hand: twelve() has 2 emergencies per business, all English.
    assert f"business {PAWS}: emergency scenarios: 2 of at least 3" in found
    assert f"business {SUNRISE}: emergency scenarios: 2 of at least 3" in found
    assert f"business {SUNRISE}: emergency scenarios in {HINDI_GROUP}: 0 of at least 1" in found


def test_a_freeze_json_in_a_collecting_version_is_a_problem(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    manifest = read_json(dataset_dir / MANIFEST_FILE)
    manifest["stage"] = "collecting"
    write_json(dataset_dir / MANIFEST_FILE, manifest)
    found = problems_of(dataset_dir)
    assert f"{FREEZE_FILE} exists, but the manifest's stage is collecting" in found


# ── The split's balance ──────────────────────────────────────────────────────────────────────


def splits_of(scenarios: Sequence[Scenario], tests: set[str]) -> dict[str, Split]:
    return {s.id: "test" if s.id in tests else "dev" for s in scenarios}


def test_too_few_friends_scenarios_in_test_is_unbalanced() -> None:
    friends = [parsed(scenario(f"f-{i}", author="friend")) for i in range(10)]
    # By hand: 10 friend scenarios need max(1, 10 // 5) = 2 in test; 1 is too few.
    found = balance_problems(friends, splits_of(friends, {"f-0"}), [PAWS])
    assert "author friend: 1 of 10 in test, fewer than 2" in found
    assert "author friend: 1 of 10 in test, fewer than 2" not in balance_problems(
        friends, splits_of(friends, {"f-0", "f-1"}), [PAWS]
    )


def test_a_stratum_all_in_test_is_unbalanced_and_a_single_unit_is_not_judged() -> None:
    sos = [parsed(scenario(f"e-{i}", kind="handoff", emergency=True)) for i in range(2)]
    plain = [parsed(scenario(f"p-{i}")) for i in range(2)]
    every = [*sos, *plain]
    found = balance_problems(every, splits_of(every, {"e-0", "e-1"}), [PAWS])
    assert "stratum emergency: all 2 in test, none in dev" in found
    assert "flag emergency: all 2 in test, none in dev" in found
    alone = [sos[0], *plain]
    found = balance_problems(alone, splits_of(alone, {"e-0", "p-0"}), [PAWS])
    assert not any("emergency" in p for p in found), found  # one unit: nothing to balance


def test_each_business_needs_its_emergencies_in_both_splits() -> None:
    # Overall the emergency stratum and flag are balanced (2 in test, 2 in dev), but each business
    # has all of its own in one split: refused for both (decided 2026-11-11).
    sos = [
        parsed(scenario("p-sos-0", PAWS, kind="handoff", emergency=True)),
        parsed(scenario("p-sos-1", PAWS, kind="handoff", emergency=True)),
        parsed(scenario("s-sos-0", SUNRISE, kind="handoff", emergency=True)),
        parsed(scenario("s-sos-1", SUNRISE, kind="handoff", emergency=True)),
    ]
    found = balance_problems(sos, splits_of(sos, {"p-sos-0", "p-sos-1"}), [SUNRISE, PAWS])
    assert not any(p.startswith(("stratum", "flag")) for p in found), found
    assert f"business {PAWS} emergency: all 2 in test, none in dev" in found
    assert f"business {SUNRISE} emergency: 0 of 2 in test, fewer than 1" in found
    mixed = balance_problems(sos, splits_of(sos, {"p-sos-0", "s-sos-1"}), [SUNRISE, PAWS])
    assert not any("emergency" in p for p in mixed), mixed


def test_hindi_and_hinglish_emergencies_need_both_splits() -> None:
    sos = [
        parsed(scenario("s-hi", SUNRISE, language="hi", kind="handoff", emergency=True)),
        parsed(
            scenario(
                "s-mix", SUNRISE, language="hi", code_mixed=True, kind="handoff", emergency=True
            )
        ),
        parsed(scenario("s-en-0", SUNRISE, kind="handoff", emergency=True)),
        parsed(scenario("s-en-1", SUNRISE, kind="handoff", emergency=True)),
    ]
    # By hand: Sunrise's emergencies are split 2 and 2 (fine), but both Hindi ones are in dev.
    found = balance_problems(sos, splits_of(sos, {"s-en-0", "s-en-1"}), [SUNRISE])
    assert f"business {SUNRISE} emergency in {HINDI_GROUP}: 0 of 2 in test, fewer than 1" in found
    assert f"business {SUNRISE} emergency: 2 of 4 in test" not in " ".join(found)
    one = [sos[0], *sos[2:]]  # a single Hindi emergency: one unit, nothing to balance
    found = balance_problems(one, splits_of(one, {"s-en-0"}), [SUNRISE])
    assert not any(HINDI_GROUP in p and "emergency" in p for p in found), found


def test_hindi_and_hinglish_need_a_share_in_test() -> None:
    hindi = [
        parsed(scenario("h-0", SUNRISE, language="hi")),
        parsed(scenario("h-1", SUNRISE, language="hi")),
        parsed(scenario("h-2", SUNRISE, language="hi", code_mixed=True)),
    ]
    found = balance_problems(hindi, splits_of(hindi, set()), [SUNRISE])
    assert f"language {HINDI_GROUP}: 0 of 3 in test, fewer than 1" in found


def unmarked(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """`items` with hash-letter messages, so a check without placeholders accepts them."""
    return [{**item, "message": letters(item["id"])} for item in items]


def test_an_unbalanced_split_is_refused_writes_nothing_and_shows_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_dir = make_repo(tmp_path, requirements=LOW_MINIMUMS)
    write_scenarios(dataset_dir, unmarked(twelve()))
    before = {n: (dataset_dir / n).read_bytes() for n in (MANIFEST_FILE, SCENARIOS_FILE)}
    monkeypatch.setattr(
        checks, "assign_splits", lambda scenarios, rule: {s.id: "test" for s in scenarios}
    )
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read(dataset_dir), frozen_on=TODAY)
    assert refused.value.unbalanced is True
    assert any("none in dev" in p for p in refused.value.problems)

    out = io.StringIO()
    assert main(["--dataset", str(dataset_dir), "freeze"], out) == 1
    text = out.getvalue()
    assert f"PROBLEM  {WAY_OUT}" in text and "freeze refused: nothing was written" in text
    assert not (dataset_dir / FREEZE_FILE).exists()
    assert {n: (dataset_dir / n).read_bytes() for n in before} == before


GOLDEN: Final[dict[str, str]] = {
    "pair-en": "dev",
    "pair-hi": "dev",
    "paw-book-1": "test",
    "paw-book-2": "test",
    "paw-read-1": "dev",
    "paw-read-2": "test",
    "paw-sos-1": "dev",
    "paw-sos-2": "test",
    "sun-book-1": "dev",
    "sun-book-2": "test",
    "sun-read-1": "test",
    "sun-read-2": "dev",
    "sun-sos-1": "dev",
    "sun-sos-2": "test",
}
"""The rule's output for `twelve()` plus one split_group pair, computed once from the rule as
written on 2026-11-11 (seed `tool_calls`, 50 %). It pins the rule: dropping the seed from the
shuffle, ignoring split_group or changing the offset each changes it. Changing this mapping is a
decision about the split rule, never a test fix."""


def test_the_split_rule_is_pinned_and_a_split_group_shares_a_split(tmp_path: Path) -> None:
    pair = [
        scenario("pair-en", SUNRISE, split_group="pair-1"),
        scenario("pair-hi", SUNRISE, language="hi", split_group="pair-1"),
    ]
    dataset = read(frozen_repo(tmp_path, [*twelve(), *pair]))
    found = {s.id: s.split for s in dataset.scenarios}
    assert found == GOLDEN
    assert found["pair-en"] == found["pair-hi"]


def test_freeze_twice_is_refused(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path, twelve())
    with pytest.raises(FreezeRefused):
        freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    assert LOW_MINIMUMS["min_scenarios"] == 1
