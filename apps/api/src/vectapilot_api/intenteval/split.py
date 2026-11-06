"""The eval split of `intents` (M4 PR 1): `stratified-systematic-v2` over eval rows, and the freeze.

**The rule** is `evalset/split.py`'s v2 rule with this dataset's fields; its integer primitives
(`held_out_count`, `split_offset`, `systematic_positions`) are imported, and `evalset/split.py`
does not change. Its unit and repair code is typed to `support_qa`'s questions, so the same steps
are written out here for eval rows:
- **Units.** Eval rows that share a `split_group` (a message and its translations, one message
  written for both businesses, a near-duplicate pair) are one unit and always share a split;
  every other row is a unit of its own. Training rows are never split.
- **The line.** A unit is placed by its lead, the member that comes first in this order: author,
  language group, business, intent (in decision order), then `sha256("<seed>:<id>")`, a fixed
  shuffle nobody can steer by choosing text. Units line up by their leads.
- **Test.** `T = round(U * test_percent / 100)` of the `U` units (50 %: D3 (a)) go to test, at
  evenly spaced positions from a seeded offset, so every run of neighbours in the line (each
  author, each language group inside it, each business, each intent) gets its share, give or
  take one.
- **The repair**, one intent at a time in decision order (safety intents first, so they get the
  nearest partners): an intent found in 2 or more units and missing from one split swaps one of
  its units with the nearest unit of that split (same author, language group and business first,
  then the closest in the line) whose swap leaves every intent in each split where it already
  was. Only when no such partner exists does one unit move alone. Integer arithmetic only; no
  set's iteration order decides anything. The repair *tries* to put every such intent in both
  splits; a unit moved alone can undo an earlier intent's repair (split groups that mix intents
  make this possible), so it guarantees nothing by itself.

**Balance, checked before the freeze** (`balance_problems`, as `evalset/split.py`'s; noah's
review): `freeze` refuses, writing nothing, when an intent found in 2 or more units is missing
from a split; when a business's safety intent (its route template's, `intents.policy`) spans 2 or
more units and is missing from a split, since §6 judges safety recall per business; or when the
friends' rows, the Hindi and Hinglish rows or any business (each 2 or more rows) has fewer than
`max(1, count // 5)` rows in test or none in dev. **The way out** (README, "Split rule and
freeze"): add or remove one eval row and run `freeze` again. Never change the seed or the
percentage: trying seeds until the split looks right is choosing the test set by hand.

**The freeze.** `freeze` runs the rule once, on the complete eval set, writes each eval row's
`split`, and records `freeze.json`: the content hash of every training and eval row, each eval
row's split, the split and near-duplicate rules, each business's type and languages (a type
change moves its routes; a language change moves what a row may be), and the verification
sample's ids (the list the note's `x of n` changed labels is measured on). From then on the checks
(CI included) fail when anything frozen changes, appears or disappears; the way out is a new
version.

v0.1 is the first version, so there is no carrying of an earlier version's splits yet (unlike
`support_qa`'s `pinned` and `replaces`); a v0.2 adds that before it exists (README, "Adding a
version").
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split, SplitRule
from vectapilot_api.evalset.split import held_out_count, split_offset, systematic_positions
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.intenteval.rows import (
    FREEZE_FILE,
    BusinessProfile,
    EvalRow,
    IntentManifest,
    NearDuplicateRule,
    TrainRow,
)
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent


def content_hash(row: TrainRow | EvalRow) -> str:
    """SHA-256 of every field but `split` (pinned on its own), as canonical JSON (sorted keys, no
    spaces, UTF-8)."""
    payload = row.model_dump(mode="json", exclude={"split"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _order(row: EvalRow, seed: str) -> tuple[str, str, str, int, str]:
    shuffle = hashlib.sha256(f"{seed}:{row.id}".encode()).hexdigest()
    return (
        row.author,
        row.language_group,
        row.business_id,
        DECISION_ORDER.index(row.intent),
        shuffle,
    )


def unit_key(row: EvalRow) -> tuple[str, str]:
    """Rows with the same key share a split: `("split_group", <name>)`, or `("id", <id>)`."""
    if row.split_group is not None:
        return ("split_group", row.split_group)
    return ("id", row.id)


@dataclass(frozen=True)
class SplitUnit:
    """Eval rows that share a split, in rule order; the first (the lead) places the unit."""

    members: tuple[EvalRow, ...]

    @property
    def lead(self) -> EvalRow:
        return self.members[0]

    def has(self, intent: Intent) -> bool:
        return any(r.intent == intent for r in self.members)


def split_units(rows: Iterable[EvalRow], seed: str) -> list[SplitUnit]:
    """Every unit, its members in rule order, the units in the order of their leads."""
    by_key: defaultdict[tuple[str, str], list[EvalRow]] = defaultdict(list)
    for row in rows:
        by_key[unit_key(row)].append(row)
    units = [SplitUnit(tuple(sorted(m, key=lambda r: _order(r, seed)))) for m in by_key.values()]
    return sorted(units, key=lambda unit: _order(unit.lead, seed))


def _distance(a: EvalRow, b: EvalRow) -> int:
    """0: same author, language group and business; 1: same author and language group; 2: same
    author; 3: otherwise."""
    first = (a.author, a.language_group, a.business_id)
    second = (b.author, b.language_group, b.business_id)
    for distance, size in ((0, 3), (1, 2), (2, 1)):
        if first[:size] == second[:size]:
            return distance
    return 3


def _intent_counts(line: Sequence[SplitUnit], test: Sequence[bool], side: bool) -> Counter[str]:
    return Counter(
        r.intent for unit, t in zip(line, test, strict=True) if t is side for r in unit.members
    )


def _swap_keeps_intents(
    counts: Mapping[bool, Counter[str]], incoming: SplitUnit, outgoing: SplitUnit, side: bool
) -> bool:
    """Whether moving `incoming` into split `side` and `outgoing` out of it leaves every intent
    in both splits where it already was."""
    arriving: Counter[str] = Counter(r.intent for r in incoming.members)
    leaving: Counter[str] = Counter(r.intent for r in outgoing.members)
    for split, out, into in ((side, leaving, arriving), (not side, arriving, leaving)):
        before = counts[split]
        if any(before[i] - out[i] + into[i] <= 0 for i in before):
            return False
    return True


def repair_intents(line: Sequence[SplitUnit], test: list[bool]) -> None:
    """Give every intent found in 2 or more units a row in each split, in place (the module
    docstring). Intents in decision order; partners by the smallest `(distance, |i - j|, i, j)`."""
    for intent in DECISION_ORDER:
        members = [i for i, unit in enumerate(line) if unit.has(intent)]
        if len(members) < 2:
            continue
        for into_test in (True, False):
            if any(test[i] is into_test for i in members):
                continue
            counts = {side: _intent_counts(line, test, side) for side in (True, False)}
            choices = [
                (_distance(line[i].lead, line[j].lead), abs(i - j), i, j)
                for i in members
                for j, t in enumerate(test)
                if t is into_test and _swap_keeps_intents(counts, line[i], line[j], into_test)
            ]
            if choices:
                _, _, i, j = min(choices)
                test[i], test[j] = into_test, not into_test
            else:
                test[members[0]] = into_test


def assign_splits(rows: Sequence[EvalRow], rule: SplitRule) -> dict[str, Split]:
    """Each eval row id's split under `rule` (the module docstring)."""
    line = split_units(rows, rule.seed)
    n = len(line)
    chosen = set(
        systematic_positions(n, held_out_count(n, rule.test_percent), split_offset(rule.seed, n))
    )
    test = [i in chosen for i in range(n)]
    repair_intents(line, test)
    return {
        row.id: "test" if is_test else "dev"
        for unit, is_test in zip(line, test, strict=True)
        for row in unit.members
    }


