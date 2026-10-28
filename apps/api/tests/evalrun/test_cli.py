"""`python -m vectapilot_api.evalrun` (`evalrun/cli.py`) with its outside world replaced: a
TEST-ONLY copy of the dataset in pytest's tmp_path (the real synthetic corpus, read only), a fake
git, a stand-in `execute` (no Postgres, no paid call) and H2 stand-ins. Nothing here passes a
real key; the "live" paths run with fake Settings values and the stand-in, so nothing is sent."""

import dataclasses
import io
import json
import tomllib
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
    HYBRID_CONFIG,
    PAWS,
    PAWS_STORE_HOURS,
    REPO,
    SENTINEL_QUESTION,
    PreparedMetrics,
    question,
    run_info,
)
from vectapilot_api.answer.prompt import PROMPT_VERSION
from vectapilot_api.config import Settings
from vectapilot_api.evalrun import cli
from vectapilot_api.evalrun.cli import Io, Refused, committed_dev_run
from vectapilot_api.evalrun.dryrun import DRY_RUN_ANSWER
from vectapilot_api.evalrun.harness import RunConfig, RunOutcome, Target
from vectapilot_api.evalrun.ingest import IngestCount
from vectapilot_api.evalrun.ledger import LEDGER_FILE, LedgerEntry, append_ledger, read_ledger
from vectapilot_api.evalrun.report import (
    RUN_FILE,
    CostInfo,
    RunInfo,
    RunStatus,
    fingerprint_digest,
    language_map_sha256,
    run_fingerprint_payload,
)
from vectapilot_api.evalrun.scoring import (
    AnswerLine,
    ComponentHit,
    ComponentLists,
    HarnessTime,
    HitRef,
    QuestionResult,
    base_fields,
)
from vectapilot_api.evalrun.spend import BRANCH_LINE, RECORDED_SPEND_FILE
from vectapilot_api.evalset.checks import CheckResult
from vectapilot_api.evalset.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.evalset.questions import EvalQuestion, Split, dump_question
from vectapilot_api.evalset.ranking import has_evidence
from vectapilot_api.evalset.split import build_freeze
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.llm_cli import GitError, GitInfo
from vectapilot_api.retrieval.search import RetrievalConfig

COMMIT: Final = "a" * 40
HEAD_REF: Final = "branch m3/close-out at aaaaaaa"
"""The fake git's `read_head_ref` (TEST-ONLY)."""
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
            hit_groups = (0,) if scored else ()
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
                    "components": (
                        ComponentLists(
                            vector=(ComponentHit(rank=1, chunk_id="c1", groups=hit_groups),),
                            text=(),
                            harness=HarnessTime(vector_ms=0.0, text_ms=0.0, fuse_ms=0.0),
                        )
                        if config.hybrid
                        else None
                    ),
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
            head_paths=lambda root, under: [p for p in self.head if p.is_relative_to(under)],
            head_ref=lambda root: HEAD_REF,
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


# ── The retrieval mode (M3 PR 5) ─────────────────────────────────────────────────────────────


def run_infos(world: World) -> list[RunInfo]:
    return [RunInfo.model_validate_json((d / RUN_FILE).read_text()) for d in run_dirs(world)]


@pytest.mark.parametrize(
    ("settings_mode", "flag", "mode"),
    [
        ("vector", None, "vector"),
        ("hybrid", None, "hybrid"),
        ("vector", "hybrid", "hybrid"),
        ("hybrid", "vector", "vector"),
    ],
)
def test_the_run_measures_the_retrieval_mode_flag_else_settings(
    world: World, settings_mode: str, flag: str | None, mode: str
) -> None:
    # PR 4's refusal of a hybrid setting is gone (its two tests removed with it): the run's mode
    # is --retrieval-mode, like --min-score, and Settings' rag_retrieval_mode is its default.
    world.settings = world.settings.model_copy(update={"rag_retrieval_mode": settings_mode})

    assert world.run("run", "--split", "dev", *(("--retrieval-mode", flag) if flag else ())) == 0

    [call] = world.execute.calls
    assert call["config"].retrieval == RetrievalConfig(mode=mode, candidate_k=20, rrf_k=60)  # type: ignore[arg-type]
    [out] = run_dirs(world)
    raw = json.loads((out / RUN_FILE).read_text())
    if mode == "vector":
        assert "retrieval" not in raw["config"]  # absent, never null: M2's run.json
    else:
        assert raw["config"]["retrieval"] == {
            "mode": "hybrid",
            "candidate_k": 20,
            "rrf_k": 60,
            "language_map_sha256": language_map_sha256(),
        }


