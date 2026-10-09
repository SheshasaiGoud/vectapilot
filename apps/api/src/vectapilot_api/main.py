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
from vectapilot_api.health import router as health_router
from vectapilot_api.log import configure_logging

logger = logging.getLogger("vectapilot_api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. A factory (not a module-level `app`) so tests can pass their own settings."""
    settings = settings or Settings()  # values come from the environment
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Clients connect lazily, so the API starts even when a dependency is down:
        # /healthz stays green and /readyz reports the problem.
        app.state.db_engine = create_async_engine(str(settings.database_url), pool_pre_ping=True)
        app.state.redis = Redis.from_url(str(settings.redis_url))
        logger.info("startup", extra={"environment": settings.environment, "version": __version__})
        yield
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()
        logger.info("shutdown")

    app = FastAPI(title="VectaPilot API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.include_router(health_router)
    return app


def run() -> None:
    """Local development server (`uv run vectapilot-api`). Containers call uvicorn directly."""
    import uvicorn

    uvicorn.run("vectapilot_api.main:create_app", factory=True, host="127.0.0.1", port=8000)
