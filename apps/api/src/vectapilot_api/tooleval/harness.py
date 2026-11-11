"""What every tool-call harness command shares (M5 PR 6): the outside world (`Io`), the refusals,
loading a checked dataset, the settings rules, the test discipline and M5's spend lines.

**Dry or real** (scoring rules, "Dry or real; test once"): a run is live only with `--live
--max-usd X` (at most $1.00, the cap typed again at a terminal: shesha's alone, agents never pass
it), refused unless every manifest says synthetic (the dataset's, each business's corpus manifest,
`tools.json` and `seed.json`). Without `--live` the model is the scripted fake (`plans.py`), the
folder goes to the git-ignored `evals/reports/tmp/`, and its summary starts "DRY RUN — plumbing
only, never a result".

**The test discipline** (`check_test_run`, `ledger.py`): test needs a frozen dataset, a live run
(dry runs never touch test), a clean working tree (so "committed in HEAD" names the code), the
ledger committed as on disk, at most one ledgered run per config unless `--test-rerun-reason`, a
committed, complete live dev run with the same fingerprint, and the person at a terminal typing
the confirmation. Every test run is shesha's.

**The sandbox** is reset as the owner (`VECTAPILOT_MIGRATION_DATABASE_URL`), only in local, test
and ci (`tools.sandbox.SANDBOX_ENVIRONMENTS`); the agent runs as the app role (`Database`, its
role check included), exactly as the API does.
"""

import hashlib
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Final, TextIO

from vectapilot_api.agent.loop import run_turn
from vectapilot_api.agent.types import TurnLoop
from vectapilot_api.config import Settings
from vectapilot_api.evalrun.budget import CostCap
from vectapilot_api.evalrun.cli import load_settings, owner_url
from vectapilot_api.evalrun.ledger import LedgerEntry, parse_ledger
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalrun.spend import (
    HeadFile,
    HeadPaths,
    MilestoneSpend,
    SpendError,
    read_head_paths,
    read_head_ref,
    spend_refusal,
    spend_text,
)
from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, load_corpus_manifest
from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.gateway.gateway import build_provider
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.llm_cli import GitError, GitInfo, dataset_at_commit, read_git, read_head_file
from vectapilot_api.tooleval.checks import ToolCallsDataset, check_dataset
from vectapilot_api.tooleval.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.tooleval.ledger import LEDGER_PATH, runs_of
from vectapilot_api.tooleval.metrics import H2_TOOL_METRICS, ToolMetrics
from vectapilot_api.tooleval.runinfo import RUN_FILE, TOOL_REPORTS, read_run
from vectapilot_api.tooleval.scenarios import FREEZE_FILE, MANIFEST_FILE, SCENARIOS_FILE, Scenario
from vectapilot_api.tooleval.spend import m5_spend
from vectapilot_api.tools.sandbox import SANDBOX_ENVIRONMENTS

PROG: Final = "python -m vectapilot_api.tooleval"
RUNBOOK: Final = "docs/plans/m5-pr-checks.md, PR 7"
TEST_WORDS: Final = "test tool_calls"


class Refused(Exception):
    """The command cannot run as asked. The message names ids and rules, never text."""


@dataclass(frozen=True)
class Io:
    """What the commands read from outside: tests pass their own, so no test runs git, reads the
    shell's environment, waits for a terminal or builds a paid client."""

    git: Callable[[], GitInfo] = read_git
    head_file: HeadFile = read_head_file
    head_paths: HeadPaths = read_head_paths
    head_ref: Callable[[Path], str] = read_head_ref
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.perf_counter
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout_is_terminal: Callable[[], bool] = field(default=lambda: sys.stdout.isatty())
    load_settings: Callable[[bool], Settings] = load_settings
    owner_url: Callable[[], str | None] = owner_url
    build_provider: Callable[[Settings], Provider] = build_provider
    """Live only; a dry run always uses the scripted `PlanProvider`."""
    metrics: ToolMetrics = H2_TOOL_METRICS
    loop: TurnLoop = run_turn
    """H1. Tests pass stand-ins; the real one raises NotImplementedError until shesha writes it."""


