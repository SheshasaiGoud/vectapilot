"""The run folders of the tool-call harness (M5 PR 6): where they live, what `run.json` says, the
one line per scenario in `results.jsonl`, and the replies and labels files.

**Folders**, under `evals/reports/tool_calls/<version>/` when live and
`evals/reports/tmp/tool_calls/<version>/` (git-ignored) when dry, each `<UTC start>-<tag>-<split>`:
`-live-agent-<split>` (paid calls, `--live`) or `-fake-agent-<split>` (the scripted fake model).
The plan's wording was `-live-<split>`; that name matches M3's `LIVE_FOLDER`
(`evalrun/spend.py`), whose count would then refuse every M3 live run on a run.json it cannot
read, so the tag says `agent` (tested: no M5 name matches M3's or M4's pattern, and theirs never
match M5's).

**run.json** (`ToolRunInfo`) names everything needed to trace a number: the dataset version and
freeze hash, the commit, the fingerprint (`Fingerprint`: `AGENT_PROMPT_VERSION`, the alias and
its model, `catalog_version`, each business's step schema, sandbox and freeze hashes, the limits),
routing `none` ("safety floor not exercised"), status and cost.

**results.jsonl** (`ResultLine`) holds ids, the outcome, the proposal's tool and key arguments, the
write rows as the database holds them, the hand-off reason, steps, latency and cost: **never
text**. The model's replies go to `replies.jsonl` (`ReplyLine`), apart, for the label sheet:
synthetic model text, committed with a live folder like M2's `answers.jsonl`. shesha's labels of
false success claims go to `labels.jsonl` (`LabelLine`).
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalset.corpus import Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, Split
from vectapilot_api.gateway.types import BusinessId, OpaqueId, Slug, Usd
from vectapilot_api.intenteval.rows import LanguageGroup
from vectapilot_api.llm_cli import DRY_RUN_REPORTS_DIR, REPORTS_DIR
from vectapilot_api.tooleval.scenarios import ExpectedKind
from vectapilot_api.tooleval.split import SandboxPins
from vectapilot_api.tools.config import CustomerRef, ToolName

DATASET_NAME: Final = "tool_calls"
TOOL_REPORTS: Final = REPORTS_DIR / DATASET_NAME
"""Live folders and the test-run ledger, relative to the repository root."""
DRY_TOOL_REPORTS: Final = DRY_RUN_REPORTS_DIR / DATASET_NAME

RUN_FILE: Final = "run.json"
RESULTS_FILE: Final = "results.jsonl"
REPLIES_FILE: Final = "replies.jsonl"
LABELS_FILE: Final = "labels.jsonl"
SUMMARY_MD: Final = "summary.md"
LLM_LOG_FILE: Final = "llm_call_log.jsonl"

LIVE_TAG: Final = "live-agent"
DRY_TAG: Final = "fake-agent"

DRY_LABEL: Final = "DRY RUN — scripted fake model: plumbing only, never a result"
"""The first line of every dry summary (scoring rules, "Dry or real")."""
ROUTING_NOTE: Final = (
    "router=none: the agent service ran the loop with no router and no W11 step; the safety floor"
    " was not exercised"
)

SplitName = Literal["dev", "test"]
RunStatus = Literal["complete", "partial", "failed"]
OutcomeKind = Literal["reply", "confirmation_required", "handoff", "answer", "not_run"]
"""A turn's response kind, or `not_run`: a scenario a stopped run never reached (never scored)."""
WriteKind = Literal["booking", "retired", "ticket"]
ActionStatusName = Literal["pending", "executed", "failed"]
Name = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")]
"""A code or a class name (`turn_timeout`, `OutcomeUnknown`): never free text."""
KeyArguments = dict[str, Annotated[str, Field(min_length=1, max_length=64)]]


def stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def folder_name(*, live: bool, started: datetime, split: str) -> str:
    """`<UTC start>-live-agent-<split>` or `<UTC start>-fake-agent-<split>` (module docstring)."""
    return f"{stamp(started)}-{LIVE_TAG if live else DRY_TAG}-{split}"


def run_dir(root: Path, version: str, name: str, *, live: bool) -> Path:
    return root / (TOOL_REPORTS if live else DRY_TOOL_REPORTS) / version / name


