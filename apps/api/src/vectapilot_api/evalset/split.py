"""The held-out test split (M2 PR 7): a rule fixed before any question exists, and a freeze.

**Units.** Questions that share a `split_group` (a question and its translations, or the same
text asked of both businesses) always share a split (decided 2026-10-22, item 4); every other
question is a unit of its own. Without split groups, a unit is a question and everything below
reads "question". (A split group is not an evidence group, and not a balance group below.)

**The rule (`stratified-systematic-v2`, 30 % test).** A unit is placed by its lead, the member
that comes first in this order: author type, language group, business, category, then
`sha256("<seed>:<id>")` (a fixed shuffle nobody can steer by choosing text). Line up all the units
by their leads. `T = round(U * test_percent / 100)` of the `U` units go to test (at least 1 when
`U ≥ 2`, never all), at evenly spaced positions that start at a seeded offset
`r = sha256("<seed>:offset") mod U`: position `i` is test when `(i*T + r) // U` steps up between
`i` and `i + 1`. Evenly spaced positions over a sorted line give every run of neighbours its
share of test, give or take one: each author type is one run, each language group inside an
author is one run, and so on. So friends' questions, Hindi and Hinglish questions and each
business get about `test_percent` in test, and position 0 can be test too.

Then a fixed repair, one category at a time in name order: a category found in 2 or more units
and missing from one split swaps one of its units with the nearest unit of that split (same
author, language group and business first, then the closest in the line) whose swap leaves every
category in each split where it already was. The swap keeps `T`; only when no such partner
exists does one unit move alone. Integer arithmetic only, so no float decides a split, and no
set's iteration order decides anything (a golden test runs the rule under two `PYTHONHASHSEED`
values).

v1 (replaced on 2026-10-22, before any question existed: kenji's review, K1) ran the same idea per
category, with no offset. Position 0 was never test and the sort put `friend_blind` first, so
friends' questions almost never reached test, and Hindi rows and one business were over-sampled.

**Balance, checked before the freeze.** `freeze` refuses, and `ready` reports from a dry run, when
`friend_blind`, the Hindi and Hinglish questions or any business (each a group of 2 or more
questions) has fewer than `max(1, count // 5)` questions in test or none in dev, or a category
found in 2 or more units is missing from one split. **The way out** (README, "If freeze
refuses"): add or remove one question and run `ready` again. Never change the seed or the
percentage: trying seeds until the split looks right is choosing the test set by hand.

**The freeze.** `freeze` runs the rule once, on the complete set, writes each row's `split`, and
records `freeze.json`: every question's split and the SHA-256 of what defines it (`content_hash`),
the SHA-256 of every corpus file, and of each business's corpus manifest (which documents may
answer whom). From then on the checks (CI included) fail when a frozen question changes, moves
split, disappears or a new one appears, or a corpus file or manifest changes. The way out is a
new dataset version (README, "Adding a version"). It keeps every earlier id's split (`pinned`); a
question that corrects an earlier one names it in `replaces` and takes its split, a new question
that joins a split group with frozen members takes their split, and only the other new units go
through the rule. So a question tuned on in dev never becomes test, *as long as a corrected
question names the one it replaces*: the check can see a changed question that kept its id, but
not a rewrite filed as a new one.
"""

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import (
    CorpusPins,
    EvalModel,
    Sha256,
    describe_validation_error,
)
from vectapilot_api.evalset.questions import (
    FREEZE_FILE,
    DatasetError,
    DatasetVersion,
    EvalManifest,
    EvalQuestion,
    Split,
    SplitRule,
)
from vectapilot_api.gateway.types import BusinessId, Slug

HASHED_FIELDS: Final = frozenset(
    {
        "id",
        "replaces",
        "split_group",
        "business_id",
        "question",
        "language",
        "code_mixed",
        "category",
        "subtype",
        "author_type",
        "saw_documents",
        "expected_outcome",
        "expected_evidence",
        "reference_answer",
        "created",
        "synthetic",
    }
)
"""What defines a question. Left out: `split` (pinned on its own) and `notes` (comments)."""


