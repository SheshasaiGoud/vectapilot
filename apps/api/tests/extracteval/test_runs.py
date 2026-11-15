"""The harness's runs (`extracteval/runs.py`), end to end at $0.

Dry runs go through the real extraction core (text, the extraction's gateway on the scripted
`ScriptProvider`, signals, the invariant check) over the committed fixtures, with the stand-in
router (`accept_everything`, deliberately not H1) and canned H2 metrics (deliberately not H2).
Live-path runs use a frozen TEST copy in tmp_path, paid-looking settings and a `FakeProvider`
handed out by `build_provider`: nothing is ever sent anywhere. Every value and quote of the
fixtures is a canary: none may reach run.json, results.jsonl, summary.md, the llm_call log or
stdout; values.jsonl holds them.
"""

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import pytest

from tests.extracteval.fakes import UNWRITTEN_H2, canned_metrics, unwritten_h1
from tests.extracteval.harness_helpers import (
    FIXTURES,
    dry_args,
    dry_repo,
    frozen_repo,
    io_for,
    json_lines,
    live_replies,
    scripted,
)
from tests.extraction.fakes import referral, vaccination
from tests.extraction.standins import (
    REFERRAL_ANSWER,
    REFERRAL_LINES,
    VACCINATION_ANSWER,
    VACCINATION_LINES,
    pdf,
    reply,
    with_changes,
)
from tests.extraction.standins import run as run_service
from tests.intenteval.harness_helpers import FakeRepo
from vectapilot_api.evalrun.budget import record_for
from vectapilot_api.evalrun.ledger import parse_ledger
from vectapilot_api.extracteval import runs
from vectapilot_api.extracteval.cli import harness_main
from vectapilot_api.extracteval.dryrun import FIXTURES_FILE, FixtureFile
from vectapilot_api.extracteval.harness import LEDGER_PATH, EvalDocument, Io
from vectapilot_api.extracteval.runinfo import (
    DRY_EXTRACT_REPORTS,
    DRY_LABEL,
    EXTRACT_REPORTS,
    LLM_LOG_FILE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    VALUES_FILE,
    FieldLine,
    read_results,
    read_run,
)
from vectapilot_api.extraction.config import KindConfig
from vectapilot_api.extraction.service import ExtractionOutcome, ExtractionService
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import CallRecord

FIXTURE_FILE: Final = FixtureFile.model_validate_json((FIXTURES / FIXTURES_FILE).read_bytes())


def canaries() -> set[str]:
    """Every gold value, scripted value and quote of the fixtures, and every document line."""
    found: set[str] = set()
    for document in FIXTURE_FILE.documents:
        found |= {v for v in document.gold.values() if v is not None and len(v) > 4}
        for step in document.script:
            for item in (step.answer or {}).values():
                found |= {t for t in (item.value, item.quote) if t is not None and len(t) > 4}
    for source in FIXTURES.glob("*.pdf.txt"):
        found |= {
            line.split("} ", 1)[-1]
            for line in source.read_text("utf-8").splitlines()
            if line and not line.startswith("===")
        }
    return found


def only_folder(root: Path, *, live: bool) -> Path:
    base = root / (EXTRACT_REPORTS if live else DRY_EXTRACT_REPORTS) / "v0.1"
    folders = [p for p in base.iterdir() if p.is_dir()]
    assert len(folders) == 1, folders
    return folders[0]


def run_dry(tmp_path: Path, io_: Io | None = None) -> tuple[FakeRepo, Path, int]:
    repo, dataset_dir = dry_repo(tmp_path)
    code = harness_main(dry_args(dataset_dir), io_ or io_for(repo))
    return repo, only_folder(repo.root, live=False), code


# ── Dry runs ─────────────────────────────────────────────────────────────────────────────────


