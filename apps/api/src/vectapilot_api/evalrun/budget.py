"""The eval run's `--max-usd` cap: embeddings and the answer model under one cap (M2 PR 8).

M1's live-run pattern (ADR 0006 Exposure, ADR 0007 "Live-run stops"), extended to embeddings:
each paid request's worst case is reserved **before** it is sent, and the run stops before a
request whose reservation would cross the cap (`CapReached`). Nothing here reads a key.

- **Embeddings** (`CappedEmbedder`, wrapped around the configured embedder, so every attempt of
  every batch, ingest and question alike, passes through it): the worst case of one request is
  `sum(token_upper_bound(text)) * price / 1,000,000`, with `token_upper_bound` the embeddings
  module's assumed bound (UTF-8 bytes + 8 per text, unverified: ADR 0008, PR 4) and the catalog's
  list price (`embeddings/models.toml`). After the request the provider's reported tokens are
  counted; a report above the bound means the assumption failed, and the run stops after it
  (`CostCap.stop`), as M1's `bound_held`. A request that fails keeps its whole reservation. A
  request the cap refuses is never sent, but the runner still writes its `embedding_call` record
  (outcome `internal_error`, no tokens): read it next to the run's stop line.
- **The answer model** (`CappedGateway`, an `LLMGateway` whose two entry points reserve first):
  M1's formula (`measure.budget.request_reservation`) for the exact request about to be sent,
  system prompt and messages included, every retry and the repair included. Afterwards the call's
  `llm_call` record settles it as M1's `Ledger` does (complete cost; a cost-incomplete call keeps
  at least its reservation; a missing record keeps the reservation) and stops the run after a
  bound miss, an account or invalid-request outcome, or a `us` geo (M1's stop rules).

The cap counts per run. As in M1, it guards against mistakes only: the real control is that keys
never reach an agent, and the providers' own spend limits are the only cap across runs.
"""

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from vectapilot_api.embeddings.base import Embedder, Embeddings, InputType, token_upper_bound
from vectapilot_api.gateway.catalog import Catalog
from vectapilot_api.gateway.gateway import LLMGateway, StructuredResult
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.types import (
    BusinessId,
    CallRecord,
    LLMRequest,
    LLMResult,
    RetryPolicy,
)
from vectapilot_api.llm_cli import STOPPING_GEO, STOPPING_OUTCOMES
from vectapilot_api.measure.budget import (
    TOKENS_PER_MTOK,
    Reservation,
    bound_held,
    request_reservation,
)
from vectapilot_api.measure.records import RecordError, parse_llm_call_lines


def usd(value: Decimal) -> str:
    """A dollar amount as fixed-point text (never scientific), e.g. `$0.0042`."""
    return "$" + ("0" if value.is_zero() else format(value.normalize(), "f"))


class CapReached(Exception):
    """A paid request was not sent: its worst case on top of what the run counted would cross the
    cap. The message holds amounts and what was refused, never text."""


@dataclass
class CostCap:
    """What the run has counted against `cap` (None: no cap, a dry run without `--max-usd`)."""

    cap: Decimal | None
    embedding_usd: Decimal = field(default_factory=Decimal)
    llm_usd: Decimal = field(default_factory=Decimal)
    llm_cost_complete: bool = True
    """False once any answer call's cost was incomplete: `llm_usd` is then a lower bound."""
    embedding_requests: int = 0
    llm_calls: int = 0
    stop: str | None = None
    """Why the run must stop after the current question (the first reason wins); None: carry on."""

    @property
    def total(self) -> Decimal:
        return self.embedding_usd + self.llm_usd

    def fits(self, amount: Decimal) -> bool:
        return self.cap is None or self.total + amount <= self.cap

    def require(self, amount: Decimal, what: str) -> None:
        """Raise `CapReached` unless reserving `amount` keeps the run at or under the cap."""
        if not self.fits(amount):
            cap = usd(self.cap if self.cap is not None else Decimal(0))
            raise CapReached(
                f"{what} not sent: its worst case {usd(amount)} on top of the {usd(self.total)}"
                f" counted so far would cross the cap {cap}"
            )

    def stop_after(self, reason: str) -> None:
        if self.stop is None:
            self.stop = reason


# ── Embeddings ───────────────────────────────────────────────────────────────────────────────


def embedding_usd(tokens: int, price_per_mtok: Decimal) -> Decimal:
    return Decimal(tokens) * price_per_mtok / TOKENS_PER_MTOK


def embedding_reservation(texts: Sequence[str], price_per_mtok: Decimal) -> tuple[int, Decimal]:
    """(the token bound, its worst-case cost) of one embedding request of `texts`."""
    bound = sum(token_upper_bound(text) for text in texts)
    return bound, embedding_usd(bound, price_per_mtok)


