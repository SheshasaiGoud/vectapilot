"""The executor: the only way to run a read tool or propose a write (ADR 0016 decision 1, M5 PR 2).

One `ToolExecutor` per business, built from that business's `tools.json`. Each call:

1. **Context is injected (W7).** `ToolContext` carries the business, the customer and the trace id;
   the executor refuses a context for any business but its own, and every statement gets them
   bound by `ToolScope.fetch`. A tool's arguments never hold them: the input models forbid
   unknown fields, so an argument named `business_id` is `invalid_arguments`.
2. **Enabled tools only.** A name the business does not enable, or no platform tool has, is
   `not_enabled`.
3. **Arguments are validated** against the business's input model (`spec.business_input_model`):
   a failure is `invalid_arguments`, naming schema fields only.
4. **Reads run in a snapshot (W8, W9).** `tool_transaction` opens `REPEATABLE READ READ ONLY`
   (`db.py`), so a write is refused by Postgres (SQLSTATE 25006), and sets `statement_timeout` and
   `lock_timeout` below the call's budget, transaction-local. `asyncio.timeout(budget)` is only
   the outer backstop. A server timeout (SQLSTATE 57014, 55P03) or the backstop is `timeout`; any
   other database or network failure is `unavailable`.
5. **`propose` never writes (W1, W3).** A write tool's `validate` runs in the same read-only
   snapshot and returns typed summary fields; the summary is its fixed template over them. The
   result is a `WriteProposal`. PR 3 stores it; only the confirm route executes it.
6. **Leak fence.** The business of every row read is in the envelope. Any row of another business
   makes the result `unavailable` (fail closed), and the ids stay in the envelope for the eval's
   leak check, never in the observation.
7. **Caps (W10).** A read's output is cut to `ObservationCaps.max_rows` rows, then row by row
   until its rendered observation fits `max_bytes` (`truncated` is set). The floor is zero rows:
   an empty list marked `truncated` still tells the loop to narrow the search. An output that
   cannot fit even so (or a tool without rows) is `unavailable`.
8. **Audit (W12), fail closed.** Every call for a platform tool inserts one `tool_audit` row in a
   short transaction of its own, after the read transaction ended (that one is read-only): tool,
   kind, phase, outcome, duration, trace id, action id (none before PR 3). Never the arguments,
   the result or an arguments hash (low-entropy ids are reversible by enumeration). If the insert
   fails, the call returns `unavailable`: no observation without its audit row.
9. **One `tool_call` log record per call,** cancellations and bugs included, with the same
   metadata (and the business). A cancelled call is logged and re-raised, never audited: awaiting
   the database in a cancelled task would hold the cancel up. A bug (any other exception) is
   logged with its type only and re-raised; the loop hands off on it (H1 rule 7). A caller's
   bug (a context for another business, `run` for a write tool or `propose` for a read) is such
   a bug: logged as `error` and raised, so a loop that dispatches without `kind(tool)` shows up.

Errors carry fixed text (`types.ERROR_MESSAGES`), so a call never echoes a value or a database
message, and a foreign id and a missing id give byte-identical observations.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.config import ToolName, ToolsConfig, ToolsModel
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.spec import (
    READ_TIMEOUT_S,
    AnyTool,
    ReadTool,
    ToolScope,
    business_input_model,
)
from vectapilot_api.tools.types import (
    OK,
    PROPOSED,
    UNEXPECTED_FIELD,
    ToolContext,
    ToolEnvelope,
    ToolError,
    ToolErrorCode,
    ToolKind,
    WriteProposal,
    render,
)

TOOL_CALL: Final = "tool_call"
"""The log message of the one record per call, and the key that holds it (as `llm_call`)."""
CANCELLED: Final = "cancelled"
ERROR: Final = "error"
"""Log-only outcomes: never in `tool_audit` (a cancelled call and a bug are not audited)."""

logger = logging.getLogger("vectapilot_api.tools")
# Pinned at INFO, as the gateway's: a quieter `log_level` must never drop a `tool_call` record.
logger.setLevel(logging.INFO)

TIMEOUT_SQLSTATES: Final = frozenset({"57014", "55P03"})
"""query_canceled (what `statement_timeout` raises) and lock_not_available (`lock_timeout`)."""
STATEMENT_SHARE: Final = 0.75
LOCK_SHARE: Final = 0.5
"""Shares of a call's budget for the server-side timeouts [proposal]: below the budget, so the
server ends a slow statement before the asyncio backstop has to, and the lock wait ends first."""
AUDIT_TIMEOUT_S: Final = 2.0

SET_TIMEOUTS: Final = text(
    "SELECT set_config('statement_timeout', :statement, true),"
    " set_config('lock_timeout', :lock, true)"
)
"""`SET LOCAL` as a function: `SET` takes no bind parameters, `set_config(..., true)` does, and is
just as transaction-local (it ends at commit or rollback, so a pooled connection keeps none)."""

AUDIT_INSERT: Final = text(
    "INSERT INTO tool_audit (business_id, tool, kind, phase, outcome, duration_ms, trace_id,"
    " action_id) VALUES (:business_id, :tool, :kind, :phase, :outcome, :duration_ms, :trace_id,"
    " :action_id)"
)
"""Metadata only: the columns are the whole record (W12)."""


class AuditPhase(StrEnum):
    """Migration 0006's `tool_audit.phase` values (a test compares)."""

    RUN = "run"
    PROPOSE = "propose"
    CONFIRM = "confirm"