def test_a_dry_run_scores_every_fixture_and_labels_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, folder, code = run_dry(tmp_path)
    out = capsys.readouterr().out

    assert code == 0
    assert folder.name.endswith("-fake-extract-dev")
    info = read_run(folder)
    assert (info.status, info.dry_run, info.llm_provider, info.split) == (
        "complete",
        True,
        "fake",
        "dev",
    )
    assert info.freeze_sha256 is None and info.fingerprint.freeze_sha256 is None
    assert info.fingerprint.fixtures_sha256 is not None
    assert info.fingerprint.extract_prompt_version == "extract-fields.1"
    assert set(info.fingerprint.businesses) == {"sunrise-clinic", "paws-and-co"}
    assert info.documents == 6 and info.cost.llm_calls == 6
    documents = read_results(folder)
    assert [d.id for d in documents] == [d.id for d in FIXTURE_FILE.documents]
    assert all(d.run == "scored" for d in documents)
    summary = (folder / SUMMARY_MD).read_text("utf-8")
    assert summary.splitlines()[2].startswith(f"**{DRY_LABEL}**")
    assert "DRY RUN: a scripted fake model over fixture documents" in out
    assert "(DRY RUN: git ignores it)" in out


def test_a_dry_run_shows_e3_failures_and_the_invariant_check_by_id(tmp_path: Path) -> None:
    _, folder, _ = run_dry(tmp_path)
    by_id = {d.id: d for d in read_results(folder)}

    truncated = by_id["tonly-referral-truncated"]
    assert (truncated.status, truncated.failure, truncated.error) == (
        "needs_review",
        "truncated",
        "OutputTruncated",
    )
    assert all(f.status == "review" and f.reasons == ("truncated",) for f in truncated.fields)
    assert all(f.correct is False and not f.has_value for f in truncated.fields)

    plain = by_id["tonly-vaccination-plain"]
    assert plain.status == "complete" and all(f.correct for f in plain.fields)
    assert {f.status for f in plain.fields} == {"accepted"}

    swap = {f.field: f for f in by_id["tonly-referral-swap"].fields}
    assert swap["date_of_birth"].status == "review" and swap["date_of_birth"].corrected
    assert swap["date_of_birth"].correct is False
    assert swap["insurer"].status == "review"  # printed, so never absent (E4)

    planted = {f.field: f for f in by_id["tonly-vaccination-planted"].fields}
    assert (planted["issuing_clinic"].status, planted["issuing_clinic"].correct) == (
        "accepted",
        False,
    )  # the fixture's gold disagrees on purpose: a silent error for the plumbing to count


