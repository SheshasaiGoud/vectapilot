"""`uv run python -m vectapilot_api.intenteval <command>`: check, report on, sample and freeze the
`intents` dataset (M4 PR 1), and measure the router on it (M4 PR 4).

Run from anywhere inside the repository (the dataset defaults to `evals/datasets/intents/v0.1`).
Dataset commands (no model, no network, nothing written but the dataset's own files):

- `check`  — every row valid for the version's stage (what CI runs); prints problems and
  warnings, never edits a row. Exit 1 on any problem
- `report` — counts per intent, business, language group, file and author, the eval splits,
  D3's minimums and the verification state; ids and counts only, never text
- `sample` — the seeded, stratified verification sample of the training rows, written to
  `verification-sample.txt` (D3; the list shesha verifies in PR 5 step 2)
- `freeze` — refused below D3's minimums, with an unverified sample or an unbalanced split;
  otherwise assigns dev and test, writes eval.jsonl, freeze.json and the manifest (once per
  version)

Exit 2: a file cannot be read or parsed. These call no model, network or database, and none
writes, completes or suggests a row.

Harness commands (M4 PR 4; `steps.py`, `runs.py`; runbook: docs/plans/m4-pr-checks.md, PR 5):

- `embed [--live --max-usd X]` — query vectors for every row; dry by default (fake embedder, $0)
- `train --embed DIR` · `calibrate MODEL_DIR` · `thresholds MODEL_DIR` — the classifier, T on
  dev, D8's tau and sigma on dev ($0)
- `pin MODEL_DIR` — the test-run ledger's pin (shesha, at a terminal)
- `run --split dev|test --mode classifier --model MODEL_DIR` ($0) and `run --split dev|test
  --mode llm [--live --max-usd X]` (dry unless `--live`)
- `summarize RUN_DIR` (rewrite a summary) · `summarize --classifier DIR --llm DIR` (the replayed
  hybrid and the comparison)
- `dry-run` — every step on dev, dry, into `evals/reports/tmp/` (`make intent-dry-run`)

`--live` and every test-split step are shesha's alone; agents never pass `--live` and never run
test. Exit codes: 0 complete · 1 refused or failed · 3 partial.
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
from vectapilot_api.intenteval import hybrid, runs, steps
from vectapilot_api.intenteval.checks import IntentDataset, check_dataset, freeze_dataset
from vectapilot_api.intenteval.files import (
    DEFAULT_DATASET,
    read_dataset,
    write_frozen,
    write_sample,
)
from vectapilot_api.intenteval.harness import PROG, Io, Refused
from vectapilot_api.intenteval.report import render_report
from vectapilot_api.intenteval.rows import EVAL_FILE, FREEZE_FILE, MANIFEST_FILE, SAMPLE_FILE
from vectapilot_api.intenteval.runinfo import read_run
from vectapilot_api.intenteval.sample import draw_sample
from vectapilot_api.intents.classifier import ClassifierError
from vectapilot_api.llm_cli import GitError
from vectapilot_api.measure.budget import MAX_USD_CEILING

WAY_OUT: Final = (
    "way out: add or remove one eval row and run freeze again; never change the seed or the "
    "percentage (README, 'Split rule and freeze')"
)
"""Printed when the only problem is the split's balance (`split.balance_problems`)."""

DATASET_COMMANDS: Final = frozenset({"check", "report", "sample", "freeze"})
_RERUN_REASON: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vectapilot_api.intenteval",
        description="Check, report on, sample and freeze the intents dataset; measure the router on"
        " it. Without --live everything is a DRY RUN ($0).",
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
    _harness_commands(commands)
    return parser


def _paid(command: argparse.ArgumentParser) -> None:
    command.add_argument("--live", action="store_true", help="paid calls (shesha only)")
    command.add_argument("--max-usd", help=f"the cap in US dollars, at most {MAX_USD_CEILING}")


