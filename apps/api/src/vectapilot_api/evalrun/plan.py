"""The run's worst-case cost, printed before anything is sent (M2 PR 8). Pure.

The same bounds the cap reserves call by call (`budget.py`), added up over the whole plan at the
catalogs' list prices, every retry and repair included:

- **ingest**: every chunk of every listed document, `token_upper_bound(content)` tokens, times
  `1 + max_retries` attempts, at the embedding price. A real run embeds only documents its
  namespace does not hold yet (often none), so this is an upper bound.
- **per question**: the question's embedding (`token_upper_bound(question)`, times the attempts)
  plus the answer call's reservation (M1's formula, `measure.budget.request_reservation`) for the
  largest prompt the question can produce: `answer/prompt.build_prompt` over the `search_k`
  customer-visible chunks of its business with the most prompt bytes. No prompt can be longer,
  since a prompt's length is fixed text plus, per chunk, its title, language, page and content.

What the cap then does is per call: the plan may exceed the cap, and the run stops (partial)
before the first call that would cross it.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from vectapilot_api.answer.prompt import build_prompt
from vectapilot_api.answer.types import ModelAnswer
from vectapilot_api.embeddings.base import token_upper_bound
from vectapilot_api.evalrun.budget import embedding_usd
from vectapilot_api.evalset.corpus import Corpus
from vectapilot_api.evalset.questions import EvalQuestion
from vectapilot_api.gateway.catalog import Catalog
from vectapilot_api.ingestion.types import ChunkingConfig
from vectapilot_api.measure.budget import request_reservation
from vectapilot_api.retrieval.types import SearchHit

PAGE_LINE_BYTES = len(b"\nPage: ")


@dataclass(frozen=True)
class PlanBound:
    ingest_tokens: int
    ingest_usd: Decimal
    question_embedding_usd: Decimal
    answer_usd: Decimal
    largest_answer_usd: Decimal

    @property
    def total(self) -> Decimal:
        return self.ingest_usd + self.question_embedding_usd + self.answer_usd


def _visible_hits(
    corpus: Corpus, business_id: str, config: ChunkingConfig, embedding_model: str
) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for document in corpus.documents(business_id):
        source = document.source
        if not source.answers_customers:
            continue
        for chunk in corpus.chunks(document, config):
            hits.append(
                SearchHit(
                    chunk_id=uuid.UUID(int=len(hits) + 1),
                    document_id=uuid.UUID(int=len(hits) + 1),
                    source_id=source.source_id,
                    version=source.version,
                    title=source.title,
                    language=source.language,
                    content_kind=source.content_kind,
                    page=chunk.page,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    score=0.0,
                    embedding_model=embedding_model,
                )
            )
    return hits


def _block_bytes(hit: SearchHit) -> int:
    """The bytes of a hit's own text in its prompt block (the rest of a block is fixed)."""
    page = PAGE_LINE_BYTES + len(str(hit.page)) if hit.page is not None else 0
    return sum(len(text.encode("utf-8")) for text in (hit.title, hit.language, hit.content)) + page


def worst_case(
    corpus: Corpus,
    business_ids: Sequence[str],
    questions: Sequence[EvalQuestion],
    config: ChunkingConfig,
    *,
    search_k: int,
    embedding_model: str,
    embedding_price: Decimal,
    catalog: Catalog,
    answer_alias: str,
    max_retries: int,
    max_repairs: int,
) -> PlanBound:
    """The plan's worst case (module docstring)."""
    attempts = 1 + max_retries
    ingest_tokens = sum(
        token_upper_bound(chunk.content)
        for business_id in business_ids
        for document in corpus.documents(business_id)
        for chunk in corpus.chunks(document, config)
    )
    largest: Mapping[str, list[SearchHit]] = {
        business_id: sorted(
            _visible_hits(corpus, business_id, config, embedding_model),
            key=_block_bytes,
            reverse=True,
        )[:search_k]
        for business_id in business_ids
    }
    embed = Decimal(0)
    answer = Decimal(0)
    single = Decimal(0)
    for question in questions:
        embed += embedding_usd(token_upper_bound(question.question) * attempts, embedding_price)
        hits = largest.get(question.business_id, [])
        if not hits:
            continue  # nothing to retrieve: no model call is possible
        prompt = build_prompt(question.question, hits)
        reservation = request_reservation(
            case_id=question.id,
            alias_name=answer_alias,
            prompt_bytes=len(prompt.system.encode("utf-8")) + len(prompt.user.encode("utf-8")),
            schema=ModelAnswer,
            catalog=catalog,
            max_retries=max_retries,
            max_repairs=max_repairs,
        )
        answer += reservation.usd
        single = max(single, reservation.usd)
    return PlanBound(
        ingest_tokens=ingest_tokens,
        ingest_usd=embedding_usd(ingest_tokens * attempts, embedding_price),
        question_embedding_usd=embed,
        answer_usd=answer,
        largest_answer_usd=single,
    )
