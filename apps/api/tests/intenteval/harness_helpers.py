"""Test doubles for the intent harness (M4 PR 4). Nothing here is H1 or H2.

- `frozen_copy(tmp)`: a TEST-ONLY repository in tmp_path: the two business manifests of
  `data/sample`, and an `intents` version frozen from `helpers.complete_dataset`'s rows with
  their ids renamed `tonly-...` and their text the hash letters alone (no marker), so that the
  live paths, which refuse placeholders, can be tested too. Never the real dataset; the texts are
  hashes, never messages.
- `FakeRepo`: HEAD as a dict, `git` as a fixed commit; `commit()` copies files into HEAD.
- `Terminal`: a stdin that says it is a terminal and plays typed lines.
- **H2's stand-ins** (`StandInMetrics`), deliberately NOT H2, as M3's were (`tests/evalrun/
  helpers.py`): the matrix puts every decided row on its *gold* diagonal ("as if right"), macro-F1
  is an arbitrary fraction of the matrix, every confidence goes to the last bin, ECE is 1/8. They
  record what the harness passed in, which is what these tests check.
- **H1's stand-in** (`ask_always`), deliberately NOT H1: it asks the fallback for every row and
  takes its answer, ignoring the floor, tau and tighten-only; any `GatewayError` is a hand-off at
  `high`. It exercises the replay plumbing; H1's own behaviour is tested by its strict xfails.
"""

import io
import json
import math
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Final

from tests.conftest import TEST_SETTINGS_FIELDS
from tests.intenteval.helpers import (
    BUSINESSES,
    CORPUS_DIR,
    complete_dataset,
    complete_eval,
    complete_train,
    filler,
    manifest,
)
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.base import Embedder, EmbeddingSpec
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.intenteval.checks import freeze_dataset
from vectapilot_api.intenteval.cli import harness_main
from vectapilot_api.intenteval.files import DEFAULT_DATASET, write_frozen
from vectapilot_api.intenteval.harness import Io
from vectapilot_api.intenteval.ledger import LEDGER_PATH
from vectapilot_api.intenteval.llmrun import DryRunRouterProvider
from vectapilot_api.intenteval.metrics import (
    COLUMNS,
    RELIABILITY_BINS,
    ClassScores,
    ConfusionMatrix,
    Decided,
    IntentMetrics,
    MacroF1,
    ReliabilityBin,
)
from vectapilot_api.intenteval.rows import (
    EVAL_FILE,
    TRAIN_FILE,
    EvalRow,
    TrainRow,
    dump_row,
    load_eval,
    load_train,
)
from vectapilot_api.intenteval.runinfo import (
    DRY_INTENT_REPORTS,
    INTENT_REPORTS,
    RUN_FILE,
    IntentRunInfo,
    read_run,
    write_json,
)
from vectapilot_api.intenteval.sample import draw_sample, render_sample
from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds
from vectapilot_api.intents.fallback import IntentFallback
from vectapilot_api.intents.policy import Priority, RoutePolicy, handoff
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent
from vectapilot_api.llm_cli import GitInfo

COMMIT: Final = "c" * 40
PAID: Final = {
    "environment": "local",
    "llm_provider": "anthropic",
    "anthropic_api_key": "test-only-not-a-key",
    "embeddings_provider": "voyage",
    "voyage_api_key": "test-only-not-a-key",
}
"""Settings that pass the live checks; the tests' Io builds fakes, so nothing is ever sent."""


def _unmarked[R: (TrainRow, EvalRow)](row: R) -> R:
    new_id = row.id.replace("example-", "tonly-")
    return row.model_copy(update={"id": new_id, "text": filler(new_id).removeprefix("TEST-ONLY ")})


