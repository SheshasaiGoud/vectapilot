"""`python -m vectapilot_api.evalrun` (`evalrun/cli.py`) with its outside world replaced: a
TEST-ONLY copy of the dataset in pytest's tmp_path (the real synthetic corpus, read only), a fake
git, a stand-in `execute` (no Postgres, no paid call) and H2 stand-ins. Nothing here passes a
real key; the "live" paths run with fake Settings values and the stand-in, so nothing is sent."""

import dataclasses
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Final

import pytest

from tests.conftest import make_settings
from tests.evalrun.helpers import (
    CORPUS_DIR,
    PAWS,
    PAWS_STORE_HOURS,
    REPO,
    SENTINEL_QUESTION,
    PreparedMetrics,
    question,
    run_info,
)
from vectapilot_api.config import Settings
from vectapilot_api.evalrun import cli
from vectapilot_api.evalrun.cli import Io, Refused, committed_dev_run
from vectapilot_api.evalrun.dryrun import DRY_RUN_ANSWER
from vectapilot_api.evalrun.harness import RunConfig, RunOutcome, Target
from vectapilot_api.evalrun.ingest import IngestCount
from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry, append_ledger, read_ledger
from vectapilot_api.evalrun.report import RUN_FILE, RunInfo, RunStatus
from vectapilot_api.evalrun.scoring import AnswerLine, HitRef, QuestionResult, base_fields
from vectapilot_api.evalset.checks import CheckResult
from vectapilot_api.evalset.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.evalset.questions import EvalQuestion, Split, dump_question
from vectapilot_api.evalset.ranking import has_evidence
from vectapilot_api.evalset.split import build_freeze
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.llm_cli import GitInfo

COMMIT: Final = "a" * 40
QUESTIONS: Final = (
    question("test-only-1", text=SENTINEL_QUESTION),
    question("test-only-2", PAWS, evidence=((PAWS_STORE_HOURS,),)),
    question("test-only-3", evidence=(), expected="dont_know", category="out_of_scope"),
)
PAID: Final = {
    "environment": "local",
    "llm_provider": "anthropic",
    "anthropic_api_key": "test-only-not-a-key",
    "embeddings_provider": "voyage",
    "voyage_api_key": "test-only-not-a-key",
}
"""Settings that pass the live check; the stand-in `execute` never sends anything."""


def write_dataset(root: Path, questions: tuple[EvalQuestion, ...] = QUESTIONS) -> Path:
    folder = root / DEFAULT_DATASET
    folder.mkdir(parents=True)
    manifest = (REPO / DEFAULT_DATASET / "manifest.json").read_text("utf-8")
    (folder / "manifest.json").write_text(manifest, encoding="utf-8")
    (folder / "questions.jsonl").write_text(
        "".join(dump_question(q) + "\n" for q in questions), encoding="utf-8"
    )
    return folder


@dataclass
class FakeExecute:
    """`harness.execute`'s stand-in: one prepared result per question, through the sink."""

    status: RunStatus = "complete"
    stop: str | None = None
    raises: BaseException | None = None
    before: Callable[[], None] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self, target: Target, settings: Settings, config: Any, cap: Any, **kwargs: Any
    ) -> RunOutcome:
        self.calls.append({"target": target, "settings": settings, "config": config, **kwargs})
        if self.before is not None:
            self.before()
        if self.raises is not None:
            raise self.raises
        results = []
        for q in target.questions:
            scored = has_evidence(q)
            result = QuestionResult.model_validate(
                {
                    **base_fields(q, target.mapping[q.business_id]),
                    "retrieval": "scored" if scored else "not_scored",
                    "hits": (
                        HitRef(
                            rank=1,
                            chunk_id="c1",
                            document_id="d1",
                            source_id="faq",
                            version="v",
                            chunk_index=0,
                            page=None,
                            score=0.9,
                            groups=(0,) if scored else (),
                        ),
                    ),
                    "evidence_chunk_ids": ("c1",),
                    "hit_at_k": True if scored else None,
                    "all_groups_at_k": True if scored else None,
                    "reciprocal_rank": "1" if scored else None,
                    "evidence_complete": True if scored else None,
                    "outcome": "answered",
                    "trace_id": "0123456789abcdef0123456789abcdef",
                }
            )
            answer = AnswerLine(
                question_id=q.id,
                trace_id="0123456789abcdef0123456789abcdef",
                expected_outcome=q.expected_outcome,
                outcome="answered",
                reason=None,
                error=None,
                answer=DRY_RUN_ANSWER,
                sources=(),
            )
            kwargs["sink"](result, answer)
            results.append(result)
        counts = tuple(
            IngestCount(business, eval_id, 9, 0, 0, 0)
            for business, eval_id in target.mapping.items()
        )
        return RunOutcome(self.status, self.stop, results, counts)


