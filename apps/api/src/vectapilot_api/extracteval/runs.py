"""The harness commands (M6 PR 4): `run` and `summarize`.

- **`run --split dev|test`** runs every document of the split through the extraction core
  in-process (`service.ExtractionService`: text, one model call, signals, routing (H1), the
  invariant check), each with its own business's configuration, and writes a folder: `run.json`,
  `results.jsonl` (ids and codes only), `values.jsonl` (synthetic values, never printed),
  `llm_call_log.jsonl` (the gateway's metadata-only records) and, once H2 is written,
  `summary.md`. Dry unless `--live --max-usd X` (`harness.py`). The model is the extraction's
  gateway (its own time limits, no cache, E11) as a `CappedGateway`: each call's worst case is
  reserved before it is sent, and `CapReached` stops the run (the documents not reached are
  `not_run`). Test follows the discipline in `harness.py`: the ledger entry is appended before the
  first document runs.
- **Stops:** H1 not written refuses a live run before any paid call (`harness.h1_written`); a dry
  run stops after its first document (the core turns the stub's NotImplementedError into an
  all-review `internal_error` extraction: `failed`, the rest `not_run`). An extraction or an
  `llm_call` record naming another business than its document's stops the run at once (exit 4).
  A cap stop or a stop rule (`CostCap.stop`) ends it `partial`; any other exception `failed`.
- **`summarize RUN_DIR`** (re)writes the summary from run.json and results.jsonl only (never
  values.jsonl). While H2 is unwritten it writes none and says so; the results are kept. When
  review recall's misses are not exactly the silent errors (`summary.identity_holds`), the
  summary opens with "Inconsistent" and the command exits 1: a wrong H2 never yields a clean
  headline.

Exit codes: 0 complete (summary written, consistent) · 1 refused, failed, no summary or
inconsistent · 3 partial · 4 a cross-business leak.
"""

import asyncio
import hashlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal, TextIO

from pydantic import BaseModel

from vectapilot_api.config import Settings
from vectapilot_api.evalrun.budget import CappedGateway, CapReached, CostCap, record_for, usd
from vectapilot_api.evalrun.intervals import DEFAULT_CONFIDENCE
from vectapilot_api.evalrun.ledger import LedgerEntry, append_ledger
from vectapilot_api.extracteval.compare import FieldKind, same_value
from vectapilot_api.extracteval.dryrun import ScriptProvider
from vectapilot_api.extracteval.harness import (
    DRY_REASON,
    LEDGER_PATH,
    EvalDocument,
    Io,
    Loaded,
    Refused,
    check_settings,
    check_test_run,
    cost_info,
    h1_written,
    load_dry,
    load_live,
    spend_lines,
    synthetic_everywhere,
)
from vectapilot_api.extracteval.runinfo import (
    LLM_LOG_FILE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    VALUES_FILE,
    DocumentLine,
    ExtractRunInfo,
    FieldLine,
    Fingerprint,
    Limits,
    RunStatus,
    SplitName,
    ValueLine,
    folder_name,
    json_line,
    read_results,
    read_run,
    run_dir,
    write_json,
)
from vectapilot_api.extracteval.summary import Context, identity_holds, render
from vectapilot_api.extraction.config import FieldType, KindConfig
from vectapilot_api.extraction.extractor import (
    EXTRACT_ATTEMPT_TIMEOUT_S,
    EXTRACT_DEADLINE_S,
    FieldExtractor,
    Proposal,
)
from vectapilot_api.extraction.prompt import EXTRACT_ALIAS, EXTRACT_PROMPT_VERSION
from vectapilot_api.extraction.service import ExtractionOutcome, ExtractionService
from vectapilot_api.extraction.signals import EXTRACTION_RULES_VERSION
from vectapilot_api.extraction.text import DEFAULT_LIMITS, DocumentText
from vectapilot_api.gateway.catalog import Catalog, load_catalog
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.llm_cli import GitInfo, captured, confirmed

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_PARTIAL: Final = 3
EXIT_LEAK: Final = 4
H1_MISSING: Final = (
    "H1 (route_fields) is not written yet: every extraction is an all-review internal_error, so"
    " the run stopped after its first document (guide: docs/learning/m6-routing.md)"
)
H1_MISSING_LIVE: Final = (
    "H1 (route_fields) is not written yet: a live run would pay for all-review extractions"
    " (guide: docs/learning/m6-routing.md)"
)
H2_MISSING: Final = (
    "summary not written: H2 is not written yet (extracteval/metrics.py; guide"
    " docs/learning/m6-metrics.md). The results and run.json are kept: run `summarize` on this"
    " folder once it is"
)
INCONSISTENT_EXIT: Final = (
    "summary.md opens with Inconsistent: review recall's misses are not exactly the silent"
    " errors. H2 or the results are wrong; no number in this run may be read (exit 1)"
)
NOT_IMPLEMENTED: Final = NotImplementedError.__name__
SummaryState = Literal["written", "h2_missing", "inconsistent"]


