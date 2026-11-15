"""M6 PR 4: the stage-aware check of `doc_extraction` (`extracteval/checks.py`).

Each test builds a throwaway repository (`helpers.make_repo`) with TEST-ONLY documents. Problems
name files, ids, fields, pages and rules: the canary planted in lines and values never reaches one.
"""

import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from tests.extracteval.helpers import (
    COMMITTED,
    PAWS,
    REFERRAL,
    REFERRAL_FIELDS,
    SUNRISE,
    TABLE_AND_IMAGE,
    VACCINATION,
    VACCINATION_FIELDS,
    add_document,
    check,
    gold,
    make_repo,
    read,
    small_repo,
)
from tests.extracteval.pdfs import pdf_of_text
from tests.ingestion.pdf_builder import build_pdf
from vectapilot_api.extracteval.checks import (
    PLACEHOLDER_WORD,
    check_dataset,
    minimums,
    pdf_problems,
    slot_problems,
)
from vectapilot_api.extracteval.files import read_dataset
from vectapilot_api.extracteval.gold import DOCUMENTS_DIR, load_manifest
from vectapilot_api.extracteval.source import (
    SourceDocument,
    SourceLine,
    SourcePage,
    parse_source,
)

CANARY: Final = "Canary Quillon"


def problems(dataset_dir: Path) -> tuple[str, ...]:
    found = check(dataset_dir).problems
    assert not any(CANARY in p for p in found)
    return found


def has(found: tuple[str, ...], *parts: str) -> bool:
    return any(all(part in problem for part in parts) for problem in found)


# ── Slots ─────────────────────────────────────────────────────────────────────────────────────


def test_the_committed_slots_meet_d8_with_a_dev_and_a_test_slot_per_tag() -> None:
    manifest = read_dataset(COMMITTED).manifest
    assert slot_problems(manifest) == []
    for business_id in manifest.businesses:
        mine = [s for s in manifest.slots if s.business_id == business_id]
        assert [s.split for s in mine].count("dev") == 6 == [s.split for s in mine].count("test")


def _slots_manifest(slots: list[dict[str, Any]]) -> Any:
    data = json.loads((COMMITTED / "manifest.json").read_text("utf-8"))
    return load_manifest(json.dumps(data | {"slots": slots}))


def test_slots_that_cannot_meet_d8_are_problems() -> None:
    slots: list[dict[str, Any]] = json.loads((COMMITTED / "manifest.json").read_text("utf-8"))[
        "slots"
    ]
    # sunrise-11 (table, dev) moved to test: table has no dev slot, and 5 dev / 7 test.
    moved = [s | {"split": "test"} if s["id"] == "sunrise-11" else s for s in slots]
    found = slot_problems(_slots_manifest(moved))
    assert "slots of business sunrise-clinic: 5 dev and 7 test, not 50/50" in found
    assert "slots of business sunrise-clinic: table needs a dev and a test slot" in found
    # The other way (mutation audit CS02, CS03): sunrise-02 (absent, test) moved to dev leaves
    # 7 dev / 5 test and absent with no test slot.
    moved = [s | {"split": "dev"} if s["id"] == "sunrise-02" else s for s in slots]
    found = slot_problems(_slots_manifest(moved))
    assert "slots of business sunrise-clinic: 7 dev and 5 test, not 50/50" in found
    assert "slots of business sunrise-clinic: absent needs a dev and a test slot" in found
    # One injection slot of paws-and-co dropped: fewer than D8's 2, and fewer slots in all.
    dropped = [s for s in slots if s["id"] != "paws-07"]
    found = slot_problems(_slots_manifest(dropped))
    assert "slots of business paws-and-co: 1 require injection, fewer than 2" in found
    assert "slots of business paws-and-co: 11, fewer than 12" in found
    assert "the manifest has 23 slots, fewer than D8's documents" in found


# ── Valid documents ───────────────────────────────────────────────────────────────────────────


def test_valid_documents_in_their_slots_pass(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL)
    add_document(dataset_dir, gold("p-1", PAWS), VACCINATION)
    add_document(
        dataset_dir, gold("p-2", PAWS, split="test", tags=["table", "image_page"]), TABLE_AND_IMAGE
    )
    assert check(dataset_dir).problems == ()


def test_the_test_only_marker_is_refused_without_the_test_switch(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL)
    found = check_dataset(read(dataset_dir)).problems
    assert has(found, "s-1.pdf.txt: page 1 line 1 is a placeholder")


