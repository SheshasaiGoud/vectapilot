"""`uv run python -m vectapilot_api.extracteval <command>`: check, report on and freeze the
`doc_extraction` dataset, and measure the extraction core on it (M6 PR 4, D8 (b)).

Run from anywhere inside the repository (the dataset defaults to
`evals/datasets/doc_extraction/v0.1`).

Dataset commands (no model, no network, no database; nothing written but the dataset's own files):

- `check`  — every slot, gold row and document valid for the version's stage (what CI runs);
  prints problems, never a value or a line of a document, never edits anything. Exit 1 on any
  problem
- `report` — the slots, counts per business, tag and split, and D8's minimums; then each gold row
  beside its page text (synthetic values and text, to this terminal only). `--counts` prints the
  counts alone (`make extract-check`); `--id ID` (repeatable) limits the documents
- `freeze` — refused while any slot is empty, a minimum is short or `check` has a problem;
  otherwise writes freeze.json and the manifest (`stage: frozen`), once per version

Exit 2: a file cannot be read or parsed, or the extraction configuration is invalid. None of
these writes, completes or suggests a document or a gold value.

Harness commands (`runs.py`; runbook: docs/plans/m6-pr-checks.md, PR 5; no database):

- `run --split dev|test [--live --max-usd X]` — every document of the split through the
  extraction core in-process, each with its business's configuration. Dry by default: a scripted
  fake model over the fixture documents in `tests/` (`--fixtures`), never the dataset's, into the
  git-ignored `evals/reports/tmp/` (`make extract-dry-run`). Live: the frozen dataset, through the
  capped gateway, refused unless everything is synthetic
- `summarize RUN_DIR` — (re)write the folder's summary through H2 (never prints a value)

`--live` and every test run are shesha's alone; agents never pass `--live` and never run test.
Exit codes: 0 complete · 1 refused or failed · 3 partial · 4 a cross-business leak.
"""

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final, TextIO

from pydantic import ValidationError

from vectapilot_api.evalrun.cli import cap_value
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.files import find_repo_root
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval import runs
from vectapilot_api.extracteval.checks import DocExtractionDataset, check_dataset, freeze_dataset
from vectapilot_api.extracteval.files import DEFAULT_DATASET, read_dataset, write_frozen
from vectapilot_api.extracteval.gold import FREEZE_FILE, MANIFEST_FILE
from vectapilot_api.extracteval.harness import PROG, Io, Refused
from vectapilot_api.extracteval.report import render_report
from vectapilot_api.extraction.config import ExtractionConfigError
from vectapilot_api.llm_cli import GitError
from vectapilot_api.measure.budget import MAX_USD_CEILING

DATASET_COMMANDS: Final = frozenset({"check", "report", "freeze"})
_RERUN_REASON: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Check, report on and freeze the doc_extraction dataset; measure the"
        " extraction core on it. Without --live everything is a DRY RUN ($0).",
        epilog="--live is shesha's alone (docs/plans/m6-pr-checks.md, PR 5); agents never pass it.",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default {DEFAULT_DATASET})"
    )
    parser.add_argument("--corpus", type=Path, help="the business manifests (default: manifest's)")
    parser.add_argument(
        "--extraction", type=Path, help="the extraction configuration (default: manifest's)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every slot, gold row and document")
    report = commands.add_parser("report", help="counts and minimums, then gold beside page text")
    report.add_argument("--counts", action="store_true", help="the counts only, no document")
    report.add_argument("--id", action="append", default=[], help="only this document (repeat)")
    freeze = commands.add_parser("freeze", help="pin the version once every slot is filled")
    freeze.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    run = commands.add_parser("run", help="every document of a split through the extraction core")
    run.add_argument("--split", choices=("dev", "test"), required=True)
    run.add_argument("--live", action="store_true", help="paid calls (shesha only)")
    run.add_argument("--max-usd", help=f"the cap in US dollars, at most {MAX_USD_CEILING}")
    run.add_argument("--test-rerun-reason", help="a slug, recorded in the test-run ledger")
    run.add_argument("--fixtures", type=Path, help="the dry run's fixtures (default: tests')")
    summarize = commands.add_parser("summarize", help="(re)write a folder's summary through H2")
    summarize.add_argument("run_dir", type=Path)
    return parser