# ── The fingerprint ──────────────────────────────────────────────────────────────────────────


def make_fingerprint(loaded: Loaded, settings: Settings, catalog: Catalog) -> Fingerprint:
    return Fingerprint(
        freeze_sha256=loaded.freeze_sha256,
        extract_prompt_version=EXTRACT_PROMPT_VERSION,
        extraction_rules_version=EXTRACTION_RULES_VERSION,
        alias=EXTRACT_ALIAS,
        model=catalog.aliases[EXTRACT_ALIAS].model,
        catalog_version=catalog.catalog_version,
        businesses=loaded.pins,
        limits=Limits(
            max_bytes=DEFAULT_LIMITS.max_bytes,
            max_pages=DEFAULT_LIMITS.max_pages,
            max_characters=DEFAULT_LIMITS.max_characters,
            attempt_timeout_s=EXTRACT_ATTEMPT_TIMEOUT_S,
            deadline_s=EXTRACT_DEADLINE_S,
            max_retries=settings.llm_max_retries,
            max_repairs=settings.llm_max_repairs,
        ),
        fixtures_sha256=None if loaded.fixtures is None else loaded.fixtures.sha256,
    )


def trace_id_for(run_id: str, document_id: str) -> str:
    """A W3C trace id per document, derived from the run and the document (never all zeros in
    practice; the service refuses one that is)."""
    return hashlib.sha256(f"extracteval:{run_id}:{document_id}".encode()).hexdigest()[:32]


# ── One document ─────────────────────────────────────────────────────────────────────────────


class RecordingExtractor:
    """The model call (`extractor.FieldExtractor`), keeping the last proposal (for values.jsonl)
    and a `CapReached` the service would otherwise turn into an all-review extraction."""

    def __init__(self, inner: FieldExtractor) -> None:
        self._inner = inner
        self.proposal: Proposal | None = None
        self.cap_reached: CapReached | None = None
        self.called = False

    def reset(self) -> None:
        self.proposal, self.cap_reached, self.called = None, None, False

    async def __call__(
        self, kind: KindConfig, document: DocumentText, *, business_id: str, trace_id: str
    ) -> Proposal:
        self.called = True
        try:
            proposal = await self._inner(kind, document, business_id=business_id, trace_id=trace_id)
        except CapReached as reached:
            self.cap_reached = reached
            raise
        self.proposal = proposal
        return proposal


@dataclass(frozen=True)
class Done:
    line: DocumentLine
    values: tuple[ValueLine, ...] = ()
    cap_reached: CapReached | None = None
    leaks: tuple[str, ...] = ()


_KINDS: Final[dict[FieldType, FieldKind]] = {
    FieldType.TEXT: "text",
    FieldType.DATE: "date",
    FieldType.ENUM: "enum",
    FieldType.IDENTIFIER: "identifier",
}
"""The core's field types as the eval's own comparison names them (`compare.FieldKind`)."""