def test_no_value_quote_or_line_leaves_values_jsonl(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, folder, _ = run_dry(tmp_path)
    printed = capsys.readouterr()
    texts = {
        name: (folder / name).read_text("utf-8")
        for name in (RUN_FILE, RESULTS_FILE, SUMMARY_MD, LLM_LOG_FILE)
    }
    texts["stdout"], texts["stderr"] = printed.out, printed.err
    found = canaries()
    assert len(found) > 40 and "Ilsa Corvane" in found and "Patient name: Ilsa Corvane" in found
    for canary in found:
        for name, text in texts.items():
            assert canary not in text, f"a fixture value or line reached {name}"
    values = json_lines(folder / VALUES_FILE)
    assert len(values) == sum(len(d.gold) for d in FIXTURE_FILE.documents)
    by_key = {(v["id"], v["field"]): v for v in values}
    clinic = by_key[("tonly-vaccination-planted", "issuing_clinic")]
    assert clinic["value"] == "Hollowbrook Vet Clinic"
    assert clinic["gold"] == "Fernside Vet Clinic"
    assert clinic["quote"] == "Issuing clinic: Hollowbrook Vet Clinic"
    dob = by_key[("tonly-referral-plain", "date_of_birth")]
    assert (dob["value"], dob["proposed"]) == ("1990-03-14", "14/03/1990")


def test_h2_not_written_keeps_the_results_and_writes_no_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    code = harness_main(dry_args(dataset_dir), io_for(repo, metrics=UNWRITTEN_H2))
    folder = only_folder(repo.root, live=False)
    assert code == 1
    assert "summary not written: H2 is not written yet" in capsys.readouterr().out
    assert not (folder / SUMMARY_MD).exists()
    assert len(read_results(folder)) == 6 and read_run(folder).status == "complete"

    assert harness_main(["summarize", str(folder)], io_for(repo)) == 0
    assert (folder / SUMMARY_MD).read_text("utf-8").startswith("# doc_extraction run")


def test_an_inconsistent_h2_writes_the_warning_first_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    code = harness_main(dry_args(dataset_dir), io_for(repo, metrics=canned_metrics()))
    printed = capsys.readouterr()
    folder = only_folder(repo.root, live=False)
    assert code == 1
    lines = (folder / SUMMARY_MD).read_text("utf-8").splitlines()
    assert lines[2].startswith(f"**{DRY_LABEL}**") and lines[4].startswith("**Inconsistent")
    assert "summary.md opens with Inconsistent" in printed.err
    assert read_run(folder).status == "complete"  # the run itself finished; its numbers do not

    assert harness_main(["summarize", str(folder)], io_for(repo, metrics=canned_metrics())) == 1
    assert harness_main(["summarize", str(folder)], io_for(repo)) == 0
    assert "Inconsistent" not in (folder / SUMMARY_MD).read_text("utf-8")


def test_h1_not_written_stops_a_dry_run_after_its_first_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = dry_repo(tmp_path)
    code = harness_main(dry_args(dataset_dir), io_for(repo, route=unwritten_h1))
    folder = only_folder(repo.root, live=False)
    info = read_run(folder)
    documents = read_results(folder)
    assert code == 1
    assert info.status == "failed" and "H1 (route_fields) is not written yet" in (info.stop or "")
    assert documents[0].run == "scored" and documents[0].failure == "internal_error"
    assert documents[0].error == "NotImplementedError"
    assert [d.run for d in documents[1:]] == ["not_run"] * 5
    assert not (folder / SUMMARY_MD).exists()
    assert info.cost.llm_calls == 1


def test_summarize_refuses_a_folder_whose_results_do_not_match_run_json(tmp_path: Path) -> None:
    repo, folder, _ = run_dry(tmp_path)
    results = folder / RESULTS_FILE
    results.write_text("".join(results.read_text("utf-8").splitlines(True)[:-1]), "utf-8")
    assert harness_main(["summarize", str(folder)], io_for(repo)) == 1


def test_each_field_is_scored_as_its_templates_type(tmp_path: Path) -> None:
    # Identifiers and enums are exact; only text is case-folded (compare.py). A field typed by the
    # wrong comparison would quietly raise measured accuracy (mutation audit U11, U12).
    _, folder, _ = run_dry(tmp_path)
    types = {d.id: {f.field: f.type for f in d.fields} for d in read_results(folder)}
    referral_types = types["tonly-referral-plain"]
    vaccination_types = types["tonly-vaccination-plain"]
    assert (referral_types["policy_number"], referral_types["date_of_birth"]) == (
        "identifier",
        "date",
    )
    assert (vaccination_types["species"], vaccination_types["pet_name"]) == ("enum", "text")


class OtherBusinessService(ExtractionService):
    """The real core, whose outcome then names another business: a planted leak."""

    async def extract(
        self, kind: KindConfig, data: bytes, *, business_id: str, trace_id: str
    ) -> ExtractionOutcome:
        outcome = await super().extract(kind, data, business_id=business_id, trace_id=trace_id)
        return dataclasses.replace(outcome, business_id="other-business")


def record_naming_another_business(lines: list[str], trace_id: str | None) -> CallRecord | None:
    """The run's `llm_call` record, then naming another business: a planted leak."""
    record = record_for(lines, trace_id)
    return None if record is None else record.model_copy(update={"business_id": "other-business"})


@pytest.mark.parametrize(
    ("planted", "named"),
    [
        ("extraction", "the extraction names another business"),
        ("llm_call", "its llm_call record names another business"),
    ],
)
def test_a_cross_business_leak_stops_the_run_at_once_with_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, planted: str, named: str
) -> None:
    # The hard gate (zero cross-business leaks): the first document's leak stops the run, the
    # rest never run, no summary, exit 4 (mutation audit U15, U17).
    if planted == "extraction":
        monkeypatch.setattr(runs, "ExtractionService", OtherBusinessService)
    else:
        monkeypatch.setattr(runs, "record_for", record_naming_another_business)
    _, folder, code = run_dry(tmp_path)
    info = read_run(folder)
    documents = read_results(folder)

    assert code == runs.EXIT_LEAK == 4
    assert (info.leak, info.status) == (True, "failed")
    first = FIXTURE_FILE.documents[0].id
    assert info.stop == f"cross-business leak in document {first}: {named}"
    assert documents[0].run == "scored"
    assert [d.run for d in documents[1:]] == ["not_run"] * 5
    assert info.cost.llm_calls == 1
    assert not (folder / SUMMARY_MD).exists()


# ── Live path (a FakeProvider behind paid-looking settings: nothing is sent) ──────────────────