def test_an_example_id_is_refused(tmp_path: Path) -> None:
    dataset_dir = make_repo(
        tmp_path,
        slots=[
            {"id": "example-1", "business_id": SUNRISE, "tags": [], "split": "dev"},
            {"id": "s-2", "business_id": SUNRISE, "tags": [], "split": "test"},
            {"id": "p-1", "business_id": PAWS, "tags": [], "split": "dev"},
            {"id": "p-2", "business_id": PAWS, "tags": [], "split": "test"},
        ],
        requirements={"min_documents": 1, "min_per_business": 1, "min_tag_per_business": {}},
    )
    add_document(dataset_dir, gold("example-1"), REFERRAL)
    assert has(check_dataset(read(dataset_dir)).problems, "example-1: is a placeholder id")


# ── On its slot ───────────────────────────────────────────────────────────────────────────────


def test_a_document_off_its_slot_is_refused(tmp_path: Path) -> None:
    dataset_dir = make_repo(
        tmp_path,
        slots=[
            {"id": "s-1", "business_id": SUNRISE, "tags": ["confusable"], "split": "dev"},
            {"id": "s-2", "business_id": SUNRISE, "tags": [], "split": "test"},
            {"id": "p-1", "business_id": PAWS, "tags": [], "split": "dev"},
            {"id": "p-2", "business_id": PAWS, "tags": [], "split": "test"},
        ],
        requirements={"min_documents": 1, "min_per_business": 1, "min_tag_per_business": {}},
    )
    add_document(dataset_dir, gold("s-1"), REFERRAL)  # the slot's tag missing
    add_document(dataset_dir, gold("s-2", split="dev"), REFERRAL)  # the other split
    add_document(dataset_dir, gold("p-1"), REFERRAL)  # another business's slot
    add_document(dataset_dir, gold("s-9"), REFERRAL)  # no such slot

    found = problems(dataset_dir)
    assert has(found, "document s-1: its slot requires the tag(s) confusable")
    assert has(found, "document s-2: its slot's split is test")
    assert has(found, "document p-1: its slot is business paws-and-co's")
    assert has(found, "document s-9: no slot has this id")


def test_a_repeated_id_and_a_kind_the_business_does_not_enable(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL)
    add_document(dataset_dir, gold("s-1"), None)
    vaccination = gold("s-2", split="test", kind="vaccination_record", fields=VACCINATION_FIELDS)
    add_document(dataset_dir, vaccination, VACCINATION)

    found = problems(dataset_dir)
    assert "document id s-1 is used more than once" in found
    assert has(found, "document s-2: its business does not enable the kind vaccination_record")


# ── Gold against the template ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("business_id", "field", "value", "expected"),
    [
        (SUNRISE, "date_of_birth", "14/07/1990", "is not an ISO date"),
        (SUNRISE, "date_of_birth", "2026-02-30", "is not a real date"),
        (PAWS, "species", "Dog", "is not one of the business's enum ids"),
        (PAWS, "species", "hamster", "is not one of the business's enum ids"),  # narrowed away
        (SUNRISE, "policy_number", "bc/2026/0417", "does not match the template's identifier"),
        (SUNRISE, "policy_number", "BC/" + "1" * 22, "is longer than the template's 24 characters"),
        (SUNRISE, "patient_name", "A" * 81, "is longer than the template's 80 characters"),
        (SUNRISE, "insurer", "Call 98765 43210", "looks like personal data"),
        (SUNRISE, "insurer", "...", "has no letter or digit"),  # the comparison trims it away
    ],
)
def test_a_gold_value_that_is_not_canonical_for_its_template(
    tmp_path: Path, business_id: str, field: str, value: str, expected: str
) -> None:
    dataset_dir = small_repo(tmp_path)
    default = REFERRAL_FIELDS if business_id == SUNRISE else VACCINATION_FIELDS
    row = gold("s-1" if business_id == SUNRISE else "p-1", business_id)
    row["fields"] = {**default, field: value}
    add_document(dataset_dir, row, REFERRAL if business_id == SUNRISE else VACCINATION)

    found = check(dataset_dir).problems
    assert has(found, f"field {field} {expected}"), found
    assert not any(value in p for p in found)


def test_a_gold_value_exactly_at_the_templates_length_is_kept(tmp_path: Path) -> None:
    # The boundary (mutation audit CG03): 80 characters is patient_name's limit, not over it. The
    # name gets a line of its own (a source line holds at most 85 characters).
    dataset_dir = small_repo(tmp_path)
    name = "A" * 80
    add_document(
        dataset_dir,
        gold("s-1", fields={**REFERRAL_FIELDS, "patient_name": name}),
        REFERRAL.replace("Patient name: Mira Fernhall", f"Patient name:\n{name}"),
    )
    assert check(dataset_dir).problems == ()


