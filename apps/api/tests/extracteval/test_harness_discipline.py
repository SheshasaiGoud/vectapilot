"""The run's refusals, all before any model call (`extracteval/harness.py`, `runs.py`): dry or real,
the settings, synthetic everywhere, H1 not written, the test discipline, M6's spend and the typed
cap. A throwaway repository, HEAD as a dict, a fake terminal. Nothing here can make a paid call:
every refused run here fails before a provider is built (`no_provider` raises if it is)."""

import io
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.extracteval.fakes import STAND_IN_ROUTE, unwritten_h1
from tests.extracteval.harness_helpers import (
    FIXTURES,
    dry_args,
    dry_repo,
    frozen_repo,
    io_for,
    live_replies,
    scripted,
)
from tests.extracteval.helpers import TODAY, filled_repo
from tests.extracteval.runjson import T0, extract_run_info, extract_run_json
from tests.extraction.standins import raising
from tests.intenteval.harness_helpers import FakeRepo
from vectapilot_api.evalrun.ledger import LedgerEntry
from vectapilot_api.extracteval import runs
from vectapilot_api.extracteval.checks import freeze_dataset
from vectapilot_api.extracteval.cli import harness_main
from vectapilot_api.extracteval.files import read_dataset, write_frozen
from vectapilot_api.extracteval.harness import (
    LEDGER_PATH,
    Io,
    Loaded,
    Refused,
    check_test_run,
    config_files,
    confirm_test,
    h1_written,
    load_dry,
    load_live,
    synthetic_everywhere,
)
from vectapilot_api.extracteval.runinfo import (
    DRY_EXTRACT_REPORTS,
    EXTRACT_REPORTS,
    RUN_FILE,
    read_run,
)

LIVE: list[str] = ["--live", "--max-usd", "0.50"]


def refused(argv: list[str], io_: Io, capsys: pytest.CaptureFixture[str]) -> str:
    assert harness_main(argv, io_) == 1
    return capsys.readouterr().err


def no_folder(root: Path) -> bool:
    return not (root / EXTRACT_REPORTS).exists() and not (root / DRY_EXTRACT_REPORTS).exists()


def live_args(dataset_dir: Path, split: str = "dev") -> list[str]:
    return ["--dataset", str(dataset_dir), "run", "--split", split, *LIVE]


# ── Arguments (argparse, exit 2) ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--live"], "--live requires --max-usd"),
        (["--max-usd", "0.50"], "--max-usd is for --live runs only"),
        (["--live", "--max-usd", "1.01"], "--max-usd must be plain digits"),
        (["--live", "--max-usd", "1e-1"], "--max-usd must be plain digits"),
        (["--live", "--max-usd", "0.5", "--fixtures", "x"], "--fixtures is for dry runs only"),
        (["--test-rerun-reason", "Not A Slug"], "--test-rerun-reason must be"),
    ],
)
def test_bad_arguments_stop_before_anything_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    repo = FakeRepo(root=tmp_path)
    argv = ["--dataset", str(tmp_path), "run", "--split", "dev", *extra]
    with pytest.raises(SystemExit) as stopped:
        harness_main(argv, io_for(repo))
    assert stopped.value.code == 2
    assert message in capsys.readouterr().err
    assert no_folder(tmp_path)


# ── Dry or real ──────────────────────────────────────────────────────────────────────────────


def test_a_dry_run_on_test_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    argv = ["--dataset", str(dataset_dir), "run", "--split", "test", "--fixtures", str(FIXTURES)]
    assert "dry runs are refused on the test split" in refused(argv, io_for(repo), capsys)
    assert no_folder(repo.root)


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
    repo, dataset_dir = frozen_repo(tmp_path)
    argv = live_args(dataset_dir) if live else dry_args(dataset_dir)
    assert message in refused(argv, io_for(repo, live_settings=paid), capsys)
    assert no_folder(repo.root)


@pytest.mark.parametrize("environment", ["production"])
def test_the_harness_runs_only_in_local_test_or_ci(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], environment: str
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    err = refused(dry_args(dataset_dir), io_for(repo, environment=environment), capsys)
    assert "runs only in local, test or ci" in err
    assert no_folder(repo.root)


