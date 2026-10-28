"""`uv run python -m vectapilot_api.evalrun <command>`: the eval harness (M2 PR 8).

    run --split dev                    # dry run (default): fake embedder, canned answers, $0
    run --split dev --retrieval-mode hybrid   # M3: both legs and RRF (default: Settings' mode)
    run --split dev --live --max-usd X # shesha only, in a terminal without an agent; never an agent
    ingest [--fresh]                   # dry only: ingest the corpus with the fake embedder
    summarize RUN_DIR                  # recompute summary.md/json from a run folder (labels too)
    label-sheet RUN_DIR                # print questions, answers and cited text (stdout only)
    faithfulness-template RUN_DIR      # write faithfulness_labels.jsonl (null labels, no text)
    compare RUN_A RUN_B                # two runs of one chunk config, paired by question (stdout)

**`run` is the one command** of PR 9's live run: it registers the eval businesses if needed
(owner URL), ingests the corpus through the product (`KbService`, idempotent), checks the stored
chunks against the corpus at the chunk config, then answers and scores each question of the split
through `AnswerService`, and writes the run folder (`report.py`). Every number in its summary is
recomputed from the folder's files.

**Live runs are human-only** (ADR 0006/0007): `--live` needs `--max-usd X` (0 < X <= 1.00), Settings
with the paid providers (`llm_provider` anthropic and `embeddings_provider` voyage, keys in the
process environment only), and the cap typed again at a terminal (M1's `confirmed`: stdin and
stdout both at a terminal). Every paid request's worst case, embeddings included, is reserved
before it is sent (`budget.py`). Agents never pass `--live`. The API server's `api_allow_paid_llm`
opt-in plays no part: the harness calls `AnswerService` directly, under its own cap.

**What `run` refuses** (exit 1, before anything is sent):
- `environment: production`, always;
- a dataset that fails `check` (placeholders are allowed in dry runs only, for TEST-ONLY copies);
- `--live` unless the dataset manifest and every corpus manifest say `synthetic: true` (decided
  2026-10-22): a live folder commits the model's answers, so real pilot data needs its own
  decision first. Today the manifests' parsers refuse anything else; this guard holds when they
  are relaxed;
- a live run on a dataset that is not frozen (tuning before the split exists would tune on future
  test questions); a dry run on an unfrozen dataset runs every question, labelled `unassigned`;
- a dry run whose Settings name a paid provider (`harness.check_run` refuses it again);
- in hybrid mode, a search k above `rag_candidate_k` (`report.SEARCH_K_ABOVE_CANDIDATE_K`: the
  API refuses such Settings at startup, so the harness never measures them; M3 PR 5);
- a live run of M3 (hybrid, or `--m3-spend`) whose cap is above what is left of M3's $5.00 (a
  live folder without run.json counts at the $1.00 per-run ceiling until its spend is recorded),
  or while one of M3's live folders is not committed as it is on disk (`spend.py`, D15 (iii));
- a question whose text `POST /v1/answer` would refuse (named by id);
- `--split test` unless it is live, the dataset is frozen, a complete live **dev** run with the same
  config fingerprint is committed (its run.json byte for byte in HEAD), the test-run ledger
  (`evals/reports/<dataset>/test-runs.jsonl`) is committed (byte for byte the one in HEAD, so no
  earlier entry can be lost), and it has no entry for that fingerprint, or the run names
  `--test-rerun-reason`. Each test run is appended to the ledger before its first paid call, and
  run.json records how many entries came before it (this config, this frozen version), so "test
  runs once per reported config" is auditable in git;
- a metric k above the search k, or a search k below 1 (D6, asserted again in `RunConfig` and in
  run.json's `ConfigInfo`).

**The retrieval mode** (M3 PR 5) is the run's own: `--retrieval-mode vector|hybrid`, like
`--min-score`, defaults to Settings' `rag_retrieval_mode`, and the harness builds its search in
that mode whatever Settings say (PR 4's refusal of a hybrid setting is gone). A hybrid run records
run.json's `config.retrieval` block (mode, `rag_candidate_k`, `rag_rrf_k`, the language map's
sha256), part of the config fingerprint; a vector run has none, so its fingerprint stays M2's. The
eval namespaces depend on the embedding model and chunk config only, so a hybrid run reuses the
vector runs' namespaces and, once they are ingested, embeds one query per question and no
document. The plan says so only when a complete live vector dev run committed in HEAD used the
same namespaces (`committed_vector_dev_run`), and prints a WARNING with the ingest's worst case
otherwise. Its "M3 spend so far" line names the branch and commit whose HEAD it read, and says
that every M3 live run is made, and its folder committed, on `m3/close-out` (decided 2026-10-27).

**`compare`** (M3 PR 6, `compare.py`) reads two run folders and prints, never writes: redirect
stdout to keep the report. It reads git only on the test split (the ledger as committed in HEAD).

**Labelling** (M3 PR 7). `label-sheet` prints, to stdout only, each question, its reply and, under
each shown answer, the text of every chunk it cites, rebuilt from the corpus at run.json's chunk
config once freeze.json, the questions and the corpus are shown to be the run's (`cited.py`; a
mismatch refuses the whole sheet, nothing printed, and so does a citation of a chunk the prompt did
not hold or of a document no customer may see, a leak signal). It writes no file and logs nothing.
`faithfulness-template` writes the folder's `faithfulness_labels.jsonl` (ids and nulls, one line per
shown answer) and refuses when it exists; `run` does not write it, so a run folder holds M2's
files. The labels themselves are shesha's.

Everything the run writes after its calls (run.json, labels, the summary) can still fail; it then
prints "the run FAILED, no summary written", where the folder is and what was spent, never
"nothing was sent".

Exit codes: 0 complete · 1 failed or refused · 2 invalid arguments · 3 partial · 4 aborted on a
cross-business leak or a hit no customer may see, also when writing the folder failed afterwards.
A stop rule tripped by the last question leaves no question unrun: `complete`, exit 0, and never
for the note (decided 2026-10-22, as M1). The status line in summary.md decides, never the code.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, Final, TextIO

from pydantic import ValidationError

from vectapilot_api.answer.prompt import PROMPT_VERSION
from vectapilot_api.answer.service import ANSWER_ALIAS
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.runner import EMBEDDING_CALL
from vectapilot_api.evalrun.budget import CostCap, record_for, usd
from vectapilot_api.evalrun.cited import check_corpus, cited_texts
from vectapilot_api.evalrun.compare import CompareRefused, compare, load_run, render_comparison
from vectapilot_api.evalrun.harness import (
    HarnessRefused,
    RunConfig,
    RunOutcome,
    Target,
    api_questions,
    execute,
)
from vectapilot_api.evalrun.ingest import IngestRefused
from vectapilot_api.evalrun.intervals import DEFAULT_CONFIDENCE
from vectapilot_api.evalrun.ledger import (
    LEDGER_FILE,
    LedgerEntry,
    append_ledger,
    parse_ledger,
    read_ledger,
)
from vectapilot_api.evalrun.namespace import NamespaceError, namespaces
from vectapilot_api.evalrun.plan import worst_case
from vectapilot_api.evalrun.report import (
    ANSWER_LOG_FILE,
    ANSWERS_FILE,
    BOOTSTRAP_MIN_N,
    DEFAULT_BOOTSTRAP_SEED,
    DEFAULT_RESAMPLES,
    EMBEDDING_LOG_FILE,
    FAITHFULNESS_LABELS_FILE,
    LABELS_FILE,
    LLM_LOG_FILE,
    RATE_MIN_N,
    RESULTS_FILE,
    RUN_FILE,
    SEARCH_K_ABOVE_CANDIDATE_K,
    ConfigInfo,
    CostInfo,
    IngestInfo,
    RetrievalInfo,
    RunInfo,
    SplitName,
    SummaryError,
    config_fingerprint,
    faithfulness_template,
    label_sheet_header,
    label_template,
    language_map_sha256,
    m3_spend_reason,
    read_answers,
    read_results,
    read_run,
    render_label_sheet,
    retrieval_text,
    summarize,
    write_summary,
)
from vectapilot_api.evalrun.scoring import (
    H2_METRICS,
    AnswerLine,
    MetricFunctions,
    QuestionResult,
    not_run,
)
from vectapilot_api.evalrun.spend import (
    M3_CEILING_USD,
    M3_LIVE_BRANCH,
    RECORDED_SPEND_FILE,
    HeadPaths,
    M3Spend,
    SpendError,
    m3_spend,
    read_head_paths,
    read_head_ref,
    spend_refusal,
    spend_text,
)
from vectapilot_api.evalset.checks import check_dataset, select_split
from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, CorpusError, review_problems
from vectapilot_api.evalset.files import DEFAULT_DATASET, Dataset, read_dataset, repo_root_of
from vectapilot_api.evalset.questions import (
    FREEZE_FILE,
    MANIFEST_FILE,
    QUESTIONS_FILE,
    DatasetError,
    EvalQuestion,
)
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.llm_cli import (
    _KEEP_OR_DELETE,
    DRY_RUN_REPORTS_DIR,
    REPORTS_DIR,
    GitError,
    GitInfo,
    captured,
    confirmed,
    dataset_at_commit,
    read_git,
    read_head_file,
    stdout_is_terminal,
)
from vectapilot_api.measure.budget import MAX_USD_CEILING
from vectapilot_api.measure.records import RecordError, parse_llm_call_lines
from vectapilot_api.retrieval.search import RetrievalConfig
from vectapilot_api.retrieval.types import RetrievalMode

PROG: Final = "python -m vectapilot_api.evalrun"
RUNBOOK: Final = "docs/learning/m2-eval-run.md"

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_PARTIAL: Final = 3
EXIT_LEAK: Final = 4

EMBEDDINGS_LOGGER: Final = "vectapilot_api.embeddings"
ANSWER_LOGGER: Final = "vectapilot_api.answer"
_RERUN_REASON: Final = r"^[a-z0-9]+(-[a-z0-9]+)*$"
M3_KEEP: Final = (
    f"this run counts toward M3's {usd(M3_CEILING_USD)}: commit this folder on {M3_LIVE_BRANCH},"
    f" never delete it (without run.json it counts at the {usd(MAX_USD_CEILING)} per-run ceiling"
    f" until its spend is recorded in {RECORDED_SPEND_FILE}; runbook §6)"
)
"""The failure line's advice for a live run of M3, instead of M1's "commit it as evidence only,
or delete it" (decided 2026-10-27, shesha: a live folder without run.json counts at the ceiling)."""


