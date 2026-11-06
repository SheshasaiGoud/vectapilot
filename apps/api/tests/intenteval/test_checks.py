"""M4 PR 1: the stage-aware check, D3's minimums and the freeze (on made-up rows)."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Final

import pytest

from tests.intenteval.helpers import (
    PROFILES,
    SMALL_REQUIREMENTS,
    complete_dataset,
    complete_eval,
    complete_train,
    dataset,
    eval_row,
    filler,
    manifest,
    noah_rows,
    train_row,
)
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.evalset.questions import PLACEHOLDER_MARKER
from vectapilot_api.intenteval.checks import (
    IntentDataset,
    check_dataset,
    freeze_dataset,
    minimums,
    readiness_problems,
)
from vectapilot_api.intenteval.rows import BusinessProfile, EvalRow, TrainRow
from vectapilot_api.intenteval.sample import draw_sample
from vectapilot_api.intenteval.split import assign_splits, build_freeze
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

FROZEN_ON: Final = date(2026, 11, 20)
SIX: Final = "alpha beta gamma delta epsilon zeta"
NEAR: Final = "alpha beta gamma kappa epsilon zeta"  # 26/37 of SIX: just above 70 %


def problems_of(**fields: object) -> list[str]:
    return list(check_dataset(dataset(**fields), allow_placeholders=True).problems)  # type: ignore[arg-type]


def test_an_empty_collecting_version_passes() -> None:
    result = check_dataset(dataset())
    assert (result.problems, result.warnings) == ((), ())


def test_a_business_without_a_manifest_or_with_an_unknown_type_is_refused() -> None:
    businesses = {
        "sunrise-clinic": BusinessProfile(business_type="human-healthcare", languages=("en",))
    }
    problems = problems_of(businesses=businesses)
    assert problems[0] == "business sunrise-clinic: unknown business type 'human-healthcare'; " + (
        "the platform knows ['human_healthcare', 'veterinary', 'ecommerce', 'retail', 'service']"
    )
    assert problems[1] == "business paws-and-co has no manifest under data/sample"


def test_rows_are_checked_for_business_language_placeholder_and_duplicates() -> None:
    train = [train_row(id="example-t1", business_id="other-shop")]
    evals = [
        eval_row(id="example-e1", business_id="paws-and-co", language="hi"),
        eval_row(id="example-t1", text=filler("another text")),
        eval_row(id="example-e3", split="dev"),
    ]
    result = check_dataset(dataset(train=train, eval_rows=evals))
    assert list(result.problems) == [
        "row id example-t1 is used more than once",
        "train.jsonl row example-t1: its business is not in the dataset manifest",
        "train.jsonl row example-t1: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "eval.jsonl row example-e1: its business does not list hi",
        "eval.jsonl row example-e1: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "eval.jsonl row example-t1: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "eval.jsonl row example-e3: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "eval.jsonl row example-e3: split is set by `freeze`, never by hand (leave it null)",
    ]


@pytest.mark.parametrize("row_id", ["example", "example-1"])
def test_a_placeholder_id_alone_is_refused(row_id: str) -> None:
    row = eval_row(id=row_id, text="alpha beta gamma")
    assert check_dataset(dataset(eval_rows=[row])).problems == (
        f"eval.jsonl row {row_id}: is a placeholder (a marker such as EXAMPLE-ONLY)",
    )


def test_placeholder_text_alone_is_refused() -> None:
    # Every other placeholder case has an `example-` id, which the id rule refuses on its own.
    # Here the ids are ordinary and only the text carries a marker (each spelling once), so only
    # the text screen can catch it.
    train = [train_row(id="t1", text=filler("t1"))]  # TEST-ONLY <hash>
    evals = [eval_row(id="e1", text=f"{PLACEHOLDER_MARKER} alpha beta gamma")]
    assert check_dataset(dataset(train=train, eval_rows=evals)).problems == (
        "train.jsonl row t1: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "eval.jsonl row e1: is a placeholder (a marker such as EXAMPLE-ONLY)",
    )
    clean = [eval_row(id="e1", text="alpha beta gamma")]
    assert check_dataset(dataset(eval_rows=clean)).problems == ()


def test_freeze_refuses_placeholder_text_under_ordinary_ids() -> None:
    train = [r.model_copy(update={"id": r.id.removeprefix("example-")}) for r in complete_train()]
    evals = [r.model_copy(update={"id": r.id.removeprefix("example-")}) for r in complete_eval()]
    complete = complete_dataset(
        train=train, eval_rows=evals, sample=draw_sample(train, manifest().verification)
    )
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(complete, frozen_on=FROZEN_ON)
    problems = refused.value.problems
    assert len(problems) == len(train) + len(evals) == 34
    assert all(p.endswith(": is a placeholder (a marker such as EXAMPLE-ONLY)") for p in problems)


def test_personal_data_is_named_by_row_never_by_text() -> None:
    row = eval_row(text="TEST-ONLY call 98765 43210 or someone@gmail.com")
    problems = problems_of(eval_rows=[row])
    assert problems == [
        "eval.jsonl row example-e1: text holds what looks like personal data (a phone-like "
        "number that is not a fake +91 00000 number); invent it (README, 'Content rules')",
        "eval.jsonl row example-e1: text holds what looks like personal data (an e-mail address "
        "outside .example); invent it (README, 'Content rules')",
    ]
    assert not any("98765" in p or "gmail" in p for p in problems)


@pytest.mark.parametrize("make", [train_row, eval_row])
def test_every_row_is_screened_for_personal_data_whatever_its_business(
    make: Callable[..., TrainRow | EvalRow],
) -> None:
    # zara: a row with an unlisted business was skipped before the screen, so a writer fixing
    # the business id learned of the number only on the next run, perhaps after a push.
    row = make(business_id="other-shop", text="TEST-ONLY call 98765 43210")
    problems = problems_of(**{"train" if make is train_row else "eval_rows": [row]})
    file_name = "train.jsonl" if make is train_row else "eval.jsonl"
    assert problems == [
        f"{file_name} row {row.id}: its business is not in the dataset manifest",
        f"{file_name} row {row.id}: text holds what looks like personal data (a phone-like "
        "number that is not a fake +91 00000 number); invent it (README, 'Content rules')",
    ]
    assert not any("98765" in p or "43210" in p for p in problems)


def test_a_train_eval_near_duplicate_is_refused_and_named_by_id() -> None:
    problems = problems_of(
        train=[train_row(id="example-t1", text=SIX)],
        eval_rows=[eval_row(id="example-e1", text=NEAR)],
    )
    assert problems == [
        "train row example-t1 and eval row example-e1 are near-duplicates (70 %, threshold "
        "70 %): drop the train row (README, 'Near-duplicates')"
    ]


def test_near_duplicate_eval_rows_must_share_a_split_group() -> None:
    twins = [
        eval_row(id="example-e1", text=SIX),
        eval_row(id="example-e2", text=SIX.upper(), business_id="paws-and-co"),
    ]
    assert problems_of(eval_rows=twins) == [
        "eval rows example-e1 and example-e2 are near-duplicates (100 %, threshold 70 %) and "
        "share no split_group: give both one, or drop one (README, 'Near-duplicates')"
    ]
    grouped = [r.model_copy(update={"split_group": "example-pair"}) for r in twins]
    assert problems_of(eval_rows=grouped) == []
    other = [grouped[0], grouped[1].model_copy(update={"split_group": "example-other"})]
    assert len(problems_of(eval_rows=other)) == 1


def test_distinct_rows_pass_the_screen() -> None:
    train = [train_row(id=f"example-t{k}") for k in range(5)]
    evals = [eval_row(id=f"example-e{k}") for k in range(5)]
    assert problems_of(train=train, eval_rows=evals) == []


def test_a_split_group_of_one_is_a_warning() -> None:
    result = check_dataset(
        dataset(eval_rows=[eval_row(split_group="example-lonely")]), allow_placeholders=True
    )
    assert result.ok
    assert result.warnings == ("split_group example-lonely has only one row",)


def test_a_freeze_file_while_collecting_is_a_problem() -> None:
    complete = complete_dataset()
    rows = [r.model_copy(update={"split": "dev"}) for r in complete.eval_rows]
    sample = complete.sample or ()
    freeze = build_freeze(complete.manifest, complete.train, rows, PROFILES, sample, FROZEN_ON)
    assert problems_of(freeze=freeze) == [
        "freeze.json exists, but the manifest's stage is collecting"
    ]


# ── The verification sample is the seeded draw ───────────────────────────────────────────────

DRAFT: Final = "a" * 64
SAMPLE_NOT_DRAWN: Final = (
    "verification-sample.txt is not the seeded draw of the training draft: run `sample` again, "
    "never choose which rows to verify"
)


def test_while_train_is_the_draft_the_sample_must_be_the_seeded_draw() -> None:
    train = complete_train()
    drawn = draw_sample(train, manifest().verification)
    chosen = drawn[1:]  # a hand-picked list: one drawn row left out
    draft = {"train": train, "train_sha256": DRAFT, "train_draft_sha256": DRAFT}
    assert problems_of(sample=drawn, **draft) == []
    assert problems_of(sample=chosen, **draft) == [SAMPLE_NOT_DRAWN]
    # No sample yet, no draft recorded, or train changed since the draft: nothing to compare.
    assert problems_of(sample=None, **draft) == []
    assert problems_of(sample=chosen, train=train, train_sha256=DRAFT) == []
    assert problems_of(sample=chosen, **{**draft, "train_sha256": "b" * 64}) == []


def test_the_sample_draw_is_checked_in_the_frozen_stage_too() -> None:
    complete = complete_dataset(train_sha256=DRAFT, train_draft_sha256=DRAFT)
    frozen = freeze_dataset(complete, frozen_on=FROZEN_ON, allow_placeholders=True)
    chosen = tuple(reversed(complete.sample or ()))
    after = complete_dataset(
        eval_rows=frozen.eval_rows,
        freeze=frozen.freeze,
        stage="frozen",
        sample=chosen,
        train_sha256=DRAFT,
        train_draft_sha256=DRAFT,
    )
    problems = check_dataset(after, allow_placeholders=True).problems
    assert problems == (
        SAMPLE_NOT_DRAWN,
        "the verification sample changed after the freeze; v0.1 is frozen: make a new version "
        "instead (README, 'Adding a version')",
    )


# ── D3's minimums ─────────────────────────────────────────────────────────────────────────────


def test_an_empty_version_misses_every_minimum_and_the_sample() -> None:
    problems = readiness_problems(dataset())
    assert problems[0] == "eval rows: 0 of at least 160"
    assert "eval rows of out_of_scope: 0 of at least 20" in problems
    assert "business sunrise-clinic: eval rows of professional_judgement: 0 of at least 10" in (
        problems
    )
    assert "business sunrise-clinic: eval safety rows in Hindi or Hinglish: 0 of at least 6" in (
        problems
    )
    assert "business sunrise-clinic: eval emergency rows in Hindi or Hinglish: 0 of at least 3" in (
        problems
    )
    assert not any("paws-and-co: eval safety rows in Hindi" in p for p in problems)
    assert "train rows of question: 0 of at least 40" in problems
    assert (
        problems[-1] == "no verification-sample.txt: run `sample`, then verify every row it lists"
    )


def test_a_complete_dataset_meets_every_minimum_exactly() -> None:
    complete = complete_dataset()
    assert readiness_problems(complete) == []
    for k in range(len(complete.eval_rows)):
        fewer = [*complete.eval_rows[:k], *complete.eval_rows[k + 1 :]]
        assert readiness_problems(complete_dataset(eval_rows=fewer)), k


def test_the_safety_minimums_follow_each_business_type() -> None:
    # A business of a default type has one safety intent (emergency) and no Hindi minimum.
    shop = {"paws-and-co": BusinessProfile(business_type="retail", languages=("en", "hi"))}
    names = [m.what for m in minimums(dataset(businesses={**PROFILES, **shop}))]
    assert "business paws-and-co: eval rows of emergency" in names
    assert "business paws-and-co: eval rows of professional_judgement" not in names
    assert "business paws-and-co: eval safety rows in Hindi or Hinglish" in names


def test_hindi_minimums_count_hindi_and_hinglish_safety_rows_only() -> None:
    evals = [
        eval_row(id="example-1", language="hi", intent=Intent.EMERGENCY),
        eval_row(id="example-2", language="hi", code_mixed=True, intent=Intent.EMERGENCY),
        eval_row(id="example-3", language="hi", intent=Intent.QUESTION),
        eval_row(id="example-4", intent=Intent.EMERGENCY),
    ]
    found = {m.what: m.have for m in minimums(dataset(eval_rows=evals))}
    assert found["business sunrise-clinic: eval safety rows in Hindi or Hinglish"] == 2
    assert found["business sunrise-clinic: eval emergency rows in Hindi or Hinglish"] == 2
    assert found["business sunrise-clinic: eval rows of emergency"] == 3


# ── The freeze ────────────────────────────────────────────────────────────────────────────────


def test_freeze_assigns_dev_and_test_and_pins_the_version() -> None:
    complete = complete_dataset()
    frozen = freeze_dataset(complete, frozen_on=FROZEN_ON, allow_placeholders=True)
    assert frozen.manifest.stage == "frozen"
    assert frozen.manifest.model_copy(update={"stage": "collecting"}) == complete.manifest
    splits = {r.id: r.split for r in frozen.eval_rows}
    assert set(splits.values()) == {"dev", "test"}
    for intent in DECISION_ORDER:
        assert {r.split for r in frozen.eval_rows if r.intent == intent} == {"dev", "test"}
    assert frozen.freeze.frozen_on == FROZEN_ON
    assert {i: f.split for i, f in frozen.freeze.eval_rows.items()} == splits
    assert set(frozen.freeze.train_rows) == {r.id for r in complete.train}
    assert frozen.freeze.businesses == PROFILES
    assert frozen.freeze.verification_sample == complete.sample

    after = complete_dataset(eval_rows=frozen.eval_rows, freeze=frozen.freeze, stage="frozen")
    assert check_dataset(after, allow_placeholders=True).problems == ()


def test_a_frozen_version_fails_when_a_row_changes() -> None:
    complete = complete_dataset()
    frozen = freeze_dataset(complete, frozen_on=FROZEN_ON, allow_placeholders=True)
    changed = [
        frozen.eval_rows[0].model_copy(update={"text": filler("changed")}),
        *frozen.eval_rows[1:],
    ]
    after = complete_dataset(eval_rows=changed, freeze=frozen.freeze, stage="frozen")
    problems = check_dataset(after, allow_placeholders=True).problems
    assert problems == (
        f"eval row {changed[0].id} changed after the freeze; v0.1 is frozen: make a new version "
        "instead (README, 'Adding a version')",
    )


def frozen_by_hand(collecting: IntentDataset) -> IntentDataset:
    """`collecting` frozen without `freeze`'s gate: splits by the manifest's rule and a freeze.json
    that matches every row, so no pin differs and only the readiness check can fail."""
    splits = assign_splits(collecting.eval_rows, collecting.manifest.split_rule)
    rows = tuple(r.model_copy(update={"split": splits[r.id]}) for r in collecting.eval_rows)
    sample = collecting.sample or ()
    freeze = build_freeze(
        collecting.manifest, collecting.train, rows, collecting.businesses, sample, FROZEN_ON
    )
    stage = collecting.manifest.model_copy(update={"stage": "frozen"})
    return replace(collecting, manifest=stage, eval_rows=rows, freeze=freeze)


def test_a_frozen_version_below_a_minimum_fails_check() -> None:
    # The backstop for a freeze not written by `freeze` (the stage set by hand, or freeze.json
    # built from too few rows): freeze.json pins neither the minimums nor whether the sample is
    # verified, so only the frozen branch's readiness check sees these.
    short = frozen_by_hand(complete_dataset(eval_rows=complete_eval()[1:]))
    assert check_dataset(short, allow_placeholders=True).problems == (
        f"eval rows: 17 of at least {SMALL_REQUIREMENTS['min_eval_rows']}",
    )

    train = complete_train()
    sampled = draw_sample(train, manifest().verification)
    late = [r.model_copy(update={"verified": r.id != sampled[0]}) for r in train]
    unverified = frozen_by_hand(complete_dataset(train=late))
    assert unverified.sample == sampled
    assert check_dataset(unverified, allow_placeholders=True).problems == (
        f"1 of {len(sampled)} sampled training rows are not verified yet",
    )


def test_a_frozen_stage_needs_its_freeze_and_every_split() -> None:
    complete = complete_dataset(stage="frozen")
    problems = check_dataset(complete, allow_placeholders=True).problems
    assert problems == (
        "18 eval row(s) have no split in a frozen version",
        "the manifest's stage is frozen, but there is no freeze.json",
    )


def test_freeze_refuses_placeholders_short_minimums_and_an_unverified_sample() -> None:
    with pytest.raises(FreezeRefused) as placeholders:
        freeze_dataset(complete_dataset(), frozen_on=FROZEN_ON)
    assert all("is a placeholder" in p for p in placeholders.value.problems)

    with pytest.raises(FreezeRefused) as short:
        freeze_dataset(
            complete_dataset(eval_rows=complete_eval()[1:]),
            frozen_on=FROZEN_ON,
            allow_placeholders=True,
        )
    assert (
        short.value.problems[0]
        == f"eval rows: 17 of at least {SMALL_REQUIREMENTS['min_eval_rows']}"
    )

    unverified = [r.model_copy(update={"verified": False}) for r in complete_train()]
    with pytest.raises(FreezeRefused) as sample:
        freeze_dataset(
            complete_dataset(train=unverified), frozen_on=FROZEN_ON, allow_placeholders=True
        )
    assert sample.value.problems == ("16 of 16 sampled training rows are not verified yet",)


LOW_REQUIREMENTS: Final = {
    "min_eval_rows": 1,
    "min_eval_per_intent": 1,
    "min_eval_per_safety_intent_per_business": 0,
    "min_hindi_safety_per_hindi_business": 0,
    "min_hindi_emergency_per_hindi_business": 0,
    "min_train_per_intent": 1,
}


def test_freeze_refuses_a_split_the_repair_could_not_balance_and_writes_nothing() -> None:
    # noah's six rows (helpers.NOAH_LAYOUT), plus one row of each missing intent so that the
    # minimums hold: the repair leaves both complaint units in dev.
    extra = (
        ("r6", "sunrise-clinic", "human_request"),
        ("r7", "paws-and-co", "action_request"),
        ("r8", "sunrise-clinic", "question"),
        ("r9", "paws-and-co", "out_of_scope"),
    )
    rows = [*noah_rows(), *(eval_row(id=i, business_id=b, intent=n) for i, b, n in extra)]
    complete = complete_dataset(eval_rows=rows, requirements=LOW_REQUIREMENTS)
    assert check_dataset(complete, allow_placeholders=True).problems == ()
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(complete, frozen_on=FROZEN_ON, allow_placeholders=True)
    assert refused.value.problems == ("intent complaint: 0 of 2 in test, fewer than 1",)
    assert refused.value.unbalanced


def test_freeze_refuses_a_business_whose_safety_intent_sits_in_one_split() -> None:
    # The helpers' set with the Hindi judgement row by shesha instead of a friend: every intent
    # is in both splits, but both of Sunrise's professional_judgement rows land in dev.
    rows = [
        r.model_copy(update={"author": "shesha"}) if r.id == "example-e-sun-hi-judgement" else r
        for r in complete_eval()
    ]
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(
            complete_dataset(eval_rows=rows), frozen_on=FROZEN_ON, allow_placeholders=True
        )
    assert refused.value.problems == (
        "business sunrise-clinic, safety intent professional_judgement: 0 of 2 in test, "
        "fewer than 1",
    )
    assert refused.value.unbalanced


def test_other_refusals_are_not_marked_unbalanced() -> None:
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(complete_dataset(eval_rows=complete_eval()[1:]), frozen_on=FROZEN_ON)
    assert not refused.value.unbalanced


def test_freeze_refuses_a_version_that_is_already_frozen() -> None:
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(complete_dataset(stage="frozen"), frozen_on=FROZEN_ON)
    assert refused.value.problems == ("v0.1 is already frozen",)
