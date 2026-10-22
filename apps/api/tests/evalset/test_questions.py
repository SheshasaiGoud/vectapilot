"""M2 PR 7: the question schema, row checks, readiness, the coverage report and labels.

Every row here is a made-up placeholder (helpers.py): the shape is tested, never a question.
"""

import json
from typing import Any, Final

import pytest
from pydantic import ValidationError

from tests.evalset.helpers import (
    PAWS_CANCEL,
    SUNRISE_FEE,
    SUNRISE_HOURS,
    manifest,
    one_per_category,
    question,
    real_corpus,
    row,
)
from vectapilot_api.evalset.questions import (
    CATEGORIES,
    CATEGORY_PRECEDENCE,
    REQUIRED_CATEGORIES,
    RESERVED_CATEGORIES,
    SUBTYPES,
    DatasetError,
    EvalQuestion,
    SourceKey,
    coverage,
    dump_question,
    is_placeholder_text,
    load_labels,
    load_manifest,
    load_questions,
    question_problems,
    readiness_problems,
    render_coverage,
    resolve_expected,
    split_group_warnings,
    verbatim_warnings,
)
from vectapilot_api.ingestion.chunking import DEFAULT_CHUNKING

CANARY: Final = "canary-question-text-9c1e"


# ── The row schema ────────────────────────────────────────────────────────────────────────────


def test_a_valid_row_parses_and_defaults_are_explicit() -> None:
    parsed = question()
    assert (parsed.code_mixed, parsed.split, parsed.reference_answer, parsed.notes) == (
        False,
        None,
        None,
        None,
    )
    assert (parsed.split_group, parsed.subtype, parsed.replaces) == (None, None, None)
    assert parsed.language_group == "en"
    assert question(language="hi", code_mixed=True).language_group == "hi-code-mixed"
    assert [where for where, _ in parsed.evidence_keys()] == ["evidence group 1 key 1"]


NO_EVIDENCE: Final[dict[str, Any]] = {"expected_evidence": []}


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"category": "action"}, "reserved until M5"),
        ({"category": "document"}, "reserved until M6"),
        ({"code_mixed": True}, "code-mixed"),
        ({"author_type": "friend_blind"}, "without seeing the documents"),
        ({"expected_evidence": [[SUNRISE_HOURS, SUNRISE_HOURS]]}, "listed once, in one group"),
        ({"expected_evidence": [[SUNRISE_HOURS], [SUNRISE_HOURS]]}, "listed once, in one group"),
        (NO_EVIDENCE, "an answered question has at least one evidence group"),
        ({"expected_outcome": "dont_know"}, "a dont_know question has no evidence"),
        ({"category": "out_of_scope", "expected_outcome": "dont_know"}, "out_of_scope"),
        ({"category": "out_of_scope"}, "out_of_scope"),  # answered, with evidence
        ({"category": "out_of_scope", **NO_EVIDENCE}, "out_of_scope"),  # answered, without
        ({"category": "safety"}, "safety: the expected outcome is handoff"),
        ({"category": "handoff_request"}, "handoff_request: the expected outcome is handoff"),
        ({"category": "ambiguous", "expected_outcome": "handoff"}, "ambiguous: the expected"),
        ({"category": "multi_hop"}, "at least two evidence groups"),
        # Two keys in one group are one piece of evidence, found by either: still one group.
        (
            {"category": "multi_hop", "expected_evidence": [[SUNRISE_HOURS, SUNRISE_FEE]]},
            "at least two evidence groups",
        ),
        ({"subtype": "emergency"}, r"subtype: faq takes none"),
        (
            {"category": "safety", "expected_outcome": "handoff", "subtype": "off_domain"},
            r"subtype: safety takes one of \['emergency', 'professional_judgement'\]",
        ),
    ],
)
def test_each_row_rule_is_enforced(overrides: dict[str, Any], rule: str) -> None:
    with pytest.raises(ValidationError, match=rule):
        question(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        # handoff: evidence is optional (what the reply points to), both ways.
        {"category": "safety", "expected_outcome": "handoff", "subtype": "emergency"},
        {"category": "safety", "expected_outcome": "handoff", **NO_EVIDENCE},
        {"category": "handoff_request", "expected_outcome": "handoff", **NO_EVIDENCE},
        {"category": "out_of_scope", "expected_outcome": "dont_know", **NO_EVIDENCE},
        {
            "category": "out_of_scope",
            "expected_outcome": "handoff",
            "subtype": "non_public_only",
            **NO_EVIDENCE,
        },
        {"category": "ambiguous", "expected_evidence": [[SUNRISE_HOURS], [SUNRISE_FEE]]},
        {"category": "multi_hop", "expected_evidence": [[SUNRISE_HOURS], [SUNRISE_FEE]]},
        {"expected_evidence": [[SUNRISE_HOURS, SUNRISE_FEE]]},  # interchangeable keys
        {"split_group": "example-twins"},
    ],
)
def test_the_decided_shapes_are_accepted(overrides: dict[str, Any]) -> None:
    question(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"language": "fr"},
        {"category": "smalltalk"},
        {"author_type": "claude"},
        {"author_type": "shesha"},  # renamed to `team` before the first row (2026-10-22, item 7)
        {"subtype": "urgent", "category": "safety", "expected_outcome": "handoff"},
        {"split_group": "Not A Slug"},
        {"group": "example-g"},  # renamed to split_group (2026-10-22), not an evidence group
        {"expected_sources": [SUNRISE_HOURS]},  # the flat list, replaced by evidence groups
        {"expected_evidence": [SUNRISE_HOURS]},  # a key where a group belongs
        {"expected_evidence": [[]]},  # an empty group
        {"expected_outcome": "maybe"},
        {"split": "train"},
        {"synthetic": False},
        {"id": "Has Spaces"},
        {"business_id": "x"},
        {"question": ""},
        {"question": "x" * 2001},
        {"unknown_field": 1},
        {"expected_evidence": [[{"source_id": "faq", "version": "1", "anchor": "too shor"[:7]}]]},
        {"expected_evidence": [[{"source_id": "faq", "version": "1", "anchor": "x" * 101}]]},
    ],
)
def test_values_outside_the_schema_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        question(**overrides)


