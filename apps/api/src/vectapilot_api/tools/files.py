"""Reading the tool configuration folder (`VECTAPILOT_TOOLS_CONFIG_DIR`, ADR 0016 decision 1).

    <dir>/README.md              optional, ignored
    <dir>/<business_id>/tools.json   the business's tools (`config.ToolsConfig`)
    <dir>/<business_id>/seed.json    its synthetic sandbox (`sandbox.SandboxSeed`)

Fail closed and fail fast: the API reads the whole folder once at startup (`create_app`), and any
problem stops it, naming the folder or business, the file and the rule. A missing folder, a folder
with no business, a business folder whose name is not a business id or differs from its files'
`business_id`, a missing or unknown file (a typo such as `tool.json` must not leave a business
silently unconfigured), or an invalid or inconsistent file are all refused. Names starting with a
dot (`.DS_Store`) are ignored. Files are read as UTF-8 and at most `MAX_FILE_BYTES` each.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.tools.config import (
    SEED_FILE,
    TOOLS_FILE,
    ToolsConfig,
    ToolsConfigError,
    parse_tools_config,
)
from vectapilot_api.tools.sandbox import SandboxSeed, parse_seed, seed_problems

README: Final = "README.md"
BUSINESS_FILES: Final = frozenset({TOOLS_FILE, SEED_FILE})
MAX_FILE_BYTES: Final = 256 * 1024
"""A bound, not a measurement: the committed files are a few kilobytes."""


@dataclass(frozen=True)
class BusinessTools:
    """One business's two files, validated and consistent."""

    config: ToolsConfig
    seed: SandboxSeed


def _read(path: Path, where: str) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        raise ToolsConfigError(f"{where} cannot be read") from None
    if len(data) > MAX_FILE_BYTES:
        raise ToolsConfigError(f"{where} is larger than {MAX_FILE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolsConfigError(f"{where} is not UTF-8") from None


def read_business_tools(folder: Path) -> BusinessTools:
    """`folder` (`<dir>/<business_id>`) as validated, mutually consistent files."""
    name = folder.name
    try:
        business_id = parse_business_id(name)
    except InvalidBusinessId:
        raise ToolsConfigError(
            f"tool configuration folder {name!r} is not named after a business id"
        ) from None
    if not folder.is_dir():
        raise ToolsConfigError(f"{business_id}: no tool configuration folder")
    present = {entry.name for entry in folder.iterdir() if not entry.name.startswith(".")}
    if unknown := sorted(present - BUSINESS_FILES):
        raise ToolsConfigError(f"{business_id}: unexpected file(s) {', '.join(unknown)}")
    if missing := sorted(BUSINESS_FILES - present):
        raise ToolsConfigError(f"{business_id}: missing {', '.join(missing)}")
    config = parse_tools_config(
        _read(folder / TOOLS_FILE, f"{business_id}/{TOOLS_FILE}"),
        where=f"{business_id}/{TOOLS_FILE}",
    )
    seed = parse_seed(
        _read(folder / SEED_FILE, f"{business_id}/{SEED_FILE}"), where=f"{business_id}/{SEED_FILE}"
    )
    if config.business_id != business_id:
        raise ToolsConfigError(f"{business_id}/{TOOLS_FILE} names a different business_id")
    if problems := seed_problems(seed, config):
        raise ToolsConfigError(f"{business_id}/{SEED_FILE}: {'; '.join(problems)}")
    return BusinessTools(config, seed)


def load_tools_dir(directory: Path) -> Mapping[BusinessId, BusinessTools]:
    """Every business in `directory`, read and validated; read-only, sorted by business id."""
    if not directory.is_dir():
        raise ToolsConfigError(
            "the tool configuration folder (VECTAPILOT_TOOLS_CONFIG_DIR) is missing"
        )
    found: dict[BusinessId, BusinessTools] = {}
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith(".") or (entry.name == README and entry.is_file()):
            continue
        if not entry.is_dir():
            raise ToolsConfigError(
                f"unexpected file {entry.name!r} in the tool configuration folder"
            )
        tools = read_business_tools(entry)
        found[tools.config.business_id] = tools
    if not found:
        raise ToolsConfigError("the tool configuration folder holds no business")
    return MappingProxyType(found)
