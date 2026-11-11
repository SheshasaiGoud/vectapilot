"""One stage-aware check of a `tool_calls` version, D8's minimums, and the freeze (M5 PR 6).

How CI stays green while `scenarios.jsonl` is empty, and becomes strict (as `intents`):
- `stage: collecting` (v0.1 as committed): every business has its configuration (a corpus manifest
  for its languages, a sandbox folder for its customers and ids), and every scenario is checked on
  its own and against the others: the schema (on load), unique ids, the business, the language,
  the customer, an expectation the sandbox can meet (an enabled write tool, seeded ids, the
  customer's own booking, a configured ticket category), never `answer` (the harness's
  `router=none` cannot produce it), no placeholder marker, no personal data, the near-duplicate
  screen, and no split by hand. D8's minimums are not checked: zero scenarios pass; `report`
  shows how far each one is.
- `stage: frozen`: `freeze` sets it in the same change that writes the splits and freeze.json, and
  only when the minimums and the split's balance hold. From then on the same CI test enforces
  them and every pin.

A problem names the file, the scenario id and the rule, never a scenario's text. `check` never
edits a scenario.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from vectapilot_api.evalset.checks import CheckResult, FreezeRefused
from vectapilot_api.evalset.personal_data import personal_data_findings
from vectapilot_api.evalset.questions import PLACEHOLDER_MARKER, is_placeholder_text
from vectapilot_api.intenteval.checks import Minimum
from vectapilot_api.intenteval.rows import BusinessProfile
from vectapilot_api.intenteval.similarity import near_pairs_within
from vectapilot_api.tooleval.scenarios import (
    FREEZE_FILE,
    SCENARIOS_FILE,
    Scenario,
    ToolCallsManifest,
    groups_for,
    repeated_ids,
)
from vectapilot_api.tooleval.split import (
    HINDI_GROUP,
    SandboxPins,
    ToolCallsFreeze,
    assign_splits,
    balance_problems,
    build_freeze,
    freeze_problems,
)
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.files import BusinessTools

PLACEHOLDER_ID_PREFIX: Final = "example"


@dataclass(frozen=True)
class ToolCallsDataset:
    """One version as read from disk (`files.read_dataset`); pure values."""

    manifest: ToolCallsManifest
    scenarios: tuple[Scenario, ...]
    businesses: Mapping[str, BusinessProfile]
    """Each listed business's type and languages, from its corpus manifest (missing: a problem)."""
    sandbox: Mapping[str, BusinessTools]
    """Each listed business's `tools.json` and `seed.json`, validated (missing: a problem)."""
    sandbox_pins: Mapping[str, SandboxPins]
    freeze: ToolCallsFreeze | None


def business_problems(dataset: ToolCallsDataset) -> list[str]:
    """A listed business without a corpus manifest or a sandbox folder."""
    problems: list[str] = []
    for business_id in dataset.manifest.businesses:
        if business_id not in dataset.businesses:
            problems.append(
                f"business {business_id} has no manifest under {dataset.manifest.corpus}"
            )
        if business_id not in dataset.sandbox:
            problems.append(
                f"business {business_id} has no tool configuration under {dataset.manifest.sandbox}"
            )
    return problems


def _is_placeholder(scenario: Scenario) -> bool:
    prefixed = scenario.id == PLACEHOLDER_ID_PREFIX or scenario.id.startswith(
        f"{PLACEHOLDER_ID_PREFIX}-"
    )
    return prefixed or is_placeholder_text(scenario.message)


def expectation_problems(scenario: Scenario, tools: BusinessTools) -> list[str]:
    """Why the sandbox cannot meet `scenario`'s expectation, as fixed text (the ids it names are
    the scenario's own, never its message)."""
    expect = scenario.expect
    seed = tools.seed
    problems: list[str] = []
    if scenario.customer_ref not in seed.customers:
        problems.append("its customer_ref is not one of the business's seeded customers")
    if expect.kind == "answer":
        problems.append("expects answer, which the harness's router=none never produces")
    if expect.tool is None or expect.arguments is None:
        return problems
    if expect.tool not in tools.config.enabled_tools:
        problems.append(f"expects {expect.tool.value}, which the business does not enable")
    slots = {slot.id for slot in seed.slots}
    arguments = expect.arguments
    if expect.tool is ToolName.BOOK and arguments["slot_id"] not in slots:
        problems.append("expects a slot_id that is not a seeded slot")
    if expect.tool is ToolName.RESCHEDULE:
        own = {b.id for b in seed.bookings if b.customer_ref == scenario.customer_ref}
        if arguments["booking_id"] not in own:
            problems.append(
                "expects a booking_id that is not one of the customer's seeded bookings"
            )
        if arguments["new_slot_id"] not in slots:
            problems.append("expects a new_slot_id that is not a seeded slot")
    if (
        expect.tool is ToolName.CREATE_TICKET
        and tools.config.category_name(arguments["category"]) is None
    ):
        problems.append("expects a ticket category the business does not configure")
    return problems