def prepared() -> PreparedMetrics:
    """H2's stand-in for `FakeExecute`'s results: test-only-1 and -2 scored, each a hit at rank 1,
    so every mean (both together, or either alone) is 1, written out by hand."""
    metrics = PreparedMetrics()
    for q in QUESTIONS:
        if has_evidence(q):
            metrics.prepare(q.id, hit=True, rr=Fraction(1))
            metrics.evidence_all[q.id] = True
    for ids in ({"test-only-1", "test-only-2"}, {"test-only-1"}, {"test-only-2"}):
        metrics.prepare_mean(ids, hit=Fraction(1), mrr=Fraction(1))
    return metrics


class Terminal(io.StringIO):
    """A stdin that says it is a terminal, with the cap typed in."""

    def isatty(self) -> bool:
        return True


@dataclass
class World:
    root: Path
    dataset: Path
    execute: FakeExecute
    settings: Settings
    stdin: io.StringIO
    head: dict[PurePosixPath, bytes] = field(default_factory=dict)
    clock: list[datetime] = field(default_factory=lambda: [datetime(2026, 10, 22, 9, tzinfo=UTC)])
    metrics: PreparedMetrics = field(default_factory=prepared)
    stdout_terminal: bool = True

    def now(self) -> datetime:
        self.clock[0] += timedelta(seconds=1)
        return self.clock[0]

    def io(self) -> Io:
        return Io(
            load_settings=lambda live: self.settings,
            owner_url=lambda: "postgresql+asyncpg://owner:test-only@127.0.0.1:1/x",
            git=lambda: GitInfo(root=self.root, commit=COMMIT, uncommitted_changes=False),
            head_file=lambda root, path: self.head.get(path),
            now=self.now,
            stdin=self.stdin,
            stdout_is_terminal=lambda: self.stdout_terminal,
            execute=self.execute,
            metrics=self.metrics.functions(),
        )

    def run(self, *args: str) -> int:
        argv = ["--dataset", str(self.dataset), "--corpus", str(CORPUS_DIR), *args]
        return cli.run(argv, self.io())


@pytest.fixture
def world(tmp_path: Path) -> World:
    root = tmp_path / "repo"
    return World(
        root=root,
        dataset=write_dataset(root),
        execute=FakeExecute(),
        settings=cli.load_settings(False).model_copy(),
        stdin=io.StringIO(),
    )


def run_dirs(world: World) -> list[Path]:
    return sorted(p for p in world.root.glob("evals/reports/**/*-*-*") if (p / RUN_FILE).exists())


# ── A dry run ────────────────────────────────────────────────────────────────────────────────


