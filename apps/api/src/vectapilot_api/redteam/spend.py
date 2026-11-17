"""M7's spend across its live runs (M7 PR 2; D7 (b), decided 2026-11-16: at most $1.00 in total
for one ledgered raw-arm live run, after M5's H1 and the Anthropic key; shesha's alone).

The counting core is M3's (`evalrun.spend.count_spend`); only the rules differ (`M7_RULES`):
- **What counts:** every M7 live folder committed in HEAD,
  `evals/reports/redteam/<version>/<UTC>-live-redteam` (the only paid runs: every dry run and
  every CI run uses the fake provider). Every such folder is M7's by its name, so a committed
  `recorded-spend.json` in one always counts, as in M4 to M6 (its `counts_toward_m3` field is
  M3's and is not read here).
- **A live folder without run.json** counts at M1's $1.00 per-run ceiling until its real spend is
  recorded and committed, as in M3 to M6: with D7's $1.00 ceiling, one such folder uses all of it.
- **One branch:** `m7/close-out` (plan PR 9: the live run is made, and its folder committed,
  there).
- **Never M3's to M6's:** `M7_LIVE_FOLDER` matches only M7's folders, and no M7 folder name
  matches M3's `LIVE_FOLDER`, M4's, M5's or M6's pattern (tests/redteam/test_spend.py, both
  ways), so their sums and refusals do not change, and theirs are never counted here.
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
from vectapilot_api.redteam.runinfo import RedteamRunInfo

M7_CEILING_USD: Final = Decimal("1.00")
"""D7 (b), decided 2026-11-16 by shesha: all of M7's paid runs together."""

M7_LIVE_BRANCH: Final = "m7/close-out"

M7_LIVE_FOLDER: Final = re.compile(r"^evals/reports/redteam/[^/]+/[^/]+-live-redteam$")
"""An M7 live folder, relative to the repository root (`runinfo.folder_name`)."""


def m7_run_reason(content: bytes) -> RunSpend | None:
    """A committed M7 run.json's spend: every live one counts (None: not a valid run.json)."""
    try:
        info = RedteamRunInfo.model_validate_json(content)
    except ValidationError:
        return None
    why = None if info.dry_run else "live redteam"
    return RunSpend(info.cost.total_usd, info.cost.llm_cost_complete, why)


def _m7_note_counts(note: RecordedSpend) -> bool:
    """Every folder M7's pattern matches is M7's (module docstring)."""
    return True


M7_RULES: Final = SpendRules(
    milestone="M7",
    decision="D7",
    ceiling=M7_CEILING_USD,
    branch=M7_LIVE_BRANCH,
    branch_decided="the M7 plan, PR 9",
    folder=M7_LIVE_FOLDER,
    disk_glob="redteam/*/*-live-*",
    run_reason=m7_run_reason,
    note_counts=_m7_note_counts,
    what_counts="-live-redteam, or without run.json",
)


def m7_spend(root: Path, head_paths: HeadPaths, head_file: HeadFile) -> MilestoneSpend:
    """Count M7's spend from HEAD (`count_spend` with `M7_RULES`)."""
    return count_spend(root, M7_RULES, head_paths, head_file)
