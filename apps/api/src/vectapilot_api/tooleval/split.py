"""The split of `tool_calls` (M5 PR 6, D8 (b)): 50/50 by a rule fixed before any scenario, and the
freeze.

**The rule** is `evalset/split.py`'s `stratified-systematic-v2` with this dataset's fields; its
integer primitives (`held_out_count`, `split_offset`, `systematic_positions`) are imported and do
not change:
- **Units.** Scenarios that share a `split_group` are one unit and always share a split; every
  other scenario is a unit of its own.
- **The line.** A unit is placed by its lead, the member that comes first in this order: author,
  language group, business, stratum (`Scenario.stratum`, in `STRATA` order: emergency, confirm,
  steering, read-only, then the expected kind), then `sha256("<seed>:<id>")`, a fixed shuffle
  nobody can steer by choosing text.
- **Test.** `T = round(U * 50 / 100)` of the `U` units go to test, at evenly spaced positions from
  a seeded offset, so every run of neighbours in the line (each author, each language group inside
  it, each business, each stratum) gets about half, give or take one.
- **The repair**, one stratum at a time in `STRATA` order: a stratum found in 2 or more units and
  missing from one split swaps one of its units with the nearest unit of that split (same author,
  language group and business first, then the closest in the line) whose swap leaves every
  stratum in each split where it already was; only when no such partner exists does one unit move
  alone. Integer arithmetic only; no set's iteration order decides anything.

**Balance, checked before the freeze** (`balance_problems`): `freeze` refuses, writing nothing,
when a stratum or one of D8's flags (`confirm`, steering seed text, `read_only`, `emergency`)
found in 2 or more units is missing from a split, or when the friends' scenarios, the Hindi and
Hinglish scenarios or any business (each 2 or more) have fewer than `max(1, count // 5)` in test or
none in dev. Decided 2026-11-11: each business's emergency scenarios, and its Hindi and Hinglish
emergency scenarios, found in 2 or more units, also need one in each split (a business's safety
misses then show on dev and on test). **The way out** (README, "Split rule and freeze"): add or
remove one scenario and run `freeze` again. Never change the seed or the percentage: trying seeds
until the split looks right is choosing the test set by hand.

**The freeze** records `freeze.json`: every scenario's split and content hash, the split and
near-duplicate rules, each business's type and languages, and the SHA-256 of each business's
`tools.json` and `seed.json`: a scenario's expectation names seeded ids, so a changed seed changes
what it means. From then on the checks (CI included) fail when anything frozen changes, appears or
disappears; the way out is a new version.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split, SplitRule
from vectapilot_api.evalset.split import held_out_count, split_offset, systematic_positions
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.intenteval.rows import BusinessProfile, NearDuplicateRule
from vectapilot_api.tooleval.scenarios import (
    FREEZE_FILE,
    STRATA,
    Scenario,
    ToolCallsManifest,
)


def content_hash(scenario: Scenario) -> str:
    """SHA-256 of every field but `split` (pinned on its own), as canonical JSON (sorted keys, no
    spaces, UTF-8)."""
    payload = scenario.model_dump(mode="json", exclude={"split"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _order(scenario: Scenario, seed: str) -> tuple[str, str, str, int, str]:
    shuffle = hashlib.sha256(f"{seed}:{scenario.id}".encode()).hexdigest()
    return (
        scenario.author,
        scenario.language_group,
        scenario.business_id,
        STRATA.index(scenario.stratum),
        shuffle,
    )


def unit_key(scenario: Scenario) -> tuple[str, str]:
    """Scenarios with the same key share a split: `("split_group", <name>)`, or `("id", <id>)`."""
    if scenario.split_group is not None:
        return ("split_group", scenario.split_group)
    return ("id", scenario.id)


@dataclass(frozen=True)
class SplitUnit:
    """Scenarios that share a split, in rule order; the first (the lead) places the unit."""

    members: tuple[Scenario, ...]

    @property
    def lead(self) -> Scenario:
        return self.members[0]

    def has(self, stratum: str) -> bool:
        return any(s.stratum == stratum for s in self.members)


def split_units(scenarios: Iterable[Scenario], seed: str) -> list[SplitUnit]:
    """Every unit, its members in rule order, the units in the order of their leads."""
    by_key: defaultdict[tuple[str, str], list[Scenario]] = defaultdict(list)
    for scenario in scenarios:
        by_key[unit_key(scenario)].append(scenario)
    units = [SplitUnit(tuple(sorted(m, key=lambda s: _order(s, seed)))) for m in by_key.values()]
    return sorted(units, key=lambda unit: _order(unit.lead, seed))


def _distance(a: Scenario, b: Scenario) -> int:
    """0: same author, language group and business; 1: same author and language group; 2: same
    author; 3: otherwise."""
    first = (a.author, a.language_group, a.business_id)
    second = (b.author, b.language_group, b.business_id)
    for distance, size in ((0, 3), (1, 2), (2, 1)):
        if first[:size] == second[:size]:
            return distance
    return 3


def _counts(line: Sequence[SplitUnit], test: Sequence[bool], side: bool) -> Counter[str]:
    return Counter(
        s.stratum for unit, t in zip(line, test, strict=True) if t is side for s in unit.members
    )


def _swap_keeps_strata(
    counts: Mapping[bool, Counter[str]], incoming: SplitUnit, outgoing: SplitUnit, side: bool
) -> bool:
    """Whether moving `incoming` into split `side` and `outgoing` out of it leaves every stratum
    in both splits where it already was."""
    arriving: Counter[str] = Counter(s.stratum for s in incoming.members)
    leaving: Counter[str] = Counter(s.stratum for s in outgoing.members)
    for split, out, into in ((side, leaving, arriving), (not side, arriving, leaving)):
        before = counts[split]
        if any(before[k] - out[k] + into[k] <= 0 for k in before):
            return False
    return True


def repair_strata(line: Sequence[SplitUnit], test: list[bool]) -> None:
    """Give every stratum found in 2 or more units a scenario in each split, in place (the module
    docstring). Strata in `STRATA` order; partners by the smallest `(distance, |i - j|, i, j)`."""
    for stratum in STRATA:
        members = [i for i, unit in enumerate(line) if unit.has(stratum)]
        if len(members) < 2:
            continue
        for into_test in (True, False):
            if any(test[i] is into_test for i in members):
                continue
            counts = {side: _counts(line, test, side) for side in (True, False)}
            choices = [
                (_distance(line[i].lead, line[j].lead), abs(i - j), i, j)
                for i in members
                for j, t in enumerate(test)
                if t is into_test and _swap_keeps_strata(counts, line[i], line[j], into_test)
            ]
            if choices:
                _, _, i, j = min(choices)
                test[i], test[j] = into_test, not into_test
            else:
                test[members[0]] = into_test


def assign_splits(scenarios: Sequence[Scenario], rule: SplitRule) -> dict[str, Split]:
    """Each scenario id's split under `rule` (the module docstring)."""
    line = split_units(scenarios, rule.seed)
    n = len(line)
    chosen = set(
        systematic_positions(n, held_out_count(n, rule.test_percent), split_offset(rule.seed, n))
    )
    test = [i in chosen for i in range(n)]
    repair_strata(line, test)
    return {
        scenario.id: "test" if is_test else "dev"
        for unit, is_test in zip(line, test, strict=True)
        for scenario in unit.members
    }