def _types(kind: KindConfig) -> dict[str, FieldKind]:
    return {spec.name: _KINDS[spec.type] for spec in kind.template.fields}


def unscored(document: EvalDocument, kind: KindConfig, run: str, error: str | None) -> DocumentLine:
    """A document without an extraction (`not_run`, `harness_error`): every field, none scored."""
    fields = tuple(
        FieldLine(
            field=name,
            type=field_type,
            status=None,
            level=None,
            reasons=(),
            page=None,
            quote_hits=0,
            has_value=False,
            gold_null=document.gold.get(name) is None,
            correct=None,
        )
        for name, field_type in _types(kind).items()
    )
    return DocumentLine(
        id=document.id,
        business_id=document.business_id,
        kind=document.kind,
        split=document.split,
        tags=document.tags,
        run="harness_error" if run == "harness_error" else "not_run",
        status=None,
        error=error,
        fields=fields,
    )


def _proposed(proposal: Proposal | None, name: str) -> tuple[str | None, str | None]:
    if proposal is None:
        return None, None
    dumped = proposal.answer.model_dump()
    item = dumped.get(name)
    if not isinstance(item, dict):
        return None, None
    value, quote = item.get("value"), item.get("quote")
    return (
        value if isinstance(value, str) else None,
        quote if isinstance(quote, str) else None,
    )


def checked_value(outcome: ExtractionOutcome, name: str) -> str | None:
    """The field's proposal as the checks read it (`FieldEvidence.value`, canonical where they
    allow it), before "no quote, no value" or a withheld value dropped it; None when the call
    failed (E3: no signals) or nothing was proposed."""
    if outcome.signals is None:
        return None
    evidence = outcome.signals.evidence.get(name)
    return None if evidence is None else evidence.value


def scored_line(
    document: EvalDocument,
    kind: KindConfig,
    outcome: ExtractionOutcome,
    record: CallRecord | None,
    latency_ms: float,
    called: bool,
) -> DocumentLine:
    """One scored document. `correct` scores the value the system returned (stored); the level
    check scores the proposal at its level as routed (`routed_level`, `proposal_correct`), so an
    unverified guess that "no quote, no value" dropped still counts against its own level."""
    types = _types(kind)
    fields = [
        FieldLine(
            field=result.field,
            type=types[result.field],
            status=result.status.value,
            level=None if result.level is None else result.level.value,
            reasons=tuple(sorted(reason.value for reason in result.reasons)),
            page=result.page,
            quote_hits=result.quote_hits,
            has_value=result.value is not None,
            gold_null=document.gold.get(result.field) is None,
            correct=same_value(types[result.field], document.gold.get(result.field), result.value),
            corrected=result.field in outcome.corrected,
            withheld=result.field in outcome.withheld,
            unverified=result.field in outcome.unverified,
            routed_level=None if result.routed_level is None else result.routed_level.value,
            proposal_correct=same_value(
                types[result.field],
                document.gold.get(result.field),
                checked_value(outcome, result.field),
            ),
        )
        for result in outcome.fields
    ]
    return DocumentLine(
        id=document.id,
        business_id=document.business_id,
        kind=document.kind,
        split=document.split,
        tags=document.tags,
        run="scored",
        status=outcome.status.value,
        failure=None if outcome.failure is None else outcome.failure.value,
        error=outcome.error,
        pages=outcome.pages,
        image_pages=len(outcome.image_pages),
        signal_errors=len(outcome.signal_errors),
        llm_calls=0 if record is None else 1,
        input_tokens=0 if record is None else record.input_tokens,
        output_tokens=0 if record is None else record.output_tokens,
        cost_usd=Decimal(0) if record is None else record.cost_usd,
        cost_complete=(not called) if record is None else record.cost_complete,
        latency_ms=latency_ms,
        fields=tuple(fields),
    )


