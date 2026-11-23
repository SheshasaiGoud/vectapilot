"""Known gaps graduated to hard, code-side (decided 2026-11-21, shesha; plan, PR 6's decisions and
PR 7).

A frozen version's `gates.json` never changes, so a `known_gap` case that starts to pass (g-015
and g-030 once H1 matches PR 4's terms, `docs/notes/m7-term-review.md`) cannot be relabelled
there. Its strict xfail would turn XPASS and fail CI, as designed (the gap list stays true); the
fix is to graduate it here. `graduated.json`, next to this module, lists the graduated ids per
dataset version. Two readers, one file:
- the case marks and `redteam run` (`scored_label`): a graduated case is scored as **hard**: no
  known-gap mark, pending on its hand-writes like any hard case (`pending:H1` while H1 is a stub),
  then it must pass;
- `scripts/redteam_gate.py`, which imports nothing from the project, reads the same file by path:
  it counts a graduated case as hard, fails when it fails or is xfailed as a known gap, and
  refuses an id that is not a frozen `known_gap` case. A `known_gap` case that passes and is not
  graduated still fails the gate (a mark is missing).

Graduation only tightens (known_gap to hard, G2): an id the version does not label `known_gap`
is refused (`GraduationError`), so the file can never loosen a label.
"""

import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Final

from vectapilot_api.redteam.cases import Label

GRADUATED_FILE: Final = Path(__file__).with_name("graduated.json")
_CASE_ID: Final = re.compile(r"[a-l]-[0-9]{3}")


class GraduationError(ValueError):
    """`graduated.json` is unreadable or names a case it may not graduate (ids only)."""


def read_graduated(path: Path, version: str) -> frozenset[str]:
    """The ids `path` graduates for dataset `version` (none when the version is not listed)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GraduationError(f"cannot read {path.name}: {type(error).__name__}") from None
    versions = data.get("versions") if isinstance(data, dict) else None
    if not isinstance(versions, dict):
        raise GraduationError(f"{path.name} needs a versions object")
    ids = versions.get(version, [])
    if not isinstance(ids, list) or not all(
        isinstance(i, str) and _CASE_ID.fullmatch(i) for i in ids
    ):
        raise GraduationError(f"{path.name}: {version} lists case ids only")
    if len(set(ids)) != len(ids):
        raise GraduationError(f"{path.name}: {version} lists an id twice")
    return frozenset(ids)


def graduation_problems(graduated: frozenset[str], labels: Mapping[str, str]) -> list[str]:
    """Every graduated id that is not a `known_gap` case of `labels` (the version's gates)."""
    return [
        f"{case_id}: graduated, but not a frozen known_gap case"
        for case_id in sorted(graduated)
        if labels.get(case_id) != "known_gap"
    ]


@cache
def graduated_ids(version: str) -> frozenset[str]:
    """`graduated.json`'s ids for `version`."""
    return read_graduated(GRADUATED_FILE, version)


def scored_labels(labels: Mapping[str, Label], version: str) -> dict[str, Label]:
    """The labels a run scores with: `labels` (the version's `gates.json`) with every graduated
    case as `hard`. Raises `GraduationError` for an id that is not a `known_gap` case."""
    graduated = graduated_ids(version)
    problems = graduation_problems(graduated, labels)
    if problems:
        raise GraduationError("; ".join(problems))
    return {case_id: "hard" if case_id in graduated else label for case_id, label in labels.items()}
