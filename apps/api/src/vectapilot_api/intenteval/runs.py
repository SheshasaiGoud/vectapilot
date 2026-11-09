"""The measuring commands (M4 PR 4): `run --mode classifier|llm`, and what every run folder's
summary shares.

- **Classifier mode** is local and $0: every row of the split scored from the model folder's
  embed cache (`classify.py`). Real only when that cache is a committed `-live-embed` folder's.
- **LLM mode** is dry unless `--live --max-usd X` (shesha's alone; the cap typed again at a
  terminal, refused above what is left of M4's $2.00, and unless every manifest says synthetic).
  A dry run answers through `DryRunRouterProvider`, so the gateway, the structured output and the
  records run for real at $0.
- **`summarize` and `dry-run`** live in `hybrid.py`.
- **Test** follows the discipline in `harness.py` and `ledger.py`: the ledger entry is appended
  before the first row is scored or sent.

While H2 is unwritten, a run keeps its results and run.json and writes no summary.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Final

from vectapilot_api.embeddings.catalog import EmbeddingCatalogError, load_embedding_catalog
from vectapilot_api.evalrun.budget import CappedGateway, CostCap, record_for, usd
from vectapilot_api.evalrun.intervals import DEFAULT_CONFIDENCE
from vectapilot_api.evalrun.ledger import append_ledger
from vectapilot_api.evalrun.report import DEFAULT_BOOTSTRAP_SEED, DEFAULT_RESAMPLES, CostInfo
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.intenteval.cache import CacheEntry
from vectapilot_api.intenteval.classify import score_rows
from vectapilot_api.intenteval.harness import (
    Io,
    Loaded,
    Refused,
    check_settings,
    check_test_run,
    confirm_test,
    corpus_synthetic,
    cost_info,
    in_head,
    load,
    relative,
    require_frozen,
    spend_lines,
)
from vectapilot_api.intenteval.ledger import LEDGER_PATH, RunEntry
from vectapilot_api.intenteval.llmrun import (
    DryRunRouterProvider,
    LlmRun,
    row_trace_id,
    run_llm_rows,
)
from vectapilot_api.intenteval.model import ARTIFACT_FILE, CALIBRATION_FILE, THRESHOLDS_FILE
from vectapilot_api.intenteval.model import read_model as read_model_folder
from vectapilot_api.intenteval.rows import EvalRow
from vectapilot_api.intenteval.runinfo import (
    INTENT_REPORTS,
    LLM_LOG_FILE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    Fingerprint,
    IntentRunInfo,
    ResultLine,
    SplitName,
    folder_name,
    read_run,
    run_dir,
    write_json,
)
from vectapilot_api.intenteval.steps import (
    EmbedFolder,
    checked_inputs,
)
from vectapilot_api.intenteval.summary import Context, render
from vectapilot_api.intents.fallback import (
    ROUTER_ALIAS,
    ROUTER_PROMPT_VERSION,
    IntentChoice,
    LLMIntentFallback,
    build_router_request,
)
from vectapilot_api.llm_cli import captured, confirmed

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_PARTIAL: Final = 3
H2_MISSING: Final = (
    "summary not written: H2 is not written yet (intenteval/metrics.py; guide"
    " docs/learning/m4-metrics.md). The results and run.json are kept: run `summarize` on this"
    " folder once it is"
)


def zero_cost() -> CostInfo:
    zero = Decimal(0)
    return CostInfo(
        cap_usd=None,
        embedding_usd=zero,
        llm_usd=zero,
        total_usd=zero,
        llm_cost_complete=True,
        embedding_requests=0,
        llm_calls=0,
    )


def write_results(folder: Path, lines: Sequence[ResultLine]) -> None:
    with (folder / RESULTS_FILE).open("x", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line.model_dump_json() + "\n")


def _split(loaded: Loaded, split: str) -> SplitName:
    """The split's name, refused when it has no rows."""
    if split not in ("dev", "test"):
        raise Refused("--split is dev or test")
    if not loaded.split_rows(split):
        raise Refused(f"no rows on the {split} split: nothing to run")
    return "dev" if split == "dev" else "test"


def embedding_usd_per_message(entries: Sequence[CacheEntry]) -> Decimal | None:
    """The mean embedding cost of one eval row's own request, at the catalog's list price."""
    evals = [e for e in entries if e.file == "eval" and e.reported_tokens is not None]
    if not evals:
        return None
    try:
        price = load_embedding_catalog().price(evals[0].model)
    except EmbeddingCatalogError:
        return None
    tokens = sum(e.reported_tokens or 0 for e in evals)
    return Decimal(tokens) * price / Decimal(1_000_000) / len(evals)


