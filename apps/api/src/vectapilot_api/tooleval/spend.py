"""M5's spend across its live runs (M5 PR 6; D8 (b), decided 2026-11-10: $1.00 for all of M5's
paid runs, one live dev and one live test run, each still <= $1.00).

The counting core is M3's (`evalrun.spend.count_spend`); only the rules differ (`M5_RULES`):
- **What counts:** every M5 live folder committed in HEAD,
  `evals/reports/tool_calls/<version>/<UTC>-live-agent-<split>` (the only paid runs). Every such
  folder is M5's by its name, so a committed `recorded-spend.json` in one always counts (its
  `counts_toward_m3` field is M3's and is not read here).
- **A live folder without run.json** counts at M1's $1.00 per-run ceiling until its real spend is
  recorded and committed, as in M3 and M4: with D8's $1.00 ceiling, one such folder uses all of it.
- **One branch:** `m5/close-out` (plan PR 7: every live run is made, and its folder committed,
  there, before the next live run).
- **Never M3's or M4's:** `M5_LIVE_FOLDER` matches only M5's folders, and no M5 folder name matches
  M3's `LIVE_FOLDER` or M4's `M4_LIVE_FOLDER` (tests/tooleval/test_spend.py), so their sums and
  refusals do not change, and theirs are never counted here.
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
from vectapilot_api.tooleval.runinfo import ToolRunInfo

M5_CEILING_USD: Final = Decimal("1.00")
"""D8 (b), decided 2026-11-10 by shesha: all of M5's paid runs together."""

M5_LIVE_BRANCH: Final = "m5/close-out"

M5_LIVE_FOLDER: Final = re.compile(
    r"^evals/reports/tool_calls/[^/]+/[^/]+-live-agent-(?:dev|test)$"
)
"""An M5 live folder, relative to the repository root (`runinfo.folder_name`)."""


def m5_run_reason(content: bytes) -> RunSpend | None:
    """A committed M5 run.json's spend: every live one counts (None: not a valid run.json)."""
    try:
        info = ToolRunInfo.model_validate_json(content)
    except ValidationError:
        return None
    why = None if info.dry_run else f"live agent {info.split}"
    return RunSpend(info.cost.total_usd, info.cost.llm_cost_complete, why)


def _m5_note_counts(note: RecordedSpend) -> bool:
    """Every folder M5's pattern matches is M5's (module docstring)."""
    return True


M5_RULES: Final = SpendRules(
    milestone="M5",
    decision="D8",
    ceiling=M5_CEILING_USD,
    branch=M5_LIVE_BRANCH,
    branch_decided="the M5 plan, PR 7",
    folder=M5_LIVE_FOLDER,
    disk_glob="tool_calls/*/*-live-*",
    run_reason=m5_run_reason,
    note_counts=_m5_note_counts,
    what_counts="-live-agent-<split>, or without run.json",
)


def m5_spend(root: Path, head_paths: HeadPaths, head_file: HeadFile) -> MilestoneSpend:
    """Count M5's spend from HEAD (`count_spend` with `M5_RULES`)."""
    return count_spend(root, M5_RULES, head_paths, head_file)
