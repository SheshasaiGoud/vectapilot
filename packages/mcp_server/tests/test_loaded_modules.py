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
NEVER_LOADED: Final = (
    "vectapilot_api.actions",
    "vectapilot_api.agent",
    "vectapilot_api.guards",
    "vectapilot_api.auth.store",
    "vectapilot_api.auth.tokens",
    "vectapilot_api.auth.dependencies",
    "vectapilot_api.auth.cookies",
    "vectapilot_api.auth.http",
    "vectapilot_api.widget",
    "vectapilot_api.staff",
)
"""Prefixes: the confirm service and tokens (W2), the agent loop (another caller entirely), M7's
guards (PR 6: the server's results are the core's observation, never the guarded route's screened
copy, so `server.py`'s docstring can say so), and M8's session code (ADR 0019 decision 9: the MCP
server gets no session). The app role it connects as may INSERT a staff session (migration 0008),
so only code keeps the MCP from minting one: it must never hold the statements that do.
`vectapilot_api.auth.origins` may load (`config` needs it); `startup.SESSION_CUSTOMER_REF` is
written out there for this reason."""
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
    assert "vectapilot_api.auth.origins" in modules  # and the auth prefixes are listed one by one
    assert EXECUTE not in modules
    offending = [
        name
        for name in modules
        if any(name == prefix or name.startswith(prefix + ".") for prefix in NEVER_LOADED)
    ]
    assert offending == []
