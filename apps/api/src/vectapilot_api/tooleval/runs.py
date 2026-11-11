"""The harness commands (M5 PR 6): `run`, `summarize` and `label-sheet`.

- **`run --split dev|test`** runs every scenario of the split through the agent (`turns.py`) and
  writes a folder: `run.json`, `results.jsonl` (ids only), `replies.jsonl`, `llm_call_log.jsonl`
  (the gateway's metadata-only records) and, once H2 is written, `summary.md`. Dry unless `--live
  --max-usd X` (`harness.py`). The model is the agent's own gateway as a `CappedGateway`: every
  step's worst case is reserved before it is sent, and `CapReached` stops the run (the scenarios not
  reached are `not_run`). Test follows the discipline in `harness.py`: the ledger entry is
  appended before the first scenario runs.
- **Stops:** a cross-business leak stops the run at once (exit 4; nothing after it runs); H1 not
  written yet (`run_turn` raises NotImplementedError, which the service turns into an
  `agent_error` hand-off) stops it after the first scenario (`failed`); any other exception
  `failed`. The sandbox of every business is reset once more at the end.
- **`summarize RUN_DIR`** (re)writes the summary from the folder, with shesha's labels if present.
  While H2 is unwritten it writes none and says so; the results are kept.
- **`label-sheet RUN_DIR`** prints, to this terminal only, each reply with its scenario's message
  and expectation, and writes `labels.jsonl` (one `false_success_claim: null` line per reply) if
  the folder has none: shesha sets each label, then runs `summarize`.

Exit codes: 0 complete · 1 refused or failed · 3 partial · 4 a cross-business leak.
"""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final, TextIO, cast

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from vectapilot_api.agent.profiles import (
    AgentConfigError,
    AgentProfile,
    build_profiles,
    business_types,
)
from vectapilot_api.agent.prompt import AGENT_ALIAS, AGENT_PROMPT_VERSION
from vectapilot_api.agent.responses import AgentRequest
from vectapilot_api.agent.service import AgentRun, AgentService
from vectapilot_api.agent.steps import build_agent_step
from vectapilot_api.agent.types import LoopLimits
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, create_api_engine
from vectapilot_api.evalrun.budget import CappedGateway, CapReached, CostCap, usd
from vectapilot_api.evalrun.intervals import DEFAULT_CONFIDENCE
from vectapilot_api.evalrun.ledger import LedgerEntry, append_ledger
from vectapilot_api.evalrun.report import terminal_text
from vectapilot_api.gateway.catalog import Catalog, load_catalog
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import CallRecord
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.llm_cli import captured, confirmed
from vectapilot_api.main import build_action_services
from vectapilot_api.measure.records import RecordError, parse_llm_call_lines
from vectapilot_api.tooleval.harness import (
    Io,
    Loaded,
    Refused,
    check_settings,
    check_test_run,
    cost_info,
    load,
    require_frozen,
    spend_lines,
    synthetic_everywhere,
)
from vectapilot_api.tooleval.ledger import LEDGER_PATH
from vectapilot_api.tooleval.plans import DEFAULT_PLANS, PlanProvider, load_plans
from vectapilot_api.tooleval.runinfo import (
    LABELS_FILE,
    LLM_LOG_FILE,
    REPLIES_FILE,
    RESULTS_FILE,
    RUN_FILE,
    SUMMARY_MD,
    Fingerprint,
    LabelLine,
    Limits,
    ReplyLine,
    ResultLine,
    RunStatus,
    SplitName,
    ToolRunInfo,
    folder_name,
    read_labels,
    read_replies,
    read_results,
    read_run,
    run_dir,
    write_json,
)
from vectapilot_api.tooleval.scenarios import Scenario
from vectapilot_api.tooleval.summary import Context, render
from vectapilot_api.tooleval.turns import (
    Bench,
    LeakLog,
    NoAnswers,
    leak_checked,
    not_run,
    reset,
    run_scenario,
)
from vectapilot_api.tools.executor import ToolExecutor, TransactionSource
from vectapilot_api.tools.files import BusinessTools

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_PARTIAL: Final = 3
EXIT_LEAK: Final = 4
H1_MISSING: Final = (
    "H1 (run_turn) is not written yet: every turn is an agent_error hand-off, so the run stopped"
    " after its first scenario (guide: docs/learning/m5-agent-loop.md)"
)
H2_MISSING: Final = (
    "summary not written: H2 is not written yet (tooleval/metrics.py; guide"
    " docs/learning/m5-metrics.md). The results and run.json are kept: run `summarize` on this"
    " folder once it is"
)


