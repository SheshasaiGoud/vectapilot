"""The freeze of a `redteam` version (M7 PR 2): what `freeze.json` pins, and the rule that gate
labels only tighten.

`freeze` runs before PR 4's term files exist (scoring rules, "Independence"). It pins:
- the SHA-256 of `cases.jsonl` and of `gates.json`, byte for byte;
- the gate labels and the ceiling as frozen (`gates`) [proposal: a digest alone cannot say *what*
  loosened, so the frozen labels are kept to compare with];
- the PII generator: its version, seed and test vectors (`pii.vector_digests`: digests, never
  values), so a changed generator shows before a frozen case runs with other values.

After the freeze, changing a case's text, fixture or `expect`, adding or removing a case, or a
changed generator is a new version (v0.2): every one is a problem. `gates.json` may change only
by tightening (`loosening_problems`): `known_gap` → `hard`, `soft` → `hard`, a lower ceiling.
Any other move (`hard` → `soft` or `known_gap`, `soft` ↔ `known_gap`, a raised ceiling) is
refused; a case that is truly wrong leaves only through v0.2, with the reason recorded.

Re-freezing is allowed until the PR that froze the version merges (nothing frozen is merged yet):
`freeze --refreeze` rewrites the pins and says so, so every re-freeze is reported.
"""

import hashlib
from datetime import date
from typing import Final

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion
from vectapilot_api.redteam.cases import FREEZE_FILE, GATES_FILE, Gates, Label
from vectapilot_api.redteam.pii import SEED_MAX, PiiGenerator, PiiType, vector_digests

RULES_NOTE: Final = "make a new version instead (v0.2, with the reason recorded)"
_RANK: Final[dict[Label, int]] = {"known_gap": 0, "soft": 0, "hard": 1}
"""Only a move up this rank is a tightening; between the two zeros nothing may move."""


class PiiPins(EvalModel):
    version: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,39}$")
    seed: int = Field(ge=0, le=SEED_MAX)
    vectors: dict[PiiType, Sha256]


class RedteamFreeze(EvalModel):
    """`freeze.json`: written by `freeze`, never by hand."""

    dataset: str = Field(min_length=1)
    version: DatasetVersion
    frozen_on: date
    cases_sha256: Sha256
    gates_sha256: Sha256
    gates: Gates
    pii: PiiPins


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pii_pins(generator: PiiGenerator, version: str) -> PiiPins:
    return PiiPins.model_validate(
        {"version": version, "seed": generator.seed, "vectors": vector_digests(generator)}
    )


def load_freeze(text: str) -> RedteamFreeze:
    try:
        return RedteamFreeze.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(freeze)")
    raise DatasetError(f"{FREEZE_FILE} is not a valid redteam freeze: {detail}")


def loosening_problems(frozen: Gates, current: Gates) -> list[str]:
    """Every label move or ceiling change of `current` that is not a tightening of `frozen`."""
    problems: list[str] = []
    if current.max_over_escalated > frozen.max_over_escalated:
        problems.append(
            f"{GATES_FILE} raises max_over_escalated above the frozen ceiling; labels only"
            f" tighten, so {RULES_NOTE}"
        )
    for case_id in sorted(set(frozen.labels) | set(current.labels)):
        before, after = frozen.labels.get(case_id), current.labels.get(case_id)
        if before is None or after is None:
            what = "added" if before is None else "removed"
            problems.append(f"{GATES_FILE}: case {case_id}'s label was {what} after the freeze")
        elif after != before and _RANK[after] <= _RANK[before]:
            problems.append(
                f"{GATES_FILE}: case {case_id} moved from {before} to {after}; labels only"
                f" tighten (known_gap or soft to hard), so {RULES_NOTE}"
            )
    return problems


def tightenings(frozen: Gates, current: Gates) -> list[str]:
    """The allowed moves since the freeze, for `check`'s warnings (each worth a note line)."""
    moved = [
        f"case {case_id}: {frozen.labels[case_id]} -> {label}"
        for case_id, label in sorted(current.labels.items())
        if case_id in frozen.labels
        and frozen.labels[case_id] != label
        and _RANK[label] > _RANK[frozen.labels[case_id]]
    ]
    if current.max_over_escalated < frozen.max_over_escalated:
        moved.append("max_over_escalated lowered")
    return moved