M2_RUN_KEYS: Final = frozenset(
    {
        "record_version", "run_id", "status", "stop", "problems", "dry_run", "environment",
        "dataset", "dataset_version", "dataset_stage", "dataset_synthetic", "corpus_synthetic",
        "freeze_sha256", "dataset_committed", "corpus_review_pending", "split",
        "questions_selected", "commit", "uncommitted_changes", "started_at", "ended_at",
        "embeddings_provider", "embedding_model", "embedding_models_answered", "llm_provider",
        "answer_alias", "answer_model", "answer_models_answered", "catalog_version",
        "prompt_version", "config", "config_fingerprint", "namespaces", "confidence",
        "bootstrap_seed", "bootstrap_resamples", "rate_min_n", "bootstrap_min_n", "cost",
        "ingest", "test_rerun_reason", "test_runs_before_config", "test_runs_before_freeze",
    }
)  # fmt: skip
"""RunInfo's fields at 4140a48 (PR 6 merged, before PR 5), typed out from that commit."""


@pytest.mark.parametrize("flags", [(), ("--m3-spend",)], ids=["vector", "vector-m3-spend"])
def test_a_vector_run_json_has_exactly_m2s_keys_unless_named_by_m3_spend(
    world: World, flags: tuple[str, ...]
) -> None:
    # Review of PR 5, ananya A5: M2's RunInfo (extra="forbid") must still read a vector run.json.
    assert world.run("run", "--split", "dev", *flags) == 0

    [out] = run_dirs(world)
    raw = json.loads((out / RUN_FILE).read_text())
    if flags:
        assert set(raw) == M2_RUN_KEYS | {"m3_spend"} and raw["m3_spend"] is True
    else:
        assert set(raw) == M2_RUN_KEYS
        assert RunInfo.model_validate(raw).m3_spend is False  # read back as False
    assert "retrieval" not in raw["config"]


def test_a_vector_runs_fingerprint_stays_m2s_and_a_hybrid_one_has_its_own(world: World) -> None:
    assert world.run("run", "--split", "dev") == 0
    assert world.run("run", "--split", "dev", "--retrieval-mode", "vector") == 0
    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid") == 0

    plain, vector, hybrid = run_infos(world)
    assert plain.config_fingerprint == vector.config_fingerprint != hybrid.config_fingerprint
    for info in (plain, hybrid):
        assert info.config_fingerprint == fingerprint_digest(run_fingerprint_payload(info))
    # One namespace per business, embedding model and chunk config, whatever the mode.
    assert plain.namespaces == hybrid.namespaces
    mappings = [call["target"].mapping for call in world.execute.calls]
    assert mappings[0] == mappings[2]


