"""M6 PR 4: `freeze` and its pins (`extracteval/checks.freeze_dataset`, `freeze.py`).

The freeze assigns no split (each is its slot's); it pins every gold row, source and PDF and the
slots. Every later change is a problem naming a new version. It does not pin the extraction
configuration (M6 PR 4 review, ananya-rag): a template or business file change is a new config
in the run's fingerprint, while gold is still checked against the current template.
"""

import json
from pathlib import Path

import pytest

from tests.extracteval.helpers import (
    PAWS,
    REFERRAL,
    TODAY,
    add_document,
    check,
    filled_repo,
    gold,
    make_repo,
    read,
    small_repo,
)
from tests.extracteval.pdfs import pdf_of_text
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.extracteval.checks import freeze_dataset
from vectapilot_api.extracteval.files import write_frozen
from vectapilot_api.extracteval.freeze import gold_hash
from vectapilot_api.extracteval.gold import DOCUMENTS_DIR, FREEZE_FILE, GOLD_FILE, MANIFEST_FILE


def frozen_repo(tmp_path: Path) -> Path:
    dataset_dir = filled_repo(tmp_path)
    write_frozen(
        dataset_dir, freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    )
    return dataset_dir


def test_freeze_pins_every_document_and_the_slots_not_the_configuration(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path)
    dataset = read(dataset_dir)

    assert dataset.manifest.stage == "frozen"
    assert dataset.freeze is not None
    assert dataset.freeze.frozen_on == TODAY
    assert {i: d.split for i, d in dataset.freeze.documents.items()} == {
        "s-1": "dev",
        "s-2": "test",
        "p-1": "dev",
        "p-2": "test",
    }
    (row,) = [r for r in dataset.rows if r.id == "p-2"]
    assert dataset.freeze.documents["p-2"].gold_sha256 == gold_hash(row)
    frozen = json.loads((dataset_dir / FREEZE_FILE).read_text("utf-8"))
    assert set(frozen) == {"dataset", "version", "frozen_on", "slots_sha256", "documents"}
    assert check(dataset_dir).problems == ()
    # The gold file and the documents are not rewritten.
    assert (dataset_dir / GOLD_FILE).read_text("utf-8").count("\n") == 4


def test_freeze_is_refused_while_a_slot_is_empty_or_a_minimum_is_short(tmp_path: Path) -> None:
    dataset_dir = make_repo(tmp_path)  # the committed slots and D8's minimums
    add_document(dataset_dir, gold("sunrise-05", tags=["confusable"]), REFERRAL)

    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    assert "documents: 1 of at least 24" in refused.value.problems
    assert "slots filled: 1 of at least 24" in refused.value.problems
    assert not (dataset_dir / FREEZE_FILE).exists()


def test_freeze_is_refused_on_a_check_problem_and_when_frozen(tmp_path: Path) -> None:
    dataset_dir = small_repo(tmp_path)
    for doc_id, business_id, split in (
        ("s-1", "sunrise-clinic", "dev"),
        ("s-2", "sunrise-clinic", "test"),
        ("p-1", PAWS, "dev"),
        ("p-2", PAWS, "test"),
    ):
        add_document(
            dataset_dir,
            gold(doc_id, business_id, split=split),
            REFERRAL.replace("Mira", doc_id.upper()),
        )
    # The TEST-ONLY marker without the test switch is a check problem.
    with pytest.raises(FreezeRefused):
        freeze_dataset(read(dataset_dir), frozen_on=TODAY)
    frozen = frozen_repo(tmp_path / "other")
    with pytest.raises(FreezeRefused) as refused:
        freeze_dataset(read(frozen), frozen_on=TODAY, allow_placeholders=True)
    assert refused.value.problems == ("v0.1 is already frozen",)


def _manifest(dataset_dir: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((dataset_dir / MANIFEST_FILE).read_text("utf-8"))
    return data


def test_every_change_after_the_freeze_is_a_problem(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path)
    documents = dataset_dir / DOCUMENTS_DIR
    rows = [json.loads(line) for line in (dataset_dir / GOLD_FILE).read_text("utf-8").splitlines()]
    rows[0]["fields"]["insurer"] = None  # a gold correction
    (dataset_dir / GOLD_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows[:3]), "utf-8")
    source = (documents / "s-2.pdf.txt").read_text("utf-8").replace("Dated", "Dated:")
    (documents / "s-2.pdf.txt").write_text(source, "utf-8")
    (documents / "s-2.pdf").write_bytes(pdf_of_text(source))
    manifest = _manifest(dataset_dir)
    slots = manifest["slots"]
    assert isinstance(slots, list)
    slots[2]["tags"] = ["confusable"]
    (dataset_dir / MANIFEST_FILE).write_text(json.dumps(manifest), "utf-8")

    found = check(dataset_dir).problems
    new = "v0.1 is frozen: make a new version instead"
    assert (
        f"document s-1's gold row changed after the freeze; {new} (README, 'Adding a version')"
        in found
    )
    assert any(p.startswith("document s-2's .pdf.txt changed after the freeze") for p in found)
    assert any(p.startswith("document s-2's .pdf changed after the freeze") for p in found)
    assert any(p.startswith("the slots changed after the freeze") for p in found)
    assert any(p.startswith("document p-2 was frozen and is gone") for p in found)
    assert any(p.startswith("documents/p-2: files without a gold row") for p in found)