class TransactionSource(Protocol):
    """What the executor needs from the database: `Database` in the API (its role check
    included); in the canary suite, an owner engine without it."""

    def business_transaction(
        self, business_id: str, *, snapshot: bool = False, read_committed: bool = False
    ) -> AbstractAsyncContextManager[AsyncConnection]: ...


class ObservationCaps(ToolsModel):
    """W10's caps on one observation [proposal; estimates, not measurements]."""

    max_rows: int = Field(default=20, ge=0, le=20)
    max_bytes: int = Field(default=8192, ge=512, le=65536)


class WrongToolKind(ValueError):
    """`run` was asked for a write tool or `propose` for a read tool: a caller's bug."""


def server_timeouts(budget_s: float) -> tuple[str, str]:
    """`statement_timeout` and `lock_timeout` for a call of `budget_s` seconds, in milliseconds."""
    statement = max(1, int(budget_s * 1000 * STATEMENT_SHARE))
    lock = max(1, int(budget_s * 1000 * LOCK_SHARE))
    return f"{statement}ms", f"{lock}ms"


@asynccontextmanager
async def tool_transaction(
    source: TransactionSource, business_id: str, budget_s: float, *, read_only: bool
) -> AsyncIterator[AsyncConnection]:
    """A business transaction with server timeouts below `budget_s`; `read_only` makes it the
    `REPEATABLE READ READ ONLY` snapshot, otherwise it is READ COMMITTED, set explicitly so no
    server, database or role default can change it (W8; `db.READ_COMMITTED`)."""
    async with source.business_transaction(
        business_id, snapshot=read_only, read_committed=not read_only
    ) as connection:
        statement, lock = server_timeouts(budget_s)
        await connection.execute(SET_TIMEOUTS, {"statement": statement, "lock": lock})
        yield connection


def failure_of(error: BaseException) -> ToolErrorCode:
    """The typed failure for a database or network error: `timeout` for a server timeout."""
    if isinstance(error, DBAPIError):
        state = getattr(error.orig, "sqlstate", None)
        if state in TIMEOUT_SQLSTATES:
            return ToolErrorCode.TIMEOUT
    return ToolErrorCode.UNAVAILABLE


def field_names(error: ValidationError, model: type[BaseModel]) -> tuple[str, ...]:
    """The schema fields `error` names; an unknown key shows as `UNEXPECTED_FIELD`, never itself.
    A whole-model rule (the date range) names no field."""
    names: set[str] = set()
    for problem in error.errors():
        location = problem["loc"]
        if not location:
            continue
        first = location[0]
        names.add(
            first if isinstance(first, str) and first in model.model_fields else UNEXPECTED_FIELD
        )
    return tuple(sorted(names))[:10]


def platform_name(tool: str) -> ToolName | None:
    """`tool` as a platform tool's name, or None (then `not_enabled`, and never repeated)."""
    try:
        return ToolName(tool)
    except ValueError:
        return None


def outcome_of(result: BaseModel) -> str:
    if isinstance(result, ToolError):
        return result.code.value
    return PROPOSED if isinstance(result, WriteProposal) else OK