def write_summary(
    folder: Path,
    info: IntentRunInfo,
    lines: Sequence[ResultLine],
    context: Context,
    modes: Mapping[str, Sequence[ResultLine]] | None = None,
) -> bool:
    """Write summary.md (rewritten each time: it is derived), or say why not. True when written."""
    try:
        text = render(info, lines, context, modes=modes)
    except NotImplementedError:
        print(H2_MISSING)
        return False
    (folder / SUMMARY_MD).write_text(text, encoding="utf-8")
    print(text)
    return True


def summary_context(loaded: Loaded, io: Io, embed: EmbedFolder | None = None) -> Context:
    return Context(
        policies=loaded.policies,
        businesses=loaded.dataset.manifest.businesses,
        metrics=io.metrics,
        embedding_usd_per_message=None
        if embed is None
        else embedding_usd_per_message(embed.entries),
    )


def exit_code(info: IntentRunInfo, summarized: bool) -> int:
    if info.status == "partial":
        return EXIT_PARTIAL
    return EXIT_OK if summarized and info.status == "complete" else EXIT_FAILED


# ── classifier mode ──────────────────────────────────────────────────────────────────────────


def run_classifier(
    io: Io, *, dataset: Path | None, model_folder: Path, split: str, reason: str | None
) -> tuple[Path, int]:
    git = io.git()
    model = read_model_folder(model_folder)
    loaded, source, _ = checked_inputs(io, model_folder, dataset)
    freeze = require_frozen(loaded, "a classifier run")
    split_name = _split(loaded, split)
    rows = loaded.split_rows(split_name)
    real = source.real and not model.info.dry_run
    fingerprint = Fingerprint(
        freeze_sha256=freeze,
        artifact_sha256=model.artifact_sha256,
        temperature=model.artifact.temperature,
        tau=model.thresholds.tau,
        sigma=model.thresholds.sigma,
    )
    before = 0
    if split_name == "test":
        permitted = check_test_run(io, git, loaded, mode="classifier", real=real, reason=reason)
        pin = permitted.pin
        pinned = (pin.artifact_sha256, pin.temperature, pin.tau, pin.sigma)
        mine = (model.artifact_sha256, model.artifact.temperature, model.thresholds.tau)
        if pinned != (*mine, model.thresholds.sigma):
            raise Refused("this model folder is not the pinned artifact, T, tau and sigma")
        for name in (ARTIFACT_FILE, CALIBRATION_FILE, THRESHOLDS_FILE):
            if not in_head(git.root, model_folder / name, io.head_file):
                raise Refused(f"{model_folder.name}/{name} is not committed in HEAD as on disk")
        if not confirm_test(io, "test classifier"):
            raise Refused("not confirmed")
        before = permitted.before
    started = io.now()
    name = folder_name("classifier", real=real, started=started, split=split_name)
    out = run_dir(git.root, loaded.version, name, real=real)
    out.mkdir(parents=True, exist_ok=False)
    if split_name == "test":
        append_ledger(
            git.root / LEDGER_PATH,
            RunEntry(
                dataset=loaded.dataset.manifest.dataset,
                version=loaded.version,
                freeze_sha256=freeze,
                mode="classifier",
                fingerprint_sha256=fingerprint.sha256(),
                run_id=name,
                commit=git.commit,
                started_at=started,
                rerun_reason=reason,
            ),
        )
    lines = score_rows(rows, model.artifact, source.by_id(), loaded.policies, io.clock)
    write_results(out, lines)
    info = IntentRunInfo(
        kind="classifier",
        run_id=name,
        status="complete",
        dry_run=not real,
        dry_reason=source.dry_reason,
        environment="local",
        dataset=loaded.dataset.manifest.dataset,
        dataset_version=loaded.version,
        dataset_stage=loaded.dataset.manifest.stage,
        freeze_sha256=freeze,
        dataset_committed=loaded.committed,
        split=split_name,
        rows=len(rows),
        commit=git.commit,
        uncommitted_changes=git.uncommitted_changes,
        started_at=started,
        ended_at=io.now(),
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        embed_folder=model.info.embed_folder,
        model_folder=relative(git.root, model_folder),
        embedding_model=model.artifact.embedding_model,
        embedding_cache_sha256=model.artifact.embedding_cache_sha256,
        confidence=str(DEFAULT_CONFIDENCE),
        bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
        bootstrap_resamples=DEFAULT_RESAMPLES,
        cost=zero_cost(),
        test_rerun_reason=reason if split_name == "test" else None,
        test_runs_before=before,
    )
    write_json(out / RUN_FILE, info)
    summarized = write_summary(out, info, lines, summary_context(loaded, io, source))
    print(f"Written to {out} ({'real' if real else 'DRY RUN: git ignores it'}).")
    return out, exit_code(info, summarized)


