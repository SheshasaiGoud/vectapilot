"""What every intent-harness command shares (M4 PR 4): the outside world (`Io`), the refusals,
loading a checked dataset, deciding dry or real, the test discipline and M4's spend lines.

**Dry or real, decided by the vectors** (plan, "Dry or real"; `embed_realness`): an embed folder
is real only when it sits under `evals/reports/intents/<version>/`, is named `-live-embed`, says
`dry_run: false` and `complete`, and its run.json and cache are in HEAD byte for byte. Everything
downstream (model, classifier, hybrid) inherits that, whatever the flags. An LLM run is live only
with `--live`.

**The test discipline** (`check_test_run`, `ledger.py`): test needs a frozen dataset, a clean
working tree (so "committed in HEAD" means the code too), the ledger committed as on disk, the
version's pin, a real run (dry runs never touch test), at most one ledgered run per mode unless
`--test-rerun-reason`, and the person at a terminal typing the confirmation: every test run is
shesha's, $0 ones included, and agents never run test. An LLM test run also needs a committed,
complete live LLM dev run with the same fingerprint.
"""

import hashlib
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Final, TextIO

from vectapilot_api.config import Settings
from vectapilot_api.embeddings.base import Embedder, EmbeddingSpec
from vectapilot_api.embeddings.factory import build_embedder
from vectapilot_api.evalrun.budget import CostCap
from vectapilot_api.evalrun.cli import load_settings
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
from vectapilot_api.intenteval.checks import IntentDataset, business_policies, check_dataset
from vectapilot_api.intenteval.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.intenteval.ledger import (
    LEDGER_PATH,
    LedgerMode,
    PinEntry,
    RunEntry,
    parse_entries,
    pin_for,
    runs_of,
)
from vectapilot_api.intenteval.llmrun import Router
from vectapilot_api.intenteval.metrics import H2_INTENT_METRICS, IntentMetrics
from vectapilot_api.intenteval.rows import (
    EVAL_FILE,
    FREEZE_FILE,
    MANIFEST_FILE,
    SAMPLE_FILE,
    TRAIN_FILE,
    EvalRow,
)
from vectapilot_api.intenteval.runinfo import (
    INTENT_REPORTS,
    RUN_FILE,
    IntentRunInfo,
)
from vectapilot_api.intenteval.spend import m4_spend
from vectapilot_api.intents.decide import route_message
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.llm_cli import GitError, GitInfo, dataset_at_commit, read_git, read_head_file

PROG: Final = "python -m vectapilot_api.intenteval"
RUNBOOK: Final = "docs/plans/m4-pr-checks.md, PR 5"


class Refused(Exception):
    """The command cannot run as asked. The message names ids and rules, never text."""


def _build_embedder(settings: Settings, spec: EmbeddingSpec) -> Embedder:
    return build_embedder(settings)


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
    build_embedder: Callable[[Settings, EmbeddingSpec], Embedder] = _build_embedder
    """Live only; a dry run always uses the fake embedder (`steps.embed`)."""
    build_provider: Callable[[Settings], Provider] = build_provider
    """Live only; a dry LLM run always uses `DryRunRouterProvider`."""
    metrics: IntentMetrics = H2_INTENT_METRICS
    router: Router = route_message
    """H1. Tests pass stand-ins; the real one raises NotImplementedError until shesha writes it."""


# ── The dataset ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Loaded:
    """A checked dataset and what every command derives from it."""

    root: Path
    directory: Path
    dataset: IntentDataset
    policies: Mapping[str, RoutePolicy]
    freeze_sha256: str | None
    committed: bool
    """Every dataset file read is byte for byte the one in HEAD."""

    @property
    def frozen(self) -> bool:
        return self.dataset.manifest.stage == "frozen" and self.freeze_sha256 is not None

    @property
    def version(self) -> str:
        return self.dataset.manifest.version

    def split_rows(self, split: str) -> tuple[EvalRow, ...]:
        return tuple(r for r in self.dataset.eval_rows if r.split == split)