# ── The fingerprint ──────────────────────────────────────────────────────────────────────────


class _SchemaOnly:
    """A `TransactionSource` for building a step schema, which reads configuration only."""

    def business_transaction(self, business_id: str, **_: bool) -> object:
        raise RuntimeError("a schema is built without the database")


def schema_sha256(tools: BusinessTools) -> str:
    """SHA-256 of the business's `AgentStep` JSON schema (canonical JSON)."""
    executor = ToolExecutor(cast(TransactionSource, _SchemaOnly()), tools.config)
    schema = build_agent_step(executor).model_json_schema()
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_fingerprint(
    loaded: Loaded, settings: Settings, catalog: Catalog, plans_sha256: str | None
) -> Fingerprint:
    businesses = loaded.dataset.manifest.businesses
    return Fingerprint(
        freeze_sha256=loaded.freeze_sha256,
        agent_prompt_version=AGENT_PROMPT_VERSION,
        alias=AGENT_ALIAS,
        catalog_version=catalog.catalog_version,
        model=catalog.aliases[AGENT_ALIAS].model,
        step_schemas={b: schema_sha256(loaded.dataset.sandbox[b]) for b in businesses},
        sandbox={b: loaded.dataset.sandbox_pins[b] for b in businesses},
        limits=Limits(
            max_steps=settings.agent_max_steps,
            turn_timeout_s=settings.agent_turn_timeout_seconds,
            attempt_timeout_s=settings.agent_llm_attempt_timeout_seconds,
            deadline_s=settings.agent_llm_deadline_seconds,
            max_retries=settings.llm_max_retries,
            max_repairs=settings.llm_max_repairs,
            action_lifetime_s=settings.action_lifetime_seconds,
        ),
        plans_sha256=plans_sha256,
    )


# ── The records ──────────────────────────────────────────────────────────────────────────────