class Refused(Exception):
    """The command cannot run as asked. The message names ids and rules, never text."""


# ── The outside world (tests pass their own) ─────────────────────────────────────────────────

Execute = Callable[..., Coroutine[Any, Any, RunOutcome]]
"""`harness.execute`'s shape: tests pass a stand-in that needs no Postgres."""


def load_settings(live: bool) -> Settings:
    """Settings for a run. A dry run is always the fake embedder and the fake model, no key."""
    if live:
        return Settings()
    return Settings(
        llm_provider="fake",
        anthropic_api_key=None,
        embeddings_provider="fake",
        embeddings_model=None,
        voyage_api_key=None,
    )


def owner_url() -> str | None:
    """The owner's database URL (only registering an eval business needs it), if configured."""
    try:
        return str(MigrationSettings().migration_database_url.get_secret_value())
    except ValidationError:
        return None


@dataclass(frozen=True)
class Io:
    load_settings: Callable[[bool], Settings]
    owner_url: Callable[[], str | None]
    git: Callable[[], GitInfo]
    head_file: Callable[[Path, PurePosixPath], bytes | None]
    now: Callable[[], datetime]
    stdin: TextIO
    stdout_is_terminal: Callable[[], bool]
    execute: Execute
    metrics: MetricFunctions = H2_METRICS
    """H2's functions; CLI tests pass stand-ins that return prepared values."""
    head_paths: HeadPaths = read_head_paths
    """The files under a directory as committed in HEAD (M3's spend, `spend.py`)."""
    head_ref: Callable[[Path], str] = read_head_ref
    """The branch and short commit HEAD is at, named in the plan's M3 spend line (`spend.py`)."""


def default_io() -> Io:
    return Io(
        load_settings=load_settings,
        owner_url=owner_url,
        git=read_git,
        head_file=read_head_file,
        now=lambda: datetime.now(UTC),
        stdin=sys.stdin,
        stdout_is_terminal=stdout_is_terminal,
        execute=execute,
    )


# ── Arguments ────────────────────────────────────────────────────────────────────────────────


_USD_TEXT: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
"""`--max-usd` as plain digits (M1's rule): no sign, exponent, NaN or infinity."""