def frozen_copy(root: Path, *, frozen: bool = True) -> Path:
    """A TEST-ONLY frozen `intents` version under `root` (module docstring); its folder. With
    `frozen=False` the same rows at stage `collecting`: no splits, no freeze.json."""
    for business in BUSINESSES:
        target = root / "data" / "sample" / business
        target.mkdir(parents=True)
        shutil.copy(CORPUS_DIR / business / "manifest.json", target / "manifest.json")
    train = [_unmarked(r) for r in complete_train()]
    eval_rows = [_unmarked(r) for r in complete_eval()]
    dataset = complete_dataset(
        train=train, eval_rows=eval_rows, sample=draw_sample(train, manifest().verification)
    )
    folder = root / DEFAULT_DATASET
    folder.mkdir(parents=True)
    (folder / TRAIN_FILE).write_text("".join(dump_row(r) + "\n" for r in dataset.train), "utf-8")
    (folder / "verification-sample.txt").write_text(render_sample(dataset.sample or ()), "utf-8")
    if not frozen:
        data = dataset.manifest.model_dump(mode="json")
        (folder / "manifest.json").write_text(json.dumps(data, indent=2) + "\n", "utf-8")
        text = "".join(dump_row(r) + "\n" for r in dataset.eval_rows)
        (folder / EVAL_FILE).write_text(text, "utf-8")
        return folder
    write_frozen(folder, freeze_dataset(dataset, frozen_on=date(2026, 11, 9)))
    return folder


@dataclass
class FakeRepo:
    root: Path
    head: dict[PurePosixPath, bytes] = field(default_factory=dict)
    dirty: bool = False

    def git(self) -> GitInfo:
        return GitInfo(root=self.root, commit=COMMIT, uncommitted_changes=self.dirty)

    def head_file(self, root: Path, path: PurePosixPath) -> bytes | None:
        assert root == self.root
        return self.head.get(path)

    def head_paths(self, root: Path, under: PurePosixPath) -> list[PurePosixPath]:
        assert root == self.root
        return [p for p in self.head if p.is_relative_to(under)]

    def commit(self, *paths: Path) -> None:
        """Put files (or every file under folders) into HEAD as they are on disk."""
        for path in paths:
            files = sorted(path.rglob("*")) if path.is_dir() else [path]
            for file in files:
                if file.is_file():
                    relative = file.resolve().relative_to(self.root.resolve()).as_posix()
                    self.head[PurePosixPath(relative)] = file.read_bytes()


class Terminal(io.StringIO):
    """A stdin at a terminal, playing the lines given."""

    def isatty(self) -> bool:
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        self.t += 0.004
        return self.t


class Now:
    """A UTC clock that moves one second per call, so folder names never collide."""

    def __init__(self) -> None:
        self.t = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


def settings(live: bool) -> Settings:
    return Settings(**(TEST_SETTINGS_FIELDS | (PAID if live else {})))


# ── H2's stand-ins (module docstring) ────────────────────────────────────────────────────────


@dataclass
class StandInMetrics:
    matrices: list[tuple[tuple[Intent, ...], tuple[Decided, ...]]] = field(default_factory=list)
    bins: list[tuple[tuple[float, ...], tuple[bool, ...]]] = field(default_factory=list)

    def bundle(self) -> IntentMetrics:
        def confusion_matrix(gold: Sequence[Intent], decided: Sequence[Decided]) -> ConfusionMatrix:
            self.matrices.append((tuple(gold), tuple(decided)))
            cells = {g: dict.fromkeys(COLUMNS, 0) for g in DECISION_ORDER}
            for g, d in zip(gold, decided, strict=True):
                cells[g][None if d is None else g] += 1  # as if every decided row were right
            return ConfusionMatrix(cells)

        def macro_f1(matrix: ConfusionMatrix) -> MacroF1:
            zero = Fraction(0)
            per = tuple(ClassScores(i, 0, 0, 0, zero, zero, zero) for i in DECISION_ORDER)
            return MacroF1(per, Fraction(matrix.decided_total(None) + 1, matrix.total + 2))

        def reliability_bins(
            confidences: Sequence[float], correct: Sequence[bool]
        ) -> tuple[ReliabilityBin, ...]:
            self.bins.append((tuple(confidences), tuple(correct)))
            edges = [Fraction(i, RELIABILITY_BINS) for i in range(RELIABILITY_BINS + 1)]
            empty = [ReliabilityBin(edges[i], edges[i + 1], 0, 0, None, None) for i in range(4)]
            n, right = len(confidences), sum(correct)
            last = (
                ReliabilityBin(edges[4], edges[5], 0, 0, None, None)
                if n == 0
                else ReliabilityBin(
                    edges[4], edges[5], n, right, math.fsum(confidences) / n, Fraction(right, n)
                )
            )
            return (*empty, last)

        def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> float:
            return 0.125

        return IntentMetrics(
            confusion_matrix=confusion_matrix,
            macro_f1=macro_f1,
            reliability_bins=reliability_bins,
            expected_calibration_error=expected_calibration_error,
        )