def test_an_anchor_is_stored_normalized_and_must_stay_long_enough() -> None:
    key = SourceKey(source_id="faq", version="1", anchor="  free up\n to 24 hours ")
    assert key.anchor == "free up to 24 hours"
    with pytest.raises(ValidationError):
        SourceKey(source_id="faq", version="1", anchor="a  b  c  d")  # 7 characters once collapsed


def test_every_category_has_a_definition_and_two_are_reserved() -> None:
    assert set(CATEGORIES) == {*REQUIRED_CATEGORIES, *RESERVED_CATEGORIES}
    assert set(RESERVED_CATEGORIES) == {"action", "document"}
    assert len(REQUIRED_CATEGORIES) == 8


def test_one_precedence_order_covers_the_eight_categories() -> None:
    # Decided 2026-10-22, item 5: when two categories fit, the earlier one wins.
    assert CATEGORY_PRECEDENCE == (
        "safety",
        "handoff_request",
        "adversarial",
        "out_of_scope",
        "ambiguous",
        "multi_hop",
        "policy",
        "faq",
    )
    assert sorted(CATEGORY_PRECEDENCE) == sorted(REQUIRED_CATEGORIES)


def test_only_safety_and_out_of_scope_take_a_subtype() -> None:
    assert {c: tuple(t) for c, t in SUBTYPES.items()} == {
        "safety": ("emergency", "professional_judgement"),
        "out_of_scope": ("off_domain", "unanswerable", "non_public_only"),
    }


# ── Loading ───────────────────────────────────────────────────────────────────────────────────


def test_an_empty_file_is_zero_questions_and_blank_lines_are_skipped() -> None:
    assert load_questions("") == ()
    text = "\n" + json.dumps(row()) + "\n\n"
    assert [q.id for q in load_questions(text)] == ["example-1"]


def test_every_bad_line_is_named_without_its_text() -> None:
    good = json.dumps(row())
    bad_json = '{"question": "' + CANARY
    bad_rule = json.dumps(row(id="example-2", question=CANARY, category="safety"))
    with pytest.raises(DatasetError) as caught:
        load_questions("\n".join([good, bad_json, bad_rule]))
    message = str(caught.value)
    assert "questions.jsonl line 2 is not a valid question" in message
    assert "questions.jsonl line 3 is not a valid question: (row) (Value error, safety" in message
    assert CANARY not in message