def content_hash(question: EvalQuestion) -> str:
    """SHA-256 of the question's defining fields as canonical JSON (sorted keys, no spaces)."""
    payload = question.model_dump(mode="json", include=set(HASHED_FIELDS))
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def held_out_count(n: int, test_percent: int) -> int:
    """How many of `n` units go to test: `n * percent / 100` rounded half up, at least 1 when
    `n >= 2`, and never all `n`."""
    if n < 2:
        return 0
    return max(1, min(n - 1, (n * test_percent + 50) // 100))


def split_offset(seed: str, n: int) -> int:
    """Where the evenly spaced test positions start: `sha256("<seed>:offset") mod n` (0 when
    `n` is 0). Fixed by the seed, so nobody picks it."""
    if n == 0:
        return 0
    return int(hashlib.sha256(f"{seed}:offset".encode()).hexdigest(), 16) % n


def systematic_positions(n: int, tests: int, offset: int) -> tuple[int, ...]:
    """The `tests` positions of `0 … n-1` that go to test, evenly spaced from `offset`
    (`0 <= offset < n`, `0 <= tests <= n`): position `i` is test when `(i*tests + offset) // n`
    steps up between `i` and `i + 1`."""
    return tuple(i for i in range(n) if ((i + 1) * tests + offset) // n > (i * tests + offset) // n)


def _order(question: EvalQuestion, seed: str) -> tuple[str, str, str, str, str]:
    shuffle = hashlib.sha256(f"{seed}:{question.id}".encode()).hexdigest()
    return (
        question.author_type,
        question.language_group,
        question.business_id,
        question.category,
        shuffle,
    )


def unit_key(question: EvalQuestion) -> tuple[str, str]:
    """Questions with the same key share a split: `("split_group", <name>)`, or `("id", <id>)`
    for a question without a split group."""
    if question.split_group is not None:
        return ("split_group", question.split_group)
    return ("id", question.id)


@dataclass(frozen=True)
class SplitUnit:
    """Questions that share a split, in rule order; the first (the lead) places the unit."""

    members: tuple[EvalQuestion, ...]

    @property
    def lead(self) -> EvalQuestion:
        return self.members[0]

    def has(self, category: str) -> bool:
        return any(q.category == category for q in self.members)


def split_units(questions: Iterable[EvalQuestion], seed: str) -> list[SplitUnit]:
    """Every unit, its members in rule order, the units in the order of their leads."""
    by_key: defaultdict[tuple[str, str], list[EvalQuestion]] = defaultdict(list)
    for question in questions:
        by_key[unit_key(question)].append(question)
    units = [SplitUnit(tuple(sorted(m, key=lambda q: _order(q, seed)))) for m in by_key.values()]
    return sorted(units, key=lambda unit: _order(unit.lead, seed))


def _distance(a: EvalQuestion, b: EvalQuestion) -> int:
    """0: same author, language group and business; 1: same author and language group;
    2: same author; 3: otherwise. A swap at distance 0 changes no group's test count."""
    first = (a.author_type, a.language_group, a.business_id)
    second = (b.author_type, b.language_group, b.business_id)
    for distance, size in ((0, 3), (1, 2), (2, 1)):
        if first[:size] == second[:size]:
            return distance
    return 3


def _category_counts(line: Sequence[SplitUnit], test: Sequence[bool], side: bool) -> Counter[str]:
    """How many questions of each category are in split `side` (True: test)."""
    return Counter(
        q.category for unit, t in zip(line, test, strict=True) if t is side for q in unit.members
    )


def _swap_keeps_categories(
    counts: Mapping[bool, Counter[str]], incoming: SplitUnit, outgoing: SplitUnit, side: bool
) -> bool:
    """Whether moving `incoming` into split `side` and `outgoing` out of it leaves every category
    in both splits where it already was (`counts`: per category, before the swap)."""
    arriving: Counter[str] = Counter(q.category for q in incoming.members)
    leaving: Counter[str] = Counter(q.category for q in outgoing.members)
    for split, out, into in ((side, leaving, arriving), (not side, arriving, leaving)):
        before = counts[split]
        if any(before[c] - out[c] + into[c] <= 0 for c in before):
            return False
    return True


def repair_categories(line: Sequence[SplitUnit], test: list[bool]) -> None:
    """Give every category found in 2 or more units a question in each split, in place. `line`
    is in rule order and `test[i]` says whether `line[i]` is test. Categories are fixed in name
    order. A category missing from a split swaps one of its units into it with one unit already
    there, chosen among the swaps that leave every category in both splits where it was, by the
    smallest `(distance, |i - j|, i, j)` (`_distance` of the leads: same author, language group
    and business first). Only when no such partner exists does a unit move alone (the total
    changes by one)."""
    for category in sorted({q.category for unit in line for q in unit.members}):
        members = [i for i, unit in enumerate(line) if unit.has(category)]
        if len(members) < 2:
            continue
        for into_test in (True, False):
            if any(test[i] is into_test for i in members):
                continue
            # Every unit with this category is in the other split.
            counts = {side: _category_counts(line, test, side) for side in (True, False)}
            choices = [
                (_distance(line[i].lead, line[j].lead), abs(i - j), i, j)
                for i in members
                for j, t in enumerate(test)
                if t is into_test and _swap_keeps_categories(counts, line[i], line[j], into_test)
            ]
            if choices:
                _, _, i, j = min(choices)
                test[i], test[j] = into_test, not into_test
            else:
                test[members[0]] = into_test


def kept_split(question: EvalQuestion, pinned: Mapping[str, Split]) -> Split | None:
    """The split an earlier version fixed for this question: its own id's, or (for a correction)
    the split of the id it `replaces`; None for a new question."""
    if question.id in pinned:
        return pinned[question.id]
    if question.replaces is not None:
        return pinned.get(question.replaces)
    return None


def assign_splits(
    questions: Sequence[EvalQuestion],
    rule: SplitRule,
    pinned: Mapping[str, Split] | None = None,
) -> dict[str, Split]:
    """Each question id's split under `rule` (the module docstring). A question an earlier
    version fixed keeps that split (`kept_split`), a new question in a unit with such a question
    takes it too, and the rule places the other units."""
    pinned = pinned or {}
    splits: dict[str, Split] = {}
    line: list[SplitUnit] = []
    for unit in split_units(questions, rule.seed):
        known = [s for s in (kept_split(q, pinned) for q in unit.members) if s is not None]
        if not known:
            line.append(unit)
            continue
        # A split group that mixes frozen dev and test questions is refused (`carried_problems`);
        # its new members go to dev meanwhile, so a dry run never puts them in test.
        shared: Split = known[0] if all(s == known[0] for s in known) else "dev"
        for question in unit.members:
            splits[question.id] = kept_split(question, pinned) or shared
    n = len(line)
    chosen = set(
        systematic_positions(n, held_out_count(n, rule.test_percent), split_offset(rule.seed, n))
    )
    test = [i in chosen for i in range(n)]
    repair_categories(line, test)
    for unit, is_test in zip(line, test, strict=True):
        for question in unit.members:
            splits[question.id] = "test" if is_test else "dev"
    return splits


# ── Balance: what the freeze needs from the assigned splits ───────────────────────────────────


@dataclass(frozen=True)
class BalanceGroup:
    """Questions that share one value, whose dev and test counts `ready` shows and the freeze
    checks: `kind` is author, language, business or category."""

    kind: str
    name: str
    members: tuple[EvalQuestion, ...]

    @property
    def label(self) -> str:
        return f"{self.kind} {self.name}"


HINDI_GROUP: Final = "hi + hi-code-mixed"


def balance_groups(questions: Sequence[EvalQuestion]) -> list[BalanceGroup]:
    """The groups `ready` previews and the freeze checks, in a fixed order: each author type,
    each language group, Hindi and Hinglish together, each business, each category. A question
    is in several groups."""
    groups: list[BalanceGroup] = []

    def by(kind: str, values: Iterable[str], value_of: Callable[[EvalQuestion], str]) -> None:
        groups.extend(
            BalanceGroup(kind, v, tuple(q for q in questions if value_of(q) == v)) for v in values
        )

    by("author", ("team", "friend_blind"), lambda q: q.author_type)
    by("language", sorted({q.language_group for q in questions}), lambda q: q.language_group)
    by("language", [HINDI_GROUP], lambda q: HINDI_GROUP if q.language == "hi" else "")
    by("business", sorted({q.business_id for q in questions}), lambda q: q.business_id)
    by("category", sorted({q.category for q in questions}), lambda q: q.category)
    return groups


def _is_balanced_group(group: BalanceGroup) -> bool:
    """The groups that must each get about the test share (kenji's K1): friends' questions,
    Hindi and Hinglish together, and every business."""
    return (group.kind, group.name) in {
        ("author", "friend_blind"),
        ("language", HINDI_GROUP),
    } or group.kind == "business"


def balance_problems(questions: Sequence[EvalQuestion], splits: Mapping[str, Split]) -> list[str]:
    """Why these splits must not be frozen (the module docstring, "Balance"). Each problem names
    a group and its counts, never a question. A category counts its units: two translations in
    one split group cannot be in both splits."""
    problems: list[str] = []
    for group in balance_groups(questions):
        n = len(group.members)
        if _is_balanced_group(group):
            size, need = n, max(1, n // 5)
        elif group.kind == "category":
            size, need = len({unit_key(q) for q in group.members}), 1
        else:
            continue
        if size < 2:
            continue
        tests = sum(splits[q.id] == "test" for q in group.members)
        if tests < need:
            problems.append(f"{group.label}: {tests} of {n} in test, fewer than {need}")
        if tests == n:
            problems.append(f"{group.label}: all {n} in test, none in dev")
    return problems


# ── The freeze manifest ───────────────────────────────────────────────────────────────────────


class FrozenQuestion(EvalModel):
    split: Split
    sha256: Sha256


class FreezeManifest(EvalModel):
    """`freeze.json`: what the frozen version is made of. Written by `freeze`, never by hand."""

    dataset: str = Field(min_length=1)
    version: DatasetVersion
    frozen_on: date
    split_rule: SplitRule
    questions: dict[Slug, FrozenQuestion] = Field(min_length=1)
    corpus: dict[BusinessId, dict[str, Sha256]] = Field(min_length=1)
    """Every listed corpus file of the dataset's businesses and its SHA-256."""
    corpus_manifests: dict[BusinessId, Sha256] = Field(min_length=1)
    """Each business's corpus manifest, hashed (`corpus.manifest_sha256`): a status, audience,
    version or language change moves what may answer a customer even when no file changes."""


def load_freeze(text: str) -> FreezeManifest:
    try:
        return FreezeManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(freeze)")
    raise DatasetError(f"{FREEZE_FILE} is not a valid freeze manifest: {detail}")


def build_freeze(
    manifest: EvalManifest,
    questions: Sequence[EvalQuestion],
    splits: Mapping[str, Split],
    pins: CorpusPins,
    frozen_on: date,
) -> FreezeManifest:
    return FreezeManifest(
        dataset=manifest.dataset,
        version=manifest.version,
        frozen_on=frozen_on,
        split_rule=manifest.split_rule,
        questions={
            q.id: FrozenQuestion(split=splits[q.id], sha256=content_hash(q)) for q in questions
        },
        corpus={b: dict(pins.files[b]) for b in manifest.businesses},
        corpus_manifests={b: pins.manifests[b] for b in manifest.businesses},
    )


def freeze_problems(
    manifest: EvalManifest,
    freeze: FreezeManifest,
    questions: Sequence[EvalQuestion],
    pins: CorpusPins,
) -> list[str]:
    """Everything that differs from the freeze. Each problem says to bump the version."""
    bump = f"{manifest.version} is frozen: make a new version instead (README, 'Adding a version')"
    problems: list[str] = []
    if (freeze.dataset, freeze.version) != (manifest.dataset, manifest.version):
        problems.append(f"{FREEZE_FILE} belongs to another dataset or version")
    if freeze.split_rule != manifest.split_rule:
        problems.append(f"the split rule changed after the freeze; {bump}")
    present = {q.id for q in questions}
    for question_id in sorted(set(freeze.questions) - present):
        problems.append(f"question {question_id} was frozen and is gone; {bump}")
    for question in questions:
        frozen = freeze.questions.get(question.id)
        if frozen is None:
            problems.append(f"question {question.id} was added after the freeze; {bump}")
            continue
        if question.split != frozen.split:
            problems.append(
                f"question {question.id} moved from {frozen.split} to {question.split}; {bump}"
            )
        if content_hash(question) != frozen.sha256:
            problems.append(f"{frozen.split} question {question.id} changed; {bump}")
    for business_id in manifest.businesses:
        if dict(pins.files.get(business_id, {})) != freeze.corpus.get(business_id):
            problems.append(f"the corpus of {business_id} changed after the freeze; {bump}")
        if pins.manifests.get(business_id) != freeze.corpus_manifests.get(business_id):
            problems.append(
                f"the corpus manifest of {business_id} changed after the freeze; {bump}"
            )
    return problems


def carried_problems(previous: FreezeManifest, questions: Sequence[EvalQuestion]) -> list[str]:
    """A new version keeps an earlier id only for the very same question; a corrected question
    gets a new id and names the old one in `replaces`, which must be in the previous freeze,
    gone from this version, and replaced once; and a split group cannot join questions frozen in
    different splits."""
    present = {q.id for q in questions}
    problems = [
        f"question {q.id} reuses an id from {previous.version} for a changed question; "
        "give it a new id and set `replaces`"
        for q in questions
        if q.id in previous.questions and content_hash(q) != previous.questions[q.id].sha256
    ]
    replaced = Counter(q.replaces for q in questions if q.replaces is not None)
    for question in questions:
        old = question.replaces
        if old is None:
            continue
        if old not in previous.questions:
            problems.append(
                f"question {question.id} replaces {old}, which is not in {previous.version}'s "
                f"{FREEZE_FILE}"
            )
        elif old in present:
            problems.append(
                f"question {question.id} replaces {old}, which is still in this version"
            )
    problems += [f"question {old} is replaced {n} times" for old, n in replaced.items() if n > 1]
    pinned = pins_from(previous)
    frozen_in: defaultdict[str, Counter[Split]] = defaultdict(Counter)
    for question in questions:
        split = kept_split(question, pinned)
        if question.split_group is not None and split is not None:
            frozen_in[question.split_group][split] += 1
    problems += [
        f"split_group {group} joins questions frozen in dev and in test in {previous.version}; "
        "a split group shares one split"
        for group, splits in frozen_in.items()
        if len(splits) > 1
    ]
    return problems


def pins_from(previous: FreezeManifest | None) -> dict[str, Split]:
    return {} if previous is None else {i: f.split for i, f in previous.questions.items()}
