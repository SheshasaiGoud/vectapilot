"""`uv run python -m vectapilot_api.evalset <command>`: check, report on and freeze eval v0.

Run from anywhere inside the repository (the dataset defaults to `evals/datasets/support_qa/
v0.1`). Commands:

- `check`  — every row valid for the version's stage (what CI runs); prints problems, warnings
- `ready`  — `check` plus the size and coverage requirements, the corpus review, and a dry run of
  the split with its dev and test counts per group (what `freeze` needs; writes nothing)
- `report` — counts per business, category, split, language, author and outcome
- `lookup BUSINESS TEXT` — lines of the business's documents containing TEXT, with each line's
  source, version, status, audience, heading and chunk numbers: where a human finds an anchor
- `show BUSINESS SOURCE_ID [--version V]` — a document as ingestion loads it, chunk by chunk
- `freeze` — assign the splits by the recorded rule and pin them (writes files; once per version)
- `import-friends CSV` — a friends' template file as draft JSONL rows on stdout, the questions
  copied verbatim; every field a human must decide is left null, so `check` refuses the rows
  until shesha completes them. An optional last column `category` is shesha's triage: `action`
  and `document` rows are dropped and counted (reserved until M5 and M6), another category is
  copied. Comments are never copied (only counted): read them in the CSV

Nothing here calls a model or a network, and nothing writes, completes or suggests a question.
"""

import argparse
import csv
import io
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, TextIO

from vectapilot_api.evalset.checks import (
    CheckResult,
    FreezeRefused,
    check_dataset,
    freeze_dataset,
    preview_splits,
)
from vectapilot_api.evalset.corpus import (
    ANCHOR_MAX_CHARS,
    ANCHOR_MIN_CHARS,
    Corpus,
    CorpusError,
    lookup,
    review_problems,
)
from vectapilot_api.evalset.files import (
    DEFAULT_DATASET,
    Dataset,
    find_repo_root,
    read_dataset,
    write_frozen,
)
from vectapilot_api.evalset.personal_data import personal_data_findings
from vectapilot_api.evalset.questions import (
    PLACEHOLDER_MARKER,
    REQUIRED_CATEGORIES,
    RESERVED_CATEGORIES,
    DatasetError,
    coverage,
    readiness_problems,
    render_coverage,
)
from vectapilot_api.ingestion.types import ChunkingConfig

FRIEND_COLUMNS: Final = ("business_id", "language", "question", "comment")
TRIAGE_COLUMN: Final = "category"
"""shesha's optional column after the friends' four: a category, `action`/`document` to drop."""
FRIEND_LANGUAGES: Final = {"en": ("en", False), "hi": ("hi", False), "hinglish": ("hi", True)}
WAY_OUT: Final = (
    "way out: add or remove one question and run ready again; never change the seed or the "
    "percentage (README, 'If freeze refuses')"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.evalset",
        description="Check, report on and freeze the eval v0 dataset (no model calls).",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default: {DEFAULT_DATASET})"
    )
    parser.add_argument("--corpus", type=Path, help="the corpus folder (default: the manifest's)")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate every row for the version's stage")
    check.add_argument("--max-chars", type=int, help="resolve anchors at this chunk size")
    check.add_argument("--overlap", type=int, help="... and this overlap")
    commands.add_parser("ready", help="check, plus the size and coverage requirements")
    commands.add_parser("report", help="print the coverage counts")
    find = commands.add_parser("lookup", help="find lines containing a text (anchor candidates)")
    find.add_argument("business_id")
    find.add_argument("text")
    show = commands.add_parser("show", help="print one document chunk by chunk")
    show.add_argument("business_id")
    show.add_argument("source_id")
    show.add_argument("--version")
    freeze = commands.add_parser("freeze", help="assign splits by the rule and pin the version")
    freeze.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    friends = commands.add_parser("import-friends", help="friends' CSV to draft JSONL on stdout")
    friends.add_argument("csv_file", type=Path)
    friends.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    return parser


def _print_result(result: CheckResult, out: TextIO) -> None:
    for problem in result.problems:
        print(f"PROBLEM  {problem}", file=out)
    for warning in result.warnings:
        print(f"warning  {warning}", file=out)


def _check(dataset: Dataset, config: ChunkingConfig, out: TextIO) -> int:
    result = check_dataset(
        dataset.manifest,
        dataset.questions,
        dataset.corpus,
        freeze=dataset.freeze,
        previous_freeze=dataset.previous_freeze,
        config=config,
    )
    _print_result(result, out)
    stage = dataset.manifest.stage
    verdict = "ok" if result.ok else f"{len(result.problems)} problem(s)"
    print(f"check ({stage}, {len(dataset.questions)} questions): {verdict}", file=out)
    return 0 if result.ok else 1