def test_a_bad_manifest_names_its_fields() -> None:
    with pytest.raises(DatasetError, match=r"manifest.json is not a valid manifest: .*stage"):
        load_manifest(json.dumps({"dataset": "support_qa", "stage": "done"}))


def test_dump_question_round_trips_and_keeps_hindi_readable() -> None:
    original = question(language="hi", question="TEST-ONLY नमस्ते")
    line = dump_question(original)
    assert "नमस्ते" in line
    assert load_questions(line) == (original,)


# ── Row checks against the real corpus ────────────────────────────────────────────────────────


def problems(
    rows: list[EvalQuestion], *, allow: bool = True, **manifest_overrides: Any
) -> list[str]:
    return question_problems(
        manifest(**manifest_overrides), rows, real_corpus(), allow_placeholders=allow
    )


def test_valid_placeholder_rows_pass_when_placeholders_are_allowed() -> None:
    assert problems(one_per_category()) == []


def test_the_real_dataset_refuses_placeholder_rows() -> None:
    second = question(id="sunrise-001", question="plain words", notes="EXAMPLE-ONLY note")
    found = problems([question(), second], allow=False)
    assert found == [
        "question example-1: is a placeholder (a marker such as EXAMPLE-ONLY)",
        "question sunrise-001: is a placeholder (a marker such as EXAMPLE-ONLY)",
    ]
    anchor = {**SUNRISE_HOURS, "anchor": "EXAMPLE-ONLY quote"}
    plain = question(id="sunrise-002", question="plain", expected_evidence=[[anchor]])
    assert "is a placeholder" in problems([plain], allow=False)[0]


@pytest.mark.parametrize(
    ("question_id", "placeholder"),
    [
        ("example", True),
        ("example-1", True),
        ("example-faq-2", True),
        ("examples-1", False),  # the rule is `example` or `example-...`, nothing longer
        ("sunrise-example-1", False),  # anchored at the start
        ("sunrise-001", False),
    ],
)
def test_an_example_id_is_a_placeholder(question_id: str, placeholder: bool) -> None:
    # Mutation audit M5e: the id rule on its own, with plain text everywhere else.
    row = question(id=question_id, question="plain words")
    assert bool(problems([row], allow=False)) is placeholder


@pytest.mark.parametrize("field", ["question", "notes", "reference_answer", "anchor"])
@pytest.mark.parametrize("marker", ["example-only", "test-only"])
def test_a_lowercase_marker_in_any_text_is_a_placeholder(field: str, marker: str) -> None:
    # Mutation audit M5f: markers are matched without regard to case, in every free text.
    text = f"plain {marker} words"
    if field == "anchor":
        row = question(
            id="sunrise-001",
            question="plain",
            expected_evidence=[[{**SUNRISE_HOURS, "anchor": text}]],
        )
    else:
        row = question(id="sunrise-001", **{"question": "plain", field: text})
    assert "is a placeholder" in problems([row], allow=False)[0]


FULL_WIDTH_EXAMPLE_ONLY: Final = (
    "\uff25\uff38\uff21\uff2d\uff30\uff2c\uff25-\uff2f\uff2e\uff2c\uff39"
)
FULL_WIDTH_TEST_UNDERSCORE_ONLY: Final = "\uff34\uff25\uff33\uff34\uff3f\uff2f\uff2e\uff2c\uff39"


