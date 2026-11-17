"""The run folders of the red-team harness (M7 PR 2): where they live and what `run.json` says.

**Folders** (plan PR 2, "Folders"):
- dry: `evals/reports/tmp/redteam/<UTC start>-fake-redteam/` (git-ignored; no version level);
- live: `evals/reports/redteam/<version>/<UTC start>-live-redteam/` (D7's raw arm, shesha's).
The tag names the suite, as M5's `-live-agent-` and M6's `-live-extract-` do: no M2-M6 spend
pattern matches these names, and M7's matches none of theirs (tests/redteam/test_spend.py, both
ways).

**run.json** (`RedteamRunInfo`) carries the scoring rules' fingerprint (`Fingerprint`): the
dataset version and freeze SHA-256, the `gates.json` SHA-256, the rules version (SHA-256 of
`data/safety/`), the PII generator's version and seed, `guard_routes` and `guard_answer`, the
provider, model ids, `PROMPT_VERSION` and `AGENT_PROMPT_VERSION`, and the agent loop (the
scripted replay standing in for M5 H1 `run_turn`, or `run_turn` itself), plus the commit, status
and cost. Ids, codes, hashes and amounts only; never a case's text or a generated value.

Step B3 of PR 2 writes run.json and the folder's other files; this step fixes their names and
the run.json model, which M7's spend rules read (`spend.py`).
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from vectapilot_api.evalrun.report import CostInfo
from vectapilot_api.evalset.corpus import Sha256
from vectapilot_api.evalset.questions import DatasetVersion
from vectapilot_api.gateway.types import OpaqueId, Slug
from vectapilot_api.llm_cli import DRY_RUN_REPORTS_DIR, REPORTS_DIR
from vectapilot_api.redteam.pii import SEED_MAX

DATASET_NAME: Final = "redteam"
REDTEAM_REPORTS: Final = REPORTS_DIR / DATASET_NAME
"""Live folders and the test-run ledger, relative to the repository root."""
DRY_REDTEAM_REPORTS: Final = DRY_RUN_REPORTS_DIR / DATASET_NAME

RUN_FILE: Final = "run.json"
RESULTS_FILE: Final = "results.jsonl"
SUMMARY_MD: Final = "summary.md"

LIVE_TAG: Final = "live-redteam"
DRY_TAG: Final = "fake-redteam"
DRY_LINE: Final = (
    "DRY: deterministic guards with scripted model text; says nothing about the model's own"
    " resistance. The interval is arithmetic for scale on a constructed set, not a coverage"
    " estimate."
)
"""Every dry summary's statement (scoring rules, "Dry or live; test once")."""
REPLAY_LINE: Final = "agent loop: scripted replay standing in for M5 H1 `run_turn`"

RunStatus = Literal["complete", "partial", "failed"]
AgentLoop = Literal["replay", "run_turn"]
Switch = Literal["off", "on"]


def stamp(started: datetime) -> str:
    return started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def folder_name(*, live: bool, started: datetime) -> str:
    """`<UTC start>-live-redteam` or `<UTC start>-fake-redteam`."""
    return f"{stamp(started)}-{LIVE_TAG if live else DRY_TAG}"


def run_dir(root: Path, version: str, name: str, *, live: bool) -> Path:
    """Live: under the version; dry: directly under `tmp/redteam/` (plan PR 2, "Folders")."""
    if live:
        return root / REDTEAM_REPORTS / version / name
    return root / DRY_REDTEAM_REPORTS / name


class _Closed(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class Fingerprint(_Closed):
    """What a run's numbers depend on (scoring rules, "Fingerprint"): a live run is ledgered
    once per fingerprint, the probe once per `rules_version`."""

    dataset_version: DatasetVersion
    freeze_sha256: Sha256 | None
    gates_sha256: Sha256
    rules_version: Sha256 | None
    """SHA-256 of `data/safety/` (`guards.config.GuardConfig.rules_version`); None before PR 4's
    term files exist."""
    pii_generator: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,39}$")
    pii_seed: int = Field(ge=0, le=SEED_MAX)
    guard_routes: Switch
    guard_answer: Switch
    provider: Slug
    """`fake` or the live provider's name."""
    models: dict[Slug, OpaqueId]
    """Each alias the run used and its catalog model."""
    prompt_version: OpaqueId
    agent_prompt_version: OpaqueId
    agent_loop: AgentLoop

    def sha256(self) -> str:
        """SHA-256 of the canonical JSON (sorted keys, no spaces)."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RedteamRunInfo(_Closed):
    """`run.json` of a red-team run folder."""

    record_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    status: RunStatus
    stop: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    """Why the run stopped early, as a closed code, or None."""
    dry_run: bool
    environment: str = Field(pattern=r"^[a-z]+$")
    dataset: Literal["redteam"]
    dataset_version: DatasetVersion
    dataset_stage: Literal["collecting", "frozen"]
    cases: int = Field(ge=0)
    cases_reviewed: int | None = Field(default=None, ge=0)
    """Cases whose `reviewed_by` names a person (D6 (a)); None: not recorded."""
    commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    uncommitted_changes: bool
    started_at: datetime
    ended_at: datetime
    fingerprint: Fingerprint
    fingerprint_sha256: Sha256
    cost: CostInfo
    test_rerun_reason: str | None = Field(
        default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=80
    )
    """A slug, never free text (M6 PR 4 review: run.json is committed)."""
