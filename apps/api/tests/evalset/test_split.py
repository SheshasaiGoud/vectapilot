"""M2 PR 7: the test-split rule, content hashes and the freeze manifest (hand-computed cases).

Every row is a made-up placeholder (helpers.py). The "realistic" cases carry attributes only
(author, language, business, category) and `TEST-ONLY` text: they test the rule's balance, never a
question.
"""

import hashlib
import json
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from tests.evalset.helpers import (
    PAWS_CANCEL,
    SUNRISE_FEE,
    SUNRISE_HOURS,
    golden_layout,
    manifest,
    question,
    real_corpus,
)
from vectapilot_api.evalset.corpus import CorpusPins, manifest_sha256
from vectapilot_api.evalset.questions import (
    REQUIRED_CATEGORIES,
    DatasetError,
    EvalQuestion,
    SplitRule,
)
from vectapilot_api.evalset.split import (
    FreezeManifest,
    SplitUnit,
    assign_splits,
    balance_groups,
    balance_problems,
    build_freeze,
    carried_problems,
    content_hash,
    freeze_problems,
    held_out_count,
    kept_split,
    load_freeze,
    pins_from,
    repair_categories,
    split_offset,
    split_units,
    systematic_positions,
)

RULE: Final = SplitRule(rule="stratified-systematic-v2", seed="support_qa", test_percent=30)
WAY_OUT: Final = "v0.1 is frozen: make a new version instead (README, 'Adding a version')"


@pytest.mark.parametrize(
    ("n", "percent", "expected"),
    [
        (0, 30, 0),
        (1, 30, 0),  # one question: dev only
        (2, 30, 1),  # 0.6 rounds to 1
        (3, 30, 1),  # 0.9 → 1
        (4, 30, 1),  # 1.2 → 1
        (5, 30, 2),  # 1.5 rounds half up → 2
        (10, 30, 3),
        (40, 30, 12),
        (42, 30, 13),  # 12.6 → 13
        (2, 99, 1),  # never all of them
        (40, 1, 1),  # 0.4 → 0, but at least 1
    ],
)
def test_how_many_questions_go_to_test(n: int, percent: int, expected: int) -> None:
    assert held_out_count(n, percent) == expected


@pytest.mark.parametrize(
    ("n", "tests", "offset", "expected"),
    [
        # (i·3 + 0) // 10 steps up after i = 3, 6, 9.
        (10, 3, 0, (3, 6, 9)),
        # (i·3 + 8) // 10: 8 → 11 at i = 0, 17 → 20 at i = 3, 29 → 32 at i = 7. Position 0 is test.
        (10, 3, 8, (0, 3, 7)),
        (4, 1, 0, (3,)),
        (4, 1, 3, (0,)),
        (5, 0, 2, ()),
        (0, 0, 0, ()),
    ],
)
def test_systematic_positions_are_evenly_spaced_from_the_offset(
    n: int, tests: int, offset: int, expected: tuple[int, ...]
) -> None:
    assert systematic_positions(n, tests, offset) == expected


def test_the_offset_is_the_seeded_hash_modulo_n() -> None:
    digest = int(hashlib.sha256(b"support_qa:offset").hexdigest(), 16)
    assert [split_offset("support_qa", n) for n in (1, 7, 40, 42)] == [
        digest % n for n in (1, 7, 40, 42)
    ]
    assert split_offset("support_qa", 0) == 0


def test_identical_rows_are_ordered_by_the_seeded_hash_of_the_id() -> None:
    rows = [question(id=f"example-{i}", question=f"TEST-ONLY {i}") for i in range(4)]
    order = sorted(
        (r.id for r in rows),
        key=lambda i: hashlib.sha256(f"support_qa:{i}".encode()).hexdigest(),
    )
    # n = 4, 1 to test: (i + 1 + r) // 4 > (i + r) // 4 exactly at i = 3 - r.
    r = split_offset("support_qa", 4)
    splits = assign_splits(rows, RULE)
    assert [i for i, s in splits.items() if s == "test"] == [order[3 - r]]
    other_seed = assign_splits(rows, RULE.model_copy(update={"seed": "another"}))
    assert sum(s == "test" for s in other_seed.values()) == 1


