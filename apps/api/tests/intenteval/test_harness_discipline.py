"""The test discipline's refusals, one rule at a time (M4 PR 4; plan, "Test discipline"), on the
TEST-ONLY repository of `harness_helpers.frozen_copy`. Each test starts from a state the rule must
refuse and checks that nothing is ledgered or written. HEAD is a dict; "live" runs build fakes;
H1 and H2 are stand-ins (deliberately not H1 or H2).
"""

import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.intenteval.harness_helpers import (
    FakeRepo,
    Now,
    Terminal,
    committed_test_runs,
    dry_model,
    folders,
    frozen_copy,
    hybrid_args,
    live_llm_args,
    make_io,
    pinned_model,
    real_model,
    reformat_json,
    rewrite_run,
    run_args,
)
from vectapilot_api.evalrun.ledger import append_ledger
from vectapilot_api.intenteval.cli import harness_main
from vectapilot_api.intenteval.ledger import LEDGER_PATH, PinEntry, read_entries
from vectapilot_api.intenteval.runinfo import (
    DRY_INTENT_REPORTS,
    DRY_LABEL,
    RESULTS_FILE,
    read_results,
    read_run,
)
from vectapilot_api.intents.classifier import IntentScores
from vectapilot_api.intents.decide import RouteDecision, RouterThresholds
from vectapilot_api.intents.fallback import IntentFallback
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER


@pytest.fixture
def world(tmp_path: Path) -> tuple[FakeRepo, Path, Now]:
    root = tmp_path.resolve()
    return FakeRepo(root), frozen_copy(root), Now()


def ledger_size(repo: FakeRepo) -> int:
    return len(read_entries(repo.root / LEDGER_PATH))


def replace_pin(repo: FakeRepo, **changes: Any) -> None:
    """Rewrite the committed ledger's only entry (the pin) with `changes`, and commit it."""
    [pin] = read_entries(repo.root / LEDGER_PATH)
    assert isinstance(pin, PinEntry)
    (repo.root / LEDGER_PATH).unlink()
    append_ledger(repo.root / LEDGER_PATH, pin.model_copy(update=changes))
    repo.commit(repo.root / LEDGER_PATH)


