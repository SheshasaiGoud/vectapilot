"""The run folders of the intent harness (M4 PR 4): where they live, what `run.json` says, and the
one line per row in `results.jsonl`.

**Folders** [proposal, plan PR 4], under `evals/reports/intents/<version>/` when real and
`evals/reports/tmp/intents/<version>/` (git-ignored) when dry, each `<UTC start>-<tag>`:

| what                         | real                    | dry                    |
|------------------------------|-------------------------|------------------------|
| embeddings (`embed`)         | `-live-embed`           | `-fake-embed`          |
| the classifier (`train` ...) | `-local-model`          | `-fake-model`          |
| classifier mode (`run`)      | `-local-classifier-<s>` | `-fake-classifier-<s>` |
| LLM mode (`run`)             | `-live-llm-<s>`         | `-fake-llm-<s>`        |
| hybrid (`summarize`)         | `-replay-hybrid-<s>`    | `-fake-hybrid-<s>`     |

Only `-live-embed` and `-live-llm-<s>` cost money; M4's spend counts exactly those
(`intenteval/spend.py`), and none of these names matches M3's `LIVE_FOLDER` (tested). `-local-model`
is not in the plan's list: the artifact, the calibration and the dev curve need a folder of their
own, written once by `train` and completed by `calibrate` and `thresholds` [proposal].

**Dry or real** is decided by where the vectors came from, never by a flag (plan, "Dry or real"):
a classifier, model or hybrid folder is real only when its embedding cache is the one of a
`-live-embed` folder committed in HEAD, byte for byte. An LLM folder is real only with `--live`.

**run.json** (`IntentRunInfo`) names everything needed to trace a number: the dataset version and
freeze hash, the commit, the fingerprint (`Fingerprint`), the inputs, seed and B, status and cost.
**results.jsonl** (`ResultLine`) holds ids, labels, routes, probabilities, latency and cost:
never a message's text.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalset.corpus import Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.gateway.types import OpaqueId, Slug, Usd
from vectapilot_api.intenteval.rows import LanguageGroup
from vectapilot_api.intents.policy import Priority, RouteKind
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.llm_cli import DRY_RUN_REPORTS_DIR, REPORTS_DIR

DATASET_NAME: Final = "intents"
INTENT_REPORTS: Final = REPORTS_DIR / DATASET_NAME
"""Real folders and the test-run ledger, relative to the repository root."""
DRY_INTENT_REPORTS: Final = DRY_RUN_REPORTS_DIR / DATASET_NAME

RUN_FILE: Final = "run.json"
RESULTS_FILE: Final = "results.jsonl"
SUMMARY_MD: Final = "summary.md"
EMBEDDING_LOG_FILE: Final = "embedding_call_log.jsonl"
LLM_LOG_FILE: Final = "llm_call_log.jsonl"

DRY_LABEL: Final = "DRY RUN — fake embedder/provider: plumbing only, never a result"
"""The first line of every dry summary (plan, "Dry or real")."""

RunKind = Literal["embed", "classifier", "llm", "hybrid"]
Mode = Literal["classifier", "llm", "hybrid"]
SplitName = Literal["dev", "test"]
RunStatus = Literal["complete", "partial", "failed"]
DecidedBy = Literal[
    "classifier", "safety_floor", "llm", "classifier_kept", "fail_closed", "not_run"
]
"""`decide.DecidedBy`, plus `not_run`: a row a stopped run never reached (never scored)."""

_TAGS: Final[dict[tuple[str, bool], str]] = {
    ("embed", True): "live-embed",
    ("embed", False): "fake-embed",
    ("model", True): "local-model",
    ("model", False): "fake-model",
    ("classifier", True): "local-classifier",
    ("classifier", False): "fake-classifier",
    ("llm", True): "live-llm",
    ("llm", False): "fake-llm",
    ("hybrid", True): "replay-hybrid",
    ("hybrid", False): "fake-hybrid",
}


def stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def folder_name(kind: str, *, real: bool, started: datetime, split: str | None = None) -> str:
    """`<UTC start>-<tag>[-<split>]` (module docstring's table)."""
    tag = _TAGS[(kind, real)]
    return f"{stamp(started)}-{tag}" + ("" if split is None else f"-{split}")


def run_dir(root: Path, version: str, name: str, *, real: bool) -> Path:
    return root / (INTENT_REPORTS if real else DRY_INTENT_REPORTS) / version / name


class _Closed(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class Fingerprint(_Closed):
    """What a mode's numbers depend on (plan PR 4: artifact sha256, T, tau, sigma,
    `ROUTER_PROMPT_VERSION`, `catalog_version`, freeze hash). A field the mode does not use is
    None: classifier mode has no prompt, LLM mode no artifact; hybrid has both.

    tau and sigma: sigma is set exactly when the mode uses D8's thresholds (classifier, hybrid).
    Then tau None means **no confidence band** (decided 2026-11-09: every row below the floor goes
    to the LLM), never "not used"; a tau without a sigma is refused. Both None: LLM mode."""

    freeze_sha256: Sha256 | None
    artifact_sha256: Sha256 | None = None
    temperature: float | None = None
    tau: float | None = None
    sigma: float | None = None
    router_prompt_version: OpaqueId | None = None
    catalog_version: OpaqueId | None = None
    router_model: OpaqueId | None = None
    """The `small` alias's catalog model (the request's alias pins it too, through the prompt's
    fingerprint; named here so the report can print it)."""

    @model_validator(mode="after")
    def _tau_needs_sigma(self) -> "Fingerprint":
        if self.tau is not None and self.sigma is None:
            raise ValueError("a fingerprint with tau needs sigma (tau None with sigma: no band)")
        return self

    @property
    def no_confidence_band(self) -> bool:
        """D8's thresholds are used (sigma set) and tau is None."""
        return self.sigma is not None and self.tau is None

    def sha256(self) -> str:
        """SHA-256 of the canonical JSON (sorted keys, no spaces): equal fingerprints, equal
        hashes, on every machine."""
        payload = self.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IntentRunInfo(_Closed):
    """`run.json` of an embed, classifier, LLM or hybrid folder."""

    record_version: Literal[1] = 1
    kind: RunKind
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    status: RunStatus
    stop: str | None = None
    """Why the run stopped early (a cap, a stop rule), or None."""
    dry_run: bool
    dry_reason: str | None = None
    """Why it is dry: "fake embedder", "embedding cache not from a committed -live-embed folder",
    ... None when real."""
    environment: str
    dataset: str
    dataset_version: str
    dataset_stage: str
    freeze_sha256: Sha256 | None
    dataset_committed: bool
    """Whether every dataset file read is byte for byte in the commit."""
    split: SplitName | None = None
    """None for an embed folder (it embeds every row)."""
    rows: int = Field(ge=0)
    commit: str
    uncommitted_changes: bool
    started_at: datetime
    ended_at: datetime
    fingerprint: Fingerprint
    fingerprint_sha256: Sha256
    embed_folder: str | None = None
    """The `-live-embed`/`-fake-embed` folder whose cache the run read (repository-relative)."""
    model_folder: str | None = None
    inputs: tuple[str, ...] = ()
    """A hybrid folder's classifier and LLM folders (repository-relative), in that order."""
    embedding_model: OpaqueId | None = None
    embedding_cache_sha256: Sha256 | None = None
    llm_provider: str | None = None
    router_alias: Slug | None = None
    confidence: str
    bootstrap_seed: int
    bootstrap_resamples: int = Field(ge=1)
    cost: CostInfo
    test_rerun_reason: str | None = None
    test_runs_before: int = Field(default=0, ge=0)
    """Ledger entries of this mode on this frozen version before this one (test only)."""


class ResultLine(_Closed):
    """One row's outcome in one mode. Ids, labels and numbers; never the message's text."""

    id: Slug
    business_id: Slug
    language_group: LanguageGroup
    unit: str = Field(min_length=1, max_length=200)
    """The row's split unit (`split.unit_key`, joined by `:`): the bootstrap resamples units."""
    gold: Intent
    safety: bool
    """The gold intent is one of the business type's safety intents (`policy.safety_intents`)."""
    decided_by: DecidedBy
    final_intent: Intent | None
    """None when fail-closed or not run."""
    route: RouteKind | None
    """None only when not run."""
    priority: Priority | None
    llm_intent: Intent | None = None
    """The LLM's raw answer when it was asked and answered."""
    error: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
    """The `GatewayError` class name when fail-closed, never its text."""
    top_probability: float | None = Field(default=None, ge=0, le=1)
    """Calibrated (T as the artifact's) top-1 probability; classifier and hybrid only."""
    top_probability_raw: float | None = Field(default=None, ge=0, le=1)
    """The same at T = 1, for the raw-against-calibrated reliability table."""
    classifier_intent: Intent | None = None
    """The classifier's top-1 (ties in decision order); classifier and hybrid only."""
    probabilities: tuple[float, ...] | None = None
    """Calibrated probabilities in decision order (8 values); the hybrid replays them."""
    latency_ms: float | None = Field(default=None, ge=0)
    """Classifier: embedding + scoring. LLM: the call. Hybrid: composed from replayed calls."""
    scoring_ms: float | None = Field(default=None, ge=0)
    embedding_ms: float | None = Field(default=None, ge=0)
    cost_usd: Usd | None = None
    """The row's LLM call (complete cost), when one was made; embedding cost is per folder."""
    cost_complete: bool = True


def read_run(folder: Path) -> IntentRunInfo:
    path = folder / RUN_FILE
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {RUN_FILE}")
    try:
        return IntentRunInfo.model_validate_json(path.read_bytes())
    except ValidationError as error:
        detail = describe_validation_error(error, "(run)")
    raise DatasetError(f"{folder.name}/{RUN_FILE} is not a valid intent run.json: {detail}")


def read_results(folder: Path) -> tuple[ResultLine, ...]:
    path = folder / RESULTS_FILE
    if not path.is_file():
        raise DatasetError(f"{folder.name} has no {RESULTS_FILE}")
    lines: list[ResultLine] = []
    for number, text in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not text.strip():
            continue
        try:
            lines.append(ResultLine.model_validate_json(text))
        except ValidationError as error:
            detail = describe_validation_error(error, "(line)")
            raise DatasetError(f"{RESULTS_FILE} line {number} is not valid: {detail}") from None
    return tuple(lines)


def write_json(path: Path, model: BaseModel) -> None:
    """Write a model as indented JSON, refusing to overwrite (`x`): a folder is written once."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(model.model_dump_json(indent=2) + "\n")