def test_the_split_does_not_depend_on_file_order() -> None:
    rows = [
        question(
            id=f"example-{category}-{i}",
            question=f"TEST-ONLY {category} {i}",
            category=category,
            expected_outcome="dont_know",
            expected_evidence=[],
        )
        for category in ("policy", "adversarial")
        for i in range(10)
    ] + [
        question(
            id="example-lonely",
            question="TEST-ONLY lonely",
            category="out_of_scope",
            expected_outcome="dont_know",
            expected_evidence=[],
        )
    ]
    splits = assign_splits(rows, RULE)
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311 (a fixed test order, not security)
    assert assign_splits(shuffled, RULE) == splits
    assert sum(s == "test" for s in splits.values()) == held_out_count(21, 30) == 6
    # Two runs of 10 in a line of 21 with 6 test: each gets 60 / 21 = 2.86, so 2 or 3.
    for category in ("policy", "adversarial"):
        assert sum(splits[f"example-{category}-{i}"] == "test" for i in range(10)) in (2, 3)


def _attribute_row(
    i: int, category: str, author: str, language: str, mixed: bool, business: str
) -> EvalQuestion:
    """A TEST-ONLY row with only the attributes the rule reads."""
    hours = [[SUNRISE_HOURS] if business == "sunrise-clinic" else [PAWS_CANCEL]]
    outcome, evidence = {
        "safety": ("handoff", []),
        "handoff_request": ("handoff", []),
        "ambiguous": ("answered", hours),
        "multi_hop": ("answered", [[SUNRISE_HOURS], [SUNRISE_FEE]]),
    }.get(category, ("dont_know", []))
    return question(
        id=f"example-{i:02d}",
        question=f"TEST-ONLY {i}",
        category=category,
        author_type=author,
        saw_documents=author == "team",
        language=language,
        code_mixed=mixed,
        business_id=business,
        expected_outcome=outcome,
        expected_evidence=evidence,
    )