def value_lines(
    document: EvalDocument, outcome: ExtractionOutcome, proposal: Proposal | None
) -> tuple[ValueLine, ...]:
    lines: list[ValueLine] = []
    for result in outcome.fields:
        proposed, quote = _proposed(proposal, result.field)
        lines.append(
            ValueLine(
                id=document.id,
                field=result.field,
                value=result.value,
                proposed=proposed,
                quote=quote,
                gold=document.gold.get(result.field),
            )
        )
    return tuple(lines)


# ── run ──────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    status: RunStatus
    stop: str | None = None
    leak: bool = False


Sink = Callable[[DocumentLine, Sequence[ValueLine]], None]


@dataclass
class _Run:
    """One run's live objects."""

    io: Io
    settings: Settings
    catalog: Catalog
    cap: CostCap
    lines: list[str]
    """The gateway's captured `llm_call` lines."""
    provider: Provider
    run_id: str
    loaded: Loaded

    def kind_of(self, document: EvalDocument) -> KindConfig:
        kind = self.loaded.extraction.kind_config(document.business_id, document.kind)
        if kind is None:  # the check and the fixture loader refuse this before any run
            raise Refused(f"document {document.id}: its business does not enable its kind")
        return kind

    async def all(self, documents: Sequence[EvalDocument], sink: Sink) -> Outcome:
        settings = self.settings
        gateway = CappedGateway(
            self.provider,
            self.catalog,
            attempt_timeout_s=EXTRACT_ATTEMPT_TIMEOUT_S,
            deadline_s=EXTRACT_DEADLINE_S,
            retry_policy=settings.llm_retry_policy(),
            max_repairs=settings.llm_max_repairs,
            cap=self.cap,
            records=lambda trace: record_for(self.lines, trace),
        )
        recorder = RecordingExtractor(FieldExtractor(gateway))
        service = ExtractionService(recorder, route=self.io.route)
        try:
            return await self._documents(service, recorder, documents, sink)
        finally:
            await gateway.aclose()

    def _rest(self, documents: Sequence[EvalDocument], sink: Sink) -> None:
        for left in documents:
            sink(unscored(left, self.kind_of(left), "not_run", None), ())

    async def _documents(
        self,
        service: ExtractionService,
        recorder: RecordingExtractor,
        documents: Sequence[EvalDocument],
        sink: Sink,
    ) -> Outcome:
        for index, document in enumerate(documents):
            if isinstance(self.provider, ScriptProvider):
                self.provider.begin(document.id)
            done = await self._one(service, recorder, document)
            if done.cap_reached is not None:
                self._rest(documents[index:], sink)
                return Outcome("partial", str(done.cap_reached))
            sink(done.line, done.values)
            if done.leaks:
                self._rest(documents[index + 1 :], sink)
                return Outcome(
                    "failed",
                    f"cross-business leak in document {document.id}: {'; '.join(done.leaks)}",
                    leak=True,
                )
            if done.line.failure == "internal_error" and done.line.error == NOT_IMPLEMENTED:
                self._rest(documents[index + 1 :], sink)
                return Outcome("failed", H1_MISSING)
            print(f"document {index + 1} of {len(documents)}: {document.id}", flush=True)
            if self.cap.stop is not None:
                self._rest(documents[index + 1 :], sink)
                return Outcome("partial", self.cap.stop)
        return Outcome("complete")

    async def _one(
        self, service: ExtractionService, recorder: RecordingExtractor, document: EvalDocument
    ) -> Done:
        kind = self.kind_of(document)
        trace = trace_id_for(self.run_id, document.id)
        recorder.reset()
        started = self.io.clock()
        try:
            outcome = await service.extract(
                kind, document.pdf, business_id=document.business_id, trace_id=trace
            )
        except Exception as error:  # a refusal or a bug outside the core's fail-closed path
            if recorder.cap_reached is not None:
                return Done(unscored(document, kind, "not_run", None), (), recorder.cap_reached)
            return Done(unscored(document, kind, "harness_error", type(error).__name__))
        latency_ms = max(0.0, (self.io.clock() - started) * 1000)
        if recorder.cap_reached is not None:  # nothing was sent: the document was not run
            return Done(unscored(document, kind, "not_run", None), (), recorder.cap_reached)
        record = record_for(self.lines, trace)
        leaks: list[str] = []
        if outcome.business_id != document.business_id:
            leaks.append("the extraction names another business")
        if record is not None and record.business_id != document.business_id:
            leaks.append("its llm_call record names another business")
        line = scored_line(document, kind, outcome, record, latency_ms, recorder.called)
        return Done(line, value_lines(document, outcome, recorder.proposal), None, tuple(leaks))