@dataclass(frozen=True)
class AuditEntry:
    business_id: str
    tool: ToolName
    kind: ToolKind
    phase: AuditPhase
    outcome: str
    duration_ms: float
    trace_id: str | None
    action_id: UUID | None = None


class ToolExecutor:
    """Runs one business's read tools and proposes its write tools (never executes one)."""

    def __init__(
        self,
        database: TransactionSource,
        config: ToolsConfig,
        *,
        registry: Mapping[ToolName, AnyTool] = REGISTRY,
        caps: ObservationCaps | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._database = database
        self._config = config
        self._registry = registry
        self._caps = caps or ObservationCaps()
        self._clock = clock
        self._inputs = {
            name: business_input_model(registry[name], config) for name in config.enabled_tools
        }

    @property
    def business_id(self) -> str:
        return self._config.business_id

    @property
    def enabled(self) -> tuple[ToolName, ...]:
        """The business's enabled tools, sorted (stable schema order for PR 4 and PR 5)."""
        return tuple(sorted(self._inputs))

    def kind(self, tool: ToolName) -> ToolKind:
        return self._registry[tool].kind

    def input_model(self, tool: ToolName) -> type[BaseModel]:
        """The business's input model for an enabled tool (its JSON schema is what a model or an
        MCP client sees)."""
        return self._inputs[tool]

    async def run(
        self, tool: str, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolEnvelope:
        """Run a read tool; its typed output or typed error, in the envelope."""
        return await self._call(tool, arguments, context, ToolKind.READ)

    async def propose(
        self, tool: str, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolEnvelope:
        """Validate a write tool's call in a read-only snapshot and return a `WriteProposal` with
        its rendered summary, or a typed refusal. Writes nothing but its own audit row."""
        return await self._call(tool, arguments, context, ToolKind.WRITE)

    # ── One call ───────────────────────────────────────────────────────────────────────────

    def _refuse_caller_bugs(
        self, name: ToolName | None, context: ToolContext, wanted: ToolKind
    ) -> None:
        """Raise on a caller's bug: a context for another business (a possible isolation bug) or
        the wrong entry point for a platform tool (`run` for a write, `propose` for a read; the
        loop dispatches by `kind(tool)`). Inside `_call`'s logged block, so each leaves its
        `tool_call` record (W12), never an audit row: nothing ran."""
        if context.business_id != self.business_id:
            raise ValueError("the context names another business than this executor's")
        if name is not None and name in self._registry and self._registry[name].kind is not wanted:
            raise WrongToolKind(f"{name.value} is a {self._registry[name].kind.value} tool")

    async def _call(
        self, tool: str, arguments: Mapping[str, object], context: ToolContext, kind: ToolKind
    ) -> ToolEnvelope:
        name = platform_name(tool)
        phase = AuditPhase.RUN if kind is ToolKind.READ else AuditPhase.PROPOSE
        scope = ToolScope(self.business_id, context.customer_ref, self._config)
        started = self._clock()

        def log(outcome: str, *, audited: bool) -> None:
            self._log(name, kind, phase, outcome, started, context, audited=audited)

        try:
            self._refuse_caller_bugs(name, context, kind)
            result = await self._attempt(name, arguments, scope, kind)
            result = self._fence(result, scope)
            result, observation = self._observe(name, result)
            outcome = outcome_of(result)
            audited = name is not None and await self._audit(
                AuditEntry(
                    business_id=self.business_id,
                    tool=name,
                    kind=kind,
                    phase=phase,
                    outcome=outcome,
                    duration_ms=self._elapsed_ms(started),
                    trace_id=context.trace_id,
                )
            )
        except asyncio.CancelledError:
            log(CANCELLED, audited=False)
            raise
        except Exception:
            log(ERROR, audited=False)
            raise
        if name is not None and not audited:
            result = ToolError.of(ToolErrorCode.UNAVAILABLE)
            observation = render(name, result)
            outcome = outcome_of(result)
        log(outcome, audited=audited)
        return ToolEnvelope(
            tool=name,
            kind=kind,
            outcome=outcome,
            result=result,
            observation=observation,
            business_ids=frozenset(scope.seen),
        )

    async def _attempt(
        self,
        name: ToolName | None,
        arguments: Mapping[str, object],
        scope: ToolScope,
        kind: ToolKind,
    ) -> BaseModel:
        if name is None or name not in self._inputs:
            return ToolError.of(ToolErrorCode.NOT_ENABLED)
        spec = self._registry[name]
        model = self._inputs[name]
        try:
            validated = model.model_validate(dict(arguments))
        except ValidationError as error:
            return ToolError.of(ToolErrorCode.INVALID_ARGUMENTS, field_names(error, model))
        budget = spec.timeout_s if kind is ToolKind.READ else READ_TIMEOUT_S
        try:
            async with (
                asyncio.timeout(budget),
                tool_transaction(self._database, scope.business_id, budget, read_only=True) as c,
            ):
                if isinstance(spec, ReadTool):
                    output: BaseModel = await spec.run(c, validated, scope)
                    return output
                fields = await spec.validate(c, validated, scope)
        except TimeoutError:  # before OSError: TimeoutError is one
            return ToolError.of(ToolErrorCode.TIMEOUT)
        except (SQLAlchemyError, OSError) as error:
            return ToolError.of(failure_of(error))
        if isinstance(fields, ToolError):
            return fields
        return WriteProposal(
            tool=name,
            arguments=validated.model_dump(mode="json"),
            summary=spec.summary(fields),
        )

    def _fence(self, result: BaseModel, scope: ToolScope) -> BaseModel:
        """Fail closed on any row of another business (the ids stay in the envelope)."""
        if scope.seen <= {scope.business_id}:
            return result
        logger.warning("tool_foreign_rows", extra={"business_id": scope.business_id})
        return ToolError.of(ToolErrorCode.UNAVAILABLE)

    def _observe(self, name: ToolName | None, result: BaseModel) -> tuple[BaseModel, str]:
        """`result` within the caps, and its observation text."""
        spec = None if name is None else self._registry[name]
        if isinstance(result, ToolError | WriteProposal) or not isinstance(spec, ReadTool):
            return result, render(name, result)
        rows = self._caps.max_rows
        while rows >= 0:
            capped = result if spec.cap_rows is None else spec.cap_rows(result, rows)
            observation = render(name, capped)
            if len(observation.encode("utf-8")) <= self._caps.max_bytes:
                return capped, observation
            if spec.cap_rows is None:
                break
            rows -= 1
        unavailable = ToolError.of(ToolErrorCode.UNAVAILABLE)
        return unavailable, render(name, unavailable)

    async def _audit(self, entry: AuditEntry) -> bool:
        """Insert `entry` in its own short transaction; False if that failed (never raised)."""
        try:
            async with (
                asyncio.timeout(AUDIT_TIMEOUT_S),
                tool_transaction(
                    self._database, entry.business_id, AUDIT_TIMEOUT_S, read_only=False
                ) as connection,
            ):
                await connection.execute(
                    AUDIT_INSERT,
                    {
                        "business_id": entry.business_id,
                        "tool": entry.tool.value,
                        "kind": entry.kind.value,
                        "phase": entry.phase.value,
                        "outcome": entry.outcome,
                        "duration_ms": entry.duration_ms,
                        "trace_id": entry.trace_id,
                        "action_id": entry.action_id,
                    },
                )
        except (TimeoutError, SQLAlchemyError, OSError) as error:
            logger.warning(
                "tool_audit_failed",
                extra={"business_id": entry.business_id, "reason": type(error).__name__},
            )
            return False
        return True

    def _elapsed_ms(self, started: float) -> float:
        return max(0.0, (self._clock() - started) * 1000)

    def _log(
        self,
        name: ToolName | None,
        kind: ToolKind,
        phase: AuditPhase,
        outcome: str,
        started: float,
        context: ToolContext,
        *,
        audited: bool,
    ) -> None:
        record = {
            "business_id": self.business_id,  # the context's, unless that was the caller's bug
            "tool": None if name is None else name.value,
            "kind": kind.value,
            "phase": phase.value,
            "outcome": outcome,
            "duration_ms": self._elapsed_ms(started),
            "trace_id": context.trace_id,
            "action_id": None,
            "audited": audited,
        }
        logger.info(TOOL_CALL, extra={TOOL_CALL: record})
