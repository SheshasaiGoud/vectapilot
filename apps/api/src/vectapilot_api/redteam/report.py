"""`report`: counts of a `redteam` version, and with the full report one line per case.

Codes and counts only, never a case's input or fixture text (an agent may run `report --counts`
on the probe, which it never reads; scoring rules, "Independence"). The full report adds one line
per case: its id, subgroup, business, language row, surface, route, expectation and label.
"""

from collections import Counter
from collections.abc import Callable, Iterable
from typing import Final

from vectapilot_api.redteam.cases import Case
from vectapilot_api.redteam.checks import RedteamDataset

_ROWS: Final[tuple[tuple[str, Callable[[Case], str]], ...]] = (
    ("subgroup", lambda c: c.subgroup),
    ("business", lambda c: c.business_id),
    ("language", lambda c: c.language_row),
    ("surface", lambda c: c.surface.value),
    ("route", lambda c: c.route.value),
    ("expect", lambda c: c.expect),
    ("obfuscation", lambda c: "-" if c.obfuscation is None else c.obfuscation.value),
)


def _counts(title: str, values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return [f"{title}:"] + [f"  {value:<28} {n:>4}" for value, n in sorted(counts.items())]


def render_report(dataset: RedteamDataset, *, counts_only: bool) -> str:
    manifest, cases = dataset.manifest, dataset.cases
    reviewed = sum(1 for case in cases if case.reviewed_by)
    lines = [
        f"{manifest.dataset} {manifest.version} ({manifest.stage}): {len(cases)} cases,"
        f" {len({case.family for case in cases})} families, {reviewed} reviewed by a person",
    ]
    for title, key in _ROWS:
        lines += _counts(title, (key(case) for case in cases))
    labels = dataset.gates.labels
    lines += _counts("label", (labels.get(case.id, "(none)") for case in cases))
    lines.append(f"max_over_escalated: {dataset.gates.max_over_escalated}")
    if not counts_only:
        lines.append("cases:")
        lines += [
            f"  {c.id}  {c.subgroup:<6} {c.business_id:<16} {c.language_row:<14}"
            f" {c.surface.value:<12} {c.route.value:<10} {c.expect:<20}"
            f" {labels.get(c.id, '(none)')}"
            for c in cases
        ]
    return "\n".join(lines) + "\n"
