"""PR 8: the measurement CLI (`llm_cli.py`), an I/O shell around the gateway and the summary.

No test reaches the network or uses a real key. The live path runs on a stand-in for the Anthropic
adapter (a scripted `FakeProvider` put where `build_provider` looks the adapter up), or once on the
real adapter with httpx2's `MockTransport` and the fake key "canary-key-7f3a". No test runs git on
its own behalf except the `read_git` and `read_head_file` tests (read-only commands). Runs write
into `tmp_path`; the killed-run test runs a dry run in a child process.
"""

import asyncio
import functools
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final

import httpx2
import pytest
from pydantic import SecretStr

from tests.conftest import make_settings
from vectapilot_api import llm_cli
from vectapilot_api.config import Settings
from vectapilot_api.gateway import gateway as gateway_module
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.errors import (
    AuthenticationFailed,
    BillingError,
    GatewayError,
    InvalidRequest,
    ModelRefused,
    SpendLimitReached,
)
from vectapilot_api.gateway.providers.anthropic import AnthropicProvider
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply, FakeStep
from vectapilot_api.gateway.types import CallOutcome, Usage
from vectapilot_api.llm_cli import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PARTIAL,
    GitError,
    GitInfo,
    Io,
    read_git,
    run,
)
from vectapilot_api.measure.budget import Ledger, reserve
from vectapilot_api.measure.dataset import load_cases
from vectapilot_api.measure.records import CallMade, parse_llm_call_lines
from vectapilot_api.measure.schemas import SCHEMAS

CANARY_KEY: Final = "canary-key-7f3a"  # fake on purpose, and not shaped like a real key
CANARY_COMPLETION: Final = "canary-completion-7f3a"
COMMIT: Final = "ab" * 20
NOW: Final = datetime(2026, 10, 15, 9, 30, 5, tzinfo=UTC)
STAMP: Final = "20261015T093005Z"
REAL_DATASET: Final = Path(__file__).parents[3] / "evals" / "datasets" / "gateway_smoke" / "v0.1"
INTENT_JSON: Final = '{"intent":"booking","language":"en","needs_human":false}'

TINY_CASES: Final[list[dict[str, Any]]] = [
    {
        "id": "t-small-plain",
        "business_id": "test-clinic",
        "alias": "small",
        "language": "en",
        "kind": "plain",
        "prompt": "When do you open on Mondays? prompt-text-small",
        "fake_reply": "[dry run] At nine.",
    },
    {
        "id": "t-large-plain",
        "business_id": "test-clinic",
        "alias": "large",
        "language": "en",
        "kind": "plain",
        "prompt": "What does a check-up cost? prompt-text-large",
        "fake_reply": "[dry run] See the fee list.",
    },
    {
        "id": "t-small-intent",
        "business_id": "test-clinic",
        "alias": "small",
        "language": "en",
        "kind": "structured",
        "schema_name": "IntentLabel",
        "prompt": "I would like to book a visit. prompt-text-intent",
        "fake_reply": INTENT_JSON,
    },
]
"""Three synthetic cases (a test-only business): small plain, large plain, small structured."""


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────


class Terminal(io.StringIO):
    """Typed input at a terminal."""

    def isatty(self) -> bool:
        return True


def live_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "environment": "local",
        "llm_provider": "anthropic",
        "anthropic_api_key": CANARY_KEY,
    }
    return make_settings(**(fields | overrides))


@dataclass
class World:
    """The CLI's outside world for one test: records what the CLI read from it."""

    root: Path
    settings: Settings = field(default_factory=make_settings)
    stdin: io.StringIO = field(default_factory=io.StringIO)
    commit: str = COMMIT
    stdout_tty: bool = True
    loads: list[bool] = field(default_factory=list)
    git_reads: int = 0
    load_settings: Callable[[bool], Settings] | None = None
    head: dict[PurePosixPath, bytes | None] = field(default_factory=dict)
    """HEAD's files, by path from the root; a file not listed is committed as it is on disk."""
    head_reads: list[PurePosixPath] = field(default_factory=list)

    def io(self) -> Io:
        def load(live: bool) -> Settings:
            self.loads.append(live)
            if self.load_settings is not None:
                return self.load_settings(live)
            return self.settings

        def git() -> GitInfo:
            self.git_reads += 1
            return GitInfo(root=self.root, commit=self.commit, uncommitted_changes=False)

        def head_file(root: Path, relative: PurePosixPath) -> bytes | None:
            assert root == self.root
            self.head_reads.append(relative)
            if relative in self.head:
                return self.head[relative]
            on_disk = self.root / relative
            return on_disk.read_bytes() if on_disk.is_file() else None

        return Io(
            load_settings=load,
            git=git,
            head_file=head_file,
            now=lambda: NOW,
            stdin=self.stdin,
            stdout_is_terminal=lambda: self.stdout_tty,
        )


