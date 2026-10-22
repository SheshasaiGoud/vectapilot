"""M2 PR 7: one stage-aware check of a version, the freeze, and the way to a new version.

The rows are made-up placeholders (helpers.py), so every call here passes
`allow_placeholders=True`; the real dataset's check never does.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any, Final

import pytest

from tests.evalset.helpers import (
    SMALL_REQUIREMENTS,
    copy_reviewed_corpus,
    manifest,
    manifest_data,
    one_per_category,
    question,
    real_corpus,
    reviewed_corpus,
)
from vectapilot_api.evalset import checks
from vectapilot_api.evalset.checks import (
    FreezeRefused,
    check_dataset,
    freeze_dataset,
    preview_splits,
    select_split,
)
from vectapilot_api.evalset.files import read_dataset, write_frozen, write_questions
from vectapilot_api.evalset.questions import EvalManifest

TODAY: Final = date(2026, 11, 2)
PENDING: Final = (
    "sunrise-clinic/faq-hi.md (faq-hi 2026-10) is review: pending; shesha reviews it and sets "
    "`review` to done in its manifest entry before the freeze"
)


def small(**overrides: Any) -> EvalManifest:
    return manifest(requirements=SMALL_REQUIREMENTS, **overrides)


def test_an_empty_collecting_version_passes() -> None:
    result = check_dataset(manifest(), [], real_corpus(), freeze=None)
    assert result.ok
    assert (result.problems, result.warnings) == ((), ())


def test_a_business_without_a_corpus_is_a_problem() -> None:
    result = check_dataset(
        manifest(businesses=["sunrise-clinic", "nowhere-shop"]), [], real_corpus(), freeze=None
    )
    assert result.problems == ("business nowhere-shop has no corpus under data/sample",)


def test_a_frozen_stage_needs_the_requirements_the_splits_and_a_freeze_file() -> None:
    result = check_dataset(
        manifest(stage="frozen"), [question()], real_corpus(), freeze=None, allow_placeholders=True
    )
    assert "questions: 1 of at least 40" in result.problems
    assert "1 question(s) have no split in a frozen version" in result.problems
    assert result.problems[-1] == "the manifest's stage is frozen, but there is no freeze.json"


def _freeze(rows: list[Any], **overrides: Any) -> Any:
    return freeze_dataset(
        small(**overrides),
        rows,
        reviewed_corpus(),
        previous_freeze=None,
        frozen_on=TODAY,
        allow_placeholders=True,
    )


def test_freeze_assigns_every_split_and_the_result_checks_clean() -> None:
    frozen = _freeze(one_per_category())
    assert frozen.manifest.stage == "frozen"
    # 8 questions, round(8 * 30 / 100) = 2 to test. The line is Paws & Co's 4, then Sunrise's
    # 4, and evenly spaced positions put one test question in each half.
    assert [q.split for q in frozen.questions].count("test") == 2
    for business in ("sunrise-clinic", "paws-and-co"):
        assert [q.split for q in frozen.questions if q.business_id == business].count("test") == 1
    assert frozen.freeze.frozen_on == TODAY
    assert set(frozen.freeze.questions) == {q.id for q in frozen.questions}
    result = check_dataset(
        frozen.manifest,
        frozen.questions,
        reviewed_corpus(),
        freeze=frozen.freeze,
        allow_placeholders=True,
    )
    assert result.ok, result.problems
    assert (
        len(select_split(frozen.questions, "dev")) + len(select_split(frozen.questions, "test"))
        == 8
    )


def test_a_collecting_version_with_a_freeze_file_is_a_problem() -> None:
    frozen = _freeze(one_per_category())
    rows = [q.model_copy(update={"split": None}) for q in frozen.questions]
    result = check_dataset(
        small(), rows, real_corpus(), freeze=frozen.freeze, allow_placeholders=True
    )
    assert result.problems == ("freeze.json exists, but the manifest's stage is collecting",)


def test_freeze_is_refused_unless_ready_valid_and_collecting() -> None:
    with pytest.raises(FreezeRefused) as caught:
        freeze_dataset(manifest(), [], real_corpus(), previous_freeze=None, frozen_on=TODAY)
    assert caught.value.problems[0] == "questions: 0 of at least 40"
    with pytest.raises(FreezeRefused) as caught:
        _freeze([question()], stage="frozen")
    assert caught.value.problems == ("v0.1 is already frozen",)
    with pytest.raises(FreezeRefused) as caught:
        _freeze([question()], previous_version="v0.0")
    assert caught.value.problems == ("v0.0's freeze.json is missing",)
    with pytest.raises(FreezeRefused) as caught:  # the CLI's way: placeholders refused
        freeze_dataset(
            small(), one_per_category(), reviewed_corpus(), previous_freeze=None, frozen_on=TODAY
        )
    assert caught.value.problems == tuple(
        f"question {q.id}: is a placeholder (a marker such as EXAMPLE-ONLY)"
        for q in one_per_category()
    )  # the only reason left: the same rows freeze with allow_placeholders (above)
    assert caught.value.unbalanced is False


def test_freeze_waits_until_every_pending_document_is_reviewed() -> None:
    # Decided 2026-10-22, item 1: faq-hi stays `review: pending` until shesha has read it, and
    # no version is frozen against an unreviewed document.
    with pytest.raises(FreezeRefused) as caught:
        freeze_dataset(
            small(),
            one_per_category(),
            real_corpus(),
            previous_freeze=None,
            frozen_on=TODAY,
            allow_placeholders=True,
        )
    assert caught.value.problems == (PENDING,)
    assert _freeze(one_per_category()).manifest.stage == "frozen"  # the same, once reviewed


def test_a_frozen_version_with_a_pending_review_fails_its_check() -> None:
    frozen = _freeze(one_per_category())
    result = check_dataset(
        frozen.manifest,
        frozen.questions,
        real_corpus(),  # faq-hi pending again; the manifest hash leaves `review` out
        freeze=frozen.freeze,
        allow_placeholders=True,
    )
    assert result.problems == (PENDING,)


def test_freeze_is_refused_when_the_splits_would_be_unbalanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The rule balances real layouts (test_split.py); this proves the refusal is wired in.
    monkeypatch.setattr(checks, "assign_splits", lambda rows, *_: {q.id: "dev" for q in rows})
    with pytest.raises(FreezeRefused) as caught:
        _freeze(one_per_category())
    assert caught.value.problems == (
        "business paws-and-co: 0 of 4 in test, fewer than 1",
        "business sunrise-clinic: 0 of 4 in test, fewer than 1",
    )
    assert caught.value.unbalanced is True  # the CLI prints the way out


def test_ready_previews_the_split_without_writing() -> None:
    preview = preview_splits(small(), one_per_category(), previous_freeze=None)
    rows = {group: (dev, test) for group, dev, test in preview.rows}
    assert rows["total"] == (6, 2)
    assert rows["author team"] == (6, 2)
    assert rows["author friend_blind"] == (0, 0)
    assert (rows["business paws-and-co"], rows["business sunrise-clinic"]) == ((3, 1), (3, 1))
    assert preview.problems == ()
    assert all(q.split is None for q in one_per_category())


def test_select_split_refuses_an_unfrozen_dataset_or_an_unknown_split() -> None:
    with pytest.raises(ValueError, match="not frozen"):
        select_split([question()], "dev")
    with pytest.raises(ValueError, match="split must be one of"):
        select_split([question(split="dev")], "Dev")  # type: ignore[arg-type]


def test_replaces_without_a_previous_version_is_a_problem() -> None:
    row = question(replaces="example-0")
    result = check_dataset(manifest(), [row], real_corpus(), freeze=None, allow_placeholders=True)
    assert result.problems == (
        "question example-1 sets `replaces`, but there is no previous version's freeze.json",
    )


# ── Files: freeze, read back, edit, and a new version ─────────────────────────────────────────


def _version(tmp_path: Path, version: str, **overrides: Any) -> Path:
    folder = tmp_path / "evals" / "datasets" / "support_qa" / version
    folder.mkdir(parents=True)
    data = manifest_data(version=version, requirements=SMALL_REQUIREMENTS, **overrides)
    (folder / "manifest.json").write_text(json.dumps(data))
    (folder / "questions.jsonl").write_text("")
    return folder


def test_a_frozen_version_on_disk_turns_red_when_a_test_question_changes(tmp_path: Path) -> None:
    copy_reviewed_corpus(tmp_path / "data" / "sample")
    folder = _version(tmp_path, "v0.1")
    extra = [question(id=f"example-faq-{i}", question=f"TEST-ONLY faq {i}") for i in (2, 3)]
    write_questions(folder, [*one_per_category(), *extra])  # faq: 3 questions, 1 goes to test
    dataset = read_dataset(folder)
    frozen = freeze_dataset(
        dataset.manifest,
        dataset.questions,
        dataset.corpus,
        previous_freeze=None,
        frozen_on=TODAY,
        allow_placeholders=True,
    )
    write_frozen(folder, frozen)

    reread = read_dataset(folder)
    assert reread.manifest.stage == "frozen"
    assert reread.freeze == frozen.freeze
    assert check_dataset(
        reread.manifest,
        reread.questions,
        reread.corpus,
        freeze=reread.freeze,
        allow_placeholders=True,
    ).ok

    test_id = next(q.id for q in reread.questions if q.split == "test")
    lines = (folder / "questions.jsonl").read_text("utf-8").splitlines()
    edited = [
        line.replace("TEST-ONLY", "TEST-ONLY edited") if f'"id": "{test_id}"' in line else line
        for line in lines
    ]
    (folder / "questions.jsonl").write_text("\n".join(edited) + "\n", "utf-8")
    again = read_dataset(folder)
    result = check_dataset(
        again.manifest, again.questions, again.corpus, freeze=again.freeze, allow_placeholders=True
    )
    assert result.problems == (
        f"test question {test_id} changed; v0.1 is frozen: make a new version instead "
        "(README, 'Adding a version')",
    )

    # A corpus file edited in place is caught too.
    faq = tmp_path / "data" / "sample" / "paws-and-co" / "faq.md"
    faq.write_text(faq.read_text("utf-8") + "\nTEST-ONLY extra line.\n", "utf-8")
    after = read_dataset(folder)
    found = check_dataset(
        after.manifest, after.questions, after.corpus, freeze=after.freeze, allow_placeholders=True
    ).problems
    assert found[-1].startswith("the corpus of paws-and-co changed after the freeze")


def test_a_new_version_keeps_every_earlier_split(tmp_path: Path) -> None:
    copy_reviewed_corpus(tmp_path / "data" / "sample")
    first = _version(tmp_path, "v0.1")
    write_questions(first, one_per_category())
    one = read_dataset(first)
    frozen_one = freeze_dataset(
        one.manifest,
        one.questions,
        one.corpus,
        previous_freeze=None,
        frozen_on=TODAY,
        allow_placeholders=True,
    )
    write_frozen(first, frozen_one)

    second = _version(tmp_path, "v0.2", previous_version="v0.1")
    added = [question(id=f"example-new-{i}", question=f"TEST-ONLY new {i}") for i in range(4)]
    carried = [q.model_copy(update={"split": None}) for q in frozen_one.questions]
    write_questions(second, [*carried, *added])
    two = read_dataset(second)
    assert two.previous_freeze == frozen_one.freeze
    frozen_two = freeze_dataset(
        two.manifest,
        two.questions,
        two.corpus,
        previous_freeze=two.previous_freeze,
        frozen_on=TODAY,
        allow_placeholders=True,
    )
    splits = {q.id: q.split for q in frozen_two.questions}
    assert all(splits[q.id] == q.split for q in frozen_one.questions)
    assert sum(splits[q.id] == "test" for q in added) == 1  # 4 new faq rows: 1.2 → 1

    changed = [carried[0].model_copy(update={"question": "TEST-ONLY changed"}), *carried[1:]]
    result = check_dataset(
        two.manifest,
        changed,
        two.corpus,
        freeze=None,
        previous_freeze=two.previous_freeze,
        allow_placeholders=True,
    )
    assert result.problems == (
        f"question {carried[0].id} reuses an id from v0.1 for a changed question; give it a new "
        "id and set `replaces`",
    )


def test_a_corrected_dev_question_stays_dev_in_the_next_version(tmp_path: Path) -> None:
    # kenji's review (K2): a label-only fix (here a corrected reference answer) needs a new id,
    # and a new id would go through the rule again; `replaces` keeps the old split.
    copy_reviewed_corpus(tmp_path / "data" / "sample")
    first = _version(tmp_path, "v0.1")
    write_questions(first, one_per_category())
    one = read_dataset(first)
    frozen_one = freeze_dataset(
        one.manifest,
        one.questions,
        one.corpus,
        previous_freeze=None,
        frozen_on=TODAY,
        allow_placeholders=True,
    )
    write_frozen(first, frozen_one)
    old = next(q for q in frozen_one.questions if q.split == "dev")

    second = _version(tmp_path, "v0.2", previous_version="v0.1")
    corrected = old.model_copy(
        update={
            "id": f"{old.id}-b",
            "replaces": old.id,
            "split": None,
            "reference_answer": "TEST-ONLY corrected reference",
        }
    )
    kept = [q.model_copy(update={"split": None}) for q in frozen_one.questions if q.id != old.id]
    write_questions(second, [*kept, corrected])
    two = read_dataset(second)
    assert check_dataset(
        two.manifest,
        two.questions,
        two.corpus,
        freeze=None,
        previous_freeze=two.previous_freeze,
        allow_placeholders=True,
    ).ok
    frozen_two = freeze_dataset(
        two.manifest,
        two.questions,
        two.corpus,
        previous_freeze=two.previous_freeze,
        frozen_on=TODAY,
        allow_placeholders=True,
    )
    splits = {q.id: q.split for q in frozen_two.questions}
    assert splits[corrected.id] == "dev"
    assert all(splits[q.id] == q.split for q in frozen_one.questions if q.id != old.id)