def scenario_problems(dataset: ToolCallsDataset, *, allow_placeholders: bool = False) -> list[str]:
    """Every problem with the scenarios, in file order (the module docstring). `allow_placeholders`
    exists for tests only; the CLI and the dataset test never set it."""
    manifest = dataset.manifest
    problems = [f"scenario id {i} is used more than once" for i in repeated_ids(dataset.scenarios)]
    for scenario in dataset.scenarios:
        where = f"{SCENARIOS_FILE} scenario {scenario.id}"
        # No early exit: every message goes through the screens below (zara, M4).
        profile = dataset.businesses.get(scenario.business_id)
        if scenario.business_id not in manifest.businesses:
            problems.append(f"{where}: its business is not in the dataset manifest")
        elif profile is not None and scenario.language_group not in groups_for(profile.languages):
            problems.append(f"{where}: its business does not list {scenario.language}")
        tools = dataset.sandbox.get(scenario.business_id)
        if tools is not None:
            problems += [f"{where}: {p}" for p in expectation_problems(scenario, tools)]
        if not allow_placeholders and _is_placeholder(scenario):
            problems.append(f"{where}: is a placeholder (a marker such as {PLACEHOLDER_MARKER})")
        problems += [
            f"{where}: message holds what looks like personal data ({finding}); invent it "
            "(README, 'Content rules')"
            for finding in personal_data_findings(scenario.message)
        ]
        if manifest.stage == "collecting" and scenario.split is not None:
            problems.append(f"{where}: split is set by `freeze`, never by hand (leave it null)")
    return problems


def near_duplicate_problems(dataset: ToolCallsDataset) -> list[str]:
    """Scenario pairs at or above the threshold that share no `split_group`."""
    threshold = dataset.manifest.near_duplicate.threshold_percent
    groups = {s.id: s.split_group for s in dataset.scenarios}
    problems: list[str] = []
    pairs = near_pairs_within([(s.id, s.message) for s in dataset.scenarios], threshold)
    for pair in pairs:
        group = groups[pair.first]
        if group is None or group != groups[pair.second]:
            problems.append(
                f"scenarios {pair.first} and {pair.second} are near-duplicates ({pair.percent} %, "
                f"threshold {threshold} %) and share no split_group: give both one, or drop one "
                "(README, 'Near-duplicates')"
            )
    return problems


def split_group_warnings(scenarios: Sequence[Scenario]) -> list[str]:
    sizes = Counter(s.split_group for s in scenarios if s.split_group is not None)
    return [f"split_group {g} has only one scenario" for g, n in sorted(sizes.items()) if n == 1]