def _ready(dataset: Dataset, out: TextIO) -> int:
    status = _check(dataset, ChunkingConfig(), out)
    missing = readiness_problems(dataset.manifest, dataset.questions, dataset.corpus)
    for item in missing:
        print(f"MISSING  {item}", file=out)
    reviews = review_problems(dataset.corpus, dataset.manifest.businesses)
    for item in reviews:
        print(f"REVIEW   {item}", file=out)
    preview = preview_splits(dataset.manifest, dataset.questions, dataset.previous_freeze)
    rule = dataset.manifest.split_rule
    print(
        f"split preview: a dry run of {rule.rule} ({rule.test_percent} % test); nothing written",
        file=out,
    )
    print(f"  {'group':<34} {'dev':>4} {'test':>4}", file=out)
    for group, dev, test in preview.rows:
        print(f"  {group:<34} {dev:>4} {test:>4}", file=out)
    for item in preview.problems:
        print(f"BALANCE  {item}", file=out)
    if preview.problems:
        print(f"BALANCE  {WAY_OUT}", file=out)
    ok = not missing and not reviews and not preview.problems and status == 0
    print(f"ready: {'yes' if ok else 'no'}", file=out)
    return 0 if ok else 1


def _lookup(corpus: Corpus, business_id: str, text: str, out: TextIO) -> int:
    if business_id not in corpus.business_ids:
        print(f"no corpus for business {business_id}", file=out)
        return 1
    lines = lookup(corpus, business_id, text)
    for found in lines:
        source = found.source
        usable = "" if source.answers_customers else "  << NEVER ANSWERS A CUSTOMER"
        where = " > ".join(found.heading_path) or (
            f"page {found.page}" if found.page else f"row {found.row}" if found.row else "-"
        )
        print(
            f"{source.source_id} {source.version} · {source.status} · {source.audience} · "
            f"{source.file} · {where} · chunks {list(found.chunk_indexes)}{usable}",
            file=out,
        )
        print(f"    {found.line}", file=out)
    print(
        f"{len(lines)} line(s). An anchor quotes {ANCHOR_MIN_CHARS}-{ANCHOR_MAX_CHARS} "
        "characters of one document: the clause that states the fact (a line break matches a "
        "space).",
        file=out,
    )
    return 0


def _show(
    corpus: Corpus, business_id: str, source_id: str, version: str | None, out: TextIO
) -> int:
    matches = [
        d
        for d in corpus.documents(business_id)
        if d.source.source_id == source_id and version in (None, d.source.version)
    ]
    if len(matches) != 1:
        versions = [
            d.source.version
            for d in corpus.documents(business_id)
            if d.source.source_id == source_id
        ]
        print(f"{len(matches)} documents match; versions of {source_id}: {versions}", file=out)
        return 1
    document = matches[0]
    source = document.source
    print(
        f"{business_id} · {source.source_id} {source.version} · {source.status} · "
        f"{source.audience} · {source.content_kind} · {source.language} · {source.file}",
        file=out,
    )
    for chunk in corpus.chunks(document, ChunkingConfig()):
        where = " > ".join(chunk.heading_path) or (
            f"page {chunk.page}" if chunk.page else f"row {chunk.row}" if chunk.row else "-"
        )
        print(
            f"\n── chunk {chunk.chunk_index} · chars {chunk.char_start}-{chunk.char_end} · "
            f"{where} ──",
            file=out,
        )
        print(chunk.content, file=out)
    return 0


def _freeze(dataset: Dataset, today: date, out: TextIO) -> int:
    try:
        frozen = freeze_dataset(
            dataset.manifest,
            dataset.questions,
            dataset.corpus,
            previous_freeze=dataset.previous_freeze,
            frozen_on=today,
        )
    except FreezeRefused as refused:
        for problem in refused.problems:
            print(f"PROBLEM  {problem}", file=out)
        if refused.unbalanced:
            print(f"PROBLEM  {WAY_OUT}", file=out)
        print("freeze refused: nothing was written", file=out)
        return 1
    write_frozen(dataset.directory, frozen)
    splits = [q.split for q in frozen.questions]
    print(
        f"frozen {frozen.manifest.dataset} {frozen.manifest.version}: "
        f"{splits.count('dev')} dev, {splits.count('test')} test. Review the diff and commit "
        "questions.jsonl, freeze.json and manifest.json together.",
        file=out,
    )
    return 0


@dataclass(frozen=True)
class FriendDrafts:
    rows: list[dict[str, object]]
    """Draft question objects, not yet valid on purpose."""
    comments: int
    """How many imported rows had a comment. Comments are never copied: they are free text a
    friend wrote for shesha, and the dataset is public (zara's review, Z1)."""
    dropped_reserved: int = 0
    """Rows shesha triaged as `action` or `document`: reserved until M5 and M6, so not imported,
    only counted (decided 2026-10-22, item 5). The README records the count."""
    skipped_examples: int = 0
    """Rows carrying the template's `EXAMPLE-ONLY` marker."""


