"""The chunk runner's shared parts (M7 PR 8): a case's retrieved chunks, the answer model's scripted
replies, and B's prompt check. Two runners read them: the answer path's (`redteam.answer`,
`POST /v1/answer` with `guard_answer = on`) and the agent's for B on the agent route (b-007,
b-008: `agent_router = on`, a stub router, the agent's own `AnswerService` over the chunk screen,
kenji's PR 6 review).

**The guarded answer step, as `create_app` builds it** (`answer_service`): an `AnswerService` over
`ScreeningSearchService` (the route's matcher) on its own `BudgetedGateway` (`answer_gateway`,
`guards.budget.build_budgeted_gateway` argument for argument, a test pins it), with
`RagConfig` and `RetrievalConfig` from Settings. What differs is only what `create_app` cannot
know: the vector search is wrapped by `RecordingSearch`, which records the chunk ids it returned
before any screen (B's reach control), and in process the search is a fixture stand-in.

**In process** (`fixture_search`): the case's `fixture.chunks` are the hits, whatever the
question's vector, each with a fixed id derived from the case and the chunk's slug (`chunk_ids`)
and the score `FIXTURE_SCORE` (above any `rag_min_score` Settings allow but 1.0, so the evidence
gate keeps them); a customer reads only `public` chunks, as `retrieval.types.VISIBLE_AUDIENCES`
says. In the database environment the chunks are rows the served suite planted
(`tests/redteam/served.py`), and the search is the real one.

**The scripted answer** (`answer_replies`): one `ModelAnswer` JSON per `answer` step, `answered`,
its text filled, citing exactly the chunks `fixture.context.cited` names, by their ids.

**B's outcome needs the prompt** (`scoring.REACH_CONTROLLED`): `dropped` means the poisoned chunk
(`poisoned`: a B case's chunks its answer does not cite) is absent from every request the answer
model received: no start marker carries its id, and its cleaned text (`guards.normalize.clean`) is
not there (`shown_to_model`), so a chunk whose cleaning changes its text (b-006's tag characters)
is still found. Codes only leave this module: never a chunk's or a prompt's text.
"""

import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.answer.prompt import QUESTION_END, QUESTION_START
from vectapilot_api.answer.service import AnswerService, RagConfig
from vectapilot_api.config import Settings
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.runner import EmbeddingRunner, batch_deadline_s
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.providers.base import Provider, ProviderRequest
from vectapilot_api.guards.budget import BudgetedGateway
from vectapilot_api.guards.limits import Limiter
from vectapilot_api.guards.normalize import clean
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.search import ScreeningSearchService
from vectapilot_api.intents.decide import RouteDecision
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.kb.types import ContentKind
from vectapilot_api.redteam.cases import Case, Category
from vectapilot_api.redteam.replay import answer_texts
from vectapilot_api.retrieval.search import (
    BusinessTransactions,
    RetrievalConfig,
    StoredModels,
    VectorSearch,
)
from vectapilot_api.retrieval.types import VISIBLE_AUDIENCES, SearchHit, VectorQuery

CHUNK_NAMESPACE: Final = uuid.UUID("5b0f6a52-1d3e-4c8f-9a3b-7e2d4c6f8a10")
"""A fixed namespace for the in-process chunk ids (uuid5): the same case and slug always give the
same id, so a run is reproducible and two cases never share one."""
FIXTURE_SCORE: Final = 0.9
"""Every in-process hit's cosine: above `rag_min_score`'s default (0.3), so the evidence gate keeps
the chunks the screen keeps."""
FIXTURE_TITLE: Final = "TEST-ONLY red-team chunk"
FIXTURE_VERSION: Final = "1"
BOUNDARY_LINE: Final = re.compile(r"^Boundary: ([0-9a-f]{16})$", re.MULTILINE)
"""The answer prompt's boundary line (`answer/prompt.py`'s `SYSTEM_PROMPT`)."""


class AnswerNotShown(RuntimeError):
    """A case scripts an answer, the guards let the response through, and the service still did
    not show it (an abstention): the fixture or the runner does not fit, never an outcome. The
    message names the case and the abstention's code only."""


# ── The case's chunks ────────────────────────────────────────────────────────────────────────