def _checked_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse argument combinations before anything is read (exit 2, argparse's)."""
    live = getattr(args, "live", False)
    text = getattr(args, "max_usd", None)
    args.cap_text = text
    args.cap = None
    if live and text is None:
        parser.error("--live requires --max-usd")
    if text is not None:
        args.cap = cap_value(text)
        if args.cap is None:
            parser.error(f"--max-usd must be plain digits above 0 and at most {MAX_USD_CEILING}")
        if not live:
            parser.error("--max-usd is for --live runs only")
    if live and getattr(args, "fixtures", None) is not None:
        parser.error("--fixtures is for dry runs only (a live run reads the frozen dataset)")
    reason = getattr(args, "test_rerun_reason", None)
    if reason is not None and (_RERUN_REASON.fullmatch(reason) is None or len(reason) > 80):
        parser.error("--test-rerun-reason must be a short lowercase slug (a-z, 0-9, -)")


def _check(dataset: DocExtractionDataset, out: TextIO) -> int:
    result = check_dataset(dataset)
    for problem in result.problems:
        print(f"PROBLEM  {problem}", file=out)
    for warning in result.warnings:
        print(f"warning  {warning}", file=out)
    verdict = "ok" if result.ok else f"{len(result.problems)} problem(s)"
    manifest = dataset.manifest
    print(
        f"check ({manifest.stage}, {len(dataset.rows)} documents in {len(manifest.slots)} slots):"
        f" {verdict}",
        file=out,
    )
    return 0 if result.ok else 1


def _freeze(dataset: DocExtractionDataset, dataset_dir: Path, today: date, out: TextIO) -> int:
    try:
        frozen = freeze_dataset(dataset, frozen_on=today)
    except FreezeRefused as refused:
        for problem in refused.problems:
            print(f"PROBLEM  {problem}", file=out)
        print("freeze refused: nothing was written", file=out)
        return 1
    write_frozen(dataset_dir, frozen)
    splits = [document.split for document in frozen.freeze.documents.values()]
    print(
        f"frozen {frozen.manifest.dataset} {frozen.manifest.version}: "
        f"{splits.count('dev')} dev, {splits.count('test')} test. Review the diff and commit "
        f"{FREEZE_FILE} and {MANIFEST_FILE} together.",
        file=out,
    )
    return 0


def _harness(args: argparse.Namespace, io: Io) -> int:
    if args.command == "run":
        return runs.run_command(
            io,
            dataset=args.dataset,
            corpus=args.corpus,
            extraction=args.extraction,
            split=args.split,
            live=args.live,
            max_usd=args.cap,
            cap_text=args.cap_text,
            reason=args.test_rerun_reason,
            fixtures=args.fixtures,
        )[1]
    return runs.summarize_command(io, args.run_dir)


def harness_main(argv: Sequence[str] | None, io: Io) -> int:
    """The harness commands, with `io` (tests pass their own)."""
    parser = _parser()
    args = parser.parse_args(argv)
    _checked_args(parser, args)
    try:
        return _harness(args, io)
    except (
        Refused,
        DatasetError,
        CorpusError,
        ExtractionConfigError,
        GitError,
        ValidationError,
        ValueError,
        OSError,
    ) as error:
        print(f"{PROG}: {error}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    if args.command not in DATASET_COMMANDS:
        return harness_main(argv, Io())
    dataset_dir: Path = args.dataset or find_repo_root(Path.cwd()) / DEFAULT_DATASET
    try:
        dataset = read_dataset(dataset_dir, args.corpus, args.extraction)
    except (DatasetError, ExtractionConfigError) as error:
        print(f"error: {error}", file=out)
        return 2
    if args.command == "check":
        return _check(dataset, out)
    if args.command == "report":
        unknown = sorted(set(args.id) - {row.id for row in dataset.rows})
        if unknown:
            print(f"error: no gold row with id {', '.join(unknown)}", file=out)
            return 2
        out.write(render_report(dataset, counts_only=args.counts, only=args.id))
        return 0
    return _freeze(dataset, dataset_dir, args.today or date.today(), out)
