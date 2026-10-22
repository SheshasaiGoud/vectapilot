"""One check of a whole dataset version, stage-aware, and the freeze itself (M2 PR 7).

How CI stays green while `questions.jsonl` is empty, and becomes strict:

- `stage: collecting` (v0.1 as committed): every row is checked on its own (schema, unique ids
  and texts, business, language, no placeholder, no personal data, evidence resolves, no split by
  hand). Sizes, coverage and the corpus review are not: zero rows pass. `ready` lists what is
  still missing and previews the split from a dry run, but CI does not run it.
- `stage: frozen`: `freeze` sets it, in the same reviewable change that writes the splits and
  freeze.json. It refuses while a corpus document waits for shesha's review
  (`corpus.review_problems`) and when the splits it would write are unbalanced (`split.py`). From
  then on the same CI test enforces the requirements, the review and every pin; a later edit
  that breaks one turns CI red.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import get_args

from vectapilot_api.evalset.corpus import Corpus, review_problems
from vectapilot_api.evalset.questions import (
    FREEZE_FILE,
    EvalManifest,
    EvalQuestion,
    Split,
    question_problems,
    readiness_problems,
    split_group_warnings,
    verbatim_warnings,
)
from vectapilot_api.evalset.split import (
    FreezeManifest,
    assign_splits,
    balance_groups,
    balance_problems,
    build_freeze,
    carried_problems,
    freeze_problems,
    pins_from,
)
from vectapilot_api.ingestion.chunking import DEFAULT_CHUNKING
from vectapilot_api.ingestion.types import ChunkingConfig


@dataclass(frozen=True)
class CheckResult:
    problems: tuple[str, ...]
    """Each one fails the check."""
    warnings: tuple[str, ...]
    """Worth a look; never a failure."""

    @property
    def ok(self) -> bool:
        return not self.problems


def check_dataset(
    manifest: EvalManifest,
    questions: Sequence[EvalQuestion],
    corpus: Corpus,
    *,
    freeze: FreezeManifest | None,
    previous_freeze: FreezeManifest | None = None,
    config: ChunkingConfig = DEFAULT_CHUNKING,
    allow_placeholders: bool = False,
) -> CheckResult:
    """Every problem with the version at its stage (see the module docstring)."""
    problems = [
        f"business {b} has no corpus under {manifest.corpus}"
        for b in manifest.businesses
        if b not in corpus.business_ids
    ]
    problems += question_problems(
        manifest, questions, corpus, config=config, allow_placeholders=allow_placeholders
    )
    if previous_freeze is not None:
        problems += carried_problems(previous_freeze, questions)
    else:
        problems += [
            f"question {q.id} sets `replaces`, but there is no previous version's {FREEZE_FILE}"
            for q in questions
            if q.replaces is not None
        ]
    if manifest.stage == "collecting":
        if freeze is not None:
            problems.append(f"{FREEZE_FILE} exists, but the manifest's stage is collecting")
    else:
        problems += readiness_problems(manifest, questions, corpus)
        problems += review_problems(corpus, manifest.businesses)
        unassigned = sum(q.split is None for q in questions)
        if unassigned:
            problems.append(f"{unassigned} question(s) have no split in a frozen version")
        if freeze is None:
            problems.append(f"the manifest's stage is frozen, but there is no {FREEZE_FILE}")
        else:
            problems += freeze_problems(manifest, freeze, questions, corpus.pins())
    warnings = [*verbatim_warnings(corpus, questions), *split_group_warnings(questions)]
    return CheckResult(problems=tuple(problems), warnings=tuple(warnings))


class FreezeRefused(Exception):
    """The dataset cannot be frozen yet; `problems` says why. `unbalanced`: the only problem is
    the split's balance, whose way out is adding or removing one question (`split.py`)."""

    def __init__(self, problems: Sequence[str], *, unbalanced: bool = False) -> None:
        super().__init__(f"cannot freeze: {len(problems)} problem(s)")
        self.problems = tuple(problems)
        self.unbalanced = unbalanced