def _realistic(friend_categories: tuple[str, ...]) -> list[EvalQuestion]:
    """42 rows sized 8/7/5/4/5/4/5/4 by category, 10 friend_blind rows spread over
    `friend_categories`, 11 Hindi rows (6 of them Hinglish), businesses mixed."""
    categories: list[str] = list(REQUIRED_CATEGORIES)
    sizes = dict(zip(categories, (8, 7, 5, 4, 5, 4, 5, 4), strict=True))
    friends = dict.fromkeys(categories, 0)
    for k in range(10):
        friends[friend_categories[k % len(friend_categories)]] += 1
    rows: list[EvalQuestion] = []
    for category in categories:
        for j in range(sizes[category]):
            i = len(rows)
            rows.append(
                _attribute_row(
                    i,
                    category,
                    "friend_blind" if j < friends[category] else "team",
                    "hi" if i % 4 == 1 else "en",
                    i % 8 == 1,
                    ("sunrise-clinic", "paws-and-co")[(i // 3) % 2],
                )
            )
    return rows


@pytest.mark.parametrize(
    "friend_categories",
    [
        ("faq", "policy", *REQUIRED_CATEGORIES),
        ("faq", "policy", "out_of_scope", "safety", "handoff_request"),  # friends cluster
    ],
)
def test_friends_hindi_and_both_businesses_reach_test_in_a_realistic_layout(
    friend_categories: tuple[str, ...],
) -> None:
    # kenji's review (K1): under v1, friend_blind rows almost never reached test.
    rows = _realistic(friend_categories)
    assert len(rows) == 42
    assert sum(q.author_type == "friend_blind" for q in rows) == 10
    hindi = [q for q in rows if q.language == "hi"]
    assert (len(hindi), sum(q.code_mixed for q in hindi)) == (11, 6)
    splits = assign_splits(rows, RULE)

    def tests(members: list[EvalQuestion]) -> int:
        return sum(splits[q.id] == "test" for q in members)

    assert abs(tests(rows) - 13) <= 1  # round(42 * 30 / 100) = 13
    assert tests([q for q in rows if q.author_type == "friend_blind"]) >= 2
    assert tests(hindi) >= 2
    for business in ("sunrise-clinic", "paws-and-co"):
        assert tests([q for q in rows if q.business_id == business]) >= 4  # 21 rows: 20 % = 4
    for category in {q.category for q in rows}:
        members = [q for q in rows if q.category == category]
        assert 1 <= tests(members) < len(members), category
    assert balance_problems(rows, splits) == []


def test_pinned_ids_keep_their_split_and_only_new_ids_are_placed() -> None:
    rows = _realistic(("faq",))[:8]
    pinned = {rows[0].id: "test", rows[1].id: "test"}
    splits = assign_splits(rows, RULE, pinned)  # type: ignore[arg-type]
    assert splits[rows[0].id] == splits[rows[1].id] == "test"
    rest = [r for r in rows if r.id not in pinned]
    assert {i: splits[i] for i in (r.id for r in rest)} == assign_splits(rest, RULE)


@pytest.mark.parametrize("old_split", ["dev", "test"])
def test_a_question_that_replaces_a_pinned_one_takes_its_split(old_split: str) -> None:
    # kenji's review (K2): a corrected question gets a new id, and must not be re-split.
    corrected = question(id="example-new", question="TEST-ONLY corrected", replaces="example-old")
    splits = assign_splits([corrected], RULE, {"example-old": old_split})  # type: ignore[dict-item]
    assert splits == {"example-new": old_split}


def test_replaces_is_never_the_questions_own_id() -> None:
    with pytest.raises(ValueError, match="cannot replace itself"):
        question(id="example-1", replaces="example-1")


# ── The golden case: the exact output, and the same under any hash seed ───────────────────────

GOLDEN: Final = {
    "example-adversarial-0": "dev",
    "example-adversarial-1": "test",
    "example-faq-0": "dev",
    "example-faq-1": "test",
    "example-out-of-scope-0": "dev",
    "example-out-of-scope-1": "test",
    "example-policy-0": "dev",
    "example-policy-1": "dev",
    "example-policy-2": "dev",
    "example-policy-3": "test",
}
"""Hand-derived (mutation audit M2b/M2f). The line, by sha256("support_qa:<id>") inside each run:
adversarial-0, adversarial-1, faq-1, faq-0, out-of-scope-1, out-of-scope-0, policy-2, policy-0,
policy-1, policy-3. T = round(10 * 0.3) = 3, offset 1: positions 2, 6, 9 (faq-1, policy-2,
policy-3). Repair in name order: adversarial has no test row; of the test rows only policy's
(2 in test) may leave, and the nearest pair is (adversarial-1, policy-2), 5 apart: swapped. faq
is in both splits. out_of_scope has no test row, and now no test row may leave (adversarial,
faq and policy have one each), so out-of-scope-1 moves alone: 4 in test. Repairing out_of_scope
first would have given policy-2's place to out-of-scope-0 and moved adversarial-0 alone: the
order of the repairs decides, so it must never come from a set's iteration order."""


def test_the_golden_case_gives_exactly_this_split() -> None:
    rows = golden_layout()
    assert assign_splits(rows, RULE) == GOLDEN
    assert [u.lead.id.removeprefix("example-") for u in split_units(rows, RULE.seed)] == [
        "adversarial-0",
        "adversarial-1",
        "faq-1",
        "faq-0",
        "out-of-scope-1",
        "out-of-scope-0",
        "policy-2",
        "policy-0",
        "policy-1",
        "policy-3",
    ]
    offset = split_offset(RULE.seed, 10)
    assert (offset, systematic_positions(10, held_out_count(10, 30), offset)) == (1, (2, 6, 9))


API_DIR: Final = Path(__file__).parents[2]
GOLDEN_SCRIPT: Final = """
import json
from tests.evalset.helpers import golden_layout
from vectapilot_api.evalset.questions import SplitRule
from vectapilot_api.evalset.split import assign_splits
rows = golden_layout()
rule = SplitRule(rule="stratified-systematic-v2", seed="support_qa", test_percent=30)
order = [c for c in {q.category for q in rows} if c in ("adversarial", "out_of_scope")]
print(json.dumps({"set_order": order, "splits": assign_splits(rows, rule)}))
"""


def _run_with_hash_seed(seed: int, code: str) -> str:
    """`code` in a fresh interpreter (this one's) with PYTHONHASHSEED fixed; its stdout."""
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


def test_the_golden_case_is_the_same_under_two_hash_seeds() -> None:
    # A set of strings iterates in an order that changes with PYTHONHASHSEED, and one test run
    # sees only one seed. So two seeds under which {"adversarial", "out_of_scope"} iterate in
    # opposite orders are found first: a rule that took its repair order from a set would
    # give two different splits for the golden case under them.
    probe = "print(list({'adversarial', 'out_of_scope'}))"
    seeds: dict[str, int] = {}
    for seed in range(64):
        seeds.setdefault(_run_with_hash_seed(seed, probe), seed)
        if len(seeds) == 2:
            break
    assert len(seeds) == 2, "no two hash seeds in 0-63 iterate the set differently"
    results = [json.loads(_run_with_hash_seed(seed, GOLDEN_SCRIPT)) for seed in seeds.values()]
    assert {tuple(r["set_order"]) for r in results} == {
        ("adversarial", "out_of_scope"),
        ("out_of_scope", "adversarial"),
    }
    assert [r["splits"] for r in results] == [GOLDEN, GOLDEN]


# ── Groups: questions that share a split ──────────────────────────────────────────────────────

SHORT: Final = {"sunrise-clinic": "sun", "paws-and-co": "paws"}


def _twins(n: int) -> list[EvalQuestion]:
    """`n` pairs (the same TEST-ONLY text to each business, grouped) and `n` loose rows."""
    rows: list[EvalQuestion] = []
    for k in range(n):
        for business, key in (("sunrise-clinic", SUNRISE_HOURS), ("paws-and-co", PAWS_CANCEL)):
            rows.append(
                question(
                    id=f"example-{SHORT[business]}-{k}",
                    question=f"TEST-ONLY twin {k}",
                    business_id=business,
                    split_group=f"example-pair-{k}",
                    expected_evidence=[[key]],
                )
            )
        rows.append(question(id=f"example-loose-{k}", question=f"TEST-ONLY loose {k}"))
    return rows


def test_a_group_is_one_unit_placed_by_its_first_member_in_rule_order() -> None:
    units = split_units(_twins(1), RULE.seed)
    assert [[q.id for q in u.members] for u in units] == [
        ["example-paws-0", "example-sun-0"],  # paws-and-co sorts before sunrise-clinic
        ["example-loose-0"],
    ]


def test_grouped_questions_always_share_a_split() -> None:
    splits = assign_splits(_twins(10), RULE)  # 10 pairs and 10 loose rows: 20 units, 6 test
    for k in range(10):
        assert splits[f"example-sun-{k}"] == splits[f"example-paws-{k}"], k
    pairs = sum(splits[f"example-sun-{k}"] == "test" for k in range(10))
    loose = sum(splits[f"example-loose-{k}"] == "test" for k in range(10))
    assert pairs + loose == held_out_count(20, 30) == 6


@pytest.mark.parametrize("frozen", ["dev", "test"])
def test_a_new_question_that_joins_a_frozen_group_takes_its_split(frozen: str) -> None:
    old = question(id="example-old", question="TEST-ONLY old", split_group="example-g")
    new = question(
        id="example-new", question="TEST-ONLY new", split_group="example-g", language="hi"
    )
    pinned: dict[str, Any] = {"example-old": frozen}
    assert assign_splits([old, new], RULE, pinned) == {
        "example-old": frozen,
        "example-new": frozen,
    }


def test_a_group_that_mixes_frozen_splits_is_refused_and_its_new_member_waits_in_dev() -> None:
    rows = [
        question(id=f"example-{x}", question=f"TEST-ONLY {x}", split_group="example-g")
        for x in "abc"
    ]
    pinned: dict[str, Any] = {"example-a": "dev", "example-b": "test"}
    assert assign_splits(rows, RULE, pinned) == {
        "example-a": "dev",
        "example-b": "test",
        "example-c": "dev",
    }
    _, freeze = _frozen(rows[:2])
    mixed = freeze.model_copy(
        update={
            "questions": {
                i: freeze.questions[i].model_copy(update={"split": pinned[i]}) for i in pinned
            }
        }
    )
    assert carried_problems(mixed, rows) == [
        "split_group example-g joins questions frozen in dev and in test in v0.1; a split group "
        "shares one split"
    ]


def test_kept_split_reads_the_id_then_replaces() -> None:
    pinned: dict[str, Any] = {"example-1": "test", "example-0": "dev"}
    assert kept_split(question(), pinned) == "test"
    assert kept_split(question(id="example-2", replaces="example-0"), pinned) == "dev"
    assert kept_split(question(id="example-3", replaces="example-9"), pinned) is None
    assert kept_split(question(id="example-4"), pinned) is None


# ── The category repair ───────────────────────────────────────────────────────────────────────


def _line(categories: str) -> list[SplitUnit]:
    """One single-row unit per letter: `f` faq, `p` policy, `a` adversarial. Same author,
    language and business, so every swap is at distance 0."""
    names = {"f": "faq", "p": "policy", "a": "adversarial"}
    return [
        SplitUnit(
            (
                question(
                    id=f"example-{i}",
                    question=f"TEST-ONLY {i}",
                    category=names[c],
                    expected_outcome="dont_know",
                    expected_evidence=[],
                ),
            )
        )
        for i, c in enumerate(categories)
    ]


def test_a_category_without_test_swaps_with_the_nearest_spare_test_row() -> None:
    # faq at 0, 1 (both dev); policy at 2, 3 test and 4 dev. Pairs (i, j) with i in {0, 1},
    # j in {2, 3}: |1 - 2| = 1 is the smallest, so 1 goes to test and 2 to dev.
    test = [False, False, True, True, False]
    repair_categories(_line("ffppp"), test)
    assert test == [False, True, False, True, False]


def test_a_category_all_in_test_swaps_one_into_dev() -> None:
    # adversarial (name order first) is fine; faq at 1, 2 is all test; policy at 3, 4 is dev,
    # with 2 there. The nearest pair is (2, 3).
    test = [True, True, True, False, False]
    repair_categories(_line("affpp"), test)
    assert test == [True, True, False, True, False]


def test_without_a_partner_a_member_moves_alone() -> None:
    # faq has no test row; the only test row is policy's single one, which must stay.
    test = [False, False, True]
    repair_categories(_line("ffp"), test)
    assert test == [True, False, True]


def _unit(i: int, *categories: str) -> SplitUnit:
    """A unit of TEST-ONLY rows, one per category (a group when there are several)."""
    return SplitUnit(
        tuple(
            question(
                id=f"example-{i}-{k}",
                question=f"TEST-ONLY {i} {k}",
                category=c,
                expected_outcome="dont_know",
                expected_evidence=[],
            )
            for k, c in enumerate(categories)
        )
    )


def test_a_partner_may_leave_when_the_incoming_unit_brings_its_category() -> None:
    # faq (units 0, 1) has no test row. Unit 1 is a group holding faq and policy. Unit 2 is the
    # only policy row in test, yet swapping (1, 2) keeps policy in test (unit 1 brings it) and in
    # dev (unit 2 takes it there). It is also the nearest valid pair: (1, 4) and (1, 5) would
    # take dev's only policy row, (0, 2) test's only one; (0, 4) and (0, 5) are farther.
    line = [
        _unit(0, "faq"),
        _unit(1, "faq", "policy"),
        _unit(2, "policy"),
        _unit(3, "adversarial"),
        _unit(4, "adversarial"),
        _unit(5, "adversarial"),
    ]
    test = [False, False, True, False, True, True]
    repair_categories(line, test)
    assert test == [False, True, False, False, True, True]


def test_a_swap_that_would_empty_a_category_in_the_other_split_is_refused() -> None:
    # faq (units 0, 3) has no test row. The nearest pairs, (3, 2) and (3, 4), would move unit 3
    # and with it dev's only policy row: refused. (3, 5) and (0, 2) are both 2 apart; the tie
    # goes to the smaller i, so unit 0 swaps with unit 2.
    line = [
        _unit(0, "faq"),
        _unit(1, "adversarial"),
        _unit(2, "adversarial"),
        _unit(3, "faq", "policy"),
        _unit(4, "adversarial"),
        _unit(5, "policy"),
    ]
    test = [False, False, True, False, True, True]
    repair_categories(line, test)
    assert test == [True, False, False, False, True, True]


# ── Balance ───────────────────────────────────────────────────────────────────────────────────


def test_balance_names_each_group_short_of_test_or_without_dev() -> None:
    friends = [
        _attribute_row(i, "faq", "friend_blind", "en", False, "sunrise-clinic") for i in range(10)
    ]
    others = [
        _attribute_row(i, "policy", "team", "hi", False, "paws-and-co") for i in range(10, 13)
    ]
    rows = friends + others
    # 1 friend row in test (of 10: needs 2), every paws row in test (none in dev).
    splits: dict[str, Any] = {q.id: "dev" for q in rows}
    splits[friends[0].id] = "test"
    splits.update({q.id: "test" for q in others})
    assert balance_problems(rows, splits) == [
        "author friend_blind: 1 of 10 in test, fewer than 2",
        "language hi + hi-code-mixed: all 3 in test, none in dev",
        "business paws-and-co: all 3 in test, none in dev",
        "business sunrise-clinic: 1 of 10 in test, fewer than 2",
        "category policy: all 3 in test, none in dev",
    ]


def test_a_category_counts_units_so_one_group_need_not_be_in_both_splits() -> None:
    # Two translations of one question share a group: one unit, so their category is not
    # required in both splits; a second unit of that category makes it required again.
    pair = [
        question(id="example-en", question="TEST-ONLY en", split_group="example-g"),
        question(id="example-hi", question="TEST-ONLY hi", split_group="example-g", language="hi"),
    ]
    others = [
        _attribute_row(i, "policy", "team", "en", False, "sunrise-clinic") for i in range(10, 14)
    ]
    splits: dict[str, Any] = {q.id: "dev" for q in pair + others}
    splits[others[0].id] = "test"
    assert balance_problems(pair + others, splits) == []
    third = question(id="example-3", question="TEST-ONLY 3")
    splits[third.id] = "dev"
    assert "category faq: 0 of 3 in test, fewer than 1" in balance_problems(
        [*pair, *others, third], splits
    )


def test_balance_groups_list_every_group_in_a_fixed_order() -> None:
    rows = _realistic(("faq",))
    labels = [g.label for g in balance_groups(rows)]
    assert labels[:7] == [
        "author team",
        "author friend_blind",
        "language en",
        "language hi",
        "language hi-code-mixed",
        "language hi + hi-code-mixed",
        "business paws-and-co",
    ]
    assert labels[-1] == "category safety"


# ── Content hashes ────────────────────────────────────────────────────────────────────────────


def test_the_content_hash_is_sha256_of_this_canonical_json() -> None:
    row = question(notes="TEST-ONLY note", split="dev")
    canonical = (
        '{"author_type":"team","business_id":"sunrise-clinic","category":"faq",'
        '"code_mixed":false,"created":"2026-10-22","expected_evidence":[[{"anchor":'
        '"Saturday: 9:00 to 14:00","source_id":"faq","version":"2026-10"}]],'
        '"expected_outcome":"answered","id":"example-1","language":"en",'
        '"question":"TEST-ONLY question 1","reference_answer":null,"replaces":null,'
        '"saw_documents":true,"split_group":null,"subtype":null,"synthetic":true}'
    )
    assert content_hash(row) == hashlib.sha256(canonical.encode()).hexdigest()


def test_notes_and_split_are_not_hashed_but_everything_else_is() -> None:
    base = content_hash(question())
    assert content_hash(question(notes="TEST-ONLY", split="test")) == base
    for change in (
        {"question": "TEST-ONLY question 1!"},
        {"category": "policy"},
        {"expected_outcome": "handoff", "category": "adversarial"},
        {"reference_answer": "TEST-ONLY"},
        {"created": "2026-10-23"},
        {"replaces": "example-0"},
        {"split_group": "example-g"},
        {"expected_evidence": [[SUNRISE_HOURS, SUNRISE_FEE]]},
    ):
        assert content_hash(question(**change)) != base, change
    # Regrouping the same keys is a different label: one group of two vs two groups of one.
    one_group = question(category="multi_hop", expected_evidence=[[SUNRISE_HOURS], [SUNRISE_FEE]])
    assert content_hash(one_group) != content_hash(
        question(expected_evidence=[[SUNRISE_HOURS, SUNRISE_FEE]])
    )
    urgent = {"category": "safety", "expected_outcome": "handoff", "expected_evidence": []}
    assert content_hash(question(**urgent, subtype="emergency")) != content_hash(question(**urgent))


# ── The freeze manifest ───────────────────────────────────────────────────────────────────────


def _frozen(rows: list[EvalQuestion]) -> tuple[list[EvalQuestion], FreezeManifest]:
    splits = assign_splits(rows, RULE)
    assigned = [q.model_copy(update={"split": splits[q.id]}) for q in rows]
    freeze = build_freeze(manifest(), assigned, splits, real_corpus().pins(), date(2026, 11, 1))
    return assigned, freeze


def _problems(
    rows: list[EvalQuestion],
    freeze: FreezeManifest,
    files: dict[str, Any] | None = None,
    manifests: dict[str, str] | None = None,
) -> list[str]:
    pins = real_corpus().pins()
    changed = CorpusPins(
        files={**pins.files, **(files or {})}, manifests={**pins.manifests, **(manifests or {})}
    )
    return freeze_problems(manifest(stage="frozen"), freeze, rows, changed)


def test_an_untouched_freeze_has_no_problems_and_survives_a_round_trip() -> None:
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    assert _problems(rows, freeze) == []
    assert load_freeze(freeze.model_dump_json()) == freeze
    assert pins_from(freeze) == {q.id: q.split for q in rows}
    assert pins_from(None) == {}
    assert set(freeze.corpus_manifests) == {"sunrise-clinic", "paws-and-co"}


def test_every_change_after_the_freeze_is_named_with_the_way_out() -> None:
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    test_row = next(q for q in rows if q.split == "test")
    dev_row = next(q for q in rows if q.split == "dev")
    changed = [
        test_row.model_copy(update={"question": "TEST-ONLY edited"}),
        dev_row.model_copy(update={"split": "test"}),
        question(id="example-new", question="TEST-ONLY new", split="dev"),
    ]
    found = _problems(changed, freeze, files={"paws-and-co": {"faq.md": "0" * 64}})
    gone = sorted(set(freeze.questions) - {test_row.id, dev_row.id})
    assert found == [
        *(f"question {i} was frozen and is gone; {WAY_OUT}" for i in gone),
        f"test question {test_row.id} changed; {WAY_OUT}",
        f"question {dev_row.id} moved from dev to test; {WAY_OUT}",
        f"question example-new was added after the freeze; {WAY_OUT}",
        f"the corpus of paws-and-co changed after the freeze; {WAY_OUT}",
    ]


@pytest.mark.parametrize(
    ("source_id", "change"),
    [
        ("staff-notes", {"audience": "public"}),  # a staff-only document becomes answerable
        ("holiday-hours", {"status": "approved"}),  # a draft becomes answerable
    ],
)
def test_a_corpus_manifest_change_after_the_freeze_is_a_problem(
    source_id: str, change: dict[str, str]
) -> None:
    # kenji's review (K4): no file changes, but what may answer a customer does.
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    paws = real_corpus().manifests["paws-and-co"]
    sources = tuple(
        s.model_copy(update=change) if s.source_id == source_id else s for s in paws.sources
    )
    flipped = manifest_sha256(paws.model_copy(update={"sources": sources}))
    assert _problems(rows, freeze, manifests={"paws-and-co": flipped}) == [
        f"the corpus manifest of paws-and-co changed after the freeze; {WAY_OUT}"
    ]


def test_the_manifest_hash_ignores_what_decides_no_answer() -> None:
    paws = real_corpus().manifests["paws-and-co"]
    renamed = paws.model_copy(update={"name": "TEST-ONLY", "review": "done"})
    assert manifest_sha256(renamed) == manifest_sha256(paws)
    assert manifest_sha256(paws.model_copy(update={"languages": ("en",)})) != manifest_sha256(paws)


def test_a_freeze_of_another_version_or_rule_is_refused() -> None:
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    other = freeze.model_copy(
        update={"version": "v0.2", "split_rule": RULE.model_copy(update={"test_percent": 50})}
    )
    found = freeze_problems(manifest(stage="frozen"), other, rows, real_corpus().pins())
    assert found[:2] == [
        "freeze.json belongs to another dataset or version",
        f"the split rule changed after the freeze; {WAY_OUT}",
    ]


def test_a_new_version_may_keep_an_id_only_for_the_same_question() -> None:
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    same = rows[0].model_copy(update={"split": None, "notes": "TEST-ONLY comment"})
    edited = rows[1].model_copy(update={"question": "TEST-ONLY edited", "split": None})
    assert carried_problems(freeze, [same, edited]) == [
        f"question {edited.id} reuses an id from v0.1 for a changed question; give it a new id "
        "and set `replaces`"
    ]


def test_replaces_must_name_a_frozen_id_that_is_gone_and_replaced_once() -> None:
    rows, freeze = _frozen(_realistic(("faq",))[:6])
    kept, old, other = rows[0], rows[1], rows[2]

    def fix(new_id: str, replaces: str) -> EvalQuestion:
        return question(id=new_id, question=f"TEST-ONLY {new_id}", replaces=replaces)

    assert carried_problems(freeze, [kept, fix("example-a", old.id)]) == []
    assert carried_problems(
        freeze,
        [
            kept,
            fix("example-b", "example-unknown"),
            fix("example-c", kept.id),
            fix("example-d", other.id),
            fix("example-e", other.id),
        ],
    ) == [
        "question example-b replaces example-unknown, which is not in v0.1's freeze.json",
        f"question example-c replaces {kept.id}, which is still in this version",
        f"question {other.id} is replaced 2 times",
    ]


def test_a_bad_freeze_file_names_its_fields() -> None:
    with pytest.raises(
        DatasetError, match=r"^freeze.json is not a valid freeze manifest: .*questions"
    ):
        load_freeze('{"dataset": "support_qa", "version": "v0.1"}')