class _Closed(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class Limits(_Closed):
    """The agent's bounds as the run used them (Settings)."""

    max_steps: int = Field(ge=1)
    turn_timeout_s: float = Field(gt=0)
    attempt_timeout_s: float = Field(gt=0)
    deadline_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_repairs: int = Field(ge=0)
    action_lifetime_s: int = Field(gt=0)


class Fingerprint(_Closed):
    """What a run's numbers depend on (scoring rules, "Dry or real; test once"): a test run is
    allowed once per fingerprint (`ledger.py`)."""

    freeze_sha256: Sha256 | None
    agent_prompt_version: OpaqueId
    alias: Slug
    catalog_version: OpaqueId
    model: OpaqueId
    """The alias's catalog model (the alias pins it; named so the report can print it)."""
    step_schemas: dict[BusinessId, Sha256]
    """Each business's `AgentStep` JSON schema, as the gateway quotes it into the prompt."""
    sandbox: dict[BusinessId, SandboxPins]
    limits: Limits
    routing: Literal["none"] = "none"
    plans_sha256: Sha256 | None = None
    """The dry run's fixture plans (`plans.py`); None when live."""

    def sha256(self) -> str:
        """SHA-256 of the canonical JSON (sorted keys, no spaces)."""
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ToolRunInfo(_Closed):
    """`run.json` of a tool-call run folder."""

    record_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    status: RunStatus
    stop: str | None = None
    """Why the run stopped early (a cap, a stop rule, a leak, H1 not written), or None."""
    leak: bool = False
    """A tool returned a row of another business: the run stopped (exit 4)."""
    dry_run: bool
    dry_reason: str | None = None
    environment: str
    dataset: str
    dataset_version: str
    dataset_stage: str
    freeze_sha256: Sha256 | None
    dataset_committed: bool
    split: SplitName
    scenarios: int = Field(ge=0)
    commit: str
    uncommitted_changes: bool
    started_at: datetime
    ended_at: datetime
    fingerprint: Fingerprint
    fingerprint_sha256: Sha256
    llm_provider: str
    routing: Literal["none"] = "none"
    routing_note: str = ROUTING_NOTE
    confidence: str
    cost: CostInfo
    test_rerun_reason: str | None = None
    test_runs_before: int = Field(default=0, ge=0)


class WriteRow(_Closed):
    """One write row the database holds after a scenario, read on the owner's connection
    (`writes.py`): ids and states only."""

    kind: WriteKind
    """`booking` (a booking an action created), `retired` (a booking an action rescheduled), or
    `ticket`."""
    row_id: str = Field(min_length=1, max_length=64)
    action_id: str | None
    """The action named by the row (`action_id`, or `retired_by_action_id` for `retired`)."""
    slot_id: str | None = None
    """A created booking's slot."""
    category: str | None = None
    """A ticket's category."""
    action_status: ActionStatusName | None
    """That action's status; None when no such action exists."""
    confirmed_at_set: bool
    action_trace_ours: bool
    """The action was proposed under this scenario's turn trace id (False also when no such action
    exists): a row anything else wrote between the reset and the count is counted, never hidden."""
    confirm_trace_ours: bool
    """The action's confirm trace id is one of this scenario's harness confirm calls."""
    executed_audit: bool
    """An `executed` confirm audit row names the action."""


class ActiveBookings(_Closed):
    customer_ref: CustomerRef
    actual: int = Field(ge=0)
    expected: int = Field(ge=0)
    """The seed's active bookings of this customer, plus one when the scenario confirms a
    `book`."""


class ResultLine(_Closed):
    """One scenario's outcome. Ids, codes and counts; never the message, a reply or a summary."""

    id: Slug
    business_id: BusinessId
    language_group: LanguageGroup
    split: Split
    unit: str = Field(min_length=1, max_length=200)
    read_only: bool
    emergency: bool
    confirm: bool
    steering: bool
    expected_kind: ExpectedKind
    expected_tool: ToolName | None
    expected_arguments: KeyArguments | None
    expected_write_rows: int = Field(ge=0)
    outcome: OutcomeKind
    handoff_reason: Name | None = None
    priority: Name | None = None
    guidance_lines: int = Field(default=0, ge=0)
    """A hand-off's emergency guidance as shown: the number of lines, never their text."""
    proposals_stored: int = Field(default=0, ge=0)
    """Pending actions stored under this turn's trace id (the database's count)."""
    proposals_audited: int = Field(default=0, ge=0)
    """Valid proposals the executor audited under this turn's trace id (`tool_audit`, phase
    `propose`, outcome `proposed`): written before the store runs, so a proposal whose store then
    failed (`proposal_not_stored`) is still counted from the database."""
    proposal_tool: ToolName | None = None
    proposal_arguments: KeyArguments | None = None
    """The stored proposal's key arguments (`KEY_ARGUMENTS`), never a ticket's text."""
    proposal_matched: bool | None = None
    """None without exactly one stored proposal."""
    confirmed_action: str | None = None
    """The action id the harness confirmed (only a matching proposal of a `confirm` scenario)."""
    confirm_results: tuple[Name, ...] = ()
    """Each confirm call, in order: two at once, then one repeat; `executed`, `failed`, or the
    exception's class name. A call whose answer is `OutcomeUnknown` or `ConfirmFailed` confirms
    again, as W9 tells a client (`turns.confirm_twice`), until it gets a definite answer or runs
    out of attempts."""
    confirm_unresolved: bool = False
    """The repeat confirm still had no definite answer (`executed`, `failed`, `ActionNotFound`)
    after its last attempt: a write may still have been in flight when the rows were read. Not
    scored for tool success; listed by id next to the write counts."""
    write_rows: tuple[WriteRow, ...] = ()
    sandbox_exact: bool | None = None
    """For a `confirm` scenario: the sandbox holds exactly the expected write. None otherwise."""
    active_bookings: tuple[ActiveBookings, ...] = ()
    reads: tuple[Name, ...] = ()
    """Read tools called, in order (`tool_audit`, by trace id). Reported, never required."""
    tool_errors: tuple[Name, ...] = ()
    """Typed tool errors of the turn's calls (`tool_audit`, by trace id)."""
    steps: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    cost_usd: Usd | None = None
    cost_complete: bool = True
    error: Name | None = None
    """The class name of an exception that became a hand-off, never its text."""


class ReplyLine(_Closed):
    """A `reply` outcome's text, for the label sheet only (`replies.jsonl`)."""

    id: Slug
    text: str = Field(min_length=1, max_length=2000)


class LabelLine(_Closed):
    """shesha's label of one reply (`labels.jsonl`): does it claim a write that never happened?"""

    id: Slug
    false_success_claim: bool | None
    """None until labelled."""
    labelled_by: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9 ._-]{0,39}$")
    labelled_on: date | None = None


