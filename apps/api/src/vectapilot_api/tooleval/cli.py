"""`uv run python -m vectapilot_api.tooleval <command>`: check, report on and freeze the
`tool_calls` dataset, and measure the agent on it (M5 PR 6, D8).

Run from anywhere inside the repository (the dataset defaults to `evals/datasets/tool_calls/v0.1`).
Dataset commands (no model, no network, no database; nothing written but the dataset's own files):

- `check`  — every scenario valid for the version's stage (what CI runs); prints problems and
  warnings, never edits a scenario. Exit 1 on any problem
- `report` — counts per business, language group, stratum, flag, author and split, and D8's
  minimums; ids and counts only, never text
- `freeze` — refused below D8's minimums or with an unbalanced split; otherwise assigns dev and
  test, writes scenarios.jsonl, freeze.json and the manifest (once per version)

Exit 2: a file cannot be read or parsed. None of these writes, completes or suggests a scenario.

Harness commands (`runs.py`; runbook: docs/plans/m5-pr-checks.md, PR 7):

- `run --split dev|test [--live --max-usd X]` — every scenario of the split through the agent
  (`router=none`), counted from the database; dry by default (a scripted fake model reading
  fixture plans from `tests/`, `--plans`), into the git-ignored `evals/reports/tmp/`
  (`make tool-dry-run`). Needs a frozen dataset, the database at migration 0006 and the owner's
  URL for the sandbox reset
- `summarize RUN_DIR` — (re)write the folder's summary through H2, with shesha's labels if any
- `label-sheet RUN_DIR` — print each reply for shesha's false-success labels (this terminal
  only) and write `labels.jsonl` if missing

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
from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.files import find_repo_root
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.llm_cli import GitError
from vectapilot_api.measure.budget import MAX_USD_CEILING
from vectapilot_api.tooleval import runs
from vectapilot_api.tooleval.checks import ToolCallsDataset, check_dataset, freeze_dataset
from vectapilot_api.tooleval.files import DEFAULT_DATASET, read_dataset, write_frozen
from vectapilot_api.tooleval.harness import PROG, Io, Refused
from vectapilot_api.tooleval.report import render_report
from vectapilot_api.tooleval.scenarios import FREEZE_FILE, MANIFEST_FILE, SCENARIOS_FILE
from vectapilot_api.tools.config import ToolsConfigError

WAY_OUT: Final = (
    "way out: add or remove one scenario and run freeze again; never change the seed or the"
    " percentage (README, 'Split rule and freeze')"
)
DATASET_COMMANDS: Final = frozenset({"check", "report", "freeze"})
_RERUN_REASON: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Check, report on and freeze the tool_calls dataset; measure the agent on it."
        " Without --live everything is a DRY RUN ($0).",
        epilog="--live is shesha's alone (docs/plans/m5-pr-checks.md, PR 7); agents never pass it.",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default {DEFAULT_DATASET})"
    )
    parser.add_argument("--corpus", type=Path, help="the business manifests (default: manifest's)")
    parser.add_argument("--sandbox", type=Path, help="the tool configuration (default: manifest's)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every scenario for the version's stage")
    commands.add_parser("report", help="print the counts and D8's minimums")
    freeze = commands.add_parser("freeze", help="assign dev and test and pin the version")
    freeze.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    run = commands.add_parser("run", help="every scenario of a split through the agent")
    run.add_argument("--split", choices=("dev", "test"), required=True)
    run.add_argument("--live", action="store_true", help="paid calls (shesha only)")
    run.add_argument("--max-usd", help=f"the cap in US dollars, at most {MAX_USD_CEILING}")
    run.add_argument("--test-rerun-reason", help="a slug, recorded in the test-run ledger")
    run.add_argument("--plans", type=Path, help="the dry run's fixture plans (default: tests')")
    for name, text in (
        ("summarize", "(re)write a folder's summary through H2"),
        ("label-sheet", "print the replies for shesha's labels; write labels.jsonl if missing"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument("run_dir", type=Path)
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
    if live and getattr(args, "plans", None) is not None:
        parser.error("--plans is for dry runs only (a live run has no scripted model)")
    reason = getattr(args, "test_rerun_reason", None)
    if reason is not None and (_RERUN_REASON.fullmatch(reason) is None or len(reason) > 80):
        parser.error("--test-rerun-reason must be a short lowercase slug (a-z, 0-9, -)")


def _print_result(result: CheckResult, out: TextIO) -> None:
    for problem in result.problems:
        print(f"PROBLEM  {problem}", file=out)
    for warning in result.warnings:
        print(f"warning  {warning}", file=out)


def _check(dataset: ToolCallsDataset, out: TextIO) -> int:
    result = check_dataset(dataset)
    _print_result(result, out)
    verdict = "ok" if result.ok else f"{len(result.problems)} problem(s)"
    print(
        f"check ({dataset.manifest.stage}, {len(dataset.scenarios)} scenarios): {verdict}",
        file=out,
    )
    return 0 if result.ok else 1


def _freeze(dataset: ToolCallsDataset, dataset_dir: Path, today: date, out: TextIO) -> int:
    try:
        frozen = freeze_dataset(dataset, frozen_on=today)
    except FreezeRefused as refused:
        for problem in refused.problems:
            print(f"PROBLEM  {problem}", file=out)
        if refused.unbalanced:
            print(f"PROBLEM  {WAY_OUT}", file=out)
        print("freeze refused: nothing was written", file=out)
        return 1
    write_frozen(dataset_dir, frozen)
    splits = [s.split for s in frozen.scenarios]
    print(
        f"frozen {frozen.manifest.dataset} {frozen.manifest.version}: "
        f"{splits.count('dev')} dev, {splits.count('test')} test. Review the diff and commit "
        f"{SCENARIOS_FILE}, {FREEZE_FILE} and {MANIFEST_FILE} together.",
        file=out,
    )
    return 0


def _harness(args: argparse.Namespace, io: Io, out: TextIO) -> int:
    if args.command == "run":
        return runs.run_command(
            io,
            dataset=args.dataset,
            corpus=args.corpus,
            sandbox=args.sandbox,
            split=args.split,
            live=args.live,
            max_usd=args.cap,
            cap_text=args.cap_text,
            reason=args.test_rerun_reason,
            plans=args.plans,
        )[1]
    if args.command == "summarize":
        return runs.summarize_command(io, args.run_dir)
    return runs.label_sheet_command(
        io,
        args.run_dir,
        dataset=args.dataset,
        corpus=args.corpus,
        sandbox=args.sandbox,
        out=out,
    )


def harness_main(argv: Sequence[str] | None, io: Io, out: TextIO | None = None) -> int:
    """The harness commands, with `io` (tests pass their own)."""
    parser = _parser()
    args = parser.parse_args(argv)
    _checked_args(parser, args)
    try:
        return _harness(args, io, out or sys.stdout)
    except (
        Refused,
        DatasetError,
        CorpusError,
        ToolsConfigError,
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
        return harness_main(argv, Io(), out)
    dataset_dir: Path = args.dataset or find_repo_root(Path.cwd()) / DEFAULT_DATASET
    try:
        dataset = read_dataset(dataset_dir, args.corpus, args.sandbox)
    except (DatasetError, CorpusError, ToolsConfigError) as error:
        print(f"error: {error}", file=out)
        return 2
    if args.command == "check":
        return _check(dataset, out)
    if args.command == "report":
        out.write(render_report(dataset))
        return 0
    return _freeze(dataset, dataset_dir, args.today or date.today(), out)