def _triaged_category(value: str, number: int) -> str | None:
    """shesha's triage cell: blank (None), a required category, or a reserved one."""
    if not value:
        return None
    category = value.casefold()
    if category not in (*REQUIRED_CATEGORIES, *RESERVED_CATEGORIES):
        raise DatasetError(
            f"row {number}: {TRIAGE_COLUMN} must be blank or one of "
            f"{[*REQUIRED_CATEGORIES, *RESERVED_CATEGORIES]}"
        )
    return category


def import_friends(text: str, today: date, known_businesses: Sequence[str]) -> FriendDrafts:
    """Rows of a filled friends' template as draft question objects (`id`, `expected_outcome`
    and, unless triaged, `category` stay null for shesha). Rows marked EXAMPLE-ONLY and blank
    rows are skipped, rows triaged as `action` or `document` dropped; both are counted. Raises
    `DatasetError` naming the row and the column, never a value: a bad header, business,
    language or category, or a question that looks like it holds personal data
    (`personal_data.py`)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff"), newline=""))
    header = tuple(reader.fieldnames or ())
    if header not in (FRIEND_COLUMNS, (*FRIEND_COLUMNS, TRIAGE_COLUMN)):
        raise DatasetError(
            f"the header must be exactly: {','.join(FRIEND_COLUMNS)} "
            f"(optionally followed by ,{TRIAGE_COLUMN})"
        )
    drafts: list[dict[str, object]] = []
    comments = dropped = skipped = 0
    for number, row in enumerate(reader, start=2):
        values = {k: (v or "").strip() for k, v in row.items() if k is not None}
        if not values.get("question"):
            continue
        if PLACEHOLDER_MARKER.casefold() in values["question"].casefold():
            skipped += 1
            continue
        category = _triaged_category(values.get(TRIAGE_COLUMN, ""), number)
        if category in RESERVED_CATEGORIES:
            dropped += 1
            continue
        if values["business_id"] not in known_businesses:
            raise DatasetError(f"row {number}: business_id must be one of {list(known_businesses)}")
        language = FRIEND_LANGUAGES.get(values["language"].casefold())
        if language is None:
            raise DatasetError(f"row {number}: language must be one of {list(FRIEND_LANGUAGES)}")
        findings = personal_data_findings(values["question"])
        if findings:
            raise DatasetError(
                f"row {number}: question looks like it holds personal data ({findings[0]}); "
                "ask the friend to replace it with an invented one"
            )
        comments += bool(values.get("comment"))
        drafts.append(
            {
                "id": None,
                "replaces": None,
                "split_group": None,
                "business_id": values["business_id"],
                "question": values["question"],
                "language": language[0],
                "code_mixed": language[1],
                "category": category,
                "subtype": None,
                "author_type": "friend_blind",
                "saw_documents": False,
                "expected_outcome": None,
                "expected_evidence": [],
                "reference_answer": None,
                "split": None,
                "notes": None,
                "created": today.isoformat(),
                "synthetic": True,
            }
        )
    return FriendDrafts(
        rows=drafts, comments=comments, dropped_reserved=dropped, skipped_examples=skipped
    )


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    dataset_dir: Path = args.dataset or find_repo_root(Path.cwd()) / DEFAULT_DATASET
    try:
        dataset = read_dataset(dataset_dir, args.corpus)
        if args.command == "check":
            defaults = ChunkingConfig()
            config = ChunkingConfig(
                max_chars=args.max_chars or defaults.max_chars,
                overlap_chars=defaults.overlap_chars if args.overlap is None else args.overlap,
            )
            return _check(dataset, config, out)
        if args.command == "ready":
            return _ready(dataset, out)
        if args.command == "report":
            out.write(
                render_coverage(dataset.manifest, coverage(dataset.questions, dataset.corpus))
            )
            return 0
        if args.command == "lookup":
            return _lookup(dataset.corpus, args.business_id, args.text, out)
        if args.command == "show":
            return _show(dataset.corpus, args.business_id, args.source_id, args.version, out)
        today: date = args.today or date.today()
        if args.command == "freeze":
            return _freeze(dataset, today, out)
        text = args.csv_file.read_text("utf-8")
        drafts = import_friends(text, today, dataset.manifest.businesses)
        for draft in drafts.rows:
            print(json.dumps(draft, ensure_ascii=False), file=out)
        print(
            f"{len(drafts.rows)} draft row(s); complete id, category, expected_outcome and "
            "expected_evidence before adding them to questions.jsonl. "
            f"{drafts.dropped_reserved} row(s) triaged as action or document were dropped "
            "(reserved until M5 and M6): add the count to the README. "
            f"{drafts.skipped_examples} row(s) marked {PLACEHOLDER_MARKER} were skipped. "
            f"{drafts.comments} row(s) had a comment: read them in the CSV (comments are not "
            "copied).",
            file=sys.stderr,
        )
        return 0
    except (DatasetError, CorpusError) as error:
        print(f"error: {error}", file=out)
        return 2