@dataclass(frozen=True)
class Frozen:
    manifest: EvalManifest
    questions: tuple[EvalQuestion, ...]
    freeze: FreezeManifest


def freeze_dataset(
    manifest: EvalManifest,
    questions: Sequence[EvalQuestion],
    corpus: Corpus,
    *,
    previous_freeze: FreezeManifest | None,
    frozen_on: date,
    allow_placeholders: bool = False,
) -> Frozen:
    """Assign every split by the manifest's rule (earlier versions' ids keep theirs, a `replaces`
    takes the replaced id's) and pin the result. Refused unless the version is collecting, every
    row is valid, `ready` passes, no corpus document waits for review and the splits are balanced
    (`split.balance_problems`). `allow_placeholders` is for tests only; the CLI never sets it."""
    if manifest.stage != "collecting":
        raise FreezeRefused([f"{manifest.version} is already frozen"])
    if manifest.previous_version is not None and previous_freeze is None:
        raise FreezeRefused([f"{manifest.previous_version}'s {FREEZE_FILE} is missing"])
    checked = check_dataset(
        manifest,
        questions,
        corpus,
        freeze=None,
        previous_freeze=previous_freeze,
        allow_placeholders=allow_placeholders,
    )
    problems = [
        *checked.problems,
        *readiness_problems(manifest, questions, corpus),
        *review_problems(corpus, manifest.businesses),
    ]
    if problems:
        raise FreezeRefused(problems)
    splits = assign_splits(questions, manifest.split_rule, pins_from(previous_freeze))
    unbalanced = balance_problems(questions, splits)
    if unbalanced:
        raise FreezeRefused(unbalanced, unbalanced=True)
    assigned = tuple(
        EvalQuestion.model_validate({**q.model_dump(), "split": splits[q.id]}) for q in questions
    )
    frozen_manifest = EvalManifest.model_validate({**manifest.model_dump(), "stage": "frozen"})
    freeze = build_freeze(frozen_manifest, assigned, splits, corpus.pins(), frozen_on)
    return Frozen(manifest=frozen_manifest, questions=assigned, freeze=freeze)


@dataclass(frozen=True)
class SplitPreview:
    """What `freeze` would assign, from a dry run that writes nothing (`ready`)."""

    splits: Mapping[str, Split]
    rows: tuple[tuple[str, int, int], ...]
    """`(group, dev, test)` for every group of `split.balance_groups`, total first."""
    problems: tuple[str, ...]
    """`split.balance_problems`: each one would make `freeze` refuse."""


def preview_splits(
    manifest: EvalManifest,
    questions: Sequence[EvalQuestion],
    previous_freeze: FreezeManifest | None,
) -> SplitPreview:
    """Run the split rule as `freeze` would, and count dev and test per group."""
    splits = assign_splits(questions, manifest.split_rule, pins_from(previous_freeze))

    def counts(members: Sequence[EvalQuestion]) -> tuple[int, int]:
        tests = sum(splits[q.id] == "test" for q in members)
        return len(members) - tests, tests

    rows = [("total", *counts(questions))]
    rows += [(group.label, *counts(group.members)) for group in balance_groups(questions)]
    return SplitPreview(
        splits=splits, rows=tuple(rows), problems=tuple(balance_problems(questions, splits))
    )


SPLITS: tuple[Split, ...] = get_args(Split)


def select_split(questions: Sequence[EvalQuestion], split: Split) -> tuple[EvalQuestion, ...]:
    """The questions of one split. PR 8 tunes on `dev` only; refuses an unassigned dataset, so
    nothing is ever tuned on questions whose split is not fixed yet, and an unknown split name
    (a typo would otherwise select nothing)."""
    if split not in SPLITS:
        raise ValueError(f"split must be one of {list(SPLITS)}")
    if any(q.split is None for q in questions):
        raise ValueError("the dataset is not frozen: splits are unassigned")
    return tuple(q for q in questions if q.split == split)
