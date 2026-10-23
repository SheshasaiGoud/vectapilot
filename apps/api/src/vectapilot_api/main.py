"""App factory.

Run locally:  uv run vectapilot-api
Or directly:  uv run uvicorn --factory vectapilot_api.main:create_app
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.telemetry import TelemetryConfig
from redis.asyncio import Redis

from vectapilot_api import __version__
from vectapilot_api.answer.router import router as answer_router
from vectapilot_api.answer.service import ANSWER_ALIAS, AnswerService, RagConfig
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, create_api_engine
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.factory import build_embedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import build_gateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.health import router as health_router
from vectapilot_api.kb.errors import install_error_handlers
from vectapilot_api.kb.router import router as kb_router
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.log import configure_logging
from vectapilot_api.retrieval.router import router as search_router
from vectapilot_api.retrieval.search import SearchService

logger = logging.getLogger("vectapilot_api")

TELEMETRY_OFF: Final[TelemetryConfig] = {
    "tracing": False,
    "metrics": False,
    "logs": False,
    "operation_spans": False,
    "auto_configure": False,
}
"""FastAPI's built-in OpenTelemetry, every part off (M3 PR 1, ADR 0007 amendment).

FastAPI 0.143.0 turns tracing, metrics and logs on by default (`applications.py:1033-1045`) and
uses the process-wide providers. Once any provider exists (an SDK, `opentelemetry-instrument`), it
would record `url.query` (our `?q=` question), keep the body of a failed validation and log stack
traces with their messages, and read a client's `traceparent` as the parent span
(`telemetry/_asgi.py`), while ADR 0011 decision 9 keeps the trace id server-made. With the three
signals off, `FastAPI.__call__` never enters its telemetry layer (`NativeTelemetry.enabled()`).
`operation_spans` and `auto_configure` add nothing while every signal is off; they are pinned so
that turning one signal back on later is a deliberate change. The span comes with M9 (D12).
`tests/test_fastapi_telemetry.py` proves it, and proves the defaults would record."""


def create_app(
    settings: Settings | None = None, *, llm_provider: Provider | None = None
) -> FastAPI:
    """Build the app. A factory (not a module-level `app`) so tests can pass their own settings.

    `llm_provider`, if given, replaces the provider Settings name (`build_gateway`'s `provider`):
    tests pass a scripted fake. Without it, `llm_provider = "fake"` gives the fake with an empty
    script, so an answer that reaches the model step fails (500) until a test scripts it.

    Every paid provider in Settings (`llm_provider`, `embeddings_provider`) needs
    `api_allow_paid_llm` (ADR 0006 Exposure, 2026-10-22 and D9 2026-10-23), injected provider or
    not: the rule reads Settings only.
    """
    settings = settings or Settings()  # values come from the environment
    configure_logging(settings.log_level)
    # Fail fast (ADR 0006, amended 2026-10-22 and 2026-10-23): until M7 adds budgets, every answer
    # and every search is an uncapped paid call, so a paid provider without the opt-in stops the
    # server here.
    settings.require_api_paid_provider_opt_in()
    # Fail fast (ADR 0006): an invalid catalog, or an alias whose model has no price, stops the
    # app here, before it serves anything. The answer's alias must exist too (M2 PR 6).
    catalog = load_catalog()
    if ANSWER_ALIAS not in catalog.aliases:
        raise ValueError(f"the model catalog has no {ANSWER_ALIAS!r} alias for answers")
    # The same for the embedding model (ADR 0008): a model or dimension the catalog does not allow
    # stops the app here.
    embedding_spec = load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Clients connect lazily, so the API starts even when a dependency is down:
        # /healthz stays green and /readyz reports the problem.
        # The secret DSNs are unwrapped only here, straight into the clients — never into a log.
        # `create_api_engine`: bound values never in an error's text, and the pool-return reset
        # of `app.business_id` (M3 PR 1, ADR 0009 amendment).
        engine = create_api_engine(str(settings.database_url.get_secret_value()))
        database = Database(engine)
        # ADR 0009: a role that could skip or switch off RLS (superuser, BYPASSRLS, owner of a
        # relation, or a member of such a role) stops startup here (UnsafeDatabaseRole).
        try:
            await database.verify_role_at_startup(settings.readiness_timeout_seconds)
            embedder = build_embedder(settings)
        except BaseException:
            await engine.dispose()
            raise
        app.state.db_engine = engine
        app.state.database = database
        app.state.redis = Redis.from_url(str(settings.redis_url.get_secret_value()))
        # Embedding batches retry with the gateway's policy (ADR 0006's bounds: one retry policy
        # for every provider call); each request keeps its own timeout.
        policy = settings.llm_retry_policy()
        runner = EmbeddingRunner(
            embedder,
            embedding_spec,
            policy,
            deadline_s=batch_deadline_s(policy, settings.embeddings_timeout_seconds),
        )
        app.state.embedder = embedder
        app.state.kb_service = KbService(PostgresKbStore(database), runner)
        # M2 PR 6: search (H1's `vector_search` in one business transaction) and cited answers
        # through the gateway, which closes its provider when this block ends.
        search = SearchService(database, runner)
        app.state.search_service = search
        async with build_gateway(settings, provider=llm_provider) as gateway:
            app.state.answer_service = AnswerService(
                search, gateway, RagConfig.from_settings(settings)
            )
            logger.info(
                "startup", extra={"environment": settings.environment, "version": __version__}
            )
            yield
        await app.state.embedder.aclose()
        await app.state.redis.aclose()
        await app.state.db_engine.dispose()
        logger.info("shutdown")

    app = FastAPI(
        title="VectaPilot API", version=__version__, lifespan=lifespan, telemetry=TELEMETRY_OFF
    )
    app.state.settings = settings
    app.state.catalog = catalog
    app.include_router(health_router)
    app.include_router(kb_router)
    app.include_router(search_router)
    app.include_router(answer_router)
    install_error_handlers(app)
    return app


def run() -> None:
    """Local development server (`uv run vectapilot-api`). Containers call uvicorn directly."""
    import uvicorn

    uvicorn.run("vectapilot_api.main:create_app", factory=True, host="127.0.0.1", port=8000)
