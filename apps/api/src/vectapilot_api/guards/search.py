"""`ScreeningSearchService`: the search the agent's own `AnswerService` reads (M7 PR 6; G7, G13).

A subclass of `SearchService`, as G13's seams are (`AnswerService` takes the concrete class): it
overrides `search`, the one method `AnswerService` calls on its search, and passes every other
argument through. Each hit's content is checked against its input cap, counted for hidden
characters on the raw text, cleaned for the model and folded for the injection screen (G5); a
chunk with an injection signal, over its cap or empty once cleaned is **dropped before the prompt**
(G7), and every chunk that stays reaches the model as its clean content, never the raw text. The
hybrid `fused` list keeps only the chunks that stayed, so the answer's counts never name a dropped
chunk.

It is reachable under `agent_router = on` (`agent/service.py`'s `_answer`), through the agent's
own `AnswerService`, and under `guard_answer = on` (M7 PR 8), through `/v1/answer`'s separate
guarded `AnswerService`: M3's instance, which serves `/v1/answer` while `guard_answer` is off,
keeps the plain `SearchService` (G11), and `/v1/kb/search` never screens its hits.

**Fail closed.** The screen runs only inside a request the route tracks (`screens.track_screens`,
with the business's guards set); outside one, or when the matcher or the screen raises, the search
raises `GuardFailed` with the class name only, raised outside `except` and recorded in the tally
(`screens.seam_failed`), which the guarded `AgentService` passes through to the route
(`guard_error`, G3). A cancel propagates unchanged.
"""

from collections import Counter

from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.guards.config import BusinessGuards
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.screens import (
    NO_GUARD_SCOPE,
    ScreenTally,
    current_screens,
    noted,
    screen_chunk,
    seam_failed,
)
from vectapilot_api.guards.types import Check, Finding
from vectapilot_api.retrieval.search import (
    SEARCH_OPERATION,
    BusinessTransactions,
    RetrievalConfig,
    SearchResult,
    SearchService,
    SearchStats,
    StoredModels,
    VectorSearch,
)
from vectapilot_api.retrieval.types import FusedHit, Reader, SearchHit


class ScreeningSearchService(SearchService):
    """`SearchService` with G7's chunk screen on every hit (module docstring)."""

    def __init__(
        self,
        database: BusinessTransactions,
        runner: EmbeddingRunner,
        *,
        matcher: Matcher,
        retrieval: RetrievalConfig | None = None,
        search: VectorSearch | None = None,
        stored_models: StoredModels | None = None,
    ) -> None:
        """As `SearchService` (its vector stand-ins for tests), plus the matcher the screen uses
        (H1, or `create_app`'s test seam)."""
        super().__init__(
            database, runner, retrieval=retrieval, search=search, stored_models=stored_models
        )
        self._matcher = matcher

    async def search(
        self,
        business_id: str,
        question: str,
        *,
        k: int,
        reader: Reader,
        trace_id: str,
        operation: str = SEARCH_OPERATION,
        stats: SearchStats | None = None,
    ) -> SearchResult:
        tally = current_screens()
        business = None if tally is None else tally.business
        if tally is None or business is None:
            raise seam_failed(tally, NO_GUARD_SCOPE)
        result = await super().search(
            business_id,
            question,
            k=k,
            reader=reader,
            trace_id=trace_id,
            operation=operation,
            stats=stats,
        )
        failure: str | None = None
        try:
            kept = self._screen(result.hits, business, tally)
        except Exception as error:  # fail closed (module docstring); a cancel is not caught
            failure = type(error).__name__
        if failure is not None:
            raise seam_failed(tally, failure)
        by_id = {hit.chunk_id: hit for hit in kept}
        fused = None
        if result.fused is not None:  # hybrid: the same chunks, each with its clean content
            fused = tuple(
                FusedHit(hit=by_id[f.hit.chunk_id], rrf_score=f.rrf_score, ranks=f.ranks)
                for f in result.fused
                if f.hit.chunk_id in by_id
            )
        return SearchResult(hits=kept, embedding_model=result.embedding_model, fused=fused)

    def _screen(
        self, hits: tuple[SearchHit, ...], business: BusinessGuards, tally: ScreenTally
    ) -> tuple[SearchHit, ...]:
        noted(tally, Check.INJECTION)
        kept: list[SearchHit] = []
        found: Counter[Finding] = Counter()
        for hit in hits:
            content, findings = screen_chunk(hit.content, business.injection, self._matcher)
            found.update(findings)
            if content is None:
                tally.dropped_chunks += 1
                continue
            kept.append(SearchHit.model_validate(hit.model_dump() | {"content": content}))
            tally.kept_chunks[str(hit.chunk_id)] = content
        tally.findings.update(found)
        return tuple(kept)
