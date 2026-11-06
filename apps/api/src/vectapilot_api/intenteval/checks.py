"""One stage-aware check of an `intents` version, D3's minimums, and the freeze (M4 PR 1).

How CI stays green while the files are empty, and becomes strict, as for `support_qa`
(`evalset/checks.py`):
- `stage: collecting` (v0.1 as committed): every business resolves to a route template, and
  every row is checked on its own and against the others: the schema (on load), unique ids
  across both files, the business, the language, no placeholder marker, no personal data
  (`evalset/personal_data.py`), the near-duplicate screen (`similarity.py`), no split by hand,
  and, while `train.jsonl` is the recorded draft, a verification sample that is exactly the
  seeded draw. D3's minimums are not checked: zero rows pass. `report` shows how far each
  minimum is.
- `stage: frozen`: `freeze` sets it in the same change that writes the splits and freeze.json,
  and only when the minimums, the verification sample and the split's balance hold. From then on
  the same CI test enforces them and every pin; a later edit turns CI red.

A problem names the file, the row id and the rule, never a row's text. `check` never edits a row:
a near-duplicate pair is listed by id, and a human decides which row to drop or group.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.evalset.personal_data import personal_data_findings
from vectapilot_api.evalset.questions import PLACEHOLDER_MARKER, is_placeholder_text
from vectapilot_api.intenteval.rows import (
    EVAL_FILE,
    FREEZE_FILE,
    SAMPLE_FILE,
    TRAIN_FILE,
    BusinessProfile,
    EvalRow,
    IntentManifest,
    TrainRow,
    repeated_ids,
)
from vectapilot_api.intenteval.sample import draw_sample, sample_problems
from vectapilot_api.intenteval.similarity import near_pairs_between, near_pairs_within
from vectapilot_api.intenteval.split import (
    IntentFreeze,
    assign_splits,
    balance_problems,
    build_freeze,
    freeze_problems,
)
from vectapilot_api.intents.policy import RoutePolicy, UnknownBusinessType, policy_for
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

PLACEHOLDER_ID_PREFIX: Final = "example"
"""An id `example` or `example-...` is a placeholder, as in `support_qa`."""


@dataclass(frozen=True)
class IntentDataset:
    """One version as read from disk (`files.read_dataset`); pure values."""

    manifest: IntentManifest
    train: tuple[TrainRow, ...]
    eval_rows: tuple[EvalRow, ...]
    businesses: Mapping[str, BusinessProfile]
    """The listed businesses whose configuration was found (a missing one is a problem)."""
    freeze: IntentFreeze | None
    sample: tuple[str, ...] | None
    """`verification-sample.txt`'s ids; None when the file does not exist."""
    train_sha256: str
    """SHA-256 of `train.jsonl` as read (`report` compares it with `train_draft_sha256`)."""


def business_policies(dataset: IntentDataset) -> dict[str, RoutePolicy]:
    """The route template of every listed business whose configuration and type resolve."""
    policies: dict[str, RoutePolicy] = {}
    for business_id in dataset.manifest.businesses:
        profile = dataset.businesses.get(business_id)
        if profile is None:
            continue
        try:
            policies[business_id] = policy_for(profile.business_type)
        except UnknownBusinessType:
            continue
    return policies


def business_problems(dataset: IntentDataset) -> list[str]:
    """A listed business without a configuration, or whose type is unknown: `intenteval`
    refuses the dataset (ADR 0015 decision 2), never falls back to a default template."""
    problems: list[str] = []
    for business_id in dataset.manifest.businesses:
        profile = dataset.businesses.get(business_id)
        if profile is None:
            problems.append(
                f"business {business_id} has no manifest under {dataset.manifest.corpus}"
            )
            continue
        try:
            policy_for(profile.business_type)
        except UnknownBusinessType as error:
            problems.append(f"business {business_id}: {error}")
    return problems


def _is_placeholder(row: TrainRow | EvalRow) -> bool:
    prefixed = row.id == PLACEHOLDER_ID_PREFIX or row.id.startswith(f"{PLACEHOLDER_ID_PREFIX}-")
    return prefixed or is_placeholder_text(row.text)


