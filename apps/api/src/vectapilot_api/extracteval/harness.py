"""What the extraction harness's commands share (M6 PR 4): the outside world (`Io`), the refusals,
loading what a run reads, the settings rules, the test discipline and M6's spend lines.

**Dry or real** (scoring rules, "Dry or real; test once"):
- **Live** only with `--live --max-usd X` (at most $1.00, the cap typed again at a terminal:
  shesha's alone, agents never pass it), on the **frozen dataset** of the split, refused unless
  everything is synthetic (`synthetic_everywhere`: the dataset's manifest, every gold row, each
  business's corpus manifest and extraction business file), refused while H1 is not written
  (`h1_written`: probed before any paid call), with the paid provider in Settings. The
  extraction configuration and the corpus manifests must be inside the repository (the freeze
  does not pin them, so the commit must be able to name them), and run.json's
  `dataset_committed` says whether every dataset and configuration file read is the one in HEAD.
- **Dry** otherwise: the fake provider only, the **fixture documents** from `tests/`
  (`dryrun.py`) and never the dataset's documents or gold (only its manifest, for the version and
  the businesses' configuration folders), dev only, into the git-ignored `evals/reports/tmp/`;
  its summary starts "DRY RUN — plumbing only, never a result".

**The test discipline** (`check_test_run`): test needs a frozen dataset, a live run, a clean
working tree (so "committed in HEAD" names the code), the ledger committed as on disk, at most one
ledgered run per config unless `--test-rerun-reason`, a committed, complete live dev run with the
same fingerprint, and the person at a terminal typing `test doc_extraction`.

No database: the core runs in-process, as the route would, with each business's own
configuration (`ExtractionConfig.kind_config`, E8). Local, test and ci only (D6: synthetic
documents, never production).
"""

import hashlib
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Final, TextIO

from vectapilot_api.config import Settings
from vectapilot_api.evalrun.budget import CostCap
from vectapilot_api.evalrun.cli import load_settings
from vectapilot_api.evalrun.ledger import LedgerEntry, parse_ledger
from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalrun.spend import (
    HeadFile,
    HeadPaths,
    MilestoneSpend,
    SpendError,
    read_head_paths,
    read_head_ref,
    spend_refusal,
    spend_text,
)
from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, load_corpus_manifest
from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval.checks import DocExtractionDataset, business_pins, check_dataset
from vectapilot_api.extracteval.dryrun import DEFAULT_FIXTURES, Fixtures, load_fixtures
from vectapilot_api.extracteval.files import DEFAULT_DATASET, read_dataset
from vectapilot_api.extracteval.gold import (
    FREEZE_FILE,
    GOLD_FILE,
    MANIFEST_FILE,
    DocExtractionManifest,
    Tag,
    load_manifest,
)
from vectapilot_api.extracteval.metrics import H2_EXTRACT_METRICS, ExtractMetrics
from vectapilot_api.extracteval.runinfo import (
    EXTRACT_REPORTS,
    LEDGER_FILE,
    RUN_FILE,
    BusinessPins,
    SplitName,
    read_run,
)
from vectapilot_api.extracteval.spend import m6_spend
from vectapilot_api.extraction.files import (
    BUSINESSES,
    JSON_SUFFIX,
    TEMPLATES,
    ExtractionConfig,
    load_extraction_dir,
)
from vectapilot_api.extraction.routing import DocumentSignals, ReviewPolicy, route_fields
from vectapilot_api.extraction.service import Route
from vectapilot_api.gateway.gateway import build_provider
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.llm_cli import GitError, GitInfo, dataset_at_commit, read_git, read_head_file

PROG: Final = "python -m vectapilot_api.extracteval"
RUNBOOK: Final = "docs/plans/m6-pr-checks.md, PR 5"
TEST_WORDS: Final = "test doc_extraction"
LEDGER_PATH: Final = EXTRACT_REPORTS / LEDGER_FILE
"""`evals/reports/doc_extraction/test-runs.jsonl`, relative to the repository root."""
HARNESS_ENVIRONMENTS: Final = frozenset({"local", "test", "ci"})
DRY_REASON: Final = "scripted fake model over fixture documents from tests/"


