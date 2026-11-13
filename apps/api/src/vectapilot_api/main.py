"""App factory.

Run locally:  uv run vectapilot-api
Or directly:  uv run uvicorn --factory vectapilot_api.main:create_app
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.telemetry import TelemetryConfig
from redis.asyncio import Redis

from vectapilot_api import __version__
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.router import router as actions_router
from vectapilot_api.agent.llm import build_agent_gateway
from vectapilot_api.agent.profiles import build_profiles, business_types
from vectapilot_api.agent.router import router as agent_router
from vectapilot_api.agent.routing import ClassifierRouter, load_router_model
from vectapilot_api.agent.service import AgentService, ProposalStore
from vectapilot_api.agent.types import LoopLimits
from vectapilot_api.answer.router import router as answer_router
from vectapilot_api.answer.service import ANSWER_ALIAS, AnswerService, RagConfig
from vectapilot_api.config import Settings
from vectapilot_api.db import Database, create_api_engine
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.factory import build_embedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.extraction.extractor import FieldExtractor, build_extraction_gateway
from vectapilot_api.extraction.files import load_extraction_dir
from vectapilot_api.extraction.router import router as extraction_router
from vectapilot_api.extraction.service import ExtractionService, page_reader
from vectapilot_api.extraction.store import PostgresExtractionStore
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.gateway import build_gateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.health import router as health_router
from vectapilot_api.intents.fallback import LLMIntentFallback
from vectapilot_api.kb.errors import install_error_handlers
from vectapilot_api.kb.router import router as kb_router
from vectapilot_api.kb.service import KbService
from vectapilot_api.kb.store import PostgresKbStore
from vectapilot_api.log import configure_logging
from vectapilot_api.retrieval.router import router as search_router
from vectapilot_api.retrieval.search import RetrievalConfig, SearchService
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.executor import TransactionSource
from vectapilot_api.tools.files import load_tools_dir

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


def build_action_services(
    database: TransactionSource, configs: Mapping[str, ToolsConfig], *, lifetime_s: int
) -> tuple[ProposalStore, ConfirmService]:
    """The pending-action store and the confirm service for the businesses in `configs` (M5 PR 3).

    Built here because this module is the only one that imports the store (`actions.pending`, the
    only code that creates a confirm token: W2's fence, tests/actions/test_write_boundary.py). The
    app's lifespan and the tool-call eval harness (`tooleval`, M5 PR 6) both take them from here,
    so the harness stores and confirms exactly as the API does."""
    return (
        PendingActionStore(database, configs, lifetime_s=lifetime_s),
        ConfirmService(database, configs),
    )


def create_app(
    settings: Settings | None = None,
    *,
    llm_provider: Provider | None = None,
    agent_provider: Provider | None = None,
    extraction_provider: FakeProvider | None = None,
) -> FastAPI:
    """Build the app. A factory (not a module-level `app`) so tests can pass their own settings.

    `llm_provider`, if given, replaces the provider Settings name (`build_gateway`'s `provider`):
    tests pass a scripted fake. Without it, `llm_provider = "fake"` gives the fake with an empty
    script, so an answer that reaches the model step fails (500) until a test scripts it.
    `agent_provider` does the same for the agent's own gateway (M5 PR 4, `agent/llm.py`); an
    unscripted agent step is a hand-off (`agent_error`), never a 500.

    `extraction_provider` scripts `POST /v1/extractions`' model (M6 PR 3). It is always a
    `FakeProvider`, whatever Settings say (E11, decided D3 (a)): in M6 the route never reaches a
    paid model, and anything else is a TypeError. Without it the fake has an empty script, so the
    model step fails and the extraction is stored all-review (fail closed, E3).

    Every paid provider in Settings (`llm_provider`, `embeddings_provider`) needs
    `api_allow_paid_llm` (ADR 0006 Exposure, 2026-10-22 and D9 2026-10-23), injected provider or
    not: the rule reads Settings only.
    """
    settings = settings or Settings()  # values come from the environment
    # E11 (M6 PR 3): the extraction route's model is the fake provider, never one Settings name.
    if extraction_provider is not None and not isinstance(extraction_provider, FakeProvider):
        raise TypeError("the extraction route's provider is a FakeProvider in M6 (E11)")
    extraction_fake = FakeProvider() if extraction_provider is None else extraction_provider
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
    # Fail fast (ADR 0016 decision 1, M5 PR 1): with a tool configuration folder, every business's
    # tools.json and seed.json must be there and valid, or the app stops here (ToolsConfigError).
    tools = None if settings.tools_config_dir is None else load_tools_dir(settings.tools_config_dir)
    # Fail fast (W11, M5 PR 4): each business's tools.json and corpus manifest must name the same
    # business type, so a typo in either can never switch the safety step off (AgentConfigError).
    # Settings requires corpus_dir whenever tools_config_dir is set.
    corpus_types = None
    if tools is not None:
        if settings.corpus_dir is None:  # pragma: no cover (Settings refuses it)
            raise ValueError("corpus_dir is required with tools_config_dir")
        corpus_types = business_types(tools, settings.corpus_dir)
    # Fail fast (ADR 0017 decision 2, M6 PR 1): with an extraction folder, every template must
    # compile and every business file must match its corpus manifest's type and only tighten its
    # template (E5), or the app stops here (ExtractionConfigError). Settings requires corpus_dir.
    extraction = None
    if settings.extraction_config_dir is not None:
        if settings.corpus_dir is None:  # pragma: no cover (Settings refuses it)
            raise ValueError("corpus_dir is required with extraction_config_dir")
        extraction = load_extraction_dir(settings.extraction_config_dir, settings.corpus_dir)
    # Fail fast (D7 (a)): `agent_router = on` needs a live M4 model for the API's embedding model
    # (RouterModelError); Settings requires the folder's setting when it is on.
    router_model = None
    if settings.agent_router == "on":
        if settings.agent_router_model_dir is None:  # pragma: no cover (Settings refuses it)
            raise ValueError("agent_router_model_dir is required when agent_router is 'on'")
        router_model = load_router_model(settings.agent_router_model_dir, embedding_spec)

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
        # through the gateway, which closes its provider when this block ends. M3 PR 4: both
        # follow `rag_retrieval_mode` (vector by default; hybrid fuses both legs in one snapshot).
        search = SearchService(database, runner, retrieval=RetrievalConfig.from_settings(settings))
        app.state.search_service = search
        # M5 PR 3: pending actions and their confirm route (ADR 0016 decisions 2-3). Without a
        # tool configuration no business has tools, so nothing can be stored or confirmed.
        tool_configs = {business: t.config for business, t in (tools or {}).items()}
        app.state.pending_actions, app.state.confirm_service = build_action_services(
            database, tool_configs, lifetime_s=settings.action_lifetime_seconds
        )
        async with (
            build_gateway(settings, provider=llm_provider) as gateway,
            build_agent_gateway(settings, provider=agent_provider) as agent_gateway,
            build_extraction_gateway(settings, provider=extraction_fake) as extraction_gateway,
            page_reader() as extraction_reader,
        ):
            app.state.answer_service = AnswerService(
                search, gateway, RagConfig.from_settings(settings)
            )
            # M5 PR 4: `/v1/agent` (ADR 0016 decisions 4, 5, 7), on its own gateway instance. The
            # router (when on) embeds with the API's runner and falls back on the agent's gateway.
            router = None
            if router_model is not None:
                router = ClassifierRouter(runner, router_model, LLMIntentFallback(agent_gateway))
            app.state.agent_service = AgentService(
                build_profiles(database, tools or {}, corpus_types or {}),
                gateway=agent_gateway,
                store=app.state.pending_actions,
                answers=app.state.answer_service,
                limits=LoopLimits(max_steps=settings.agent_max_steps),
                turn_timeout_s=settings.agent_turn_timeout_seconds,
                routing=settings.agent_router,
                router=router,
                router_timeout_s=settings.agent_router_timeout_seconds,
                router_artifact=None if router_model is None else router_model.artifact_sha256,
            )
            # M6 PR 3: `POST /v1/extractions` (ADR 0017 decisions 3-6) on the extraction's own
            # gateway (the fake provider, E11), with H1's `route_fields` (fail closed until it is
            # written) and the store under RLS. PDFs are read in the app's own reading thread, so
            # a slow one never blocks the event loop.
            app.state.extraction_service = ExtractionService(
                FieldExtractor(extraction_gateway), reader=extraction_reader
            )
            app.state.extraction_store = PostgresExtractionStore(database)
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
    app.state.tools = tools
    app.state.extraction = extraction
    app.include_router(health_router)
    app.include_router(kb_router)
    app.include_router(search_router)
    app.include_router(answer_router)
    app.include_router(actions_router)
    app.include_router(agent_router)
    app.include_router(extraction_router)
    install_error_handlers(app)
    return app


def run() -> None:
    """Local development server (`uv run vectapilot-api`). Containers call uvicorn directly."""
    import uvicorn

    uvicorn.run("vectapilot_api.main:create_app", factory=True, host="127.0.0.1", port=8000)