def _load(
    io: Io,
    git: GitInfo,
    *,
    dataset: Path | None,
    corpus: Path | None,
    extraction: Path | None,
    fixtures: Path | None,
    split: str,
    live: bool,
) -> Loaded:
    if live:
        loaded = load_live(dataset, corpus, extraction, git, io.head_file, split)
    else:
        loaded = load_dry(dataset, corpus, extraction, fixtures, git, split)
    if not synthetic_everywhere(loaded):
        raise Refused(
            "every manifest, gold row, fixture file and business file must say synthetic: true"
        )
    return loaded


def run_command(
    io: Io,
    *,
    dataset: Path | None,
    corpus: Path | None,
    extraction: Path | None,
    split: str,
    live: bool,
    max_usd: Decimal | None,
    cap_text: str | None,
    reason: str | None,
    fixtures: Path | None,
) -> tuple[Path, int]:
    git = io.git()
    loaded = _load(
        io,
        git,
        dataset=dataset,
        corpus=corpus,
        extraction=extraction,
        fixtures=fixtures,
        split=split,
        live=live,
    )
    split_name: SplitName = "test" if split == "test" else "dev"
    settings = io.load_settings(live)
    check_settings(settings, live=live)
    if live and not h1_written(io.route):
        raise Refused(H1_MISSING_LIVE)
    catalog = load_catalog()
    if EXTRACT_ALIAS not in catalog.aliases:
        raise Refused(f"the model catalog has no {EXTRACT_ALIAS!r} alias for extraction")
    fingerprint = make_fingerprint(loaded, settings, catalog)
    before = 0
    if split_name == "test":
        before = check_test_run(
            io, git, loaded, fingerprint=fingerprint.sha256(), live=live, reason=reason
        )
    documents = loaded.documents
    mode = (
        "LIVE: paid calls."
        if live
        else "DRY RUN: a scripted fake model over fixture documents; no number is a result."
    )
    print(
        f"{loaded.manifest.dataset} {loaded.version}, split {split_name}: {len(documents)}"
        f" documents through the extraction core ({EXTRACT_ALIAS} → {fingerprint.model}, prompt"
        f" {EXTRACT_PROMPT_VERSION}, rules {EXTRACTION_RULES_VERSION}), one call each."
        f"\nMode: {mode}"
        + ("" if max_usd is None else f"\nCap: {usd(max_usd)}; every call is reserved first."),
        flush=True,
    )
    if live:
        if max_usd is None:
            raise Refused("--live requires --max-usd")
        print(spend_lines(io, git, f"live extract {split_name}", max_usd))
        if not confirmed(io.stdin, cap_text or "", stdout_is_terminal=io.stdout_is_terminal()):
            raise Refused("not confirmed")
    started = io.now()
    name = folder_name(live=live, started=started, split=split_name)
    out = run_dir(git.root, loaded.version, name, live=live)
    out.mkdir(parents=True, exist_ok=False)
    if split_name == "test" and loaded.freeze_sha256 is not None:
        append_ledger(
            git.root / LEDGER_PATH,
            LedgerEntry(
                dataset=loaded.manifest.dataset,
                version=loaded.version,
                freeze_sha256=loaded.freeze_sha256,
                config_fingerprint=fingerprint.sha256(),
                run_id=name,
                commit=git.commit,
                started_at=started,
                rerun_reason=reason,
            ),
        )
    cap = CostCap(cap=max_usd)
    written: list[DocumentLine] = []
    with (
        (out / RESULTS_FILE).open("x", encoding="utf-8") as results,
        (out / VALUES_FILE).open("x", encoding="utf-8") as values,
        (out / LLM_LOG_FILE).open("x", encoding="utf-8") as log,
        captured(gateway_logger, log) as capture,
    ):

        def sink(line: DocumentLine, lines: Sequence[ValueLine]) -> None:
            written.append(line)
            _write(results, line)
            for value in lines:
                _write(values, value)

        provider: Provider = (
            ScriptProvider(loaded.fixtures.file)
            if loaded.fixtures is not None
            else io.build_provider(settings)
        )
        run = _Run(io, settings, catalog, cap, capture.lines, provider, name, loaded)
        try:
            outcome = asyncio.run(run.all(documents, sink))
        except Exception as error:  # an outage or a bug: the class name only
            outcome = Outcome("failed", f"{type(error).__name__} (message not shown)")
    info = ExtractRunInfo(
        run_id=name,
        status=outcome.status,
        stop=outcome.stop,
        leak=outcome.leak,
        dry_run=not live,
        dry_reason=None if live else DRY_REASON,
        environment=settings.environment,
        dataset=loaded.manifest.dataset,
        dataset_version=loaded.version,
        dataset_stage=loaded.manifest.stage,
        freeze_sha256=loaded.freeze_sha256,
        dataset_committed=loaded.committed,
        split=split_name,
        documents=len(documents),
        commit=git.commit,
        uncommitted_changes=git.uncommitted_changes,
        started_at=started,
        ended_at=io.now(),
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        llm_provider=settings.llm_provider if live else "fake",
        confidence=str(DEFAULT_CONFIDENCE),
        cost=cost_info(cap),
        test_rerun_reason=reason if split_name == "test" else None,
        test_runs_before=before,
    )
    write_json(out / RUN_FILE, info)
    summarized: SummaryState | None = (
        None if info.status == "failed" else write_summary(out, info, written, io)
    )
    print(
        f"run {info.status}"
        + (f" ({info.stop})" if info.stop else "")
        + f"; spend {usd(cap.total)}. Written to {out}"
        + (
            ". Commit this folder on m6/close-out before the next live run."
            if live
            else " (DRY RUN: git ignores it)."
        )
    )
    return out, exit_code(info, summarized)