def dataset_dir(arg: Path | None, git: GitInfo) -> Path:
    return arg if arg is not None else git.root / DEFAULT_DATASET


def load(arg: Path | None, git: GitInfo, head_file: HeadFile, *, live: bool) -> Loaded:
    """Read and check the dataset (placeholders only in runs that are not live: a TEST-ONLY
    copy can never reach a paid call or a committed folder)."""
    directory = dataset_dir(arg, git)
    dataset = read_dataset(directory)
    checked = check_dataset(dataset, allow_placeholders=not live)
    if not checked.ok:
        for problem in checked.problems:
            print(f"PROBLEM  {problem}", file=sys.stderr)
        raise Refused(f"the dataset fails its check ({len(checked.problems)} problem(s))")
    names = [MANIFEST_FILE, TRAIN_FILE, EVAL_FILE, FREEZE_FILE, SAMPLE_FILE]
    paths = [directory / name for name in names if (directory / name).is_file()]
    files = {path: path.read_bytes() for path in paths}
    freeze = directory / FREEZE_FILE
    return Loaded(
        root=git.root,
        directory=directory,
        dataset=dataset,
        policies=business_policies(dataset),
        freeze_sha256=hashlib.sha256(freeze.read_bytes()).hexdigest() if freeze.is_file() else None,
        committed=dataset_at_commit(git.root, files, head_file),
    )


def require_frozen(loaded: Loaded, what: str) -> str:
    if not loaded.frozen or loaded.freeze_sha256 is None:
        raise Refused(
            f"{what} needs a frozen dataset: dev must exist before T, tau and sigma, and every"
            " folder pins the freeze hash (README, 'Split rule and freeze')"
        )
    return loaded.freeze_sha256


def corpus_synthetic(loaded: Loaded) -> bool:
    """Every business manifest says `synthetic: true` (the parser refuses anything else today;
    checked again so the guard holds if it is relaxed)."""
    corpus = repo_root_of(loaded.directory) / loaded.dataset.manifest.corpus
    found = []
    for business_id in loaded.dataset.manifest.businesses:
        path = corpus / business_id / CORPUS_MANIFEST_FILE
        manifest = load_corpus_manifest(path.read_text("utf-8"), where=business_id)
        found.append(manifest.synthetic)
    return all(found)