def cap_value(text: str) -> Decimal | None:
    """The cap `text` names, or None when it is not plain digits above 0 and at most the
    ceiling."""
    if _USD_TEXT.fullmatch(text) is None:
        return None
    value = Decimal(text)
    return value if 0 < value <= MAX_USD_CEILING else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="The eval harness: ingest, answer and score eval v0, write a run folder."
        " Without --live everything is a DRY RUN (fake embedder, canned answers, $0).",
        epilog=f"--live is shesha's alone (runbook: {RUNBOOK}); agents never pass it.",
    )
    parser.add_argument(
        "--dataset", type=Path, help=f"a version folder (default {DEFAULT_DATASET})"
    )
    parser.add_argument("--corpus", type=Path, help="the corpus folder (default: the manifest's)")
    commands = parser.add_subparsers(dest="command", required=True)

    def chunking(command: argparse.ArgumentParser) -> None:
        defaults = ChunkingConfig()
        command.add_argument("--max-chars", type=int, default=defaults.max_chars)
        command.add_argument("--overlap", type=int, default=defaults.overlap_chars)
        command.add_argument(
            "--fresh",
            action="store_true",
            help="first delete the documents of this config's eval businesses (never others)",
        )

    run = commands.add_parser("run", help="ingest, answer and score one split; write a run folder")
    run.add_argument("--split", choices=("dev", "test"), required=True)
    chunking(run)
    run.add_argument("--search-k", type=int, help="k passed to the search (default rag_context_k)")
    run.add_argument("--metric-k", type=int, default=5, help="hit@k and MRR@k's k (default 5)")
    run.add_argument("--min-score", type=float, help="rag_min_score (default: Settings)")
    run.add_argument(
        "--retrieval-mode",
        choices=("vector", "hybrid"),
        help="vector (M2's path) or hybrid (both legs and RRF; M3); default: rag_retrieval_mode",
    )
    run.add_argument(
        "--m3-spend",
        action="store_true",
        help="count this vector run toward M3's $5.00 (D3's re-run); hybrid runs always count",
    )
    run.add_argument("--test-rerun-reason", help="a slug, recorded in the test-run ledger")
    run.add_argument(
        "--live", action="store_true", help="paid calls (shesha only; needs --max-usd)"
    )
    run.add_argument("--max-usd", help=f"the run's cap in US dollars, 0 < X <= {MAX_USD_CEILING}")
    ingest = commands.add_parser("ingest", help="dry only: ingest and check the corpus")
    chunking(ingest)
    for name in ("summarize", "label-sheet"):
        command = commands.add_parser(name)
        command.add_argument("run_dir", type=Path)
    template = commands.add_parser(
        "faithfulness-template",
        help="write the run folder's faithfulness_labels.jsonl: one null line per shown answer",
    )
    template.add_argument("run_dir", type=Path)
    compare = commands.add_parser(
        "compare",
        help="two runs of one chunk config, paired by question; prints, never writes",
    )
    compare.add_argument("run_a", type=Path, help="run A's folder")
    compare.add_argument("run_b", type=Path, help="run B's folder")
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = _parser()
    args = parser.parse_args(argv)
    if getattr(args, "live", False) and args.max_usd is None:
        parser.error("--live requires --max-usd")
    args.max_usd_text = getattr(args, "max_usd", None)
    if args.max_usd_text is not None:
        args.max_usd = cap_value(args.max_usd_text)
        if args.max_usd is None:
            parser.error(f"--max-usd must be plain digits above 0 and at most {MAX_USD_CEILING}")
    reason = getattr(args, "test_rerun_reason", None)
    if reason is not None and (re.fullmatch(_RERUN_REASON, reason) is None or len(reason) > 80):
        parser.error("--test-rerun-reason must be a short lowercase slug (a-z, 0-9, -)")
    return args


# ── The test-split discipline (the ledger itself: `ledger.py`) ───────────────────────────────


def committed_dev_run(
    root: Path,
    reports: Path,
    fingerprint: str,
    head_file: Callable[[Path, PurePosixPath], bytes | None],
) -> str | None:
    """The id of a complete live dev run with this config fingerprint whose run.json is in HEAD
    byte for byte, or None."""
    for path in sorted(reports.glob(f"*-live-dev/{RUN_FILE}")):
        content = path.read_bytes()
        try:
            info = RunInfo.model_validate_json(content)
        except ValidationError:
            continue
        if (
            info.config_fingerprint == fingerprint
            and info.status == "complete"
            and info.split == "dev"
            and not info.dry_run
        ):
            relative = PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
            if head_file(root, relative) == content:
                return info.run_id
    return None


def committed_vector_dev_run(
    root: Path,
    reports: Path,
    mapping: Mapping[str, str],
    head_file: Callable[[Path, PurePosixPath], bytes | None],
) -> str | None:
    """The id of a complete live vector dev run whose `namespaces` are `mapping` and whose
    run.json is in HEAD byte for byte, or None (read-only, as `committed_dev_run`): the evidence
    behind a hybrid plan's "no document embedding expected" (review of PR 5, ananya A1)."""
    for path in sorted(reports.glob(f"*-live-dev/{RUN_FILE}")):
        content = path.read_bytes()
        try:
            info = RunInfo.model_validate_json(content)
        except ValidationError:
            continue
        if (
            info.namespaces == dict(mapping)
            and info.config.retrieval is None
            and info.status == "complete"
            and info.split == "dev"
            and not info.dry_run
        ):
            relative = PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
            if head_file(root, relative) == content:
                return info.run_id
    return None


# ── run ──────────────────────────────────────────────────────────────────────────────────────


def _select(dataset: Dataset, split: str, live: bool) -> tuple[SplitName, tuple[EvalQuestion, ...]]:
    frozen = dataset.manifest.stage == "frozen" and dataset.freeze is not None
    if not frozen:
        if live:
            raise Refused(
                "a live run needs a frozen dataset: tuning before the split exists would tune on"
                " future test questions (README, 'Split rule and freeze')"
            )
        if split == "test":
            raise Refused("the dataset is not frozen: it has no test split")
        return "unassigned", dataset.questions
    if split == "test":
        if not live:
            raise Refused("dry runs are refused on the test split: rehearse on dev")
        return "test", select_split(dataset.questions, "test")
    return "dev", select_split(dataset.questions, "dev")


