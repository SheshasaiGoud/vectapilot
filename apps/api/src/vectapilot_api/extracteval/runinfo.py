"""The run folders of the extraction harness (M6 PR 4): where they live, what `run.json` says, the
one line per document in `results.jsonl`, and `values.jsonl`.

**Folders**, under `evals/reports/doc_extraction/<version>/` when live and
`evals/reports/tmp/doc_extraction/<version>/` (git-ignored) when dry, each
`<UTC start>-<tag>-<split>`: `-live-extract-<split>` (paid calls, `--live`) or
`-fake-extract-<split>` (the scripted fake model over fixture documents from `tests/`). The tag
names the eval, as M5's `-live-agent-`: a bare `-live-<split>` would match M3's `LIVE_FOLDER`
(`evalrun/spend.py`), whose count would then refuse every M3 live run on a run.json it cannot
read (tested: no M6 name matches M3's, M4's or M5's pattern, and theirs never match M6's).

**run.json** (`ExtractRunInfo`) names everything needed to trace a number: the dataset version and
freeze hash, the commit, the fingerprint (`Fingerprint`, scoring rules "Dry or real; test once":
both versions, the alias and its model, `catalog_version`, each business's template and business
file hashes, the freeze hash, the limits, and when dry the fixtures' SHA-256), status and cost.

**results.jsonl** (`DocumentLine`, one per document, its fields nested as `FieldLine`s) holds
ids, statuses, levels, reason codes, pages, counts, whether each field is correct, tokens, latency
and cost: **never a value, a quote or document text**. Every string in it is an id, a closed code
or an exception's type name (patterns below), so a value cannot slip in through a free-text field.

**values.jsonl** (`ValueLine`) is the only file with values: per field the value the system
returned (what is scored), the model's own proposal and quote, and the gold. Synthetic documents
only (a live run is refused otherwise); never logged, never printed by `summarize`. shesha reads
it to see a miss.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.extracteval.compare import FieldKind
from vectapilot_api.extracteval.gold import FieldName, Tag
from vectapilot_api.gateway.types import BusinessId, OpaqueId, Slug
from vectapilot_api.llm_cli import DRY_RUN_REPORTS_DIR, REPORTS_DIR

DATASET_NAME: Final = "doc_extraction"
EXTRACT_REPORTS: Final = REPORTS_DIR / DATASET_NAME
"""Live folders and the test-run ledger, relative to the repository root."""
DRY_EXTRACT_REPORTS: Final = DRY_RUN_REPORTS_DIR / DATASET_NAME
LEDGER_FILE: Final = "test-runs.jsonl"
"""`evals/reports/doc_extraction/test-runs.jsonl`: one line per test run, written before it."""

RUN_FILE: Final = "run.json"
RESULTS_FILE: Final = "results.jsonl"
VALUES_FILE: Final = "values.jsonl"
SUMMARY_MD: Final = "summary.md"
LLM_LOG_FILE: Final = "llm_call_log.jsonl"

LIVE_TAG: Final = "live-extract"
DRY_TAG: Final = "fake-extract"
DRY_LABEL: Final = "DRY RUN — plumbing only, never a result"
"""The first line of every dry summary (scoring rules, "Dry or real")."""

SplitName = Literal["dev", "test"]
RunStatus = Literal["complete", "partial", "failed"]
DocumentRun = Literal["scored", "not_run", "harness_error"]
"""`scored`: the core returned an extraction (an E3 all-review one included: the system's real
output). `not_run`: the run stopped before it (a cap, a leak, H1 not written). `harness_error`:
the harness could not get an extraction (an exception outside the core's own fail-closed path, a
refusal): "not scored", by id."""
DocumentStatusName = Literal["complete", "needs_review"]
FieldStatusName = Literal["accepted", "review", "absent"]
LevelName = Literal["high", "medium", "low"]
Code = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
"""A reason code or failure (`extraction.routing.Reason`'s values): never free text."""
TypeName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")]
"""An exception's class name, never its message (E9)."""
Count = Annotated[int, Field(ge=0)]


def stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def folder_name(*, live: bool, started: datetime, split: str) -> str:
    """`<UTC start>-live-extract-<split>` or `<UTC start>-fake-extract-<split>`."""
    return f"{stamp(started)}-{LIVE_TAG if live else DRY_TAG}-{split}"


def run_dir(root: Path, version: str, name: str, *, live: bool) -> Path:
    return root / (EXTRACT_REPORTS if live else DRY_EXTRACT_REPORTS) / version / name


class _Closed(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class Limits(_Closed):
    """E12's limits, the extractor's time limits and the gateway's retries and repairs as the run
    used them."""

    max_bytes: int = Field(ge=1)
    max_pages: int = Field(ge=1)
    max_characters: int = Field(ge=1)
    attempt_timeout_s: float = Field(gt=0)
    deadline_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_repairs: int = Field(ge=0)


class BusinessPins(EvalModel):
    """One business's extraction files as a run read them (SHA-256 of the bytes): part of the
    fingerprint, never of the freeze (`freeze.py`: they are the system under test)."""

    business_sha256: Sha256
    templates: dict[FieldName, Sha256] = Field(min_length=1)
    """Each enabled kind's template."""


class Fingerprint(_Closed):
    """What a run's numbers depend on (scoring rules, "Dry or real; test once"): a test run is
    allowed once per fingerprint."""

    freeze_sha256: Sha256 | None
    extract_prompt_version: OpaqueId
    extraction_rules_version: OpaqueId
    alias: Slug
    model: OpaqueId
    """The alias's catalog model (the alias pins it; named so the report can print it)."""
    catalog_version: OpaqueId
    businesses: dict[BusinessId, BusinessPins] = Field(min_length=1)
    """Each business's business file and, per enabled kind, template (SHA-256)."""
    limits: Limits
    fixtures_sha256: Sha256 | None = None
    """The dry run's fixture documents and scripted replies; None when live."""

    def sha256(self) -> str:
        """SHA-256 of the canonical JSON (sorted keys, no spaces)."""
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExtractRunInfo(_Closed):
    """`run.json` of an extraction run folder: ids, versions, hashes and cost; never a value."""

    record_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    status: RunStatus
    stop: str | None = None
    """Why the run stopped early (a cap, a stop rule, H1 not written), or None."""
    leak: bool = False
    """An extraction or its `llm_call` record named another business than its document's."""
    dry_run: bool
    dry_reason: str | None = None
    environment: str
    dataset: str
    dataset_version: str
    dataset_stage: str
    freeze_sha256: Sha256 | None
    dataset_committed: bool
    split: SplitName
    documents: int = Field(ge=0)
    commit: str
    uncommitted_changes: bool
    started_at: datetime
    ended_at: datetime
    fingerprint: Fingerprint
    fingerprint_sha256: Sha256
    llm_provider: str
    confidence: str
    cost: CostInfo
    test_rerun_reason: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=80
    )
    """The CLI's slug rule again, in the model (M6 PR 4 review, zara-safety): run.json is
    committed, so a free-text reason must never be able to carry a value into it."""
    test_runs_before: int = Field(default=0, ge=0)


class FieldLine(_Closed):
    """One field of one document: codes and flags, never its value or quote."""

    field: FieldName
    type: FieldKind
    status: FieldStatusName | None
    """None when the document is not scored."""
    level: LevelName | None
    reasons: tuple[Code, ...]
    """Sorted, each once."""
    page: int | None = Field(ge=1)
    quote_hits: Count
    has_value: bool
    """The system returned a value (E2 verified it; `service.verified`)."""
    gold_null: bool
    correct: bool | None
    """`compare.same_value(type, gold, value)`; None when the document is not scored."""
    corrected: bool = False
    """The invariant check moved it to review (`routing_invariant`)."""
    withheld: bool = False
    unverified: bool = False
    routed_level: LevelName | None = None
    """The level routing gave the proposed value, after the invariant check and before "no quote,
    no value" or a withheld value cleared `level` (`service.FieldResult.routed_level`): what
    correctness per level is checked on (M6 PR 4 review, ananya-rag)."""
    proposal_correct: bool | None = None
    """`compare.same_value(type, gold, proposal)` for the proposal as the checks read it
    (`signals.FieldEvidence.value`, before any drop; None proposed when the call failed): scored
    with `routed_level`. None exactly when the document is not scored."""

    @model_validator(mode="after")
    def _consistent(self) -> "FieldLine":
        if list(self.reasons) != sorted(set(self.reasons)):
            raise ValueError("reasons are sorted, each once")
        if (self.status is None) != (self.correct is None):
            raise ValueError("a field has a status exactly when it is scored (correct is set)")
        if (self.status is None) != (self.proposal_correct is None):
            raise ValueError("a field has a status exactly when proposal_correct is set")
        if self.status is None and (
            self.level is not None
            or self.routed_level is not None
            or self.reasons
            or self.has_value
        ):
            raise ValueError("a field that is not scored has no level, reason or value")
        if self.level is not None and self.routed_level != self.level:
            raise ValueError("a stored level is the routed level (only a drop clears it)")
        return self


class DocumentLine(_Closed):
    """One document's extraction as scored: ids, codes, counts, tokens, latency and cost."""

    id: Slug
    business_id: BusinessId
    kind: FieldName
    split: SplitName
    tags: tuple[Tag, ...]
    run: DocumentRun
    status: DocumentStatusName | None
    """None when the document is not scored."""
    failure: Code | None = None
    """E3's failure (`truncated`, `invalid_output`, `timeout`, `model_unavailable`,
    `internal_error`), or None."""
    error: TypeName | None = None
    """The exception type behind `failure` or a harness error (never its text)."""
    pages: Count = 0
    image_pages: Count = 0
    signal_errors: Count = 0
    llm_calls: Count = 0
    input_tokens: Count = 0
    output_tokens: Count = 0
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    cost_complete: bool = True
    latency_ms: float | None = Field(default=None, ge=0)
    """The harness's clock around the whole extraction (PDF read, model call, checks)."""
    fields: tuple[FieldLine, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "DocumentLine":
        names = [line.field for line in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("each field is listed once")
        scored = self.run == "scored"
        if scored != (self.status is not None):
            raise ValueError("a document has a status exactly when it is scored")
        if any((line.status is not None) != scored for line in self.fields):
            raise ValueError("a scored document's fields are all scored, and only then")
        return self


class ValueLine(_Closed):
    """One field's values (`values.jsonl`, synthetic only): never logged or printed."""

    id: Slug
    field: FieldName
    value: str | None = Field(max_length=1000)
    """What the system returned (what is scored)."""
    proposed: str | None = Field(max_length=1000)
    """The model's proposal, before any check."""
    quote: str | None = Field(max_length=1000)
    gold: str | None = Field(max_length=1000)


def read_run(folder: Path) -> ExtractRunInfo:
    path = folder / RUN_FILE
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {RUN_FILE}")
    try:
        return ExtractRunInfo.model_validate_json(path.read_bytes())
    except ValidationError as error:
        detail = describe_validation_error(error, "(run)")
    raise DatasetError(f"{folder.name}/{RUN_FILE} is not a valid doc_extraction run.json: {detail}")


def read_results(folder: Path) -> tuple[DocumentLine, ...]:
    """`results.jsonl` (blank lines skipped). Raises `DatasetError` naming the line, never it.

    A document id may appear once: H2 counts one line per document, so a repeated id (a
    hand-edited or corrupt file; `run` cannot write one, dataset ids are unique) would be counted
    twice, silently where its fields are correct. Refused, naming the id (a validated slug) and
    both lines (M6 H2 review, kenji-ml).
    """
    path = folder / RESULTS_FILE
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {RESULTS_FILE}")
    lines: list[DocumentLine] = []
    first_line: dict[str, int] = {}
    for number, text in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not text.strip():
            continue
        try:
            line = DocumentLine.model_validate_json(text)
        except ValidationError as error:
            detail = describe_validation_error(error, "(line)")
            raise DatasetError(f"{RESULTS_FILE} line {number} is not valid: {detail}") from None
        if line.id in first_line:
            raise DatasetError(
                f"{RESULTS_FILE} line {number} repeats document {line.id}"
                f" (first on line {first_line[line.id]})"
            )
        first_line[line.id] = number
        lines.append(line)
    return tuple(lines)


def write_json(path: Path, model: BaseModel) -> None:
    """Write a model as indented JSON, refusing to overwrite (`x`): a folder is written once."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(model.model_dump_json(indent=2) + "\n")


def json_line(model: BaseModel) -> str:
    return model.model_dump_json() + "\n"


def by_id(lines: Sequence[DocumentLine]) -> dict[str, DocumentLine]:
    return {line.id: line for line in lines}