def check_settings(settings: Settings, *, live: bool, needs: str) -> None:
    """`needs` is `llm` or `embeddings`: the provider a live run of this command pays."""
    if settings.environment == "production":
        raise Refused("the intent harness never runs with environment production")
    provider = settings.llm_provider if needs == "llm" else settings.embeddings_provider
    if live and provider == "fake":
        raise Refused(
            f"--live needs the paid {needs} provider in Settings, with its key in the process"
            f" environment only ({RUNBOOK}); a fake provider's numbers are never results"
        )
    if not live and (settings.llm_provider != "fake" or settings.embeddings_provider != "fake"):
        raise Refused("a run without --live uses the fake providers only: no paid call")


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def in_head(root: Path, path: Path, head_file: HeadFile) -> bool:
    """`path` is inside the repository and byte for byte the file in HEAD."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        return False
    return head_file(root, PurePosixPath(relative(root, path))) == resolved.read_bytes()


# ── Dry or real ──────────────────────────────────────────────────────────────────────────────


def embed_realness(
    root: Path, folder: Path, info: IntentRunInfo, cache_file: str, head_file: HeadFile
) -> str | None:
    """None when `folder` is a real embed folder (module docstring), else why it is dry."""
    if info.dry_run:
        return "fake embedder"
    expected = (root / INTENT_REPORTS / info.dataset_version).resolve()
    if folder.resolve().parent != expected or not folder.name.endswith("-live-embed"):
        return "the embed folder is not a -live-embed folder under evals/reports/intents"
    if info.status != "complete":
        return f"the embed folder is {info.status}"
    for name in (RUN_FILE, cache_file):
        if not in_head(root, folder / name, head_file):
            return f"the embed folder's {name} is not committed in HEAD as on disk"
    return None


# ── Confirmation, the ledger, the test discipline ────────────────────────────────────────────


def confirm_test(io: Io, words: str) -> bool:
    """True only if `words` is typed again exactly, with stdin and stdout at a terminal (a guard
    against mistakes, like M1's `confirmed`: agents never run test)."""
    if not io.stdin.isatty() or not io.stdout_is_terminal():
        print("A test-split step is confirmed at a terminal (stdin and stdout).", file=sys.stderr)
        return False
    print(
        f"This uses the test split. Type '{words}' exactly and press Enter; anything else"
        " cancels: ",
        end="",
        flush=True,
    )
    try:
        answer = io.stdin.readline()
    except KeyboardInterrupt:
        return False
    return answer.strip() == words


def committed_ledger(io: Io, root: Path) -> list[PinEntry | RunEntry]:
    """The ledger, refused unless the file on disk is the one in HEAD (none in both is fine)."""
    path = root / LEDGER_PATH
    working = path.read_bytes() if path.is_file() else None
    if working != io.head_file(root, PurePosixPath(LEDGER_PATH.as_posix())):
        raise Refused(
            f"{LEDGER_PATH} is not as committed in HEAD: commit the test-run ledger (with the"
            " folders it names) first, so no earlier entry can be lost"
        )
    return parse_entries(working.decode("utf-8")) if working is not None else []


@dataclass(frozen=True)
class Permitted:
    """What a permitted test run records."""

    pin: PinEntry
    before: int
    reason: str | None


def check_test_run(
    io: Io,
    git: GitInfo,
    loaded: Loaded,
    *,
    mode: LedgerMode,
    real: bool,
    reason: str | None,
) -> Permitted:
    """Refuse a test run the discipline does not allow (module docstring)."""
    freeze = require_frozen(loaded, "a test run")
    if not real:
        raise Refused("dry runs are refused on the test split: rehearse on dev")
    if git.uncommitted_changes:
        raise Refused("a test run needs a clean working tree: commit first, so HEAD names the code")
    entries = committed_ledger(io, git.root)
    name = loaded.dataset.manifest.dataset
    pin = pin_for(entries, name, loaded.version, freeze)
    if pin is None:
        raise Refused(f"no pin for {name} {loaded.version} in {LEDGER_PATH}: run `pin` first")
    earlier = runs_of(entries, name, loaded.version, freeze, mode)
    if earlier and reason is None:
        raise Refused(
            f"{mode} mode already has {len(earlier)} test run(s) on this frozen version (first:"
            f" {earlier[0].run_id}): the test split runs once per mode; a rerun needs"
            " --test-rerun-reason, recorded in the ledger"
        )
    return Permitted(pin=pin, before=len(earlier), reason=reason)


def head_run_ids(io: Io, root: Path) -> set[str]:
    return {e.run_id for e in committed_ledger(io, root) if isinstance(e, RunEntry)}


# ── M4's spend ───────────────────────────────────────────────────────────────────────────────


def spend_lines(io: Io, git: GitInfo, why: str, cap: Decimal) -> str:
    """Count M4's spend from HEAD and refuse a cap above what is left (`intenteval/spend.py`)."""
    try:
        counted: MilestoneSpend = m4_spend(git.root, io.head_paths, io.head_file)
    except (SpendError, GitError) as error:
        raise Refused(f"M4's spend cannot be counted: {error}") from None
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
    """run.json's `cost` block from the run's cap (M2's `CostInfo`)."""
    return CostInfo(
        cap_usd=cap.cap,
        embedding_usd=cap.embedding_usd,
        llm_usd=cap.llm_usd,
        total_usd=cap.total,
        llm_cost_complete=cap.llm_cost_complete,
        embedding_requests=cap.embedding_requests,
        llm_calls=cap.llm_calls,
    )