def test_a_live_run_needs_a_frozen_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    err = refused(live_args(dataset_dir), io_for(repo, live_settings=True), capsys)
    assert "a live run needs a frozen dataset" in err
    assert no_folder(repo.root)


def test_a_live_run_refuses_a_document_edited_after_the_freeze(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    source = dataset_dir / "documents" / "s-1.pdf.txt"
    source.write_text(source.read_text("utf-8").replace("referral", "TEST-ONLY referral"))
    err = refused(live_args(dataset_dir), io_for(repo, live_settings=True), capsys)
    assert "the dataset fails its check" in err
    assert no_folder(repo.root)


def test_a_live_run_never_reads_a_placeholder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # TEST-ONLY documents frozen with the test switch: every pin matches, so the placeholder rule
    # alone refuses (ADR 0012: a TEST-ONLY copy never reaches a paid call; mutation audit H02).
    dataset_dir = filled_repo(tmp_path)
    frozen = freeze_dataset(read_dataset(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    write_frozen(dataset_dir, frozen)
    repo = FakeRepo(root=dataset_dir.parents[3])
    build, fake = scripted(live_replies())
    io_ = io_for(repo, live_settings=True, typed="0.50\n", provider=build)

    err = refused(live_args(dataset_dir), io_, capsys)

    assert "the dataset fails its check" in err
    problems = [line for line in err.splitlines() if line.startswith("PROBLEM")]
    assert problems and all("is a placeholder" in line for line in problems), problems
    assert fake.calls == []
    assert no_folder(repo.root)


def test_a_live_run_reads_its_configuration_from_inside_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The freeze does not pin templates or business files, so a live run must read the ones a
    # commit can name: an override outside the repository is refused before anything is sent.
    repo, dataset_dir = frozen_repo(tmp_path)
    outside = tmp_path / "elsewhere"
    shutil.copytree(repo.root / "data", outside)
    for flag, folder in (("--extraction", "extraction"), ("--corpus", "sample")):
        argv = [flag, str(outside / folder), *live_args(dataset_dir)]
        build, fake = scripted(live_replies())
        io_ = io_for(repo, live_settings=True, typed="0.50\n", provider=build)
        err = refused(argv, io_, capsys)
        assert f"{flag}: a live run reads its configuration from inside the repository" in err
        assert fake.calls == []
    assert no_folder(repo.root)


def test_files_at_the_commit_covers_the_configuration_too(tmp_path: Path) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    repo.commit(dataset_dir)
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "dev")
    assert not loaded.committed  # the dataset is in HEAD, its configuration is not yet

    repo.commit(repo.root / "data")
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "dev")
    assert loaded.committed
    names = sorted(p.relative_to(repo.root).as_posix() for p in _config_of(loaded))
    assert names == [
        "data/extraction/businesses/paws-and-co.json",
        "data/extraction/businesses/sunrise-clinic.json",
        "data/extraction/templates/human_healthcare/referral_letter.json",
        "data/extraction/templates/veterinary/vaccination_record.json",
        "data/sample/paws-and-co/manifest.json",
        "data/sample/sunrise-clinic/manifest.json",
    ]

    template = repo.root / "data/extraction/templates/veterinary/vaccination_record.json"
    template.write_text(template.read_text("utf-8").replace('"Animal"', '"Animal", "Kind"'))
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "dev")
    assert (
        not loaded.committed
    )  # an uncommitted label: summary.md says "... files at the commit: NO"


