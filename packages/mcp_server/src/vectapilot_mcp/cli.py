"""`vectapilot-mcp --business-id <id> --customer-ref <ref>`: the stdio MCP server.

An MCP host starts it as a child process and talks JSON-RPC over its stdin and stdout, so
**stdout belongs to the protocol**: one stray line there corrupts the stream. Logging therefore
goes to stderr, as one JSON object per line (the API's formatter, not its `configure_logging`,
which writes to stdout for uvicorn). The SDK's `stdio_server` also points file descriptor 1 at
stderr while it serves, a second fence for any `print` in a dependency.

Settings come from `VECTAPILOT_*` environment variables, as the API's do: the database as
`vectapilot_app` (`VECTAPILOT_DATABASE_URL`), the tool configuration folder
(`VECTAPILOT_TOOLS_CONFIG_DIR`, with `VECTAPILOT_CORPUS_DIR`, which Settings requires with it) and
`VECTAPILOT_ENVIRONMENT` (local, test or ci, set explicitly). Any startup refusal is one line on
stderr naming the setting, flag or rule (never a value) and exit status 2; the server never starts
half-checked. Exit status 2 means only "never started": an error after startup (while serving or
shutting down) is not mapped to a refusal, so it exits non-zero with its own traceback on stderr
and points whoever reads the host's log at the real cause, not the database.

The SDK's own loggers (`mcp.*`) are pinned at INFO or above whatever `VECTAPILOT_LOG_LEVEL` says:
at DEBUG the SDK logs a malformed stdin line, and so the client's raw text, with `%r` (rule 5:
no message or argument text in logs). The M1 gateway logger is pinned the same way.

Example (local stack running, from packages/mcp_server):

    VECTAPILOT_TOOLS_CONFIG_DIR=../../data/sandbox VECTAPILOT_CORPUS_DIR=../../data/sample \\
      uv run vectapilot-mcp --business-id paws-and-co --customer-ref cust-001
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from typing import Final

from mcp.server.stdio import stdio_server
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.config import Settings
from vectapilot_api.db import UnsafeDatabaseRole
from vectapilot_api.log import JsonFormatter
from vectapilot_api.tools.config import ToolsConfigError
from vectapilot_mcp.server import build_server
from vectapilot_mcp.startup import StartupRefused, prepare

PROGRAM: Final = "vectapilot-mcp"
REFUSED_EXIT: Final = 2
SDK_LOGGER: Final = "mcp"


def configure_logging(level: str) -> None:
    """Every log line to stderr as JSON; never stdout (the protocol's channel). The SDK's
    loggers never below INFO (module docstring)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger(SDK_LOGGER).setLevel(max(logging.INFO, root.level))


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "VectaPilot's tools over MCP (stdio) for one business and one customer, sandbox"
            " only: reads run, writes are only proposed, nothing can be confirmed."
        ),
    )
    parser.add_argument("--business-id", required=True, help="the business (sandbox only)")
    parser.add_argument("--customer-ref", required=True, help="the customer (sandbox only)")
    return parser.parse_args(argv)


async def serve(settings: Settings, business_id: str, customer_ref: str) -> None:
    """Check everything (`startup.prepare`), then serve over stdio until the host closes it.

    Only `prepare` maps an unreachable database to `StartupRefused`, naming the error's type only
    (a driver's message can name the host, the role or the database)."""
    try:
        prepared = await prepare(settings, business_id, customer_ref)
    except (OSError, SQLAlchemyError) as error:
        raise StartupRefused(f"the database is unavailable ({type(error).__name__})") from None
    try:
        server = build_server(prepared.executor, prepared.customer_ref)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await prepared.database.engine.dispose()


def refuse(reason: str) -> int:
    print(f"{PROGRAM}: {reason}", file=sys.stderr)
    return REFUSED_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = Settings()
    except ValidationError as error:
        # `hide_input_in_errors` (Settings): the text names fields and rules, never a value.
        return refuse(f"invalid settings: {error}")
    configure_logging(settings.log_level)
    try:
        asyncio.run(serve(settings, args.business_id, args.customer_ref))
    except (StartupRefused, ToolsConfigError, UnsafeDatabaseRole) as error:
        return refuse(str(error))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