def test_a_dry_run_writes_a_complete_run_folder_labelled_dry_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("run", "--split", "dev") == 0

    [out] = run_dirs(world)
    assert out.relative_to(world.root).parts[:5] == (
        "evals",
        "reports",
        "tmp",
        "support_qa",
        "v0.1",
    )
    assert out.name == "20261022T090001Z-fake-unassigned"
    names = {p.name for p in out.iterdir()}
    assert names == {
        "run.json",
        "results.jsonl",
        "answers.jsonl",
        "labels.jsonl",
        "summary.md",
        "summary.json",
        "embedding_call_log.jsonl",
        "llm_call_log.jsonl",
        "rag_answer_log.jsonl",
    }
    info = RunInfo.model_validate_json((out / "run.json").read_text("utf-8"))
    assert (info.dry_run, info.status, info.split, info.dataset_stage) == (
        True,
        "complete",
        "unassigned",
        "collecting",
    )
    assert (info.embeddings_provider, info.embedding_model, info.llm_provider) == (
        "fake",
        "fake-embedding-1024",
        "fake",
    )
    assert (info.config.search_k, info.config.metric_k, info.config.rag_min_score) == (5, 5, 0.3)
    assert (info.config.max_chars, info.config.overlap_chars) == (1200, 150)
    assert info.dataset_committed is False and info.commit == COMMIT
    assert info.dataset_synthetic and info.corpus_synthetic
    assert info.corpus_review_pending == 1  # faq-hi, review: pending
    assert info.questions_selected == 3 and info.freeze_sha256 is None
    assert set(info.namespaces) == {"sunrise-clinic", PAWS}
    assert info.bootstrap_seed == 20261022 and info.bootstrap_resamples == 2000
    summary = (out / "summary.md").read_text("utf-8")
    assert summary.splitlines()[2].startswith("**DRY RUN: fake embedder and canned fake answers")
    labels = [json.loads(line) for line in (out / "labels.jsonl").read_text().splitlines()]
    assert [line["question_id"] for line in labels] == ["test-only-1", "test-only-2", "test-only-3"]
    assert all(line["label"] is None for line in labels)
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out and "Written to" in captured.out
    assert "zq7" not in captured.out + captured.err  # the sentinel question text, never printed
    assert "zq7" not in (out / "results.jsonl").read_text() + summary
    assert DRY_RUN_ANSWER in (out / "answers.jsonl").read_text()


def test_the_dry_run_hands_execute_the_fake_settings_the_namespaces_and_no_cap(
    world: World,
) -> None:
    world.run("run", "--split", "dev", "--fresh")

    [call] = world.execute.calls
    assert call["dry_run"] is True and call["fresh"] is True
    assert call["settings"].llm_provider == "fake"
    assert call["settings"].embeddings_provider == "fake"
    assert call["owner_url"].startswith("postgresql+asyncpg://owner:")
    target: Target = call["target"]
    assert [q.id for q in target.questions] == ["test-only-1", "test-only-2", "test-only-3"]
    assert all(v.startswith(f"{k}-ev-") for k, v in target.mapping.items())


@pytest.mark.parametrize(("status", "code"), [("partial", 3), ("aborted_leak", 4), ("failed", 1)])
def test_the_exit_code_follows_the_runs_status(world: World, status: RunStatus, code: int) -> None:
    world.execute.status, world.execute.stop = status, "TEST-ONLY stop"

    assert world.run("run", "--split", "dev") == code
    [out] = run_dirs(world)
    assert RunInfo.model_validate_json((out / "run.json").read_text()).status == status


def test_h1_or_h2_not_written_yet_fails_the_run_and_keeps_every_question(world: World) -> None:
    world.execute.raises = NotImplementedError("H1: shesha writes vector_search")

    assert world.run("run", "--split", "dev") == 1

    [out] = run_dirs(world)
    info = RunInfo.model_validate_json((out / "run.json").read_text())
    assert info.status == "failed" and info.stop is not None
    assert info.stop.startswith("NotImplementedError: H1")
    results = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [r["outcome"] for r in results] == ["not_run"] * 3
    assert "FAILED: NotImplementedError" in (out / "summary.md").read_text()


def test_a_metric_k_above_the_search_k_is_refused_before_anything(world: World) -> None:
    assert world.run("run", "--split", "dev", "--metric-k", "6") == 1
    assert world.execute.calls == [] and run_dirs(world) == []


def test_production_is_refused(world: World) -> None:
    world.settings = make_settings(**(PAID | {"environment": "production"}))

    assert world.run("run", "--split", "dev") == 1
    assert world.execute.calls == []


