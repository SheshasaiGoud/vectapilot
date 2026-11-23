"""The answer path's runner (M7 PR 8; `plan.CasePath.ANSWER`): `POST /v1/answer` with
`guard_answer = on`, for B on the answer route, G and H-req written to it, H-out and I on its
answers, and L's look-alikes there.

**One pipeline, two environments.** `build_answer_pipeline` wires one case's guarded answer exactly
as `create_app` does with `guard_answer = "on"` (G13): an `AnswerGuard` over the guard folder, the
types and guidance the guarded routes serve each business with, and a separate `AnswerService`
over `ScreeningSearchService` on its own `BudgetedGateway` (`redteam.chunks`). What differs per
case is only what `create_app` cannot know (a test pins the rest equal):
- **the model** is a `ReplayProvider` scripted with the case's `answer` steps, each citing the
  chunks `fixture.context.cited` names (`chunks.answer_replies`);
- **the vector search** is wrapped to record the chunk ids it returned before the screen
  (`chunks.RecordingSearch`, B's reach control);
- **the seams** (`agent.GuardSeams`): H1, H2 and H3 when registered; tests pass stand-ins;
- **the limiter**: an allowing in-memory `FakeLimiter` (no answer case arms an outage).

`in_process` (every answer case but the surface's end-to-end ones): the guard is called directly,
the search reads the case's `fixture.chunks` through stand-ins with no database
(`chunks.fixture_search`), and the embedder is the fake. `database` (b-001, h-009: one end-to-end
case per surface): the case goes through the served app's real route, with the route's two
guard dependencies answered by this case's pipeline (`answering`; the served app keeps
`guard_answer` off, so `/v1/kb/search` stays k-001's plain search), over the app's own database,
embedding runner and guard folder; its chunks are rows the served suite planted for it
(`tests/redteam/served.py`, `answer_chunks`), found by the real search.

**What a result holds** (`scoring.CaseResult`, codes only): the outcome from the response and the
answer route's `guard` record (`answer_outcome`), the record's `decided_by`, `error_type` and
findings, the hand-off reason, the fake's call count, and the controls: B whether the poisoned
chunk was retrieved before the screen, and for `dropped` that it never reached the model
(`chunks.poison_shown`); I and H-out whether H3 ran (`output` among the checks). A false mask of the
question (H2 replaced text the case wrote, as the agent runner reports it) is `masked:<labels>`.
A scripted answer the service did not show while no guard decided is a fixture error
(`chunks.AnswerNotShown`), never an outcome.
"""

import asyncio
import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from vectapilot_api.answer.router import ANSWER_ERRORS
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.answer.types import AnswerRequest
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.extraction.files import ExtractionConfig
from vectapilot_api.gateway.providers.fake import FakeReply
from vectapilot_api.guards.answer import AnswerGuard
from vectapilot_api.guards.config import GuardConfig
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.limits import Limiter
from vectapilot_api.guards.types import Check, RouteSlug
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType
from vectapilot_api.redteam.agent import (
    ACTION_OUTCOMES,
    BUSINESS_HEADER,
    REAL_SEAMS,
    TRACE_ID,
    FixtureSource,
    GuardSeams,
    Observed,
    ReplayProvider,
    World,
    _checks,
    _decided,
    _error_type,
    _findings,
    _reason,
    guard_records,
    handoff_outcome,
    masked_outcome,
    served_parts,
    world_of,
)
from vectapilot_api.redteam.cases import Case, Category
from vectapilot_api.redteam.chunks import (
    RecordingSearch,
    answer_gateway,
    answer_replies,
    answer_service,
    asked_question,
    chunk_ids,
    fixture_models,
    fixture_runner,
    fixture_search,
    poison_retrieved,
    poison_shown,
    require_shown,
)
from vectapilot_api.redteam.scoring import CaseResult
from vectapilot_api.retrieval.router import typed_error
from vectapilot_api.retrieval.search import BusinessTransactions, StoredModels, VectorSearch
from vectapilot_api.retrieval.vector import vector_search
from vectapilot_api.tools.files import BusinessTools

if TYPE_CHECKING:
    # Annotations only: `harness` imports this module to register the runner.
    from vectapilot_api.redteam.harness import FilledCase, RunEnv, Runner

ANSWER_PATH: Final = "/v1/answer"


# ── The world and one case's pipeline ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnswerWorld:
    """What every answer pipeline is built over: the guard folder, and the type and emergency
    guidance each business is served with (`create_app`'s `served` and `tool_guidance`)."""

    guards: GuardConfig | None
    types: Mapping[str, BusinessType]
    guidance: Mapping[str, tuple[str, ...]]
    settings: Settings