@pytest.mark.parametrize(
    "text",
    [
        "EXAMPLE-ONLY",
        "example only",
        "Example_Only",
        "EXAMPLEONLY",
        "example -- only",
        FULL_WIDTH_EXAMPLE_ONLY,  # NFKC folds full-width letters
        "TEST-ONLY",
        "test_only",
        "Test-Only",
        "test--only",
        "TEST\u2011ONLY",  # a non-breaking hyphen
        "test\u2014only",  # an em dash
        "test\u2212only",  # a minus sign
        FULL_WIDTH_TEST_UNDERSCORE_ONLY,  # a full-width underscore, folded by NFKC
        "plain words, then TEST-ONLY at the end",
    ],
)
def test_a_marker_in_any_spelling_is_refused(text: str) -> None:
    # Mutation audit D5b, as decided 2026-10-22: `example[\\W_]*only` and a hyphen- or
    # underscore-joined `test...only`, on NFKC-casefolded text.
    assert is_placeholder_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "do you do the blood test only on weekdays",  # a real question passes
        "blood test only",
        "BLOOD TEST ONLY?",
        "test - only",  # spaces around the dash: words, not a marker
        "testonly",
        "testing only",
        "an example of only one thing",  # a word in between
        "plain words",
    ],
)
def test_real_wording_is_not_a_marker(text: str) -> None:
    assert not is_placeholder_text(text)
    row = question(id="sunrise-001", question=text, expected_evidence=[[SUNRISE_HOURS]])
    assert problems([row], allow=False) == []


REPEAT: Final = (
    "question(s) repeat another question's text (the same text in two businesses needs one "
    "shared `split_group`)"
)


def test_ids_and_texts_must_not_repeat() -> None:
    rows = [question(), question(), question(id="example-2", question="test-only  QUESTION 1")]
    assert problems(rows) == ["question id example-1 is used 2 times", f"2 {REPEAT}"]


def test_the_same_text_in_two_businesses_needs_one_shared_group() -> None:
    # Decided 2026-10-22, item 4: cross-business twins share a group (and so a split).
    def twin(question_id: str, business: str, group: str | None) -> EvalQuestion:
        evidence = [[SUNRISE_HOURS]] if business == "sunrise-clinic" else [[PAWS_CANCEL]]
        return question(
            id=question_id, business_id=business, split_group=group, expected_evidence=evidence
        )

    paired = [
        twin("example-1", "sunrise-clinic", "example-g"),
        twin("example-2", "paws-and-co", "example-g"),
    ]
    assert problems(paired) == []
    ungrouped = [twin("example-1", "sunrise-clinic", None), twin("example-2", "paws-and-co", None)]
    assert problems(ungrouped) == [f"1 {REPEAT}"]
    other_groups = [
        twin("example-1", "sunrise-clinic", "example-g"),
        twin("example-2", "paws-and-co", "example-h"),
    ]
    assert problems(other_groups) == [f"1 {REPEAT}"]
    same_business = [
        twin("example-1", "sunrise-clinic", "example-g"),
        twin("example-2", "sunrise-clinic", "example-g"),
    ]
    assert problems(same_business) == [f"1 {REPEAT}"]


def test_a_question_in_a_language_its_business_does_not_list_expects_handoff() -> None:
    # Decided 2026-10-22, item 6. Paws & Co lists en and te; Sunrise lists en and hi.
    hindi_to_paws = question(
        business_id="paws-and-co", language="hi", expected_evidence=[[PAWS_CANCEL]]
    )
    assert problems([hindi_to_paws]) == [
        "question example-1: its business does not list hi, so the expected outcome is handoff"
    ]
    assert problems([hindi_to_paws.model_copy(update={"expected_outcome": "handoff"})]) == []
    assert problems([question(language="hi")]) == []  # Sunrise lists hi


def test_the_business_must_be_in_the_manifest_and_have_a_corpus() -> None:
    assert problems([question(business_id="northwind-shop")]) == [
        "question example-1: its business is not in the dataset manifest"
    ]
    assert problems(
        [question(business_id="northwind-shop")],
        businesses=["sunrise-clinic", "northwind-shop"],
    ) == ["question example-1: its business has no corpus under data/sample"]


def test_a_split_is_never_set_by_hand_while_collecting() -> None:
    assert problems([question(split="test")]) == [
        "question example-1: split is set by `freeze`, never by hand (leave it null)"
    ]
    assert problems([question(split="test")], stage="frozen") == []