def test_a_dry_run_with_a_paid_provider_in_settings_is_refused(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.settings = make_settings(**PAID)

    assert world.run("run", "--split", "dev") == 1
    assert world.execute.calls == [] and run_dirs(world) == []
    assert "a run without --live uses the fake providers only" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--split", "dev"],
        ["run", "--split", "dev", "--live", "--max-usd", "0.50"],
        ["ingest"],
    ],
    ids=["dry", "live", "ingest"],
)
@pytest.mark.usefixtures("checked_and_selected")
def test_a_hybrid_retrieval_mode_is_refused_until_pr5_adds_retrieval_mode(
    world: World, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    # Decided 2026-10-25 (shesha): the harness builds vector-mode search and scores the vector
    # list only, so a hybrid setting would be a run that reports one mode while its Settings name
    # another. Everything else would let these runs through: the live one has the paid Settings
    # and the cap typed at a terminal.
    live = "--live" in args
    world.settings = make_settings(**(PAID if live else {}), rag_retrieval_mode="hybrid")
    world.stdin = Terminal("0.50\n")

    assert world.run(*args) == 1

    assert world.execute.calls == [] and run_dirs(world) == []
    err = capsys.readouterr().err
    assert "rag_retrieval_mode must be vector" in err and "--retrieval-mode" in err
    assert "hybrid" not in err  # a fixed message: the setting's name, never its value
    # The same Settings in vector mode go through (the refusal is the only thing that stopped it).
    world.settings = world.settings.model_copy(update={"rag_retrieval_mode": "vector"})
    world.stdin = Terminal("0.50\n")
    assert world.run(*args) == 0 and len(world.execute.calls) == 1


def test_a_search_k_of_zero_is_refused_never_replaced_by_the_default(world: World) -> None:
    assert world.run("run", "--split", "dev", "--search-k", "0") == 1
    assert world.execute.calls == [] and run_dirs(world) == []


def test_fresh_says_what_it_deletes_in_the_plan(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run("run", "--split", "dev", "--fresh")

    assert (
        "--fresh: deletes the documents of 2 eval business(es) of this config first, then embeds"
        " them again." in capsys.readouterr().out
    )


def test_the_plan_never_calls_data_synthetic_that_its_manifest_does_not(world: World) -> None:
    dataset = read_dataset(world.dataset, CORPUS_DIR)
    unmarked = dataclasses.replace(
        dataset, manifest=dataset.manifest.model_copy(update={"synthetic": False})
    )
    settings = cli.load_settings(False)
    config = RunConfig(
        chunking=ChunkingConfig(),
        search_k=5,
        metric_k=5,
        rag_min_score=0.3,
        weak_evidence_outcome="dont_know",
        llm_max_retries=2,
        llm_max_repairs=1,
    )

    plan = cli._plan_text(unmarked, "dev", QUESTIONS, config, settings, "m", "a", False)

    assert "NOT synthetic data (dataset manifest synthetic: false" in plan
    assert "; synthetic data" not in plan


@pytest.mark.usefixtures("checked_and_selected")
def test_a_question_the_api_would_refuse_is_refused_before_anything(world: World) -> None:
    padded = (*QUESTIONS, question("test-only-4", text=" \n "))
    world.dataset = write_dataset(world.root / "other", padded)

    assert world.run("run", "--split", "dev") == 1
    assert world.execute.calls == [] and run_dirs(world) == []


# ── Live: refusals before anything is sent ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--split", "dev", "--live"],
        ["run", "--split", "dev", "--live", "--max-usd", "1.50"],
        ["run", "--split", "dev", "--live", "--max-usd", "1e-1"],
        ["run", "--split", "dev", "--live", "--max-usd", "0"],
        ["run", "--split", "dev", "--test-rerun-reason", "Not A Slug"],
    ],
    ids=["no-cap", "above-ceiling", "exponent", "zero", "bad-reason"],
)
def test_bad_arguments_exit_2(world: World, args: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        world.run(*args)
    assert exit_.value.code == 2 and world.execute.calls == []


def test_a_live_run_refuses_placeholder_rows(world: World) -> None:
    world.settings = make_settings(**PAID)

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1
    assert world.execute.calls == [] and run_dirs(world) == []


@pytest.fixture
def checked_and_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live paths past the dataset check and the split (a frozen TEST-ONLY dataset would need 40
    questions and a reviewed corpus): both are tested on their own below."""
    monkeypatch.setattr(cli, "check_dataset", lambda *a, **k: CheckResult((), ()))
    monkeypatch.setattr(cli, "_select", lambda dataset, split, live: (split, dataset.questions))


@pytest.mark.usefixtures("checked_and_selected")
def test_a_live_run_with_a_fake_provider_is_refused(world: World) -> None:
    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1
    assert world.execute.calls == []


@pytest.mark.usefixtures("checked_and_selected")
def test_a_live_run_without_the_cap_typed_at_a_terminal_sends_nothing(world: World) -> None:
    world.settings = make_settings(**PAID)

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1
    assert world.execute.calls == [] and run_dirs(world) == []


@pytest.mark.usefixtures("checked_and_selected")
def test_a_failure_after_the_calls_never_says_nothing_was_sent(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    world.metrics.mean_hit_offset = Fraction(1, 1000)  # H2's stand-in disagrees with itself

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1

    [out] = run_dirs(world)
    err = capsys.readouterr().err
    assert "nothing was sent" not in err
    assert "the run FAILED, no summary written: all: H2's mean_hit_at_k disagrees" in err
    assert f"Its inputs are in {out}; commit it as evidence only, or delete it" in err
    assert "Run status complete; counted spend $0." in err
    assert f"run `python -m vectapilot_api.evalrun summarize {out}`" in err
    assert (out / RUN_FILE).is_file() and not (out / "summary.md").exists()


@pytest.mark.usefixtures("checked_and_selected")
def test_a_confirmed_live_run_writes_its_folder_where_it_is_committed(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 0

    [out] = run_dirs(world)
    assert out.relative_to(world.root).parts[:4] == ("evals", "reports", "support_qa", "v0.1")
    assert out.name.endswith("-live-dev")
    [call] = world.execute.calls
    assert call["dry_run"] is False and call["settings"].llm_provider == "anthropic"
    info = RunInfo.model_validate_json((out / "run.json").read_text())
    assert info.dry_run is False and info.cost.cap_usd is not None
    printed = capsys.readouterr().out
    assert "LIVE: paid embedding and answer-model calls" in printed
    assert "whole plan $" in printed and "Cap: $0.5" in printed


# ── The test split ───────────────────────────────────────────────────────────────────────────


def frozen(world: World) -> Any:
    dataset = read_dataset(world.dataset, CORPUS_DIR)
    splits: dict[str, Split] = {
        q.id: ("test" if i == 0 else "dev") for i, q in enumerate(dataset.questions)
    }
    questions = tuple(q.model_copy(update={"split": splits[q.id]}) for q in dataset.questions)
    manifest = dataset.manifest.model_copy(update={"stage": "frozen"})
    freeze = build_freeze(manifest, questions, splits, dataset.corpus.pins(), date(2026, 10, 22))
    return dataclasses.replace(dataset, manifest=manifest, questions=questions, freeze=freeze)


def test_the_split_rules(world: World) -> None:
    unfrozen = read_dataset(world.dataset, CORPUS_DIR)
    assert cli._select(unfrozen, "dev", live=False) == ("unassigned", unfrozen.questions)
    with pytest.raises(Refused, match="a live run needs a frozen dataset"):
        cli._select(unfrozen, "dev", live=True)
    with pytest.raises(Refused, match="not frozen: it has no test split"):
        cli._select(unfrozen, "test", live=False)
    dataset = frozen(world)
    assert [q.id for q in cli._select(dataset, "dev", live=True)[1]] == [
        "test-only-2",
        "test-only-3",
    ]
    assert cli._select(dataset, "test", live=True)[0] == "test"
    with pytest.raises(Refused, match="dry runs are refused on the test split"):
        cli._select(dataset, "test", live=False)


def dev_run(world: World, fingerprint: str, **changes: Any) -> Path:
    folder = world.root / "evals/reports/support_qa/v0.1/20261021T000000Z-live-dev"
    folder.mkdir(parents=True)
    values = {"run_id": folder.name, "dry_run": False, "config_fingerprint": fingerprint}
    info = run_info(**(values | changes))
    path = folder / RUN_FILE
    path.write_text(info.model_dump_json(), encoding="utf-8")
    return path


def test_a_test_run_needs_a_committed_complete_live_dev_run_with_its_config(world: World) -> None:
    reports = world.root / "evals/reports/support_qa/v0.1"
    path = dev_run(world, "f" * 64)
    relative = PurePosixPath(path.relative_to(world.root).as_posix())
    head = {relative: path.read_bytes()}

    def committed(fingerprint: str, files: dict[PurePosixPath, bytes]) -> str | None:
        return committed_dev_run(world.root, reports, fingerprint, lambda r, p: files.get(p))

    assert committed("f" * 64, head) == "20261021T000000Z-live-dev"
    assert committed("f" * 64, {}) is None  # not in HEAD: not committed
    assert committed("e" * 64, head) is None  # another config


@pytest.mark.parametrize(
    "changes", [{"status": "partial", "stop": "x"}, {"dry_run": True}, {"split": "test"}]
)
def test_a_partial_dry_or_test_run_does_not_count_as_the_dev_run(
    world: World, changes: dict[str, Any]
) -> None:
    path = dev_run(world, "f" * 64, **changes)
    relative = PurePosixPath(path.relative_to(world.root).as_posix())

    found = committed_dev_run(
        world.root,
        world.root / "evals/reports/support_qa/v0.1",
        "f" * 64,
        lambda r, p: path.read_bytes() if p == relative else None,
    )
    assert found is None


def test_the_ledger_appends_and_reads_back(tmp_path: Path) -> None:
    ledger = tmp_path / "evals/reports/support_qa" / LEDGER_FILE
    entry = LedgerEntry(
        dataset="support_qa",
        version="v0.1",
        freeze_sha256="0" * 64,
        config_fingerprint="f" * 64,
        run_id="r1",
        commit=COMMIT,
        started_at=datetime(2026, 10, 22, tzinfo=UTC),
        rerun_reason=None,
    )

    append_ledger(ledger, entry)
    append_ledger(ledger, entry.model_copy(update={"run_id": "r2", "rerun_reason": "cap-stop"}))

    assert [e.run_id for e in read_ledger(ledger)] == ["r1", "r2"]
    assert read_ledger(tmp_path / "missing.jsonl") == []


@pytest.fixture
def test_split_world(world: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A world whose dataset passes as frozen with a committed dev run for its config."""
    monkeypatch.setattr(cli, "check_dataset", lambda *a, **k: CheckResult((), ()))
    monkeypatch.setattr(cli, "_select", lambda dataset, split, live: (split, dataset.questions))
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    monkeypatch.setattr(
        cli, "committed_dev_run", lambda root, reports, fingerprint, head: "20261021-live-dev"
    )
    yield world


def test_a_test_run_is_in_the_ledger_before_its_first_call(test_split_world: World) -> None:
    world = test_split_world
    ledger = world.root / "evals/reports/support_qa" / LEDGER_FILE
    seen: list[list[LedgerEntry]] = []
    world.execute.before = lambda: seen.append(read_ledger(ledger))

    assert world.run("run", "--split", "test", "--live", "--max-usd", "0.50") == 0

    [entries] = seen
    [entry] = entries
    [out] = run_dirs(world)
    assert entry.run_id == out.name and out.name.endswith("-live-test")
    assert entry.commit == COMMIT and entry.rerun_reason is None


LEDGER: Final = PurePosixPath("evals/reports/support_qa") / LEDGER_FILE


def commit_ledger(world: World) -> None:
    """What `git commit` of the ledger does, as far as `head_file` can tell."""
    world.head[LEDGER] = (world.root / LEDGER).read_bytes()


def test_a_second_test_run_of_a_config_needs_a_recorded_reason(test_split_world: World) -> None:
    world = test_split_world
    assert world.run("run", "--split", "test", "--live", "--max-usd", "0.50") == 0
    commit_ledger(world)
    world.stdin = Terminal("0.50\n")

    assert world.run("run", "--split", "test", "--live", "--max-usd", "0.50") == 1
    assert len(world.execute.calls) == 1

    world.stdin = Terminal("0.50\n")
    assert (
        world.run(
            "run",
            "--split",
            "test",
            "--live",
            "--max-usd",
            "0.50",
            "--test-rerun-reason",
            "first-run-stopped-by-cap",
        )
        == 0
    )
    ledger = read_ledger(world.root / LEDGER)
    assert [e.rerun_reason for e in ledger] == [None, "first-run-stopped-by-cap"]
    first, rerun = run_dirs(world)
    info = RunInfo.model_validate_json((rerun / RUN_FILE).read_text())
    assert (info.test_rerun_reason, info.test_runs_before_config, info.test_runs_before_freeze) == (
        "first-run-stopped-by-cap",
        1,
        1,
    )
    assert RunInfo.model_validate_json((first / RUN_FILE).read_text()).test_runs_before_config == 0
    summary = (rerun / "summary.md").read_text()
    assert "test RERUN (reason: first-run-stopped-by-cap), not for the note yet" in summary
    assert "| Test-run ledger | 1 earlier test run(s) of this config, 1 of this frozen" in summary


@pytest.mark.parametrize("head", [None, b"TEST-ONLY another ledger\n"], ids=["absent", "other"])
def test_a_test_run_is_refused_while_the_ledger_is_not_as_committed(
    test_split_world: World, head: bytes | None
) -> None:
    world = test_split_world
    assert world.run("run", "--split", "test", "--live", "--max-usd", "0.50") == 0
    if head is not None:
        world.head[LEDGER] = head  # HEAD holds other bytes than the working tree
    world.stdin = Terminal("0.50\n")

    assert (
        world.run(
            "run",
            "--split",
            "test",
            "--live",
            "--max-usd",
            "0.50",
            "--test-rerun-reason",
            "first-run-stopped-by-cap",
        )
        == 1
    )
    assert len(world.execute.calls) == 1 and len(read_ledger(world.root / LEDGER)) == 1


def test_a_test_run_without_a_committed_dev_run_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "check_dataset", lambda *a, **k: CheckResult((), ()))
    monkeypatch.setattr(cli, "_select", lambda dataset, split, live: (split, dataset.questions))
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run("run", "--split", "test", "--live", "--max-usd", "0.50") == 1
    assert world.execute.calls == [] and run_dirs(world) == []
    assert not (world.root / "evals/reports/support_qa" / LEDGER_FILE).exists()