class Refused(Exception):
    """The command cannot run as asked. The message names ids and rules, never text."""


@dataclass(frozen=True)
class Io:
    """What the commands read from outside: tests pass their own, so no test runs git, reads the
    shell's environment, waits for a terminal or builds a paid client."""

    git: Callable[[], GitInfo] = read_git
    head_file: HeadFile = read_head_file
    head_paths: HeadPaths = read_head_paths
    head_ref: Callable[[Path], str] = read_head_ref
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.perf_counter
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    stdout_is_terminal: Callable[[], bool] = field(default=lambda: sys.stdout.isatty())
    load_settings: Callable[[bool], Settings] = load_settings
    build_provider: Callable[[Settings], Provider] = build_provider
    """Live only; a dry run always uses the scripted `ScriptProvider`."""
    metrics: ExtractMetrics = H2_EXTRACT_METRICS
    route: Route = route_fields
    """H1. Tests pass stand-ins; the real one raises NotImplementedError until shesha writes it."""


@dataclass(frozen=True)
class EvalDocument:
    """One document as the harness runs it: from the frozen dataset (live) or a fixture (dry)."""

    id: str
    business_id: str
    kind: str
    split: SplitName
    tags: tuple[Tag, ...]
    gold: dict[str, str | None]
    pdf: bytes


@dataclass(frozen=True)
class Loaded:
    """What a run reads: the manifest and the configuration, and the documents of its split."""

    root: Path
    directory: Path
    corpus_dir: Path
    manifest: DocExtractionManifest
    extraction: ExtractionConfig
    documents: tuple[EvalDocument, ...]
    pins: dict[str, BusinessPins]
    freeze_sha256: str | None
    committed: bool
    """Live: every dataset file and every configuration file read (each business file, each
    enabled kind's template, each corpus manifest) is byte for byte the one in HEAD. Dry: False
    (not read)."""
    dataset: DocExtractionDataset | None = None
    """Live only."""
    fixtures: Fixtures | None = None
    """Dry only."""

    @property
    def version(self) -> str:
        return self.manifest.version


def _split(split: str) -> SplitName:
    if split not in ("dev", "test"):
        raise Refused("--split is dev or test")
    return "dev" if split == "dev" else "test"


def _dataset_documents(dataset: DocExtractionDataset, split: SplitName) -> tuple[EvalDocument, ...]:
    documents: list[EvalDocument] = []
    for row in dataset.rows:
        if row.split != split:
            continue
        pdf = dataset.documents[row.id].pdf
        if pdf is None:  # the check refuses this; kept so a type can never be None here
            raise Refused(f"document {row.id} has no PDF")
        documents.append(
            EvalDocument(
                id=row.id,
                business_id=row.business_id,
                kind=row.kind,
                split=row.split,
                tags=row.tags,
                gold=dict(row.fields),
                pdf=pdf,
            )
        )
    return tuple(documents)