def served_types(
    tools: Mapping[str, BusinessTools], extraction: ExtractionConfig | None = None
) -> dict[str, BusinessType]:
    """The type a guarded route serves each business as, as `create_app` computes it: the tool
    file's, and the extraction file's for a health type or a business without tools."""
    served = {business: loaded.config.business_type for business, loaded in tools.items()}
    for business, extracted in (extraction.businesses if extraction else {}).items():
        if extracted.file.business_type in HEALTH_TYPES or business not in served:
            served[business] = extracted.file.business_type
    return served


def tool_guidance(tools: Mapping[str, BusinessTools]) -> dict[str, tuple[str, ...]]:
    """Each business's `tools.json` emergency guidance, as `create_app` reads it."""
    return {business: loaded.config.emergency_guidance for business, loaded in tools.items()}


def answer_world(world: World) -> AnswerWorld:
    """In process: the agent runner's world (`agent.world_of`), which has no extraction folder."""
    return AnswerWorld(
        world.guards, served_types(world.tools), tool_guidance(world.tools), world.settings
    )


def served_answer_world(app: FastAPI) -> AnswerWorld:
    """The database environment: the served app's own folders, read as `create_app` read them."""
    state = app.state
    tools: Mapping[str, BusinessTools] = state.tools or {}
    return AnswerWorld(
        state.guards, served_types(tools, state.extraction), tool_guidance(tools), state.settings
    )


@dataclass(frozen=True)
class AnswerPipeline:
    guard: AnswerGuard
    service: AnswerService
    provider: ReplayProvider
    chunk_ids: Mapping[str, uuid.UUID]
    retrieved: list[uuid.UUID] = field(default_factory=list)
    """The chunk ids the vector search returned, before the screen."""


def build_answer_pipeline(
    case: Case,
    fill: Callable[[str], str],
    world: AnswerWorld,
    *,
    database: BusinessTransactions,
    runner: EmbeddingRunner,
    limiter: Limiter,
    ids: Mapping[str, uuid.UUID],
    search: VectorSearch = vector_search,
    stored_models: StoredModels | None = None,
    seams: GuardSeams = REAL_SEAMS,
) -> AnswerPipeline:
    """One case's guarded answer as `create_app` wires it (module docstring)."""
    settings = world.settings
    provider = ReplayProvider()
    provider.load([FakeReply(text=reply) for reply in answer_replies(case, fill, ids)])
    retrieved: list[uuid.UUID] = []
    service = answer_service(
        settings,
        database=database,
        runner=runner,
        matcher=seams.matcher,
        gateway=answer_gateway(settings, provider, limiter),
        search=RecordingSearch(search, retrieved),
        stored_models=stored_models,
    )
    guard = AnswerGuard(
        config=world.guards,
        limiter=limiter,
        matcher=seams.matcher,
        types=world.types,
        guidance=world.guidance,
        check=seams.check,
        mask=seams.mask,
    )
    return AnswerPipeline(guard, service, provider, ids, retrieved)


# ── Outcomes ─────────────────────────────────────────────────────────────────────────────────