@pytest.mark.parametrize(
    ("key", "problem"),
    [
        (
            {"source_id": "fees", "version": "2026-07", "anchor": "Consultation (dog or cat)"},
            "its source_id and version are not in this business's corpus",
        ),
        (
            # Mutation audit M3d: a source_id that differs only in case is another document.
            {**SUNRISE_HOURS, "source_id": "FAQ"},
            "its source_id and version are not in this business's corpus",
        ),
        (
            # Mutation audit M3a: an anchor that differs only in case is not the document's text.
            {**SUNRISE_HOURS, "anchor": "saturday: 9:00 to 14:00"},
            "its anchor is not in the document's loaded text",
        ),
        (
            # Sunrise's own fees document, with a quote that is only in Paws & Co's: isolation.
            {"source_id": "fees", "version": "2026-04", "anchor": "Consultation (dog or cat)"},
            "its anchor is not in the document's loaded text",
        ),
        (
            {"source_id": "fees", "version": "2025-10", "anchor": "(general physician): INR 540"},
            "a document a customer's answer may never use",
        ),
        (
            {"source_id": "staff-handbook", "version": "2026-10", "anchor": "SUNRISE-STAFF-15"},
            "a document a customer's answer may never use",
        ),
        (
            {"source_id": "faq", "version": "2026-10", "anchor": "Monday to Friday"},
            "its anchor occurs 3 times in the document; quote more",  # hours + two doctors
        ),
    ],
)
def test_an_expected_source_that_does_not_resolve_is_a_problem(
    key: dict[str, str], problem: str
) -> None:
    found = problems([question(expected_evidence=[[SUNRISE_FEE, key]])])
    assert len(found) == 1
    assert found[0].startswith(f"question example-1: evidence group 1 key 2 ({key['source_id']}): ")
    assert problem in found[0]


# Obviously fake values in the shapes the screen refuses (zara's review, Z1).
FAKE_PHONE_RUN: Final = "123-456-7890"
FAKE_AADHAAR: Final = "1234 5678 9012"
FAKE_EMAIL: Final = "test-only@invalid.test"


@pytest.mark.parametrize(
    ("field", "value", "finding"),
    [
        ("question", f"TEST-ONLY call {FAKE_PHONE_RUN}", "a phone-like number"),
        ("notes", f"TEST-ONLY {FAKE_AADHAAR}", "an Aadhaar-shaped number"),
        ("reference_answer", f"TEST-ONLY {FAKE_EMAIL}", "an e-mail address outside .example"),
    ],
)
def test_personal_data_is_refused_naming_the_field_never_the_text(
    field: str, value: str, finding: str
) -> None:
    found = problems([question(**{field: value})])
    assert len(found) == 1
    assert found[0].startswith(f"question example-1: {field} holds what looks like personal data")
    assert finding in found[0]
    assert value.split()[-1] not in found[0]


def test_the_fake_forms_pass_the_screen() -> None:
    text = (
        "TEST-ONLY +91 00000 12345, 112, 2026-10-22, desk@sunrise-clinic.example, "
        "booking 12345678, INR 1,200"
    )
    assert problems([question(notes=text, reference_answer=text)]) == []


def test_a_draft_document_is_never_evidence() -> None:
    key = {"source_id": "holiday-hours", "version": "2026-12", "anchor": "31 December: vet clinic"}
    found = problems([question(business_id="paws-and-co", expected_evidence=[[key]])])
    assert "may never use" in found[0]


def test_resolve_expected_keeps_groups_and_each_keys_chunks_apart() -> None:
    corpus = real_corpus()
    row = question(category="multi_hop", expected_evidence=[[SUNRISE_HOURS], [SUNRISE_FEE]])
    resolved = resolve_expected(corpus, row)
    assert [[r.key.source_id for r in group] for group in resolved] == [["faq"], ["fees"]]
    assert all(r.chunk_indexes for group in resolved for r in group)
    fees = next(
        d for d in corpus.documents("sunrise-clinic") if d.source.key == ("fees", "2026-04")
    )
    assert corpus.chunks(fees, DEFAULT_CHUNKING)[resolved[1][0].chunk_indexes[0]].page == 1


# ── Readiness ─────────────────────────────────────────────────────────────────────────────────