def _dataset_files(dataset: Dataset, corpus_dir: Path) -> dict[Path, bytes]:
    """Every file the run reads: the dataset's and each business's corpus manifest and files."""
    names = [MANIFEST_FILE, QUESTIONS_FILE] + ([FREEZE_FILE] if dataset.freeze else [])
    files = {dataset.directory / name: (dataset.directory / name).read_bytes() for name in names}
    for business_id, manifest in dataset.corpus.manifests.items():
        folder = corpus_dir / business_id
        for name in [CORPUS_MANIFEST_FILE, *(source.file for source in manifest.sources)]:
            files[folder / name] = (folder / name).read_bytes()
    return files


def _run_dir(root: Path, dataset: Dataset, started: datetime, live: bool, split: str) -> Path:
    base = root / (REPORTS_DIR if live else DRY_RUN_REPORTS_DIR)
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    manifest = dataset.manifest
    return (
        base / manifest.dataset / manifest.version / f"{stamp}-{'live' if live else 'fake'}-{split}"
    )


@dataclass
class Sinks:
    """results.jsonl and answers.jsonl, written through line by line as the run goes."""

    results: TextIO
    answers: TextIO
    written: list[QuestionResult]

    def __call__(self, result: QuestionResult, answer: AnswerLine | None) -> None:
        self.written.append(result)
        self.results.write(result.model_dump_json() + "\n")
        self.results.flush()
        if answer is not None:
            self.answers.write(answer.model_dump_json() + "\n")
            self.answers.flush()


