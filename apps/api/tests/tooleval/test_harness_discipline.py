"""The run's refusals, all before any database or model (`tooleval/harness.py`, `runs.py`): dry or
real, the settings, the test discipline, the owner's URL, M5's spend and the typed cap. Unit; a
throwaway repository, HEAD as a dict, a fake terminal. Nothing here can make a paid call: every
refusal comes before a provider is built, and the tests' Io builds none."""

import io
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, cast

import pytest

from tests.conftest import TEST_SETTINGS_FIELDS
from tests.intenteval.harness_helpers import PAID, FakeRepo, Now, Terminal
from tests.tooleval.fakes import canned_metrics
from tests.tooleval.helpers import PAWS, SUNRISE, frozen_on_split, letters, make_repo, scenario
from tests.tooleval.runjson import tool_run_info, tool_run_json
from tests.tools.helpers import REPO_ROOT
from vectapilot_api.config import Settings
from vectapilot_api.evalrun.ledger import LedgerEntry, parse_ledger
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.tooleval import runs
from vectapilot_api.tooleval.cli import harness_main
from vectapilot_api.tooleval.harness import (
    Io,
    Refused,
    check_settings,
    confirm_test,
    load,
    synthetic_everywhere,
)
from vectapilot_api.tooleval.ledger import LEDGER_PATH
from vectapilot_api.tooleval.plans import DEFAULT_PLANS
from vectapilot_api.tooleval.runinfo import DRY_TOOL_REPORTS, TOOL_REPORTS, read_run
from vectapilot_api.tooleval.runs import Outcome, make_fingerprint

DRY_PLANS: Final = REPO_ROOT / DEFAULT_PLANS
OWNER: Final = "postgresql+asyncpg://test-only:not-a-secret@127.0.0.1:1/none"


def settings(live: bool) -> Settings:
    return Settings(**(TEST_SETTINGS_FIELDS | (PAID if live else {})))


def no_provider(settings: Settings) -> Provider:
    raise AssertionError("a refused run never builds a provider")


def unmarked(scenario_id: str, business: str = PAWS, **fields: Any) -> dict[str, Any]:
    """A scenario without the TEST-ONLY marker (hash letters only), so a live path can load it."""
    return scenario(scenario_id, business, message=letters(scenario_id), **fields)


SCENARIOS: Final = [unmarked("tonly-1"), unmarked("tonly-2", SUNRISE, read_only=True)]


def repo_for(tmp_path: Path, split: str = "dev") -> tuple[FakeRepo, Path]:
    dataset_dir = frozen_on_split(tmp_path, SCENARIOS, split)
    root = dataset_dir.parents[3]
    return FakeRepo(root=root), dataset_dir


def io_for(
    repo: FakeRepo,
    *,
    live_settings: bool | None = None,
    owner: str | None = OWNER,
    typed: str = "",
) -> Io:
    return Io(
        git=repo.git,
        head_file=repo.head_file,
        head_paths=repo.head_paths,
        head_ref=lambda root: "branch m5/close-out at ccccccc",
        now=Now(),
        stdin=Terminal(typed),
        stdout_is_terminal=lambda: True,
        load_settings=lambda live: settings(live if live_settings is None else live_settings),
        owner_url=lambda: owner,
        build_provider=no_provider,
    )


def refused(argv: list[str], io_: Io, capsys: pytest.CaptureFixture[str]) -> str:
    if "--live" not in argv and "--plans" not in argv and "run" in argv:
        argv = [*argv, "--plans", str(DRY_PLANS)]
    assert harness_main(argv, io_) == 1
    return capsys.readouterr().err


def no_folder(root: Path) -> bool:
    return not (root / TOOL_REPORTS).exists() and not (root / DRY_TOOL_REPORTS).exists()


def test_a_collecting_dataset_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_dir = make_repo(tmp_path)
    repo = FakeRepo(root=dataset_dir.parents[3])
    err = refused(["--dataset", str(dataset_dir), "run", "--split", "dev"], io_for(repo), capsys)
    assert "needs a frozen dataset" in err


@pytest.mark.parametrize(
    ("live", "paid", "message"),
    [
        (False, True, "a run without --live uses the fake provider only"),
        (True, False, "--live needs the paid LLM provider"),
    ],
)
def test_the_provider_must_fit_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], live: bool, paid: bool, message: str
) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev"]
    argv += ["--live", "--max-usd", "0.50"] if live else []
    assert message in refused(argv, io_for(repo, live_settings=paid), capsys)
    assert no_folder(repo.root)


def test_the_harness_never_runs_outside_the_sandbox_environments() -> None:
    production = settings(False).model_copy(update={"environment": "production"})
    with pytest.raises(Refused, match="local, test or ci"):
        check_settings(production, live=False)