def test_a_moved_document_and_a_frozen_version_without_its_freeze(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path)
    manifest = _manifest(dataset_dir)
    slots = manifest["slots"]
    assert isinstance(slots, list)
    slots[0]["split"], slots[1]["split"] = "test", "dev"
    (dataset_dir / MANIFEST_FILE).write_text(json.dumps(manifest), "utf-8")
    rows = [json.loads(line) for line in (dataset_dir / GOLD_FILE).read_text("utf-8").splitlines()]
    rows[0]["split"], rows[1]["split"] = "test", "dev"
    (dataset_dir / GOLD_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")

    found = check(dataset_dir).problems
    assert any(p.startswith("document s-1 moved from dev to test") for p in found)

    (dataset_dir / FREEZE_FILE).unlink()
    assert (
        "the manifest's stage is frozen, but there is no freeze.json" in check(dataset_dir).problems
    )


def test_a_freeze_of_another_version_is_a_problem(tmp_path: Path) -> None:
    # The copy-forward case: v0.1 copied as v0.2, stage and freeze.json included. Every other pin
    # still matches, so only the freeze's own version names it (mutation audit FZ04).
    dataset_dir = frozen_repo(tmp_path)
    manifest = _manifest(dataset_dir) | {"version": "v0.2"}
    (dataset_dir / MANIFEST_FILE).write_text(json.dumps(manifest), "utf-8")

    assert "freeze.json belongs to another dataset or version" in check(dataset_dir).problems


def test_a_freeze_file_while_collecting_is_a_problem(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path)
    manifest = _manifest(dataset_dir) | {"stage": "collecting"}
    (dataset_dir / MANIFEST_FILE).write_text(json.dumps(manifest), "utf-8")

    assert (
        "freeze.json exists, but the manifest's stage is collecting" in check(dataset_dir).problems
    )


def _template(dataset_dir: Path) -> Path:
    root = dataset_dir.parents[3]
    return root / "data" / "extraction" / "templates" / "veterinary" / "vaccination_record.json"


def test_a_configuration_change_after_the_freeze_is_a_new_config_not_a_new_version(
    tmp_path: Path,
) -> None:
    # PR 5's natural dev fix: a label added to the template (and a business file tweak). The
    # frozen dataset still passes its check; the run fingerprint's pins change, so the change is
    # a new config with its own test run.
    dataset_dir = frozen_repo(tmp_path)
    before = read(dataset_dir).business_pins()
    template = _template(dataset_dir)
    template.write_text(
        template.read_text("utf-8").replace(
            '"Owner name", "Owner",', '"Owner name", "Owner", "Kept by",'
        ),
        "utf-8",
    )
    business = dataset_dir.parents[3] / "data" / "extraction" / "businesses" / "paws-and-co.json"
    business.write_text(
        business.read_text("utf-8").replace('"retention_days": 30', '"retention_days": 29'), "utf-8"
    )

    assert check(dataset_dir).problems == ()
    after = read(dataset_dir).business_pins()
    assert after["paws-and-co"] != before["paws-and-co"]
    assert after["paws-and-co"].templates != before["paws-and-co"].templates
    assert after["sunrise-clinic"] == before["sunrise-clinic"]


def test_a_renamed_template_field_after_the_freeze_fails_the_gold_check(tmp_path: Path) -> None:
    dataset_dir = frozen_repo(tmp_path)
    template = _template(dataset_dir)
    template.write_text(
        template.read_text("utf-8").replace('"name": "issuing_clinic"', '"name": "issued_by"'),
        "utf-8",
    )

    found = check(dataset_dir).problems
    for doc_id in ("p-1", "p-2"):
        assert f"gold.jsonl document {doc_id}: field issued_by is missing (write null when the" in (
            " ".join(found)
        )
        assert f"gold.jsonl document {doc_id}: field issuing_clinic is not in the" in " ".join(
            found
        )