def test_synthetic_everywhere_reads_every_business_file(tmp_path: Path) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "dev")
    assert synthetic_everywhere(loaded)
    # The parser refuses an enabled kind without synthetic: true; relaxed here (model_copy does
    # not validate) to show the guard holds if the parser ever changes.
    business = loaded.extraction.businesses["paws-and-co"]
    relaxed = replace(business, file=business.file.model_copy(update={"synthetic": False}))
    extraction = replace(
        loaded.extraction, businesses={**loaded.extraction.businesses, "paws-and-co": relaxed}
    )
    assert not synthetic_everywhere(replace(loaded, extraction=extraction))
    # The same for the dataset's manifest and a gold row (live) and the fixture file (dry).
    manifest = loaded.manifest.model_copy(update={"synthetic": False})
    assert not synthetic_everywhere(replace(loaded, manifest=manifest))
    assert loaded.dataset is not None
    first, *rest = loaded.dataset.rows
    rows = (first.model_copy(update={"synthetic": False}), *rest)
    assert not synthetic_everywhere(replace(loaded, dataset=replace(loaded.dataset, rows=rows)))
    dry_root, dry_dir = dry_repo(tmp_path / "dry")
    dry = load_dry(dry_dir, None, None, FIXTURES, dry_root.git(), "dev")
    assert synthetic_everywhere(dry) and dry.fixtures is not None
    relaxed_file = dry.fixtures.file.model_copy(update={"synthetic": False})
    assert not synthetic_everywhere(replace(dry, fixtures=replace(dry.fixtures, file=relaxed_file)))


def test_a_run_is_refused_unless_everything_is_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The run's own guard, apart from the parsers that refuse most of this first (mutation audit
    # U26): when it says no, nothing is written and no provider is built.
    monkeypatch.setattr(runs, "synthetic_everywhere", lambda loaded: False)
    repo, dataset_dir = dry_repo(tmp_path)
    err = refused(dry_args(dataset_dir), io_for(repo), capsys)
    assert "must say synthetic: true" in err
    assert no_folder(repo.root)


def test_a_corpus_manifest_that_is_not_synthetic_refuses_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    corpus = repo.root / "data" / "sample" / "paws-and-co" / "manifest.json"
    corpus.write_text(corpus.read_text("utf-8").replace('"synthetic": true', '"synthetic": false'))
    err = refused(live_args(dataset_dir), io_for(repo, live_settings=True), capsys)
    assert "synthetic" in err
    assert no_folder(repo.root)


def test_h1_not_written_refuses_a_live_run_before_any_paid_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not h1_written(unwritten_h1)
    assert h1_written(STAND_IN_ROUTE)
    assert h1_written(raising)  # written, and failing: the service fails closed on it (E3)
    repo, dataset_dir = frozen_repo(tmp_path)
    io_ = io_for(repo, live_settings=True, typed="0.50\n", route=unwritten_h1)
    assert "H1 (route_fields) is not written yet" in refused(live_args(dataset_dir), io_, capsys)
    assert no_folder(repo.root)


def test_a_live_run_without_the_typed_cap_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    build, fake = scripted(live_replies())
    io_ = io_for(repo, live_settings=True, typed="0.5\n", provider=build)
    assert "not confirmed" in refused(live_args(dataset_dir), io_, capsys)
    assert fake.calls == []
    assert no_folder(repo.root)


def test_a_live_cap_above_what_is_left_of_m6s_dollar_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    spent = repo.root / "evals/reports/doc_extraction/v0.1/20261113T120000Z-live-extract-dev"
    spent.mkdir(parents=True)
    (spent / "run.json").write_bytes(extract_run_json(spent.name, total="0.80"))
    repo.commit(spent)
    build, fake = scripted(live_replies())
    io_ = io_for(repo, live_settings=True, typed="0.50\n", provider=build)
    err = refused(live_args(dataset_dir), io_, capsys)
    assert "--max-usd $0.5 is more than what is left of M6's $1 ceiling" in err
    assert fake.calls == []


def _config_of(loaded: Loaded) -> list[Path]:
    assert loaded.dataset is not None
    root = loaded.root
    return config_files(loaded.dataset, root / "data" / "extraction", loaded.corpus_dir)


# ── The test discipline ──────────────────────────────────────────────────────────────────────


