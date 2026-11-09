"""`python -m vectapilot_api.intenteval embed|train|calibrate|thresholds|pin|run|summarize|dry-run`
end to end (M4 PR 4), on a TEST-ONLY repository in tmp_path (`harness_helpers.frozen_copy`).

Nothing here is sent anywhere: the "live" paths run with test-only Settings values and an Io that
builds the fake embedder and the dry provider, so a "live" folder here is a fake one in tmp_path
and every number is plumbing. HEAD is a dict (`FakeRepo`). H1 and H2 are stand-ins (deliberately
not H1 or H2), except where a test checks what happens while they are unwritten.
"""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Final, NoReturn

import pytest

from tests.intenteval.harness_helpers import (
    VERSION,
    FakeRepo,
    Now,
    StandInMetrics,
    Terminal,
    dry_model,
    folders,
    frozen_copy,
    live_embed,
    make_io,
    real_model,
    run_args,
    texts,
)
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.base import Embedder, EmbeddingSpec
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.intenteval.cli import harness_main
from vectapilot_api.intenteval.hybrid import H1_MISSING
from vectapilot_api.intenteval.ledger import LEDGER_PATH, PinEntry, RunEntry, read_entries
from vectapilot_api.intenteval.metrics import IntentMetrics
from vectapilot_api.intenteval.runinfo import (
    DRY_INTENT_REPORTS,
    DRY_LABEL,
    INTENT_REPORTS,
    read_results,
    read_run,
)
from vectapilot_api.intenteval.runs import H2_MISSING
from vectapilot_api.intents.decide import RouteDecision

CLASSIFIER_LIVE: Final = [
    *("run", "--split", "dev", "--mode", "classifier"),
    *("--model", "m", "--live", "--max-usd", "1"),
]


@pytest.fixture
def world(tmp_path: Path) -> tuple[FakeRepo, Path, Now]:
    root = tmp_path.resolve()
    return FakeRepo(root), frozen_copy(root), Now()


def no_paid_embedder(settings: Settings, spec: EmbeddingSpec) -> Embedder:
    raise AssertionError("paid embedder built in a dry run")


def no_paid_provider(settings: Settings) -> Provider:
    raise AssertionError("paid provider built in a dry run")