def test_a_hybrid_plan_names_its_retrieval(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid") == 0

    out = capsys.readouterr().out
    assert (
        "Retrieval: hybrid · candidate k 20 (each leg, and the fused list) · RRF k 60 · language"
        f" map {language_map_sha256()[:12]} · the top 5 fused hits reach the evidence gate"
    ) in out
    assert "M3 spend" not in out  # a dry run is never M3's spend


VECTOR_DEV: Final = PurePosixPath("evals/reports/support_qa/v0.1/20261024T000000Z-live-dev")
"""A TEST-ONLY live vector dev folder (M2's, in a real repository) in tmp_path."""


def plant_vector_dev_run(
    world: World, mapping: dict[str, str], *, committed: bool = True, **changes: Any
) -> None:
    info = run_info(
        **({"run_id": VECTOR_DEV.name, "dry_run": False, "namespaces": mapping} | changes)
    )
    content = info.model_dump_json(indent=2).encode()
    (world.root / VECTOR_DEV).mkdir(parents=True)
    (world.root / VECTOR_DEV / RUN_FILE).write_bytes(content)
    if committed:
        world.head[VECTOR_DEV / RUN_FILE] = content


def hybrid_mapping(world: World, capsys: pytest.CaptureFixture[str]) -> tuple[dict[str, str], str]:
    """A first hybrid dry run, with no committed vector run: its namespaces and plan."""
    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid") == 0
    [call] = world.execute.calls
    world.execute.calls.clear()
    return dict(call["target"].mapping), capsys.readouterr().out


def test_a_hybrid_plan_says_no_document_embedding_only_on_a_committed_vector_runs_namespaces(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # Review of PR 5, ananya A1: the line is checked against HEAD, never assumed.
    mapping, out = hybrid_mapping(world, capsys)
    ids = ", ".join(sorted(mapping.values()))
    assert "Document embeddings: WARNING" in out  # nothing committed yet: the other test

    plant_vector_dev_run(world, mapping)
    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid") == 0

    out = capsys.readouterr().out
    assert (
        f"Document embeddings: same namespaces as {VECTOR_DEV.name} (a complete live vector dev"
        f" run committed in HEAD): no document embedding expected. The eval namespaces ({ids})"
        " depend on the embedding model and chunk config, never on the retrieval mode; ingest"
        " embeds only documents a namespace does not hold yet (D3's volume rule: no reset-db or"
        " volume prune since that run), and the text leg embeds nothing: one query embedding per"
        " question, as in vector mode."
    ) in out.splitlines()
    assert "WARNING" not in out


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"committed": False},
        {"config": HYBRID_CONFIG},
        {"dry_run": True},
        {"status": "partial", "stop": "TEST-ONLY stop"},
        {"other_namespaces": True},
    ],
    ids=["none", "not-committed", "hybrid", "dry", "partial", "other-namespaces"],
)
def test_a_hybrid_plan_warns_when_no_committed_vector_run_used_its_namespaces(
    world: World, capsys: pytest.CaptureFixture[str], changes: dict[str, Any]
) -> None:
    mapping, _ = hybrid_mapping(world, capsys)
    ids = ", ".join(sorted(mapping.values()))
    if changes:
        planted = dict(mapping)
        if changes.pop("other_namespaces", False):
            planted[PAWS] = "paws-and-co-ev-cccccccccc"  # another chunk config's namespace
        plant_vector_dev_run(world, planted, **changes)

    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid") == 0

    out = capsys.readouterr().out
    assert (
        "Document embeddings: WARNING: no committed live vector dev run used these namespaces"
        f" ({ids}): ingest may embed the corpus (worst case $0), and compare refuses a different"
        " chunk config. For M3's hybrid dev runs, pass M2's dev folder's --max-chars and"
        " --overlap (and --search-k) when they are not the defaults"
        " (docs/learning/m2-eval-run.md §6)."
    ) in out.splitlines()
    assert "no document embedding expected" not in out