# ── H1's stand-in (module docstring) ─────────────────────────────────────────────────────────


async def ask_always(
    scores: IntentScores,
    message: str,
    policy: RoutePolicy,
    fallback: IntentFallback,
    *,
    thresholds: RouterThresholds,
    business_id: str,
    trace_id: str,
) -> RouteDecision:
    try:
        intent = await fallback(message, business_id=business_id, trace_id=trace_id)
    except GatewayError as error:
        return RouteDecision(
            intent=None,
            route=handoff(Priority.HIGH),
            decided_by="fail_closed",
            error=type(error).__name__,
        )
    return RouteDecision(
        intent=intent, route=policy.route(intent), decided_by="llm", llm_intent=intent
    )


def make_io(
    repo: FakeRepo,
    *,
    stdin: io.StringIO | None = None,
    terminal: bool = True,
    metrics: StandInMetrics | None = None,
    provider: Provider | None = None,
    router: Any = ask_always,
    now: Now | None = None,
) -> Io:
    """An Io that reads nothing from outside: fake git, fake clocks, fake providers only."""

    def build_embedder(s: Settings, spec: EmbeddingSpec) -> Embedder:
        return FakeEmbedder(spec)

    return Io(
        git=repo.git,
        head_file=repo.head_file,
        head_paths=repo.head_paths,
        head_ref=lambda root: "branch m4/close-out at ccccccc",
        now=now or Now(),
        clock=Clock(),
        stdin=stdin if stdin is not None else io.StringIO(),
        stdout_is_terminal=lambda: terminal,
        load_settings=settings,
        build_embedder=build_embedder,
        build_provider=lambda s: provider if provider is not None else DryRunRouterProvider(),
        metrics=(metrics or StandInMetrics()).bundle(),
        router=router,
    )


# ── Driving the CLI (shared by the harness's CLI tests) ──────────────────────────────────────

VERSION: Final = "v0.1"


def folders(root: Path, real: bool, suffix: str) -> list[Path]:
    """The run folders of `VERSION` whose names end with `suffix`, real or dry."""
    base = root / (INTENT_REPORTS if real else DRY_INTENT_REPORTS) / VERSION
    return sorted(p for p in base.glob(f"*{suffix}") if p.is_dir()) if base.is_dir() else []


def texts(dataset: Path) -> list[str]:
    """Every row's text, to check that none is written anywhere."""
    train = load_train((dataset / TRAIN_FILE).read_text("utf-8"))
    evals = load_eval((dataset / EVAL_FILE).read_text("utf-8"))
    return [r.text for r in (*train, *evals)]


def written_text(root: Path) -> str:
    """Every file under `root`, concatenated (empty when it does not exist)."""
    if not root.exists():
        return ""
    return "\n".join(p.read_text("utf-8") for p in sorted(root.rglob("*")) if p.is_file())


def run_args(dataset: Path, split: str, mode: str, *extra: str) -> list[str]:
    return ["--dataset", str(dataset), "run", "--split", split, "--mode", mode, *extra]


def live_llm_args(dataset: Path, split: str, *extra: str) -> list[str]:
    return run_args(dataset, split, "llm", "--live", "--max-usd", "0.50", *extra)