def test_the_dry_run_writes_every_folder_under_tmp_and_no_text(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    metrics = StandInMetrics()
    io = make_io(repo, metrics=metrics, now=now)
    # A dry run never builds a paid client, whatever Settings say.
    io = replace(io, build_embedder=no_paid_embedder, build_provider=no_paid_provider)

    code = harness_main(["--dataset", str(dataset), "dry-run"], io)

    assert code == 0
    assert not (repo.root / INTENT_REPORTS / VERSION).exists()  # nothing real
    for suffix in ("-fake-embed", "-fake-model", "-fake-classifier-dev", "-fake-llm-dev"):
        assert len(folders(repo.root, False, suffix)) == 1, suffix
    [hybrid] = folders(repo.root, False, "-fake-hybrid-dev")
    summary = (hybrid / "summary.md").read_text("utf-8")
    assert summary.splitlines()[2].startswith(f"**{DRY_LABEL}**")
    assert "## Comparison against LLM-only (paired)" in summary
    run = read_run(hybrid)
    assert run.dry_run and run.kind == "hybrid" and len(run.inputs) == 2
    # No row's text in any file written, results and logs included.
    written = "\n".join(
        p.read_text("utf-8") for p in (repo.root / DRY_INTENT_REPORTS).rglob("*") if p.is_file()
    )
    assert all(text not in written for text in texts(dataset))
    # H2's stand-in got every scored row: gold and decided per row, for each summary.
    assert metrics.matrices and all(len(g) == len(d) for g, d in metrics.matrices)


def unwritten(*args: object, **kwargs: object) -> NoReturn:
    raise NotImplementedError


UNWRITTEN_H2: Final = IntentMetrics(
    confusion_matrix=unwritten,
    macro_f1=unwritten,
    reliability_bins=unwritten,
    expected_calibration_error=unwritten,
)


async def unwritten_router(*args: object, **kwargs: object) -> RouteDecision:
    raise NotImplementedError


def test_with_h2_unwritten_a_run_keeps_its_results_and_writes_no_summary(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = dry_model(repo, dataset, now)
    io = make_io(repo, now=now)
    # A local stand-in for H2 unwritten (not the real stubs), so this stays green once H2 is.
    real_h2 = replace(io, metrics=UNWRITTEN_H2)

    code = harness_main(
        ["--dataset", str(dataset), "run", "--split", "dev", "--mode", "classifier", "--model",
         str(model)],
        real_h2,
    )  # fmt: skip

    assert code == 1
    [run] = folders(repo.root, False, "-fake-classifier-dev")
    assert (run / "run.json").is_file() and (run / "results.jsonl").is_file()
    assert not (run / "summary.md").exists()
    assert H2_MISSING in capsys.readouterr().out


def test_with_h1_unwritten_the_hybrid_is_refused(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    io = make_io(repo, now=now, router=unwritten_router)  # H1 unwritten, as a local stand-in

    assert harness_main(["--dataset", str(dataset), "dry-run"], io) == 1
    assert H1_MISSING in capsys.readouterr().err
    assert folders(repo.root, False, "-fake-hybrid-dev") == []


@pytest.mark.parametrize(
    "argv",
    [
        ["embed", "--live"],
        ["embed", "--live", "--max-usd", "1.50"],
        ["embed", "--max-usd", "0.50"],
        CLASSIFIER_LIVE,
        ["run", "--split", "dev", "--mode", "classifier"],
        ["run", "--split", "dev", "--mode", "llm", "--model", "m"],
        ["run", "--split", "test", "--mode", "llm", "--test-rerun-reason", "Not A Slug"],
        ["summarize"],
        ["summarize", "x", "--classifier", "c", "--llm", "l"],
        ["summarize", "--classifier", "c"],
    ],
    ids=[
        "live-without-cap",
        "cap-above-1",
        "cap-without-live",
        "classifier-live",
        "classifier-without-model",
        "llm-with-model",
        "reason-not-slug",
        "summarize-nothing",
        "summarize-both",
        "summarize-half",
    ],
)
def test_impossible_arguments_are_refused_before_anything_runs(
    world: tuple[FakeRepo, Path, Now], argv: list[str]
) -> None:
    repo, dataset, now = world

    with pytest.raises(SystemExit) as exited:
        harness_main(["--dataset", str(dataset), *argv], make_io(repo, now=now))

    assert exited.value.code == 2
    assert not (repo.root / "evals" / "reports").exists()


def test_a_dry_model_never_touches_test(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = dry_model(repo, dataset, now)
    argv = ["--dataset", str(dataset), "run", "--split", "test", "--mode", "classifier"]

    assert harness_main([*argv, "--model", str(model)], make_io(repo, now=now)) == 1
    assert "dry runs are refused on the test split" in capsys.readouterr().err
    assert harness_main(["--dataset", str(dataset), "pin", str(model)], make_io(repo, now=now)) == 1
    assert "dry model" in capsys.readouterr().err
    assert not (repo.root / LEDGER_PATH).exists()


# ── The "live" path on fakes, and the test discipline ─────────────────────────────────────────


def test_a_live_embed_needs_the_cap_typed_at_a_terminal(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world

    assert live_embed(repo, dataset, now, typed="0.5\n") == 1  # not exactly as given
    assert "not confirmed" in capsys.readouterr().err
    piped = make_io(repo, stdin=Terminal("0.50\n"), terminal=False, now=now)
    argv = ["--dataset", str(dataset), "embed", "--live", "--max-usd", "0.50"]
    assert harness_main(argv, piped) == 1
    assert folders(repo.root, True, "-live-embed") == []

    assert live_embed(repo, dataset, now) == 0
    out = capsys.readouterr().out
    assert "M4 spend so far (D7;" in out and "on m4/close-out" in out
    [embed] = folders(repo.root, True, "-live-embed")
    run = read_run(embed)
    assert (run.dry_run, run.status, run.embedding_model) == (False, "complete", "voyage-4")
    assert run.cost.cap_usd == Decimal("0.50")  # the typed --max-usd is the run's cap


def test_an_uncommitted_embed_folder_makes_everything_after_it_dry(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    repo, dataset, now = world
    assert live_embed(repo, dataset, now) == 0
    [embed] = folders(repo.root, True, "-live-embed")

    io = make_io(repo, now=now)
    assert harness_main(["--dataset", str(dataset), "train", "--embed", str(embed)], io) == 0

    [model] = folders(repo.root, False, "-fake-model")  # dry: the cache is not in HEAD
    assert "not committed in HEAD" in json.loads((model / "model.json").read_text())["dry_reason"]


def test_the_whole_discipline_from_dev_to_one_test_run_per_mode(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = real_model(repo, dataset, now)
    io = make_io(repo, now=now)

    # Dev, both modes, real: the classifier from the committed cache, the LLM "live".
    assert harness_main(run_args(dataset, "dev", "classifier", "--model", str(model)), io) == 0
    [classifier_dev] = folders(repo.root, True, "-local-classifier-dev")
    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(run_args(dataset, "dev", "llm", "--live", "--max-usd", "0.50"), live) == 0
    [llm_dev] = folders(repo.root, True, "-live-llm-dev")
    assert read_run(llm_dev).cost.cap_usd == Decimal("0.50")

    # The next live run is refused until that folder is committed (M4's spend, D7).
    again = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(run_args(dataset, "dev", "llm", "--live", "--max-usd", "0.50"), again) == 1
    assert "a live folder of M4 is not committed" in capsys.readouterr().err
    repo.commit(llm_dev)

    # The replayed hybrid on dev: real, from two real folders.
    hybrid = ["--dataset", str(dataset), "summarize", "--classifier", str(classifier_dev)]
    assert harness_main([*hybrid, "--llm", str(llm_dev)], io) == 0
    [hybrid_dev] = folders(repo.root, True, "-replay-hybrid-dev")
    hybrid_info = read_run(hybrid_dev)
    assert not hybrid_info.dry_run
    # Its fingerprint: the classifier's artifact, T, tau and sigma, and the LLM run's prompt.
    fp, fp_c, fp_l = (
        hybrid_info.fingerprint,
        read_run(classifier_dev).fingerprint,
        read_run(llm_dev).fingerprint,
    )
    assert (fp.artifact_sha256, fp.temperature, fp.tau, fp.sigma) == (
        fp_c.artifact_sha256,
        fp_c.temperature,
        fp_c.tau,
        fp_c.sigma,
    )
    assert None not in (fp.artifact_sha256, fp.temperature, fp.sigma)
    # tau is the thresholds file's, None included (no confidence band, decided 2026-11-09; the
    # fake embedder's dev rows may well qualify no grid value). The file always names tau.
    chosen = json.loads((model / "thresholds.json").read_text("utf-8"))
    assert "tau" in chosen and fp.tau == chosen["tau"]
    assert fp.no_confidence_band == (chosen["tau"] is None)
    assert (fp.router_prompt_version, fp.catalog_version, fp.router_model) == (
        fp_l.router_prompt_version,
        fp_l.catalog_version,
        fp_l.router_model,
    )
    assert hybrid_info.fingerprint_sha256 == fp.sha256()

    # Test needs a pin first, and the pin needs the model folder committed and a terminal.
    test_classifier = run_args(dataset, "test", "classifier", "--model", str(model))
    assert harness_main(test_classifier, make_io(repo, stdin=Terminal(""), now=now)) == 1
    assert "no pin" in capsys.readouterr().err
    pin = ["--dataset", str(dataset), "pin", str(model)]
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 1
    assert "is not committed in HEAD as on disk" in capsys.readouterr().err
    repo.commit(model)
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 0
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 1
    assert "not as committed in HEAD" in capsys.readouterr().err  # the ledger, first
    repo.commit(repo.root / LEDGER_PATH)
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 1
    assert "already pinned" in capsys.readouterr().err

    # One classifier test run, typed at a terminal; ledgered before it scores.
    typed = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(test_classifier, typed) == 0
    [classifier_test] = folders(repo.root, True, "-local-classifier-test")
    entries = read_entries(repo.root / LEDGER_PATH)
    assert [type(e) for e in entries] == [PinEntry, RunEntry]
    run_entry = entries[1]
    assert isinstance(run_entry, RunEntry)
    assert (run_entry.run_id, run_entry.mode) == (classifier_test.name, "classifier")
    repo.commit(classifier_test, repo.root / LEDGER_PATH)
    # A second one needs a reason.
    retyped = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(test_classifier, retyped) == 1
    assert "runs once per mode" in capsys.readouterr().err
    # With a reason it runs, and the ledger and run.json both record it.
    retyped = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main([*test_classifier, "--test-rerun-reason", "fix-x"], retyped) == 0
    rerun = folders(repo.root, True, "-local-classifier-test")[-1]
    assert rerun != classifier_test
    first, second = read_entries(repo.root / LEDGER_PATH)[1:]
    assert isinstance(first, RunEntry) and isinstance(second, RunEntry)
    assert (first.rerun_reason, second.rerun_reason, second.run_id) == (None, "fix-x", rerun.name)
    rerun_info = read_run(rerun)
    assert (rerun_info.test_rerun_reason, rerun_info.test_runs_before) == ("fix-x", 1)
    assert read_run(classifier_test).test_runs_before == 0
    repo.commit(rerun, repo.root / LEDGER_PATH)

    # LLM on test: needs the committed live dev run with the same fingerprint (it is).
    live_test = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    llm_test_args = run_args(dataset, "test", "llm", "--live", "--max-usd", "0.50")
    assert harness_main(llm_test_args, live_test) == 0
    [llm_test] = folders(repo.root, True, "-live-llm-test")
    repo.commit(llm_test, repo.root / LEDGER_PATH)
    # A second LLM test run needs a reason too, and records it.
    again = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(llm_test_args, again) == 1
    assert "runs once per mode" in capsys.readouterr().err
    again = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main([*llm_test_args, "--test-rerun-reason", "outage"], again) == 0
    llm_rerun = folders(repo.root, True, "-live-llm-test")[-1]
    last = read_entries(repo.root / LEDGER_PATH)[-1]
    assert isinstance(last, RunEntry)
    assert (last.mode, last.rerun_reason, last.run_id) == ("llm", "outage", llm_rerun.name)
    llm_rerun_info = read_run(llm_rerun)
    assert (llm_rerun_info.test_rerun_reason, llm_rerun_info.test_runs_before) == ("outage", 1)
    assert read_run(llm_test).test_rerun_reason is None
    repo.commit(llm_rerun, repo.root / LEDGER_PATH)

    # The hybrid on test replays the ledgered runs only, typed at a terminal.
    hybrid_test = ["--dataset", str(dataset), "summarize", "--classifier", str(classifier_test)]
    typed = make_io(repo, stdin=Terminal("test hybrid\n"), now=now)
    assert harness_main([*hybrid_test, "--llm", str(llm_test)], typed) == 0
    [hybrid_test_dir] = folders(repo.root, True, "-replay-hybrid-test")
    summary = (hybrid_test_dir / "summary.md").read_text("utf-8")
    assert "## Comparison against LLM-only (paired)" in summary
    assert DRY_LABEL not in summary
    # Every result line has ids and labels, never a text field.
    for folder in (classifier_test, llm_test, hybrid_test_dir):
        assert all("text" not in line.model_dump() for line in read_results(folder))
    written = "\n".join(
        p.read_text("utf-8") for p in (repo.root / INTENT_REPORTS).rglob("*") if p.is_file()
    )
    assert all(text not in written for text in texts(dataset))


def test_a_dirty_tree_never_runs_test(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = real_model(repo, dataset, now)
    repo.commit(model)
    repo.dirty = True

    pin = ["--dataset", str(dataset), "pin", str(model)]
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 1
    assert "clean working tree" in capsys.readouterr().err


def test_llm_on_test_needs_a_committed_live_dev_run_with_its_fingerprint(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = real_model(repo, dataset, now)
    repo.commit(model)
    pin = ["--dataset", str(dataset), "pin", str(model)]
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 0
    repo.commit(repo.root / LEDGER_PATH)

    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    args = run_args(dataset, "test", "llm", "--live", "--max-usd", "0.50")

    assert harness_main(args, live) == 1
    assert "no complete live LLM dev run with this fingerprint" in capsys.readouterr().err
    assert folders(repo.root, True, "-live-llm-test") == []
    assert len(read_entries(repo.root / LEDGER_PATH)) == 1  # the pin only: nothing ledgered