def chunk_ids(case: Case) -> Mapping[str, uuid.UUID]:
    """Each fixture chunk's in-process id, by its slug (module docstring)."""
    return MappingProxyType(
        {
            chunk.id: uuid.uuid5(CHUNK_NAMESPACE, f"{case.id}/{chunk.id}")
            for chunk in case.fixture.chunks
        }
    )


def cited_slugs(case: Case) -> tuple[str, ...]:
    return () if case.fixture.context is None else case.fixture.context.cited


def poisoned(case: Case) -> tuple[str, ...]:
    """A B case's poisoned chunks: every chunk its scripted answer does not cite. Other
    categories have none (their chunks are the evidence)."""
    if case.category is not Category.B:
        return ()
    cited = set(cited_slugs(case))
    return tuple(chunk.id for chunk in case.fixture.chunks if chunk.id not in cited)


def answer_replies(
    case: Case, fill: Callable[[str], str], ids: Mapping[str, uuid.UUID]
) -> tuple[str, ...]:
    """The answer model's replies, one `ModelAnswer` JSON per `answer` step (module docstring)."""
    citations = [str(ids[slug]) for slug in cited_slugs(case)]
    return tuple(
        json.dumps(
            {"outcome": "answered", "answer": fill(text), "citations": citations},
            ensure_ascii=False,
        )
        for text in answer_texts(case)
    )


def fixture_hits(
    case: Case, fill: Callable[[str], str], ids: Mapping[str, uuid.UUID], *, model: str
) -> tuple[tuple[SearchHit, str], ...]:
    """The case's chunks as hits, each with its audience, in fixture order."""
    return tuple(
        (
            SearchHit(
                chunk_id=ids[chunk.id],
                document_id=uuid.uuid5(CHUNK_NAMESPACE, f"{case.id}/{chunk.id}/document"),
                source_id=f"redteam-{chunk.id}",
                version=FIXTURE_VERSION,
                title=FIXTURE_TITLE,
                language=case.language,
                content_kind=ContentKind.FAQ,
                page=None,
                chunk_index=0,
                content=fill(chunk.text),
                score=FIXTURE_SCORE,
                embedding_model=model,
            ),
            chunk.audience,
        )
        for chunk in case.fixture.chunks
    )


def fixture_search(
    case: Case, fill: Callable[[str], str], ids: Mapping[str, uuid.UUID], *, model: str
) -> VectorSearch:
    """In process, `vector_search`'s seam: the case's chunks the reader may see, at most `k`
    (module docstring). It never reads the connection."""
    hits = fixture_hits(case, fill, ids, model=model)

    async def search(_connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        visible = VISIBLE_AUDIENCES[query.reader]
        return [hit for hit, audience in hits if audience in visible][: query.k]

    return search


def fixture_models(model: str) -> StoredModels:
    """In process, `stored_models_in`'s seam: the business's chunks all carry `model`."""

    async def stored(_connection: AsyncConnection, _business_id: str) -> frozenset[str]:
        return frozenset({model})

    return stored


def fixture_runner(settings: Settings) -> EmbeddingRunner:
    """In process, the API's embedding runner as `create_app` builds it, over the fake embedder."""
    spec = load_embedding_catalog().resolve(
        settings.embeddings_provider, settings.embeddings_model, settings.embeddings_dimension
    )
    policy = settings.llm_retry_policy()
    return EmbeddingRunner(
        FakeEmbedder(spec),
        spec,
        policy,
        deadline_s=batch_deadline_s(policy, settings.embeddings_timeout_seconds),
    )


class RecordingSearch:
    """`vector_search`'s seam around `inner`, recording every chunk id it returned, before any
    screen (B's reach control). Ids only, never a chunk's text."""

    def __init__(self, inner: VectorSearch, seen: list[uuid.UUID]) -> None:
        self.inner = inner
        self.seen = seen

    async def __call__(self, connection: AsyncConnection, query: VectorQuery) -> list[SearchHit]:
        hits = await self.inner(connection, query)
        self.seen.extend(hit.chunk_id for hit in hits)
        return hits


# ── The guarded answer step (module docstring) ───────────────────────────────────────────────


def answer_gateway(settings: Settings, provider: Provider, limiter: Limiter) -> BudgetedGateway:
    """`guards.budget.build_budgeted_gateway`'s gateway, argument for argument (a test pins them
    equal), built without its context manager: a replay provider holds nothing to close."""
    return BudgetedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=settings.llm_attempt_timeout_seconds,
        deadline_s=settings.llm_deadline_seconds,
        retry_policy=settings.llm_retry_policy(),
        max_repairs=settings.llm_max_repairs,
        limiter=limiter,
    )