def test_a_live_run_refuses_placeholder_scenarios(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset_dir = frozen_on_split(tmp_path, [scenario("s-1")])
    repo = FakeRepo(root=dataset_dir.parents[3])
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.5"]
    err = refused(argv, io_for(repo), capsys)
    assert "is a placeholder" in err and "fails its check" in err


def test_every_manifest_is_synthetic_in_the_committed_repository(tmp_path: Path) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    loaded = load(dataset_dir, None, None, repo.git(), repo.head_file, live=True)
    assert synthetic_everywhere(loaded)
    # One business's seed saying otherwise is enough to refuse (its parser refuses it today; the
    # guard must hold if that is ever relaxed).
    tools = loaded.dataset.sandbox[PAWS]
    unsynthetic = replace(tools, seed=tools.seed.model_copy(update={"synthetic": False}))
    dataset = replace(loaded.dataset, sandbox={**loaded.dataset.sandbox, PAWS: unsynthetic})
    assert not synthetic_everywhere(replace(loaded, dataset=dataset))


def test_a_live_run_is_refused_unless_every_manifest_is_synthetic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    monkeypatch.setattr(runs, "synthetic_everywhere", lambda loaded: False)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.5"]
    err = refused(argv, io_for(repo, typed="0.5\n"), capsys)
    assert "--live needs every manifest to say synthetic: true" in err
    assert no_folder(repo.root)


def test_dry_runs_never_touch_test(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, dataset_dir = repo_for(tmp_path, "test")
    err = refused(["--dataset", str(dataset_dir), "run", "--split", "test"], io_for(repo), capsys)
    assert "dry runs are refused on the test split" in err


def test_an_empty_split_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, dataset_dir = repo_for(tmp_path, "dev")
    err = refused(["--dataset", str(dataset_dir), "run", "--split", "test"], io_for(repo), capsys)
    assert "no scenarios on the test split" in err


def live_test(dataset_dir: Path) -> list[str]:
    return ["--dataset", str(dataset_dir), "run", "--split", "test", "--live", "--max-usd", "0.5"]


def fingerprint_of(repo: FakeRepo, dataset_dir: Path) -> str:
    loaded = load(dataset_dir, None, None, repo.git(), repo.head_file, live=True)
    return make_fingerprint(loaded, settings(True), load_catalog(), None).sha256()


def ledger_line(fingerprint: str, freeze: str, run_id: str = "t0-live-agent-test") -> bytes:
    entry = LedgerEntry(
        dataset="tool_calls",
        version="v0.1",
        freeze_sha256=freeze,
        config_fingerprint=fingerprint,
        run_id=run_id,
        commit="c" * 40,
        started_at=Now()(),
        rerun_reason=None,
    )
    return (entry.model_dump_json() + "\n").encode("utf-8")


def committed_dev_run(
    repo: FakeRepo,
    fingerprint: str,
    *,
    status: str = "complete",
    dry: bool = False,
    commit: bool = True,
) -> None:
    """A live dev folder of this fingerprint, complete and committed unless told otherwise (its
    fingerprint field is a stand-in; only the hash is compared)."""
    folder = repo.root / TOOL_REPORTS / "v0.1" / "t1-live-agent-dev"
    folder.mkdir(parents=True)
    info = tool_run_info("t1-live-agent-dev", status=status, dry=dry).model_copy(
        update={"fingerprint_sha256": fingerprint}
    )
    (folder / "run.json").write_text(info.model_dump_json(indent=2) + "\n", "utf-8")
    if commit:
        repo.commit(folder)


def test_the_test_discipline_refuses_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = repo_for(tmp_path, "test")
    loaded = load(dataset_dir, None, None, repo.git(), repo.head_file, live=True)
    freeze = loaded.freeze_sha256 or ""
    fingerprint = fingerprint_of(repo, dataset_dir)

    repo.dirty = True
    assert "clean working tree" in refused(live_test(dataset_dir), io_for(repo), capsys)
    repo.dirty = False

    ledger = repo.root / LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(ledger_line(fingerprint, freeze))
    assert "is not as committed in HEAD" in refused(live_test(dataset_dir), io_for(repo), capsys)

    repo.commit(ledger)
    err = refused(live_test(dataset_dir), io_for(repo), capsys)
    assert "already has 1 test run(s)" in err and "--test-rerun-reason" in err

    rerun = [*live_test(dataset_dir), "--test-rerun-reason", "provider-outage"]
    assert "run dev first" in refused(rerun, io_for(repo), capsys)

    committed_dev_run(repo, fingerprint)
    err = refused(rerun, io_for(repo, typed="test tool_call\n"), capsys)
    assert err.strip().endswith("not confirmed")
    assert not list((repo.root / TOOL_REPORTS / "v0.1").glob("*-live-agent-test"))


@pytest.mark.parametrize(
    ("typed", "stdin_tty", "stdout_tty", "expected"),
    [
        ("test tool_calls\n", True, True, True),
        ("test tool_call\n", True, True, False),
        ("test tool_calls extra\n", True, True, False),
        ("test tool_calls\n", False, True, False),
        ("test tool_calls\n", True, False, False),
    ],
    ids=["exact", "short", "longer", "stdin-not-a-terminal", "stdout-not-a-terminal"],
)
def test_the_test_words_are_typed_exactly_at_a_terminal(
    typed: str, stdin_tty: bool, stdout_tty: bool, expected: bool
) -> None:
    stdin = Terminal(typed) if stdin_tty else io.StringIO(typed)
    assert confirm_test(Io(stdin=stdin, stdout_is_terminal=lambda: stdout_tty)) is expected


@pytest.mark.parametrize(
    "dev_run",
    [{"status": "partial"}, {"dry": True}, {"commit": False}],
    ids=["partial", "dry", "uncommitted"],
)
def test_only_a_complete_committed_live_dev_run_unlocks_test(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dev_run: dict[str, Any]
) -> None:
    repo, dataset_dir = repo_for(tmp_path, "test")
    committed_dev_run(repo, fingerprint_of(repo, dataset_dir), **dev_run)
    err = refused(live_test(dataset_dir), io_for(repo, typed="test tool_calls\n0.5\n"), capsys)
    assert "run dev first" in err
    assert not list((repo.root / TOOL_REPORTS / "v0.1").glob("*-live-agent-test"))


def test_a_live_test_run_is_ledgered_before_its_first_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every check passes; the run itself is replaced, so no provider is used and no database is
    # touched: the replacement only reads the ledger as the first scenario would find it.
    repo, dataset_dir = repo_for(tmp_path, "test")
    loaded = load(dataset_dir, None, None, repo.git(), repo.head_file, live=True)
    fingerprint = fingerprint_of(repo, dataset_dir)
    committed_dev_run(repo, fingerprint)
    ledger = repo.root / LEDGER_PATH
    seen: list[LedgerEntry] = []

    async def first_scenario(self: object, scenarios: object, sink: object) -> Outcome:
        seen.extend(parse_ledger(ledger.read_text("utf-8")))
        return Outcome("complete")

    monkeypatch.setattr(runs._Run, "all", first_scenario)
    io_ = replace(
        io_for(repo, typed="test tool_calls\n0.5\n"),
        build_provider=lambda settings: cast(Provider, object()),
        metrics=canned_metrics(),
    )
    assert harness_main(live_test(dataset_dir), io_) == 0

    [folder] = (repo.root / TOOL_REPORTS / "v0.1").glob("*-live-agent-test")
    assert [(e.run_id, e.config_fingerprint, e.freeze_sha256) for e in seen] == [
        (folder.name, fingerprint, loaded.freeze_sha256)
    ]
    assert len(ledger.read_text("utf-8").splitlines()) == 1
    info = read_run(folder)
    assert (info.split, info.dry_run, info.test_runs_before) == ("test", False, 0)


def test_a_run_needs_the_owners_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    err = refused(
        ["--dataset", str(dataset_dir), "run", "--split", "dev"], io_for(repo, owner=None), capsys
    )
    assert "owner's URL" in err
    assert no_folder(repo.root)


def test_a_live_cap_above_what_is_left_of_d8_is_refused_before_the_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    spent = repo.root / TOOL_REPORTS / "v0.1" / "t0-live-agent-dev"
    spent.mkdir(parents=True)
    (spent / "run.json").write_bytes(tool_run_json("t0-live-agent-dev", total="0.90"))
    repo.commit(spent)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.20"]
    err = refused(argv, io_for(repo, typed="0.20\n"), capsys)
    assert "more than what is left of M5's $1 ceiling (D8)" in err
    assert not any((repo.root / TOOL_REPORTS / "v0.1").glob("*-live-agent-dev-*"))


def test_a_live_cap_typed_wrong_cancels_before_any_folder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.20"]
    err = refused(argv, io_for(repo, typed="0.2\n"), capsys)
    assert err.strip().endswith("not confirmed")
    assert no_folder(repo.root)
    out = capsys.readouterr().out
    assert "TEST-ONLY" not in out and "tonly-1" not in out  # no scenario text or id printed


def test_a_missing_plan_file_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, dataset_dir = repo_for(tmp_path)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--plans", str(tmp_path / "x")]
    assert "plan file x is missing" in refused(argv, io_for(repo), capsys)