@dataclass(frozen=True)
class Loaded:
    """A checked dataset and what every command derives from it."""

    root: Path
    directory: Path
    corpus_dir: Path
    sandbox_dir: Path
    dataset: ToolCallsDataset
    freeze_sha256: str | None
    committed: bool
    """Every dataset file read is byte for byte the one in HEAD."""

    @property
    def frozen(self) -> bool:
        return self.dataset.manifest.stage == "frozen" and self.freeze_sha256 is not None

    @property
    def version(self) -> str:
        return self.dataset.manifest.version

    def split_rows(self, split: str) -> tuple[Scenario, ...]:
        return tuple(s for s in self.dataset.scenarios if s.split == split)


def load(
    dataset_arg: Path | None,
    corpus_arg: Path | None,
    sandbox_arg: Path | None,
    git: GitInfo,
    head_file: HeadFile,
    *,
    live: bool,
) -> Loaded:
    """Read and check the dataset (placeholders only in runs that are not live: a TEST-ONLY copy
    can never reach a paid call or a committed folder)."""
    directory = dataset_arg if dataset_arg is not None else git.root / DEFAULT_DATASET
    root = repo_root_of(directory)
    dataset = read_dataset(directory, corpus_arg, sandbox_arg)
    checked = check_dataset(dataset, allow_placeholders=not live)
    if not checked.ok:
        for problem in checked.problems:
            print(f"PROBLEM  {problem}", file=sys.stderr)
        raise Refused(f"the dataset fails its check ({len(checked.problems)} problem(s))")
    names = (MANIFEST_FILE, SCENARIOS_FILE, FREEZE_FILE)
    files = {
        directory / n: (directory / n).read_bytes() for n in names if (directory / n).is_file()
    }
    freeze = directory / FREEZE_FILE
    return Loaded(
        root=git.root,
        directory=directory,
        corpus_dir=corpus_arg or root / dataset.manifest.corpus,
        sandbox_dir=sandbox_arg or root / dataset.manifest.sandbox,
        dataset=dataset,
        freeze_sha256=hashlib.sha256(freeze.read_bytes()).hexdigest() if freeze.is_file() else None,
        committed=dataset_at_commit(git.root, files, head_file),
    )


def require_frozen(loaded: Loaded) -> str:
    if not loaded.frozen or loaded.freeze_sha256 is None:
        raise Refused(
            "a run needs a frozen dataset: the splits exist only after `freeze` (README, 'Split"
            " rule and freeze')"
        )
    return loaded.freeze_sha256


def synthetic_everywhere(loaded: Loaded) -> bool:
    """Every manifest says `synthetic: true`: the dataset's, each business's corpus manifest, its
    `tools.json` and its `seed.json` (their parsers refuse anything else today; checked again so
    the guard holds if one is relaxed)."""
    found = [loaded.dataset.manifest.synthetic is True]
    for business_id in loaded.dataset.manifest.businesses:
        path = loaded.corpus_dir / business_id / CORPUS_MANIFEST_FILE
        corpus = load_corpus_manifest(path.read_text("utf-8"), where=business_id)
        tools = loaded.dataset.sandbox[business_id]
        found += [corpus.synthetic is True, tools.config.synthetic, tools.seed.synthetic]
    return all(found)


