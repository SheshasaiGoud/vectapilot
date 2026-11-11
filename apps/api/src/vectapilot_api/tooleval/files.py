"""Reading and writing a `tool_calls` version (the I/O shell around the pure modules).

Businesses are read from their configuration (rule 9): each corpus manifest
(`<corpus>/<business_id>/manifest.json`) for its type and languages, and each sandbox folder
(`<sandbox>/<business_id>/tools.json` and `seed.json`, validated as the API reads them) for its
enabled tools, ticket categories, customers and seeded ids. No document is loaded.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.files import repo_root_of
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.intenteval.files import read_businesses
from vectapilot_api.tooleval.checks import FrozenToolCalls, ToolCallsDataset
from vectapilot_api.tooleval.scenarios import (
    FREEZE_FILE,
    MANIFEST_FILE,
    SCENARIOS_FILE,
    dump_scenario,
    load_manifest,
    load_scenarios,
)
from vectapilot_api.tooleval.split import SandboxPins, load_freeze
from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE
from vectapilot_api.tools.files import BusinessTools, read_business_tools

DEFAULT_DATASET: Final = Path("evals", "datasets", "tool_calls", "v0.1")
"""The current version, relative to the repository root."""


def read_sandbox(
    sandbox_dir: Path, business_ids: Iterable[str]
) -> tuple[dict[str, BusinessTools], dict[str, SandboxPins]]:
    """Each listed business's validated tool files and their SHA-256 (a missing folder is
    skipped: the check reports it). Raises `ToolsConfigError` for an invalid folder."""
    tools: dict[str, BusinessTools] = {}
    pins: dict[str, SandboxPins] = {}
    for business_id in business_ids:
        folder = sandbox_dir / business_id
        if not folder.is_dir():
            continue
        tools[business_id] = read_business_tools(folder)
        pins[business_id] = SandboxPins(
            tools_sha256=hashlib.sha256((folder / TOOLS_FILE).read_bytes()).hexdigest(),
            seed_sha256=hashlib.sha256((folder / SEED_FILE).read_bytes()).hexdigest(),
        )
    return tools, pins


def _read(dataset_dir: Path, name: str) -> str:
    path = dataset_dir / name
    if not path.is_file():
        raise DatasetError(f"{name} is missing in {dataset_dir.name} (it may be empty, not absent)")
    return path.read_text("utf-8")


def read_dataset(
    dataset_dir: Path, corpus_dir: Path | None = None, sandbox_dir: Path | None = None
) -> ToolCallsDataset:
    """The version in `dataset_dir`, its businesses from `corpus_dir` and `sandbox_dir` (default:
    the manifest's folders under the repository root). Raises `DatasetError`, `CorpusError` or
    `ToolsConfigError`."""
    manifest = load_manifest(_read(dataset_dir, MANIFEST_FILE))
    root = repo_root_of(dataset_dir)
    tools, pins = read_sandbox(sandbox_dir or root / manifest.sandbox, manifest.businesses)
    freeze_path = dataset_dir / FREEZE_FILE
    return ToolCallsDataset(
        manifest=manifest,
        scenarios=load_scenarios(_read(dataset_dir, SCENARIOS_FILE)),
        businesses=read_businesses(corpus_dir or root / manifest.corpus, manifest.businesses),
        sandbox=tools,
        sandbox_pins=pins,
        freeze=load_freeze(freeze_path.read_text("utf-8")) if freeze_path.is_file() else None,
    )


def _json(data: Mapping[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_frozen(dataset_dir: Path, frozen: FrozenToolCalls) -> None:
    """Write the three files of a freeze: the scenarios with their splits, freeze.json and the
    manifest."""
    text = "".join(dump_scenario(s) + "\n" for s in frozen.scenarios)
    (dataset_dir / SCENARIOS_FILE).write_text(text, "utf-8")
    (dataset_dir / FREEZE_FILE).write_text(_json(frozen.freeze.model_dump(mode="json")), "utf-8")
    (dataset_dir / MANIFEST_FILE).write_text(
        _json(frozen.manifest.model_dump(mode="json")), "utf-8"
    )
