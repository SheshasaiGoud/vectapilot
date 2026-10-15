"""`vectapilot-llm`: the M1 measurement CLI, an I/O shell around the gateway (PR 8; ADR 0006, Cost;
ADR 0007, Measurement).

    uv run vectapilot-llm                       # dry run: fake provider, canned replies, no key, $0
    make llm-demo                               # the same, from the repository root
    uv run vectapilot-llm --live --max-usd X    # shesha only, in her own terminal; agents never

What a run does, in order:

1. Reads the arguments. `--live` without `--max-usd`, or a cap outside 0 < X <= 1.00, stops here,
   before anything else is read or built.
2. Reads the commit and whether the working tree has changes (read-only git commands; the run
   directories under `evals/reports/` do not count as changes), then the dataset
   (`evals/datasets/gateway_smoke/v0.1/` by default), checked against the catalog and the schemas,
   and refused if two cases send the same prompt on one alias (they would count twice toward n;
   the message names the case ids, never the prompt). Then whether the commit names the dataset:
   its two files must be, byte for byte, the files at the same paths in the commit
   (`dataset_at_commit`). A dataset outside the repository, untracked, git-ignored or changed is
   still run, but its summary says so and it cannot feed the note.
3. Loads Settings. A dry run overrides them to the fake provider and drops any key, so it needs no
   key and no setting can make it call a model. `--live` needs Settings to say `llm_provider`
   "anthropic", with the key in the process environment (Settings refuses one in `.env`). This
   module never reads, prints or logs the key.
4. Prints the plan: the planned calls and the worst-case cost of each alias and of the whole plan,
   at the catalog's list prices (`measure.budget` has the formula).
5. `--live` only: asks for the cap to be typed again, with stdin and stdout both at a terminal.
   Piped input, redirected output (the plan would go unseen), an empty line or any other text
   cancels the run before any provider exists: nothing is called. This guards against mistakes,
   not against automation: a program can open a pseudo-terminal and type the cap. What keeps an
   agent from making a live run is that the key never enters an agent's environment (PR 9: the key
   is entered in a separate terminal); the Console's monthly spend limit is the backstop.
6. Builds the gateway with `build_gateway`. A dry run hands in its own provider: a `FakeProvider`
   re-scripted before each call with that case's canned reply (and canned repair reply). So the
   Anthropic adapter is never built without `--live` (D9), and never with `--live` but no cap.
7. Runs the calls one by one: every case once, then any repeats. Before each call it reserves the
   call's worst case and stops before any call whose reservation would cross the cap; a
   cost-incomplete call keeps its reservation. Each call gets a fresh `trace_id`. A `GatewayError`
   is an outcome to measure (the call still wrote its record), except the `STOPPING_OUTCOMES`: a
   spend limit, a billing or an authentication failure (no further call can succeed) and an
   invalid request (the request itself is wrong). Those stop the run after the call, as do a
   response from the `us` geo (`STOPPING_GEO`: a setup error, the workspace default must be
   global; a missing geo does not stop), a call beyond its worst-case bound, a call without its
   record, an unexpected error and Ctrl-C (`_stop_after`, `_call`).
8. Reads the gateway's `llm_call` log lines back (a handler on the gateway's logger, formatted by
   the app's `JsonFormatter`) and FAILS the run unless it read exactly one record per call made.
9. Summarises (`measure.summary`), prints the Markdown and writes the summary. The summary names
   the gateway limits the run used (`RunLimits`, from Settings and the catalog) and what stopped
   it (`stop_detail`: the "Run stopped" line, which holds only case ids, dollar amounts, outcome
   names, exception type names and "inference_geo us"). A run that did not make every planned call
   is marked partial; only a complete live run can feed the "what I measured" note, and never one
   that a stop ended, even after its last planned call (`measure.summary` has the rules).

The cap is per run: nothing limits how many runs are made, so the Console's monthly spend limit is
the only cap across runs. It holds while the worst-case bound's assumptions hold (`measure.budget`).
If one fails, that one call may cross the cap: a call billed beyond its token bound or answered
from the `us` geo then stops the run; a call with no geo reported only keeps its reservation, and
the run carries on.

Where the output goes (relative to the repository root):

- dry run: `evals/reports/tmp/<dataset>/<version>/<UTC start>-fake/`. Git ignores
  `evals/reports/tmp/`: dry runs are never committed.
- live run: `evals/reports/<dataset>/<version>/<UTC start>-live/`. Live runs are committed, so
  their numbers last (ADR 0007): a partial one as evidence only; only a complete one feeds the note.

Each run directory holds `summary.md` and `summary.json`, written at the end, and the two inputs the
summary was built from: `calls_made.jsonl` (case, repeat and trace id per call) and
`llm_call_log.jsonl` (the gateway's log lines as captured: metadata only, ADR 0007). The inputs are
written as the run goes, flushed line by line, and each call is listed before it is sent. So a run
cut off by a failed count check, an error or a killed process (a closed terminal sends SIGHUP)
still shows every call that was made, and so may have been billed.

Exit codes: 0 a complete run; 1 failed or refused; 2 invalid arguments; 3 a partial run (stopped
before its last planned call), summarised and marked partial. A run stopped by its last planned
call made every call, so it is not partial and exits 0; the summary still keeps it out of the note
(decided by shesha, 2026-10-15).
"""

