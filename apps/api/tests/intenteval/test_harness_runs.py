"""LLM mode's live path and the rules every run shares (M4 PR 4): which Settings a run accepts,
when the paid client is built, how a failure and a cap stop are written, and the exit codes.
Fakes only: "live" here builds the dry provider or a scripted one, never a paid client, and the
Settings' keys are test-only strings.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import TEST_SETTINGS_FIELDS
from tests.intenteval.harness_helpers import (
    PAID,
    FakeRepo,
    Now,
    Terminal,
    folders,
    frozen_copy,
    live_llm_args,
    make_io,
    settings,
    texts,
    written_text,
)
from vectapilot_api.config import Settings
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest, ProviderResponse
from vectapilot_api.intenteval import runs, steps
from vectapilot_api.intenteval.cli import harness_main
from vectapilot_api.intenteval.harness import Refused, check_settings
from vectapilot_api.intenteval.llmrun import DryRunRouterProvider
from vectapilot_api.intenteval.runinfo import Fingerprint, IntentRunInfo, read_results, read_run
from vectapilot_api.intenteval.runs import EXIT_FAILED, EXIT_OK, EXIT_PARTIAL, exit_code


@pytest.fixture
def world(tmp_path: Path) -> tuple[FakeRepo, Path, Now]:
    root = tmp_path.resolve()
    return FakeRepo(root), frozen_copy(root), Now()


def with_fields(**changes: Any) -> Settings:
    return Settings(**(TEST_SETTINGS_FIELDS | changes))


# ── check_settings ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fields", "live", "needs", "refusal"),
    [
        (PAID | {"environment": "production"}, True, "llm", "never runs with environment"),
        (PAID | {"environment": "production"}, False, "llm", "never runs with environment"),
        ({}, True, "llm", "--live needs the paid llm provider"),
        ({}, True, "embeddings", "--live needs the paid embeddings provider"),
        (PAID | {"embeddings_provider": "fake"}, True, "embeddings", "paid embeddings provider"),
        (PAID | {"embeddings_provider": "fake"}, False, "llm", "uses the fake providers only"),
        (PAID | {"llm_provider": "fake"}, False, "embeddings", "uses the fake providers only"),
    ],
    ids=[
        "production-live",
        "production-dry",
        "live-fake-llm",
        "live-fake-embeddings",
        "live-fake-embeddings-paid-llm",
        "dry-paid-llm",
        "dry-paid-embeddings",
    ],
)  # fmt: skip
def test_check_settings_refuses(
    fields: dict[str, Any], live: bool, needs: str, refusal: str
) -> None:
    with pytest.raises(Refused, match=refusal):
        check_settings(with_fields(**fields), live=live, needs=needs)


@pytest.mark.parametrize(
    ("live", "needs"), [(True, "llm"), (True, "embeddings"), (False, "llm"), (False, "embeddings")]
)
def test_check_settings_allows_paid_providers_live_and_fakes_dry(live: bool, needs: str) -> None:
    check_settings(settings(live), live=live, needs=needs)


def test_a_live_llm_run_on_fake_settings_is_refused_before_any_folder(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    io = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    io = replace(io, load_settings=lambda live: settings(False))  # fake providers, --live

    assert harness_main(live_llm_args(dataset, "dev"), io) == 1

    assert "--live needs the paid llm provider" in capsys.readouterr().err
    assert folders(repo.root, True, "-llm-dev") == []


# ── exit codes ───────────────────────────────────────────────────────────────────────────────


def run_info(status: str) -> IntentRunInfo:
    t0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    fingerprint = Fingerprint(freeze_sha256="f" * 64)
    zero = Decimal(0)
    return IntentRunInfo(
        kind="llm",
        run_id="t1-fake-llm-dev",
        status=status,
        dry_run=True,
        environment="local",
        dataset="intents",
        dataset_version="v0.1",
        dataset_stage="frozen",
        freeze_sha256="f" * 64,
        dataset_committed=True,
        split="dev",
        rows=1,
        commit="c" * 40,
        uncommitted_changes=False,
        started_at=t0,
        ended_at=t0,
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        confidence="19/20",
        bootstrap_seed=1,
        bootstrap_resamples=10,
        cost=CostInfo(
            cap_usd=None,
            embedding_usd=zero,
            llm_usd=zero,
            total_usd=zero,
            llm_cost_complete=True,
            embedding_requests=0,
            llm_calls=0,
        ),
    )


@pytest.mark.parametrize(
    ("status", "summarized", "code"),
    [
        ("complete", True, EXIT_OK),
        ("complete", False, EXIT_FAILED),
        ("failed", False, EXIT_FAILED),
        ("partial", True, EXIT_PARTIAL),
        ("partial", False, EXIT_PARTIAL),
    ],
)
def test_exit_codes(status: str, summarized: bool, code: int) -> None:
    assert (EXIT_OK, EXIT_FAILED, EXIT_PARTIAL) == (0, 1, 3)
    assert exit_code(run_info(status), summarized) == code


# ── LLM mode, live ───────────────────────────────────────────────────────────────────────────


def test_the_paid_provider_is_built_only_after_the_cap_is_typed(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    built: list[Provider] = []

    def build(s: Settings) -> Provider:
        built.append(DryRunRouterProvider())
        return built[-1]

    refused = replace(make_io(repo, stdin=Terminal("0.5\n"), now=now), build_provider=build)
    assert harness_main(live_llm_args(dataset, "dev"), refused) == 1
    assert "not confirmed" in capsys.readouterr().err
    assert built == [] and folders(repo.root, True, "-live-llm-dev") == []

    typed = replace(make_io(repo, stdin=Terminal("0.50\n"), now=now), build_provider=build)
    assert harness_main(live_llm_args(dataset, "dev"), typed) == 0
    [provider] = built
    assert isinstance(provider, DryRunRouterProvider) and provider.requests > 0


class Raising:
    """A provider whose every call raises a RuntimeError carrying a row's text (a bug)."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        raise RuntimeError(self.text)

    async def aclose(self) -> None:
        """Nothing to release."""