HINDI_GROUP: Final = "hi + hi-code-mixed"


def balance_problems(
    rows: Sequence[EvalRow], splits: Mapping[str, Split], policies: Mapping[str, RoutePolicy]
) -> list[str]:
    """Why these splits must not be frozen (the module docstring, "Balance"), in a fixed order:
    the friends' rows, the Hindi and Hinglish rows, each business (in `policies`' order), each
    intent (decision order), then each business's safety intents. `policies` maps each business
    to its route template. Each problem names a group and its counts, never a row."""
    problems: list[str] = []

    def judge(label: str, members: Sequence[EvalRow], size: int, need: int) -> None:
        if size < 2:
            return
        n = len(members)
        tests = sum(splits[r.id] == "test" for r in members)
        if tests < need:
            problems.append(f"{label}: {tests} of {n} in test, fewer than {need}")
        if tests == n:
            problems.append(f"{label}: all {n} in test, none in dev")

    def share(label: str, members: Sequence[EvalRow]) -> None:
        """About the test share: at least a fifth in test, and not all of it."""
        judge(label, members, len(members), max(1, len(members) // 5))

    def both(label: str, members: Sequence[EvalRow]) -> None:
        """In both splits, if its rows sit in 2 or more units (one unit cannot be split)."""
        judge(label, members, len({unit_key(r) for r in members}), 1)

    share("author friend", [r for r in rows if r.author == "friend"])
    share(f"language {HINDI_GROUP}", [r for r in rows if r.language == "hi"])
    for business_id in policies:
        share(f"business {business_id}", [r for r in rows if r.business_id == business_id])
    for intent in DECISION_ORDER:
        both(f"intent {intent}", [r for r in rows if r.intent == intent])
    for business_id, policy in policies.items():
        for intent in DECISION_ORDER:
            if intent in policy.safety_intents:
                both(
                    f"business {business_id}, safety intent {intent}",
                    [r for r in rows if r.business_id == business_id and r.intent == intent],
                )
    return problems


# ── The freeze manifest ───────────────────────────────────────────────────────────────────────


class FrozenEvalRow(EvalModel):
    split: Split
    sha256: Sha256


class IntentFreeze(EvalModel):
    """`freeze.json`: what the frozen version is made of. Written by `freeze`, never by hand."""

    dataset: str = Field(min_length=1)
    version: DatasetVersion
    frozen_on: date
    split_rule: SplitRule
    near_duplicate: NearDuplicateRule
    businesses: dict[BusinessId, BusinessProfile] = Field(min_length=1)
    train_rows: dict[Slug, Sha256] = Field(min_length=1)
    eval_rows: dict[Slug, FrozenEvalRow] = Field(min_length=1)
    verification_sample: tuple[Slug, ...] = Field(min_length=1)
    """`verification-sample.txt`'s ids at the freeze, in file order: the list the note's `x of n`
    changed labels is measured on, so it cannot be swapped for another verified 20 % later."""


def load_freeze(text: str) -> IntentFreeze:
    try:
        return IntentFreeze.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(freeze)")
    raise DatasetError(f"{FREEZE_FILE} is not a valid intents freeze: {detail}")


def build_freeze(
    manifest: IntentManifest,
    train: Sequence[TrainRow],
    eval_rows: Sequence[EvalRow],
    businesses: Mapping[str, BusinessProfile],
    sample: Sequence[str],
    frozen_on: date,
) -> IntentFreeze:
    """The freeze of rows whose splits are assigned (`eval_rows`' `split` set), with the
    verification sample's ids (`sample`, as read from its file)."""
    frozen_eval: dict[str, FrozenEvalRow] = {}
    for row in eval_rows:
        if row.split is None:
            raise ValueError(f"eval row {row.id} has no split")
        frozen_eval[row.id] = FrozenEvalRow(split=row.split, sha256=content_hash(row))
    return IntentFreeze(
        dataset=manifest.dataset,
        version=manifest.version,
        frozen_on=frozen_on,
        split_rule=manifest.split_rule,
        near_duplicate=manifest.near_duplicate,
        businesses={b: businesses[b] for b in manifest.businesses},
        train_rows={r.id: content_hash(r) for r in train},
        eval_rows=frozen_eval,
        verification_sample=tuple(sample),
    )


def _row_changes(
    kind: str, frozen: Mapping[str, str], rows: Iterable[TrainRow | EvalRow], bump: str
) -> list[str]:
    present: dict[str, str] = {r.id: content_hash(r) for r in rows}
    problems = [
        f"{kind} row {i} was frozen and is gone; {bump}" for i in sorted(set(frozen) - set(present))
    ]
    for row_id, sha in present.items():
        if row_id not in frozen:
            problems.append(f"{kind} row {row_id} was added after the freeze; {bump}")
        elif sha != frozen[row_id]:
            problems.append(f"{kind} row {row_id} changed after the freeze; {bump}")
    return problems


def freeze_problems(
    manifest: IntentManifest,
    freeze: IntentFreeze,
    train: Sequence[TrainRow],
    eval_rows: Sequence[EvalRow],
    businesses: Mapping[str, BusinessProfile],
    sample: Sequence[str] | None,
) -> list[str]:
    """Everything that differs from the freeze, `sample` (the verification sample's ids, None
    without its file) included. Each problem says to make a new version."""
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
    if tuple(sample or ()) != freeze.verification_sample:
        problems.append(f"the verification sample changed after the freeze; {bump}")
    problems += _row_changes("train", freeze.train_rows, train, bump)
    frozen_hashes = {i: f.sha256 for i, f in freeze.eval_rows.items()}
    problems += _row_changes("eval", frozen_hashes, eval_rows, bump)
    for row in eval_rows:
        frozen = freeze.eval_rows.get(row.id)
        if frozen is not None and row.split != frozen.split:
            problems.append(f"eval row {row.id} moved from {frozen.split} to {row.split}; {bump}")
    return problems
