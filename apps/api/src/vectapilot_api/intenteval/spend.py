"""M4's spend across its live runs (M4 PR 4; D7, decided 2026-11-05: $2.00 for all of M4's paid
runs, each still <= $1.00).

The counting core is M3's (`evalrun.spend.count_spend`); only the rules differ (`M4_RULES`):
- **What counts:** every M4 live folder committed in HEAD, `evals/reports/intents/<version>/
  <UTC>-live-embed` and `-live-llm-<split>` (the only paid runs: `runinfo.py`'s table). Every
  such folder is M4's by its name, so a committed `recorded-spend.json` in one always counts (its
  `counts_toward_m3` field is M3's and is not read here).
- **A live folder without run.json** counts at M1's $1.00 per-run ceiling until its spend is
  recorded and committed, as in M3.
- **One branch:** `m4/close-out` (plan PR 4 and PR 5: every live run is made, and its folder
  committed, there, before the next live run).
- **Never M3's, never M2's:** `M4_LIVE_FOLDER` matches only M4's folders, and no M4 folder name
  matches M3's `LIVE_FOLDER` (tests/intenteval/test_spend.py), so M3's sum and refusals do not
  change.
"""

import re
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from vectapilot_api.evalrun.spend import (
    HeadFile,
    HeadPaths,
    MilestoneSpend,
    RecordedSpend,
    RunSpend,
    SpendRules,
    count_spend,
)
from vectapilot_api.intenteval.runinfo import IntentRunInfo

M4_CEILING_USD: Final = Decimal("2.00")
"""D7 (b), decided 2026-11-05 by shesha: all of M4's paid runs together."""

M4_LIVE_BRANCH: Final = "m4/close-out"

M4_LIVE_FOLDER: Final = re.compile(
    r"^evals/reports/intents/[^/]+/[^/]+-live-(?:embed|llm-(?:dev|test))$"
)
"""An M4 live folder, relative to the repository root (`runinfo.folder_name`)."""


def m4_run_reason(content: bytes) -> RunSpend | None:
    """A committed M4 run.json's spend: every live one counts (None: not a valid run.json)."""
    try:
        info = IntentRunInfo.model_validate_json(content)
    except ValidationError:
        return None
    why = None if info.dry_run else f"live {info.kind}" + (f" {info.split}" if info.split else "")
    return RunSpend(info.cost.total_usd, info.cost.llm_cost_complete, why)


def _m4_note_counts(note: RecordedSpend) -> bool:
    """Every folder M4's pattern matches is M4's (module docstring)."""
    return True


M4_RULES: Final = SpendRules(
    milestone="M4",
    decision="D7",
    ceiling=M4_CEILING_USD,
    branch=M4_LIVE_BRANCH,
    branch_decided="the M4 plan, PR 4",
    folder=M4_LIVE_FOLDER,
    disk_glob="intents/*/*-live-*",
    run_reason=m4_run_reason,
    note_counts=_m4_note_counts,
    what_counts="-live-embed and -live-llm-<split>, or without run.json",
)


def m4_spend(root: Path, head_paths: HeadPaths, head_file: HeadFile) -> MilestoneSpend:
    """Count M4's spend from HEAD (`count_spend` with `M4_RULES`)."""
    return count_spend(root, M4_RULES, head_paths, head_file)
