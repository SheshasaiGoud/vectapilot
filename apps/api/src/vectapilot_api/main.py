"""App factory.

Run locally:  uv run vectapilot-api
Or directly:  uv run uvicorn --factory vectapilot_api.main:create_app
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Final

from fastapi import FastAPI
from fastapi.telemetry import TelemetryConfig
from redis.asyncio import Redis

from vectapilot_api import __version__
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.router import router as actions_router
from vectapilot_api.agent.llm import build_agent_gateway
from vectapilot_api.agent.profiles import AgentProfile, build_profiles, business_types
from vectapilot_api.agent.router import router as agent_router
from vectapilot_api.agent.routing import ClassifierRouter, load_router_model
from vectapilot_api.agent.service import AgentService, AnswerSource, ProposalStore
from vectapilot_api.agent.types import LoopLimits
from vectapilot_api.answer.router import router as answer_router
from vectapilot_api.answer.service import ANSWER_ALIAS, AnswerService, RagConfig
from vectapilot_api.config import GUARD_OFF_ENVIRONMENTS, Settings
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
from vectapilot_api.gateway.gateway import LLMGateway, build_gateway
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.agent import AgentGuard, read_only_tools
from vectapilot_api.guards.budget import (
    BudgetedGateway,
    RefusalAsRouterFailure,
    build_budgeted_agent_gateway,
    build_budgeted_gateway,
)
from vectapilot_api.guards.config import load_guard_dir
from vectapilot_api.guards.executor import screening_profiles
from vectapilot_api.guards.limits import GuardRefused, Limiter, LimitPolicy, RedisLimiter
from vectapilot_api.guards.match import match_terms
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.redact import RequestScope
from vectapilot_api.guards.routes import RouteLimits
from vectapilot_api.guards.screens import GUARD_PROPAGATE
from vectapilot_api.guards.search import ScreeningSearchService
from vectapilot_api.guards.selftest import SelftestStatus, run_selftest
from vectapilot_api.guards.store import ScreeningProposalStore
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.health import router as health_router
from vectapilot_api.intents.fallback import IntentFallback, LLMIntentFallback
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType
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
GUARDS_NOT_CONFIGURED: Final = "guards_not_configured"
"""M7 PR 1's startup warning: guarded routes serve businesses without a guard folder (local, test
and ci only), so they will hand off every message (PR 6-8)."""
GUARD_SELFTEST: Final = "guard_selftest"
"""M7 PR 1's startup warning: the term matcher is unavailable (H1 is a stub), so every guarded
route will hand off (G3, G14)."""

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


MATCHER_SEAM_REFUSED: Final = "create_app's matcher seam is for tests only (local, test and ci)"


def _agent_gateway(
    settings: Settings, provider: Provider | None, limiter: Limiter, *, guarded: bool
) -> AbstractAsyncContextManager[LLMGateway]:
    """The agent's step gateway: a `BudgetedGateway` on the limiter while `guard_routes` is on
    (G9; the router's fallback uses it too), else exactly M5's (`build_agent_gateway`)."""
    if guarded:
        return build_budgeted_agent_gateway(settings, provider=provider, limiter=limiter)
    return build_agent_gateway(settings, provider=provider)


def _agent_answer_gateway(
    settings: Settings, provider: Provider | None, limiter: Limiter, *, guarded: bool
) -> AbstractAsyncContextManager[BudgetedGateway | None]:
    """The gateway of the agent's own `AnswerService` (`large`, G9), only while `guard_routes`
    is on; M3's `AnswerService` keeps its own gateway either way (G11)."""
    if guarded:
        return build_budgeted_gateway(settings, provider=provider, limiter=limiter)
    return nullcontext(None)


def _router_fallback(gateway: LLMGateway, *, guarded: bool) -> IntentFallback:
    """M4's LLM fallback on the agent's gateway. While `guard_routes` is on, that gateway is
    budgeted, and a refusal is raised as a failure of the router, so it fails closed at its top-1
    priority instead of the route lowering it (`guards.budget.RefusalAsRouterFailure`, G2)."""
    fallback = LLMIntentFallback(gateway)
    return RefusalAsRouterFailure(fallback) if guarded else fallback


def create_app(
    settings: Settings | None = None,
    *,
    llm_provider: Provider | None = None,
    agent_provider: Provider | None = None,
    extraction_provider: FakeProvider | None = None,
    limiter: Limiter | None = None,
    matcher: Matcher | None = None,
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

    `limiter` replaces the Redis limiter and budget (M7 PR 5, G9), as `llm_provider` replaces the
    provider: unit tests pass `guards.fake_limiter.FakeLimiter`, since they run with a dead Redis.
    Without it, `RedisLimiter` is built on the app's lazy Redis client in the lifespan (no
    connection at startup). `/v1/agent` reads it while `guard_routes` is on (M7 PR 6).

    `matcher` replaces H1's `match_terms` in the routes' screens (M7 PR 6, G13): a test-only
    stand-in, refused outside local, test and ci, so guarded behaviour is tested before H1 is
    written. The startup self-test (G14) always runs H1 itself, never the stand-in.

    **`/v1/agent` with `guard_routes = "on"`** (M7 PR 6, G13): the route calls `AgentGuard` in G1's
    order; the agent's gateway is a `BudgetedGateway`, and M4's router fallback on it raises a
    refusal as a failure of the router, so it fails closed at its top-1 priority
    (`_router_fallback`, G2); its `answers` source is a second
    `AnswerService` over the chunk-screening search and its own `BudgetedGateway` (reachable only
    under `agent_router = on`; M3's instance is untouched); its store screens a ticket's text
    and stores a withheld one already expired; each profile's executor screens its tool results'
    free text (`guards.executor`); and a read-only `AgentService`, equal in every constructor
    argument but its profiles, serves messages with an injection signal. The confirm route and
    `/v1/extractions` check their limits first (`guards.routes.RouteLimits`, G15). With `"off"`
    every one of these is exactly as before M7.

    Every paid provider in Settings (`llm_provider`, `embeddings_provider`) needs
    `api_allow_paid_llm` (ADR 0006 Exposure, 2026-10-22 and D9 2026-10-23), injected provider or
    not: the rule reads Settings only.
    """
    settings = settings or Settings()  # values come from the environment
    if matcher is not None and settings.environment not in GUARD_OFF_ENVIRONMENTS:
        raise ValueError(MATCHER_SEAM_REFUSED)
    guarded = settings.guard_routes == "on"
    screen_matcher: Matcher = match_terms if matcher is None else matcher
    # E11 (M6 PR 3): the extraction route's model is the fake provider, never one Settings name.
    if extraction_provider is not None and not isinstance(extraction_provider, FakeProvider):
        raise TypeError("the extraction route's provider is a FakeProvider in M6 (E11)")
    extraction_fake = FakeProvider() if extraction_provider is None else extraction_provider
    # M7 PR 3 (G10): the app's lines are redacted; the M1 CLI and the MCP server keep plain JSON.
    configure_logging(settings.log_level, redact=True)
    # Fail fast (ADR 0006, amended 2026-10-22 and 2026-10-23): M7's budgets cover only the guarded
    # routes' model calls (G9), so embeddings, and every answer and search while `guard_answer` is
    # off, stay uncapped paid calls: a paid provider without the opt-in stops the server here.
    settings.require_api_paid_provider_opt_in()
    # Fail fast (M7 PR 1, G4): outside local, test and ci, a guarded route that serves businesses
    # needs the guard folder. In local, test and ci the app starts, and those routes will hand off
    # every message (`guards_not_configured`, wired by PR 6-8).
    settings.require_guard_config()
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
    # Fail fast (M7 PR 1, G4, G14): with a guard folder, every file must be valid, every business
    # resolved against its corpus manifest (and the type its guarded route reads), and every term
    # must pass the self-test, or the app stops here (GuardConfigError). While H1 is a stub the
    # self-test reports the matcher unavailable and the app starts (the routes hand off, G3).
    # The businesses a guarded route serves, with the type that route reads (tools.json, the
    # extraction business file): a served health business needs a guard file. Both already match
    # the corpus manifest (`business_types`, `load_extraction_dir` refuse a mismatch above); the
    # guard loader still takes the union, as defence in depth, and so do the confirm and
    # extraction routes' limits (M7 PR 6: a health business's 429 carries its guidance).
    served: dict[str, BusinessType] = {
        business_id: loaded.config.business_type for business_id, loaded in (tools or {}).items()
    }
    for business_id, extracted in (extraction.businesses if extraction else {}).items():
        if extracted.file.business_type in HEALTH_TYPES or business_id not in served:
            served[business_id] = extracted.file.business_type
    tool_guidance = {
        business_id: loaded.config.emergency_guidance
        for business_id, loaded in (tools or {}).items()
    }
    guards = None
    guard_selftest = None
    if settings.guard_config_dir is not None:
        if settings.corpus_dir is None:  # pragma: no cover (Settings refuses it)
            raise ValueError("corpus_dir is required with guard_config_dir")
        guards = load_guard_dir(settings.guard_config_dir, settings.corpus_dir, served=served)
        guard_selftest = run_selftest(guards)
        if guard_selftest.status is SelftestStatus.MATCHER_UNAVAILABLE:
            logger.warning(GUARD_SELFTEST, extra={"status": guard_selftest.status.value})
    # Production refused this above; local, test and ci say why every guarded message will be
    # handed off [proposal]. Fixed codes only, never a term.
    if settings.guards_not_configured():
        logger.warning(GUARDS_NOT_CONFIGURED, extra={"environment": settings.environment})
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
        # M7 PR 5 (G9): limits and budgets on the same lazy client; every call is bounded by
        # `guard_redis_timeout_seconds` and fails closed. Read by the routes from PR 6.
        app.state.limiter = (
            limiter
            if limiter is not None
            else RedisLimiter(
                app.state.redis,
                LimitPolicy.from_settings(settings),
                timeout_s=settings.guard_redis_timeout_seconds,
            )
        )
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
        limiter_in_use: Limiter = app.state.limiter
        async with (
            build_gateway(settings, provider=llm_provider) as gateway,
            _agent_gateway(
                settings, agent_provider, limiter_in_use, guarded=guarded
            ) as agent_gateway,
            _agent_answer_gateway(
                settings, llm_provider, limiter_in_use, guarded=guarded
            ) as agent_answer_gateway,
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
                fallback = _router_fallback(agent_gateway, guarded=guarded)
                router = ClassifierRouter(runner, router_model, fallback)
            profiles = build_profiles(database, tools or {}, corpus_types or {})
            loop_limits = LoopLimits(max_steps=settings.agent_max_steps)

            def agent_service(
                served: Mapping[BusinessId, AgentProfile],
                *,
                store: ProposalStore,
                answers: AnswerSource,
                propagate: tuple[type[Exception], ...] = (),
            ) -> AgentService:
                # One construction for both guarded services: every argument but `profiles` is
                # the same object (G13).
                return AgentService(
                    served,
                    gateway=agent_gateway,
                    store=store,
                    answers=answers,
                    limits=loop_limits,
                    turn_timeout_s=settings.agent_turn_timeout_seconds,
                    routing=settings.agent_router,
                    router=router,
                    router_timeout_s=settings.agent_router_timeout_seconds,
                    router_artifact=None if router_model is None else router_model.artifact_sha256,
                    propagate=propagate,
                )

            app.state.agent_guard = None
            app.state.read_only_agent_service = None
            app.state.confirm_limits = None
            app.state.extraction_limits = None
            if not guarded:
                app.state.agent_service = agent_service(
                    profiles, store=app.state.pending_actions, answers=app.state.answer_service
                )
            else:
                # M7 PR 6 (G1, G7, G9, G13): the seams around the same services (docstring).
                if not isinstance(agent_gateway, BudgetedGateway) or agent_answer_gateway is None:
                    raise RuntimeError("guarded routes need budgeted gateways")  # pragma: no cover
                screening = ScreeningSearchService(
                    database,
                    runner,
                    matcher=screen_matcher,
                    retrieval=RetrievalConfig.from_settings(settings),
                )
                pending: object = app.state.pending_actions
                if not isinstance(pending, PendingActionStore):  # pragma: no cover
                    raise RuntimeError("the guarded store wraps the pending-action store")
                guarded_store = ScreeningProposalStore(pending, matcher=screen_matcher)
                guarded_answers = AnswerService(
                    screening, agent_answer_gateway, RagConfig.from_settings(settings)
                )
                propagate: tuple[type[Exception], ...] = (GuardRefused, *GUARD_PROPAGATE)
                full_profiles = screening_profiles(profiles, database, matcher=screen_matcher)
                app.state.agent_service = agent_service(
                    full_profiles, store=guarded_store, answers=guarded_answers, propagate=propagate
                )
                read_only_profiles = screening_profiles(
                    build_profiles(database, read_only_tools(tools or {}), corpus_types or {}),
                    database,
                    matcher=screen_matcher,
                )
                app.state.read_only_agent_service = agent_service(
                    read_only_profiles,
                    store=guarded_store,
                    answers=guarded_answers,
                    propagate=propagate,
                )
                app.state.agent_guard = AgentGuard(
                    config=guards,
                    limiter=limiter_in_use,
                    matcher=screen_matcher,
                    profiles=full_profiles,
                    gateway=agent_gateway,
                    max_steps=settings.agent_max_steps,
                )

                def route_limits(route: RouteSlug, max_retry_after_s: int | None) -> RouteLimits:
                    return RouteLimits(
                        route=route,
                        config=guards,
                        limiter=limiter_in_use,
                        types=served,
                        guidance=tool_guidance,
                        max_retry_after_s=max_retry_after_s,
                    )

                # G15: a 429's Retry-After stays below the action's lifetime.
                lifetime_cap = max(1, settings.action_lifetime_seconds - 1)
                app.state.confirm_limits = route_limits(RouteSlug.CONFIRM, lifetime_cap)
                app.state.extraction_limits = route_limits(RouteSlug.EXTRACTION, None)
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
    app.state.guards = guards
    app.state.guard_selftest = guard_selftest
    app.include_router(health_router)
    app.include_router(kb_router)
    app.include_router(search_router)
    app.include_router(answer_router)
    app.include_router(actions_router)
    app.include_router(agent_router)
    app.include_router(extraction_router)
    install_error_handlers(app)
    # G10-S2: the access line names the matched route's template, read from the request's scope
    # (`guards.redact.AccessLine`). For the log only: it screens and changes nothing.
    app.add_middleware(RequestScope)
    return app


def run() -> None:
    """Local development server (`uv run vectapilot-api`). Containers call uvicorn directly."""
    import uvicorn

    uvicorn.run("vectapilot_api.main:create_app", factory=True, host="127.0.0.1", port=8000)