# ── classifier and LLM test runs ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["classifier", "llm"])
def test_a_test_run_on_a_dirty_tree_is_refused(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    repo, dataset, now = world
    model = pinned_model(repo, dataset, now)
    repo.dirty = True

    if mode == "classifier":
        args = run_args(dataset, "test", "classifier", "--model", str(model))
        io = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    else:
        args, io = live_llm_args(dataset, "test"), make_io(repo, stdin=Terminal("0.50\n"), now=now)

    assert harness_main(args, io) == 1
    assert "a test run needs a clean working tree" in capsys.readouterr().err
    assert ledger_size(repo) == 1 and folders(repo.root, True, "-test") == []


@pytest.mark.parametrize(
    "changes",
    [
        {"artifact_sha256": "0" * 64},
        {"temperature": 0.123},
        {"tau": 0.05},
        {"sigma": 0.999},
    ],
    ids=["artifact", "T", "tau", "sigma"],
)
def test_a_classifier_test_run_needs_the_pinned_artifact_t_tau_and_sigma(
    world: tuple[FakeRepo, Path, Now],
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
) -> None:
    repo, dataset, now = world
    model = pinned_model(repo, dataset, now)
    replace_pin(repo, **changes)

    typed = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(run_args(dataset, "test", "classifier", "--model", str(model)), typed) == 1

    assert "not the pinned artifact, T, tau and sigma" in capsys.readouterr().err
    assert ledger_size(repo) == 1 and folders(repo.root, True, "-test") == []


def test_a_pin_without_a_confidence_band_differs_from_one_with_a_band(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    # tau None ("no confidence band", decided 2026-11-09) is a value of its own: a pin with a band
    # never matches a model without one, and the reverse.
    repo, dataset, now = world
    model = pinned_model(repo, dataset, now)
    [pin] = read_entries(repo.root / LEDGER_PATH)
    assert isinstance(pin, PinEntry)
    replace_pin(repo, tau=0.05 if pin.tau is None else None)

    typed = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(run_args(dataset, "test", "classifier", "--model", str(model)), typed) == 1

    assert "not the pinned artifact, T, tau and sigma" in capsys.readouterr().err
    assert ledger_size(repo) == 1 and folders(repo.root, True, "-test") == []


@pytest.mark.parametrize("name", ["calibration.json", "thresholds.json"])
def test_a_classifier_test_run_needs_the_model_folder_as_committed(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo, dataset, now = world
    model = pinned_model(repo, dataset, now)
    reformat_json(model / name)  # the same values, other bytes

    typed = make_io(repo, stdin=Terminal("test classifier\n"), now=now)
    assert harness_main(run_args(dataset, "test", "classifier", "--model", str(model)), typed) == 1

    assert f"{name} is not committed in HEAD as on disk" in capsys.readouterr().err
    assert ledger_size(repo) == 1


def test_an_llm_test_run_needs_the_pinned_prompt_version(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    pinned_model(repo, dataset, now)
    replace_pin(repo, router_prompt_version="other-v0")

    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(live_llm_args(dataset, "test"), live) == 1

    assert "the router prompt is not the pinned version" in capsys.readouterr().err
    assert ledger_size(repo) == 1 and folders(repo.root, True, "-live-llm-test") == []


@pytest.mark.parametrize("state", ["not-committed", "partial", "dry"])
def test_an_llm_test_run_needs_a_committed_complete_dev_run(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str], state: str
) -> None:
    repo, dataset, now = world
    pinned_model(repo, dataset, now)
    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(live_llm_args(dataset, "dev"), live) == 0
    [llm_dev] = folders(repo.root, True, "-live-llm-dev")
    if state == "partial":
        rewrite_run(llm_dev, status="partial", stop="TEST-ONLY stop")
        repo.commit(llm_dev)
    elif state == "dry":  # a hand-edited folder: the harness never writes a dry -live- folder
        rewrite_run(llm_dev, dry_run=True, dry_reason="fake provider")
        repo.commit(llm_dev)
    # else: on disk, never committed.

    live = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    assert harness_main(live_llm_args(dataset, "test"), live) == 1

    assert "no complete live LLM dev run with this fingerprint" in capsys.readouterr().err
    assert ledger_size(repo) == 1 and folders(repo.root, True, "-live-llm-test") == []


# ── pin ──────────────────────────────────────────────────────────────────────────────────────


def test_a_model_whose_embed_folder_is_no_longer_as_committed_is_never_pinned(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = real_model(repo, dataset, now)  # model.json says real
    repo.commit(model)
    [embed] = folders(repo.root, True, "-live-embed")
    reformat_json(embed / "run.json")  # same cache sha, but no longer the file in HEAD

    pin = ["--dataset", str(dataset), "pin", str(model)]
    assert harness_main(pin, make_io(repo, stdin=Terminal("pin v0.1\n"), now=now)) == 1

    assert "is a dry model" in capsys.readouterr().err
    assert not (repo.root / LEDGER_PATH).exists()


# ── the hybrid on test ───────────────────────────────────────────────────────────────────────


def test_the_hybrid_on_test_replays_only_committed_results(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    runs = committed_test_runs(repo, dataset, now)
    args = hybrid_args(dataset, runs.classifier, runs.llm)
    results = runs.classifier / RESULTS_FILE
    committed = results.read_bytes()

    results.write_bytes(committed.replace(b'"latency_ms":', b'"latency_ms": ', 1))  # one byte
    assert harness_main(args, make_io(repo, stdin=Terminal("test hybrid\n"), now=now)) == 1
    err = capsys.readouterr().err
    assert f"{runs.classifier.name}/{RESULTS_FILE} is not committed in HEAD as on disk" in err

    results.write_bytes(committed)
    repo.dirty = True
    assert harness_main(args, make_io(repo, stdin=Terminal("test hybrid\n"), now=now)) == 1
    assert "a hybrid on test needs a clean working tree" in capsys.readouterr().err

    repo.dirty = False
    assert harness_main(args, make_io(repo, stdin=Terminal("test hybrid\n"), now=now)) == 0
    assert len(folders(repo.root, True, "-replay-hybrid-test")) == 1


def test_the_hybrid_on_test_needs_the_pinned_sigma(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    runs = committed_test_runs(repo, dataset, now)
    info = read_run(runs.classifier)
    fingerprint = info.fingerprint.model_copy(update={"sigma": 0.999})
    rewrite_run(runs.classifier, fingerprint=fingerprint, fingerprint_sha256=fingerprint.sha256())
    repo.commit(runs.classifier)

    typed = make_io(repo, stdin=Terminal("test hybrid\n"), now=now)
    assert harness_main(hybrid_args(dataset, runs.classifier, runs.llm), typed) == 1

    assert "not the pinned artifact, T, tau and sigma" in capsys.readouterr().err
    assert folders(repo.root, True, "-replay-hybrid-test") == []


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"status": "partial", "stop": "TEST-ONLY"}, "replays a complete classifier run"),
        ({"dry_run": True, "dry_reason": "fake embedder"}, "dry runs are refused on the test"),
    ],
    ids=["partial", "dry"],
)
def test_the_hybrid_on_test_refuses_a_hand_edited_classifier_folder(
    world: tuple[FakeRepo, Path, Now],
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, Any],
    refusal: str,
) -> None:
    # The harness never writes either folder; the refusals hold if one is edited by hand.
    repo, dataset, now = world
    runs = committed_test_runs(repo, dataset, now)
    rewrite_run(runs.classifier, **changes)
    repo.commit(runs.classifier)

    typed = make_io(repo, stdin=Terminal("test hybrid\n"), now=now)
    assert harness_main(hybrid_args(dataset, runs.classifier, runs.llm), typed) == 1

    assert refusal in capsys.readouterr().err
    assert folders(repo.root, True, "-replay-hybrid-test") == []


def test_the_hybrid_on_test_replays_only_ledgered_runs(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    runs = committed_test_runs(repo, dataset, now)
    copy = runs.classifier.parent / "20261109T235959Z-local-classifier-test"
    shutil.copytree(runs.classifier, copy)
    rewrite_run(copy, run_id=copy.name)
    repo.commit(copy)

    typed = make_io(repo, stdin=Terminal("test hybrid\n"), now=now)
    assert harness_main(hybrid_args(dataset, copy, runs.llm), typed) == 1

    assert f"{copy.name} is not a ledgered test run" in capsys.readouterr().err


# ── the hybrid on dev ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dry_input", ["llm", "classifier"])
def test_a_hybrid_with_one_dry_input_is_dry(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str], dry_input: str
) -> None:
    repo, dataset, now = world
    io = make_io(repo, now=now)
    if dry_input == "llm":
        model = real_model(repo, dataset, now)
        assert harness_main(run_args(dataset, "dev", "llm"), io) == 0
        [llm] = folders(repo.root, False, "-fake-llm-dev")
    else:
        model = dry_model(repo, dataset, now)
        assert harness_main(live_llm_args(dataset, "dev"), make_io(
            repo, stdin=Terminal("0.50\n"), now=now
        )) == 0  # fmt: skip
        [llm] = folders(repo.root, True, "-live-llm-dev")
    assert harness_main(run_args(dataset, "dev", "classifier", "--model", str(model)), io) == 0
    real = dry_input == "llm"
    [classifier] = folders(repo.root, real, "-classifier-dev")

    assert harness_main(hybrid_args(dataset, classifier, llm), io) == 0

    assert folders(repo.root, True, "-hybrid-dev") == []
    [hybrid] = folders(repo.root, False, "-fake-hybrid-dev")
    assert hybrid.is_relative_to(repo.root / DRY_INTENT_REPORTS)
    info = read_run(hybrid)
    assert info.dry_run and info.dry_reason
    assert (hybrid / "summary.md").read_text("utf-8").splitlines()[2].startswith(f"**{DRY_LABEL}**")


async def never_asks(
    scores: IntentScores,
    message: str,
    policy: RoutePolicy,
    fallback: IntentFallback,
    *,
    thresholds: RouterThresholds,
    business_id: str,
    trace_id: str,
) -> RouteDecision:
    """A stand-in router (not H1) that never asks the fallback: the top-1 decides."""
    top = max(DECISION_ORDER, key=lambda i: scores.probabilities[i])
    return RouteDecision(intent=top, route=policy.route(top), decided_by="classifier")


def test_a_hybrid_never_replays_an_llm_run_that_stopped_early(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    model = dry_model(repo, dataset, now)
    io = make_io(repo, now=now, router=never_asks)
    assert harness_main(run_args(dataset, "dev", "classifier", "--model", str(model)), io) == 0
    assert harness_main(run_args(dataset, "dev", "llm"), io) == 0
    [classifier] = folders(repo.root, False, "-fake-classifier-dev")
    [llm] = folders(repo.root, False, "-fake-llm-dev")
    lines = read_results(llm)
    stopped = lines[-1].model_copy(
        update={"decided_by": "not_run", "final_intent": None, "llm_intent": None, "route": None}
        | {"priority": None, "error": None, "cost_usd": None, "latency_ms": None}
    )
    text = "".join(x.model_dump_json() + "\n" for x in [*lines[:-1], stopped])
    (llm / RESULTS_FILE).write_text(text, "utf-8")

    assert harness_main(hybrid_args(dataset, classifier, llm), io) == 1

    assert "the LLM run stopped before every row" in capsys.readouterr().err
    assert folders(repo.root, False, "-fake-hybrid-dev") == []