def test_a_live_dev_run_on_the_frozen_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    build, fake = scripted(live_replies())
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.50"]
    code = harness_main(argv, io_for(repo, live_settings=True, typed="0.50\n", provider=build))
    out = capsys.readouterr().out

    assert code == 0, out
    folder = only_folder(repo.root, live=True)
    assert folder.name.endswith("-live-extract-dev")
    info = read_run(folder)
    assert (info.dry_run, info.llm_provider, info.status) == (False, "anthropic", "complete")
    assert info.freeze_sha256 is not None and info.fingerprint.fixtures_sha256 is None
    assert [d.id for d in read_results(folder)] == ["s-1", "p-1"]
    assert len(fake.calls) == 2
    assert "M6 spend so far" in out and "m6/close-out" in out
    assert not (repo.root / LEDGER_PATH).exists()


def test_a_cap_too_small_for_one_call_runs_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    build, fake = scripted(live_replies())
    argv = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live"]
    argv += ["--max-usd", "0.000001"]
    io_ = io_for(repo, live_settings=True, typed="0.000001\n", provider=build)
    assert harness_main(argv, io_) == 3
    folder = only_folder(repo.root, live=True)
    info = read_run(folder)
    assert info.status == "partial" and "not sent" in (info.stop or "")
    assert [d.run for d in read_results(folder)] == ["not_run", "not_run"]
    assert fake.calls == []


def test_a_test_run_is_ledgered_first_and_once_per_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, dataset_dir = frozen_repo(tmp_path)
    repo.commit(dataset_dir)
    build, _ = scripted(live_replies())
    dev = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.50"]
    assert harness_main(dev, io_for(repo, live_settings=True, typed="0.50\n", provider=build)) == 0
    dev_folder = only_folder(repo.root, live=True)
    repo.commit(dev_folder)

    test = [*dev[:4], "test", *dev[5:]]
    build, fake = scripted(live_replies())
    typed = "test doc_extraction\n0.50\n"
    io_ = io_for(repo, live_settings=True, typed=typed, provider=build)
    assert harness_main(test, io_) == 0, capsys.readouterr().err
    entries = parse_ledger((repo.root / LEDGER_PATH).read_text("utf-8"))
    test_folder = next(
        p for p in (repo.root / EXTRACT_REPORTS / "v0.1").iterdir() if p.name.endswith("-test")
    )
    info = read_run(test_folder)
    assert len(entries) == 1
    entry = entries[0]
    assert (entry.dataset, entry.version, entry.run_id) == ("doc_extraction", "v0.1", info.run_id)
    assert (entry.freeze_sha256, entry.config_fingerprint) == (
        info.freeze_sha256,
        info.fingerprint_sha256,
    )
    assert info.fingerprint_sha256 == read_run(dev_folder).fingerprint_sha256
    assert [d.id for d in read_results(test_folder)] == ["s-2", "p-2"] and len(fake.calls) == 2

    repo.commit(test_folder, repo.root / LEDGER_PATH)
    build, fake = scripted(live_replies())
    io_ = io_for(repo, live_settings=True, typed=typed, provider=build)
    assert harness_main(test, io_) == 1
    assert "already has 1 test run(s)" in capsys.readouterr().err
    assert fake.calls == []


