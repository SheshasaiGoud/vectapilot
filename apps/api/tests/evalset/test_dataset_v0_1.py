"""M2 PR 7: the committed eval v0 dataset (evals/datasets/support_qa/v0.1).

This is the test that keeps CI honest as questions arrive. While the manifest says `collecting`,
every row must be valid (zero rows are); once `freeze` sets `frozen`, the requirements and every
pin are enforced here too (`checks.py` explains the switch).
"""

import re
from typing import Final

from tests.evalset.helpers import DATASET_DIR, REPO_ROOT, real_corpus
from vectapilot_api.evalset.checks import check_dataset
from vectapilot_api.evalset.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.evalset.questions import (
    CATEGORIES,
    CATEGORY_PRECEDENCE,
    SUBTYPES,
    readiness_problems,
)

DATASET: Final = read_dataset(DATASET_DIR)
README: Final = (DATASET_DIR / "README.md").read_text("utf-8")
GUIDE: Final = (REPO_ROOT / "docs" / "learning" / "m2-eval-questions.md").read_text("utf-8")
BRIEF: Final = (DATASET_DIR / "friends-brief.md").read_text("utf-8")
ADR: Final = (REPO_ROOT / "docs" / "adr" / "0012-eval-dataset-v0.md").read_text("utf-8")


def test_the_default_dataset_is_this_version() -> None:
    assert REPO_ROOT / DEFAULT_DATASET == DATASET_DIR


def test_the_manifest_records_the_decided_rules() -> None:
    manifest = DATASET.manifest
    assert (manifest.dataset, manifest.version, manifest.synthetic) == ("support_qa", "v0.1", True)
    assert manifest.businesses == ("sunrise-clinic", "paws-and-co")
    assert manifest.corpus == "data/sample"
    # Decided 2026-10-21, items 1 and 5: 40+ questions, about 10 Hindi or Hinglish, about 10 by
    # friends who have not seen the documents. Lowering one is a decision, not an edit.
    requirements = manifest.requirements
    assert (requirements.min_questions, requirements.min_hindi, requirements.min_friend_blind) == (
        40,
        10,
        10,
    )
    assert (requirements.min_per_category, requirements.min_per_business) == (3, 15)
    # Decided 2026-10-22, item 6: 3+ safety questions per business, 8+ dont_know questions.
    assert (requirements.min_safety_per_business, requirements.min_no_answer) == (3, 8)
    # The split rule is fixed before any question exists; changing it after a freeze is refused.
    rule = manifest.split_rule
    assert (rule.rule, rule.seed, rule.test_percent) == (
        "stratified-systematic-v2",
        "support_qa",
        30,
    )
    assert manifest.previous_version is None


def test_the_readme_and_the_guide_name_the_recorded_split_rule() -> None:
    # v1 was replaced before any question existed (kenji's review, K1); the docs follow.
    rule = DATASET.manifest.split_rule.rule
    assert f"`{rule}`" in README
    assert f"`{rule}`" in GUIDE
    assert "systematic-v1" not in GUIDE


def test_the_committed_dataset_passes_the_checks_for_its_stage() -> None:
    result = check_dataset(
        DATASET.manifest, DATASET.questions, DATASET.corpus, freeze=DATASET.freeze
    )
    assert result.ok, "\n".join(result.problems)


def test_the_stage_and_the_files_agree() -> None:
    if DATASET.manifest.stage == "collecting":
        assert DATASET.freeze is None
        assert all(q.split is None for q in DATASET.questions)
    else:
        assert DATASET.freeze is not None
        assert readiness_problems(DATASET.manifest, DATASET.questions, DATASET.corpus) == []


def test_the_corpus_is_the_one_the_manifest_names() -> None:
    assert DATASET.corpus.file_hashes() == real_corpus().file_hashes()


def test_the_readme_and_the_guide_define_every_category_as_the_code_does() -> None:
    for category, definition in CATEGORIES.items():
        assert f"| `{category}` | {definition} |" in README, category
        assert f"`{category}`" in GUIDE, category
    precedence = " > ".join(f"`{c}`" for c in CATEGORY_PRECEDENCE)
    assert precedence in README
    assert precedence in GUIDE
    for category, subtypes in SUBTYPES.items():
        for subtype, definition in subtypes.items():
            assert f"| `{category}` | `{subtype}` | {definition} |" in README, subtype
            assert f"`{subtype}`" in GUIDE, subtype


def test_the_docs_state_the_no_answer_minimum_in_the_same_words() -> None:
    # The minimum is `min_no_answer` dont_know rows, and a dont_know row has no evidence.
    phrase = f"{DATASET.manifest.requirements.min_no_answer} dont_know questions with no evidence"
    for name, text in (("README", README), ("guide", GUIDE), ("ADR 0012", ADR)):
        assert phrase in " ".join(text.split()), name


def test_the_brief_keeps_self_harm_for_shesha_and_hindi_for_sunrise() -> None:
    # Decided 2026-10-22, items 6 and 8: friends write everyday emergencies but nothing about
    # self-harm, and are invited to write Hindi to Sunrise, the business that lists it.
    assert "nothing about\n  self-harm" in BRIEF
    assert "no real health details" in BRIEF
    paws = BRIEF.split("**Paws & Co**")[1].split("\n\n")[0]
    assert "Hindi" not in paws
    assert "**for Paws & Co,\nwrite in English**" in BRIEF


def test_the_friends_brief_shows_no_prices_timings_or_names_from_the_documents() -> None:
    # Default until shesha decides otherwise: friends write blind, so the brief describes each
    # business like its signboard would and gives no fact a question could copy.
    assert not re.search(r"\b\d{1,2}:\d{2}\b|INR|₹|\brs\b|\d{3,}", BRIEF, re.IGNORECASE)
    names = {"Testwala", "Samplekar", "Demoreddy", "Mockwal", "Mockford", "Sampleton", "Testudo"}
    assert not any(name in BRIEF for name in names)
    corpus_text = " ".join(
        d.loaded.text for b in DATASET.manifest.businesses for d in DATASET.corpus.documents(b)
    )
    assert all(name in corpus_text for name in names)  # the list above is still the corpus's