def row_problems(dataset: IntentDataset, *, allow_placeholders: bool = False) -> list[str]:
    """Every problem with the rows, file by file in file order (the module docstring).
    `allow_placeholders` exists for tests only; the CLI and the dataset test never set it."""
    manifest = dataset.manifest
    problems = [
        f"row id {i} is used more than once" for i in repeated_ids(dataset.train, dataset.eval_rows)
    ]
    files: tuple[tuple[str, Sequence[TrainRow | EvalRow]], ...] = (
        (TRAIN_FILE, dataset.train),
        (EVAL_FILE, dataset.eval_rows),
    )
    for file_name, rows in files:
        for row in rows:
            where = f"{file_name} row {row.id}"
            # No early exit: every row's text goes through the screens below, so a writer who
            # fixes one problem never learns of personal data only on the next run (zara).
            profile = dataset.businesses.get(row.business_id)
            if row.business_id not in manifest.businesses:
                problems.append(f"{where}: its business is not in the dataset manifest")
            elif profile is not None and row.language not in profile.languages:
                problems.append(f"{where}: its business does not list {row.language}")
            if not allow_placeholders and _is_placeholder(row):
                problems.append(
                    f"{where}: is a placeholder (a marker such as {PLACEHOLDER_MARKER})"
                )
            problems += [
                f"{where}: text holds what looks like personal data ({finding}); invent it "
                "(README, 'Content rules')"
                for finding in personal_data_findings(row.text)
            ]
    if manifest.stage == "collecting":
        problems += [
            f"{EVAL_FILE} row {r.id}: split is set by `freeze`, never by hand (leave it null)"
            for r in dataset.eval_rows
            if r.split is not None
        ]
    return problems


def near_duplicate_problems(dataset: IntentDataset) -> list[str]:
    """Train-eval pairs at or above the threshold (refused), and eval-eval pairs at or above it
    that share no `split_group` (`similarity.py`)."""
    threshold = dataset.manifest.near_duplicate.threshold_percent
    train = [(r.id, r.text) for r in dataset.train]
    evals = [(r.id, r.text) for r in dataset.eval_rows]
    problems = [
        f"train row {p.first} and eval row {p.second} are near-duplicates ({p.percent} %, "
        f"threshold {threshold} %): drop the train row (README, 'Near-duplicates')"
        for p in near_pairs_between(train, evals, threshold)
    ]
    groups = {r.id: r.split_group for r in dataset.eval_rows}
    for pair in near_pairs_within(evals, threshold):
        group = groups[pair.first]
        if group is None or group != groups[pair.second]:
            problems.append(
                f"eval rows {pair.first} and {pair.second} are near-duplicates ({pair.percent} "
                f"%, threshold {threshold} %) and share no split_group: give both one, or drop "
                "one (README, 'Near-duplicates')"
            )
    return problems


def sample_draw_problems(dataset: IntentDataset) -> list[str]:
    """While `train.jsonl` is still the recorded draft (`train_draft_sha256`), the committed
    verification sample must be exactly the seeded draw (`sample.py`). After shesha's first label
    correction the strata change, the list cannot be re-derived, and git history is the audit."""
    draft = dataset.manifest.train_draft_sha256
    if dataset.sample is None or draft is None or draft != dataset.train_sha256:
        return []
    if dataset.sample == draw_sample(dataset.train, dataset.manifest.verification):
        return []
    return [
        f"{SAMPLE_FILE} is not the seeded draw of the training draft: run `sample` again, never "
        "choose which rows to verify"
    ]


def split_group_warnings(rows: Sequence[EvalRow]) -> list[str]:
    """Split groups with a single row: most likely a typo in the group's name."""
    sizes = Counter(r.split_group for r in rows if r.split_group is not None)
    return [f"split_group {g} has only one row" for g, n in sorted(sizes.items()) if n == 1]


@dataclass(frozen=True)
class Minimum:
    """One of D3's minimums: what is counted, how many there are, how many `freeze` needs."""

    what: str
    have: int
    need: int

    @property
    def met(self) -> bool:
        return self.have >= self.need