def check_settings(settings: Settings, *, live: bool) -> None:
    if settings.environment not in SANDBOX_ENVIRONMENTS:
        raise Refused("the tool-call harness resets the sandbox: it runs only in local, test or ci")
    if live and settings.llm_provider == "fake":
        raise Refused(
            f"--live needs the paid LLM provider in Settings, with its key in the process"
            f" environment only ({RUNBOOK}); a fake provider's numbers are never results"
        )
    if not live and settings.llm_provider != "fake":
        raise Refused("a run without --live uses the fake provider only: no paid call")


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def in_head(root: Path, path: Path, head_file: HeadFile) -> bool:
    """`path` is inside the repository and byte for byte the file in HEAD."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        return False
    return head_file(root, PurePosixPath(relative(root, path))) == resolved.read_bytes()


# ── Confirmation, the ledger, the test discipline ────────────────────────────────────────────


def confirm_test(io: Io) -> bool:
    """True only if `TEST_WORDS` is typed again exactly, with stdin and stdout at a terminal (a
    guard against mistakes: agents never run test)."""
    if not io.stdin.isatty() or not io.stdout_is_terminal():
        print("A test run is confirmed at a terminal (stdin and stdout).", file=sys.stderr)
        return False
    print(
        f"This uses the test split, once per config. Type '{TEST_WORDS}' exactly and press"
        " Enter; anything else cancels: ",
        end="",
        flush=True,
    )
    try:
        answer = io.stdin.readline()
    except KeyboardInterrupt:
        return False
    return answer.strip() == TEST_WORDS


def committed_ledger(io: Io, root: Path) -> list[LedgerEntry]:
    """The ledger, refused unless the file on disk is the one in HEAD (none in both is fine)."""
    path = root / LEDGER_PATH
    working = path.read_bytes() if path.is_file() else None
    if working != io.head_file(root, PurePosixPath(LEDGER_PATH.as_posix())):
        raise Refused(
            f"{LEDGER_PATH} is not as committed in HEAD: commit the test-run ledger (with the"
            " folders it names) first, so no earlier entry can be lost"
        )
    return parse_ledger(working.decode("utf-8")) if working is not None else []


def committed_live_dev_run(io: Io, root: Path, version: str, fingerprint: str) -> str | None:
    """A complete live dev run with this fingerprint whose run.json is in HEAD, or None."""
    for path in sorted((root / TOOL_REPORTS / version).glob(f"*-live-agent-dev/{RUN_FILE}")):
        try:
            info = read_run(path.parent)
        except ValueError:
            continue
        if (
            info.fingerprint_sha256 == fingerprint
            and info.status == "complete"
            and not info.dry_run
            and in_head(root, path, io.head_file)
        ):
            return info.run_id
    return None


def check_test_run(
    io: Io,
    git: GitInfo,
    loaded: Loaded,
    *,
    fingerprint: str,
    live: bool,
    reason: str | None,
) -> int:
    """Refuse a test run the discipline does not allow (module docstring); the number of earlier
    ledgered runs of this config otherwise."""
    freeze = require_frozen(loaded)
    if not live:
        raise Refused("dry runs are refused on the test split: rehearse on dev")
    if git.uncommitted_changes:
        raise Refused("a test run needs a clean working tree: commit first, so HEAD names the code")
    entries = committed_ledger(io, git.root)
    name = loaded.dataset.manifest.dataset
    earlier = runs_of(entries, name, loaded.version, freeze, fingerprint)
    if earlier and reason is None:
        raise Refused(
            f"this config already has {len(earlier)} test run(s) on this frozen version (first:"
            f" {earlier[0].run_id}): the test split runs once per config; a rerun needs"
            " --test-rerun-reason, recorded in the ledger"
        )
    if committed_live_dev_run(io, git.root, loaded.version, fingerprint) is None:
        raise Refused(
            "no complete live dev run with this fingerprint is committed in HEAD: run dev first"
        )
    if not confirm_test(io):
        raise Refused("not confirmed")
    return len(earlier)


# ── M5's spend ───────────────────────────────────────────────────────────────────────────────


def spend_lines(io: Io, git: GitInfo, why: str, cap: Decimal) -> str:
    """Count M5's spend from HEAD and refuse a cap above what is left (`spend.py`)."""
    try:
        counted: MilestoneSpend = m5_spend(git.root, io.head_paths, io.head_file)
    except (SpendError, GitError) as error:
        raise Refused(f"M5's spend cannot be counted: {error}") from None
    try:
        head = f"HEAD ({io.head_ref(git.root)})"
    except GitError:
        head = "HEAD"
    counted = MilestoneSpend(counted.counted, counted.uncommitted, counted.rules, head)
    refusal = spend_refusal(counted, cap)
    if refusal is not None:
        print(spend_text(counted, why))
        raise Refused(refusal)
    return spend_text(counted, why)


def cost_info(cap: CostCap) -> CostInfo:
    return CostInfo(
        cap_usd=cap.cap,
        embedding_usd=cap.embedding_usd,
        llm_usd=cap.llm_usd,
        total_usd=cap.total,
        llm_cost_complete=cap.llm_cost_complete,
        embedding_requests=cap.embedding_requests,
        llm_calls=cap.llm_calls,
    )