@dataclass
class LedgerWatch:
    """A paid stand-in that reads the test-run ledger as each call finds it, then answers as
    `inner`: nothing is sent anywhere."""

    inner: FakeProvider
    ledger: Path
    seen: list[list[str]] = field(default_factory=list)

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        text = self.ledger.read_text("utf-8") if self.ledger.is_file() else ""
        self.seen.append([entry.run_id for entry in parse_ledger(text)])
        return await self.inner.send(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def test_a_test_run_is_in_the_ledger_before_its_first_paid_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # "Ledgered first": a Ctrl-C or a crash after the first paid call still leaves the entry, so
    # the test split cannot be run again unnoticed (mutation audit U04).
    repo, dataset_dir = frozen_repo(tmp_path)
    repo.commit(dataset_dir)
    build, _ = scripted(live_replies())
    dev = ["--dataset", str(dataset_dir), "run", "--split", "dev", "--live", "--max-usd", "0.50"]
    assert harness_main(dev, io_for(repo, live_settings=True, typed="0.50\n", provider=build)) == 0
    repo.commit(only_folder(repo.root, live=True))

    watch = LedgerWatch(FakeProvider(live_replies()), repo.root / LEDGER_PATH)
    typed = "test doc_extraction\n0.50\n"
    io_ = io_for(repo, live_settings=True, typed=typed, provider=lambda settings: watch)
    assert harness_main([*dev[:4], "test", *dev[5:]], io_) == 0, capsys.readouterr().err

    [test_folder] = (repo.root / EXTRACT_REPORTS / "v0.1").glob("*-live-extract-test")
    assert watch.seen == [[test_folder.name], [test_folder.name]]  # s-2, then p-2


def test_the_trace_id_is_per_document_and_never_all_zeros() -> None:
    first = runs.trace_id_for("run-1", "s-1")
    assert len(first) == 32 and first != "0" * 32
    assert first != runs.trace_id_for("run-1", "p-1") != runs.trace_id_for("run-2", "s-1")
    assert json.dumps(first).isascii()


@pytest.mark.anyio
async def test_a_dropped_guess_is_scored_at_its_routed_level_never_as_a_correct_null() -> None:
    # The real core (signals, the stand-in router, the invariant check, "no quote, no value") on
    # a made-up letter whose patient name the fake model invents; the gold says it is not given.
    kind = referral()
    invented = with_changes(
        REFERRAL_ANSWER, patient_name=("Priya Shah", "Patient name: Priya Shah")
    )
    outcome, _ = await run_service(pdf(list(REFERRAL_LINES)), reply(kind, invented), kind=kind)
    gold = dict.fromkeys((spec.name for spec in kind.template.fields), None)
    document = EvalDocument(
        id="tonly-invented",
        business_id=kind.business_id,
        kind=kind.kind,
        split="dev",
        tags=("injection",),
        gold=gold,
        pdf=b"",
    )

    line = runs.scored_line(document, kind, outcome, None, 0.0, called=True)
    name = {f.field: f for f in line.fields}["patient_name"]
    # Stored: no value, and a null gold, so the stored value reads correct (field accuracy).
    assert (name.level, name.has_value, name.correct, name.unverified) == (None, False, True, True)
    # As routed: the guess was low, and it was wrong (correctness per level).
    assert (name.routed_level, name.proposal_correct) == ("low", False)
    # A verified value keeps one level, and its proposal is scored as stored.
    doctor = {f.field: f for f in line.fields}["referring_doctor"]
    assert (doctor.level, doctor.routed_level) == ("high", "high")
    assert doctor.proposal_correct is doctor.correct is False  # the gold here is null


def scored_fields(
    kind: KindConfig, outcome: ExtractionOutcome, gold: dict[str, str | None]
) -> dict[str, FieldLine]:
    """`outcome` scored against `gold` (every other field of the template null)."""
    full: dict[str, str | None] = {spec.name: None for spec in kind.template.fields}
    document = EvalDocument(
        id="tonly-case",
        business_id=kind.business_id,
        kind=kind.kind,
        split="dev",
        tags=(),
        gold=full | gold,
        pdf=b"",
    )
    line = runs.scored_line(document, kind, outcome, None, 0.0, called=True)
    return {f.field: f for f in line.fields}


@pytest.mark.anyio
async def test_an_identifier_or_an_enum_differing_only_in_case_is_wrong_and_text_is_not() -> None:
    # The real core on made-up documents; the gold differs from the stored value only in case.
    # Compared as text, the identifier and the enum would read correct (mutation audit U11, U12).
    kind = referral()
    outcome, _ = await run_service(
        pdf(list(REFERRAL_LINES)), reply(kind, REFERRAL_ANSWER), kind=kind
    )
    stored = {result.field: result.value for result in outcome.fields}
    assert (stored["patient_name"], stored["policy_number"]) == ("Asha Rao", "LHM-204117")
    fields = scored_fields(
        kind, outcome, {"patient_name": "ASHA RAO", "policy_number": "lhm-204117"}
    )
    assert (fields["patient_name"].type, fields["patient_name"].correct) == ("text", True)
    assert (fields["policy_number"].type, fields["policy_number"].correct) == ("identifier", False)

    kind = vaccination()
    outcome, _ = await run_service(
        pdf(list(VACCINATION_LINES)), reply(kind, VACCINATION_ANSWER), kind=kind
    )
    assert {result.field: result.value for result in outcome.fields}["species"] == "dog"
    species = scored_fields(kind, outcome, {"species": "DOG"})["species"]
    assert (species.type, species.correct) == ("enum", False)