def minimums(dataset: IntentDataset) -> list[Minimum]:
    """Every minimum of `Requirements`, in a fixed order (zeros included). The per-business ones
    follow each business's route template: its safety intents, and Hindi only when it lists
    `hi`."""
    need = dataset.manifest.requirements
    rows = dataset.eval_rows
    found = [Minimum("eval rows", len(rows), need.min_eval_rows)]
    per_intent = Counter(r.intent for r in rows)
    found += [
        Minimum(f"eval rows of {i}", per_intent[i], need.min_eval_per_intent)
        for i in DECISION_ORDER
    ]
    for business_id, policy in business_policies(dataset).items():
        mine = [r for r in rows if r.business_id == business_id]
        counts = Counter(r.intent for r in mine)
        found += [
            Minimum(
                f"business {business_id}: eval rows of {i}",
                counts[i],
                need.min_eval_per_safety_intent_per_business,
            )
            for i in DECISION_ORDER
            if i in policy.safety_intents
        ]
        if "hi" in dataset.businesses[business_id].languages:
            hindi = [r for r in mine if r.language == "hi" and r.intent in policy.safety_intents]
            found.append(
                Minimum(
                    f"business {business_id}: eval safety rows in Hindi or Hinglish",
                    len(hindi),
                    need.min_hindi_safety_per_hindi_business,
                )
            )
            found.append(
                Minimum(
                    f"business {business_id}: eval emergency rows in Hindi or Hinglish",
                    sum(r.intent == Intent.EMERGENCY for r in hindi),
                    need.min_hindi_emergency_per_hindi_business,
                )
            )
    per_train_intent = Counter(r.intent for r in dataset.train)
    found += [
        Minimum(f"train rows of {i}", per_train_intent[i], need.min_train_per_intent)
        for i in DECISION_ORDER
    ]
    return found


def readiness_problems(dataset: IntentDataset) -> list[str]:
    """What is missing before the freeze: every unmet minimum, as `<what>: <have> of at least
    <need>`, then the verification sample's problems (`sample.py`)."""
    problems = [f"{m.what}: {m.have} of at least {m.need}" for m in minimums(dataset) if not m.met]
    return problems + sample_problems(dataset.train, dataset.sample, dataset.manifest.verification)


def check_dataset(dataset: IntentDataset, *, allow_placeholders: bool = False) -> CheckResult:
    """Every problem with the version at its stage (the module docstring)."""
    problems = [
        *business_problems(dataset),
        *row_problems(dataset, allow_placeholders=allow_placeholders),
        *near_duplicate_problems(dataset),
        *sample_draw_problems(dataset),
    ]
    if dataset.manifest.stage == "collecting":
        if dataset.freeze is not None:
            problems.append(f"{FREEZE_FILE} exists, but the manifest's stage is collecting")
    else:
        problems += readiness_problems(dataset)
        unassigned = sum(r.split is None for r in dataset.eval_rows)
        if unassigned:
            problems.append(f"{unassigned} eval row(s) have no split in a frozen version")
        if dataset.freeze is None:
            problems.append(f"the manifest's stage is frozen, but there is no {FREEZE_FILE}")
        else:
            problems += freeze_problems(
                dataset.manifest,
                dataset.freeze,
                dataset.train,
                dataset.eval_rows,
                dataset.businesses,
                dataset.sample,
            )
    return CheckResult(
        problems=tuple(problems), warnings=tuple(split_group_warnings(dataset.eval_rows))
    )


@dataclass(frozen=True)
class FrozenIntents:
    """What `freeze` writes: the manifest (`stage: frozen`), the eval rows with their splits, and
    freeze.json. `train.jsonl` is not rewritten."""

    manifest: IntentManifest
    eval_rows: tuple[EvalRow, ...]
    freeze: IntentFreeze


def freeze_dataset(
    dataset: IntentDataset, *, frozen_on: date, allow_placeholders: bool = False
) -> FrozenIntents:
    """Assign every eval row's split by the manifest's rule and pin the version. Refused
    (`FreezeRefused`) unless the version is collecting, `check` passes, D3's minimums and the
    verification sample hold and the splits are balanced (`split.balance_problems`; then
    `unbalanced` is set, whose way out is adding or removing one eval row). `allow_placeholders`
    is for tests only."""
    manifest = dataset.manifest
    if manifest.stage != "collecting":
        raise FreezeRefused([f"{manifest.version} is already frozen"])
    checked = check_dataset(dataset, allow_placeholders=allow_placeholders)
    problems = [*checked.problems, *readiness_problems(dataset)]
    if problems:
        raise FreezeRefused(problems)
    splits = assign_splits(dataset.eval_rows, manifest.split_rule)
    unbalanced = balance_problems(dataset.eval_rows, splits, business_policies(dataset))
    if unbalanced:
        raise FreezeRefused(unbalanced, unbalanced=True)
    assigned = tuple(
        EvalRow.model_validate({**r.model_dump(), "split": splits[r.id]}) for r in dataset.eval_rows
    )
    frozen_manifest = IntentManifest.model_validate({**manifest.model_dump(), "stage": "frozen"})
    freeze = build_freeze(
        frozen_manifest,
        dataset.train,
        assigned,
        dataset.businesses,
        dataset.sample or (),  # never empty here: readiness_problems refuses a missing sample
        frozen_on,
    )
    return FrozenIntents(manifest=frozen_manifest, eval_rows=assigned, freeze=freeze)