def test_test_needs_a_clean_tree_a_committed_ledger_and_a_dev_run(tmp_path: Path) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "test")
    fingerprint = "a" * 64
    with pytest.raises(Refused, match="dry runs are refused on the test split"):
        check_test_run(
            io_for(repo), repo.git(), loaded, fingerprint=fingerprint, live=False, reason=None
        )
    repo.dirty = True
    with pytest.raises(Refused, match="clean working tree"):
        check_test_run(
            io_for(repo), repo.git(), loaded, fingerprint=fingerprint, live=True, reason=None
        )
    repo.dirty = False
    ledger = repo.root / LEDGER_PATH
    ledger.parent.mkdir(parents=True)
    entry = LedgerEntry(
        dataset="doc_extraction",
        version="v0.1",
        freeze_sha256=loaded.freeze_sha256,
        config_fingerprint=fingerprint,
        run_id="20261113T120000Z-live-extract-test",
        commit="c" * 40,
        started_at=T0,
        rerun_reason=None,
    )
    ledger.write_text(entry.model_dump_json() + "\n", "utf-8")
    with pytest.raises(Refused, match="is not as committed in HEAD"):
        check_test_run(
            io_for(repo), repo.git(), loaded, fingerprint=fingerprint, live=True, reason=None
        )
    repo.commit(ledger)
    with pytest.raises(Refused, match="already has 1 test run"):
        check_test_run(
            io_for(repo), repo.git(), loaded, fingerprint=fingerprint, live=True, reason=None
        )
    other = "b" * 64  # another config: its own first run, but no dev run with it yet
    with pytest.raises(Refused, match="no complete live dev run with this fingerprint"):
        check_test_run(io_for(repo), repo.git(), loaded, fingerprint=other, live=True, reason=None)


def test_only_a_complete_dev_run_opens_test_and_a_reason_allows_a_rerun(tmp_path: Path) -> None:
    # Mutation audit H17 (a dev run of any status) and H20 (a rerun refused despite its reason).
    repo, dataset_dir = frozen_repo(tmp_path)
    loaded = load_live(dataset_dir, None, None, repo.git(), repo.head_file, "test")
    assert loaded.freeze_sha256 is not None
    info = extract_run_info("20261113T120000Z-live-extract-dev")
    fingerprint = info.fingerprint_sha256
    run_json = repo.root / EXTRACT_REPORTS / "v0.1" / info.run_id / RUN_FILE
    run_json.parent.mkdir(parents=True)
    partial = info.model_copy(update={"status": "partial", "stop": "the cap was reached"})
    run_json.write_text(partial.model_dump_json(indent=2), "utf-8")
    repo.commit(run_json)
    assert read_run(run_json.parent).status == "partial"  # a valid run.json, only not complete

    def check(reason: str | None) -> int:
        io_ = io_for(repo, typed="test doc_extraction\n")
        return check_test_run(
            io_, repo.git(), loaded, fingerprint=fingerprint, live=True, reason=reason
        )

    with pytest.raises(Refused, match="no complete live dev run with this fingerprint"):
        check(None)

    run_json.write_text(info.model_dump_json(indent=2), "utf-8")
    repo.commit(run_json)
    assert check(None) == 0
    ledger = repo.root / LEDGER_PATH
    entry = LedgerEntry(
        dataset="doc_extraction",
        version="v0.1",
        freeze_sha256=loaded.freeze_sha256,
        config_fingerprint=fingerprint,
        run_id="20261113T130000Z-live-extract-test",
        commit="c" * 40,
        started_at=T0,
        rerun_reason=None,
    )
    ledger.write_text(entry.model_dump_json() + "\n", "utf-8")
    repo.commit(ledger)
    with pytest.raises(Refused, match="already has 1 test run"):
        check(None)
    assert check("fix-x") == 1  # the earlier runs, recorded in the rerun's run.json


def test_the_test_confirmation_is_typed_at_a_terminal() -> None:
    repo = FakeRepo(root=Path("/nonexistent"))
    assert confirm_test(io_for(repo, typed="test doc_extraction\n"))
    assert not confirm_test(io_for(repo, typed="test doc_extraction please\n"))
    assert not confirm_test(io_for(repo, typed="test tool_calls\n"))
    # The right words, but stdout or stdin is not a terminal (mutation audit H12).
    at_terminal = io_for(repo, typed="test doc_extraction\n")
    assert not confirm_test(replace(at_terminal, stdout_is_terminal=lambda: False))
    assert not confirm_test(replace(at_terminal, stdin=io.StringIO("test doc_extraction\n")))