def answer_record(records: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None:
    """The one `guard` record of the answer route among `records`, or None."""
    found = [r for r in records if r.get("route") == RouteSlug.ANSWER.value]
    if len(found) > 1:
        raise ValueError("one question wrote more than one answer guard record")
    return found[0] if found else None


def answer_outcome(
    case: Case, observed: Observed, pipeline: AnswerPipeline, fill: Callable[[str], str], sent: str
) -> str:
    """The case's outcome code (module docstring)."""
    if observed.status == 422:
        return "rejected_422"
    if observed.status == 503:
        return "unavailable_503"
    if observed.status >= 500:
        return "server_error"
    if observed.status == 429:
        return "blocked_429"
    action = None if observed.record is None else observed.record.get("action")
    requests = pipeline.provider.calls
    if isinstance(action, str) and ACTION_OUTCOMES.get(action) == "dropped":
        return "allowed" if poison_shown(case, fill, pipeline.chunk_ids, requests) else "dropped"
    body = observed.body
    if body.get("kind") == "handoff":
        return handoff_outcome(body)
    require_shown(case, body.get("outcome"))
    return masked_outcome(sent, asked_question(requests))


def answer_result(
    case: Case,
    observed: Observed,
    pipeline: AnswerPipeline,
    fill: Callable[[str], str],
    sent: str,
) -> CaseResult:
    record = observed.record
    reached: bool | None = None
    if case.category is Category.B:
        reached = poison_retrieved(case, pipeline.chunk_ids, pipeline.retrieved)
    elif case.subgroup in ("I", "H-out"):
        reached = Check.OUTPUT.value in _checks(record)
    return CaseResult(
        case.id,
        answer_outcome(case, observed, pipeline, fill, sent),
        _decided(record),
        handoff_reason=_reason(observed.body),
        error_type=_error_type(record),
        findings=_findings(record),
        reached=reached,
        model_calls=len(pipeline.provider.calls),
    )


# ── In process ───────────────────────────────────────────────────────────────────────────────


async def ask(pipeline: AnswerPipeline, business_id: str, question: str) -> Observed:
    """The guard called as the route calls it; the service's own failure mapped by M3's table."""
    try:
        request = AnswerRequest(question=question)
    except ValidationError:
        return Observed(422, {}, None)
    with guard_records() as records:
        guarded = await pipeline.guard.answer(
            business_id, request, trace_id=TRACE_ID, service=pipeline.service
        )
    record = answer_record(records)
    if guarded.failure is not None:
        typed = typed_error(guarded.failure, ANSWER_ERRORS, trace_id=TRACE_ID)
        return Observed(500 if typed is None else typed.status_code, {}, record)
    response = guarded.response
    if isinstance(response, JSONResponse):
        body = json.loads(bytes(response.body))
        return Observed(response.status_code, body if isinstance(body, dict) else {}, record)
    if response is None:  # pragma: no cover (a guard always answers or fails)
        raise RuntimeError("the answer guard returned nothing")
    return Observed(200, response.model_dump(mode="json"), record)


async def _in_process(filled: "FilledCase", world: World, seams: GuardSeams) -> CaseResult:
    case = filled.case
    settings = world.settings
    runner = fixture_runner(settings)
    ids = chunk_ids(case)
    pipeline = build_answer_pipeline(
        case,
        filled.text,
        answer_world(world),
        database=FixtureSource(),
        runner=runner,
        limiter=FakeLimiter(),
        ids=ids,
        search=fixture_search(case, filled.text, ids, model=runner.model),
        stored_models=fixture_models(runner.model),
        seams=seams,
    )
    observed = await ask(pipeline, case.business_id, filled.input)
    return answer_result(case, observed, pipeline, filled.text, filled.input)


# ── Through the served app (the database environment) ───────────────────────────────────────


@contextmanager
def answering(app: FastAPI, pipeline: AnswerPipeline) -> Iterator[None]:
    """The route's two guard dependencies answered by `pipeline` for one case; always removed."""
    from vectapilot_api.answer.router import get_answer_guard, get_guarded_answer_service

    overrides: dict[Callable[..., Any], Callable[[], Any]] = {
        get_answer_guard: lambda: pipeline.guard,
        get_guarded_answer_service: lambda: pipeline.service,
    }
    app.dependency_overrides.update(overrides)
    try:
        yield
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)


def post_question(
    client: Any, app: FastAPI, pipeline: AnswerPipeline, business_id: str, question: str
) -> Observed:
    with answering(app, pipeline), guard_records() as records:
        response = client.post(
            ANSWER_PATH, json={"question": question}, headers={BUSINESS_HEADER: business_id}
        )
    body = response.json()
    return Observed(
        response.status_code, body if isinstance(body, dict) else {}, answer_record(records)
    )


def planted_ids(env: "RunEnv", case: Case) -> Mapping[str, uuid.UUID]:
    """The ids of the rows the served suite planted for `case`'s chunks (module docstring)."""
    planted = getattr(env.app, "answer_chunks", None)
    ids = planted.get(case.id) if isinstance(planted, Mapping) else None
    wanted = {chunk.id for chunk in case.fixture.chunks}
    if not isinstance(ids, Mapping) or set(ids) != wanted:
        raise ValueError(f"case {case.id}: the served suite planted no chunks for it")
    return cast(Mapping[str, uuid.UUID], ids)


def _through_the_route(filled: "FilledCase", env: "RunEnv", seams: GuardSeams) -> CaseResult:
    case = filled.case
    app, client, _customer = served_parts(env)
    ids = planted_ids(env, case)
    # The app's own runner: the one `create_app` hands both searches.
    runner: EmbeddingRunner = app.state.search_service._runner
    pipeline = build_answer_pipeline(
        case,
        filled.text,
        served_answer_world(app),
        database=app.state.database,
        runner=runner,
        limiter=FakeLimiter(),
        ids=ids,
        seams=seams,
    )
    observed = post_question(client, app, pipeline, case.business_id, filled.input)
    return answer_result(case, observed, pipeline, filled.text, filled.input)


def answer_runner(seams: GuardSeams = REAL_SEAMS) -> "Runner":
    """The answer path's runner over `seams` (H1, H2 and H3 when registered)."""

    def run(filled: "FilledCase", env: "RunEnv") -> CaseResult:
        if env.environment == "database":
            return _through_the_route(filled, env, seams)
        if env.dataset_dir is None:
            raise ValueError("the in-process answer path needs the dataset folder")
        return asyncio.run(_in_process(filled, world_of(env.dataset_dir), seams))

    return run


run_answer: Final = answer_runner()
"""The registered runner (`harness.RUNNERS`)."""