def _answered_models(
    llm_lines: Sequence[str], embedding_lines: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The embedding and answer model ids the providers reported, from the captured records."""
    embedding: set[str] = set()
    for line in embedding_lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        record = payload.get(EMBEDDING_CALL) if isinstance(payload, dict) else None
        if isinstance(record, dict) and isinstance(record.get("model_answered"), str):
            embedding.add(record["model_answered"])
    try:
        llm = {r.model_answered for r in parse_llm_call_lines(llm_lines) if r.model_answered}
    except RecordError:
        llm = set()
    return tuple(sorted(embedding)), tuple(sorted(llm))


_PROVISIONAL_TIME: Final = datetime(2000, 1, 1, tzinfo=UTC)


def _provisional(fixed: dict[str, Any]) -> RunInfo:
    """run.json with the fields only the run can fill set to placeholders: validates every fixed
    field before the cap is asked for, so a bad field fails before anything is sent."""
    return RunInfo.model_validate(
        fixed
        | {
            "run_id": "provisional",
            "status": "complete",
            "stop": None,
            "started_at": _PROVISIONAL_TIME,
            "ended_at": _PROVISIONAL_TIME,
            "embedding_models_answered": (),
            "answer_models_answered": (),
            "cost": _cost(CostCap(cap=None)),
            "ingest": (),
        }
    )


def _cost(cap: CostCap) -> CostInfo:
    return CostInfo(
        cap_usd=cap.cap,
        embedding_usd=cap.embedding_usd,
        llm_usd=cap.llm_usd,
        total_usd=cap.total,
        llm_cost_complete=cap.llm_cost_complete,
        embedding_requests=cap.embedding_requests,
        llm_calls=cap.llm_calls,
    )


def run_command(args: argparse.Namespace, io: Io) -> int:
    live: bool = args.live
    git = io.git()
    dataset_dir: Path = args.dataset or git.root / DEFAULT_DATASET
    dataset = read_dataset(dataset_dir, args.corpus)
    corpus_dir: Path = args.corpus or repo_root_of(dataset_dir) / dataset.manifest.corpus
    checked = check_dataset(
        dataset.manifest,
        dataset.questions,
        dataset.corpus,
        freeze=dataset.freeze,
        previous_freeze=dataset.previous_freeze,
        allow_placeholders=not live,
    )
    if not checked.ok:
        for problem in checked.problems:
            print(f"PROBLEM  {problem}", file=sys.stderr)
        raise Refused(f"the dataset fails its check ({len(checked.problems)} problem(s))")
    corpus_synthetic = all(m.synthetic for m in dataset.corpus.manifests.values())
    if live and not (dataset.manifest.synthetic and corpus_synthetic):
        raise Refused(
            "--live needs data marked synthetic in the dataset manifest and every corpus manifest"
            f" (dataset synthetic: {str(dataset.manifest.synthetic).lower()}, corpus manifests"
            f" synthetic: {str(corpus_synthetic).lower()}): a live run commits the model's answers"
            " (answers.jsonl), so real data needs its own decision first (decided 2026-10-22)"
        )
    split, questions = _select(dataset, args.split, live)
    if not questions:
        raise Refused(f"no questions on the {split} split: nothing to run")
    try:
        api_questions(questions)
    except HarnessRefused as refused:
        raise Refused(str(refused)) from None
    settings = io.load_settings(live)
    _check_settings(settings, live)
    mode: RetrievalMode = (
        settings.rag_retrieval_mode if args.retrieval_mode is None else args.retrieval_mode
    )
    search_k = settings.rag_context_k if args.search_k is None else args.search_k
    if mode == "hybrid" and search_k > settings.rag_candidate_k:
        raise Refused(
            f"--search-k {search_k} is above rag_candidate_k {settings.rag_candidate_k}:"
            f" {SEARCH_K_ABOVE_CANDIDATE_K}"
        )
    config = RunConfig(
        chunking=ChunkingConfig(max_chars=args.max_chars, overlap_chars=args.overlap),
        search_k=search_k,
        metric_k=args.metric_k,
        rag_min_score=settings.rag_min_score if args.min_score is None else args.min_score,
        weak_evidence_outcome=settings.rag_weak_evidence_outcome,
        llm_max_retries=settings.llm_max_retries,
        llm_max_repairs=settings.llm_max_repairs,
        retrieval=RetrievalConfig(
            mode=mode, candidate_k=settings.rag_candidate_k, rrf_k=settings.rag_rrf_k
        ),
    )
    embedding_catalog = load_embedding_catalog()
    spec = embedding_catalog.resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    catalog = load_catalog()
    answer_model = catalog.aliases[ANSWER_ALIAS].model
    mapping = namespaces(
        dataset.manifest.businesses, embedding_model=spec.model, chunking=config.chunking
    )
    freeze_path = dataset.directory / FREEZE_FILE
    freeze_sha = (
        hashlib.sha256(freeze_path.read_bytes()).hexdigest() if freeze_path.is_file() else None
    )
    config_info = ConfigInfo(
        max_chars=config.chunking.max_chars,
        overlap_chars=config.chunking.overlap_chars,
        search_k=config.search_k,
        metric_k=config.metric_k,
        rag_min_score=config.rag_min_score,
        weak_evidence_outcome=config.weak_evidence_outcome,
        llm_max_retries=config.llm_max_retries,
        llm_max_repairs=config.llm_max_repairs,
        retrieval=_retrieval_info(config),
    )
    fingerprint = config_fingerprint(
        dataset=dataset.manifest.dataset,
        version=dataset.manifest.version,
        freeze_sha256=freeze_sha,
        embedding_model=spec.model,
        answer_model=answer_model,
        catalog_version=catalog.catalog_version,
        prompt_version=PROMPT_VERSION,
        config=config_info,
    )
    committed = dataset_at_commit(git.root, _dataset_files(dataset, corpus_dir), io.head_file)
    ledger = git.root / REPORTS_DIR / dataset.manifest.dataset / LEDGER_FILE
    before_config = before_freeze = 0
    if split == "test":
        before_config, before_freeze = _check_test_run(
            args, io, git, dataset, fingerprint, freeze_sha, ledger
        )
    fixed: dict[str, Any] = {
        "dry_run": not live,
        "environment": settings.environment,
        "dataset": dataset.manifest.dataset,
        "dataset_version": dataset.manifest.version,
        "dataset_stage": dataset.manifest.stage,
        "dataset_synthetic": dataset.manifest.synthetic,
        "corpus_synthetic": corpus_synthetic,
        "freeze_sha256": freeze_sha,
        "dataset_committed": committed,
        "corpus_review_pending": len(review_problems(dataset.corpus, dataset.manifest.businesses)),
        "split": split,
        "questions_selected": len(questions),
        "commit": git.commit,
        "uncommitted_changes": git.uncommitted_changes,
        "embeddings_provider": settings.embeddings_provider,
        "embedding_model": spec.model,
        "llm_provider": settings.llm_provider,
        "answer_alias": ANSWER_ALIAS,
        "answer_model": answer_model,
        "catalog_version": catalog.catalog_version,
        "prompt_version": PROMPT_VERSION,
        "config": config_info,
        "config_fingerprint": fingerprint,
        "namespaces": dict(mapping),
        "confidence": str(DEFAULT_CONFIDENCE),
        "bootstrap_seed": DEFAULT_BOOTSTRAP_SEED,  # fixed before any run: never a flag
        "bootstrap_resamples": DEFAULT_RESAMPLES,
        "rate_min_n": RATE_MIN_N,
        "bootstrap_min_n": BOOTSTRAP_MIN_N,
        "test_rerun_reason": args.test_rerun_reason,
        "test_runs_before_config": before_config,
        "test_runs_before_freeze": before_freeze,
        "m3_spend": args.m3_spend,
    }
    m3_reason = m3_spend_reason(_provisional(fixed))
    spend = _m3_spend(io, git, m3_reason) if live or m3_reason is not None else None
    bound = worst_case(
        dataset.corpus,
        dataset.manifest.businesses,
        questions,
        config.chunking,
        search_k=config.search_k,
        embedding_model=spec.model,
        embedding_price=embedding_catalog.price(spec.model),
        catalog=catalog,
        answer_alias=ANSWER_ALIAS,
        max_retries=config.llm_max_retries,
        max_repairs=config.llm_max_repairs,
    )
    cap = CostCap(cap=args.max_usd)
    documents: str | None = None  # with --fresh, its own line says what is embedded again
    if config.hybrid and not args.fresh:
        reports = git.root / REPORTS_DIR / dataset.manifest.dataset / dataset.manifest.version
        vector_run = committed_vector_dev_run(git.root, reports, mapping, io.head_file)
        documents = _documents_line(mapping, vector_run, bound.ingest_usd)
    print(
        _plan_text(
            dataset,
            split,
            questions,
            config,
            settings,
            spec.model,
            answer_model,
            live,
            documents=documents,
        )
        + (
            f"\n--fresh: deletes the documents of {len(mapping)} eval business(es) of this"
            " config first, then embeds them again" + (" (paid, under the cap)." if live else ".")
            if args.fresh
            else ""
        )
        + f"\nWorst case at list prices, every retry and repair included: ingest"
        f" {usd(bound.ingest_usd)} ({bound.ingest_tokens} tokens bound, embedded only if not"
        f" stored yet) · question embeddings {usd(bound.question_embedding_usd)} · answers"
        f" {usd(bound.answer_usd)} (largest single call {usd(bound.largest_answer_usd)}) · whole"
        f" plan {usd(bound.total)}."
        + (
            f"\nCap: {usd(args.max_usd)}; each paid request is reserved first, and the run stops"
            " before one that would cross it."
            if args.max_usd is not None
            else "\nCap: none (dry run)."
        )
        + _spend_lines(spend, m3_reason, live),
        flush=True,
    )
    if live and spend is not None and m3_reason is not None:
        refusal = spend_refusal(spend, args.max_usd)
        if refusal is not None:
            raise Refused(refusal)
    if live and not confirmed(
        io.stdin, args.max_usd_text, stdout_is_terminal=io.stdout_is_terminal()
    ):
        raise Refused("not confirmed")
    started = io.now()
    out = _run_dir(git.root, dataset, started, live, split)
    out.mkdir(parents=True, exist_ok=False)  # never overwrite a run
    if split == "test":
        append_ledger(
            ledger,
            LedgerEntry(
                dataset=dataset.manifest.dataset,
                version=dataset.manifest.version,
                freeze_sha256=freeze_sha,
                config_fingerprint=fingerprint,
                run_id=out.name,
                commit=git.commit,
                started_at=started,
                rerun_reason=args.test_rerun_reason,
            ),
        )
    outcome, sinks, llm_lines, embedding_lines = _execute(
        io, args, dataset, corpus_dir, questions, mapping, settings, config, cap, out, live
    )
    # From here on the run has made its calls: a failure must never say "nothing was sent".
    try:
        results = _complete_results(questions, mapping, sinks.written)
        if len(results) != len(sinks.written):
            with (out / RESULTS_FILE).open("a", encoding="utf-8") as tail:
                for result in results[len(sinks.written) :]:
                    tail.write(result.model_dump_json() + "\n")
        embedding_answered, llm_answered = _answered_models(llm_lines, embedding_lines)
        info = RunInfo.model_validate(
            fixed
            | {
                "run_id": out.name,
                "status": outcome.status,
                "stop": outcome.stop,
                "problems": outcome.problems,
                "started_at": started,
                "ended_at": io.now(),
                "embedding_models_answered": embedding_answered,
                "answer_models_answered": llm_answered,
                "cost": _cost(cap),
                "ingest": tuple(
                    IngestInfo(
                        business_id=c.business_id,
                        eval_business_id=c.eval_business_id,
                        documents=c.documents,
                        created=c.created,
                        chunks_written=c.chunks_written,
                        retired_versions=c.retired_versions,
                    )
                    for c in outcome.ingest
                ),
            }
        )
        (out / RUN_FILE).write_text(info.model_dump_json(indent=2) + "\n", encoding="utf-8")
        with (out / LABELS_FILE).open("x", encoding="utf-8") as labels:
            labels.write(label_template(out.name, results))
        markdown = write_summary(out, summarize(out, io.metrics))
    except (SummaryError, ValueError, OSError) as error:
        after = f"; {_KEEP_OR_DELETE}" if live else ""
        if live and m3_reason is not None:  # deleting it would drop its spend from M3's sum
            after = f"; {M3_KEEP}"
        spent = usd(cap.total) + ("" if live else " (dry run: fake usage)")
        entry = (
            f" Its test-run ledger entry is in {ledger.relative_to(git.root)}: commit it with"
            " the folder."
            if split == "test"
            else ""
        )
        print(
            f"{PROG}: the run FAILED, no summary written: {error}. Its inputs are in {out}{after}."
            f" Run status {outcome.status}; counted spend {spent}.{entry} Fix it, then run"
            f" `{PROG} summarize {out}`.",
            file=sys.stderr,
        )
        return EXIT_LEAK if outcome.status == "aborted_leak" else EXIT_FAILED
    print()
    print(markdown)
    branch = (
        f"a live run of M3: commit this folder on {M3_LIVE_BRANCH}"
        if m3_reason is not None
        else "a live run: commit this folder on a branch"
    )
    where = (
        "a dry run: git ignores this folder"
        if not live
        else f"{branch} (labels.jsonl after labelling)"
        + (f", with {ledger.relative_to(git.root)}" if split == "test" else "")
    )
    print(f"Written to {out} ({where}).")
    return {
        "complete": EXIT_OK,
        "partial": EXIT_PARTIAL,
        "aborted_leak": EXIT_LEAK,
        "failed": EXIT_FAILED,
    }[outcome.status]


def _retrieval_info(config: RunConfig) -> RetrievalInfo | None:
    """run.json's `config.retrieval` block: None in vector mode (M2's run.json and fingerprint)."""
    if not config.hybrid:
        return None
    return RetrievalInfo(
        mode="hybrid",
        candidate_k=config.retrieval.candidate_k,
        rrf_k=config.retrieval.rrf_k,
        language_map_sha256=language_map_sha256(),
    )


def _m3_spend(io: Io, git: GitInfo, why: str | None) -> M3Spend | None:
    """M3's spend from HEAD (`spend.py`). A run of M3 is refused when it cannot be counted; any
    other live run only says so (its own spend is not M3's)."""
    try:
        spend = m3_spend(git.root, io.head_paths, io.head_file)
        return replace(spend, head=f"HEAD ({io.head_ref(git.root)})")
    except (SpendError, GitError) as error:
        if why is not None:
            raise Refused(f"M3's spend cannot be counted: {error}") from None
        return None


def _spend_lines(spend: M3Spend | None, why: str | None, live: bool) -> str:
    if spend is None:
        if not live:
            return ""
        return (
            "\nM3 spend: this run does not count toward M3's $5.00 (vector mode without"
            " --m3-spend), and M3's spend could not be read from HEAD."
        )
    return "\n" + spend_text(spend, why)


def _check_settings(settings: Settings, live: bool) -> None:
    if settings.environment == "production":
        raise Refused("the eval harness never runs with environment production")
    if live and (settings.llm_provider == "fake" or settings.embeddings_provider == "fake"):
        raise Refused(
            "--live needs the paid providers in Settings: llm_provider anthropic and"
            " embeddings_provider voyage, with both keys in the process environment (runbook:"
            f" {RUNBOOK}); a fake provider's numbers are never results"
        )
    if not live and (settings.llm_provider != "fake" or settings.embeddings_provider != "fake"):
        raise Refused(
            "a run without --live uses the fake providers only, but Settings name"
            f" llm_provider {settings.llm_provider} and embeddings_provider"
            f" {settings.embeddings_provider}: no paid call without --live and its cap"
        )


def _check_test_run(
    args: argparse.Namespace,
    io: Io,
    git: GitInfo,
    dataset: Dataset,
    fingerprint: str,
    freeze_sha: str | None,
    ledger: Path,
) -> tuple[int, int]:
    """Refuse a test run the discipline does not allow (module docstring); return how many ledger
    entries came before it: for this config fingerprint, and for this frozen version (any
    config)."""
    reports = git.root / REPORTS_DIR / dataset.manifest.dataset / dataset.manifest.version
    if committed_dev_run(git.root, reports, fingerprint, io.head_file) is None:
        raise Refused(
            f"no complete live dev run with config {fingerprint[:12]} is committed under"
            f" {reports.relative_to(git.root)}: run dev with this config first and commit its"
            " folder"
        )
    relative = PurePosixPath(ledger.relative_to(git.root).as_posix())
    working = ledger.read_bytes() if ledger.is_file() else None
    if working != io.head_file(git.root, relative):
        raise Refused(
            f"{relative} is not as committed in HEAD: commit the test-run ledger (with the test"
            " run folders it names) before another test run, so no earlier entry can be lost"
        )
    entries = read_ledger(ledger)
    earlier = [e for e in entries if e.config_fingerprint == fingerprint]
    if earlier and args.test_rerun_reason is None:
        raise Refused(
            f"config {fingerprint[:12]} already has {len(earlier)} test run(s) in"
            f" {relative} (first: {earlier[0].run_id}): the test split runs once per reported"
            " config; a rerun needs --test-rerun-reason, recorded in the ledger"
        )
    same_version = [
        e
        for e in entries
        if e.dataset == dataset.manifest.dataset
        and e.version == dataset.manifest.version
        and e.freeze_sha256 == freeze_sha
    ]
    return len(earlier), len(same_version)


def _plan_text(
    dataset: Dataset,
    split: str,
    questions: Sequence[EvalQuestion],
    config: RunConfig,
    settings: Settings,
    embedding_model: str,
    answer_model: str,
    live: bool,
    *,
    documents: str | None = None,
) -> str:
    mode = (
        "LIVE: paid embedding and answer-model calls, billed to the keys' accounts."
        if live
        else "DRY RUN: fake embedder and canned fake answers; nothing is sent, nothing billed,"
        " and no number it prints is a result."
    )
    by_business = ", ".join(
        f"{b} {sum(q.business_id == b for q in questions)}" for b in dataset.manifest.businesses
    )
    corpus_synthetic = all(m.synthetic for m in dataset.corpus.manifests.values())
    synthetic = (
        "synthetic data"
        if dataset.manifest.synthetic and corpus_synthetic
        else "NOT synthetic data (dataset manifest synthetic:"
        f" {str(dataset.manifest.synthetic).lower()}, corpus manifests synthetic:"
        f" {str(corpus_synthetic).lower()})"
    )
    return "\n".join(
        [
            f"{dataset.manifest.dataset} {dataset.manifest.version} (stage"
            f" {dataset.manifest.stage}), split {split}: {len(questions)} questions"
            f" ({by_business}); {synthetic}",
            f"Mode: {mode}",
            f"Embeddings {settings.embeddings_provider} · {embedding_model}; answers"
            f" {settings.llm_provider} · {ANSWER_ALIAS} → {answer_model}; prompt {PROMPT_VERSION}",
            f"Config: chunks {config.chunking.max_chars}/{config.chunking.overlap_chars} · search k"
            f" {config.search_k} · metric k {config.metric_k} · rag_min_score"
            f" {config.rag_min_score} (provisional, ADR 0011) · weak evidence →"
            f" {config.weak_evidence_outcome}",
            f"Retrieval: {retrieval_text(_retrieval_info(config), config.search_k)}",
            *(() if documents is None else (documents,)),
        ]
    )


def _documents_line(mapping: Mapping[str, str], vector_run: str | None, ingest_usd: Decimal) -> str:
    """A hybrid plan's line on document embeddings (M3 PR 5), checked against HEAD
    (`committed_vector_dev_run`, review of PR 5, ananya A1): "no document embedding expected"
    only when a committed live vector dev run used these namespaces, else a warning."""
    ids = ", ".join(sorted(mapping.values()))
    if vector_run is not None:
        return (
            f"Document embeddings: same namespaces as {vector_run} (a complete live vector dev"
            f" run committed in HEAD): no document embedding expected. The eval namespaces ({ids})"
            " depend on the embedding model and chunk config, never on the retrieval mode; ingest"
            " embeds only documents a namespace does not hold yet (D3's volume rule: no reset-db"
            " or volume prune since that run), and the text leg embeds nothing: one query"
            " embedding per question, as in vector mode."
        )
    return (
        "Document embeddings: WARNING: no committed live vector dev run used these namespaces"
        f" ({ids}): ingest may embed the corpus (worst case {usd(ingest_usd)}), and compare"
        " refuses a different chunk config. For M3's hybrid dev runs, pass M2's dev folder's"
        f" --max-chars and --overlap (and --search-k) when they are not the defaults ({RUNBOOK}"
        " §6)."
    )


def _execute(
    io: Io,
    args: argparse.Namespace,
    dataset: Dataset,
    corpus_dir: Path,
    questions: Sequence[EvalQuestion],
    mapping: dict[str, str],
    settings: Settings,
    config: RunConfig,
    cap: CostCap,
    out: Path,
    live: bool,
) -> tuple[RunOutcome, Sinks, list[str], list[str]]:
    """Run `io.execute` with the folder's files open and the records captured as they come."""
    with ExitStack() as stack:
        sinks = Sinks(
            results=stack.enter_context((out / RESULTS_FILE).open("x", encoding="utf-8")),
            answers=stack.enter_context((out / ANSWERS_FILE).open("x", encoding="utf-8")),
            written=[],
        )
        logs = {
            name: stack.enter_context((out / name).open("x", encoding="utf-8"))
            for name in (EMBEDDING_LOG_FILE, LLM_LOG_FILE, ANSWER_LOG_FILE)
        }
        embedding = stack.enter_context(
            captured(logging.getLogger(EMBEDDINGS_LOGGER), logs[EMBEDDING_LOG_FILE])
        )
        llm = stack.enter_context(captured(gateway_logger, logs[LLM_LOG_FILE]))
        stack.enter_context(captured(logging.getLogger(ANSWER_LOGGER), logs[ANSWER_LOG_FILE]))

        def records(trace_id: str | None) -> CallRecord | None:
            return record_for(llm.lines, trace_id)

        target = Target(
            corpus=dataset.corpus,
            corpus_dir=corpus_dir,
            questions=tuple(questions),
            mapping=mapping,
        )
        try:
            outcome = asyncio.run(
                io.execute(
                    target,
                    settings,
                    config,
                    cap,
                    dry_run=not live,
                    owner_url=io.owner_url(),
                    records=records,
                    sink=sinks,
                    progress=lambda line: print(line, flush=True),
                    fresh=args.fresh,
                    functions=io.metrics,
                )
            )
        except NotImplementedError as missing:  # H1 or H2 not written yet
            outcome = RunOutcome("failed", f"NotImplementedError: {missing}", [])
        except (HarnessRefused, IngestRefused) as refused:
            outcome = RunOutcome("failed", str(refused), [])
        except Exception as error:  # a bug or an outage: the folder keeps what was made
            # Only the class name: a driver's message can hold a database URL or input text.
            reason = f"{type(error).__name__} (an outage or a bug; its message is not shown)"
            outcome = RunOutcome("failed", reason, [])
        return outcome, sinks, list(llm.lines), list(embedding.lines)


def _complete_results(
    questions: Sequence[EvalQuestion], mapping: dict[str, str], written: Sequence[QuestionResult]
) -> list[QuestionResult]:
    """One result per selected question: the written ones, then `not_run` for the rest."""
    done = {result.question_id for result in written}
    return [*written] + [not_run(q, mapping[q.business_id]) for q in questions if q.id not in done]


# ── ingest, summarize, label-sheet ───────────────────────────────────────────────────────────


def ingest_command(args: argparse.Namespace, io: Io) -> int:
    git = io.git()
    dataset_dir: Path = args.dataset or git.root / DEFAULT_DATASET
    dataset = read_dataset(dataset_dir, args.corpus)
    corpus_dir: Path = args.corpus or repo_root_of(dataset_dir) / dataset.manifest.corpus
    settings = io.load_settings(False)
    _check_settings(settings, live=False)
    config = RunConfig(
        chunking=ChunkingConfig(max_chars=args.max_chars, overlap_chars=args.overlap),
        search_k=settings.rag_context_k,
        metric_k=min(5, settings.rag_context_k),
        rag_min_score=settings.rag_min_score,
        weak_evidence_outcome=settings.rag_weak_evidence_outcome,
        llm_max_retries=settings.llm_max_retries,
        llm_max_repairs=settings.llm_max_repairs,
    )
    spec = load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    mapping = namespaces(
        dataset.manifest.businesses, embedding_model=spec.model, chunking=config.chunking
    )
    target = Target(corpus=dataset.corpus, corpus_dir=corpus_dir, questions=(), mapping=mapping)
    try:
        outcome = asyncio.run(
            io.execute(
                target,
                settings,
                config,
                CostCap(cap=None),
                dry_run=True,
                owner_url=io.owner_url(),
                records=lambda trace_id: None,
                sink=lambda result, answer: None,
                progress=lambda line: print(line, flush=True),
                fresh=args.fresh,
                ingest_only=True,
            )
        )
    except (HarnessRefused, IngestRefused) as refused:
        outcome = RunOutcome("failed", str(refused), [])
    except Exception as error:  # an outage or a bug: the class name only (module docstring)
        reason = f"{type(error).__name__} (an outage or a bug; its message is not shown)"
        outcome = RunOutcome("failed", reason, [])
    for problem in outcome.problems:
        print(f"PROBLEM  {problem}", file=sys.stderr)
    print(
        f"ingest (dry run, fake embedder): {outcome.status}"
        + (f": {outcome.stop}" if outcome.stop else "")
    )
    return EXIT_OK if outcome.status == "complete" else EXIT_FAILED


def summarize_command(args: argparse.Namespace, io: Io) -> int:
    run_dir: Path = args.run_dir
    print(write_summary(run_dir, summarize(run_dir, io.metrics)))
    print(f"Written to {run_dir / 'summary.md'} and summary.json.")
    return EXIT_OK


def label_sheet_command(args: argparse.Namespace, io: Io) -> int:
    """Print the label sheet (module docstring, "Labelling"). Every check runs before the first
    line is printed, so a refusal shows no question, answer or chunk text."""
    run_dir: Path = args.run_dir
    run = read_run(run_dir)
    results = read_results(run_dir)
    answers = read_answers(run_dir)
    git = io.git()
    dataset_dir: Path = args.dataset or git.root / DEFAULT_DATASET
    dataset = read_dataset(dataset_dir, args.corpus)
    freeze = dataset.directory / FREEZE_FILE
    provenance = check_corpus(run, dataset, freeze.read_bytes() if freeze.is_file() else None)
    cited = cited_texts(run, results, answers, dataset)
    questions = {q.id: q for q in dataset.questions}
    print(label_sheet_header(run.run_id, provenance), end="\n\n")
    print(render_label_sheet(answers, questions, cited), end="")
    print(
        f"Label each answer in {run_dir / LABELS_FILE} (correctness) and each shown answer in"
        f" {run_dir / FAITHFULNESS_LABELS_FILE} (faithfulness; `{PROG} faithfulness-template"
        f" {run_dir}` writes it if it is missing). Rules: docs/learning/m2-eval-questions.md §5;"
        f" steps: {RUNBOOK} step 12."
    )
    return EXIT_OK


def faithfulness_template_command(args: argparse.Namespace, io: Io) -> int:
    """Write the folder's faithfulness_labels.jsonl: one `label: null` line per shown answer
    (ids only). Refused when the file exists: labels are never overwritten."""
    run_dir: Path = args.run_dir
    run = read_run(run_dir)
    results = read_results(run_dir)
    if len(results) != run.questions_selected:
        raise Refused(
            f"{RESULTS_FILE} has {len(results)} line(s), run.json says {run.questions_selected}"
            " question(s) were selected"
        )
    path = run_dir / FAITHFULNESS_LABELS_FILE
    text = faithfulness_template(run.run_id, results)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError:
        raise Refused(f"{path} exists: labels are never overwritten") from None
    print(
        f"Wrote {path}: {len(text.splitlines())} line(s), one per shown answer (outcome answered),"
        " each with label null. From the label sheet, set label (faithful | unfaithful),"
        f" labelled_by and labelled_on, then run `{PROG} summarize {run_dir}` ({RUNBOOK} step 12)."
    )
    return EXIT_OK


def _committed_ledger(io: Io, dataset: str) -> list[LedgerEntry] | None:
    """The dataset's test-run ledger as committed in HEAD, or None when HEAD has none."""
    git = io.git()
    relative = PurePosixPath((REPORTS_DIR / dataset / LEDGER_FILE).as_posix())
    content = io.head_file(git.root, relative)
    if content is None:
        return None
    try:
        return parse_ledger(content.decode("utf-8"))
    except (UnicodeDecodeError, ValidationError):
        raise CompareRefused(f"{relative} as committed in HEAD is not a valid ledger") from None


def compare_command(args: argparse.Namespace, io: Io) -> int:
    """Print the comparison of two run folders (`compare.py`); exit 1 when it is refused."""
    try:
        run_a = load_run(args.run_a, "A")
        run_b = load_run(args.run_b, "B")
        both_test = run_a.info.split == run_b.info.split == "test"
        ledger = _committed_ledger(io, run_a.info.dataset) if both_test else None
        comparison = compare(run_a, run_b, io.metrics, ledger=ledger)
    except CompareRefused as refused:
        print(f"{PROG} compare: refused: {refused}", file=sys.stderr)
        return EXIT_FAILED
    print(render_comparison(comparison), end="")
    return EXIT_OK


def run(argv: Sequence[str] | None, io: Io) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args, io)
        if args.command == "ingest":
            return ingest_command(args, io)
        if args.command == "summarize":
            return summarize_command(args, io)
        if args.command == "compare":
            return compare_command(args, io)
        if args.command == "faithfulness-template":
            return faithfulness_template_command(args, io)
        return label_sheet_command(args, io)
    except (
        Refused,
        GitError,
        DatasetError,
        CorpusError,
        NamespaceError,
        SummaryError,
        ValueError,
        OSError,
    ) as error:
        print(f"{PROG}: {error}; nothing was sent", file=sys.stderr)
        return EXIT_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv, default_io())