import argparse
import asyncio
import logging
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Final, TextIO

from pydantic import BaseModel, ValidationError

from vectapilot_api.config import Settings
from vectapilot_api.gateway.catalog import Catalog, load_catalog
from vectapilot_api.gateway.errors import GatewayError
from vectapilot_api.gateway.gateway import LLMGateway, build_gateway
from vectapilot_api.gateway.gateway import logger as gateway_logger
from vectapilot_api.gateway.providers.base import ProviderRequest, ProviderResponse
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import CallOutcome, CallRecord, LLMRequest, Message
from vectapilot_api.log import JsonFormatter
from vectapilot_api.measure.budget import (
    MAX_USD_CEILING,
    Ledger,
    Reservation,
    bound_held,
    reserve,
    total_usd,
)
from vectapilot_api.measure.dataset import (
    CASES_FILE,
    MANIFEST_FILE,
    DatasetError,
    DatasetManifest,
    SmokeCase,
    check_dataset,
    load_cases,
    load_manifest,
)
from vectapilot_api.measure.records import (
    CallMade,
    RecordError,
    join_records,
    parse_llm_call_lines,
)
from vectapilot_api.measure.schemas import SCHEMAS
from vectapilot_api.measure.summary import (
    RunInfo,
    RunLimits,
    RunStop,
    SummaryInputError,
    check_distinct_prompts,
    render_json,
    render_markdown,
    summarise_run,
)

DATASET_DIR: Final = Path("evals", "datasets", "gateway_smoke", "v0.1")
"""The default dataset, relative to the repository root."""

DATASET_FILES: Final = (MANIFEST_FILE, CASES_FILE)
"""The files of a dataset version that a run reads: the ones the commit must name."""

REPORTS_DIR: Final = Path("evals", "reports")
"""Live runs, relative to the repository root: meant to be committed. Not a change to the code, so
`read_git` leaves it out of the working-tree check."""

RUNBOOK: Final = 'docs/plans/m1-pr-checks.md, PR 9, "Live run (shesha only)"'
"""The live run's step-by-step checklist (a test checks that the heading is there)."""

DRY_RUN_REPORTS_DIR: Final = REPORTS_DIR / "tmp"
"""Dry runs, relative to the repository root: in .gitignore, never committed."""

CALLS_MADE_FILE: Final = "calls_made.jsonl"
LLM_CALL_LOG_FILE: Final = "llm_call_log.jsonl"
SUMMARY_MD_FILE: Final = "summary.md"
SUMMARY_JSON_FILE: Final = "summary.json"

MAX_REPEATS: Final = 3
OPERATION: Final = "gateway-measure"
"""The `operation` of every call the CLI makes, so its records are told apart in any log."""

_NO_FURTHER_CALL: Final = "no further call can succeed"

STOPPING_OUTCOMES: Final[Mapping[CallOutcome, str]] = MappingProxyType(
    {
        CallOutcome.SPEND_LIMIT: _NO_FURTHER_CALL,
        CallOutcome.BILLING: _NO_FURTHER_CALL,
        CallOutcome.AUTH_FAILED: _NO_FURTHER_CALL,
        CallOutcome.INVALID_REQUEST: "the request was rejected as invalid",
    }
)
"""Outcomes that stop the run after the call instead of being measured, each with the reason its
stop detail gives. The account failures: no further call can succeed. `invalid_request` (decided by
shesha, 2026-10-15): our own request is wrong, so the rest of the plan would repeat the mistake."""

STOPPING_GEO: Final = "us"
"""A record with this `inference_geo` stops the run after the call (decided by shesha, 2026-10-15):
a setup error, as the workspace default must be global (ADR 0006). The call is cost-incomplete and
keeps its reservation. A missing geo (None) does not stop the run. The record holds the last
response's geo, so an earlier attempt's `us` under a later `global` would show only as
cost-incomplete; not expected, as the CLI never sets a geo and the workspace default applies to
every request."""

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_PARTIAL: Final = 3

