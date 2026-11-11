"""The MCP SDK stays out of the API (ADR 0016 decision 8, D1 (a); M5 PR 5). Unit.

`mcp` lives only in `packages/mcp_server/`, its own uv project and lock, which depends on this
package by path. This project must never get it, its wire types or classic `httpx` (gone since
ADR 0006: `TestClient` and the providers use `httpx2`), so the API image never ships them. Read
from this project's own lock and pyproject, and from every import under `src/`: a dependency
added by hand, by a re-lock or by an import would each turn this red.
"""

import ast
import tomllib
from pathlib import Path
from typing import Any, Final

from tests.tools.helpers import REPO_ROOT

API_DIR: Final = REPO_ROOT / "apps" / "api"
SRC: Final = API_DIR / "src" / "vectapilot_api"
KEPT_OUT: Final = frozenset({"mcp", "mcp-types", "httpx", "httpcore", "sse-starlette"})
"""The SDK, its wire types, classic httpx and its transport, and the SDK's SSE server."""
KEPT_OUT_MODULES: Final = frozenset({"mcp", "mcp_types", "httpx", "httpcore", "sse_starlette"})


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _requirement_name(requirement: str) -> str:
    """`"mcp==2.3.0"` → `"mcp"` (enough for this check: names, then a specifier or extras)."""
    for stop in "=<>!~[; ":
        requirement = requirement.split(stop, 1)[0]
    return requirement.strip().lower()


def imported_top_levels(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_api_lock_holds_no_mcp_and_no_classic_httpx() -> None:
    locked = {package["name"] for package in _toml(API_DIR / "uv.lock")["package"]}
    assert locked.isdisjoint(KEPT_OUT), sorted(locked & KEPT_OUT)
    assert "httpx2" in locked  # the positive control: the name check reads real entries


def test_the_api_pyproject_declares_no_mcp() -> None:
    pyproject = _toml(API_DIR / "pyproject.toml")
    declared = list(pyproject["project"]["dependencies"])
    for group in pyproject.get("dependency-groups", {}).values():
        declared += [entry for entry in group if isinstance(entry, str)]
    names = {_requirement_name(requirement) for requirement in declared}
    assert names.isdisjoint(KEPT_OUT), sorted(names & KEPT_OUT)
    assert "anthropic" in names


def test_no_api_module_imports_the_mcp_sdk() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if imported_top_levels(path.read_text("utf-8")) & KEPT_OUT_MODULES
    ]
    assert offenders == []


def test_the_import_scan_sees_each_spelling() -> None:
    """Red proof for the scan above."""
    assert "mcp" in imported_top_levels("import mcp.types as types")
    assert "mcp" in imported_top_levels("from mcp.server import Server")
    assert "httpx" in imported_top_levels("import httpx")
    assert "mcp_types" in imported_top_levels("from mcp_types import Tool")
    assert imported_top_levels("from . import mcp") == set()  # relative: our own module