def test_a_hybrid_plan_with_fresh_never_says_no_document_is_embedded(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    mapping, _ = hybrid_mapping(world, capsys)
    plant_vector_dev_run(world, mapping)  # even on a committed vector run's namespaces

    assert world.run("run", "--split", "dev", "--retrieval-mode", "hybrid", "--fresh") == 0

    out = capsys.readouterr().out
    assert "Document embeddings" not in out  # the --fresh line says what is embedded again
    assert "--fresh: deletes the documents of 2 eval business(es) of this config first" in out


def test_a_vector_plan_says_vector_and_nothing_about_documents(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.run("run", "--split", "dev") == 0

    out = capsys.readouterr().out
    assert "Retrieval: vector (no retrieval block)" in out
    assert "Document embeddings" not in out


def test_a_hybrid_search_k_above_the_candidate_k_is_refused_before_anything(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    hybrid = ("run", "--split", "dev", "--retrieval-mode", "hybrid")

    assert world.run(*hybrid, "--search-k", "21") == 1

    assert world.execute.calls == [] and run_dirs(world) == []
    assert "--search-k 21 is above rag_candidate_k 20: in hybrid mode the search k" in (
        capsys.readouterr().err
    )
    # At the candidate k it runs; vector mode has no candidate k.
    assert world.run(*hybrid, "--search-k", "20") == 0
    assert world.run("run", "--split", "dev", "--search-k", "21") == 0


def test_ingest_runs_in_either_retrieval_mode(world: World) -> None:
    world.settings = world.settings.model_copy(update={"rag_retrieval_mode": "hybrid"})

    assert world.run("ingest") == 0

    [call] = world.execute.calls
    assert call["ingest_only"] is True


# ── M3's spend before a live run (M3 PR 5, D15 (iii)) ────────────────────────────────────────

M3_FOLDER: Final = PurePosixPath("evals/reports/support_qa/v0.1/20261101T000000Z-live-dev")


def plant_m3_folder(world: World, total: str, *, on_disk: bool = True, **changes: Any) -> None:
    """A TEST-ONLY committed hybrid live folder in the fake HEAD (and on disk) costing `total`."""
    info = run_info(
        run_id=M3_FOLDER.name,
        dry_run=False,
        config=HYBRID_CONFIG,
        cost=CostInfo(
            cap_usd="1.00",
            embedding_usd="0",
            llm_usd=total,
            total_usd=total,
            llm_cost_complete=True,
            embedding_requests=1,
            llm_calls=1,
        ),
        **changes,
    )
    content = info.model_dump_json(indent=2).encode()
    world.head[M3_FOLDER / RUN_FILE] = content
    if on_disk:
        (world.root / M3_FOLDER).mkdir(parents=True)
        (world.root / M3_FOLDER / RUN_FILE).write_bytes(content)


LIVE: Final = ("run", "--split", "dev", "--live", "--max-usd", "0.50")


@pytest.mark.usefixtures("checked_and_selected")
@pytest.mark.parametrize(
    ("flags", "why"),
    [
        (("--retrieval-mode", "hybrid"), "hybrid (retrieval block)"),
        (("--m3-spend",), "vector, named by --m3-spend"),
    ],
    ids=["hybrid", "named-vector"],
)
def test_a_live_run_of_m3_shows_its_spend_and_records_whether_it_counts(
    world: World, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...], why: str
) -> None:
    plant_m3_folder(world, "1.20")
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, *flags) == 0

    out = capsys.readouterr().out
    assert (
        "M3 spend so far (D15 (iii); live folders committed in HEAD (branch m3/close-out at"
        " aaaaaaa) that are M3's: hybrid, named by --m3-spend, or without run.json): $1.2 in 1"
        f" folder(s) ({M3_FOLDER.name} $1.2) · ceiling $5 · left $3.8. This run counts toward it:"
        f" yes ({why})."
    ) in out
    # Decided 2026-10-27 (shesha): the plan and the closing line name the one branch.
    assert BRANCH_LINE in out.splitlines()
    new = [info for info in run_infos(world) if info.run_id != M3_FOLDER.name]
    [info] = new
    assert (
        f"Written to {world.root / M3_FOLDER.parent / info.run_id} (a live run of M3: commit"
        " this folder on m3/close-out (labels.jsonl after labelling))."
    ) in out.splitlines()
    assert info.m3_spend is ("--m3-spend" in flags)
    assert (info.config.retrieval is not None) is ("--retrieval-mode" in flags)


@pytest.mark.usefixtures("checked_and_selected")
@pytest.mark.parametrize("flags", [("--retrieval-mode", "hybrid"), ("--m3-spend",)])
def test_a_cap_above_what_is_left_of_m3_is_refused_before_the_prompt_and_any_call(
    world: World, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    plant_m3_folder(world, "4.80")
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, *flags) == 1

    assert world.execute.calls == [] and len(run_dirs(world)) == 1  # only the planted folder
    assert world.stdin.read() == "0.50\n"  # the cap prompt never read it
    assert (
        "--max-usd $0.5 is more than what is left of M3's $5 ceiling (D15 (iii)): $4.8 spent in 1"
        f" folder(s) ({M3_FOLDER.name} $4.8), left $0.2. Type a cap within what is left; nothing"
        " was sent"
    ) in capsys.readouterr().err


@pytest.mark.usefixtures("checked_and_selected")
def test_m2s_live_vector_run_is_not_m3s_and_never_refused_by_its_ceiling(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    plant_m3_folder(world, "5.00")
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE) == 0

    assert "This run counts toward it: no." in capsys.readouterr().out


@pytest.mark.usefixtures("checked_and_selected")
def test_a_live_folder_of_m3_not_committed_refuses_the_next_live_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    plant_m3_folder(world, "0.10")
    del world.head[M3_FOLDER / RUN_FILE]  # on disk only: never committed
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, "--retrieval-mode", "hybrid") == 1

    assert world.execute.calls == []
    assert (
        f"a live folder of M3 is not committed as it is on disk ({M3_FOLDER}): M3's spend counts"
        " committed folders only (D15 (iii)), so commit each live folder before the next live run"
    ) in capsys.readouterr().err


