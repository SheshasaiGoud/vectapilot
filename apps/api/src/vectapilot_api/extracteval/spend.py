"""M6's spend across its live runs (M6 PR 4; D8 (b), decided 2026-11-11: $1.00 for all of M6's
paid runs, live dev runs as needed and one test run, each still <= $1.00).

The counting core is M3's (`evalrun.spend.count_spend`); only the rules differ (`M6_RULES`):
- **What counts:** every M6 live folder committed in HEAD,
  `evals/reports/doc_extraction/<version>/<UTC>-live-extract-<split>` (the only paid runs: the
  route's gateway is always the fake provider, E11). Every such folder is M6's by its name, so a
  committed `recorded-spend.json` in one always counts (its `counts_toward_m3` field is M3's and
  is not read here).
- **A live folder without run.json** counts at M1's $1.00 per-run ceiling until its real spend is
  recorded and committed, as in M3 to M5: with D8's $1.00 ceiling, one such folder uses all of it.
- **One branch:** `m6/close-out` (plan PR 5: every live run is made, and its folder committed,
  there, before the next live run).
- **Never M3's, M4's or M5's:** `M6_LIVE_FOLDER` matches only M6's folders, and no M6 folder name
  matches M3's `LIVE_FOLDER`, M4's `M4_LIVE_FOLDER` or M5's `M5_LIVE_FOLDER`
  (tests/extracteval/test_spend.py, both ways), so their sums and refusals do not change, and
  theirs are never counted here.
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
from vectapilot_api.extracteval.runinfo import ExtractRunInfo

M6_CEILING_USD: Final = Decimal("1.00")
"""D8 (b), decided 2026-11-11 by shesha: all of M6's paid runs together."""

M6_LIVE_BRANCH: Final = "m6/close-out"

M6_LIVE_FOLDER: Final = re.compile(
    r"^evals/reports/doc_extraction/[^/]+/[^/]+-live-extract-(?:dev|test)$"
)
"""An M6 live folder, relative to the repository root (`runinfo.folder_name`)."""


def m6_run_reason(content: bytes) -> RunSpend | None:
    """A committed M6 run.json's spend: every live one counts (None: not a valid run.json)."""
    try:
        info = ExtractRunInfo.model_validate_json(content)
    except ValidationError:
        return None
    why = None if info.dry_run else f"live extract {info.split}"
    return RunSpend(info.cost.total_usd, info.cost.llm_cost_complete, why)


def _m6_note_counts(note: RecordedSpend) -> bool:
    """Every folder M6's pattern matches is M6's (module docstring)."""
    return True


M6_RULES: Final = SpendRules(
    milestone="M6",
    decision="D8",
    ceiling=M6_CEILING_USD,
    branch=M6_LIVE_BRANCH,
    branch_decided="the M6 plan, PR 5",
    folder=M6_LIVE_FOLDER,
    disk_glob="doc_extraction/*/*-live-*",
    run_reason=m6_run_reason,
    note_counts=_m6_note_counts,
    what_counts="-live-extract-<split>, or without run.json",
)


def m6_spend(root: Path, head_paths: HeadPaths, head_file: HeadFile) -> MilestoneSpend:
    """Count M6's spend from HEAD (`count_spend` with `M6_RULES`)."""
    return count_spend(root, M6_RULES, head_paths, head_file)