def _write(handle: TextIO, model: BaseModel) -> None:
    handle.write(json_line(model))
    handle.flush()


def exit_code(info: ExtractRunInfo, summarized: SummaryState | None) -> int:
    """The command's exit code (module docstring); `summarized` None: no summary was tried."""
    if info.leak:
        return EXIT_LEAK
    if info.status == "partial":
        return EXIT_PARTIAL
    return EXIT_OK if summarized == "written" and info.status == "complete" else EXIT_FAILED


# ── summarize ────────────────────────────────────────────────────────────────────────────────


def write_summary(
    folder: Path, info: ExtractRunInfo, documents: Sequence[DocumentLine], io: Io
) -> SummaryState:
    """Write summary.md (rewritten each time: it is derived), or say why not: `written`,
    `h2_missing` (none written) or `inconsistent` (written, opening with the warning)."""
    context = Context(businesses=list(info.fingerprint.businesses), metrics=io.metrics)
    try:
        text = render(info, documents, context)
        consistent = identity_holds(documents, context.metrics)
    except NotImplementedError:
        print(H2_MISSING)
        return "h2_missing"
    (folder / SUMMARY_MD).write_text(text, encoding="utf-8")
    print(text)
    if not consistent:
        print(INCONSISTENT_EXIT, file=sys.stderr)
        return "inconsistent"
    return "written"


def summarize_command(io: Io, folder: Path) -> int:
    info = read_run(folder)
    documents = read_results(folder)
    if len(documents) != info.documents:
        raise Refused(
            f"{RESULTS_FILE} has {len(documents)} line(s), run.json says {info.documents}"
            " document(s)"
        )
    summarized = write_summary(folder, info, documents, io)
    return exit_code(info, summarized)
