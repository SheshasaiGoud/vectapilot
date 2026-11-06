"""M4 PR 1: the eval split rule (`stratified-systematic-v2` over eval rows), content hashes and
the freeze manifest."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from tests.intenteval.helpers import (
    PROFILES,
    complete_eval,
    complete_train,
    eval_row,
    manifest,
    noah_rows,
    train_row,
)
from vectapilot_api.evalset.questions import DatasetError, Split
from vectapilot_api.evalset.split import held_out_count
from vectapilot_api.intenteval.rows import BusinessProfile, EvalRow
from vectapilot_api.intenteval.sample import draw_sample
from vectapilot_api.intenteval.split import (
    SplitUnit,
    assign_splits,
    balance_problems,
    build_freeze,
    content_hash,
    freeze_problems,
    load_freeze,
    repair_intents,
    split_units,
)
from vectapilot_api.intents.policy import RoutePolicy, policy_for
from vectapilot_api.intents.taxonomy import Intent

RULE: Final = manifest().split_rule
API_DIR: Final = Path(__file__).parents[2]


def rows_of(layout: tuple[tuple[str, int], ...]) -> list[EvalRow]:
    return [
        eval_row(id=f"example-{intent.replace('_', '-')}-{k}", intent=intent)
        for intent, count in layout
        for k in range(count)
    ]


GOLDEN_LAYOUT: Final = (("emergency", 2), ("complaint", 2), ("question", 4))
GOLDEN: Final = {
    "example-emergency-1": "test",
    "example-emergency-0": "dev",
    "example-complaint-0": "test",
    "example-complaint-1": "dev",
    "example-question-1": "test",
    "example-question-3": "dev",
    "example-question-2": "test",
    "example-question-0": "dev",
}
"""One author, language and business, so the line is the intents in decision order, each run
shuffled by `sha256("intents:<id>")`. U = 8, T = round(8 * 50 / 100) = 4, offset
`sha256("intents:offset") mod 8` = 7: positions 0, 2, 4, 6 are test (`(i*4 + 7) // 8` steps up
after 0, 2, 4 and 6). Every intent is in both splits, so the repair changes nothing."""


def test_the_golden_case_gives_exactly_this_split() -> None:
    rows = rows_of(GOLDEN_LAYOUT)
    assert [u.lead.id for u in split_units(rows, RULE.seed)] == list(GOLDEN)
    assert assign_splits(rows, RULE) == GOLDEN


def test_half_of_the_units_go_to_test() -> None:
    rows = rows_of((("emergency", 5), ("question", 6), ("out_of_scope", 4)))
    splits = assign_splits(rows, RULE)
    assert list(splits.values()).count("test") == held_out_count(15, 50) == 8


def test_the_split_does_not_depend_on_file_order() -> None:
    rows = rows_of((("emergency", 3), ("complaint", 4), ("question", 5)))
    shuffled = sorted(rows, key=lambda r: hashlib.sha256(r.id.encode()).hexdigest())
    assert shuffled != rows
    assert assign_splits(shuffled, RULE) == assign_splits(rows, RULE)


def test_the_lead_orders_by_author_language_business_then_intent() -> None:
    rows = [
        eval_row(id="example-a", author="shesha", intent="emergency"),
        eval_row(id="example-b", author="friend", intent="question"),
        eval_row(id="example-c", author="friend", language="hi", intent="emergency"),
        eval_row(id="example-d", author="friend", business_id="paws-and-co", intent="emergency"),
        eval_row(id="example-e", author="friend", intent="emergency"),
    ]
    order = [u.lead.id for u in split_units(rows, RULE.seed)]
    # friend < shesha; en < hi; paws-and-co < sunrise-clinic; emergency before question.
    assert order == ["example-d", "example-e", "example-b", "example-c", "example-a"]


def test_rows_in_one_split_group_share_a_split_and_the_first_in_rule_order_leads() -> None:
    rows = [
        eval_row(id="example-hi", language="hi", split_group="example-pair", intent="emergency"),
        eval_row(id="example-en", split_group="example-pair", intent="emergency"),
        *rows_of((("emergency", 3), ("question", 3))),
    ]
    units = split_units(rows, RULE.seed)
    pair = next(u for u in units if len(u.members) == 2)
    assert [r.id for r in pair.members] == ["example-en", "example-hi"]
    splits = assign_splits(rows, RULE)
    assert splits["example-en"] == splits["example-hi"]


def unit(row_id: str, intent: str) -> SplitUnit:
    return SplitUnit((eval_row(id=row_id, intent=intent),))


def test_an_intent_missing_from_test_swaps_with_the_nearest_spare_test_unit() -> None:
    line = [unit("example-e0", "emergency"), unit("example-e1", "emergency")]
    line += [unit("example-q0", "question"), unit("example-q1", "question")]
    test = [False, False, True, True]
    repair_intents(line, test)
    assert test == [False, True, False, True]


def test_without_a_partner_a_unit_moves_alone() -> None:
    line = [unit("example-e0", "emergency"), unit("example-e1", "emergency")]
    line.append(unit("example-q0", "question"))  # its only test row may not leave
    test = [False, False, True]
    repair_intents(line, test)
    assert test == [True, False, True]


def test_the_repair_serves_intents_in_decision_order_safety_first() -> None:
    # emergency and complaint both miss test; one spare test unit (a question) can leave. In
    # decision order emergency takes the swap and complaint moves alone; name order (complaint
    # first) would do the opposite.
    line = [
        unit("example-e0", "emergency"),
        unit("example-e1", "emergency"),
        unit("example-c0", "complaint"),
        unit("example-c1", "complaint"),
        unit("example-q0", "question"),
        unit("example-q1", "question"),
        unit("example-q2", "question"),
    ]
    test = [False, False, False, False, True, True, False]
    repair_intents(line, test)
    assert test == [False, True, True, False, False, True, False]


HASH_SEED_SCRIPT: Final = """
import json
from tests.intenteval.helpers import manifest
from tests.intenteval.test_split import rows_of
from vectapilot_api.intenteval.split import assign_splits
rows = rows_of((("emergency", 3), ("complaint", 2), ("question", 1), ("out_of_scope", 4)))
order = list({"emergency", "out_of_scope"})  # the probe's set: shows the seed took effect
print(json.dumps({"set_order": order, "splits": assign_splits(rows, manifest().split_rule)}))
"""


def _run_with_hash_seed(seed: int, code: str) -> str:
    done = subprocess.run(  # noqa: S603 (this interpreter, fixed code, no shell)
        [sys.executable, "-c", code],
        cwd=API_DIR,
        env={**os.environ, "PYTHONHASHSEED": str(seed)},
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout


def test_the_split_is_the_same_under_two_hash_seeds() -> None:
    # As support_qa's golden test: find two seeds under which a set of these intents iterates in
    # opposite orders; a rule that took any order from a set would split differently under them.
    probe = "print(list({'emergency', 'out_of_scope'}))"
    seeds: dict[str, int] = {}
    for seed in range(64):
        seeds.setdefault(_run_with_hash_seed(seed, probe), seed)
        if len(seeds) == 2:
            break
    assert len(seeds) == 2, "no two hash seeds in 0-63 iterate the set differently"
    results = [json.loads(_run_with_hash_seed(s, HASH_SEED_SCRIPT)) for s in seeds.values()]
    assert {tuple(r["set_order"]) for r in results} == {
        ("emergency", "out_of_scope"),
        ("out_of_scope", "emergency"),
    }
    assert results[0]["splits"] == results[1]["splits"]


# ── Balance, checked before the freeze ────────────────────────────────────────────────────────

POLICIES: Final[dict[str, RoutePolicy]] = {
    b: policy_for(p.business_type) for b, p in PROFILES.items()
}


def balance(
    rows: list[EvalRow], test_ids: set[str], policies: dict[str, RoutePolicy] = POLICIES
) -> list[str]:
    splits: dict[str, Split] = {r.id: "test" if r.id in test_ids else "dev" for r in rows}
    return balance_problems(rows, splits, policies)


def test_the_repair_alone_can_leave_an_intent_in_one_split_and_balance_names_it() -> None:
    rows = noah_rows()
    splits = assign_splits(rows, RULE)
    assert {splits["r1"], splits["r2"]} == {"dev"}
    assert balance_problems(rows, splits, POLICIES) == [
        "business sunrise-clinic: 0 of 3 in test, fewer than 1",
        "intent complaint: 0 of 2 in test, fewer than 1",
    ]


def test_an_intent_in_two_or_more_units_needs_both_splits() -> None:
    rows = [
        eval_row(id="example-q0", intent="question"),
        eval_row(id="example-q1", intent="question", business_id="paws-and-co"),
        eval_row(id="example-o0", intent="out_of_scope"),
        eval_row(id="example-o1", intent="out_of_scope", business_id="paws-and-co"),
    ]
    assert balance(rows, {"example-q0", "example-q1"}) == [
        "intent question: all 2 in test, none in dev",
        "intent out_of_scope: 0 of 2 in test, fewer than 1",
    ]
    assert balance(rows, {"example-q0", "example-o1"}) == []
    # One unit cannot be split, so an intent whose rows share a split group is not judged.
    grouped = [r.model_copy(update={"split_group": "example-g"}) for r in rows[:2]] + rows[2:]
    assert balance(grouped, {"example-q0", "example-q1"}) == [
        "intent out_of_scope: 0 of 2 in test, fewer than 1"
    ]


def test_each_business_needs_each_safety_intent_of_its_template_in_both_splits() -> None:
    # emergency is in both splits overall, but each business has it in one split only: §6
    # judges safety recall per business, so neither business could be measured on test.
    rows = [
        eval_row(id="example-se0", intent="emergency"),
        eval_row(id="example-se1", intent="emergency"),
        eval_row(id="example-sq", intent="question"),
        eval_row(id="example-pe0", intent="emergency", business_id="paws-and-co"),
        eval_row(id="example-pe1", intent="emergency", business_id="paws-and-co"),
        eval_row(id="example-pq", intent="question", business_id="paws-and-co"),
    ]
    assert balance(rows, {"example-se0", "example-se1", "example-pq"}) == [
        "business sunrise-clinic, safety intent emergency: all 2 in test, none in dev",
        "business paws-and-co, safety intent emergency: 0 of 2 in test, fewer than 1",
    ]


def test_the_safety_intents_judged_per_business_follow_its_template() -> None:
    rows = [
        eval_row(id="example-pj0", intent="professional_judgement", business_id="paws-and-co"),
        eval_row(id="example-pj1", intent="professional_judgement", business_id="paws-and-co"),
        eval_row(id="example-pq", intent="question", business_id="paws-and-co"),
        eval_row(id="example-sj0", intent="professional_judgement"),
        eval_row(id="example-sj1", intent="professional_judgement"),
        eval_row(id="example-sq", intent="question"),
    ]
    test_ids = {"example-pj0", "example-pj1", "example-sj0", "example-sq"}
    assert balance(rows, test_ids) == [
        "business paws-and-co, safety intent professional_judgement: all 2 in test, none in dev"
    ]
    # A default-template business has emergency as its only safety intent.
    retail = {**POLICIES, "paws-and-co": policy_for("retail")}
    assert balance(rows, test_ids, retail) == []


def test_friends_hindi_and_each_business_need_a_fifth_in_test_and_some_in_dev() -> None:
    sunrise = [eval_row(id=f"example-q{k}", intent="question") for k in range(10)]
    assert balance(sunrise, {"example-q0"}) == [
        "business sunrise-clinic: 1 of 10 in test, fewer than 2"
    ]
    assert balance(sunrise, {"example-q0", "example-q1"}) == []
    # Businesses are judged in the manifest's order (sunrise-clinic first), not by name.
    both_businesses = [
        eval_row(id="example-s0", intent="question"),
        eval_row(id="example-s1", intent="question"),
        eval_row(id="example-p0", intent="question", business_id="paws-and-co"),
        eval_row(id="example-p1", intent="question", business_id="paws-and-co"),
    ]
    assert balance(both_businesses, {"example-s0", "example-s1"}) == [
        "business sunrise-clinic: all 2 in test, none in dev",
        "business paws-and-co: 0 of 2 in test, fewer than 1",
    ]

    friends = [
        eval_row(id=f"example-{who}{k}", author=who, intent="question")
        for who in ("friend", "shesha")
        for k in range(5)
    ]
    assert balance(friends, {f"example-shesha{k}" for k in range(5)}) == [
        "author friend: 0 of 5 in test, fewer than 1"
    ]
    # A group of one row is not judged.
    lone = [*friends[5:], friends[0]]
    assert balance(lone, {"example-friend0", "example-shesha0", "example-shesha1"}) == []

    hindi = [
        eval_row(id="example-h0", language="hi", intent="question"),
        eval_row(id="example-h1", language="hi", code_mixed=True, intent="question"),
        eval_row(id="example-e0", intent="question"),
        eval_row(id="example-e1", intent="question"),
    ]
    assert balance(hindi, {"example-h0", "example-h1"}) == [
        "language hi + hi-code-mixed: all 2 in test, none in dev"
    ]
    assert balance(hindi, {"example-h0", "example-e0"}) == []


# ── Content hashes ────────────────────────────────────────────────────────────────────────────


def test_the_content_hash_is_sha256_of_canonical_json_without_split() -> None:
    row = eval_row(split="test")
    payload = row.model_dump(mode="json", exclude={"split"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert content_hash(row) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert content_hash(row) == content_hash(eval_row(split="dev"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("business_id", "paws-and-co"),
        ("language", "hi"),
        ("text", "TEST-ONLY another"),
        ("intent", "complaint"),
        ("author", "friend"),
        ("split_group", "example-g"),
        ("steering", True),
        ("created", "2026-11-06"),
    ],
)
def test_every_eval_field_but_split_is_hashed(field: str, value: object) -> None:
    assert content_hash(eval_row(**{field: value})) != content_hash(eval_row())


def test_verifying_a_training_row_changes_its_hash() -> None:
    assert content_hash(train_row(verified=True)) != content_hash(train_row(verified=False))


# ── The freeze manifest ───────────────────────────────────────────────────────────────────────

FROZEN_ON: Final = date(2026, 11, 20)
SAMPLE: Final = draw_sample(complete_train(), manifest().verification)


def frozen_rows() -> tuple[list[EvalRow], dict[str, Split]]:
    rows = complete_eval()
    splits = assign_splits(rows, RULE)
    assigned = [r.model_copy(update={"split": splits[r.id]}) for r in rows]
    return assigned, splits


def test_an_untouched_freeze_has_no_problems_and_survives_a_round_trip() -> None:
    rows, _ = frozen_rows()
    train = complete_train()
    freeze = build_freeze(manifest(), train, rows, PROFILES, SAMPLE, FROZEN_ON)
    assert load_freeze(freeze.model_dump_json()) == freeze
    assert freeze_problems(manifest(), freeze, train, rows, PROFILES, SAMPLE) == []
    assert set(freeze.train_rows) == {r.id for r in train}
    assert {i: f.split for i, f in freeze.eval_rows.items()} == {r.id: r.split for r in rows}
    assert freeze.verification_sample == SAMPLE


def test_every_change_after_the_freeze_is_named_with_the_way_out() -> None:
    rows, _ = frozen_rows()
    train = complete_train()
    freeze = build_freeze(manifest(), train, rows, PROFILES, SAMPLE, FROZEN_ON)
    changed_eval = [
        rows[0].model_copy(update={"intent": Intent.QUESTION}),
        rows[1].model_copy(update={"split": "dev" if rows[1].split == "test" else "test"}),
        *rows[3:],
        eval_row(id="example-new", split="dev"),
    ]
    changed_train = [train[0].model_copy(update={"verified": False}), *train[2:]]
    businesses = {
        **PROFILES,
        "paws-and-co": BusinessProfile(business_type="retail", languages=("en",)),
    }
    other = manifest(
        split_rule={"rule": "stratified-systematic-v2", "seed": "other", "test_percent": 50},
        near_duplicate={"method": "char-3gram-jaccard", "threshold_percent": 80},
    )
    swapped = (*SAMPLE[1:], "example-other")  # another list of the same size
    problems = freeze_problems(other, freeze, changed_train, changed_eval, businesses, swapped)
    bump = "v0.1 is frozen: make a new version instead (README, 'Adding a version')"
    assert problems == [
        f"the split rule changed after the freeze; {bump}",
        f"the near-duplicate rule changed after the freeze; {bump}",
        f"business paws-and-co's type or languages changed after the freeze; {bump}",
        f"the verification sample changed after the freeze; {bump}",
        f"train row {train[1].id} was frozen and is gone; {bump}",
        f"train row {train[0].id} changed after the freeze; {bump}",
        f"eval row {rows[2].id} was frozen and is gone; {bump}",
        f"eval row {rows[0].id} changed after the freeze; {bump}",
        f"eval row example-new was added after the freeze; {bump}",
        f"eval row {rows[1].id} moved from {rows[1].split} to {changed_eval[1].split}; {bump}",
    ]


def test_a_freeze_of_another_dataset_or_version_is_refused() -> None:
    rows, _ = frozen_rows()
    freeze = build_freeze(manifest(), complete_train(), rows, PROFILES, SAMPLE, FROZEN_ON)
    problems = freeze_problems(
        manifest(version="v0.2"), freeze, complete_train(), rows, PROFILES, SAMPLE
    )
    assert problems == ["freeze.json belongs to another dataset or version"]


@pytest.mark.parametrize("sample", [None, (), SAMPLE[:-1], tuple(reversed(SAMPLE))])
def test_the_verification_sample_is_pinned_by_the_freeze(sample: tuple[str, ...] | None) -> None:
    # The list the note's `x of n` is measured on: removed, emptied, shortened or reordered.
    rows, _ = frozen_rows()
    freeze = build_freeze(manifest(), complete_train(), rows, PROFILES, SAMPLE, FROZEN_ON)
    assert freeze_problems(manifest(), freeze, complete_train(), rows, PROFILES, sample) == [
        "the verification sample changed after the freeze; v0.1 is frozen: make a new version "
        "instead (README, 'Adding a version')"
    ]


def test_a_freeze_needs_every_eval_split_assigned_and_a_sample() -> None:
    with pytest.raises(ValueError, match="eval row example-e1 has no split"):
        build_freeze(manifest(), complete_train(), [eval_row()], PROFILES, SAMPLE, FROZEN_ON)
    rows, _ = frozen_rows()
    with pytest.raises(ValidationError, match="verification_sample"):
        build_freeze(manifest(), complete_train(), rows, PROFILES, (), FROZEN_ON)


def test_a_bad_freeze_file_names_its_fields() -> None:
    with pytest.raises(DatasetError, match=r"freeze.json is not a valid intents freeze: "):
        load_freeze(json.dumps({"dataset": "intents"}))
