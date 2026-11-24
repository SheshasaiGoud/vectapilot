"""Shared fixtures for the widget tests (M8 PR 1): the committed widget folder, throwaway copies of
it, and the settings that serve it next to the real guard folder (`data/safety/`, whose guidance
the config route sends, S8)."""

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from tests.conftest import make_settings
from tests.guards.helpers import SAFETY_DIR, SAMPLE_CORPUS_DIR
from vectapilot_api.config import Settings

REPO_DIR: Final = Path(__file__).resolve().parents[4]
WIDGET_DIR: Final = REPO_DIR / "data" / "widget"
BUSINESSES: Final = ("paws-and-co", "sunrise-clinic")
"""The sample businesses with a widget file (data, not code: rule 9 holds for src/)."""
ORIGIN: Final = "https://api.vectapilot.example"
"""The API's public origin in these tests (synthetic, RFC 2606)."""


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_widget(destination: Path) -> Path:
    """A throwaway copy of `data/widget`."""
    return Path(shutil.copytree(WIDGET_DIR, destination))


def edit_widget(folder: Path, business_id: str, change: Callable[[dict[str, Any]], object]) -> Path:
    """Apply `change` to `business_id`'s file in the widget folder `folder`; the file's path."""
    path = folder / "businesses" / f"{business_id}.json"
    data = read_json(path)
    change(data)
    write_json(path, data)
    return path


def guidance_of(business_id: str) -> list[str]:
    """The business's guidance lines as its guard file holds them (S8: the one source)."""
    lines = read_json(SAFETY_DIR / "businesses" / f"{business_id}.json")["guidance"]
    assert isinstance(lines, list) and lines
    return [str(line) for line in lines]


def widget_settings(**overrides: Any) -> Settings:
    """Test settings serving the committed widget folder with the sample corpus and the real
    guard folder, the API's origin set (the session routes compare `Origin` with it)."""
    fields: dict[str, Any] = {
        "widget_config_dir": WIDGET_DIR,
        "corpus_dir": SAMPLE_CORPUS_DIR,
        "guard_config_dir": SAFETY_DIR,
        "public_origin": ORIGIN,
        "session_lookup_timeout_seconds": 0.05,
    }
    return make_settings(**(fields | overrides))