# ── LLM mode ─────────────────────────────────────────────────────────────────────────────────


FAIL_CLOSED_ON_TEST: Final = (
    "fail-closed row(s) on test: the run is partial, so a provider outage can never look like"
    " safety (plan, scoring rules)"
)


def status_on_split(outcome: LlmRun, lines: Sequence[ResultLine], split: str) -> LlmRun:
    """A complete run with any fail-closed row on test is `partial` (plan, "fail_closed"); every
    other outcome stays as it is. Dev keeps them as counted rows."""
    failed = sum(line.decided_by == "fail_closed" for line in lines)
    if split != "test" or outcome.status != "complete" or failed == 0:
        return outcome
    return LlmRun(status="partial", stop=f"{failed} {FAIL_CLOSED_ON_TEST}")


def committed_llm_dev_run(io: Io, root: Path, version: str, fingerprint: str) -> str | None:
    """A complete live LLM dev run with this fingerprint whose run.json is in HEAD, or None."""
    for path in sorted((root / INTENT_REPORTS / version).glob(f"*-live-llm-dev/{RUN_FILE}")):
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


def run_llm(
    io: Io,
    *,
    dataset: Path | None,
    split: str,
    live: bool,
    max_usd: Decimal | None,
    cap_text: str | None,
    reason: str | None,
) -> tuple[Path, int]:
    git = io.git()
    loaded = load(dataset, git, io.head_file, live=live)
    freeze = require_frozen(loaded, "an LLM run")
    if live and not (loaded.dataset.manifest.synthetic and corpus_synthetic(loaded)):
        raise Refused("--live needs every manifest to say synthetic: true")
    split_name = _split(loaded, split)
    rows = loaded.split_rows(split_name)
    settings = io.load_settings(live)
    check_settings(settings, live=live, needs="llm")
    catalog = load_catalog()
    fingerprint = Fingerprint(
        freeze_sha256=freeze,
        router_prompt_version=ROUTER_PROMPT_VERSION,
        catalog_version=catalog.catalog_version,
        router_model=catalog.aliases[ROUTER_ALIAS].model,
    )
    before = 0
    if split_name == "test":
        permitted = check_test_run(io, git, loaded, mode="llm", real=live, reason=reason)
        if permitted.pin.router_prompt_version != ROUTER_PROMPT_VERSION:
            raise Refused("the router prompt is not the pinned version")
        if committed_llm_dev_run(io, git.root, loaded.version, fingerprint.sha256()) is None:
            raise Refused(
                "no complete live LLM dev run with this fingerprint is committed: run dev first"
            )
        before = permitted.before
    cap = CostCap(cap=max_usd)
    seen: list[list[str]] = [[]]
    """The gateway's captured `llm_call` lines; set to the capture's own list once it opens."""

    def records(trace: str | None) -> CallRecord | None:
        return record_for(seen[0], trace)

    def gateway_for(provider: Provider) -> CappedGateway:
        return CappedGateway(
            provider,
            catalog,
            attempt_timeout_s=settings.llm_attempt_timeout_seconds,
            deadline_s=settings.llm_deadline_seconds,
            retry_policy=settings.llm_retry_policy(),
            max_repairs=settings.llm_max_repairs,
            cap=cap,
            records=records,
        )

    # The reservation does not depend on the provider: the worst case is computed on the dry one,
    # and a paid client is built only after the spend check and the typed cap (below), so a
    # refusal never leaves one open.
    gateway = gateway_for(DryRunRouterProvider())
    worst = [
        gateway.reservation(
            build_router_request(r.text, business_id=r.business_id, trace_id=row_trace_id(r.id)),
            IntentChoice,
        ).usd
        for r in rows
    ]
    mode = (
        "LIVE: paid calls."
        if live
        else "DRY RUN: canned fake answers; nothing is sent, and no number is a result."
    )
    print(
        f"{loaded.dataset.manifest.dataset} {loaded.version}, split {split_name}: {len(rows)}"
        f" rows to the router's LLM ({ROUTER_ALIAS} → {fingerprint.router_model}, prompt"
        f" {ROUTER_PROMPT_VERSION}).\nMode: {mode}\nWorst case at list prices, every retry and"
        f" repair included: {usd(sum(worst, Decimal(0)))} (largest single call"
        f" {usd(max(worst))})." + ("" if max_usd is None else f" Cap: {usd(max_usd)}."),
        flush=True,
    )
    if live:
        if max_usd is None:
            raise Refused("--live requires --max-usd")
        print(spend_lines(io, git, f"live llm {split_name}", max_usd))
        if not confirmed(io.stdin, cap_text or "", stdout_is_terminal=io.stdout_is_terminal()):
            raise Refused("not confirmed")
    started = io.now()
    name = folder_name("llm", real=live, started=started, split=split_name)
    out = run_dir(git.root, loaded.version, name, real=live)
    out.mkdir(parents=True, exist_ok=False)
    if split_name == "test":
        append_ledger(
            git.root / LEDGER_PATH,
            RunEntry(
                dataset=loaded.dataset.manifest.dataset,
                version=loaded.version,
                freeze_sha256=freeze,
                mode="llm",
                fingerprint_sha256=fingerprint.sha256(),
                run_id=name,
                commit=git.commit,
                started_at=started,
                rerun_reason=reason,
            ),
        )
    written: list[ResultLine] = []
    with (
        (out / RESULTS_FILE).open("x", encoding="utf-8") as results,
        (out / LLM_LOG_FILE).open("x", encoding="utf-8") as log,
        captured(gateway_logger, log) as capture,
    ):
        seen[0] = capture.lines

        def sink(line: ResultLine) -> None:
            written.append(line)
            results.write(line.model_dump_json() + "\n")
            results.flush()

        try:
            if live:  # built here, so `_llm`'s finally always closes it
                gateway = gateway_for(io.build_provider(settings))
            outcome = asyncio.run(_llm(rows, gateway, cap, loaded, io, sink, records))
        except Exception as error:  # an outage or a bug: the class name only
            outcome = LlmRun(status="failed", stop=f"{type(error).__name__} (message not shown)")
    outcome = status_on_split(outcome, written, split_name)
    info = IntentRunInfo(
        kind="llm",
        run_id=name,
        status=outcome.status,
        stop=outcome.stop,
        dry_run=not live,
        dry_reason=None if live else "fake provider",
        environment=settings.environment,
        dataset=loaded.dataset.manifest.dataset,
        dataset_version=loaded.version,
        dataset_stage=loaded.dataset.manifest.stage,
        freeze_sha256=freeze,
        dataset_committed=loaded.committed,
        split=split_name,
        rows=len(rows),
        commit=git.commit,
        uncommitted_changes=git.uncommitted_changes,
        started_at=started,
        ended_at=io.now(),
        fingerprint=fingerprint,
        fingerprint_sha256=fingerprint.sha256(),
        llm_provider=settings.llm_provider if live else "fake",
        router_alias=ROUTER_ALIAS,
        confidence=str(DEFAULT_CONFIDENCE),
        bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
        bootstrap_resamples=DEFAULT_RESAMPLES,
        cost=cost_info(cap),
        test_rerun_reason=reason if split_name == "test" else None,
        test_runs_before=before,
    )
    write_json(out / RUN_FILE, info)
    summarized = info.status != "failed" and write_summary(
        out, info, written, summary_context(loaded, io)
    )
    print(
        f"llm {info.status}"
        + (f" ({info.stop})" if info.stop else "")
        + f"; spend {usd(cap.total)}. Written to {out}"
        + (
            ". Commit this folder on m4/close-out before the next live run."
            if live
            else " (DRY RUN: git ignores it)."
        )
    )
    return out, exit_code(info, summarized)


async def _llm(
    rows: Sequence[EvalRow],
    gateway: CappedGateway,
    cap: CostCap,
    loaded: Loaded,
    io: Io,
    sink: Callable[[ResultLine], None],
    records: Callable[[str], CallRecord | None],
) -> LlmRun:
    try:
        return await run_llm_rows(
            rows,
            LLMIntentFallback(gateway),
            cap,
            policies=loaded.policies,
            records=records,
            clock=io.clock,
            sink=sink,
            progress=lambda line: print(line, flush=True),
        )
    finally:
        await gateway.aclose()