HINDI_GROUP: Final = "hi + hi-code-mixed"

FLAGS: Final[tuple[tuple[str, Callable[[Scenario], bool]], ...]] = (
    ("confirm", lambda s: s.confirm),
    ("steering seed text", lambda s: s.seed == "steering"),
    ("read_only", lambda s: s.read_only),
    ("emergency", lambda s: s.emergency),
)
"""D8's flags: each one's scenarios are reported apart, so each must reach both splits."""


def balance_problems(
    scenarios: Sequence[Scenario], splits: Mapping[str, Split], businesses: Sequence[str]
) -> list[str]:
    """Why these splits must not be frozen (the module docstring, "Balance"), in a fixed order:
    the friends' scenarios, the Hindi and Hinglish ones, each business, each stratum, each flag,
    then each business's emergencies and its Hindi and Hinglish emergencies.
    Each problem names a group and its counts, never a scenario."""
    problems: list[str] = []

    def judge(label: str, members: Sequence[Scenario], size: int, need: int) -> None:
        if size < 2:
            return
        n = len(members)
        tests = sum(splits[s.id] == "test" for s in members)
        if tests < need:
            problems.append(f"{label}: {tests} of {n} in test, fewer than {need}")
        if tests == n:
            problems.append(f"{label}: all {n} in test, none in dev")

    def share(label: str, members: Sequence[Scenario]) -> None:
        judge(label, members, len(members), max(1, len(members) // 5))

    def both(label: str, members: Sequence[Scenario]) -> None:
        judge(label, members, len({unit_key(s) for s in members}), 1)

    share("author friend", [s for s in scenarios if s.author == "friend"])
    share(f"language {HINDI_GROUP}", [s for s in scenarios if s.language == "hi"])
    for business_id in businesses:
        share(f"business {business_id}", [s for s in scenarios if s.business_id == business_id])
    for stratum in STRATA:
        both(f"stratum {stratum}", [s for s in scenarios if s.stratum == stratum])
    for name, flagged in FLAGS:
        both(f"flag {name}", [s for s in scenarios if flagged(s)])
    for business_id in businesses:
        sos = [s for s in scenarios if s.business_id == business_id and s.emergency]
        both(f"business {business_id} emergency", sos)
        both(
            f"business {business_id} emergency in {HINDI_GROUP}",
            [s for s in sos if s.language == "hi"],
        )
    return problems


# ── The freeze manifest ───────────────────────────────────────────────────────────────────────


class FrozenScenario(EvalModel):
    split: Split
    sha256: Sha256


class SandboxPins(EvalModel):
    """A business's tool configuration as frozen: `tools.json` and `seed.json`, byte for byte."""

    tools_sha256: Sha256
    seed_sha256: Sha256


class ToolCallsFreeze(EvalModel):
    """`freeze.json`: what the frozen version is made of. Written by `freeze`, never by hand."""

    dataset: str = Field(min_length=1)
    version: DatasetVersion
    frozen_on: date
    split_rule: SplitRule
    near_duplicate: NearDuplicateRule
    businesses: dict[BusinessId, BusinessProfile] = Field(min_length=1)
    sandbox: dict[BusinessId, SandboxPins] = Field(min_length=1)
    scenarios: dict[Slug, FrozenScenario] = Field(min_length=1)


def load_freeze(text: str) -> ToolCallsFreeze:
    try:
        return ToolCallsFreeze.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(freeze)")
    raise DatasetError(f"{FREEZE_FILE} is not a valid tool_calls freeze: {detail}")


def build_freeze(
    manifest: ToolCallsManifest,
    scenarios: Sequence[Scenario],
    businesses: Mapping[str, BusinessProfile],
    sandbox: Mapping[str, SandboxPins],
    frozen_on: date,
) -> ToolCallsFreeze:
    """The freeze of scenarios whose splits are assigned."""
    frozen: dict[str, FrozenScenario] = {}
    for scenario in scenarios:
        if scenario.split is None:
            raise ValueError(f"scenario {scenario.id} has no split")
        frozen[scenario.id] = FrozenScenario(split=scenario.split, sha256=content_hash(scenario))
    return ToolCallsFreeze(
        dataset=manifest.dataset,
        version=manifest.version,
        frozen_on=frozen_on,
        split_rule=manifest.split_rule,
        near_duplicate=manifest.near_duplicate,
        businesses={b: businesses[b] for b in manifest.businesses},
        sandbox={b: sandbox[b] for b in manifest.businesses},
        scenarios=frozen,
    )


def freeze_problems(
    manifest: ToolCallsManifest,
    freeze: ToolCallsFreeze,
    scenarios: Sequence[Scenario],
    businesses: Mapping[str, BusinessProfile],
    sandbox: Mapping[str, SandboxPins],
) -> list[str]:
    """Everything that differs from the freeze. Each problem says to make a new version."""
    bump = f"{manifest.version} is frozen: make a new version instead (README, 'Adding a version')"
    problems: list[str] = []
    if (freeze.dataset, freeze.version) != (manifest.dataset, manifest.version):
        problems.append(f"{FREEZE_FILE} belongs to another dataset or version")
    if freeze.split_rule != manifest.split_rule:
        problems.append(f"the split rule changed after the freeze; {bump}")
    if freeze.near_duplicate != manifest.near_duplicate:
        problems.append(f"the near-duplicate rule changed after the freeze; {bump}")
    for business_id in sorted(set(freeze.businesses) | set(manifest.businesses)):
        if freeze.businesses.get(business_id) != businesses.get(business_id):
            problems.append(
                f"business {business_id}'s type or languages changed after the freeze; {bump}"
            )
        if freeze.sandbox.get(business_id) != sandbox.get(business_id):
            problems.append(
                f"business {business_id}'s tools.json or seed.json changed after the freeze; {bump}"
            )
    present = {s.id: content_hash(s) for s in scenarios}
    problems += [
        f"scenario {i} was frozen and is gone; {bump}"
        for i in sorted(set(freeze.scenarios) - set(present))
    ]
    for scenario in scenarios:
        frozen = freeze.scenarios.get(scenario.id)
        if frozen is None:
            problems.append(f"scenario {scenario.id} was added after the freeze; {bump}")
        elif present[scenario.id] != frozen.sha256:
            problems.append(f"scenario {scenario.id} changed after the freeze; {bump}")
        elif scenario.split != frozen.split:
            problems.append(
                f"scenario {scenario.id} moved from {frozen.split} to {scenario.split}; {bump}"
            )
    return problems