@pytest.mark.usefixtures("checked_and_selected")
def test_an_unreadable_committed_run_json_refuses_a_run_of_m3_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.head[M3_FOLDER / RUN_FILE] = b"{}"
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, "--retrieval-mode", "hybrid") == 1
    assert "M3's spend cannot be counted: " in capsys.readouterr().err
    assert world.execute.calls == []
    # M2's vector run only says so.
    world.stdin = Terminal("0.50\n")
    assert world.run(*LIVE) == 0
    assert (
        "M3 spend: this run does not count toward M3's $5.00 (vector mode without --m3-spend),"
        " and M3's spend could not be read from HEAD."
    ) in capsys.readouterr().out


@pytest.mark.usefixtures("checked_and_selected")
def test_the_spend_line_names_the_branch_and_a_head_it_cannot_name_refuses_a_run_of_m3(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # Review of PR 5, diego D1: HEAD is one branch's, so the plan names the one it read.
    def no_ref(root: Path) -> str:
        raise GitError("`git rev-parse --abbrev-ref HEAD` failed: run inside the repository")

    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    argv = ["--dataset", str(world.dataset), "--corpus", str(CORPUS_DIR), *LIVE]

    assert cli.run([*argv, "--retrieval-mode", "hybrid"], world.io()) == 0
    assert "live folders committed in HEAD (branch m3/close-out at aaaaaaa)" in (
        capsys.readouterr().out
    )
    world.execute.calls.clear()
    failing = dataclasses.replace(world.io(), head_ref=no_ref)

    world.stdin = Terminal("0.50\n")
    assert cli.run([*argv, "--retrieval-mode", "hybrid"], failing) == 1
    assert world.execute.calls == []
    assert (
        "M3's spend cannot be counted: `git rev-parse --abbrev-ref HEAD` failed: run inside the"
        " repository; nothing was sent"
    ) in capsys.readouterr().err


# ── A live folder without run.json (decided 2026-10-27, shesha) ──────────────────────────────

NO_RUN_JSON: Final = PurePosixPath("evals/reports/support_qa/v0.1/20261102T000000Z-live-dev")
"""A TEST-ONLY live folder committed without run.json (a killed run), in the fake HEAD only."""


@pytest.mark.usefixtures("checked_and_selected")
def test_a_live_folder_without_run_json_counts_at_the_ceiling_until_its_spend_is_recorded(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    # By hand: 3.60 (run.json) + 1.00 (no run.json: the per-run ceiling) = 4.60, left 0.40, so a
    # cap of 0.50 is refused before the prompt. Recorded at 0.10: 3.70 spent, left 1.30, it runs.
    plant_m3_folder(world, "3.60")
    world.head[NO_RUN_JSON / "llm_call_log.jsonl"] = b""
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, "--retrieval-mode", "hybrid") == 1

    captured = capsys.readouterr()
    assert world.execute.calls == [] and world.stdin.read() == "0.50\n"
    assert (
        f"$4.6 in 2 folder(s) ({M3_FOLDER.name} $3.6, {NO_RUN_JSON.name} $1 (no run.json: the"
        " per-run ceiling)) · ceiling $5 · left $0.4."
    ) in captured.out
    assert (
        "--max-usd $0.5 is more than what is left of M3's $5 ceiling (D15 (iii)): $4.6 spent in 2"
        f" folder(s) ({M3_FOLDER.name} $3.6, {NO_RUN_JSON.name} $1 (no run.json: the per-run"
        " ceiling)), left $0.4. Type a cap within what is left, or record the real spend of a"
        f" folder counted at the per-run ceiling in its {RECORDED_SPEND_FILE} (runbook §6)"
    ) in captured.err

    world.head[NO_RUN_JSON / RECORDED_SPEND_FILE] = json.dumps(
        {
            "total_usd": "0.10",
            "counts_toward_m3": True,
            "recorded_by": "shesha",
            "recorded_on": "2026-11-02",
            "source": "TEST-ONLY",
        }
    ).encode()
    world.stdin = Terminal("0.50\n")

    assert world.run(*LIVE, "--retrieval-mode", "hybrid") == 0

    assert (
        f"$3.7 in 2 folder(s) ({M3_FOLDER.name} $3.6, {NO_RUN_JSON.name} $0.1 (no run.json:"
        " recorded)) · ceiling $5 · left $1.3."
    ) in capsys.readouterr().out
    assert len(world.execute.calls) == 1


@pytest.mark.usefixtures("checked_and_selected")
@pytest.mark.parametrize("flags", [(), ("--retrieval-mode", "hybrid")], ids=["m2", "m3"])
def test_a_failed_live_run_of_m3_says_never_to_delete_its_folder(
    world: World, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...]
) -> None:
    # Deleting a live folder of M3 would drop its spend from the sum, so its failure line says
    # to commit it on m3/close-out instead of M1's "or delete it"; M2's line is unchanged.
    world.settings = make_settings(**PAID)
    world.stdin = Terminal("0.50\n")
    world.metrics.mean_hit_offset = Fraction(1, 1000)  # the summary fails after the calls

    assert world.run(*LIVE, *flags) == 1

    [out] = run_dirs(world)
    err = capsys.readouterr().err
    if flags:
        assert (
            f"Its inputs are in {out}; this run counts toward M3's $5: commit this folder on"
            " m3/close-out, never delete it (without run.json it counts at the $1 per-run"
            " ceiling until its spend is recorded in recorded-spend.json; runbook §6)."
        ) in err
        assert "or delete it" not in err
    else:
        assert f"Its inputs are in {out}; commit it as evidence only, or delete it" in err


