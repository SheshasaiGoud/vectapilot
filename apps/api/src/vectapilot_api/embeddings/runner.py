"""Embedding calls with retries and a record (M2 PR 5): what every caller of an `Embedder` uses.

- **Batches first.** `plan_batches` splits the texts before anything is sent, so a text that
  cannot fit fails the whole call before any cost. Each batch is then one `Embedder.embed` call.
- **Retries per batch** through H2's `call_with_retries`: only an error whose `retryable` is True
  is retried, a `Retry-After` is honoured up to the policy's cap, and a failed batch never repeats
  the batches before it (no double cost).
- **One record per embedding call** (one batch, its retries included): the `embedding_call` log
  record, metadata only (ADR 0007/0008): model, input count, the token estimate next to the tokens
  the provider reported, latency, outcome, attempts and the business. Never a text or a vector.
- **One model per call.** Every batch must answer with the same model id; otherwise the call fails
  (`ProviderError`) and nothing is stored: mixed vectors are never written (ADR 0008).
"""

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import Field

from vectapilot_api.embeddings.base import (
    Embedder,
    Embeddings,
    EmbeddingSpec,
    InputType,
    check_business_id,
    plan_batches,
    token_upper_bound,
)
from vectapilot_api.gateway.errors import GatewayError, ProviderError
from vectapilot_api.gateway.retry import call_with_retries
from vectapilot_api.gateway.types import (
    BusinessId,
    CallOutcome,
    Count,
    GatewayModel,
    OpaqueId,
    RetryPolicy,
    Slug,
)

EMBEDDING_CALL: Final = "embedding_call"
"""The log message of the one record per embedding call, and the key that holds the record."""

logger = logging.getLogger("vectapilot_api.embeddings")
# Pinned at INFO like the gateway's: a quieter `log_level` must never drop the records.
logger.setLevel(logging.INFO)


class EmbeddingCallRecord(GatewayModel):
    """One embedding call (one batch, retries included). Metadata only: never text or vectors."""

    business_id: BusinessId
    operation: Slug
    """What the call was for, e.g. `kb-ingest`, `kb-reindex`."""
    input_type: InputType
    model_requested: OpaqueId
    model_answered: OpaqueId | None
    """The id the provider returned; None when the call failed."""
    outcome: CallOutcome
    inputs: Count
    """How many texts the batch held."""
    token_estimate: Count
    """`token_upper_bound` summed over the batch (UTF-8 bytes + 8 per text, unverified)."""
    reported_tokens: Count | None
    """What the provider reported; None when the call failed. 0 for the fake."""
    attempts: Count
    retries: Count
    latency_ms: float = Field(ge=0)


@dataclass(frozen=True)
class EmbeddedTexts:
    """The vectors for all texts of one `EmbeddingRunner.embed`, in input order."""

    model: str
    dimension: int
    vectors: tuple[tuple[float, ...], ...] = field(repr=False)
    total_tokens: int
    calls: int


class EmbeddingRunner:
    """Batches, retries and records the calls to one `Embedder`. One instance per process."""

    def __init__(
        self,
        embedder: Embedder,
        spec: EmbeddingSpec,
        policy: RetryPolicy,
        *,
        deadline_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if embedder.model != spec.model or embedder.dimension != spec.dimension:
            raise ValueError("the embedder and the spec must name the same model and dimension")
        self._embedder = embedder
        self._spec = spec
        self._policy = policy
        self._deadline_s = deadline_s
        self._clock = clock
        self._sleep = sleep
        self._rand = rand

    @property
    def model(self) -> str:
        return self._spec.model

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_type: InputType,
        business_id: str,
        operation: str,
    ) -> EmbeddedTexts:
        """Embed `texts` in order, batch by batch. Raises the embedder's typed errors."""
        business = check_business_id(business_id)
        batches = plan_batches(texts, self._spec)  # raises before any call: no record, no cost
        results: list[Embeddings] = []
        for batch in batches:
            results.append(
                await self._call([texts[i] for i in batch], input_type, business, operation)
            )
            if results[-1].model != results[0].model:
                raise ProviderError()  # two model ids in one call: never mixed vectors
        return EmbeddedTexts(
            model=results[0].model,
            dimension=self._spec.dimension,
            vectors=tuple(vector for result in results for vector in result.vectors),
            total_tokens=sum(result.total_tokens for result in results),
            calls=len(results),
        )

    async def _call(
        self, batch: list[str], input_type: InputType, business_id: BusinessId, operation: str
    ) -> Embeddings:
        started = self._clock()
        attempts = 0
        result: Embeddings | None = None
        outcome = CallOutcome.INTERNAL_ERROR  # what an exception that is not a GatewayError records

        async def attempt() -> Embeddings:
            nonlocal attempts
            attempts += 1
            return await self._embedder.embed(batch, input_type=input_type, business_id=business_id)

        try:
            result = await call_with_retries(
                attempt,
                self._policy,
                deadline=started + self._deadline_s,
                clock=self._clock,
                sleep=self._sleep,
                rand=self._rand,
            )
            outcome = CallOutcome.OK
            return result
        except GatewayError as error:
            outcome = error.outcome
            raise
        except asyncio.CancelledError:  # caught only to record, then re-raised
            outcome = CallOutcome.CANCELLED
            raise
        finally:
            # A failure here must never replace the call's own result or exception.
            with contextlib.suppress(Exception):
                record = EmbeddingCallRecord(
                    business_id=business_id,
                    operation=operation,
                    input_type=input_type,
                    model_requested=self._spec.model,
                    model_answered=None if result is None else result.model,
                    outcome=outcome,
                    inputs=len(batch),
                    token_estimate=sum(token_upper_bound(text) for text in batch),
                    reported_tokens=None if result is None else result.total_tokens,
                    attempts=attempts,
                    retries=max(attempts - 1, 0),
                    latency_ms=max(0.0, (self._clock() - started) * 1000),
                )
                logger.info(EMBEDDING_CALL, extra={EMBEDDING_CALL: record.model_dump(mode="json")})


def batch_deadline_s(policy: RetryPolicy, request_timeout_s: float) -> float:
    """The longest one batch may take: every attempt at its timeout plus every wait at the cap.

    A safety bound derived from settings (no setting of its own): each request already has its
    timeout, so this only stops a retry whose wait would end after it.
    """
    attempts = policy.max_retries + 1
    return attempts * request_timeout_s + policy.max_retries * policy.max_delay_s