class CappedEmbedder:
    """An `Embedder` that reserves each request's worst case against `cap` before sending it."""

    def __init__(self, inner: Embedder, price_per_mtok: Decimal, cap: CostCap) -> None:
        self._inner = inner
        self._price = price_per_mtok
        self._cap = cap

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: BusinessId
    ) -> Embeddings:
        bound, reserved = embedding_reservation(texts, self._price)
        self._cap.require(reserved, "an embedding request")
        self._cap.embedding_requests += 1
        try:
            result = await self._inner.embed(texts, input_type=input_type, business_id=business_id)
        except BaseException:
            self._cap.embedding_usd += reserved  # it may have been billed: keep the reservation
            raise
        self._cap.embedding_usd += embedding_usd(result.total_tokens, self._price)
        if result.total_tokens > bound:
            self._cap.stop_after(
                "an embedding request was billed beyond its token bound (UTF-8 bytes + 8 per"
                " text), so the bound's assumption does not hold; fix it before another live run"
            )
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()


# ── The answer model ─────────────────────────────────────────────────────────────────────────


def prompt_bytes(request: LLMRequest) -> int:
    """The UTF-8 bytes of a request's own text: the system prompt and every message."""
    system = len(request.system.encode("utf-8")) if request.system is not None else 0
    return system + sum(len(message.content.encode("utf-8")) for message in request.messages)


def record_for(lines: Sequence[str], trace_id: str | None) -> CallRecord | None:
    """The one `llm_call` record with `trace_id` among captured log lines, else None."""
    if trace_id is None:
        return None
    try:
        records = parse_llm_call_lines(lines)
    except RecordError:
        return None
    found = [record for record in records if record.trace_id == trace_id]
    return found[0] if len(found) == 1 else None


class CappedGateway(LLMGateway):
    """The gateway, with each call's worst case reserved against `cap` before it is made.

    `records(trace_id)` finds the call's `llm_call` record afterwards (the harness captures the
    gateway's log lines). Built like `build_gateway` builds the plain one (`harness.py`).
    """

    def __init__(
        self,
        provider: Provider,
        catalog: Catalog,
        *,
        attempt_timeout_s: float,
        deadline_s: float,
        retry_policy: RetryPolicy,
        max_repairs: int,
        cap: CostCap,
        records: Callable[[str | None], CallRecord | None],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        super().__init__(
            provider,
            catalog,
            attempt_timeout_s=attempt_timeout_s,
            deadline_s=deadline_s,
            retry_policy=retry_policy,
            max_repairs=max_repairs,
            clock=clock,
            sleep=sleep,
            rand=rand,
        )
        self._cost_cap = cap
        self._records = records

    def reservation(self, request: LLMRequest, schema: type[BaseModel] | None) -> Reservation:
        """The worst case of this exact request (M1's formula), with this gateway's limits."""
        return request_reservation(
            case_id=request.trace_id or "untraced",
            alias_name=request.alias,
            prompt_bytes=prompt_bytes(request),
            schema=schema,
            catalog=self._catalog,
            max_retries=self._retry_policy.max_retries,
            max_repairs=self._max_repairs,
        )

    async def complete(self, request: LLMRequest) -> LLMResult:
        reservation = self._reserve(request, None)
        try:
            return await super().complete(request)
        finally:
            self._settle(request, reservation)

    async def complete_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> StructuredResult[T]:
        reservation = self._reserve(request, schema)
        try:
            return await super().complete_structured(request, schema)
        finally:
            self._settle(request, reservation)

    def _reserve(self, request: LLMRequest, schema: type[BaseModel] | None) -> Reservation:
        reservation = self.reservation(request, schema)
        self._cost_cap.require(reservation.usd, "an answer-model call")
        self._cost_cap.llm_calls += 1
        return reservation

    def _settle(self, request: LLMRequest, reservation: Reservation) -> None:
        """Count a finished call (M1's `Ledger.settle` rules) and decide whether to stop. Never
        raises: it runs in a `finally`, and must not replace the call's own exception."""
        with contextlib.suppress(Exception):
            record = self._records(request.trace_id)
            cap = self._cost_cap
            if record is None:
                cap.llm_usd += reservation.usd
                cap.llm_cost_complete = False
                cap.stop_after("an answer call has no single llm_call record (reservation kept)")
                return
            if record.cost_complete:
                cap.llm_usd += record.cost_usd
            else:
                cap.llm_usd += max(reservation.usd, record.cost_usd)
                cap.llm_cost_complete = False
            reason = STOPPING_OUTCOMES.get(record.outcome)
            if reason is not None:
                cap.stop_after(f"an answer call ended {record.outcome.value}: {reason}")
            elif not bound_held(reservation, record):
                cap.stop_after(
                    "an answer call was billed beyond its worst-case bound (measure/budget.py);"
                    " fix the bound before another live run"
                )
            elif record.inference_geo == STOPPING_GEO:
                cap.stop_after(
                    f"an answer call ran in the {STOPPING_GEO} geo: the workspace default must be"
                    " global (ADR 0006)"
                )
