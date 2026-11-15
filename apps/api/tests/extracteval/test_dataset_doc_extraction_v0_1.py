"""The committed `doc_extraction` v0.1 (M6 PR 4): what CI checks on every change.

While collecting, every document must pass `check` on its own (zero documents pass). Once frozen,
the same test enforces D8's minimums, every slot filled and every pin. The decided values are
pinned here (D8 (b), 2026-11-11) and so are the slots, fixed before any document: lowering a
minimum or moving a slot is a decision, not an edit.

**The PDFs.** `UPDATE_DOC_PDFS=1 uv run pytest -q` on this file rebuilds every document's PDF
from its `.pdf.txt` (the fixture writer, as M2's `UPDATE_SAMPLE_PDF`); without it, a PDF that is
not its source's rebuild, byte for byte, fails.
"""

import json
import os
import re
from pathlib import Path
from typing import Final

from tests.extracteval.helpers import COMMITTED, PAWS, SUNRISE, make_repo, read
from tests.extracteval.pdfs import pdf_of_text
from vectapilot_api.extracteval.checks import check_dataset
from vectapilot_api.extracteval.files import read_dataset
from vectapilot_api.extracteval.gold import DOCUMENTS_DIR, GOLD_FILE, MANIFEST_FILE
from vectapilot_api.extracteval.source import PDF_SUFFIX, SOURCE_SUFFIX, parse_source

UPDATE: Final = os.environ.get("UPDATE_DOC_PDFS") == "1"
D8_MINIMUMS: Final = {
    "min_documents": 24,
    "min_per_business": 12,
    "min_tag_per_business": {
        "absent": 3,
        "confusable": 3,
        "injection": 2,
        "image_page": 2,
        "table": 1,
    },
}
TAG_ORDER: Final = (
    ("absent",) * 3
    + ("confusable",) * 3
    + ("injection",) * 2
    + ("image_page",) * 2
    + ("table",) * 2
)
SLOTS: Final = [
    (f"{prefix}-{n + 1:02d}", business, [tag], first if n % 2 == 0 else second)
    for prefix, business, first, second in (
        ("sunrise", SUNRISE, "dev", "test"),
        ("paws", PAWS, "test", "dev"),
    )
    for n, tag in enumerate(TAG_ORDER)
]
"""The 24 slots as fixed on 2026-11-13, before any document (README, "Slots")."""


def test_the_committed_version_passes_its_check() -> None:
    assert check_dataset(read_dataset(COMMITTED)).problems == ()


def test_the_manifest_holds_d8s_minimums_and_the_slots_fixed_before_any_document() -> None:
    manifest = json.loads((COMMITTED / MANIFEST_FILE).read_text("utf-8"))
    assert manifest["synthetic"] is True
    assert manifest["requirements"] == D8_MINIMUMS
    assert manifest["businesses"] == [SUNRISE, PAWS]
    assert (manifest["corpus"], manifest["extraction"]) == ("data/sample", "data/extraction")
    slots = [(s["id"], s["business_id"], s["tags"], s["split"]) for s in manifest["slots"]]
    assert slots == SLOTS


def test_every_pdf_is_its_sources_rebuild_byte_for_byte() -> None:
    dataset = read_dataset(COMMITTED)
    folder = COMMITTED / DOCUMENTS_DIR
    for row in dataset.rows:
        source = (folder / f"{row.id}{SOURCE_SUFFIX}").read_text("utf-8")
        expected = pdf_of_text(source)
        path = folder / f"{row.id}{PDF_SUFFIX}"
        if UPDATE:
            path.write_bytes(expected)
        assert path.read_bytes() == expected, f"rebuild {path.name} (see the module docstring)"


def test_documents_and_gold_are_human_written_and_synthetic() -> None:
    dataset = read_dataset(COMMITTED)
    assert {row.author for row in dataset.rows} <= {"shesha", "friend"}
    assert all(row.synthetic for row in dataset.rows)
    assert set(dataset.documents) == {row.id for row in dataset.rows}


README: Final = (COMMITTED / "README.md").read_text("utf-8")
ILLUSTRATION: Final = re.compile(r"```pdf\.txt\n(.*?)```\n\n.*?```json\n(.*?)\n```", re.DOTALL)


def test_the_readme_holds_the_rules_and_the_brief() -> None:
    for section in (
        "## Scoring rules",
        "## Slots (fixed before any document)",
        "## Documents: the `.pdf.txt` format",
        "## Gold rows (`gold.jsonl`)",
        "## Content rules",
        "## Writers' brief",
    ):
        assert section in README, section
    assert "UPDATE_DOC_PDFS=1" in README


def test_the_readmes_two_illustrations_are_valid_but_refused(tmp_path: Path) -> None:
    illustrations = ILLUSTRATION.findall(README)
    assert len(illustrations) == 2
    rows = [json.loads(row) for _, row in illustrations]
    assert [row["id"] for row in rows] == ["example-referral", "example-vaccination"]
    slots = [
        {"id": "example-referral", "business_id": SUNRISE, "tags": [], "split": "dev"},
        {"id": "s-2", "business_id": SUNRISE, "tags": [], "split": "test"},
        {"id": "p-1", "business_id": PAWS, "tags": [], "split": "dev"},
        {"id": "example-vaccination", "business_id": PAWS, "tags": [], "split": "test"},
    ]
    low = {"min_documents": 1, "min_per_business": 1, "min_tag_per_business": {}}
    dataset_dir = make_repo(tmp_path, slots=slots, requirements=low)
    documents = dataset_dir / DOCUMENTS_DIR
    documents.mkdir()
    for (source, _), row in zip(illustrations, rows, strict=True):
        assert "EXAMPLE-ONLY" in parse_source(source).texts()[0]
        (documents / f"{row['id']}{SOURCE_SUFFIX}").write_text(source, "utf-8")
        (documents / f"{row['id']}{PDF_SUFFIX}").write_bytes(pdf_of_text(source))
    (dataset_dir / GOLD_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")

    # With the test switch, the illustrations meet every rule: the format they teach is real.
    assert check_dataset(read(dataset_dir), allow_placeholders=True).problems == ()
    # Without it (as `check` always runs), both are refused by id and by marker.
    refused = check_dataset(read(dataset_dir)).problems
    for row in rows:
        assert f"gold.jsonl document {row['id']}: is a placeholder id" in " ".join(refused)
        assert f"{row['id']}.pdf.txt: page 1 line 1 is a placeholder" in " ".join(refused)
    # And by their "Example ..." values, so a copy renamed and stripped of its marker line still
    # fails (tests/extracteval/test_no_copies.py tries exactly that).
    joined = " ".join(refused)
    assert "document example-referral: field patient_name holds the word 'example'" in joined
    assert "document example-vaccination: field pet_name holds the word 'example'" in joined
