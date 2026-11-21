"""The red-team suite's businesses as an app serves them (M7 PR 2; scoring rules, "Which path
each agent case takes").

`rt-shop` is a test-only `ecommerce` business whose files live only in the dataset folder
(`fixtures/rt-shop/`: corpus manifest, documents, `tools.json`, `seed.json`, safety file), never
in `data/sample/` or `data/sandbox/`. The harness creates it as the canary creates its RETAIL
businesses (`tests/canary/write_paths.py`, `served_with_tools`): a temporary tool configuration
folder and corpus folder that hold the sample businesses' files plus the fixture business's, so
`create_app` serves all of them. Its database row is the test fixture's (`tests/redteam/
served.py`), created as the owner role and removed afterwards.

Everything is read through the real parsers (`parse_tools_config`, `parse_seed`,
`seed_problems`), and the fixture's type must agree with its corpus manifest (W11's rule,
`agent/profiles.py`), so a fixture the API would refuse is refused here first.

**The guard folder** (M7 PR 6, leo's PR 2 review): with `safety_dir`, `write_app_folders` also
writes a guard folder, `data/safety/` copied whole plus each fixture business's `safety.json` as
`businesses/<id>.json` (rt-shop's guidance lines), so the served app and the in-process runner
load the guards exactly as `create_app` does (`guards.config.load_guard_dir`).

**Which customer a case runs as** (`customer_ref_for`) [proposal, leo's PR 2 review]: orders are
customer-scoped (`tools/order_status.py`), so a case's turn runs as the seed owner of the first
order or booking its scripted steps name, else as the seed's first customer. Derived from the
case and the seed, never a case field, so the freeze pins nothing new.
"""

import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vectapilot_api.evalset.corpus import CORPUS_MANIFEST_FILE, load_corpus_manifest
from vectapilot_api.redteam.cases import FIXTURES_DIR, CallToolStep, Case
from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE, ToolsConfigError, parse_tools_config
from vectapilot_api.tools.files import BusinessTools
from vectapilot_api.tools.sandbox import SandboxSeed, parse_seed, seed_problems

TOOLS_DIR_NAME: Final = "tools"
RECORD_ARGUMENTS: Final = ("order_id", "booking_id")
CORPUS_DIR_NAME: Final = "corpus"
GUARD_DIR_NAME: Final = "safety"
SAFETY_FILE: Final = "safety.json"
"""A fixture business's guard file (`fixtures/<id>/safety.json`)."""


def fixture_business_tools(dataset_dir: Path, business_id: str) -> BusinessTools:
    """A fixture business's `tools.json` and `seed.json`, validated and consistent with each
    other and with its corpus manifest. Raises `ToolsConfigError` naming files, never content."""
    folder = dataset_dir / FIXTURES_DIR / business_id
    where = f"{FIXTURES_DIR}/{business_id}"
    config = parse_tools_config(
        (folder / TOOLS_FILE).read_text("utf-8"), where=f"{where}/{TOOLS_FILE}"
    )
    seed = parse_seed((folder / SEED_FILE).read_text("utf-8"), where=f"{where}/{SEED_FILE}")
    if config.business_id != business_id:
        raise ToolsConfigError(f"{where}/{TOOLS_FILE} names a different business_id")
    if problems := seed_problems(seed, config):
        raise ToolsConfigError(f"{where}/{SEED_FILE}: {'; '.join(problems)}")
    manifest = load_corpus_manifest(
        (folder / CORPUS_MANIFEST_FILE).read_text("utf-8"), where=f"{where}/manifest"
    )
    if manifest.business_type != config.business_type.value:
        raise ToolsConfigError(
            f"{where}: tools.json and the corpus manifest name different business types"
        )
    return BusinessTools(config, seed)


@dataclass(frozen=True)
class AppFolders:
    """`tools_config_dir`, `corpus_dir` and (with a safety folder) `guard_config_dir` for a served
    app (module docstring)."""

    tools_dir: Path
    corpus_dir: Path
    guard_dir: Path | None = None


def write_app_folders(
    into: Path,
    *,
    dataset_dir: Path,
    sandbox_dir: Path,
    corpus_dir: Path,
    fixture_businesses: Iterable[str],
    safety_dir: Path | None = None,
) -> AppFolders:
    """Copy every sample business with a sandbox (its two tool files and its corpus folder) and
    every fixture business (its tool files, validated, and its fixture folder as its corpus) into
    `into`; with `safety_dir`, the guard folder too (module docstring). Reads only; writes only
    under `into`."""
    tools, corpus = into / TOOLS_DIR_NAME, into / CORPUS_DIR_NAME
    tools.mkdir(parents=True)
    corpus.mkdir(parents=True)
    for folder in sorted(p for p in sandbox_dir.iterdir() if p.is_dir()):
        shutil.copytree(folder, tools / folder.name)
        shutil.copytree(corpus_dir / folder.name, corpus / folder.name)
    for business_id in fixture_businesses:
        business_tools = fixture_business_tools(dataset_dir, business_id)
        (tools / business_id).mkdir()
        (tools / business_id / TOOLS_FILE).write_text(
            business_tools.config.model_dump_json(), "utf-8"
        )
        (tools / business_id / SEED_FILE).write_text(business_tools.seed.model_dump_json(), "utf-8")
        shutil.copytree(dataset_dir / FIXTURES_DIR / business_id, corpus / business_id)
    if safety_dir is None:
        return AppFolders(tools_dir=tools, corpus_dir=corpus)
    guard = shutil.copytree(safety_dir, into / GUARD_DIR_NAME)
    for business_id in fixture_businesses:
        shutil.copyfile(
            dataset_dir / FIXTURES_DIR / business_id / SAFETY_FILE,
            guard / "businesses" / f"{business_id}.json",
        )
    return AppFolders(tools_dir=tools, corpus_dir=corpus, guard_dir=guard)


def customer_ref_for(case: Case, seed: SandboxSeed) -> str:
    """The customer `case`'s turn runs as (module docstring)."""
    owners = {order.id: order.customer_ref for order in seed.orders}
    owners |= {booking.id: booking.customer_ref for booking in seed.bookings}
    for step in case.fixture.scripted_model:
        if not isinstance(step, CallToolStep):
            continue
        for name in RECORD_ARGUMENTS:
            value = step.arguments.get(name)
            if isinstance(value, str) and value in owners:
                return owners[value]
    return seed.customers[0]