def load_live(
    dataset_arg: Path | None,
    corpus_arg: Path | None,
    extraction_arg: Path | None,
    git: GitInfo,
    head_file: HeadFile,
    split: str,
) -> Loaded:
    """The frozen dataset's documents of `split`, checked (no placeholder: a TEST-ONLY copy can
    never reach a paid call or a committed folder)."""
    split_name = _split(split)
    directory = dataset_arg if dataset_arg is not None else git.root / DEFAULT_DATASET
    root = repo_root_of(directory)
    manifest = load_manifest(_read_manifest(directory))
    corpus_dir = corpus_arg or root / manifest.corpus
    extraction_dir = extraction_arg or root / manifest.extraction
    for flag, folder in (("--corpus", corpus_dir), ("--extraction", extraction_dir)):
        if not folder.resolve().is_relative_to(git.root.resolve()):
            raise Refused(
                f"{flag}: a live run reads its configuration from inside the repository, so the"
                " commit names it (the freeze does not pin it)"
            )
    dataset = read_dataset(directory, corpus_dir, extraction_dir)
    checked = check_dataset(dataset)
    if not checked.ok:
        for problem in checked.problems:
            print(f"PROBLEM  {problem}", file=sys.stderr)
        raise Refused(f"the dataset fails its check ({len(checked.problems)} problem(s))")
    freeze = directory / FREEZE_FILE
    if dataset.manifest.stage != "frozen" or not freeze.is_file():
        raise Refused(
            "a live run needs a frozen dataset: `freeze` once every slot is filled (README,"
            " 'Slots')"
        )
    documents = _dataset_documents(dataset, split_name)
    if not documents:
        raise Refused(f"no documents on the {split_name} split: nothing to run")
    read = [directory / name for name in _dataset_files(dataset)]
    read += config_files(dataset, extraction_dir, corpus_dir)
    files = {path: path.read_bytes() for path in read}
    return Loaded(
        root=git.root,
        directory=directory,
        corpus_dir=corpus_dir,
        manifest=dataset.manifest,
        extraction=dataset.extraction,
        documents=documents,
        pins=dataset.business_pins(),
        freeze_sha256=hashlib.sha256(freeze.read_bytes()).hexdigest(),
        committed=dataset_at_commit(git.root, files, head_file),
        dataset=dataset,
    )


def _read_manifest(directory: Path) -> str:
    path = directory / MANIFEST_FILE
    if not path.is_file():
        raise DatasetError(f"{MANIFEST_FILE} is missing in {directory.name}")
    return path.read_text("utf-8")


def config_files(
    dataset: DocExtractionDataset, extraction_dir: Path, corpus_dir: Path
) -> list[Path]:
    """Every configuration file a run reads for the dataset's businesses: the corpus manifest
    (business type, synthetic), the extraction business file and each enabled kind's template
    (`extraction.files`' layout)."""
    paths: list[Path] = []
    for business_id in dataset.manifest.businesses:
        paths.append(corpus_dir / business_id / CORPUS_MANIFEST_FILE)
        loaded = dataset.extraction.businesses.get(business_id)
        if loaded is None:  # the check refuses this before a run
            continue
        paths.append(extraction_dir / BUSINESSES / f"{business_id}{JSON_SUFFIX}")
        type_folder = extraction_dir / TEMPLATES / loaded.file.business_type.value
        paths += [type_folder / f"{kind}{JSON_SUFFIX}" for kind in sorted(loaded.kinds)]
    return paths


def _dataset_files(dataset: DocExtractionDataset) -> list[str]:
    """Every dataset file a live run reads, relative to the version folder."""
    names = [MANIFEST_FILE, GOLD_FILE, FREEZE_FILE]
    for document_id in sorted(dataset.documents):
        names += [f"documents/{document_id}.pdf.txt", f"documents/{document_id}.pdf"]
    return names


def load_dry(
    dataset_arg: Path | None,
    corpus_arg: Path | None,
    extraction_arg: Path | None,
    fixtures_arg: Path | None,
    git: GitInfo,
    split: str,
) -> Loaded:
    """The fixture documents (module docstring), with the dataset manifest's version and
    configuration folders. Dev only."""
    if _split(split) == "test":
        raise Refused("dry runs are refused on the test split: rehearse on dev")
    directory = dataset_arg if dataset_arg is not None else git.root / DEFAULT_DATASET
    root = repo_root_of(directory)
    manifest = load_manifest(_read_manifest(directory))
    corpus_dir = corpus_arg or root / manifest.corpus
    extraction = load_extraction_dir(extraction_arg or root / manifest.extraction, corpus_dir)
    fixtures = load_fixtures(fixtures_arg or git.root / DEFAULT_FIXTURES, extraction)
    documents = tuple(
        EvalDocument(
            id=f.id,
            business_id=f.business_id,
            kind=f.kind,
            split="dev",
            tags=f.tags,
            gold=dict(f.gold),
            pdf=fixtures.pdfs[f.id],
        )
        for f in fixtures.file.documents
    )
    unlisted = sorted({d.business_id for d in documents} - set(manifest.businesses))
    if unlisted:
        raise Refused(f"fixtures name businesses the manifest does not: {', '.join(unlisted)}")
    return Loaded(
        root=git.root,
        directory=directory,
        corpus_dir=corpus_dir,
        manifest=manifest,
        extraction=extraction,
        documents=documents,
        pins=business_pins(manifest.businesses, extraction),
        freeze_sha256=None,
        committed=False,
        fixtures=fixtures,
    )