def write_dataset(directory: Path, cases: list[dict[str, Any]] = TINY_CASES) -> Path:
    directory.mkdir(parents=True)
    manifest = {
        "dataset": "cli_test",
        "version": "v0.1",
        "created": "2026-10-15",
        "synthetic": True,
        "authored_by": "tests",
        "review": "done",
        "businesses": {"test-clinic": ["en"]},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = "".join(json.dumps(case) + "\n" for case in cases)
    (directory / "cases.jsonl").write_text(lines, encoding="utf-8")
    return directory


TINY_DIR: Final = PurePosixPath("evals", "datasets", "cli_test", "v0.1")
"""Where the tiny dataset lives in the (fake) repository: committed as it is on disk."""


@pytest.fixture
def tiny(world: World) -> Path:
    return write_dataset(world.root / TINY_DIR)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(root=tmp_path / "repo")


def dry_dir(world: World, dataset: str = "cli_test") -> Path:
    return world.root / "evals" / "reports" / "tmp" / dataset / "v0.1" / f"{STAMP}-fake"


def live_dir(world: World) -> Path:
    return world.root / "evals" / "reports" / "cli_test" / "v0.1" / f"{STAMP}-live"


def summary_of(directory: Path) -> dict[str, Any]:
    summary: dict[str, Any] = json.loads((directory / "summary.json").read_text("utf-8"))
    return summary


def forbid_adapter(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Make building the Anthropic adapter, by any path, a recorded test failure."""
    attempts: list[dict[str, Any]] = []

    def refuse(self: AnthropicProvider, **kwargs: Any) -> None:
        attempts.append(kwargs)
        raise AssertionError("the Anthropic adapter must not be built")

    monkeypatch.setattr(AnthropicProvider, "__init__", refuse)
    return attempts


@dataclass
class StandIn:
    """Stands in for the Anthropic adapter on the live path: how it was built, and the script."""

    provider: FakeProvider
    built: list[dict[str, Any]] = field(default_factory=list)


def stand_in(monkeypatch: pytest.MonkeyPatch, steps: list[FakeStep]) -> StandIn:
    fake = StandIn(FakeProvider(steps))

    def build(**kwargs: Any) -> FakeProvider:
        fake.built.append(kwargs)
        return fake.provider

    monkeypatch.setattr(gateway_module, "AnthropicProvider", build)
    return fake


def ok_steps(cases: list[dict[str, Any]] = TINY_CASES, **reply: Any) -> list[FakeStep]:
    """One valid answer per case, in order."""
    return [FakeReply(text=case["fake_reply"], **reply) for case in cases]


def tiny_reservations(settings: Settings) -> list[Decimal]:
    cases = load_cases("".join(json.dumps(case) + "\n" for case in TINY_CASES))
    return [
        reserve(
            case,
            None if case.schema_name is None else SCHEMAS[case.schema_name],
            catalog=load_catalog(),
            max_retries=settings.llm_max_retries,
            max_repairs=settings.llm_max_repairs,
        ).usd
        for case in cases
    ]


def live(world: World, tiny: Path, cap: str, *extra: str) -> int:
    """A live run whose cap is typed again at the (fake) terminal."""
    world.settings = live_settings() if world.settings.llm_provider == "fake" else world.settings
    world.stdin = Terminal(f"{cap}\n")
    return run(["--live", "--max-usd", cap, "--dataset", str(tiny), *extra], world.io())


# ── Dry runs ─────────────────────────────────────────────────────────────────────────────────


def test_a_dry_run_of_the_dataset_writes_a_summary(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["--dataset", str(REAL_DATASET)], world.io())

    out = capsys.readouterr().out
    run_dir = dry_dir(world, "gateway_smoke")
    summary = summary_of(run_dir)
    assert code == EXIT_OK
    assert world.loads == [False]  # dry-run settings
    assert (summary["calls_made"], summary["distinct_calls"], summary["partial"]) == (24, 24, False)
    assert summary["run"]["provider"] == "fake"
    assert summary["labels"][0].startswith("dry run")
    assert summary["feeds_note"] is False
    [small] = [a for a in summary["per_alias"] if a["alias"] == "small"]
    assert small["forced_repairs"][0]["result"] == "repair worked"
    # The count check passed: one record per call, both files written next to the summary.
    calls = (run_dir / "calls_made.jsonl").read_text("utf-8").splitlines()
    log = (run_dir / "llm_call_log.jsonl").read_text("utf-8").splitlines()
    records = parse_llm_call_lines(log)
    assert len(calls) == len(records) == 24
    assert [CallMade.model_validate_json(c).trace_id for c in calls] == [
        r.trace_id for r in records
    ]
    assert {r.operation for r in records} == {"gateway-measure"}
    # The plan, then the Markdown, then where it went.
    assert "Mode: DRY RUN" in out
    assert "Planned calls: 24 (24 distinct prompts, 0 repeats)" in out
    assert (run_dir / "summary.md").read_text("utf-8") in out
    assert out.rstrip().endswith("(a dry run: git ignores this directory).")


def test_dry_runs_go_where_git_ignores_them() -> None:
    root = Path(__file__).parents[3]
    ignore = (root / ".gitignore").read_text("utf-8").splitlines()

    assert f"{llm_cli.DRY_RUN_REPORTS_DIR.as_posix()}/" in ignore
    assert llm_cli.REPORTS_DIR.as_posix() + "/" not in ignore  # live summaries can be committed


def test_trace_ids_are_fresh_for_every_call(world: World, tiny: Path) -> None:
    assert run(["--dataset", str(tiny), "--repeats", "1"], world.io()) == EXIT_OK

    lines = (dry_dir(world) / "calls_made.jsonl").read_text("utf-8").splitlines()
    calls = [CallMade.model_validate_json(line) for line in lines]
    assert len({c.trace_id for c in calls}) == len(calls) == 6


def test_repeats_are_run_after_the_first_pass_and_reported_apart(
    world: World, tiny: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["--dataset", str(tiny), "--repeats", "1"], world.io())

    summary = summary_of(dry_dir(world))
    lines = (dry_dir(world) / "calls_made.jsonl").read_text("utf-8").splitlines()
    calls = [CallMade.model_validate_json(line) for line in lines]
    assert code == EXIT_OK
    assert [c.repeat for c in calls] == [0, 0, 0, 1, 1, 1]
    assert (summary["distinct_calls"], summary["repeat_calls"]) == (3, 3)
    assert sum(a["distinct_prompts"] for a in summary["per_alias"]) == 3
    assert "Planned calls: 6 (3 distinct prompts, 3 repeats)" in capsys.readouterr().out


def test_a_dry_run_stopped_by_max_calls_is_partial(
    world: World, tiny: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["--dataset", str(tiny), "--max-calls", "1"], world.io())

    summary = summary_of(dry_dir(world))
    assert code == EXIT_PARTIAL
    assert summary["partial"] is True
    assert summary["run"]["stopped"] == "max_calls"
    assert summary["run"]["stop_detail"] == "--max-calls 1 reached"
    assert summary["partial_reasons"] == ["stopped early: max_calls", "1/3 planned calls made"]
    assert "Run stopped (max_calls): --max-calls 1 reached" in capsys.readouterr().err
    markdown = (dry_dir(world) / "summary.md").read_text("utf-8")
    assert "| Stopped | max_calls: --max-calls 1 reached |" in markdown


def test_the_summary_names_the_limits_from_settings_and_the_catalog(
    world: World, tiny: Path
) -> None:
    # Values unlike the defaults, so they can only have come from these Settings.
    world.settings = make_settings(
        llm_max_retries=1,
        llm_max_repairs=0,
        llm_attempt_timeout_seconds=7.5,
        llm_deadline_seconds=20.0,
        llm_backoff_base_seconds=0.25,
        llm_backoff_cap_seconds=4.0,
    )
    caps = {name: alias.max_output_tokens for name, alias in load_catalog().aliases.items()}

    assert run(["--dataset", str(tiny)], world.io()) == EXIT_OK

    summary = summary_of(dry_dir(world))
    markdown = (dry_dir(world) / "summary.md").read_text("utf-8")
    assert summary["run"]["limits"] == {
        "max_retries": 1,
        "max_repairs": 0,
        "attempt_timeout_s": 7.5,
        "deadline_s": 20.0,
        "backoff_base_s": 0.25,
        "backoff_cap_s": 4.0,
        "max_output_tokens": caps,
    }
    cap_text = ", ".join(f"{alias} {cap}" for alias, cap in sorted(caps.items()))
    assert (
        "| Limits | retries 1 per request · repairs 0 per call · attempt timeout 7.5 s · deadline"
        f" 20 s · backoff 0.25 s up to 4 s · max_output_tokens: {cap_text} |"
    ) in markdown
    # Each alias's output tokens are read against its own cap.
    assert {a["alias"]: a["max_output_tokens"] for a in summary["per_alias"]} == caps
    assert f"of cap {caps['small']}" in markdown
    assert "not recorded" not in markdown


def test_a_dry_run_can_rehearse_the_cap(world: World, tiny: Path) -> None:
    # Without --live, --max-usd runs the same reservation rule against the fake's costs.
    code = run(["--dataset", str(tiny), "--max-usd", "0.0001"], world.io())

    summary = summary_of(dry_dir(world))
    assert code == EXIT_PARTIAL
    assert (summary["calls_made"], summary["run"]["stopped"]) == (0, "cost_cap")


# ── D9: never the Anthropic adapter without --live ───────────────────────────────────────────


def test_without_live_the_adapter_is_never_built_even_when_the_environment_says_anthropic(
    world: World,
    tiny: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = forbid_adapter(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("VECTAPILOT_ENVIRONMENT", "local")
    monkeypatch.setenv("VECTAPILOT_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("VECTAPILOT_ANTHROPIC_API_KEY", CANARY_KEY)
    monkeypatch.setenv("VECTAPILOT_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    monkeypatch.setenv("VECTAPILOT_REDIS_URL", "redis://127.0.0.1:1/0")
    world.load_settings = llm_cli.load_settings  # the real loader, reading that environment
    # The environment really does say anthropic, with a key:
    assert llm_cli.load_settings(True).llm_provider == "anthropic"

    code = run(["--dataset", str(tiny)], world.io())

    assert code == EXIT_OK
    assert attempts == []
    dry = llm_cli.load_settings(False)
    assert (dry.llm_provider, dry.anthropic_api_key) == ("fake", None)
    captured = capsys.readouterr()
    assert CANARY_KEY not in captured.out + captured.err


def test_without_live_the_adapter_is_never_built_even_by_a_loader_that_says_anthropic(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second layer: even if the dry run's settings said anthropic, it hands in its own
    # provider, so `build_provider` is never asked for one.
    attempts = forbid_adapter(monkeypatch)
    world.settings = live_settings()

    assert run(["--dataset", str(tiny)], world.io()) == EXIT_OK
    assert attempts == []


# ── --live: refused before anything is built ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--live"], "--live requires --max-usd"),
        (["--live", "--max-usd", "0"], "above 0 and at most 1.00"),
        (["--live", "--max-usd", "0.00"], "above 0 and at most 1.00"),
        (["--live", "--max-usd", "1.01"], "above 0 and at most 1.00"),
        (["--live", "--max-usd", "100"], "above 0 and at most 1.00"),
        (["--live", "--max-usd", "-0.5"], "plain decimal number"),
        (["--live", "--max-usd", "1e-2"], "plain decimal number"),
        (["--live", "--max-usd", "NaN"], "plain decimal number"),
        (["--live", "--max-usd", "Infinity"], "plain decimal number"),
        (["--live", "--max-usd", ".5"], "plain decimal number"),
        (["--max-usd", "1.01"], "above 0 and at most 1.00"),
        (["--max-calls", "0"], "--max-calls must be at least 1"),
        (["--repeats", "4"], "--repeats must be between 0 and 3"),
        (["--repeats", "-1"], "--repeats must be between 0 and 3"),
    ],
)
def test_invalid_arguments_are_refused_before_anything_is_read_or_built(
    argv: list[str],
    message: str,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = forbid_adapter(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        run(argv, world.io())

    assert caught.value.code == 2
    assert message in capsys.readouterr().err
    assert (world.git_reads, world.loads, attempts) == (0, [], [])


def test_live_needs_settings_that_say_anthropic(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts = forbid_adapter(monkeypatch)
    world.stdin = Terminal("0.50\n")

    code = run(["--live", "--max-usd", "0.50", "--dataset", str(tiny)], world.io())

    assert code == EXIT_FAILED
    assert world.loads == [True]
    assert "--live needs Settings to say llm_provider 'anthropic'" in capsys.readouterr().err
    assert world.stdin.read() == "0.50\n"  # never asked for the confirmation
    assert attempts == []


REFUSED_ANSWERS: Final = {
    "eof": "",
    "empty": "\n",
    "yes": "yes\n",
    "other-text": "0.5\n",  # the same amount, but not the text that was given
    "dollar-sign": "$0.50\n",
    "ctrl-c": "0.50\n",  # the right text, but Ctrl-C is pressed first
    "piped": "0.50\n",  # the right text, piped in: not a terminal
    "redirected-stdout": "0.50\n",  # the right text, typed from memory; the plan went to a file
}


def interrupt(size: int | None = -1, /) -> str:
    raise KeyboardInterrupt


@pytest.mark.parametrize("answer", list(REFUSED_ANSWERS))
def test_a_refused_confirmation_calls_nothing(
    answer: str,
    world: World,
    tiny: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = forbid_adapter(monkeypatch)
    world.settings = live_settings()
    text = REFUSED_ANSWERS[answer]
    world.stdin = io.StringIO(text) if answer == "piped" else Terminal(text)
    if answer == "ctrl-c":
        monkeypatch.setattr(world.stdin, "readline", interrupt)
    world.stdout_tty = answer != "redirected-stdout"

    code = run(["--live", "--max-usd", "0.50", "--dataset", str(tiny)], world.io())

    captured = capsys.readouterr()
    assert code == EXIT_FAILED
    assert "nothing was called" in captured.err
    if answer == "redirected-stdout":
        assert "stdout is not one (no redirect or pipe with --live)" in captured.err
        assert world.stdin.read() == "0.50\n"  # never read: refused before the prompt
    assert attempts == []
    assert not (world.root / "evals" / "reports").exists()  # no run directory either
    assert CANARY_KEY not in captured.out + captured.err


def test_the_plan_is_printed_before_the_confirmation(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forbid_adapter(monkeypatch)
    world.settings = live_settings()
    small_plain, large_plain, small_intent = tiny_reservations(world.settings)
    cap = format(small_plain + large_plain, "f")  # below the whole plan's worst case
    world.stdin = Terminal("no\n")

    run(["--live", "--max-usd", cap, "--dataset", str(tiny)], world.io())

    out = capsys.readouterr().out
    small = (small_plain + small_intent).normalize()
    whole = (small_plain + large_plain + small_intent).normalize()
    assert "Mode: LIVE. Real calls to the anthropic provider" in out
    assert "Planned calls: 3 (3 distinct prompts, 0 repeats)" in out
    assert f"  small: 2 calls, up to ${small:f}" in out
    assert f"  large: 1 calls, up to ${large_plain.normalize():f}" in out
    assert f"  whole plan: up to ${whole:f}" in out
    assert "The whole plan's worst case is above the cap" in out
    assert f"Cost cap for this run: ${Decimal(cap).normalize():f}" in out
    assert "Keep this terminal open until 'Written to' is printed" in out
    assert out.index("Planned calls") < out.index("Keep this terminal open")
    assert out.index("Keep this terminal open") < out.index("type the cap exactly as given")


# ── --live: the run (a stand-in for the adapter) ─────────────────────────────────────────────


def test_a_complete_live_run_builds_the_adapter_from_settings_and_can_feed_the_note(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = stand_in(monkeypatch, ok_steps())

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    markdown = (live_dir(world) / "summary.md").read_text("utf-8")
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert fake.built == [{"api_key": SecretStr(CANARY_KEY), "attempt_timeout_s": 15.0}]
    assert len(fake.provider.calls) == 3
    assert summary["run"]["provider"] == "anthropic"
    assert summary["run"]["max_usd"] == "0.50"
    limits = llm_cli.run_limits(world.settings, load_catalog())
    assert summary["run"]["limits"] == limits.model_dump(mode="json")
    assert summary["run"]["limits"]["attempt_timeout_s"] == 15.0  # the adapter's, above
    assert (summary["run"]["stopped"], summary["run"]["stop_detail"]) == (None, None)
    assert "| Stopped | no |" in markdown
    assert summary["labels"][0] == "synthetic prompts, single home network"
    assert (summary["partial"], summary["feeds_note"]) == (False, True)
    # The commit names the dataset: both files are, byte for byte, those at the commit.
    assert summary["run"]["dataset_committed"] is True
    assert world.head_reads == [TINY_DIR / "manifest.json", TINY_DIR / "cases.jsonl"]
    assert "(a complete live run: commit this directory on a branch and open a pull request)." in (
        captured.out
    )
    assert CANARY_KEY not in captured.out + captured.err


def test_the_cap_stops_the_run_before_a_call_whose_reservation_would_cross_it(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world.settings = live_settings()
    small_plain, large_plain, _ = tiny_reservations(world.settings)
    fake = stand_in(monkeypatch, ok_steps())
    # Call 1 (small) fits: 0 + its worst case <= cap. It costs the fake's $0.0000035, so call 2
    # (large) would need $0.0000035 + its whole worst case, which crosses a cap of exactly that
    # worst case.
    assert small_plain < large_plain

    code = live(world, tiny, format(large_plain, "f"))

    summary = summary_of(live_dir(world))
    assert code == EXIT_PARTIAL
    assert len(fake.provider.calls) == 1  # call 2 was never sent
    assert summary["calls_made"] == 1
    assert summary["run"]["stopped"] == "cost_cap"
    assert summary["partial"] is True
    assert summary["feeds_note"] is False
    captured = capsys.readouterr()
    assert "stopped before call 2 (t-large-plain)" in captured.err
    assert (
        "a live run that cannot feed the note (see summary.md): commit it as evidence only, or"
        " delete it before the next live run"
    ) in captured.out
    # The summary keeps the "Run stopped" line: a case id and dollar amounts, never a prompt.
    detail = summary["run"]["stop_detail"]
    assert detail == (
        f"stopped before call 2 (t-large-plain): its worst case ${large_plain.normalize():f} on top"
        f" of the $0.0000035 counted so far would cross the cap ${large_plain.normalize():f}"
    )
    assert f"Run stopped (cost_cap): {detail}" in captured.err
    markdown = (live_dir(world) / "summary.md").read_text("utf-8")
    assert f"| Stopped | cost_cap: {detail} |" in markdown
    assert not any(case["prompt"] in markdown for case in TINY_CASES)


def test_a_cap_below_the_first_reservation_calls_nothing(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = stand_in(monkeypatch, ok_steps())

    code = live(world, tiny, "0.0001")

    summary = summary_of(live_dir(world))
    assert code == EXIT_PARTIAL
    assert fake.provider.calls == []
    assert (summary["calls_made"], summary["run"]["stopped"]) == (0, "cost_cap")
    assert summary["partial_reasons"] == ["stopped early: cost_cap", "0/3 planned calls made"]


@pytest.mark.parametrize(("geo", "calls"), [("global", 3), (None, 1)])
def test_a_cost_incomplete_call_keeps_its_reservation(
    geo: str | None, calls: int, world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap = both plain calls' worst cases, less a millionth of a dollar. When call 1 is cost-
    # complete it counts $0.0000035 and call 2 fits; when its geo is unknown (None), its cost is
    # incomplete: it counts its whole reservation (measure/budget.py, Assumptions), and call 2 no
    # longer fits. A `us` geo keeps its reservation too, but stops the run itself (below).
    world.settings = live_settings()
    small_plain, large_plain, _ = tiny_reservations(world.settings)
    cap = format(small_plain + large_plain - Decimal("0.000001"), "f")
    fake = stand_in(monkeypatch, [FakeReply(text="first", inference_geo=geo), *ok_steps()[1:]])

    code = live(world, tiny, cap)

    summary = summary_of(live_dir(world))
    assert len(fake.provider.calls) == summary["calls_made"] == calls
    if geo != "global":
        assert code == EXIT_PARTIAL
        assert summary["run"]["stopped"] == "cost_cap"
        assert summary["run_cost"]["is_lower_bound"] is True
        # Partial because the cap stopped it; the cost-incomplete call is counted, not a reason.
        assert summary["partial_reasons"] == ["stopped early: cost_cap", "1/3 planned calls made"]
        assert summary["run_cost"]["cost_incomplete"] == {"k": 1, "n": 1}
    else:
        assert code == EXIT_OK
        assert summary["run_cost"]["cost_incomplete"] == {"k": 0, "n": 3}


def test_a_call_with_no_geo_neither_stops_the_run_nor_makes_it_partial(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Decided 2026-10-15 (shesha): a missing geo keeps today's behaviour (the call is
    # cost-incomplete, the run carries on), and partial means fewer calls made than planned.
    fake = stand_in(monkeypatch, [FakeReply(text="first", inference_geo=None), *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    markdown = (live_dir(world) / "summary.md").read_text("utf-8")
    assert code == EXIT_OK
    assert len(fake.provider.calls) == 3
    assert (summary["run"]["stopped"], summary["run"]["stop_detail"]) == (None, None)
    assert "Run stopped" not in capsys.readouterr().err
    assert (summary["calls_made"], summary["partial"], summary["partial_reasons"]) == (3, False, [])
    assert summary["run_cost"]["cost_incomplete"] == {"k": 1, "n": 3}
    assert summary["run_cost"]["is_lower_bound"] is True
    assert summary["feeds_note"] is True
    assert "(lower bound: 1/3 cost-incomplete)" in markdown


US_GEO_DETAIL: Final = (
    ": inference_geo us, the workspace default geo must be global (the call keeps its reservation)"
)


def test_a_us_geo_stops_the_run_after_that_call(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Decided 2026-10-15 (shesha): a `us` geo is a setup error (the workspace default must be
    # global), so the run stops after the call that reported it; the later cases are never sent.
    world.settings = live_settings()
    small_plain, _, _ = tiny_reservations(world.settings)
    fake = stand_in(monkeypatch, [FakeReply(text="first", inference_geo="us"), *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    markdown = (live_dir(world) / "summary.md").read_text("utf-8")
    captured = capsys.readouterr()
    assert code == EXIT_PARTIAL
    assert len(fake.provider.calls) == summary["calls_made"] == 1
    assert summary["run"]["stopped"] == "error"
    assert summary["run"]["stop_detail"] == "t-small-plain" + US_GEO_DETAIL
    assert f"Run stopped (error): t-small-plain{US_GEO_DETAIL}" in captured.err
    assert summary["partial_reasons"] == ["stopped early: error", "1/3 planned calls made"]
    assert (summary["partial"], summary["feeds_note"]) == (True, False)
    assert summary["run_cost"]["cost_incomplete"] == {"k": 1, "n": 1}
    # The call kept its whole reservation against the cap (its cost is only a lower bound).
    reserved = f"${small_plain.normalize():f}"
    assert f"reserved {reserved}, counted {reserved}" in captured.out
    assert "a live run that cannot feed the note" in captured.out
    assert not any(case["prompt"] in markdown for case in TINY_CASES)


def test_a_us_geo_on_the_last_planned_call_keeps_a_complete_run_out_of_the_note(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every planned call was made, so not partial (exit 0), but a stop ended the run, so it never
    # feeds the note (decided by shesha, 2026-10-15).
    last = FakeReply(text=TINY_CASES[2]["fake_reply"], inference_geo="us")
    fake = stand_in(monkeypatch, [*ok_steps()[:2], last])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    assert code == EXIT_OK
    assert len(fake.provider.calls) == summary["calls_made"] == 3
    assert (summary["partial"], summary["feeds_note"]) == (False, False)
    assert summary["run"]["stop_detail"] == "t-small-intent" + US_GEO_DETAIL
    assert "stopped by its last planned call: error" in summary["not_for_note"]


# ── --live: a dataset the commit does not name ──────────────────────────────────────────────


def _outside(world: World, tmp_path: Path) -> Path:
    return write_dataset(tmp_path / "elsewhere")


def _linked_from_inside(world: World, tmp_path: Path) -> Path:
    link = world.root / TINY_DIR
    link.parent.mkdir(parents=True)
    link.symlink_to(write_dataset(tmp_path / "elsewhere"), target_is_directory=True)
    return link


def _not_in_the_commit(world: World, tmp_path: Path) -> Path:
    world.head[TINY_DIR / "cases.jsonl"] = None  # untracked or git-ignored: not in the commit
    return write_dataset(world.root / TINY_DIR)


def _changed(world: World, tmp_path: Path) -> Path:
    world.head[TINY_DIR / "cases.jsonl"] = b"{}\n"  # the commit holds other cases
    return write_dataset(world.root / TINY_DIR)


def _changed_under_the_run_directories(world: World, tmp_path: Path) -> Path:
    under = PurePosixPath("evals", "reports", "cli_test_data", "v0.1")  # git status skips it
    world.head[under / "manifest.json"] = b"{}"
    return write_dataset(world.root / under)


@pytest.mark.parametrize(
    ("place", "asks_git"),
    [
        (_outside, False),
        (_linked_from_inside, False),
        (_not_in_the_commit, True),
        (_changed, True),
        (_changed_under_the_run_directories, True),
    ],
)
def test_a_live_run_on_a_dataset_the_commit_does_not_name_cannot_feed_the_note(
    place: Callable[[World, Path], Path],
    asks_git: bool,
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # In every case git status reports a clean tree (`World` says so), as it would: it cannot see
    # a dataset outside the repository, a git-ignored one, or one under evals/reports/.
    dataset = place(world, tmp_path)
    stand_in(monkeypatch, ok_steps())

    code = live(world, dataset, "0.50")

    summary = summary_of(live_dir(world))
    assert code == EXIT_OK  # it made every call: complete, not partial
    assert (summary["partial"], summary["feeds_note"]) == (False, False)
    assert (summary["run"]["uncommitted_changes"], summary["run"]["dataset_committed"]) == (
        False,
        False,
    )
    assert summary["not_for_note"] == [
        "dataset not as committed: the commit does not name the prompts that ran"
    ]
    assert "files as committed: no" in (live_dir(world) / "summary.md").read_text("utf-8")
    assert "a live run that cannot feed the note" in capsys.readouterr().out
    # A file outside the repository (a link resolved) is never looked up in the commit.
    assert bool(world.head_reads) is asks_git


def test_measured_outcomes_do_not_stop_the_run(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refusal is a result to report, not a reason to stop.
    fake = stand_in(monkeypatch, [ModelRefused(), *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    [small] = [a for a in summary["per_alias"] if a["alias"] == "small"]
    assert code == EXIT_OK
    assert len(fake.provider.calls) == 3
    assert small["refusals"] == {"k": 1, "n": 2}


def test_an_account_failure_stops_the_run(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = stand_in(monkeypatch, [AuthenticationFailed(status_code=401), *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    assert code == EXIT_PARTIAL
    assert len(fake.provider.calls) == 1
    assert summary["run"]["stopped"] == "error"
    assert "outcome auth_failed, no further call can succeed" in capsys.readouterr().err
    assert summary["run"]["stop_detail"] == (
        "t-small-plain: outcome auth_failed, no further call can succeed"
    )


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (SpendLimitReached(status_code=400), "outcome spend_limit, no further call can succeed"),
        (BillingError(status_code=402), "outcome billing, no further call can succeed"),
        (AuthenticationFailed(status_code=401), "outcome auth_failed, no further call can succeed"),
        # Decided 2026-10-15 (shesha): our own request is wrong, so the run stops.
        (InvalidRequest(status_code=400), "outcome invalid_request, the request was rejected as"),
    ],
    ids=["spend_limit", "billing", "auth_failed", "invalid_request"],
)
def test_a_stopping_outcome_stops_the_run_after_that_call(
    error: GatewayError,
    detail: str,
    world: World,
    tiny: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = stand_in(monkeypatch, [error, *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    markdown = (live_dir(world) / "summary.md").read_text("utf-8")
    captured = capsys.readouterr()
    assert code == EXIT_PARTIAL
    assert len(fake.provider.calls) == summary["calls_made"] == 1  # the later cases: never sent
    assert summary["per_alias"][0]["outcomes"] == {error.outcome.value: 1}
    assert summary["run"]["stopped"] == "error"
    assert summary["run"]["stop_detail"].startswith(f"t-small-plain: {detail}")
    assert f"Run stopped (error): t-small-plain: {detail}" in captured.err
    assert (summary["partial"], summary["feeds_note"]) == (True, False)
    assert "a live run that cannot feed the note" in captured.out
    assert not any(case["prompt"] in markdown for case in TINY_CASES)


def test_a_run_stopped_by_its_last_planned_call_is_complete_but_not_for_the_note(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Every planned call was made, so not partial (exit 0); the stop still keeps it out of the
    # note (decided by shesha, 2026-10-15).
    stand_in(monkeypatch, [*ok_steps()[:2], AuthenticationFailed(status_code=401)])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert (summary["calls_made"], summary["partial"]) == (3, False)
    assert summary["run"]["stopped"] == "error"
    assert summary["run"]["stop_detail"] == (
        "t-small-intent: outcome auth_failed, no further call can succeed"
    )
    assert summary["feeds_note"] is False
    assert "stopped by its last planned call: error" in summary["not_for_note"]
    assert "Run stopped (error)" in captured.err
    assert "a live run that cannot feed the note" in captured.out


def test_an_unexpected_error_stops_the_run_and_shows_only_its_type(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in(monkeypatch, [])  # an empty script: the fake raises AssertionError (a bug)

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    err = capsys.readouterr().err
    assert code == EXIT_PARTIAL
    assert summary["run"]["stopped"] == "error"
    assert summary["per_alias"][0]["outcomes"] == {"internal_error": 1}
    assert "unexpected AssertionError (internal_error)" in err
    assert "no scripted step" not in err  # the exception's own text is never shown
    assert summary["run"]["stop_detail"] == (
        "t-small-plain: unexpected AssertionError (internal_error)"
    )


class RaisesAnOddlyNamedError:
    """A provider bug whose exception type has a name that no stop detail may hold."""

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        raise type("Odd|Name\nsecond line", (RuntimeError,), {})()

    async def aclose(self) -> None:
        return None


def test_an_exception_type_name_that_is_not_an_identifier_is_not_shown(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        gateway_module, "AnthropicProvider", lambda **kwargs: RaisesAnOddlyNamedError()
    )

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))  # the odd name did not cost the run its summary
    assert code == EXIT_PARTIAL
    assert summary["run"]["stop_detail"] == (
        "t-small-plain: unexpected exception (type name not shown) (internal_error)"
    )
    assert "second line" not in capsys.readouterr().err


def test_a_call_billed_beyond_its_bound_stops_the_run(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    huge = Usage(input_tokens=5_000_000, output_tokens=5)
    stand_in(monkeypatch, [FakeReply(text="x", usage=huge), *ok_steps()[1:]])

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    assert code == EXIT_PARTIAL
    assert (summary["calls_made"], summary["run"]["stopped"]) == (1, "error")
    assert "billed beyond its worst-case bound" in capsys.readouterr().err
    assert summary["run"]["stop_detail"].startswith(
        "t-small-plain: billed beyond its worst-case bound"
    )


class Interrupted:
    """A provider whose attempt is cancelled, as Ctrl-C cancels the CLI's task."""

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        return None


def test_an_interrupt_stops_the_run_and_keeps_what_was_made(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "AnthropicProvider", lambda **kwargs: Interrupted())

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    assert code == EXIT_PARTIAL
    assert summary["run"]["stopped"] == "interrupted"
    assert summary["run"]["stop_detail"] == "interrupted during t-small-plain"
    assert summary["per_alias"][0]["outcomes"] == {"cancelled": 1}
    assert summary["run_cost"]["is_lower_bound"] is True  # a cancelled attempt may be billed


@dataclass
class CancelsItsTask:
    """A provider that cancels the running task, as Ctrl-C does through `asyncio.run`, then waits:
    the cancellation arrives at that await with the task's cancel count really raised."""

    sends: int = 0
    cancelling_at_close: int | None = None

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        self.sends += 1
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(3600)
        raise AssertionError("the cancellation never arrived")

    async def aclose(self) -> None:
        # Called by the gateway on exit, in the same task, after the CLI handled the interrupt.
        task = asyncio.current_task()
        self.cancelling_at_close = None if task is None else task.cancelling()


def test_a_real_cancel_stops_the_run_after_that_call_and_writes_everything(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = CancelsItsTask()
    monkeypatch.setattr(gateway_module, "AnthropicProvider", lambda **kwargs: provider)

    code = live(world, tiny, "0.50")

    summary = summary_of(live_dir(world))
    calls = (live_dir(world) / "calls_made.jsonl").read_text("utf-8").splitlines()
    log = (live_dir(world) / "llm_call_log.jsonl").read_text("utf-8").splitlines()
    assert code == EXIT_PARTIAL
    assert provider.sends == 1  # no further call after the interrupt
    assert provider.cancelling_at_close == 0  # `uncancel` took back the real cancel request
    assert (summary["calls_made"], summary["run"]["stopped"]) == (1, "interrupted")
    assert len(calls) == len(parse_llm_call_lines(log)) == 1
    assert (live_dir(world) / "summary.md").is_file()


def test_run_calls_refuses_a_live_run_without_a_cap(
    tiny: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `parse_args` already refuses `--live` without `--max-usd`; this is the second layer, for any
    # other caller of `run_calls`.
    attempts = forbid_adapter(monkeypatch)
    settings = live_settings()
    cases = load_cases((tiny / "cases.jsonl").read_text("utf-8"))
    plan = llm_cli.plan_calls(cases, repeats=0, catalog=load_catalog(), settings=settings)
    done = llm_cli.Execution(sink=io.StringIO())

    with pytest.raises(ValueError, match="a live run needs a cap"):
        asyncio.run(
            llm_cli.run_calls(
                settings,
                plan,
                live=True,
                capture=llm_cli.LogCapture(io.StringIO()),
                ledger=Ledger(cap=None),
                max_calls=None,
                done=done,
            )
        )

    assert (attempts, done.calls_made) == ([], [])


# ── A killed run: what was called stays on disk ──────────────────────────────────────────────

KILLED_RUN: Final = """
import asyncio, signal, sys
from datetime import UTC, datetime
from pathlib import Path
from vectapilot_api import llm_cli

signum, marker, root, dataset = int(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
signal.signal(signum, signal.SIG_DFL)  # as at an ordinary terminal, even if the parent ignores it
sends = 0


class HangsOnTheSecondSend(llm_cli.DryRunProvider):
    async def send(self, request):
        global sends
        sends += 1
        if sends == 2:
            marker.write_text("call 2 was sent", encoding="utf-8")
            await asyncio.sleep(3600)
        return await super().send(request)


llm_cli.DryRunProvider = HangsOnTheSecondSend
io = llm_cli.Io(
    load_settings=llm_cli.load_settings,
    git=lambda: llm_cli.GitInfo(root=root, commit="ab" * 20, uncommitted_changes=False),
    head_file=lambda root, relative: None,
    now=lambda: datetime(2026, 10, 15, 9, 30, 5, tzinfo=UTC),
    stdin=sys.stdin,
    stdout_is_terminal=lambda: False,
)
sys.exit(llm_cli.run(["--dataset", dataset], io))
"""
"""A dry run of the tiny dataset in a child process whose second call never returns."""


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="needs POSIX signals")
@pytest.mark.parametrize("signame", ["SIGHUP", "SIGTERM"])  # a closed terminal; `kill`
def test_a_killed_run_still_lists_every_call_it_made(
    signame: str, world: World, tiny: Path, tmp_path: Path
) -> None:
    signum = int(getattr(signal, signame))
    marker = tmp_path / "call-2-sent"
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("VECTAPILOT_", "ANTHROPIC_"))
    } | {
        "VECTAPILOT_ENVIRONMENT": "local",
        "VECTAPILOT_DATABASE_URL": "postgresql+asyncpg://u:p@127.0.0.1:1/db",
        "VECTAPILOT_REDIS_URL": "redis://127.0.0.1:1/0",
    }
    argv = [sys.executable, "-I", "-c", KILLED_RUN, str(signum), str(marker), str(world.root)]
    # Our own interpreter and arguments, no shell (S603); cwd has no .env.
    child = subprocess.Popen(  # noqa: S603
        [*argv, str(tiny)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        give_up = time.monotonic() + 60
        while not marker.exists():
            assert child.poll() is None, f"the child ended early: {child.communicate()[1]}"
            assert time.monotonic() < give_up, "call 2 was never sent"
            time.sleep(0.02)
        child.send_signal(signum)
        child.wait(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if child.stderr is not None:
            child.stderr.close()

    run_dir = dry_dir(world)
    calls = [
        CallMade.model_validate_json(line)
        for line in (run_dir / "calls_made.jsonl").read_text("utf-8").splitlines()
    ]
    records = parse_llm_call_lines((run_dir / "llm_call_log.jsonl").read_text("utf-8").splitlines())
    assert child.returncode == -signum  # ended by the signal: no `finally` of the CLI ran
    # Call 1 finished and wrote its record; call 2 was in flight, and may have been billed.
    assert [c.case_id for c in calls] == ["t-small-plain", "t-large-plain"]
    assert [r.trace_id for r in records] == [calls[0].trace_id]
    assert not (run_dir / "summary.json").exists()


# ── The count check ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("copies", "read"), [(0, 0), (2, 2)])
def test_the_run_fails_unless_it_read_exactly_one_record_per_call(
    copies: int,
    read: int,
    world: World,
    tiny: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The gateway's one `llm_call` line goes missing (0 copies) or is written twice (2).
    original = gateway_module.logger.info

    def emit(msg: object, *args: Any, **kwargs: Any) -> None:
        for _ in range(copies):
            original(msg, *args, **kwargs)

    monkeypatch.setattr(gateway_module.logger, "info", emit)

    code = run(["--dataset", str(tiny)], world.io())

    err = capsys.readouterr().err
    run_dir = dry_dir(world)
    assert code == EXIT_FAILED
    assert "t-small-plain: no single llm_call record" in err  # the run stopped at once
    assert (
        f"the run FAILED, no summary written: expected exactly 1 llm_call records (one per call"
        f" made), read {read}: the run fails"
    ) in err
    assert not (run_dir / "summary.json").exists()
    assert not (run_dir / "summary.md").exists()
    # What was called stays on disk, for inspection.
    assert len((run_dir / "calls_made.jsonl").read_text("utf-8").splitlines()) == 1
    log = (run_dir / "llm_call_log.jsonl").read_text("utf-8").splitlines()
    assert len(parse_llm_call_lines(log)) == read


def test_a_live_run_that_fails_the_count_check_says_to_keep_or_delete_it(
    world: World, tiny: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stand_in(monkeypatch, ok_steps())
    monkeypatch.setattr(gateway_module.logger, "info", lambda *args, **kwargs: None)

    code = live(world, tiny, "0.50")

    assert code == EXIT_FAILED
    assert (
        f"Its inputs are in {live_dir(world)}; commit it as evidence only, or delete it before the"
        " next live run"
    ) in capsys.readouterr().err


# ── Nothing key-shaped anywhere: the real adapter on MockTransport ───────────────────────────


def test_a_live_run_through_the_real_adapter_shows_no_key_and_no_text(
    world: World,
    tiny: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG")
    sent: list[httpx2.Request] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        body = json.loads(request.content)
        text = INTENT_JSON if "system" in body else CANARY_COMPLETION
        message = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": text, "citations": None}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 30,
                "output_tokens": 20,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "inference_geo": "global",
            },
        }
        return httpx2.Response(200, json=message, headers={"request-id": "req_011CliTest"})

    adapter = functools.partial(
        AnthropicProvider,
        base_url="https://anthropic.mock.invalid",
        transport=httpx2.MockTransport(answer),
    )
    monkeypatch.setattr(gateway_module, "AnthropicProvider", adapter)

    code = live(world, tiny, "0.50")

    captured = capsys.readouterr()
    files = [path.read_text("utf-8") for path in sorted(live_dir(world).iterdir())]
    outputs = [captured.out, captured.err, caplog.text, *files]
    assert code == EXIT_OK
    assert len(files) == 4
    assert [r.headers["x-api-key"] for r in sent] == [CANARY_KEY] * 3  # only on the wire
    secret_like = ("sk-ant-", "x-api-key", "Authorization")
    for text in outputs:
        assert CANARY_KEY not in text
        assert CANARY_COMPLETION not in text  # answers are never printed or written
        assert not any(case["prompt"] in text for case in TINY_CASES)  # nor prompts
        assert not any(marker in text for marker in secret_like)
    summary = summary_of(live_dir(world))
    assert summary["run_cost"]["cost_incomplete"] == {"k": 0, "n": 3}
    assert {a["outcomes"]["ok"] for a in summary["per_alias"]} == {1, 2}


# ── Failures before any call ─────────────────────────────────────────────────────────────────


def test_a_missing_dataset_fails_before_any_call(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["--dataset", str(tmp_path / "nowhere")], world.io())

    assert code == EXIT_FAILED
    assert "nothing was called" in capsys.readouterr().err
    assert world.loads == []


def test_an_inconsistent_dataset_fails_before_any_call(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unknown_alias = [TINY_CASES[0] | {"alias": "huge"}]
    dataset = write_dataset(tmp_path / "bad", unknown_alias)

    code = run(["--dataset", str(dataset)], world.io())

    assert code == EXIT_FAILED
    assert "case t-small-plain: its alias is not in the catalog" in capsys.readouterr().err


def test_two_cases_with_the_same_prompt_on_one_alias_fail_before_any_call(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # They would count twice toward n distinct prompts: refused before Settings, before any plan.
    attempts = forbid_adapter(monkeypatch)
    copy = TINY_CASES[0] | {"id": "t-small-copy", "fake_reply": "[dry run] Also at nine."}
    dataset = write_dataset(tmp_path / "duplicate", [*TINY_CASES, copy])

    code = run(["--dataset", str(dataset)], world.io())

    captured = capsys.readouterr()
    assert code == EXIT_FAILED
    assert (
        "cases t-small-plain and t-small-copy send the same prompt on alias small" in captured.err
    )
    assert "nothing was called" in captured.err
    assert TINY_CASES[0]["prompt"] not in captured.out + captured.err  # the ids, never the prompt
    assert captured.out == ""  # no plan either
    assert (world.loads, attempts) == ([], [])
    assert not (world.root / "evals").exists()  # no run directory


def test_a_dataset_that_is_not_utf8_fails_before_any_call(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = write_dataset(tmp_path / "latin1")
    (dataset / "cases.jsonl").write_bytes(b"\xff\n")

    code = run(["--dataset", str(dataset)], world.io())

    assert code == EXIT_FAILED
    assert "cases.jsonl is not UTF-8 text; nothing was called" in capsys.readouterr().err
    assert world.loads == []


def test_git_problems_fail_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_git() -> GitInfo:
        raise GitError("git was not found on PATH: run inside the repository")

    world = World(root=tmp_path)
    broken = replace(world.io(), git=no_git)

    assert run([], broken) == EXIT_FAILED
    assert "git was not found on PATH" in capsys.readouterr().err


def test_invalid_settings_fail_before_any_call(
    world: World, tiny: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_key(live: bool) -> Settings:
        return live_settings(anthropic_api_key=None)

    world.load_settings = no_key

    code = run(["--dataset", str(tiny)], world.io())

    err = capsys.readouterr().err
    assert code == EXIT_FAILED
    assert "invalid settings; nothing was called" in err
    assert "anthropic_api_key (VECTAPILOT_ANTHROPIC_API_KEY) is required" in err


def test_a_commit_that_is_not_a_sha_fails_before_any_call(
    world: World, tiny: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world.commit = "main"

    assert run(["--dataset", str(tiny)], world.io()) == EXIT_FAILED
    assert "cannot describe the run; nothing was called" in capsys.readouterr().err


def test_a_run_never_overwrites_another(
    world: World, tiny: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["--dataset", str(tiny)], world.io()) == EXIT_OK
    capsys.readouterr()

    code = run(["--dataset", str(tiny)], world.io())  # the same start time: the same directory

    assert code == EXIT_FAILED
    assert "cannot create the run directory" in capsys.readouterr().err


# ── Git, the entry point and the defaults ────────────────────────────────────────────────────


STATUS: Final = ":(top,exclude)evals/reports"
"""The last argument of the `git status` that `read_git` runs."""


def test_read_git_reads_the_root_the_commit_and_the_tree_state() -> None:
    asked: list[list[str]] = []
    answers = {"--show-toplevel": "/repo\n", "HEAD": f"{COMMIT}\n", STATUS: " M a.py\n"}

    def git(args: Any) -> str:
        asked.append(list(args))
        return answers[args[-1]]

    info = read_git(git)

    assert info == GitInfo(root=Path("/repo"), commit=COMMIT, uncommitted_changes=True)
    assert asked == [
        ["rev-parse", "--show-toplevel"],
        ["rev-parse", "HEAD"],
        # Never refreshes the index file; the whole tree from its top, except the run directories.
        ["--no-optional-locks", "status", "--porcelain", "--", ":(top)", STATUS],
    ]
    assert read_git(lambda args: "" if args[-1] == STATUS else answers[args[-1]]) == (
        GitInfo(root=Path("/repo"), commit=COMMIT, uncommitted_changes=False)
    )


def test_the_run_directories_are_left_out_of_the_tree_check() -> None:
    # Every live run leaves a directory under evals/reports/: it must not taint the next run.
    assert llm_cli.STATUS_ARGS[-1] == f":(top,exclude){llm_cli.REPORTS_DIR.as_posix()}"
    assert llm_cli.DRY_RUN_REPORTS_DIR.is_relative_to(llm_cli.REPORTS_DIR)


def _inside_a_git_checkout() -> bool:
    executable = shutil.which("git")
    if executable is None:
        return False
    # Read-only, fixed arguments, no shell (S603: the arguments are ours).
    done = subprocess.run(  # noqa: S603
        [executable, "rev-parse", "--is-inside-work-tree"], capture_output=True, check=False
    )
    return done.returncode == 0


@pytest.mark.skipif(not _inside_a_git_checkout(), reason="needs git and a git checkout")
def test_read_git_runs_read_only_git_commands() -> None:
    info = read_git()

    assert len(info.commit) == 40
    assert (info.root / "apps" / "api" / "pyproject.toml").is_file()
    with pytest.raises(GitError, match="failed"):
        llm_cli._git(["rev-parse", "--verify", "--quiet", "no-such-ref-7f3a"])


@pytest.mark.skipif(not _inside_a_git_checkout(), reason="needs git and a git checkout")
def test_read_head_file_reads_what_the_commit_holds_and_nothing_else() -> None:
    root = read_git().root

    pyproject = llm_cli.read_head_file(root, PurePosixPath("apps/api/pyproject.toml"))

    assert pyproject is not None
    assert b'name = "vectapilot-api"' in pyproject
    assert llm_cli.read_head_file(root, PurePosixPath("no-such-file-7f3a")) is None


def test_git_missing_from_path_is_a_git_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(GitError, match="not found on PATH"):
        read_git()
    with pytest.raises(GitError, match="not found on PATH"):
        llm_cli.read_head_file(Path("/repo"), PurePosixPath("a"))


def test_the_default_io_is_the_real_world() -> None:
    real = llm_cli.default_io()

    assert real.load_settings is llm_cli.load_settings
    assert real.git is read_git
    assert real.head_file is llm_cli.read_head_file
    assert real.now().tzinfo is UTC
    assert real.stdout_is_terminal is llm_cli.stdout_is_terminal
    assert llm_cli.stdout_is_terminal() is sys.stdout.isatty()


def test_main_prints_the_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        llm_cli.main(["--help"])

    out = capsys.readouterr().out
    assert caught.value.code == 0
    assert out.startswith("usage: vectapilot-llm [-h] [--live] [--max-usd X]")
    assert "evals/reports/tmp/<dataset>/<version>/<UTC start>-fake/" in out
    assert "cost cap per run" in out
    assert "The cap is per run." in out
    assert "the only cap across runs" in out
    assert 'Checklist: docs/plans/m1-pr-checks.md, PR 9, "Live run (shesha only)".' in out
    flat = " ".join(out.split())  # the stop rules, wherever the epilog's lines break
    assert "answered from the 'us' geo then stops the run" in flat
    assert "a call with no geo reported only keeps its reservation" in flat
    assert "stops on outcome spend_limit, billing, auth_failed, invalid_request, on a call" in flat
    assert "A run that a stop ended never feeds the note." in flat


def test_the_checklist_the_help_points_to_exists() -> None:
    plan = (Path(__file__).parents[3] / "docs" / "plans" / "m1-pr-checks.md").read_text("utf-8")
    pr9 = plan.split("\n## PR 9 ", 1)[1].split("\n## ", 1)[0]  # up to the next level-2 heading

    assert llm_cli.RUNBOOK == 'docs/plans/m1-pr-checks.md, PR 9, "Live run (shesha only)"'
    assert "\n### Live run (shesha only)\n" in pr9


def test_main_refuses_live_without_a_cap(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        llm_cli.main(["--live"])

    assert caught.value.code == 2
    assert "--live requires --max-usd" in capsys.readouterr().err


def test_the_stop_rules_are_the_account_failures_invalid_request_and_the_us_geo() -> None:
    assert set(llm_cli.STOPPING_OUTCOMES) == {
        CallOutcome.SPEND_LIMIT,
        CallOutcome.BILLING,
        CallOutcome.AUTH_FAILED,
        CallOutcome.INVALID_REQUEST,
    }
    assert llm_cli.STOPPING_GEO == "us"


def test_a_call_owns_exactly_one_record_with_its_trace_id() -> None:
    lines = Path(__file__).parent / "measure" / "fixtures" / "summary_run" / "llm_call_log.jsonl"
    first, second = parse_llm_call_lines(lines.read_text("utf-8").splitlines())[:2]
    assert first.trace_id is not None
    line = json.dumps({"message": "llm_call", "llm_call": first.model_dump(mode="json")})
    other = json.dumps({"message": "llm_call", "llm_call": second.model_dump(mode="json")})
    noise = json.dumps({"message": "something else"})

    assert llm_cli.own_record([noise, line], first.trace_id) == first
    assert llm_cli.own_record([other], first.trace_id) is None  # another call's record
    assert llm_cli.own_record([line, line], first.trace_id) is None  # two records
    assert llm_cli.own_record([], first.trace_id) is None  # none
    assert llm_cli.own_record(["not json"], first.trace_id) is None  # unreadable
