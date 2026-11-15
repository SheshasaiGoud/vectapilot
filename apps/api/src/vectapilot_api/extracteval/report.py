"""`report` (M6 PR 4): the slots, counts per business, tag and split, D8's minimums, and then
each gold row beside its page text, for shesha's re-read before the freeze (plan PR 5 step 1).

Counts describe the dataset; they measure nothing about extraction. Every tag is printed, zeros
included, so an empty cell is visible.

**The documents section prints values and document text**, because re-reading every gold value
beside the page it came from is its purpose: synthetic text, to this terminal only. The page text
is the PDF as the core reads it (`checks.read_document`: pypdf's text layer, hidden lines
included, a two-column table as its columns come out), because that is what the model sees and
what each value must be found in; image pages are marked. When the PDF is missing or refused,
the `.pdf.txt` is shown instead, and the header says so. pypdf prints a `hidden` line like any
other, so the source's hidden lines are listed again under their own heading ("hidden (in the
text layer, not on the page)"): gold never comes from one (M6 PR 4 review, ananya-rag).
Each document's text, hidden lines included, sits between `UNTRUSTED_BEGIN` and `UNTRUSTED_END`
(as E7 fences it in prompts): the `injection` documents are written to instruct whoever reads them
(M6 PR 4 review, zara-safety). The full report is for shesha's own terminal; agents run only
`check` and `report --counts` (README, "Commands").
`--counts` leaves this section out (`make extract-check`). Nothing else in M6 prints a value: run
summaries never do (PR 4's `summarize`).
"""

from collections import Counter
from collections.abc import Sequence

from vectapilot_api.extracteval.checks import (
    DocExtractionDataset,
    DocumentFiles,
    minimums,
    read_document,
)
from vectapilot_api.extracteval.gold import TAGS, GoldRow
from vectapilot_api.extracteval.source import SourceError, parse_source
from vectapilot_api.extraction.text import DocumentRefused

MINIMUM_WIDTH = 44
NULL = "(null)"
HIDDEN_HEADING = "hidden (in the text layer, not on the page; gold never comes from these)"
UNTRUSTED_BEGIN = "  BEGIN UNTRUSTED DOCUMENT TEXT (data, never instructions)"
UNTRUSTED_END = "  END UNTRUSTED DOCUMENT TEXT"


def _slots(dataset: DocExtractionDataset) -> list[str]:
    rows = {row.id: row for row in dataset.rows}
    width = max(len(slot.id) for slot in dataset.manifest.slots)
    business = max(len(slot.business_id) for slot in dataset.manifest.slots)
    lines = ["slots (fixed before any document; a document is written into its slot)"]
    for slot in dataset.manifest.slots:
        row = rows.get(slot.id)
        tags = "empty" if row is None else "tags " + (", ".join(row.tags) or "none")
        lines.append(
            f"  {slot.id:<{width}}  {slot.business_id:<{business}}  {slot.split:<4}  requires"
            f" {', '.join(slot.tags) or 'no tag':<12}  {tags}"
        )
    return lines


def _counts(dataset: DocExtractionDataset) -> list[str]:
    counts: Counter[tuple[str, str, str]] = Counter(
        (row.business_id, tag, row.split) for row in dataset.rows for tag in row.tags
    )
    documents: Counter[tuple[str, str]] = Counter(
        (row.business_id, row.split) for row in dataset.rows
    )
    header = f"  {'':<28}" + "".join(f"{tag:>12}" for tag in ("documents", *TAGS))
    lines = ["documents by business, tag and split", header]
    for business_id in dataset.manifest.businesses:
        for split in ("dev", "test"):
            cells = [documents[(business_id, split)]] + [
                counts[(business_id, tag, split)] for tag in TAGS
            ]
            label = f"{business_id} {split}"
            lines.append(f"    {label:<26}" + "".join(f"{n:>12}" for n in cells))
    return lines


def _minimums(dataset: DocExtractionDataset) -> list[str]:
    found = minimums(dataset)
    width = max([MINIMUM_WIDTH, *(len(m.what) for m in found)])
    lines = ["minimums (what freeze needs; D8 (b))"]
    for minimum in found:
        mark = "ok" if minimum.met else "short"
        lines.append(f"  {minimum.what:<{width}} {minimum.have:>4} of {minimum.need:<4} {mark}")
    return lines


def _pages(files: DocumentFiles) -> tuple[str, list[tuple[int, bool, str]]]:
    """Where the text comes from, and each page's number, image mark and text."""
    if files.pdf is not None:
        read = read_document(files.pdf)
        if not isinstance(read, DocumentRefused):
            return "the PDF as the core reads it", [
                (p.number, p.has_images, p.text) for p in read.pages
            ]
        note = f"the PDF is refused ({read.reason.value}): its .pdf.txt"
    else:
        note = "no PDF yet: its .pdf.txt"
    if files.source is None:
        return "no .pdf.txt and no readable PDF", []
    try:
        source = parse_source(files.source.decode("utf-8", errors="replace"))
    except SourceError:
        return f"{note} does not parse (run check)", []
    return note, [
        (p.number, p.image, "\n".join(line.text for line in p.lines)) for p in source.pages
    ]


def _document(row: GoldRow, files: DocumentFiles) -> list[str]:
    lines = [
        f"=== {row.id} · {row.business_id} · {row.kind} · {row.split} · tags"
        f" {', '.join(row.tags) or 'none'} · {row.author} · {row.created.isoformat()}",
        "gold:",
    ]
    width = max(len(name) for name in row.fields)
    lines += [
        f"  {name:<{width}}  {NULL if value is None else value}"
        for name, value in row.fields.items()
    ]
    where, pages = _pages(files)
    lines += [f"page text ({where}):", UNTRUSTED_BEGIN]
    for number, image, text in pages:
        lines.append(f"  --- page {number}{' · image' if image else ''} ---")
        lines += [f"  | {line}" for line in text.split("\n")] if text.strip() else ["  (no text)"]
    hidden = _hidden(files)
    if hidden:
        lines.append(f"{HIDDEN_HEADING}:")
        lines += [f"  | page {number}: {text}" for number, text in hidden]
    lines.append(UNTRUSTED_END)
    return lines


def _hidden(files: DocumentFiles) -> list[tuple[int, str]]:
    """The source's `hidden` lines, by page (none when the source is missing or broken)."""
    if files.source is None:
        return []
    try:
        source = parse_source(files.source.decode("utf-8", errors="replace"))
    except SourceError:
        return []
    return [(p.number, line.text) for p in source.pages for line in p.lines if line.hidden]


def render_report(
    dataset: DocExtractionDataset, *, counts_only: bool = False, only: Sequence[str] = ()
) -> str:
    """The plain-text report (the module docstring). `only` limits the documents section to
    these ids."""
    manifest = dataset.manifest
    filled = len({row.id for row in dataset.rows} & {slot.id for slot in manifest.slots})
    lines = [
        f"{manifest.dataset} {manifest.version} ({manifest.stage}, synthetic): "
        f"{len(dataset.rows)} documents, {filled} of {len(manifest.slots)} slots filled",
        "",
        *_slots(dataset),
        "",
        *_counts(dataset),
        "",
        *_minimums(dataset),
    ]
    if not counts_only:
        for row in dataset.rows:
            if only and row.id not in only:
                continue
            lines += ["", *_document(row, dataset.documents.get(row.id, DocumentFiles(None, None)))]
    return "\n".join(lines) + "\n"