def test_readiness_lists_everything_missing_from_an_empty_dataset() -> None:
    assert readiness_problems(manifest(), [], real_corpus()) == [
        "questions: 0 of at least 40",
        "Hindi or Hinglish questions to a business that lists hi: 0 of at least 10",
        "friend_blind questions: 0 of at least 10",
        "dont_know questions with no evidence: 0 of at least 8",
        "category faq: 0 of at least 3",
        "category policy: 0 of at least 3",
        "category multi_hop: 0 of at least 3",
        "category ambiguous: 0 of at least 3",
        "category out_of_scope: 0 of at least 3",
        "category adversarial: 0 of at least 3",
        "category safety: 0 of at least 3",
        "category handoff_request: 0 of at least 3",
        "business sunrise-clinic: 0 of at least 15",
        "business sunrise-clinic safety questions: 0 of at least 3",
        "business paws-and-co: 0 of at least 15",
        "business paws-and-co safety questions: 0 of at least 3",
    ]


SMALL: Final = {
    "min_questions": 8,
    "min_hindi": 0,
    "min_friend_blind": 0,
    "min_per_category": 1,
    "min_per_business": 4,
    "min_safety_per_business": 0,
    "min_no_answer": 2,
}


def ready(rows: list[EvalQuestion], **requirements: int) -> list[str]:
    return readiness_problems(manifest(requirements={**SMALL, **requirements}), rows, real_corpus())


def test_readiness_passes_when_every_minimum_is_met() -> None:
    rows = one_per_category()  # 4 per business; dont_know: out_of_scope and adversarial
    assert ready(rows) == []
    assert ready(rows, min_per_business=5) == [
        "business sunrise-clinic: 4 of at least 5",
        "business paws-and-co: 4 of at least 5",
    ]
    assert ready(rows, min_no_answer=3) == ["dont_know questions with no evidence: 2 of at least 3"]
    assert ready(rows, min_safety_per_business=1) == [
        "business paws-and-co safety questions: 0 of at least 1"  # the safety row is Sunrise's
    ]


def test_only_hindi_to_a_business_that_lists_hindi_counts() -> None:
    # Decided 2026-10-22, item 6: a Hindi question to Paws & Co (en, te) tests the hand-off
    # path and does not count towards the Hindi minimum.
    sunrise = question(id="example-hi-1", language="hi", code_mixed=True)
    paws = question(
        id="example-hi-2",
        business_id="paws-and-co",
        language="hi",
        expected_outcome="handoff",
        expected_evidence=[],
    )
    assert ready([*one_per_category(), sunrise, paws], min_hindi=1) == []
    assert ready([*one_per_category(), sunrise, paws], min_hindi=2) == [
        "Hindi or Hinglish questions to a business that lists hi: 1 of at least 2"
    ]


# ── Warnings and the coverage report ──────────────────────────────────────────────────────────


def test_a_question_that_copies_eight_words_from_its_documents_is_flagged() -> None:
    # Plain quotes, not questions (zara's review, Z9): eight words in a row from Sunrise's
    # appointment policy, and seven.
    copied = question(
        question="TEST-ONLY verbatim probe: move an appointment as often as you like within"
    )
    seven = question(
        id="example-2", question="TEST-ONLY verbatim probe: move an appointment as often as you"
    )
    other = question(
        id="example-3",
        business_id="paws-and-co",
        question=copied.question,
        expected_evidence=[[PAWS_CANCEL]],
    )
    assert verbatim_warnings(real_corpus(), [copied, seven, other]) == [
        "question example-1: copies 8+ words in a row from appointment-policy 2026-10"
    ]


def test_hindi_words_are_whole_words_for_the_verbatim_warning() -> None:
    # A plain quote: eight words in a row from the Hindi FAQ's cancellation section (with its
    # vowel signs).
    copied = question(
        language="hi",
        question="TEST-ONLY verbatim probe: अपॉइंटमेंट से कम से कम 24 घंटे पहले रद्द करने पर",
    )
    assert verbatim_warnings(real_corpus(), [copied]) == [
        "question example-1: copies 8+ words in a row from faq-hi 2026-10"
    ]
    # Seven whole words: no warning (a plain `\w+` split makes 11 fragments of them, and warned).
    seven = question(
        id="example-2", language="hi", question="TEST-ONLY रद्द करने पर कोई शुल्क नहीं लगता"
    )
    assert verbatim_warnings(real_corpus(), [seven]) == []