def dry_embed(repo: FakeRepo, dataset: Path, now: Now) -> Path:
    assert harness_main(["--dataset", str(dataset), "embed"], make_io(repo, now=now)) == 0
    return folders(repo.root, False, "-fake-embed")[-1]


def dry_model(repo: FakeRepo, dataset: Path, now: Now) -> Path:
    embed = dry_embed(repo, dataset, now)
    io = make_io(repo, now=now)
    assert harness_main(["--dataset", str(dataset), "train", "--embed", str(embed)], io) == 0
    model = folders(repo.root, False, "-fake-model")[-1]
    for step in ("calibrate", "thresholds"):
        assert harness_main(["--dataset", str(dataset), step, str(model)], io) == 0
    return model


def live_embed(repo: FakeRepo, dataset: Path, now: Now, typed: str = "0.50\n") -> int:
    io = make_io(repo, stdin=Terminal(typed), now=now)
    return harness_main(["--dataset", str(dataset), "embed", "--live", "--max-usd", "0.50"], io)


def real_model(repo: FakeRepo, dataset: Path, now: Now) -> Path:
    """A "real" model folder (from a committed fake "live" embed), calibrated and thresholded."""
    assert live_embed(repo, dataset, now) == 0
    [embed] = folders(repo.root, True, "-live-embed")
    repo.commit(embed)
    io = make_io(repo, now=now)
    assert harness_main(["--dataset", str(dataset), "train", "--embed", str(embed)], io) == 0
    [model] = folders(repo.root, True, "-local-model")
    for step in ("calibrate", "thresholds"):
        assert harness_main(["--dataset", str(dataset), step, str(model)], io) == 0
    return model


def pinned_model(repo: FakeRepo, dataset: Path, now: Now) -> Path:
    """`real_model`, committed and pinned, with the ledger committed."""
    model = real_model(repo, dataset, now)
    repo.commit(model)
    pin = ["--dataset", str(dataset), "pin", str(model)]
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 0
    repo.commit(repo.root / LEDGER_PATH)
    return model


@dataclass(frozen=True)
class CommittedRuns:
    """A pinned model and one committed, ledgered test run of each paid-or-not mode."""

    model: Path
    llm_dev: Path
    classifier: Path
    llm: Path


def committed_test_runs(repo: FakeRepo, dataset: Path, now: Now) -> CommittedRuns:
    model = pinned_model(repo, dataset, now)
    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(live_llm_args(dataset, "dev"), live) == 0
    [llm_dev] = folders(repo.root, True, "-live-llm-dev")
    repo.commit(llm_dev)
    typed = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(run_args(dataset, "test", "classifier", "--model", str(model)), typed) == 0
    [classifier] = folders(repo.root, True, "-local-classifier-test")
    repo.commit(classifier, repo.root / LEDGER_PATH)
    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(live_llm_args(dataset, "test"), live) == 0
    [llm] = folders(repo.root, True, "-live-llm-test")
    repo.commit(llm, repo.root / LEDGER_PATH)
    return CommittedRuns(model=model, llm_dev=llm_dev, classifier=classifier, llm=llm)


def hybrid_args(dataset: Path, classifier: Path, llm: Path) -> list[str]:
    return [
        *("--dataset", str(dataset), "summarize"),
        *("--classifier", str(classifier), "--llm", str(llm)),
    ]


def rewrite_run(folder: Path, **changes: Any) -> IntentRunInfo:
    """Rewrite a folder's run.json with `changes` (validated); the new run info."""
    info = IntentRunInfo.model_validate(read_run(folder).model_dump() | changes)
    (folder / RUN_FILE).unlink()  # `write_json` never overwrites
    write_json(folder / RUN_FILE, info)
    return info


def reformat_json(path: Path) -> None:
    """The same JSON values, other bytes (no longer the file in HEAD)."""
    path.write_text(json.dumps(json.loads(path.read_text("utf-8"))) + "\n", "utf-8")
