"""`report` (M5 PR 6): counts per business, language group, stratum, flag, author and split, and
the minimums (D8's, and each business's emergency minimums decided 2026-11-11).

Counts describe the dataset; they measure nothing about the agent. Every stratum and every
language group a business allows is printed, zeros included, so an empty cell is visible. shesha
runs `check` and `report` until the minimums hold (PR 7 step 1). Ids and counts only, never a
scenario's text.
"""

from collections import Counter
from collections.abc import Sequence

from vectapilot_api.tooleval.checks import ToolCallsDataset, minimums
from vectapilot_api.tooleval.scenarios import STRATA, Scenario, groups_for

LABEL_WIDTH = 32
MINIMUM_WIDTH = 60
"""The minimums' label column, widened to the longest label (a long business id stays aligned)."""


def _table(
    title: str, rows: Sequence[str], columns: Sequence[str], counts: Counter[tuple[str, str]]
) -> list[str]:
    """`rows` down, `columns` across, with a total column and a total row."""
    widths = [max(len(c), 4) + 2 for c in columns]
    lines = [
        f"  {title:<{LABEL_WIDTH}}"
        + "".join(f"{c:>{w}}" for c, w in zip(columns, widths, strict=True))
        + f"{'total':>7}"
    ]
    for row in (*rows, "total"):
        cells = [
            sum(n for (r, c), n in counts.items() if c == column and row in (r, "total"))
            for column in columns
        ]
        lines.append(
            f"    {row:<{LABEL_WIDTH - 2}}"
            + "".join(f"{n:>{w}}" for n, w in zip(cells, widths, strict=True))
            + f"{sum(cells):>7}"
        )
    return lines


def _flags(scenarios: Sequence[Scenario]) -> list[str]:
    return [
        f"    confirm {sum(s.confirm for s in scenarios)} · steering seed text "
        f"{sum(s.seed == 'steering' for s in scenarios)} · read_only "
        f"{sum(s.read_only for s in scenarios)} · emergency {sum(s.emergency for s in scenarios)}"
    ]


def render_report(dataset: ToolCallsDataset) -> str:
    """The plain-text report (the module docstring)."""
    manifest = dataset.manifest
    scenarios = dataset.scenarios
    lines = [
        f"{manifest.dataset} {manifest.version} ({manifest.stage}, synthetic): "
        f"{len(scenarios)} scenarios",
        "",
    ]
    for author in ("shesha", "friend"):
        mine = [s for s in scenarios if s.author == author]
        lines.append(f"author {author}: {len(mine)} scenarios")
        for business_id in manifest.businesses:
            profile = dataset.businesses.get(business_id)
            ours = [s for s in mine if s.business_id == business_id]
            listed = list(groups_for(profile.languages if profile else ()))
            extra = sorted({s.language_group for s in ours} - set(listed))
            counts: Counter[tuple[str, str]] = Counter((s.stratum, s.language_group) for s in ours)
            lines += _table(business_id, STRATA, [*listed, *extra], counts)
            lines += _flags(ours)
        lines.append("")

    lines.append("splits")
    splits: Counter[tuple[str, str]] = Counter(
        (s.stratum, s.split or "unassigned") for s in scenarios
    )
    lines += _table("stratum", STRATA, ("dev", "test", "unassigned"), splits)
    lines.append("")

    lines.append("minimums (what freeze needs; D8, and the emergency minimums of 2026-11-11)")
    found = minimums(dataset)
    width = max([MINIMUM_WIDTH, *(len(m.what) for m in found)])
    for minimum in found:
        mark = "ok" if minimum.met else "short"
        lines.append(f"  {minimum.what:<{width}} {minimum.have:>4} of {minimum.need:<4} {mark}")
    return "\n".join(lines) + "\n"