def read_run(folder: Path) -> ToolRunInfo:
    path = folder / RUN_FILE
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {RUN_FILE}")
    try:
        return ToolRunInfo.model_validate_json(path.read_bytes())
    except ValidationError as error:
        detail = describe_validation_error(error, "(run)")
    raise DatasetError(f"{folder.name}/{RUN_FILE} is not a valid tool_calls run.json: {detail}")


def _read_lines[M: BaseModel](folder: Path, name: str, model: type[M]) -> tuple[M, ...]:
    path = folder / name
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {name}")
    lines: list[M] = []
    for number, text in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not text.strip():
            continue
        try:
            lines.append(model.model_validate_json(text))
        except ValidationError as error:
            detail = describe_validation_error(error, "(line)")
            raise DatasetError(f"{name} line {number} is not valid: {detail}") from None
    return tuple(lines)


def read_results(folder: Path) -> tuple[ResultLine, ...]:
    return _read_lines(folder, RESULTS_FILE, ResultLine)


def read_replies(folder: Path) -> tuple[ReplyLine, ...]:
    return _read_lines(folder, REPLIES_FILE, ReplyLine)


def read_labels(folder: Path) -> tuple[LabelLine, ...] | None:
    """`labels.jsonl`, or None when the folder has none yet."""
    if not (folder / LABELS_FILE).is_file():
        return None
    return _read_lines(folder, LABELS_FILE, LabelLine)


def write_json(path: Path, model: BaseModel) -> None:
    """Write a model as indented JSON, refusing to overwrite (`x`): a folder is written once."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(model.model_dump_json(indent=2) + "\n")


def write_lines(path: Path, models: Sequence[BaseModel]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for model in models:
            handle.write(model.model_dump_json() + "\n")
