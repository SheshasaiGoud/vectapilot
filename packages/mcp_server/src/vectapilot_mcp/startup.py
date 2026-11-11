"""Startup: everything is checked before the server answers its first message (fail loudly).

In order, each refusal before any later step runs:

1. **The environment** (`Settings.environment`) must be **set explicitly** and be in
   `HEADER_ENVIRONMENTS` (local, test, ci): the flags assert a business and a customer, as
   `X-Business-Id` and `customer_ref` do in the API, and nothing authenticates them until M8
   (ADR 0016 decision 9). Settings defaults the environment to `local`; the API shows a missing
   value at once (production needs real providers), but a stdio server started from a
   hand-written host config gives no such signal, so here the default is refused (fail closed).
   Checked before the flags are even parsed for meaning, and before any file or database is read.
2. **The flags**: `--business-id` must be a business id and `--customer-ref` a customer reference.
3. **The tool configuration**: `VECTAPILOT_TOOLS_CONFIG_DIR` is required and read whole, as the
   API reads it (`load_tools_dir`), and the business must have a folder in it.
4. **The database**: the API's engine (`create_api_engine`: its pool reset included) wrapped in
   `Database`, whose `verify_role` must pass (the role is `vectapilot_app`, never the owner, no
   default business); then the business must be registered. Strict, unlike the API's deferred
   check: a stdio server has no `/readyz` to report a database that is down, so it does not start.

Every refusal is a `StartupRefused` with fixed text naming the setting or flag and the rule,
never a value (a DSN can hold a password; a flag is caller text).
"""

import asyncio
from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError

from vectapilot_api.business import HEADER_ENVIRONMENTS
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, InvalidBusinessId, create_api_engine, parse_business_id
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.executor import ToolExecutor
from vectapilot_api.tools.files import load_tools_dir
from vectapilot_api.tools.types import ToolContext

STARTUP_TIMEOUT_S: Final = 10.0
"""A bound on the role check and the registry lookup [proposal; not a measurement]: a database
that does not answer stops startup instead of holding the MCP host."""

REFUSED_ENVIRONMENT: Final = (
    "the MCP server runs only in local, test and ci, set explicitly (VECTAPILOT_ENVIRONMENT):"
    " --business-id and --customer-ref are not authenticated until M8"
)
BAD_BUSINESS: Final = "--business-id must be a business id (a lowercase slug)"
BAD_CUSTOMER: Final = "--customer-ref must be a customer reference (a lowercase slug, 3-40 chars)"
NO_TOOLS_DIR: Final = "VECTAPILOT_TOOLS_CONFIG_DIR is required: it names each business's tools"
NO_BUSINESS_TOOLS: Final = "--business-id has no folder in VECTAPILOT_TOOLS_CONFIG_DIR"
NOT_REGISTERED: Final = "--business-id is not a registered business in the database"
DATABASE_DOWN: Final = "the database did not answer the startup checks in time"


class StartupRefused(RuntimeError):
    """The server must not start. The message is fixed text, never a value."""


@dataclass(frozen=True)
class ServerFlags:
    """The two flags, validated: the context every call gets (W7)."""

    business_id: BusinessId
    customer_ref: str


def require_sandbox_environment(settings: Settings) -> None:
    """Step 1. pydantic-settings passes every value it reads from the environment as an init
    value, so an explicitly set environment is in `model_fields_set` and the default is not."""
    explicit = "environment" in settings.model_fields_set
    if not explicit or settings.environment not in HEADER_ENVIRONMENTS:
        raise StartupRefused(REFUSED_ENVIRONMENT)


def validate_flags(business_id: str, customer_ref: str) -> ServerFlags:
    """The flags as a business id and a customer reference, or `StartupRefused`."""
    try:
        business = parse_business_id(business_id)
    except InvalidBusinessId:
        raise StartupRefused(BAD_BUSINESS) from None
    try:
        ToolContext(business_id=business, customer_ref=customer_ref)
    except ValidationError:
        raise StartupRefused(BAD_CUSTOMER) from None
    return ServerFlags(business_id=business, customer_ref=customer_ref)


def business_config(settings: Settings, business_id: BusinessId) -> ToolsConfig:
    """The business's `tools.json`, after the whole folder validated (`ToolsConfigError`
    propagates: it names the folder, file and rule, never a value)."""
    if settings.tools_config_dir is None:
        raise StartupRefused(NO_TOOLS_DIR)
    tools = load_tools_dir(settings.tools_config_dir)
    if business_id not in tools:
        raise StartupRefused(NO_BUSINESS_TOOLS)
    return tools[business_id].config


def open_database(settings: Settings) -> Database:
    """The API's engine and role check (ADR 0009), connecting as `VECTAPILOT_DATABASE_URL`."""
    return Database(create_api_engine(str(settings.database_url.get_secret_value())))


async def check_database(database: Database, business_id: BusinessId) -> None:
    """`verify_role` (`UnsafeDatabaseRole` propagates), then the business must be registered."""
    try:
        async with asyncio.timeout(STARTUP_TIMEOUT_S):
            await database.verify_role()
            registered = await database.business_exists(business_id)
    except TimeoutError:
        raise StartupRefused(DATABASE_DOWN) from None
    if not registered:
        raise StartupRefused(NOT_REGISTERED)


@dataclass(frozen=True)
class Prepared:
    """What `serve` needs: the business's executor, the customer and the database to dispose."""

    executor: ToolExecutor
    customer_ref: str
    database: Database


async def prepare(settings: Settings, business_id: str, customer_ref: str) -> Prepared:
    """Steps 1-4 of the module docstring; the caller disposes `database.engine` afterwards. On a
    refusal after the engine exists, the engine is disposed here."""
    require_sandbox_environment(settings)
    flags = validate_flags(business_id, customer_ref)
    config = business_config(settings, flags.business_id)
    database = open_database(settings)
    try:
        await check_database(database, flags.business_id)
    except BaseException:
        await database.engine.dispose()
        raise
    return Prepared(ToolExecutor(database, config), flags.customer_ref, database)
