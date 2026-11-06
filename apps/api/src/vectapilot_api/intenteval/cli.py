"""`uv run python -m vectapilot_api.intenteval <command>`: check, report on, sample and freeze the
`intents` dataset (M4 PR 1).

Run from anywhere inside the repository (the dataset defaults to `evals/datasets/intents/v0.1`).
Commands:

- `check`  — every row valid for the version's stage (what CI runs); prints problems and
  warnings, never edits a row. Exit 1 on any problem
- `report` — counts per intent, business, language group, file and author, the eval splits,
  D3's minimums and the verification state; ids and counts only, never text
- `sample` — the seeded, stratified verification sample of the training rows, written to
  `verification-sample.txt` (D3; the list shesha verifies in PR 5 step 2)
- `freeze` — refused below D3's minimums, with an unverified sample or an unbalanced split;
  otherwise assigns dev and test, writes eval.jsonl, freeze.json and the manifest (once per
  version)

Exit 2: a file cannot be read or parsed. Nothing here calls a model, a network or a database, and
nothing writes, completes or suggests a row.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Final, TextIO

from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.evalset.corpus import CorpusError
from vectapilot_api.evalset.files import find_repo_root
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.checks import IntentDataset, check_dataset, freeze_dataset
from vectapilot_api.intenteval.files import (
    DEFAULT_DATASET,
    read_dataset,
    write_frozen,
    write_sample,
)
from vectapilot_api.intenteval.report import render_report
from vectapilot_api.intenteval.rows import EVAL_FILE, FREEZE_FILE, MANIFEST_FILE, SAMPLE_FILE
from vectapilot_api.intenteval.sample import draw_sample

WAY_OUT: Final = (
    "way out: add or remove one eval row and run freeze again; never change the seed or the "
    "percentage (README, 'Split rule and freeze')"
)
"""Printed when the only problem is the split's balance (`split.balance_problems`)."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.intenteval",
        description="Check, report on, sample and freeze the intents dataset (no model calls).",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default: {DEFAULT_DATASET})"
    )
    parser.add_argument(
        "--corpus", type=Path, help="the business manifests' folder (default: the manifest's)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate every row for the version's stage")
    commands.add_parser("report", help="print the counts, minimums and verification state")
    commands.add_parser("sample", help=f"write the verification sample to {SAMPLE_FILE}")
    freeze = commands.add_parser("freeze", help="assign dev and test and pin the version")
    freeze.add_argument("--today", type=date.fromisoformat, help=argparse.SUPPRESS)
    return parser


def _print_result(result: CheckResult, out: TextIO) -> None:
    for problem in result.problems:
        print(f"PROBLEM  {problem}", file=out)
    for warning in result.warnings:
        print(f"warning  {warning}", file=out)


def _check(dataset: IntentDataset, out: TextIO) -> int:
    result = check_dataset(dataset)
    _print_result(result, out)
    verdict = "ok" if result.ok else f"{len(result.problems)} problem(s)"
    print(
        f"check ({dataset.manifest.stage}, {len(dataset.train)} train, "
        f"{len(dataset.eval_rows)} eval): {verdict}",
        file=out,
    )
    return 0 if result.ok else 1


def _sample(dataset: IntentDataset, dataset_dir: Path, out: TextIO) -> int:
    if dataset.manifest.stage != "collecting":
        print(f"{dataset.manifest.version} is frozen: its sample is fixed", file=out)
        return 1
    if not dataset.train:
        print("no training rows yet: nothing to sample, nothing written", file=out)
        return 1
    ids = draw_sample(dataset.train, dataset.manifest.verification)
    write_sample(dataset_dir, ids)
    print(
        f"wrote {SAMPLE_FILE}: {len(ids)} of {len(dataset.train)} training rows (at least "
        f"{dataset.manifest.verification.percent} % per intent, business and language group). "
        "Commit it; shesha verifies these rows after every eval row is committed.",
        file=out,
    )
    return 0


def _freeze(dataset: IntentDataset, dataset_dir: Path, today: date, out: TextIO) -> int:
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
    splits = [r.split for r in frozen.eval_rows]
    print(
        f"frozen {frozen.manifest.dataset} {frozen.manifest.version}: "
        f"{splits.count('dev')} dev, {splits.count('test')} test. Review the diff and commit "
        f"{EVAL_FILE}, {FREEZE_FILE} and {MANIFEST_FILE} together.",
        file=out,
    )
    return 0


def main(argv: Sequence[str] | None = None, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    dataset_dir: Path = args.dataset or find_repo_root(Path.cwd()) / DEFAULT_DATASET
    try:
        dataset = read_dataset(dataset_dir, args.corpus)
    except (DatasetError, CorpusError) as error:
        print(f"error: {error}", file=out)
        return 2
    if args.command == "check":
        return _check(dataset, out)
    if args.command == "report":
        out.write(render_report(dataset))
        return 0
    if args.command == "sample":
        return _sample(dataset, dataset_dir, out)
    return _freeze(dataset, dataset_dir, args.today or date.today(), out)