def answer_service(
    settings: Settings,
    *,
    database: BusinessTransactions,
    runner: EmbeddingRunner,
    matcher: Matcher,
    gateway: BudgetedGateway,
    search: VectorSearch,
    stored_models: StoredModels | None = None,
) -> AnswerService:
    """The guarded `AnswerService` as `create_app` builds it (module docstring)."""
    return AnswerService(
        ScreeningSearchService(
            database,
            runner,
            matcher=matcher,
            retrieval=RetrievalConfig.from_settings(settings),
            search=search,
            stored_models=stored_models,
        ),
        gateway,
        RagConfig.from_settings(settings),
    )


class FixedRouter:
    """The stub router B on the agent route runs under (`agent_router = on`): every message is a
    `question`, routed by the business's own policy (RAG for every business type). **Not M4's H1**
    (`route_message`): it scores nothing, calls no model and decides nothing but this one intent,
    so the case reaches the agent's own answer step."""

    intent: Final = Intent.QUESTION

    async def route(
        self,
        message: str,
        *,
        policy: RoutePolicy,
        business_id: str,
        trace_id: str,
        progress: object,
    ) -> RouteDecision:
        return RouteDecision(
            intent=self.intent, route=policy.route(self.intent), decided_by="classifier"
        )


# ── What the answer model received ───────────────────────────────────────────────────────────


def prompt_text(requests: Sequence[ProviderRequest]) -> str:
    """Every system prompt and message the answer model received, joined."""
    parts: list[str] = []
    for request in requests:
        parts.append(request.system or "")
        parts.extend(message.content for message in request.messages)
    return "\n".join(parts)


def shown_to_model(chunk_id: uuid.UUID, text: str, requests: Sequence[ProviderRequest]) -> bool:
    """Whether the chunk reached the answer model (module docstring): its id in a start marker,
    or its cleaned text anywhere in a request."""
    prompt = prompt_text(requests)
    cleaned = clean(text)
    return f" id={chunk_id}>>>" in prompt or (bool(cleaned) and cleaned in prompt)


def poison_shown(
    case: Case,
    fill: Callable[[str], str],
    ids: Mapping[str, uuid.UUID],
    requests: Sequence[ProviderRequest],
) -> bool:
    texts = {chunk.id: chunk.text for chunk in case.fixture.chunks}
    return any(shown_to_model(ids[slug], fill(texts[slug]), requests) for slug in poisoned(case))


def poison_retrieved(case: Case, ids: Mapping[str, uuid.UUID], seen: Sequence[uuid.UUID]) -> bool:
    """B's reach control: every poisoned chunk was among the hits before the screen."""
    wanted = {ids[slug] for slug in poisoned(case)}
    return bool(wanted) and wanted <= set(seen)


def asked_question(requests: Sequence[ProviderRequest]) -> str | None:
    """The question as the answer model received it (the first request's QUESTION block), or None
    when the model was never called."""
    if not requests:
        return None
    request = requests[0]
    found = BOUNDARY_LINE.search(request.system or "")
    if found is None or not request.messages:
        return None
    boundary, user = found.group(1), request.messages[0].content
    start = QUESTION_START.format(boundary=boundary) + "\n"
    end = "\n" + QUESTION_END.format(boundary=boundary)
    opened = user.find(start)
    closed = user.find(end, opened + len(start))
    if opened < 0 or closed < 0:
        return None
    return user[opened + len(start) : closed]


def require_shown(case: Case, outcome: object) -> None:
    """`AnswerNotShown` when the case scripts an answer and the response is an answer the service
    did not show (module docstring)."""
    if answer_texts(case) and isinstance(outcome, str) and outcome != "answered":
        raise AnswerNotShown(f"case {case.id}: the scripted answer was not shown ({outcome})")