def synthetic_everywhere(loaded: Loaded) -> bool:
    """Everything a run reads says synthetic: the dataset's manifest, every gold row (live) or the
    fixture file (dry), and for each business its corpus manifest and its extraction business file
    (their parsers refuse some of this already; checked again so the guard holds if one is
    relaxed)."""
    found = [loaded.manifest.synthetic is True]
    if loaded.dataset is not None:
        found += [row.synthetic is True for row in loaded.dataset.rows]
    if loaded.fixtures is not None:
        found.append(loaded.fixtures.file.synthetic is True)
    for business_id in loaded.manifest.businesses:
        path = loaded.corpus_dir / business_id / CORPUS_MANIFEST_FILE
        corpus = load_corpus_manifest(path.read_text("utf-8"), where=business_id)
        business = loaded.extraction.businesses.get(business_id)
        found += [corpus.synthetic is True, business is not None and business.file.synthetic]
    return all(found)


def check_settings(settings: Settings, *, live: bool) -> None:
    if settings.environment not in HARNESS_ENVIRONMENTS:
        raise Refused(
            "the extraction harness runs only in local, test or ci (synthetic documents, D6)"
        )
    if live and settings.llm_provider == "fake":
        raise Refused(
            f"--live needs the paid LLM provider in Settings, with its key in the process"
            f" environment only ({RUNBOOK}); a fake provider's numbers are never results"
        )
    if not live and settings.llm_provider != "fake":
        raise Refused("a run without --live uses the fake provider only: no paid call")


