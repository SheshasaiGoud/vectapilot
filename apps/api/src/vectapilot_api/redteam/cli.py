"""`uv run python -m vectapilot_api.redteam <command>`: check, report on and freeze the `redteam`
suite, and run it (M7 PR 2; D6 (a), D7 (b), D8 (a)).

Run from anywhere inside the repository (the dataset defaults to
`evals/datasets/redteam/v0.1`).

Dataset commands (no model, no network, no database; nothing written but the dataset's own files):

- `check`  — every case, gate label and fixture file valid for the version's stage (what CI
  runs); prints problems naming case ids and fields, never a case's text or a value. Exit 1 on
  any problem
- `report` — counts per subgroup, business, language, surface, route, expectation, obfuscation
  class and label; `--counts` prints the counts alone (the only form an agent runs on the probe),
  the full report adds one line of codes per case
- `freeze` — refused while `check` has a problem or there is no case; otherwise writes
  freeze.json and the manifest (`stage: frozen`, pinned counts). `--refreeze` re-pins a frozen
  version, allowed only until the PR that froze it merges, and says so

Exit 2: a file cannot be read or parsed.

Harness commands (`runs.py`):

- `run [--live --max-usd X]` — every frozen case through `run_case`. Dry by default, into the
  git-ignored `evals/reports/tmp/redteam/<UTC>-fake-redteam/`. `--live` is D7's raw arm: refused
  in PR 2, built in the close-out, shesha's alone, at most $1.00, the cap typed again at a
  terminal; agents never pass it
- `summarize RUN_DIR` — (re)write a folder's summary

Exit codes: 0 done · 1 refused or a problem · 2 unreadable files or arguments.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final, TextIO

from vectapilot_api.evalrun.cli import cap_value
from vectapilot_api.evalset.checks import FreezeRefused
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.files import find_repo_root
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.measure.budget import MAX_USD_CEILING
from vectapilot_api.redteam import runs
from vectapilot_api.redteam.cases import FREEZE_FILE, MANIFEST_FILE
from vectapilot_api.redteam.checks import RedteamDataset, check_dataset, freeze_dataset
from vectapilot_api.redteam.files import DEFAULT_DATASET, read_dataset, write_frozen
from vectapilot_api.redteam.report import render_report

DATASET_COMMANDS: Final = frozenset({"check", "report", "freeze"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=runs.PROG,
        description="Check, report on and freeze the redteam suite; run it. Without --live"
        " everything is a DRY RUN ($0).",
        epilog="--live is shesha's alone (docs/plans/m7-pr-checks.md, PR 9); agents never pass it.",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default {DEFAULT_DATASET})"
    )
    parser.add_argument("--corpus", type=Path, help="the sample businesses (default: manifest's)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every case, gate label and fixture file")
    report = commands.add_parser("report", help="counts, then one line of codes per case")
    report.add_argument("--counts", action="store_true", help="the counts only")
    freeze = commands.add_parser("freeze", help="pin the version (before PR 4's term files)")
    freeze.add_argument("--refreeze", action="store_true", help="re-pin until its PR merges")
    freeze.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    run = commands.add_parser("run", help="every frozen case through run_case (dry)")
    run.add_argument("--live", action="store_true", help="paid calls (shesha only, PR 9)")
    run.add_argument("--max-usd", help=f"the cap in US dollars, at most {MAX_USD_CEILING}")
    summarize = commands.add_parser("summarize", help="(re)write a folder's summary")
    summarize.add_argument("run_dir", type=Path)
    return parser


def _checked_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse argument combinations before anything is read (exit 2, argparse's)."""
    live = getattr(args, "live", False)
    text = getattr(args, "max_usd", None)
    args.cap = None
    if live and text is None:
        parser.error("--live requires --max-usd")
    if text is not None:
        args.cap = cap_value(text)
        if args.cap is None:
            parser.error(f"--max-usd must be plain digits above 0 and at most {MAX_USD_CEILING}")
        if not live:
            parser.error("--max-usd is for --live runs only")


def _check(dataset: RedteamDataset, out: TextIO) -> int:
    result = check_dataset(dataset)
    for problem in result.problems:
        print(f"PROBLEM  {problem}", file=out)
    for warning in result.warnings:
        print(f"warning  {warning}", file=out)
    verdict = "ok" if result.ok else f"{len(result.problems)} problem(s)"
    manifest = dataset.manifest
    print(f"check ({manifest.stage}, {len(dataset.cases)} cases): {verdict}", file=out)
    return 0 if result.ok else 1


def _freeze(
    dataset: RedteamDataset, dataset_dir: Path, today: date, refreeze: bool, out: TextIO
) -> int:
    try:
        frozen = freeze_dataset(dataset, frozen_on=today, refreeze=refreeze)
    except FreezeRefused as refused:
        for problem in refused.problems:
            print(f"PROBLEM  {problem}", file=out)
        print("freeze refused: nothing was written", file=out)
        return 1
    write_frozen(dataset_dir, frozen)
    if frozen.refrozen_from is not None:
        print(
            f"RE-FROZEN {frozen.manifest.version} (frozen before on"
            f" {frozen.refrozen_from.isoformat()}): allowed only until the PR that froze it"
            " merges; report this re-freeze.",
            file=out,
        )
    counts = frozen.manifest.counts
    total = 0 if counts is None else counts.total
    print(
        f"frozen {frozen.manifest.dataset} {frozen.manifest.version}: {total} cases. Review the"
        f" diff and commit {FREEZE_FILE} and {MANIFEST_FILE} together.",
        file=out,
    )
    return 0


def harness_main(argv: Sequence[str] | None) -> int:
    """`run` and `summarize` (`runs.py`)."""
    parser = _parser()
    args = parser.parse_args(argv)
    _checked_args(parser, args)
    try:
        if args.command == "run":
            return runs.run_command(
                dataset=args.dataset, live=args.live, max_usd=args.cap, corpus=args.corpus
            )
        return runs.summarize_command(args.run_dir)
    except (runs.Refused, DatasetError, OSError) as error:
        print(f"{runs.PROG}: {error}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    if args.command not in DATASET_COMMANDS:
        return harness_main(argv)
    dataset_dir: Path = args.dataset or find_repo_root(Path.cwd()) / DEFAULT_DATASET
    try:
        dataset = read_dataset(dataset_dir, args.corpus)
    except (DatasetError, CorpusError) as error:
        print(f"error: {error}", file=out)
        return 2
    if args.command == "check":
        return _check(dataset, out)
    if args.command == "report":
        out.write(render_report(dataset, counts_only=args.counts))
        return 0
    return _freeze(dataset, dataset_dir, args.today or date.today(), args.refreeze, out)
