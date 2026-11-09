"""D1 (c): scikit-learn is a dev dependency only, and the API never loads an ML library (M4 PR 4,
ADR 0015 decision 4).

Three layers: a fresh interpreter imports the API (and the router and the dataset CLI) and no ML
module appears in `sys.modules`; `uv.lock` shows that no runtime dependency reaches scikit-learn,
numpy or scipy (what `uv sync --no-dev` would install); the Dockerfile installs with `--no-dev`.
"""

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Final

import pytest

API_DIR: Final = Path(__file__).parents[2]
ML_MODULES: Final = ("sklearn", "numpy", "scipy", "joblib", "threadpoolctl", "narwhals")
PROBE: Final = """
import sys
import {modules}
loaded = sorted({{m.split('.')[0] for m in sys.modules}} & set({ml!r}))
print(','.join(loaded))
"""


@pytest.mark.parametrize(
    "modules",
    [
        "vectapilot_api.main",
        "vectapilot_api.intents, vectapilot_api.intents.decide, vectapilot_api.intents.fallback",
        "vectapilot_api.intenteval.cli, vectapilot_api.intenteval.train",
    ],
    ids=["api", "router", "intenteval-cli"],
)
def test_importing_loads_no_ml_library(modules: str) -> None:
    probe = PROBE.format(modules=modules, ml=ML_MODULES)
    # A fresh interpreter (this test process may have imported sklearn in another test).
    done = subprocess.run(  # noqa: S603 (our own interpreter, our own fixed code)
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=API_DIR
    )
    assert done.stdout.strip() == "", f"{modules} loaded {done.stdout.strip()}"


def _lock() -> dict[str, Any]:
    with (API_DIR / "uv.lock").open("rb") as handle:
        return tomllib.load(handle)


def _closure(packages: dict[str, dict[str, Any]], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        package = packages.get(name, {})
        pending += [d["name"] for d in package.get("dependencies", [])]
        for extra in package.get("optional-dependencies", {}).values():
            pending += [d["name"] for d in extra]
    return seen


def test_no_runtime_dependency_reaches_an_ml_library() -> None:
    lock = _lock()
    packages = {p["name"]: p for p in lock["package"]}
    project = packages["vectapilot-api"]
    runtime = _closure(packages, [d["name"] for d in project["dependencies"]])
    dev = [d["name"] for d in project["dev-dependencies"]["dev"]]

    assert "scikit-learn" in dev
    assert runtime.isdisjoint({"scikit-learn", "numpy", "scipy", "joblib", "threadpoolctl"})


def test_scikit_learn_is_pinned_exactly_and_has_one_mypy_override() -> None:
    with (API_DIR / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    assert "scikit-learn==1.9.1" in pyproject["dependency-groups"]["dev"]
    assert not any("scikit" in d for d in pyproject["project"]["dependencies"])
    assert pyproject["tool"]["mypy"]["overrides"] == [
        {"module": ["sklearn.*"], "ignore_missing_imports": True}
    ]


def test_the_image_installs_without_dev_dependencies() -> None:
    dockerfile = (API_DIR / "Dockerfile").read_text("utf-8")
    syncs = [line for line in dockerfile.splitlines() if "uv sync" in line]

    assert syncs and all("--no-dev" in line and "--locked" in line for line in syncs)