def test_gold_names_exactly_the_templates_fields(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    fields = {k: v for k, v in REFERRAL_FIELDS.items() if k != "insurer"} | {"reason": CANARY}
    add_document(dataset_dir, gold("s-1", fields=fields), REFERRAL)

    found = problems(dataset_dir)
    assert has(found, "field insurer is missing (write null")
    assert has(found, "field reason is not in the referral_letter template")


def test_absent_is_tagged_exactly_when_a_required_field_is_null(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    no_dob = {**REFERRAL_FIELDS, "date_of_birth": None}
    add_document(dataset_dir, gold("s-1", fields=no_dob), REFERRAL)  # untagged
    no_insurer = {**REFERRAL_FIELDS, "insurer": None, "policy_number": None}  # not required
    add_document(dataset_dir, gold("s-2", split="test", fields=no_insurer), REFERRAL)
    add_document(dataset_dir, gold("p-1", PAWS, tags=["absent"]), VACCINATION)  # nothing null

    found = problems(dataset_dir)
    assert has(found, "document s-1: required field(s) date_of_birth are null, so it is tagged")
    assert not has(found, "document s-2")
    assert has(found, "document p-1: tagged absent, but every required field has a gold value")


def test_a_gold_value_with_the_word_example_is_refused_without_the_test_switch(
    tmp_path: Path,
) -> None:
    # The README's illustrations name everyone "Example ...": a copied gold row is refused even
    # with its id changed (zara-safety, PR 4 review).
    dataset_dir = small_repo(tmp_path)
    fields = {**REFERRAL_FIELDS, "patient_name": "Example Fernhall", "insurer": "EXAMPLE Health"}
    source = REFERRAL.replace("Mira Fernhall", "Example Fernhall").replace("Brightcover", "EXAMPLE")
    add_document(dataset_dir, gold("s-1", fields=fields), source)

    refused = check_dataset(read(dataset_dir)).problems
    for name in ("patient_name", "insurer"):
        assert has(refused, f"document s-1: field {name} holds the word 'example'")
    assert not has(check(dataset_dir).problems, "holds the word 'example'")  # the test switch


def test_the_word_example_is_refused_only_as_a_whole_word() -> None:
    assert PLACEHOLDER_WORD.search("Dr. Example Writer")
    assert PLACEHOLDER_WORD.search("example-pet")
    assert not PLACEHOLDER_WORD.search("Counterexamples Ltd")
    assert not PLACEHOLDER_WORD.search("Examplar Vet")


# ── Gold is on the page (M6 PR 4 review, ananya-rag) ───────────────────────────────────────


def test_a_gold_value_on_no_visible_line_is_named_by_field(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    typo = {**REFERRAL_FIELDS, "patient_name": "Mira Fernhal"}  # one letter short: still found
    typo |= {"referring_doctor": "Dr. Oren Vail", "policy_number": "BC/2026/0471"}
    add_document(dataset_dir, gold("s-1", fields=typo), REFERRAL)
    hidden = REFERRAL.replace("Insurer: Brightcover Health", "{hidden} Insurer: Brightcover Health")
    add_document(dataset_dir, gold("s-2", split="test"), hidden)
    swapped = {**VACCINATION_FIELDS, "species": "cat", "vaccine": "rabies"}
    add_document(dataset_dir, gold("p-1", PAWS, fields=swapped), VACCINATION)

    found = problems(dataset_dir)
    on_no_line = "gold is on no visible line of its .pdf.txt"
    assert has(found, "document s-1: field referring_doctor's", on_no_line)
    assert has(found, "document s-1: field policy_number's", on_no_line)
    assert not has(found, "document s-1: field patient_name")  # a prefix: E2 judges the rest
    assert has(found, "document s-2: field insurer's", on_no_line)  # only on a hidden line
    assert has(found, "document p-1: field species's", on_no_line)
    assert not has(found, "document p-1: field vaccine")
    assert not any("Fernhal" in p or "Vail" in p or "0471" in p for p in found)


def test_gold_found_as_a_synonym_in_a_column_or_with_other_end_punctuation(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    canine = VACCINATION.replace("Species: Dog", "Species: Canine").replace(
        "Issued by: Hillside Vet Clinic", "Issued by: Hillside Vet Clinic."
    )
    add_document(dataset_dir, gold("p-1", PAWS), canine)  # species dog, as the synonym Canine
    add_document(
        dataset_dir, gold("p-2", PAWS, split="test", tags=["table", "image_page"]), TABLE_AND_IMAGE
    )
    dogged = VACCINATION.replace("Species: Dog", "Species: Dogfish")  # not a whole token
    add_document(dataset_dir, gold("s-1", PAWS, split="dev"), dogged)

    found = problems(dataset_dir)
    assert not has(found, "document p-1") and not has(found, "document p-2")
    assert has(found, "document s-1: field species's gold is on no visible line")


# ── Files ─────────────────────────────────────────────────────────────────────────────────────


def test_missing_stale_and_stray_files(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), None)  # no source
    add_document(dataset_dir, gold("s-2", split="test"), REFERRAL, build=False)  # no PDF
    stale = pdf_of_text(REFERRAL.replace("Mira", "Tova"))
    add_document(dataset_dir, gold("p-1", PAWS), REFERRAL, pdf=stale)  # an older rebuild
    documents = dataset_dir / DOCUMENTS_DIR
    (documents / "p-9.pdf.txt").write_text(REFERRAL, "utf-8")  # no gold row
    (documents / "notes.md").write_text(CANARY, "utf-8")
    (documents / ".DS_Store").write_bytes(b"")

    found = problems(dataset_dir)
    assert "documents/s-1.pdf.txt is missing" in found
    assert has(found, "documents/s-2.pdf is missing: build it from its .pdf.txt")
    assert has(found, "documents/p-1.pdf: page 1 lacks the text of source line(s) of that page 4")
    assert has(found, "documents/p-9: files without a gold row")
    assert "documents/notes.md is not a document's file" in found
    assert not has(found, ".DS_Store")


def test_a_source_that_does_not_parse_or_is_not_utf8(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), f"{CANARY}\n=== page 1 ===\n", build=False)
    add_document(dataset_dir, gold("s-2", split="test"), None)
    (dataset_dir / DOCUMENTS_DIR / "s-2.pdf.txt").write_bytes(b"=== page 1 ===\n\xff\n")

    found = problems(dataset_dir)
    assert "documents/s-1.pdf.txt line 1: text before the first page marker" in found
    assert "documents/s-2.pdf.txt is not UTF-8 text" in found


def test_image_page_and_table_tags_follow_the_source_both_ways(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("p-1", PAWS), TABLE_AND_IMAGE)  # untagged
    add_document(
        dataset_dir, gold("p-2", PAWS, split="test", tags=["table", "image_page"]), VACCINATION
    )

    found = problems(dataset_dir)
    for doc_id in ("p-1", "p-2"):
        assert has(found, f"{doc_id}.pdf.txt: tagged image_page exactly when a page is marked")
        assert has(found, f"{doc_id}.pdf.txt: tagged table exactly when a line has a column")


def test_the_pdfs_image_pages_must_be_the_sources(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    no_image = pdf_of_text(TABLE_AND_IMAGE.replace("=== page 2 image ===", "=== page 2 ==="))
    row = gold("p-1", PAWS, tags=["table", "image_page"])
    add_document(dataset_dir, row, TABLE_AND_IMAGE, pdf=no_image)

    assert has(problems(dataset_dir), "p-1.pdf: its image pages differ from its source's")


def test_a_line_with_personal_data_is_named_by_page_and_line(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL + "Phone: 98765 43210\nMail: a@b.com\n")

    found = problems(dataset_dir)
    assert has(found, "s-1.pdf.txt: page 1 line 8 holds what looks like personal data (a phone")
    assert has(found, "s-1.pdf.txt: page 1 line 9 holds what looks like personal data (an e-mail")
    assert not any("98765" in p or "a@b.com" in p for p in found)
    assert not has(found, "s-1.pdf: page 1 holds")  # named once, by its source line


def test_a_pdf_holding_text_its_source_does_not_is_refused_and_screened(tmp_path: Path) -> None:
    # M6 PR 4 review (zara-safety): a live run sends the PDF's text, so a PDF padded after its
    # source was checked must fail `check` itself, not only CI's byte-for-byte rebuild.
    dataset_dir = small_repo(tmp_path)
    padded = pdf_of_text(REFERRAL + f"{CANARY} 98765 43210 owner@gmail.com\n")
    add_document(dataset_dir, gold("s-1"), REFERRAL, pdf=padded)

    found = problems(dataset_dir)
    assert has(found, "documents/s-1.pdf: page 1 holds text its source does not; rebuild it")
    assert has(found, "s-1.pdf: page 1 holds what looks like personal data (a phone")
    assert has(found, "s-1.pdf: page 1 holds what looks like personal data (an e-mail")
    assert not any("98765" in p or "owner@" in p for p in found)


def test_a_pdf_with_exactly_its_sources_text_has_no_residue(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("p-1", PAWS, tags=["table", "image_page"]), TABLE_AND_IMAGE)

    assert not has(problems(dataset_dir), "holds text its source does not")


def test_a_short_line_inside_a_longer_one_leaves_no_residue() -> None:
    # Lines come out longest first: taking "Dr" out first would break "Asha Dr Rao"'s match.
    text = "=== page 1 ===\nAsha Dr Rao\nDr\n"
    assert pdf_problems("s-1", parse_source(text), pdf_of_text(text)) == []


def test_e12_refuses_too_many_pages_in_the_source_and_the_pdf(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    six = REFERRAL + "".join(f"=== page {n} ===\nTEST-ONLY\n" for n in range(2, 7))
    add_document(dataset_dir, gold("s-1"), six)

    found = problems(dataset_dir)
    assert has(found, "s-1.pdf.txt: 6 pages, over E12's 5 (refused, never cut)")
    assert has(found, "s-1.pdf: the core refuses it (over E12: refused, never cut): too_many_pages")


def test_e12_keeps_a_document_of_exactly_five_pages(tmp_path: Path) -> None:
    # E12's limit itself is allowed (mutation audit CD01).
    dataset_dir = small_repo(tmp_path)
    five = REFERRAL + "".join(f"=== page {n} ===\nTEST-ONLY\n" for n in range(2, 6))
    add_document(dataset_dir, gold("s-1"), five)
    assert check(dataset_dir).problems == ()


def test_e12_refuses_too_much_text() -> None:
    line = "x" * 1000
    source = SourceDocument((SourcePage(1, False, tuple(SourceLine(line) for _ in range(21))),))
    pdf = build_pdf([[line] * 21])

    (problem,) = pdf_problems("s-1", source, pdf)
    assert problem.startswith("documents/s-1.pdf: the core refuses it (over E12")
    assert "too_much_text" in problem


def test_an_unreadable_pdf_is_refused_without_e12(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL, pdf=b"not a pdf")

    found = problems(dataset_dir)
    assert has(found, "s-1.pdf: the core refuses it: not_pdf")


def test_a_page_count_that_differs_from_the_source(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    add_document(dataset_dir, gold("s-1"), REFERRAL, pdf=pdf_of_text(REFERRAL + "=== page 2 ===\n"))

    assert has(problems(dataset_dir), "s-1.pdf: 2 page(s), its source has 1; rebuild it")


def test_a_business_without_an_extraction_file(tmp_path: Path) -> None:
    dataset = read(small_repo(tmp_path))
    only_sunrise = MappingProxyType({SUNRISE: dataset.extraction.businesses[SUNRISE]})
    dataset = replace(dataset, extraction=replace(dataset.extraction, businesses=only_sunrise))

    found = check_dataset(dataset, allow_placeholders=True).problems
    assert has(found, "business paws-and-co has no extraction business file")
    assert set(dataset.business_pins()) == {SUNRISE}


def test_no_problem_ever_holds_a_line_or_a_value(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    bad = REFERRAL.replace("Mira Fernhall", CANARY) + f"{{x=999}} {CANARY}\n"
    fields = {**REFERRAL_FIELDS, "patient_name": CANARY, "date_of_birth": CANARY}
    add_document(dataset_dir, gold("s-1", fields=fields, tags=["table"]), bad, build=False)
    add_document(
        dataset_dir, gold("s-2", split="test", fields=fields), REFERRAL.replace("Mira", "X")
    )

    found = problems(dataset_dir)  # asserts the canary is in none of them
    assert len(found) >= 3


# ── Minimums and the stage ────────────────────────────────────────────────────────────────────


def test_minimums_count_documents_tags_and_filled_slots(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)  # the committed slots and D8's minimums
    row = gold("sunrise-02", split="test", tags=["absent", "confusable"])
    row["fields"] = {**REFERRAL_FIELDS, "date_of_birth": None}
    add_document(dataset_dir, row, REFERRAL)

    found = {m.what: (m.have, m.need) for m in minimums(read(dataset_dir))}
    assert found["documents"] == (1, 24)
    assert found["business sunrise-clinic: documents"] == (1, 12)
    assert found["business sunrise-clinic: tagged absent"] == (1, 3)
    assert found["business sunrise-clinic: tagged confusable"] == (1, 3)
    assert found["business paws-and-co: tagged table"] == (0, 1)
    assert found["slots filled"] == (1, 24)
    # While collecting, minimums are not problems.
    assert check(dataset_dir).problems == ()