def test_a_failed_llm_run_names_the_error_class_only(
    world: tuple[FakeRepo, Path, Now], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset, now = world
    secret = texts(dataset)[-1]
    io = make_io(repo, stdin=Terminal("0.50\n"), now=now, provider=Raising(secret))

    assert harness_main(live_llm_args(dataset, "dev"), io) == 1

    [folder] = folders(repo.root, True, "-live-llm-dev")
    info = read_run(folder)
    assert (info.status, info.stop) == ("failed", "RuntimeError (message not shown)")
    assert not (folder / "summary.md").exists()
    out = capsys.readouterr()
    assert secret not in written_text(folder) + out.out + out.err


def test_the_typed_cap_is_the_runs_cap_and_stops_it(
    world: tuple[FakeRepo, Path, Now],
) -> None:
    repo, dataset, now = world
    io = make_io(repo, stdin=Terminal("0.000001\n"), now=now)
    argv = live_llm_args(dataset, "dev")
    argv[argv.index("0.50")] = "0.000001"

    assert harness_main(argv, io) == EXIT_PARTIAL

    [folder] = folders(repo.root, True, "-live-llm-dev")
    info = read_run(folder)
    assert (info.status, info.cost.cap_usd, info.cost.llm_calls) == (
        "partial",
        Decimal("0.000001"),
        0,
    )
    assert info.stop is not None and "would cross the cap" in info.stop
    lines = read_results(folder)
    assert lines and {line.decided_by for line in lines} == {"not_run"}  # never a hand-off


@pytest.mark.parametrize("command", ["embed", "llm"])
def test_a_live_run_needs_every_corpus_manifest_to_say_synthetic(
    world: tuple[FakeRepo, Path, Now],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    # The manifest parser refuses `synthetic: false` today, so the guard is reached only if that
    # is relaxed: here, by replacing the check's answer.
    repo, dataset, now = world
    monkeypatch.setattr(steps, "corpus_synthetic", lambda loaded: False)
    monkeypatch.setattr(runs, "corpus_synthetic", lambda loaded: False)
    io = make_io(repo, stdin=Terminal("0.50\n"), now=now)
    argv = (
        ["--dataset", str(dataset), "embed", "--live", "--max-usd", "0.50"]
        if command == "embed"
        else live_llm_args(dataset, "dev")
    )

    assert harness_main(argv, io) == 1

    assert "--live needs every manifest to say synthetic: true" in capsys.readouterr().err
    assert folders(repo.root, True, "-live-") == []