def minimums(dataset: ToolCallsDataset) -> list[Minimum]:
    """Every minimum, in a fixed order (zeros included): the total; for each business its
    scenarios, each language group its languages allow, its emergencies and, when it allows `hi`,
    its emergencies in Hindi or Hinglish (decided 2026-11-11); then the four flags."""
    need = dataset.manifest.requirements
    scenarios = dataset.scenarios
    found = [Minimum("scenarios", len(scenarios), need.min_scenarios)]
    for business_id in dataset.manifest.businesses:
        mine = [s for s in scenarios if s.business_id == business_id]
        found.append(
            Minimum(f"business {business_id}: scenarios", len(mine), need.min_per_business)
        )
        profile = dataset.businesses.get(business_id)
        groups = groups_for(profile.languages) if profile is not None else ()
        per_group = Counter(s.language_group for s in mine)
        found += [
            Minimum(
                f"business {business_id}: scenarios in {group}",
                per_group[group],
                need.min_per_language_group_per_business,
            )
            for group in groups
        ]
        sos = [s for s in mine if s.emergency]
        found.append(
            Minimum(
                f"business {business_id}: emergency scenarios",
                len(sos),
                need.min_emergency_per_business,
            )
        )
        if "hi" in groups:
            found.append(
                Minimum(
                    f"business {business_id}: emergency scenarios in {HINDI_GROUP}",
                    sum(s.language == "hi" for s in sos),
                    need.min_emergency_hindi_per_business,
                )
            )
    found += [
        Minimum("confirming scenarios", sum(s.confirm for s in scenarios), need.min_confirm),
        Minimum(
            "scenarios over steering seed text",
            sum(s.seed == "steering" for s in scenarios),
            need.min_steering,
        ),
        Minimum("read-only scenarios", sum(s.read_only for s in scenarios), need.min_read_only),
        Minimum(
            "emergency plus booking scenarios",
            sum(s.emergency for s in scenarios),
            need.min_emergency,
        ),
    ]
    return found


def readiness_problems(dataset: ToolCallsDataset) -> list[str]:
    return [f"{m.what}: {m.have} of at least {m.need}" for m in minimums(dataset) if not m.met]


def check_dataset(dataset: ToolCallsDataset, *, allow_placeholders: bool = False) -> CheckResult:
    """Every problem with the version at its stage (the module docstring)."""
    problems = [
        *business_problems(dataset),
        *scenario_problems(dataset, allow_placeholders=allow_placeholders),
        *near_duplicate_problems(dataset),
    ]
    if dataset.manifest.stage == "collecting":
        if dataset.freeze is not None:
            problems.append(f"{FREEZE_FILE} exists, but the manifest's stage is collecting")
    else:
        problems += readiness_problems(dataset)
        unassigned = sum(s.split is None for s in dataset.scenarios)
        if unassigned:
            problems.append(f"{unassigned} scenario(s) have no split in a frozen version")
        if dataset.freeze is None:
            problems.append(f"the manifest's stage is frozen, but there is no {FREEZE_FILE}")
        else:
            problems += freeze_problems(
                dataset.manifest,
                dataset.freeze,
                dataset.scenarios,
                dataset.businesses,
                dataset.sandbox_pins,
            )
    return CheckResult(
        problems=tuple(problems), warnings=tuple(split_group_warnings(dataset.scenarios))
    )


@dataclass(frozen=True)
class FrozenToolCalls:
    """What `freeze` writes: the manifest (`stage: frozen`), the scenarios with their splits, and
    freeze.json."""

    manifest: ToolCallsManifest
    scenarios: tuple[Scenario, ...]
    freeze: ToolCallsFreeze


def freeze_dataset(
    dataset: ToolCallsDataset, *, frozen_on: date, allow_placeholders: bool = False
) -> FrozenToolCalls:
    """Assign every scenario's split by the manifest's rule and pin the version. Refused
    (`FreezeRefused`) unless the version is collecting, `check` passes, D8's minimums hold and the
    splits are balanced (then `unbalanced` is set). `allow_placeholders` is for tests only."""
    manifest = dataset.manifest
    if manifest.stage != "collecting":
        raise FreezeRefused([f"{manifest.version} is already frozen"])
    checked = check_dataset(dataset, allow_placeholders=allow_placeholders)
    problems = [*checked.problems, *readiness_problems(dataset)]
    if problems:
        raise FreezeRefused(problems)
    splits = assign_splits(dataset.scenarios, manifest.split_rule)
    unbalanced = balance_problems(dataset.scenarios, splits, manifest.businesses)
    if unbalanced:
        raise FreezeRefused(unbalanced, unbalanced=True)
    assigned = tuple(
        Scenario.model_validate({**s.model_dump(), "split": splits[s.id]})
        for s in dataset.scenarios
    )
    frozen_manifest = ToolCallsManifest.model_validate({**manifest.model_dump(), "stage": "frozen"})
    freeze = build_freeze(
        frozen_manifest, assigned, dataset.businesses, dataset.sandbox_pins, frozen_on
    )
    return FrozenToolCalls(manifest=frozen_manifest, scenarios=assigned, freeze=freeze)