class Records:
    """The gateway's captured `llm_call` lines: each call's record for the cap (in call order per
    trace id: every step of a turn shares the turn's trace id), and each turn's cost."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._used: Counter[str] = Counter()

    def _of(self, trace: str) -> list[CallRecord]:
        try:
            return [r for r in parse_llm_call_lines(self.lines) if r.trace_id == trace]
        except RecordError:
            return []

    def next_for(self, trace: str | None) -> CallRecord | None:
        """The oldest record of `trace` not handed out yet (`CappedGateway` settles each call
        right after it ends, so that is this call's record)."""
        if trace is None:
            return None
        found = self._of(trace)
        index = self._used[trace]
        if index >= len(found):
            return None
        self._used[trace] += 1
        return found[index]

    def cost_of(self, trace: str) -> tuple[Decimal | None, bool]:
        found = self._of(trace)
        if not found:
            return None, True
        return sum((r.cost_usd for r in found), Decimal(0)), all(r.cost_complete for r in found)


# ── run ──────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    status: RunStatus
    stop: str | None = None
    leak: bool = False


def _split(loaded: Loaded, split: str) -> SplitName:
    if split not in ("dev", "test"):
        raise Refused("--split is dev or test")
    if not loaded.split_rows(split):
        raise Refused(f"no scenarios on the {split} split: nothing to run")
    return "dev" if split == "dev" else "test"


def run_command(
    io: Io,
    *,
    dataset: Path | None,
    corpus: Path | None,
    sandbox: Path | None,
    split: str,
    live: bool,
    max_usd: Decimal | None,
    cap_text: str | None,
    reason: str | None,
    plans: Path | None,
) -> tuple[Path, int]:
    git = io.git()
    loaded = load(dataset, corpus, sandbox, git, io.head_file, live=live)
    freeze = require_frozen(loaded)
    if live and not synthetic_everywhere(loaded):
        raise Refused("--live needs every manifest to say synthetic: true")
    split_name = _split(loaded, split)
    scenarios = loaded.split_rows(split_name)
    settings = io.load_settings(live)
    check_settings(settings, live=live)
    tools = {b: loaded.dataset.sandbox[b] for b in loaded.dataset.manifest.businesses}
    try:
        types = business_types(tools, loaded.corpus_dir)
    except AgentConfigError as error:
        raise Refused(f"the agent cannot be configured: {error}") from None
    if split_name == "test" and not live:
        raise Refused("dry runs are refused on the test split: rehearse on dev")
    catalog = load_catalog()
    plan_file, plans_sha = (None, None) if live else load_plans(plans or git.root / DEFAULT_PLANS)
    fingerprint = make_fingerprint(loaded, settings, catalog, plans_sha)
    before = 0
    if split_name == "test":
        before = check_test_run(
            io, git, loaded, fingerprint=fingerprint.sha256(), live=live, reason=reason
        )
    owner = io.owner_url()
    if owner is None:
        raise Refused("the sandbox reset needs the owner's URL (VECTAPILOT_MIGRATION_DATABASE_URL)")
    mode = "LIVE: paid calls." if live else "DRY RUN: a scripted fake model; no number is a result."
    print(
        f"{loaded.dataset.manifest.dataset} {loaded.version}, split {split_name}:"
        f" {len(scenarios)} scenarios through the agent ({AGENT_ALIAS} → {fingerprint.model},"
        f" prompt {AGENT_PROMPT_VERSION}), router=none (safety floor not exercised), up to"
        f" {settings.agent_max_steps} steps each.\nMode: {mode}"
        + ("" if max_usd is None else f"\nCap: {usd(max_usd)}; every step is reserved first."),
        flush=True,
    )
    if live:
        if max_usd is None:
            raise Refused("--live requires --max-usd")
        print(spend_lines(io, git, f"live agent {split_name}", max_usd))
        if not confirmed(io.stdin, cap_text or "", stdout_is_terminal=io.stdout_is_terminal()):
            raise Refused("not confirmed")
    started = io.now()
    name = folder_name(live=live, started=started, split=split_name)
    out = run_dir(git.root, loaded.version, name, live=live)
    out.mkdir(parents=True, exist_ok=False)
    if split_name == "test":
        append_ledger(
            git.root / LEDGER_PATH,
            LedgerEntry(
                dataset=loaded.dataset.manifest.dataset,
                version=loaded.version,
                freeze_sha256=freeze,
                config_fingerprint=fingerprint.sha256(),
                run_id=name,
                commit=git.commit,
                started_at=started,
                rerun_reason=reason,
            ),
        )
    cap = CostCap(cap=max_usd)
    records = Records()
    written: list[ResultLine] = []
    with (
        (out / RESULTS_FILE).open("x", encoding="utf-8") as results,
        (out / REPLIES_FILE).open("x", encoding="utf-8") as replies,
        (out / LLM_LOG_FILE).open("x", encoding="utf-8") as log,
        captured(gateway_logger, log) as capture,
    ):
        records.lines = capture.lines

        def sink(line: ResultLine, reply: ReplyLine | None) -> None:
            written.append(line)
            results.write(line.model_dump_json() + "\n")
            results.flush()
            if reply is not None:
                replies.write(reply.model_dump_json() + "\n")
                replies.flush()

        provider: Provider = (
            PlanProvider(plan_file) if plan_file is not None else io.build_provider(settings)
        )
        run = _Run(io, settings, catalog, cap, records, provider, owner, name, tools, types)
        try:
            outcome = asyncio.run(run.all(scenarios, sink))
        except Exception as error:  # an outage or a bug: the class name only
            outcome = Outcome("failed", f"{type(error).__name__} (message not shown)")
    info = ToolRunInfo(
        run_id=name,
        status=outcome.status,
        stop=outcome.stop,
        leak=outcome.leak,
        dry_run=not live,
        dry_reason=None if live else "scripted fake model (fixture plans)",
        environment=settings.environment,
        dataset=loaded.dataset.manifest.dataset,
        dataset_version=loaded.version,
        dataset_stage=loaded.dataset.manifest.stage,
        freeze_sha256=freeze,
        dataset_committed=loaded.committed,
        split=split_name,
        scenarios=len(scenarios),
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
    summarized = info.status != "failed" and write_summary(out, info, written, io, None)
    print(
        f"run {info.status}"
        + (f" ({info.stop})" if info.stop else "")
        + f"; spend {usd(cap.total)}. Written to {out}"
        + (
            ". Commit this folder on m5/close-out before the next live run."
            if live
            else " (DRY RUN: git ignores it)."
        )
    )
    return out, exit_code(info, summarized)


def exit_code(info: ToolRunInfo, summarized: bool) -> int:
    if info.leak:
        return EXIT_LEAK
    if info.status == "partial":
        return EXIT_PARTIAL
    return EXIT_OK if summarized and info.status == "complete" else EXIT_FAILED


@dataclass
class _Run:
    """One run's live objects (built inside the event loop by `all`)."""

    io: Io
    settings: Settings
    catalog: Catalog
    cap: CostCap
    records: Records
    provider: Provider
    owner_url: str
    run_id: str
    tools: Mapping[str, BusinessTools]
    types: Mapping[str, BusinessType]

    async def all(
        self, scenarios: Sequence[Scenario], sink: Callable[[ResultLine, ReplyLine | None], None]
    ) -> Outcome:
        settings = self.settings
        gateway = CappedGateway(
            self.provider,
            self.catalog,
            attempt_timeout_s=settings.agent_llm_attempt_timeout_seconds,
            deadline_s=settings.agent_llm_deadline_seconds,
            retry_policy=settings.llm_retry_policy(),
            max_repairs=settings.llm_max_repairs,
            cap=self.cap,
            records=self.records.next_for,
        )
        app_engine = create_api_engine(str(settings.database_url.get_secret_value()))
        owner = create_async_engine(self.owner_url, poolclass=NullPool, hide_parameters=True)
        seeds = {b: t.seed for b, t in self.tools.items()}
        try:
            database = Database(app_engine)
            await database.verify_role()
            configs = {b: t.config for b, t in self.tools.items()}
            store, confirm = build_action_services(
                database, configs, lifetime_s=settings.action_lifetime_seconds
            )
            profiles: Mapping[str, AgentProfile] = build_profiles(database, self.tools, self.types)

            async def respond(
                scenario: Scenario, request: AgentRequest, trace: str, log: LeakLog
            ) -> AgentRun:
                service = AgentService(
                    profiles,
                    gateway=gateway,
                    store=store,
                    answers=NoAnswers(),
                    limits=LoopLimits(max_steps=settings.agent_max_steps),
                    turn_timeout_s=settings.agent_turn_timeout_seconds,
                    loop=leak_checked(self.io.loop, scenario.business_id, log),
                    propagate=(CapReached,),
                )
                return await service.respond_unrouted(scenario.business_id, request, trace_id=trace)

            bench = Bench(
                run_id=self.run_id,
                owner=owner,
                respond=respond,
                confirm=confirm,
                seeds=seeds,
                cost_of=self.records.cost_of,
                clock=self.io.clock,
            )
            return await self._scenarios(bench, scenarios, sink)
        finally:
            try:
                for seed in seeds.values():  # leave every sandbox as seeded
                    await reset(owner, seed)
            finally:
                await gateway.aclose()
                await owner.dispose()
                await app_engine.dispose()

    async def _scenarios(
        self,
        bench: Bench,
        scenarios: Sequence[Scenario],
        sink: Callable[[ResultLine, ReplyLine | None], None],
    ) -> Outcome:
        for index, scenario in enumerate(scenarios):
            if isinstance(self.provider, PlanProvider):
                self.provider.begin(scenario.id)
            try:
                done = await run_scenario(bench, scenario)
            except CapReached as reached:
                for left in scenarios[index:]:
                    sink(not_run(left), None)
                return Outcome("partial", str(reached))
            if done.leaks:
                sink(done.line, None)
                for left in scenarios[index + 1 :]:
                    sink(not_run(left), None)
                return Outcome(
                    "failed",
                    f"cross-business leak in scenario {scenario.id}: {'; '.join(done.leaks)}",
                    leak=True,
                )
            sink(done.line, done.reply)
            if done.error == NotImplementedError.__name__:
                for left in scenarios[index + 1 :]:
                    sink(not_run(left), None)
                return Outcome("failed", H1_MISSING)
            print(f"scenario {index + 1} of {len(scenarios)}: {scenario.id}", flush=True)
            if self.cap.stop is not None:
                for left in scenarios[index + 1 :]:
                    sink(not_run(left), None)
                return Outcome("partial", self.cap.stop)
        return Outcome("complete")


# ── summarize and label-sheet ────────────────────────────────────────────────────────────────


def write_summary(
    folder: Path,
    info: ToolRunInfo,
    lines: Sequence[ResultLine],
    io: Io,
    labels: Sequence[LabelLine] | None,
) -> bool:
    """Write summary.md (rewritten each time: it is derived), or say why not. True when written."""
    context = Context(businesses=list(info.fingerprint.sandbox), metrics=io.metrics, labels=labels)
    try:
        text = render(info, lines, context)
    except NotImplementedError:
        print(H2_MISSING)
        return False
    (folder / SUMMARY_MD).write_text(text, encoding="utf-8")
    print(text)
    return True


def summarize_command(io: Io, folder: Path) -> int:
    info = read_run(folder)
    lines = read_results(folder)
    if len(lines) != info.scenarios:
        raise Refused(
            f"{RESULTS_FILE} has {len(lines)} line(s), run.json says {info.scenarios} scenario(s)"
        )
    summarized = write_summary(folder, info, lines, io, read_labels(folder))
    return exit_code(info, summarized)


def _terminal(text: str, *, multiline: bool = True) -> str:
    return terminal_text(text, multiline=multiline)


def label_sheet_command(
    io: Io,
    folder: Path,
    *,
    dataset: Path | None,
    corpus: Path | None,
    sandbox: Path | None,
    out: TextIO,
) -> int:
    """Print the label sheet (module docstring) and write the labels template if it is missing.
    Every check runs before the first line is printed."""
    info = read_run(folder)
    lines = read_results(folder)
    replies = {r.id: r for r in read_replies(folder)}
    git = io.git()
    loaded = load(dataset, corpus, sandbox, git, io.head_file, live=False)
    if (loaded.version, loaded.freeze_sha256) != (info.dataset_version, info.freeze_sha256):
        raise Refused("the dataset is not the frozen version this run used")
    scenarios = {s.id: s for s in loaded.dataset.scenarios}
    shown = [x for x in lines if x.outcome == "reply"]
    if any(x.id not in replies or x.id not in scenarios for x in shown):
        raise Refused(f"a reply or its scenario is missing ({REPLIES_FILE} or the dataset)")
    print(
        f"Label sheet for run {info.run_id}: printed to this terminal only, never written to a"
        " file or a log."
        + (" DRY RUN: scripted replies, never a result." if info.dry_run else "")
        + "\nFor each reply: does it claim that something was booked, moved or sent which the"
        " database does not hold (results.jsonl's write rows)? Set false_success_claim true or"
        f" false in {folder / LABELS_FILE}, with labelled_by and labelled_on.\n",
        file=out,
    )
    for line in shown:
        scenario = scenarios[line.id]
        expect = scenario.expect
        print(f"## {line.id} ({line.business_id}, {line.language_group})", file=out)
        print(f"message: {_terminal(scenario.message, multiline=False)}", file=out)
        print(
            f"expected: {expect.kind}"
            + (f" {expect.tool.value} {expect.arguments}" if expect.tool is not None else "")
            + f"; write rows held: {len(line.write_rows)}",
            file=out,
        )
        for text_line in _terminal(replies[line.id].text).split("\n"):
            print(f"> {text_line}", file=out)
        print("", file=out)
    path = folder / LABELS_FILE
    if not path.is_file():
        with path.open("x", encoding="utf-8") as handle:
            for line in shown:
                handle.write(
                    LabelLine(id=line.id, false_success_claim=None).model_dump_json() + "\n"
                )
        print(f"Wrote {path}: {len(shown)} line(s), each false_success_claim null.", file=out)
    return EXIT_OK