# ── run.json's versions and its fingerprint (mutation audit M7b) ─────────────────────────────

MODELS_TOML: Final = REPO / "apps" / "api" / "src" / "vectapilot_api" / "gateway" / "models.toml"
BUMPED_CATALOG: Final = "test-only-catalog.9"
BUMPED_PROMPT: Final = "test-only-prompt.9"


@pytest.mark.parametrize("mode", ["vector", "hybrid"])
@pytest.mark.parametrize("bump", [None, "catalog", "prompt", "both"])
def test_run_json_records_the_live_versions_and_a_fingerprint_of_its_own_fields(
    world: World, monkeypatch: pytest.MonkeyPatch, mode: str, bump: str | None
) -> None:
    # Mutation audit M7b. D3's pin test reads the live versions for its own fixed config only;
    # this checks the CLI's run.json. Its catalog_version and prompt_version must be the live ones
    # (gateway/models.toml, answer/prompt.py's PROMPT_VERSION), and its config_fingerprint must
    # hash its own recorded fields. A CLI that recorded or hashed a literal would agree with
    # today's values, so the live values are bumped here (TEST-ONLY) to make the two diverge.
    live = (tomllib.loads(MODELS_TOML.read_text("utf-8"))["catalog_version"], PROMPT_VERSION)
    # The CLI's own sources (cli imports PROMPT_VERSION from answer/prompt.py by name).
    assert live == (load_catalog().catalog_version, vars(cli)["PROMPT_VERSION"])
    expected = live
    if bump in ("catalog", "both"):
        bumped = load_catalog().model_copy(update={"catalog_version": BUMPED_CATALOG})
        monkeypatch.setattr(cli, "load_catalog", lambda: bumped)
        expected = (BUMPED_CATALOG, expected[1])
    if bump in ("prompt", "both"):
        monkeypatch.setattr(cli, "PROMPT_VERSION", BUMPED_PROMPT)
        expected = (expected[0], BUMPED_PROMPT)

    assert world.run("run", "--split", "dev", "--retrieval-mode", mode) == 0

    [info] = run_infos(world)
    assert (info.catalog_version, info.prompt_version) == expected
    assert info.config_fingerprint == fingerprint_digest(run_fingerprint_payload(info))
    if bump is not None:  # the bump reached the fingerprint, not only the recorded field
        unbumped = info.model_copy(update={"catalog_version": live[0], "prompt_version": live[1]})
        assert info.config_fingerprint != fingerprint_digest(run_fingerprint_payload(unbumped))