def _harness_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    embed = commands.add_parser("embed", help="query vectors for every row (dry by default)")
    _paid(embed)
    train = commands.add_parser("train", help="fit the classifier on the training rows ($0)")
    train.add_argument("--embed", type=Path, required=True, help="an embed folder")
    for name, text in (("calibrate", "fit T on dev"), ("thresholds", "D8's tau and sigma on dev")):
        step = commands.add_parser(name, help=f"{text} ($0)")
        step.add_argument("model_dir", type=Path)
    pin = commands.add_parser("pin", help="pin the model for every test run (shesha)")
    pin.add_argument("model_dir", type=Path)
    run = commands.add_parser("run", help="one mode on one split; writes a run folder")
    run.add_argument("--split", choices=("dev", "test"), required=True)
    run.add_argument("--mode", choices=("classifier", "llm"), required=True)
    run.add_argument("--model", type=Path, help="the model folder (classifier mode)")
    run.add_argument("--test-rerun-reason", help="a slug, recorded in the test-run ledger")
    _paid(run)
    summarize = commands.add_parser("summarize", help="a folder's summary, or the hybrid")
    summarize.add_argument("run_dir", type=Path, nargs="?")
    summarize.add_argument("--classifier", type=Path, help="a classifier run folder")
    summarize.add_argument("--llm", type=Path, help="an LLM run folder of the same split")
    commands.add_parser("dry-run", help="every step on dev, dry, into evals/reports/tmp/")


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
    reason = getattr(args, "test_rerun_reason", None)
    if reason is not None and (_RERUN_REASON.fullmatch(reason) is None or len(reason) > 80):
        parser.error("--test-rerun-reason must be a short lowercase slug (a-z, 0-9, -)")
    if args.command == "run":
        if args.mode == "classifier" and (args.model is None or live):
            parser.error("classifier mode needs --model and is never --live (it costs $0)")
        if args.mode == "llm" and args.model is not None:
            parser.error("--model is for classifier mode")
    if args.command == "summarize":
        hybrid = args.classifier is not None or args.llm is not None
        if hybrid == (args.run_dir is not None) or (hybrid and None in (args.classifier, args.llm)):
            parser.error("summarize takes RUN_DIR, or both --classifier and --llm")


def _harness(args: argparse.Namespace, io: Io) -> int:
    if args.command == "embed":
        folder = steps.embed(
            io, dataset=args.dataset, live=args.live, max_usd=args.cap, cap_text=args.cap_text
        )
        return runs.EXIT_PARTIAL if read_run(folder).status == "partial" else runs.EXIT_OK
    if args.command == "train":
        steps.train(io, dataset=args.dataset, embed_folder=args.embed)
        return 0
    if args.command == "calibrate":
        steps.calibrate(io, dataset=args.dataset, model_folder=args.model_dir)
        return 0
    if args.command == "thresholds":
        steps.thresholds(io, dataset=args.dataset, model_folder=args.model_dir)
        return 0
    if args.command == "pin":
        steps.pin(io, dataset=args.dataset, model_folder=args.model_dir)
        return 0
    if args.command == "run" and args.mode == "classifier":
        return runs.run_classifier(
            io,
            dataset=args.dataset,
            model_folder=args.model,
            split=args.split,
            reason=args.test_rerun_reason,
        )[1]
    if args.command == "run":
        return runs.run_llm(
            io,
            dataset=args.dataset,
            split=args.split,
            live=args.live,
            max_usd=args.cap,
            cap_text=args.cap_text,
            reason=args.test_rerun_reason,
        )[1]
    if args.command == "summarize" and args.run_dir is not None:
        return hybrid.summarize_folder(io, dataset=args.dataset, folder=args.run_dir)
    if args.command == "summarize":
        return hybrid.summarize_hybrid(
            io, dataset=args.dataset, classifier_folder=args.classifier, llm_folder=args.llm
        )[1]
    return hybrid.dry_run(io, dataset=args.dataset)


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
        GitError,
        ClassifierError,
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