# ── summarize, label-sheet, ingest ───────────────────────────────────────────────────────────


def test_summarize_rebuilds_the_summary_from_the_folder(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run("run", "--split", "dev")
    [out] = run_dirs(world)
    (out / "summary.md").unlink()
    capsys.readouterr()

    assert world.run("summarize", str(out)) == 0
    assert (out / "summary.md").read_text().startswith(f"# Eval run {out.name}")
    assert "DRY RUN" in capsys.readouterr().out


def test_the_label_sheet_prints_question_and_answer(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.run("run", "--split", "dev")
    [out] = run_dirs(world)
    capsys.readouterr()

    assert world.run("label-sheet", str(out)) == 0
    printed = capsys.readouterr().out
    assert SENTINEL_QUESTION in printed and DRY_RUN_ANSWER in printed


def test_ingest_is_a_dry_run_of_the_ingest_step_only(world: World) -> None:
    assert world.run("ingest", "--fresh") == 0

    [call] = world.execute.calls
    assert (call["ingest_only"], call["dry_run"], call["fresh"]) == (True, True, True)
    assert call["target"].questions == ()
    assert run_dirs(world) == []


def test_a_failed_ingest_prints_its_problems(
    world: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def stale(*args: Any, **kwargs: Any) -> RunOutcome:
        return RunOutcome("failed", "stale", [], problems=("sunrise-clinic: faq 2026-10 x",))

    world.execute = stale  # type: ignore[assignment]

    assert world.run("ingest") == 1
    assert "PROBLEM  sunrise-clinic: faq 2026-10 x" in capsys.readouterr().err


# ── Mutation-audit additions and the decisions of 2026-10-22 ────────────────────────────────


@pytest.mark.usefixtures("checked_and_selected")
def test_a_live_run_whose_stdout_is_not_a_terminal_sends_nothing(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # M10b: the cap is typed at a terminal, but the plan went to a file or a pipe.
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    world.stdout_terminal = False

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1

    assert world.execute.calls == [] and run_dirs(world) == []
    assert not (world.root / "evals" / "reports").exists()  # no folder, no ledger
    err = capsys.readouterr().err
    assert "stdout is not one" in err and "nothing was sent" in err


@pytest.fixture
def unmarked_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paws's corpus manifest says `synthetic: false` (a future real pilot's data; today's parser
    refuses it, so the TEST-ONLY copy is made after reading). Sunrise's still says true."""
    read = read_dataset

    def unmarked(dataset_dir: Path, corpus_dir: Path | None = None) -> Any:
        dataset = read(dataset_dir, corpus_dir)
        paws = dataset.corpus.manifests[PAWS]
        dataset.corpus.manifests[PAWS] = paws.model_copy(update={"synthetic": False})
        return dataset

    monkeypatch.setattr(cli, "read_dataset", unmarked)


@pytest.mark.usefixtures("unmarked_corpus")
def test_a_corpus_manifest_not_marked_synthetic_is_never_reported_as_synthetic(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # M15a (run.json), M15b (summary.md and the plan): one business's manifest is enough.
    assert world.run("run", "--split", "dev") == 0

    [out] = run_dirs(world)
    info = RunInfo.model_validate_json((out / RUN_FILE).read_text())
    assert (info.dataset_synthetic, info.corpus_synthetic) == (True, False)
    summary = (out / "summary.md").read_text()
    assert "NOT synthetic (dataset manifest `synthetic: true`, corpus manifests" in summary
    assert "`synthetic: false`)" in summary and "synthetic: NO" in summary
    assert "fictional businesses" not in summary
    plan = capsys.readouterr().out
    assert (
        "NOT synthetic data (dataset manifest synthetic: true, corpus manifests synthetic: false)"
        in plan
    )


@pytest.mark.usefixtures("unmarked_corpus", "checked_and_selected")
def test_a_live_run_on_data_not_marked_synthetic_is_refused_before_anything(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # M15c, decided 2026-10-22: a live folder commits the model's answers, so --live needs both
    # manifests to say synthetic. Everything else here would pass (paid Settings, cap typed).
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 1

    assert world.execute.calls == [] and run_dirs(world) == []
    err = capsys.readouterr().err
    assert "--live needs data marked synthetic" in err
    assert "corpus manifests synthetic: false" in err and "nothing was sent" in err


def test_a_stop_tripped_by_the_last_question_exits_0_and_stays_out_of_the_note(
    world: World,
) -> None:
    # Decided 2026-10-22, as M1: complete, exit 0; the status line and "Not for the note" say why.
    stop = "after question test-only-3: an answer call ended invalid_request: TEST-ONLY"
    world.execute.stop = stop

    assert world.run("run", "--split", "dev") == 0

    [out] = run_dirs(world)
    info = RunInfo.model_validate_json((out / RUN_FILE).read_text())
    assert (info.status, info.stop) == ("complete", stop)
    assert f"stopped by its last question: {stop}" in (out / "summary.md").read_text()


@pytest.mark.usefixtures("checked_and_selected")
def test_a_leak_exits_4_even_when_writing_its_summary_fails_afterwards(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # Decided 2026-10-22: a leak is a hard gate, so its exit code never turns into a plain failure.
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    world.execute.status, world.execute.stop = "aborted_leak", "CROSS-BUSINESS LEAK: TEST-ONLY"
    world.metrics.mean_hit_offset = Fraction(1, 1000)  # the summary step fails

    assert world.run("run", "--split", "dev", "--live", "--max-usd", "0.50") == 4

    err = capsys.readouterr().err
    assert "the run FAILED, no summary written" in err and "Run status aborted_leak" in err