def h1_written(route: Route) -> bool:
    """False while `route` is the unwritten stub (it raises NotImplementedError on any input).
    Probed with a made-up one-field policy and no signals, before any paid call; anything else it
    does (a decision, another exception) counts as written: the service fails closed on it."""
    try:
        route(DocumentSignals(()), ReviewPolicy(fields=("probe",)))
    except NotImplementedError:
        return False
    except Exception:  # a written H1 that fails here still fails closed in the service (E3)
        return True
    return True


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def in_head(root: Path, path: Path, head_file: HeadFile) -> bool:
    """`path` is inside the repository and byte for byte the file in HEAD."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        return False
    return head_file(root, PurePosixPath(relative(root, path))) == resolved.read_bytes()


# ── Confirmation, the ledger, the test discipline ────────────────────────────────────────────


def confirm_test(io: Io) -> bool:
    """True only if `TEST_WORDS` is typed again exactly, with stdin and stdout at a terminal (a
    guard against mistakes: agents never run test)."""
    if not io.stdin.isatty() or not io.stdout_is_terminal():
        print("A test run is confirmed at a terminal (stdin and stdout).", file=sys.stderr)
        return False
    print(
        f"This uses the test split, once per config. Type '{TEST_WORDS}' exactly and press"
        " Enter; anything else cancels: ",
        end="",
        flush=True,
    )
    try:
        answer = io.stdin.readline()
    except KeyboardInterrupt:
        return False
    return answer.strip() == TEST_WORDS


def committed_ledger(io: Io, root: Path) -> list[LedgerEntry]:
    """The ledger, refused unless the file on disk is the one in HEAD (none in both is fine)."""
    path = root / LEDGER_PATH
    working = path.read_bytes() if path.is_file() else None
    if working != io.head_file(root, PurePosixPath(LEDGER_PATH.as_posix())):
        raise Refused(
            f"{LEDGER_PATH} is not as committed in HEAD: commit the test-run ledger (with the"
            " folders it names) first, so no earlier entry can be lost"
        )
    return parse_ledger(working.decode("utf-8")) if working is not None else []


def runs_of(
    entries: Sequence[LedgerEntry], dataset: str, version: str, freeze: str, fingerprint: str
) -> list[LedgerEntry]:
    """The ledgered test runs of one config on one frozen version, in order (the first is the one
    the note reports): keyed by dataset, version, freeze hash and fingerprint, as M4 and M5."""
    return [
        e
        for e in entries
        if (e.dataset, e.version, e.freeze_sha256, e.config_fingerprint)
        == (dataset, version, freeze, fingerprint)
    ]


def committed_live_dev_run(io: Io, root: Path, version: str, fingerprint: str) -> str | None:
    """A complete live dev run with this fingerprint whose run.json is in HEAD, or None."""
    for path in sorted((root / EXTRACT_REPORTS / version).glob(f"*-live-extract-dev/{RUN_FILE}")):
        try:
            info = read_run(path.parent)
        except ValueError:
            continue
        if (
            info.fingerprint_sha256 == fingerprint
            and info.status == "complete"
            and not info.dry_run
            and in_head(root, path, io.head_file)
        ):
            return info.run_id
    return None


def check_test_run(
    io: Io, git: GitInfo, loaded: Loaded, *, fingerprint: str, live: bool, reason: str | None
) -> int:
    """Refuse a test run the discipline does not allow (module docstring); the number of earlier
    ledgered runs of this config otherwise."""
    if not live or loaded.freeze_sha256 is None:
        raise Refused("dry runs are refused on the test split: rehearse on dev")
    if git.uncommitted_changes:
        raise Refused("a test run needs a clean working tree: commit first, so HEAD names the code")
    entries = committed_ledger(io, git.root)
    name = loaded.manifest.dataset
    earlier = runs_of(entries, name, loaded.version, loaded.freeze_sha256, fingerprint)
    if earlier and reason is None:
        raise Refused(
            f"this config already has {len(earlier)} test run(s) on this frozen version (first:"
            f" {earlier[0].run_id}): the test split runs once per config; a rerun needs"
            " --test-rerun-reason, recorded in the ledger"
        )
    if committed_live_dev_run(io, git.root, loaded.version, fingerprint) is None:
        raise Refused(
            "no complete live dev run with this fingerprint is committed in HEAD: run dev first"
        )
    if not confirm_test(io):
        raise Refused("not confirmed")
    return len(earlier)


# ── M6's spend ───────────────────────────────────────────────────────────────────────────────


def spend_lines(io: Io, git: GitInfo, why: str, cap: Decimal) -> str:
    """Count M6's spend from HEAD and refuse a cap above what is left (`spend.py`)."""
    try:
        counted: MilestoneSpend = m6_spend(git.root, io.head_paths, io.head_file)
    except (SpendError, GitError) as error:
        raise Refused(f"M6's spend cannot be counted: {error}") from None
    try:
        head = f"HEAD ({io.head_ref(git.root)})"
    except GitError:
        head = "HEAD"
    counted = MilestoneSpend(counted.counted, counted.uncommitted, counted.rules, head)
    refusal = spend_refusal(counted, cap)
    if refusal is not None:
        print(spend_text(counted, why))
        raise Refused(refusal)
    return spend_text(counted, why)


def cost_info(cap: CostCap) -> CostInfo:
    return CostInfo(
        cap_usd=cap.cap,
        embedding_usd=cap.embedding_usd,
        llm_usd=cap.llm_usd,
        total_usd=cap.total,
        llm_cost_complete=cap.llm_cost_complete,
        embedding_requests=cap.embedding_requests,
        llm_calls=cap.llm_calls,
    )
