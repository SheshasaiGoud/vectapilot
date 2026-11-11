"""This project's lock against the API's (ADR 0016 decision 8, D1 (a)). Unit.

- **Shared packages, one version.** The server imports the API's code from its path, so every
  package both locks hold must be the same version: the tool core is tested in apps/api with the
  versions in its lock, and this server must run it with the same ones (a different pydantic or
  SQLAlchemy here would run code the API's tests never ran).
- **Only reviewed additions.** The packages this lock adds over the API's are the ones reviewed
  on install day (rule 11; D1 (a)'s "confirmed with uv lock on install day"). A re-lock that
  brings a new one turns this red until it is reviewed and listed.
- **`mcp` exactly pinned**, and classic `httpx` absent here too (2.x uses `httpx2`).

apps/api/tests/test_mcp_isolation.py checks the other direction (no `mcp` in the API).
"""

import tomllib
from pathlib import Path
from typing import Any, Final

from tests.fakes import REPO_ROOT

MCP_DIR: Final = REPO_ROOT / "packages" / "mcp_server"
API_DIR: Final = REPO_ROOT / "apps" / "api"

REVIEWED_ADDITIONS: Final = {
    # name: version (first upload, UTC), as locked on install day 2026-11-11.
    "attrs": "26.1.0",  # 2026-03-19, via jsonschema
    "cffi": "2.1.1",  # 2026-08-03, via cryptography
    "cryptography": "50.0.2",  # 2026-09-30, via pyjwt[crypto]
    "jsonschema": "4.26.0",  # 2026-01-07, via mcp
    "jsonschema-specifications": "2025.9.1",  # 2025-09-08, via jsonschema
    "mcp": "2.3.0",  # 2026-10-02, rule 11 yes (D1 (a))
    "mcp-types": "2.3.0",  # 2026-10-02, via mcp (the same project's wire types)
    "pycparser": "3.12",  # 2026-10-22, via cffi
    "pyjwt": "2.15.1",  # 2026-09-28, via mcp
    "python-multipart": "0.0.33",  # 2026-10-31, via mcp
    "pywin32": "312",  # 2026-06-04, via mcp, Windows only (never installed here)
    "referencing": "0.37.0",  # 2025-10-13, via jsonschema
    "rpds-py": "2026.9.1",  # 2026-10-04, via referencing
    "sse-starlette": "3.6.0",  # 2026-10-25, via mcp; held back (3.7.0 was 4 days old)
    "vectapilot-mcp": "0.1.0",  # this project
}


RELOCK: Final = (
    "re-lock packages/mcp_server (make mcp-lock) in the same PR after changing apps/api's"
    " dependencies; shared packages must match apps/api/uv.lock, and a new package needs review"
    " (rule 11: publisher, purpose, at least 7 days old) before it joins REVIEWED_ADDITIONS"
)


def _toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _versions(lock: Path) -> dict[str, str]:
    return {package["name"]: package["version"] for package in _toml(lock)["package"]}


def test_every_shared_package_has_the_api_locks_version() -> None:
    ours, api = _versions(MCP_DIR / "uv.lock"), _versions(API_DIR / "uv.lock")
    shared = set(ours) & set(api)
    assert {"pydantic", "sqlalchemy", "anyio", "starlette", "httpx2"} <= shared
    different = {name: (ours[name], api[name]) for name in shared if ours[name] != api[name]}
    assert different == {}, RELOCK


def test_the_lock_adds_only_the_reviewed_packages() -> None:
    ours, api = _versions(MCP_DIR / "uv.lock"), _versions(API_DIR / "uv.lock")
    added = {name: version for name, version in ours.items() if name not in api}
    assert added == REVIEWED_ADDITIONS, RELOCK


def test_mcp_is_pinned_exactly_and_classic_httpx_stays_out() -> None:
    dependencies = _toml(MCP_DIR / "pyproject.toml")["project"]["dependencies"]
    assert "mcp==2.3.0" in dependencies
    assert _versions(MCP_DIR / "uv.lock")["mcp"] == "2.3.0"
    assert {"httpx", "httpcore"}.isdisjoint(_versions(MCP_DIR / "uv.lock"))


def test_the_api_package_comes_from_its_path() -> None:
    sources = _toml(MCP_DIR / "pyproject.toml")["tool"]["uv"]["sources"]
    assert sources == {"vectapilot-api": {"path": "../../apps/api", "editable": True}}