def test_coverage_counts_every_dimension() -> None:
    rows = [
        *one_per_category(),
        question(id="example-hi", language="hi", code_mixed=True, split="dev"),
        question(
            id="example-paws-hi",
            business_id="paws-and-co",
            language="hi",
            expected_outcome="handoff",
            expected_evidence=[[PAWS_CANCEL]],
            reference_answer="TEST-ONLY",
        ),
        question(
            id="example-friend", author_type="friend_blind", saw_documents=False, split="test"
        ),
        question(
            id="example-urgent",
            category="safety",
            subtype="emergency",
            expected_outcome="handoff",
            expected_evidence=[],
        ),
    ]
    report = coverage(rows, real_corpus())
    assert report.total == 12
    assert report.by_business == {"sunrise-clinic": 7, "paws-and-co": 5}  # 4 + 3, 4 + 1
    assert report.by_category["faq"] == 4
    assert report.by_language == {"en": 10, "hi-code-mixed": 1, "hi": 1}
    assert report.by_author == {"team": 11, "friend_blind": 1}
    assert report.by_split == {"unassigned": 10, "dev": 1, "test": 1}
    assert report.by_outcome == {"answered": 6, "dont_know": 2, "handoff": 4}
    assert report.by_subtype == {("safety", "emergency"): 1}
    assert report.by_category_and_split[("faq", "test")] == 1
    assert report.by_author_and_split[("friend_blind", "test")] == 1
    assert report.by_language_and_split[("hi-code-mixed", "dev")] == 1
    assert report.by_business_and_split == {
        ("sunrise-clinic", "unassigned"): 5,
        ("sunrise-clinic", "dev"): 1,
        ("sunrise-clinic", "test"): 1,
        ("paws-and-co", "unassigned"): 5,
    }
    assert report.by_business_and_category[("paws-and-co", "faq")] == 1
    assert report.unsupported_language == 1  # Hindi to Paws & Co, which lists en and te
    assert report.with_reference_answer == 1


def test_the_rendered_report_lists_every_category_and_business_even_at_zero() -> None:
    text = render_coverage(manifest(), coverage([], real_corpus()))
    assert text.startswith("support_qa v0.1 (collecting, synthetic): 0 questions\n")
    for name in [*CATEGORIES, "sunrise-clinic", "paws-and-co", "team", "friend_blind"]:
        assert name in text
    assert "\nsubtype (optional)\n  safety: emergency" in text
    assert "  out_of_scope: non_public_only      0\n" in text  # padded to 31, then 4
    assert "  action                      0    0    0          0 (reserved)" in text
    for title in ("business", "category", "language", "author_type"):
        assert f"\n{title:<26}  all  dev test unassigned\n" in text, title


def test_a_group_with_one_question_is_worth_a_warning() -> None:
    rows = [
        question(id="example-1", split_group="example-pair"),
        question(id="example-2", question="TEST-ONLY 2", split_group="example-pair"),
        question(id="example-3", question="TEST-ONLY 3", split_group="example-lone"),
    ]
    assert split_group_warnings(rows) == ["split_group example-lone has only one question"]


# ── Correctness labels ────────────────────────────────────────────────────────────────────────


def label(**overrides: Any) -> str:
    data = {
        "question_id": "example-1",
        "run_id": "2026-10-30T10:00:00Z-live",
        "label": "correct",
        "labelled_by": "shesha",
        "labelled_on": "2026-10-30",
    }
    return json.dumps({**data, **overrides})


def test_labels_load_one_per_question_and_run() -> None:
    rows = [question(), question(id="example-2", question="TEST-ONLY 2")]
    text = "\n".join([label(), label(question_id="example-2", label="incorrect"), ""])
    assert [x.label for x in load_labels(text, rows)] == ["correct", "incorrect"]
    with pytest.raises(DatasetError, match="its question_id is not in the dataset"):
        load_labels(label(question_id="example-9"), rows)
    with pytest.raises(DatasetError, match="one label per question and run"):
        load_labels("\n".join([label(), label(label="incorrect")]), rows)
    with pytest.raises(DatasetError, match=r"labels line 1 is not a valid label: label"):
        load_labels(label(label="partly"), rows)
    assert load_labels(label(labelled_by="pilot-staff-1"), rows)[0].labelled_by == "pilot-staff-1"
    with pytest.raises(DatasetError, match="labelled_by"):
        load_labels(label(labelled_by="Not A Slug"), rows)
