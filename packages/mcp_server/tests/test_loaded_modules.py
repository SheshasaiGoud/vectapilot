"""What the server's process loads, not only what it imports directly (W1, W2). Unit.

apps/api's tests/actions/test_write_boundary.py and this project's ruff bans check the imports
written in packages/mcp_server. They would stay green if a module the server uses (the executor,
`db`, `config`) started importing the write path or the confirm-token code: the MCP process would
then hold it. So a fresh interpreter imports the server's three entry modules and lists every
module that ended up loaded.
"""

import subprocess
import sys
from typing import Final

LOAD: Final = (
    "import sys, vectapilot_mcp.cli, vectapilot_mcp.server, vectapilot_mcp.startup;"
    " print('\\n'.join(sorted(sys.modules)))"
)
NEVER_LOADED: Final = ("vectapilot_api.actions", "vectapilot_api.agent")
"""Prefixes: the confirm service and tokens (W2), and the agent loop (another caller entirely)."""
EXECUTE: Final = "vectapilot_api.tools.execute"
"""The only module that executes a write (W1)."""


def loaded_modules() -> list[str]:
    done = subprocess.run(  # noqa: S603 - our own interpreter and fixed code
        [sys.executable, "-I", "-c", LOAD],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return done.stdout.splitlines()


def test_the_server_process_never_loads_the_write_path_or_tokens() -> None:
    modules = loaded_modules()
    assert "vectapilot_api.tools.executor" in modules  # the positive control: the list is real
    assert EXECUTE not in modules
    offending = [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in NEVER_LOADED)
    ]
    assert offending == []
