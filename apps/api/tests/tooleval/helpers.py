"""Shared helpers for the tool-call eval's tests: a throwaway repository, TEST-ONLY scenarios, and
frozen datasets made from them.

**Every scenario here is made up by the tests and carries the `TEST-ONLY` marker,** which the
committed dataset's check refuses (`allow_placeholders` is a test-only switch). None is an example
for the writers' brief: they exercise the schema, the checks and the plumbing, never the agent.

`make_repo` lays out `<tmp>/repo` like the repository: `evals/datasets/tool_calls/v0.1` (the
committed manifest, optionally with other businesses and lower minimums), `data/sample/<business>/
manifest.json` (the committed corpus manifests, renamed when asked) and `data/sandbox` (a copy of
the committed sandbox, renamed when asked). Renamed businesses keep their type, languages, tools
and seed, so the integration tests never write the fictional businesses' own rows.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Final

from tests.tools.helpers import CORPUS_DIR, REPO_ROOT, copy_sandbox, read_json, write_json
from vectapilot_api.tooleval.checks import FrozenToolCalls, ToolCallsDataset, freeze_dataset
from vectapilot_api.tooleval.files import read_dataset, write_frozen
from vectapilot_api.tooleval.scenarios import MANIFEST_FILE, SCENARIOS_FILE, Scenario
from vectapilot_api.tooleval.split import build_freeze

COMMITTED: Final = REPO_ROOT / "evals" / "datasets" / "tool_calls" / "v0.1"
DATASET_PARTS: Final = ("evals", "datasets", "tool_calls", "v0.1")
SUNRISE: Final = "sunrise-clinic"
PAWS: Final = "paws-and-co"
LOW_MINIMUMS: Final[Mapping[str, int]] = {
    "min_scenarios": 1,
    "min_per_business": 1,
    "min_per_language_group_per_business": 0,
    "min_confirm": 0,
    "min_steering": 0,
    "min_read_only": 0,
    "min_emergency": 0,
    "min_emergency_per_business": 0,
    "min_emergency_hindi_per_business": 0,
}
TODAY: Final = date(2026, 11, 11)


def make_repo(
    tmp_path: Path,
    *,
    renames: Mapping[str, str] | None = None,
    requirements: Mapping[str, int] | None = None,
) -> Path:
    """`<tmp_path>/repo` laid out like the repository (module docstring); returns the dataset
    folder. `renames` maps a committed business id to the id it gets here."""
    renames = dict(renames or {})
    root = tmp_path / "repo"
    dataset_dir = root.joinpath(*DATASET_PARTS)
    dataset_dir.mkdir(parents=True)
    manifest = read_json(COMMITTED / MANIFEST_FILE)
    manifest["businesses"] = [renames.get(b, b) for b in manifest["businesses"]]
    if requirements is not None:
        manifest["requirements"] = dict(requirements)
    manifest["stage"] = "collecting"
    write_json(dataset_dir / MANIFEST_FILE, manifest)
    (dataset_dir / SCENARIOS_FILE).write_text("", "utf-8")
    copy_sandbox(root / "data" / "sandbox", renames)
    for business in (SUNRISE, PAWS):
        corpus = read_json(CORPUS_DIR / business / "manifest.json")
        new = renames.get(business, business)
        corpus["business_id"] = new
        folder = root / "data" / "sample" / new
        folder.mkdir(parents=True)
        write_json(folder / "manifest.json", corpus)
    return dataset_dir


def letters(seed: str) -> str:
    """48 letters from `seed`'s hash: distinct messages that no near-duplicate or personal-data
    screen confuses (no digits)."""
    digest = hashlib.sha256(seed.encode()).hexdigest()[:48]
    return digest.translate(str.maketrans("0123456789", "ghijklmnop"))


def scenario(
    scenario_id: str,
    business_id: str = PAWS,
    *,
    kind: str = "reply",
    tool: str | None = None,
    arguments: Mapping[str, str] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A TEST-ONLY scenario as a JSON object (fields as in the README)."""
    expect: dict[str, Any] = {"kind": kind}
    if tool is not None:
        expect |= {"tool": tool, "arguments": dict(arguments or {})}
    data: dict[str, Any] = {
        "id": scenario_id,
        "business_id": business_id,
        "language": "en",
        "customer_ref": "cust-001",
        "message": f"TEST-ONLY {letters(scenario_id)}",
        "expect": expect,
        "confirm": False,
        "read_only": False,
        "emergency": False,
        "seed": "plain",
        "author": "shesha",
        "created": "2026-11-11",
        "synthetic": True,
    }
    return data | fields


def write_scenarios(dataset_dir: Path, scenarios: Sequence[Mapping[str, Any]]) -> None:
    text = "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in scenarios)
    (dataset_dir / SCENARIOS_FILE).write_text(text, "utf-8")


def read(dataset_dir: Path) -> ToolCallsDataset:
    return read_dataset(dataset_dir)


def frozen_repo(
    tmp_path: Path,
    scenarios: Sequence[Mapping[str, Any]],
    *,
    renames: Mapping[str, str] | None = None,
) -> Path:
    """A throwaway repository whose dataset holds `scenarios`, frozen (with the minimums at their
    lowest and placeholders allowed: tests only)."""
    dataset_dir = make_repo(tmp_path, renames=renames, requirements=LOW_MINIMUMS)
    write_scenarios(dataset_dir, scenarios)
    write_frozen(
        dataset_dir, freeze_dataset(read(dataset_dir), frozen_on=TODAY, allow_placeholders=True)
    )
    return dataset_dir


def parsed(data: Mapping[str, Any]) -> Scenario:
    return Scenario.model_validate(dict(data))


def frozen_on_split(
    tmp_path: Path,
    scenarios: Sequence[Mapping[str, Any]],
    split: str = "dev",
    *,
    renames: Mapping[str, str] | None = None,
) -> Path:
    """Like `frozen_repo`, but every scenario on `split`, set by hand: the harness's tests choose
    which scenarios run, so they never depend on the split rule's balance (tests only)."""
    dataset_dir = make_repo(tmp_path, renames=renames, requirements=LOW_MINIMUMS)
    write_scenarios(dataset_dir, scenarios)
    dataset = read(dataset_dir)
    assigned = tuple(
        Scenario.model_validate({**s.model_dump(), "split": split}) for s in dataset.scenarios
    )
    manifest = dataset.manifest.model_copy(update={"stage": "frozen"})
    freeze = build_freeze(manifest, assigned, dataset.businesses, dataset.sandbox_pins, TODAY)
    write_frozen(dataset_dir, FrozenToolCalls(manifest=manifest, scenarios=assigned, freeze=freeze))
    return dataset_dir
