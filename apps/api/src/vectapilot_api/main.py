"""App factory.

Run locally:  uv run vectapilot-api
Or directly:  uv run uvicorn --factory vectapilot_api.main:create_app
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from vectapilot_api import __version__
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.health import router as health_router
from vectapilot_api.log import configure_logging

logger = logging.getLogger("vectapilot_api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. A factory (not a module-level `app`) so tests can pass their own settings."""
    settings = settings or Settings()  # values come from the environment
    configure_logging(settings.log_level)
    # Fail fast (ADR 0006): an invalid catalog, or an alias whose model has no price, stops the
    # app here, before it serves anything. The only LLM wiring in M1: no route, no gateway yet.
    catalog = load_catalog()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Clients connect lazily, so the API starts even when a dependency is down:
        # /healthz stays green and /readyz reports the problem.
        # The secret DSNs are unwrapped only here, straight into the clients — never into a log.
        engine = create_async_engine(
            str(settings.database_url.get_secret_value()), pool_pre_ping=True
        )
        database = Database(engine)
        # ADR 0009: a role that could skip or switch off RLS (superuser, BYPASSRLS, owner of a
        # relation, or a member of such a role) stops startup here (UnsafeDatabaseRole).
        try:
            await database.verify_role_at_startup(settings.readiness_timeout_seconds)
        except BaseException:
            await engine.dispose()
            raise
        app.state.db_engine = engine
        app.state.database = database
        app.state.redis = Redis.from_url(str(settings.redis_url.get_secret_value()))
        logger.info("startup", extra={"environment": settings.environment, "version": __version__})
        yield
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()
        logger.info("shutdown")

    app = FastAPI(title="VectaPilot API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.catalog = catalog
    app.include_router(health_router)
    return app


def run() -> None:
    """Local development server (`uv run vectapilot-api`). Containers call uvicorn directly."""
    import uvicorn

    uvicorn.run("vectapilot_api.main:create_app", factory=True, host="127.0.0.1", port=8000)