_USD_TEXT: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
"""`--max-usd` as plain digits: no sign, exponent, NaN or infinity."""

_TYPE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
"""An exception type name as a stop detail shows it: a plain identifier, at most 100 characters."""


# ── The outside world (replaced in tests) ────────────────────────────────────────────────────


class GitError(RuntimeError):
    """The commit or the working tree's state could not be read."""


@dataclass(frozen=True)
class GitInfo:
    root: Path
    """The repository root: the dataset and the reports are found from here."""
    commit: str
    uncommitted_changes: bool
    """Any change git reports outside `evals/reports/`, untracked files included: the commit then
    does not name the code. Whether it names the dataset is `dataset_at_commit`'s question: git
    status does not see a dataset outside the repository, git-ignored or under `evals/reports/`."""


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        raise GitError("git was not found on PATH: run inside the repository")
    return executable


def _git(args: Sequence[str]) -> str:
    # Read-only commands with fixed arguments, no shell (S603: the arguments are ours).
    done = subprocess.run(  # noqa: S603
        [_git_executable(), *args], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise GitError(
            f"`git {' '.join(args)}` failed (exit {done.returncode}): run inside the repository"
        )
    return done.stdout


STATUS_ARGS: Final = (
    "--no-optional-locks",
    "status",
    "--porcelain",
    "--",
    ":(top)",
    f":(top,exclude){REPORTS_DIR.as_posix()}",
)
"""`git status` of the whole tree except the run directories: every live run leaves one behind,
which would otherwise mark every later run "uncommitted changes" until it is committed or deleted.
`--no-optional-locks` keeps `git status` from refreshing the index file (read-only)."""


def read_git(git: Callable[[Sequence[str]], str] = _git) -> GitInfo:
    """The repository root, the commit and whether the working tree has changes (read-only)."""
    root = git(["rev-parse", "--show-toplevel"]).strip()
    commit = git(["rev-parse", "HEAD"]).strip()
    status = git(STATUS_ARGS)
    return GitInfo(root=Path(root), commit=commit, uncommitted_changes=bool(status.strip()))


def read_head_file(root: Path, relative: PurePosixPath) -> bytes | None:
    """The file at `relative` (from the repository root) as committed in HEAD, byte for byte; None
    when HEAD has no such file or git cannot read it (read-only: `git cat-file`)."""
    # Fixed arguments plus one path argument, no shell (S603: nothing here is parsed by a shell).
    done = subprocess.run(  # noqa: S603
        [_git_executable(), "-C", str(root), "cat-file", "blob", f"HEAD:{relative}"],
        capture_output=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def dataset_at_commit(
    root: Path,
    files: Mapping[Path, bytes],
    head_file: Callable[[Path, PurePosixPath], bytes | None],
) -> bool:
    """Whether every dataset file the run read is, byte for byte, the file at the same path in the
    commit: only then do the commit and the manifest's name and version name the prompts that ran.

    One rule covers what git status cannot see: a dataset outside the repository, untracked,
    git-ignored, or changed under `evals/reports/` (left out of the working-tree check). Paths are
    resolved first, so a link inside the repository to a copy outside it does not count as inside.
    """
    top = root.resolve()
    for path, content in files.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(top):
            return False  # git is not asked: the commit cannot hold this file
        if head_file(root, PurePosixPath(resolved.relative_to(top).as_posix())) != content:
            return False
    return True


def load_settings(live: bool) -> Settings:
    """Settings for a run. A dry run is always the fake provider, without any key (D9)."""
    if live:
        return Settings()
    return Settings(llm_provider="fake", anthropic_api_key=None)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def stdout_is_terminal() -> bool:
    """Whether what the CLI prints reaches a terminal (not `> file` or `| tee`)."""
    return sys.stdout.isatty()


@dataclass(frozen=True)
class Io:
    """What the CLI reads from outside: tests pass their own, so no test runs git, reads the shell's
    environment or waits for a terminal."""

    load_settings: Callable[[bool], Settings]
    git: Callable[[], GitInfo]
    head_file: Callable[[Path, PurePosixPath], bytes | None]
    now: Callable[[], datetime]
    stdin: TextIO
    stdout_is_terminal: Callable[[], bool]


def default_io() -> Io:
    return Io(
        load_settings=load_settings,
        git=read_git,
        head_file=read_head_file,
        now=_utc_now,
        stdin=sys.stdin,
        stdout_is_terminal=stdout_is_terminal,
    )


# ── Arguments ────────────────────────────────────────────────────────────────────────────────

_DESCRIPTION = """\
Run the gateway_smoke dataset through the LLM gateway and summarise the llm_call records
(M1 measurement; ADR 0007). Without --live this is a DRY RUN: a fake provider answers with each
case's canned reply, no model is called, no key is needed and nothing is billed."""

_EPILOG = f"""\
--live is for shesha only, in her own terminal: agents never pass it. It needs Settings with
llm_provider "anthropic" and the key in the process environment, --max-usd X with
0 < X <= {MAX_USD_CEILING}, and the cap typed again with stdin and stdout at a terminal (no pipe,
no redirect). Checklist: {RUNBOOK}.

The cap is per run. Each call's worst case is reserved before the call; the run stops before any
call whose reservation would cross the cap. If an assumption of the bound fails
(measure/budget.py), that one call may cross it: a call billed beyond its token bound or
answered from the '{STOPPING_GEO}' geo then stops the run; a call with no geo reported only keeps
its reservation. Nothing limits the number of runs: the Console's monthly spend limit is
the only cap across runs.

After a call, the run also stops on outcome {", ".join(o.value for o in STOPPING_OUTCOMES)},
on a call without its record, an unexpected error or Ctrl-C. A run that a stop ended never feeds
the note.

Output (relative to the repository root):
  dry run:  {DRY_RUN_REPORTS_DIR}/<dataset>/<version>/<UTC start>-fake/  (git-ignored)
  live run: {REPORTS_DIR}/<dataset>/<version>/<UTC start>-live/  (meant to be committed)
with {SUMMARY_MD_FILE}, {SUMMARY_JSON_FILE}, {CALLS_MADE_FILE} and {LLM_CALL_LOG_FILE}.

Exit codes: 0 complete; 1 failed or refused; 2 invalid arguments; 3 partial (stopped early)."""


@dataclass(frozen=True)
class Args:
    live: bool
    max_usd: Decimal | None
    max_usd_text: str | None
    """`--max-usd` exactly as given: the text the confirmation must repeat."""
    max_calls: int | None
    repeats: int
    dataset: Path | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vectapilot-llm",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="real, billed calls to the anthropic provider (shesha only; needs --max-usd)",
    )
    parser.add_argument(
        "--max-usd",
        metavar="X",
        help=f"cost cap per run in US dollars, 0 < X <= {MAX_USD_CEILING}; required with --live,"
        " and a dry run with it rehearses the cap",
    )
    parser.add_argument("--max-calls", metavar="N", type=int, help="stop after N calls (N >= 1)")
    parser.add_argument(
        "--repeats",
        metavar="K",
        type=int,
        default=0,
        help=f"run every case K more times after the first pass (0-{MAX_REPEATS}, default 0);"
        " reported separately, never as distinct prompts",
    )
    parser.add_argument(
        "--dataset",
        metavar="DIR",
        type=Path,
        help=f"a dataset version directory (default: {DATASET_DIR} in this repository)",
    )
    return parser


def parse_args(argv: Sequence[str] | None) -> Args:
    """Parse and check the arguments; any problem exits with status 2 before anything is read."""
    parser = _parser()
    raw = parser.parse_args(argv)
    if raw.live and raw.max_usd is None:
        parser.error("--live requires --max-usd")
    max_usd = None if raw.max_usd is None else _cap(parser, raw.max_usd)
    if raw.max_calls is not None and raw.max_calls < 1:
        parser.error("--max-calls must be at least 1")
    if not 0 <= raw.repeats <= MAX_REPEATS:
        parser.error(f"--repeats must be between 0 and {MAX_REPEATS}")
    return Args(
        live=raw.live,
        max_usd=max_usd,
        max_usd_text=raw.max_usd,
        max_calls=raw.max_calls,
        repeats=raw.repeats,
        dataset=raw.dataset,
    )


def _cap(parser: argparse.ArgumentParser, text: str) -> Decimal:
    if _USD_TEXT.fullmatch(text) is None:
        parser.error("--max-usd must be a plain decimal number of US dollars, such as 0.50")
    value = Decimal(text)
    if not 0 < value <= MAX_USD_CEILING:
        parser.error(f"--max-usd must be above 0 and at most {MAX_USD_CEILING} (the hard ceiling)")
    return value


# ── The dry run's provider and the log capture ───────────────────────────────────────────────


class DryRunProvider:
    """The dry run's provider: before each call, a fresh `FakeProvider` scripted with the case's
    canned reply, then its canned repair reply if it has one. No network, no key, no cost.

    A fresh fake per call keeps the script aligned: a call that asks for more answers than its
    case has fails inside that call (`internal_error`), never by taking the next case's reply.
    """

    def __init__(self) -> None:
        self._fake = FakeProvider()

    def script(self, case: SmokeCase) -> None:
        replies = [case.fake_reply]
        if case.fake_repair_reply is not None:
            replies.append(case.fake_repair_reply)
        self._fake = FakeProvider([FakeReply(text=text) for text in replies])

    async def send(self, request: ProviderRequest) -> ProviderResponse:
        return await self._fake.send(request)

    async def aclose(self) -> None:
        await self._fake.aclose()


def _write_line(sink: TextIO, line: str) -> None:
    """One line, flushed at once: it survives the process being killed right after."""
    sink.write(f"{line}\n")
    sink.flush()


class LogCapture(logging.Handler):
    """Keeps every record the gateway's logger emits, as the app's JSON log lines, and writes each
    line through to `sink` as it is emitted."""

    def __init__(self, sink: TextIO) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self.lines.append(line)
        _write_line(self._sink, line)


@contextmanager
def captured(logger: logging.Logger, sink: TextIO) -> Iterator[LogCapture]:
    """Capture `logger`'s records (its children's too) while the block runs."""
    handler = LogCapture(sink)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


# ── The plan ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlannedCall:
    case: SmokeCase
    repeat: int
    """0 for the case's first run (a distinct prompt), 1.. for repeats."""
    schema: type[BaseModel] | None
    request: LLMRequest
    """Without a `trace_id`: a fresh one is set when the call is made."""
    reservation: Reservation


def plan_calls(
    cases: Sequence[SmokeCase], *, repeats: int, catalog: Catalog, settings: Settings
) -> tuple[PlannedCall, ...]:
    """Every call of the run, in order: each case once, then each repeat pass."""
    calls: list[PlannedCall] = []
    for repeat in range(1 + repeats):
        for case in cases:
            schema = None if case.schema_name is None else SCHEMAS[case.schema_name]
            request = LLMRequest(
                business_id=case.business_id,
                alias=case.alias,
                messages=(Message(role="user", content=case.prompt),),
                operation=OPERATION,
            )
            reservation = reserve(
                case,
                schema,
                catalog=catalog,
                max_retries=settings.llm_max_retries,
                max_repairs=settings.llm_max_repairs,
            )
            calls.append(PlannedCall(case, repeat, schema, request, reservation))
    return tuple(calls)


def run_limits(settings: Settings, catalog: Catalog) -> RunLimits:
    """The gateway limits a run uses, for its summary: read from the same Settings and catalog that
    `build_gateway` and `plan_calls` read, for a dry run and a live run alike."""
    caps = {name: alias.max_output_tokens for name, alias in catalog.aliases.items()}
    return RunLimits(
        max_retries=settings.llm_max_retries,
        max_repairs=settings.llm_max_repairs,
        attempt_timeout_s=settings.llm_attempt_timeout_seconds,
        deadline_s=settings.llm_deadline_seconds,
        backoff_base_s=settings.llm_backoff_base_seconds,
        backoff_cap_s=settings.llm_backoff_cap_seconds,
        max_output_tokens=caps,
    )


def _usd(value: Decimal) -> str:
    return "$" + format(value.normalize(), "f")


def plan_text(
    args: Args,
    manifest: DatasetManifest,
    catalog: Catalog,
    settings: Settings,
    plan: Sequence[PlannedCall],
) -> str:
    """What the run will do and what it can cost at most, printed before anything is called."""
    distinct = sum(call.repeat == 0 for call in plan)
    mode = (
        "LIVE. Real calls to the anthropic provider, billed to the key's workspace."
        if args.live
        else "DRY RUN. A fake provider answers with each case's canned reply: no model is called,"
        " no key is used, nothing is billed."
    )
    aliases = " · ".join(
        f"{name} → {alias.model} (max_output_tokens {alias.max_output_tokens})"
        for name, alias in sorted(catalog.aliases.items())
    )
    lines = [
        f"{manifest.dataset} {manifest.version}: {distinct} cases, synthetic prompts"
        f" (prompt review: {manifest.review})",
        f"Mode: {mode}",
        f"Catalog {catalog.catalog_version}: {aliases}",
        f"Limits from Settings: {settings.llm_max_retries} retries per request,"
        f" {settings.llm_max_repairs} repair; attempt timeout"
        f" {settings.llm_attempt_timeout_seconds} s, deadline {settings.llm_deadline_seconds} s",
        f"Planned calls: {len(plan)} ({distinct} distinct prompts, {len(plan) - distinct} repeats)"
        + ("" if args.max_calls is None else f"; --max-calls stops after {args.max_calls}"),
        "Worst case at the catalog's list prices, every retry and repair included"
        " (formula: measure/budget.py):",
    ]
    for alias in sorted({call.case.alias for call in plan}):
        own = [call.reservation for call in plan if call.case.alias == alias]
        lines.append(f"  {alias}: {len(own)} calls, up to {_usd(total_usd(own))}")
    largest = max(plan, key=lambda call: call.reservation.usd)
    whole = total_usd([call.reservation for call in plan])
    lines.append(
        f"  whole plan: up to {_usd(whole)}; largest single call {_usd(largest.reservation.usd)}"
        f" ({largest.case.id})"
    )
    if args.max_usd is None:
        lines.append("Cost cap: none (dry run without --max-usd).")
    else:
        lines.append(
            f"Cost cap for this run: {_usd(args.max_usd)} (--max-usd). Each call's worst case is"
            " reserved before the call; the run stops before any call whose reservation would"
            " cross the cap."
        )
        if whole > args.max_usd:
            lines.append(
                "The whole plan's worst case is above the cap: if the calls spend enough, the run"
                " stops early and is marked partial."
            )
    if args.live:
        lines.append(
            "Keep this terminal open until 'Written to' is printed. Closing it earlier ends the"
            f" run at once, with no summary: only {CALLS_MADE_FILE} (every call made) and"
            f" {LLM_CALL_LOG_FILE} remain."
        )
    return "\n".join(lines)


def confirmed(stdin: TextIO, cap_text: str, *, stdout_is_terminal: bool) -> bool:
    """True only if the cap is typed again, exactly, with stdin and stdout both at a terminal.

    A guard against mistakes, not against automation. It stops a piped or scripted answer, a plan
    printed into a file (`> file`) instead of in front of the person typing, and a mistyped cap.
    A program can still open a pseudo-terminal and type the cap. What keeps an agent from making a
    live run is that the key never enters an agent's environment (PR 9: key entry in a separate
    terminal); the Console's monthly spend limit is the backstop.
    """
    if not stdin.isatty():
        print(
            "The confirmation must be typed at a terminal, and stdin is not one.", file=sys.stderr
        )
        return False
    if not stdout_is_terminal:
        print(
            "The plan must be read at the terminal where the cap is typed, and stdout is not one"
            " (no redirect or pipe with --live).",
            file=sys.stderr,
        )
        return False
    print(
        f"To make these real, billed calls, type the cap exactly as given ({cap_text}) and press"
        " Enter. Anything else cancels: ",
        end="",
        flush=True,
    )
    try:
        answer = stdin.readline()
    except KeyboardInterrupt:
        print()
        return False
    return answer.strip() == cap_text


# ── Running the calls ────────────────────────────────────────────────────────────────────────


@dataclass
class Execution:
    """What the run did. Filled in while it runs, so it is complete even if the run is cut off."""

    sink: TextIO
    """`calls_made.jsonl`, written through: each call is listed there before it is sent."""
    calls_made: list[CallMade] = field(default_factory=list)
    stopped: RunStop | None = None
    reason: str | None = None

    def start(self, call: CallMade) -> None:
        """List a call before it is sent: if the process dies during it, it may still be billed."""
        self.calls_made.append(call)
        _write_line(self.sink, call.model_dump_json())


async def run_calls(
    settings: Settings,
    plan: Sequence[PlannedCall],
    *,
    live: bool,
    capture: LogCapture,
    ledger: Ledger,
    max_calls: int | None,
    done: Execution,
) -> None:
    """Build the gateway and make the planned calls, one by one, into `done`."""
    if live and ledger.cap is None:  # `parse_args` refuses this too: never paid calls without a cap
        raise ValueError("a live run needs a cap")
    dry_run = None if live else DryRunProvider()
    async with build_gateway(settings, provider=dry_run) as gateway:
        for number, call in enumerate(plan, start=1):
            if max_calls is not None and len(done.calls_made) >= max_calls:
                done.stopped, done.reason = "max_calls", f"--max-calls {max_calls} reached"
                return
            if not ledger.fits(call.reservation):
                done.stopped = "cost_cap"
                done.reason = (
                    f"stopped before call {number} ({call.case.id}): its worst case"
                    f" {_usd(call.reservation.usd)} on top of the {_usd(ledger.counted)} counted"
                    f" so far would cross the cap {_usd(ledger.cap or Decimal(0))}"
                )
                return
            if dry_run is not None:
                dry_run.script(call.case)
            trace_id = secrets.token_hex(16)
            done.start(CallMade(case_id=call.case.id, repeat=call.repeat, trace_id=trace_id))
            before = len(capture.lines)
            stop = await _call(gateway, call, trace_id)
            record = own_record(capture.lines[before:], trace_id)
            counted = ledger.settle(call.reservation, record)
            print(_progress(number, len(plan), call, record, counted, ledger), flush=True)
            stop = stop or _stop_after(call, record)
            if stop is not None:
                done.stopped, done.reason = stop
                return


async def _call(
    gateway: LLMGateway, call: PlannedCall, trace_id: str
) -> tuple[RunStop, str] | None:
    """Make one call. None: carry on. Otherwise why the run must stop."""
    request = call.request.model_copy(update={"trace_id": trace_id})
    try:
        if call.schema is None:
            await gateway.complete(request)
        else:
            await gateway.complete_structured(request, call.schema)
    except GatewayError:
        return None  # an outcome to measure: the call wrote its record
    except asyncio.CancelledError:
        # Ctrl-C: `asyncio.run` cancels this task. The gateway recorded the call as `cancelled`;
        # stop, and keep what was made. `uncancel` undoes the request, as this task carries on.
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        return "interrupted", f"interrupted during {call.case.id}"
    except Exception as error:
        # A bug in a provider or the gateway (recorded as `internal_error`). Only the exception's
        # type is shown: its message could hold provider text (M3 hand-over).
        return "error", f"{call.case.id}: unexpected {_type_name(error)} (internal_error)"
    return None


def _type_name(error: Exception) -> str:
    """The exception's type name when it is a plain identifier, else a placeholder. The stop detail
    must stay one short line (`RunInfo.stop_detail`): an odd name must not cost a run its summary.
    """
    name = type(error).__name__
    return name if _TYPE_NAME.fullmatch(name) else "exception (type name not shown)"


def own_record(lines: Sequence[str], trace_id: str) -> CallRecord | None:
    """The one `llm_call` record a call wrote, or None if its new log lines hold anything else."""
    try:
        records = parse_llm_call_lines(lines)
    except RecordError:
        return None
    if len(records) != 1 or records[0].trace_id != trace_id:
        return None
    return records[0]


def _stop_after(call: PlannedCall, record: CallRecord | None) -> tuple[RunStop, str] | None:
    """Why the run must stop after this call, or None. The first rule that matches is the stop
    detail: case ids, outcome names and fixed text only, never prompt or completion text."""
    if record is None:
        return "error", f"{call.case.id}: no single llm_call record (its reservation is kept)"
    reason = STOPPING_OUTCOMES.get(record.outcome)
    if reason is not None:
        return "error", f"{call.case.id}: outcome {record.outcome}, {reason}"
    if not bound_held(call.reservation, record):
        return "error", (
            f"{call.case.id}: billed beyond its worst-case bound, so the bound's assumptions"
            " (measure/budget.py) do not hold; fix the bound before another live run"
        )
    if record.inference_geo == STOPPING_GEO:
        return "error", (
            f"{call.case.id}: inference_geo {STOPPING_GEO}, the workspace default geo must be"
            " global (the call keeps its reservation)"
        )
    return None


def _progress(
    number: int,
    total: int,
    call: PlannedCall,
    record: CallRecord | None,
    counted: Decimal,
    ledger: Ledger,
) -> str:
    kind = "structured" if call.schema is not None else "plain"
    head = f"[{number:>3}/{total}] {call.case.id} ({call.case.alias}, {kind}"
    head += f", repeat {call.repeat})" if call.repeat else ")"
    if record is None:
        result = "NO RECORD"
    else:
        cost = _usd(record.cost_usd) + ("" if record.cost_complete else " (cost incomplete)")
        result = f"{record.outcome} · {record.latency_ms:.0f} ms · {cost}"
    return (
        f"{head}: {result} · reserved {_usd(call.reservation.usd)}, counted {_usd(counted)},"
        f" run total {_usd(ledger.counted)}"
    )


# ── The run ──────────────────────────────────────────────────────────────────────────────────


def run_dir(root: Path, manifest: DatasetManifest, started_at: datetime, *, live: bool) -> Path:
    """Where a run writes its files (see the module docstring)."""
    base = root / (REPORTS_DIR if live else DRY_RUN_REPORTS_DIR)
    stamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return base / manifest.dataset / manifest.version / f"{stamp}-{'live' if live else 'fake'}"


def _fail(message: str) -> int:
    print(f"vectapilot-llm: {message}", file=sys.stderr)
    return EXIT_FAILED


_KEEP_OR_DELETE: Final = "commit it as evidence only, or delete it before the next live run"


def _utf8(content: bytes, name: str) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        raise DatasetError(f"{name} is not UTF-8 text") from None  # never the bytes themselves


def run(argv: Sequence[str] | None, io: Io) -> int:
    """The whole CLI; `main` passes the real outside world."""
    args = parse_args(argv)
    try:
        git = io.git()
        dataset_dir = git.root / DATASET_DIR if args.dataset is None else args.dataset
        # Read once, as bytes: the bytes parsed are the bytes compared with the commit.
        files = {dataset_dir / name: (dataset_dir / name).read_bytes() for name in DATASET_FILES}
        manifest = load_manifest(_utf8(files[dataset_dir / MANIFEST_FILE], MANIFEST_FILE))
        cases = load_cases(_utf8(files[dataset_dir / CASES_FILE], CASES_FILE))
        catalog = load_catalog()
        check_dataset(manifest, cases, aliases=catalog.aliases.keys(), schemas=SCHEMAS)
        check_distinct_prompts(cases)  # names the case ids and the alias, never the prompt
        dataset_committed = dataset_at_commit(git.root, files, io.head_file)
    except (GitError, OSError, DatasetError, SummaryInputError) as error:
        return _fail(f"{error}; nothing was called")
    try:
        settings = io.load_settings(args.live)
    except ValidationError as error:  # its text names fields and rules, never a value
        return _fail(f"invalid settings; nothing was called: {error}")
    if args.live and settings.llm_provider != "anthropic":
        return _fail(
            "--live needs Settings to say llm_provider 'anthropic' (VECTAPILOT_LLM_PROVIDER), with"
            " VECTAPILOT_ANTHROPIC_API_KEY in the process environment; nothing was called"
        )
    plan = plan_calls(cases, repeats=args.repeats, catalog=catalog, settings=settings)
    started_at = io.now()
    try:
        # Built before any call: a commit git reports in another form fails here, not after paying.
        run_info = RunInfo(
            dataset=manifest.dataset,
            dataset_version=manifest.version,
            dataset_review=manifest.review,
            dataset_committed=dataset_committed,
            commit=git.commit,
            uncommitted_changes=git.uncommitted_changes,
            provider="anthropic" if args.live else "fake",
            started_at=started_at,
            catalog_version=catalog.catalog_version,
            alias_models={name: alias.model for name, alias in catalog.aliases.items()},
            limits=run_limits(settings, catalog),
            planned_calls=len(plan),
            max_usd=args.max_usd,
        )
    except ValidationError as error:
        return _fail(f"cannot describe the run; nothing was called: {error}")
    print(plan_text(args, manifest, catalog, settings, plan), flush=True)
    if args.live and not confirmed(
        io.stdin, args.max_usd_text or "", stdout_is_terminal=io.stdout_is_terminal()
    ):
        return _fail("not confirmed; nothing was called")
    out = run_dir(git.root, manifest, started_at, live=args.live)
    try:
        out.mkdir(parents=True, exist_ok=False)  # before any call: never overwrite a run
    except OSError as error:
        return _fail(f"cannot create the run directory: {error}; nothing was called")

    # The two inputs are written as the run goes (see the module docstring); "x": never appended to.
    with (
        (out / CALLS_MADE_FILE).open("x", encoding="utf-8") as calls_file,
        (out / LLM_CALL_LOG_FILE).open("x", encoding="utf-8") as log_file,
        captured(gateway_logger, sink=log_file) as capture,
    ):
        done = Execution(sink=calls_file)
        asyncio.run(
            run_calls(
                settings,
                plan,
                live=args.live,
                capture=capture,
                ledger=Ledger(cap=args.max_usd),
                max_calls=args.max_calls,
                done=done,
            )
        )
    if done.reason is not None:
        print(f"Run stopped ({done.stopped}): {done.reason}", file=sys.stderr)

    try:
        measured = join_records(done.calls_made, parse_llm_call_lines(capture.lines))
        # `model_copy` skips validation; `summarise_run` validates the run's facts again.
        ended = run_info.model_copy(update={"stopped": done.stopped, "stop_detail": done.reason})
        summary = summarise_run(ended, cases, measured)
    except (RecordError, SummaryInputError) as error:
        after = f"; {_KEEP_OR_DELETE}" if args.live else ""
        return _fail(f"the run FAILED, no summary written: {error}. Its inputs are in {out}{after}")
    markdown = render_markdown(summary)
    (out / SUMMARY_MD_FILE).write_text(markdown, encoding="utf-8")
    (out / SUMMARY_JSON_FILE).write_text(render_json(summary), encoding="utf-8")
    print()
    print(markdown)
    if not args.live:
        where = "a dry run: git ignores this directory"
    elif summary.feeds_note:
        where = "a complete live run: commit this directory on a branch and open a pull request"
    else:
        where = f"a live run that cannot feed the note (see {SUMMARY_MD_FILE}): {_KEEP_OR_DELETE}"
    print(f"Written to {out} ({where}).")
    return EXIT_PARTIAL if summary.partial else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """The `vectapilot-llm` console script."""
    return run(argv, default_io())
