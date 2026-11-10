"""Confirming a pending action: the only path to a write (decided D3 (a), D4 (a); W2, W5, W6, W8,
W9, W12). The only module that imports `tools.execute` (W1, tested).

`ConfirmService.confirm(business_id, action_id, request, trace_id=...)` runs one transaction, in
READ COMMITTED (W8: a waiting second confirm re-reads the row instead of failing with SQLSTATE
40001), with `statement_timeout` and `lock_timeout` below the write budget (W9) and
`asyncio.timeout` only as the outer backstop. The checks, in W6's order:

1. **Visible?** The action is read and locked (`FOR UPDATE`) by business (RLS and the statement's
   own clause), customer and id together. Any other action, another business's, another
   customer's or an unknown id, is simply not there: `ActionNotFound`.
2. **Token?** The SHA-256 of the request's token against the stored one, in constant time
   (`tokens.token_matches`). A wrong token is `ActionNotFound`, the same 404 (W6); the stored
   result is not shown either.
3. **Already done?** An `executed` or `failed` action returns its stored result, whatever its
   expiry (W5: a repeat, concurrent or retried confirm returns one stored result and writes
   nothing). The action id is the idempotency key (D4).
4. **Expired?** `expires_at` against the database's clock: `ActionNotFound`.
5. **Confirm and execute,** in the same transaction: `confirmed_at` and `confirm_trace_id` are set
   (only here, W2), `tools.execute` re-validates and writes (W5; a SAVEPOINT turns a lost slot
   into `slot_unavailable`), the action is marked `executed` with its result or `failed` with its
   reason, and the audit row is inserted. One COMMIT for all of it; migration 0006's deferred
   composite keys are checked before it (`SET CONSTRAINTS ALL IMMEDIATE`, the body's last
   statement): every write row must belong to an executed action of its own business and
   customer. A violation there is a bug (`BrokenWrite`, a 500), never an unknown outcome, so the
   COMMIT itself can then fail only in transport.

**Audit rows (W12).** An executed or failed action, a refusal of a known action (wrong token,
expired) and a repeat insert their audit row inside the confirm's own transaction, so every
refusal costs the same one transaction as an unknown id (no timing oracle from a second one; the
one INSERT a known action's refusal adds is a known gap). An action that names no tool this
business and customer have (an unknown id) is logged only, since `tool_audit.tool` is required.

**Failures (W9, W12).** A timeout (the backstop, `statement_timeout`, `lock_timeout`) or any error
while committing leaves the outcome unknown: `OutcomeUnknown` ("retry confirm"), never "not
written", because a timed-out attempt may have waited behind another confirm of the same action
that is about to commit. The retry returns the stored result if a COMMIT landed or executes once
if none did. Any other database or network error before the COMMIT rolled this attempt back:
`ConfirmFailed` ("this attempt changed nothing; confirm again"). Both are audited in a short
transaction after the main one (when the tool is known). A bug (any other exception) is audited
as `rolled_back`, logged and raised. A cancelled confirm is logged and re-raised, never audited.
Every call leaves one `tool_call` record.

Nothing here logs or audits the token, its hash, the arguments or the result (W2, W12).
"""

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.actions.audit import (
    CallRecord,
    audit_after,
    insert_audit,
    log_call,
    unknown_outcome,
)
from vectapilot_api.actions.tokens import ConfirmToken, token_matches
from vectapilot_api.actions.types import (
    EXECUTED_MESSAGE,
    FAILURE_MESSAGES,
    ActionNotFound,
    ActionStatus,
    ConfirmFailed,
    ConfirmRequest,
    ConfirmResponse,
    OutcomeUnknown,
    WriteResult,
)
from vectapilot_api.tools.book import BookOutput
from vectapilot_api.tools.config import ToolName, ToolsConfig
from vectapilot_api.tools.create_ticket import CreateTicketOutput
from vectapilot_api.tools.execute import (
    ConfirmedAction,
    Executed,
    WriteOutcome,
    execute,
)
from vectapilot_api.tools.executor import (
    CANCELLED,
    ERROR,
    AuditEntry,
    AuditPhase,
    TransactionSource,
    tool_transaction,
)
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.reschedule import RescheduleOutput
from vectapilot_api.tools.spec import WRITE_TIMEOUT_S, ToolScope
from vectapilot_api.tools.types import WriteFailure


class ConfirmOutcome(StrEnum):
    """`tool_audit.outcome` of a confirm, besides the `WriteFailure` reasons of a failed one."""

    EXECUTED = "executed"
    REPLAYED = "replayed"
    NOT_FOUND = "not_found"
    WRONG_TOKEN = "wrong_token"  # noqa: S105 (an audit outcome, not a secret)
    EXPIRED = "expired"
    ROLLED_BACK = "rolled_back"  # audit.ROLLED_BACK (a test compares)
    OUTCOME_UNKNOWN = "outcome_unknown"  # audit.OUTCOME_UNKNOWN


WRITE_RESULTS: Final = (BookOutput, RescheduleOutput, CreateTicketOutput)

LOCK_ACTION: Final = text(
    "SELECT business_id, id, tool, arguments, customer_ref, confirm_token_sha256, status,"
    " result, failure_reason, expires_at <= clock_timestamp() AS expired"
    " FROM pending_actions"
    " WHERE business_id = :business_id AND customer_ref = :customer_ref AND id = :action_id"
    " FOR UPDATE"
)
"""Business, customer and id together: an action of anyone else is not there (W6). FOR UPDATE: a
second confirm of this action waits here, then (READ COMMITTED) reads the committed row.
`clock_timestamp()`: the expiry is judged when the row is read, after any lock wait."""
MARK_CONFIRMED: Final = text(
    "UPDATE pending_actions SET confirmed_at = now(), confirm_trace_id = :trace_id"
    " WHERE business_id = :business_id AND customer_ref = :customer_ref AND id = :action_id"
    " AND status = 'pending'"
    " RETURNING business_id, confirmed_at, confirm_trace_id"
)
"""The only statement that sets `confirmed_at` and `confirm_trace_id` (W2)."""
FINISH: Final = text(
    "UPDATE pending_actions SET status = :status, finished_at = now(),"
    " result = CAST(:result AS jsonb), failure_reason = :failure_reason"
    " WHERE business_id = :business_id AND customer_ref = :customer_ref AND id = :action_id"
    " AND status = 'pending' AND confirmed_at IS NOT NULL"
)
CHECK_KEYS_NOW: Final = text("SET CONSTRAINTS ALL IMMEDIATE")
"""The body's last statement: migration 0006's deferred action keys are checked here, in the same
transaction and retroactively for every row written so far, instead of at COMMIT. A violation
then surfaces as a statement's error (a bug, `BrokenWrite`) rather than as a failed COMMIT, which
would look like an unknown outcome and make every retry fail the same way until the action
expires. The keys still hold before anything commits; migration 0006 is unchanged."""


class BrokenWrite(RuntimeError):
    """A write row's deferred action key does not hold: a bug in the write path, rolled back."""


@dataclass
class _Attempt:
    """What one confirm learnt so far: the action's tool (once read) and whether the transaction
    body finished, so an error after it happened while committing (an unknown outcome)."""

    tool: ToolName | None = None
    committing: bool = False


@dataclass(frozen=True)
class _Decision:
    """What the main transaction decided: the audit outcome, whether its audit row was inserted
    in that transaction, and the response (None: a refusal, `ActionNotFound`)."""

    outcome: str
    audited: bool
    response: ConfirmResponse | None = None


def stored_response(row: Row[Any]) -> ConfirmResponse:
    """The stored result of an executed or failed action, as the first confirm returned it."""
    tool = ToolName(row.tool)
    if row.status == ActionStatus.EXECUTED:
        output = REGISTRY[tool].output_model.model_validate(row.result)
        return executed_response(row.id, tool, output)
    return failed_response(row.id, tool, WriteFailure(row.failure_reason))


def executed_response(action_id: UUID, tool: ToolName, output: object) -> ConfirmResponse:
    if not isinstance(output, WRITE_RESULTS):  # pragma: no cover (the three write tools)
        raise TypeError("an executed action's result is its write tool's output")
    result: WriteResult = output
    return ConfirmResponse(
        action_id=action_id,
        tool=tool,
        status=ActionStatus.EXECUTED,
        result=result,
        message=EXECUTED_MESSAGE,
    )


def failed_response(action_id: UUID, tool: ToolName, reason: WriteFailure) -> ConfirmResponse:
    return ConfirmResponse(
        action_id=action_id,
        tool=tool,
        status=ActionStatus.FAILED,
        failure_reason=reason,
        message=FAILURE_MESSAGES[reason],
    )


class ConfirmService:
    """Confirms the pending actions of the businesses in `configs` (each one's `tools.json`).

    A business without a configuration has no tools, so it has nothing to confirm: every confirm
    for it is `ActionNotFound`."""

    def __init__(
        self,
        database: TransactionSource,
        configs: Mapping[str, ToolsConfig],
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._database = database
        self._configs = configs
        self._clock = clock

    async def confirm(
        self, business_id: str, action_id: UUID, request: ConfirmRequest, *, trace_id: str
    ) -> ConfirmResponse:
        """Execute `action_id` once, or return its stored result; see the module docstring."""
        started = self._clock()
        attempt = _Attempt()

        def call(outcome: str) -> CallRecord:
            return CallRecord(
                business_id=business_id,
                tool=attempt.tool,
                phase=AuditPhase.CONFIRM,
                outcome=outcome,
                duration_ms=max(0.0, (self._clock() - started) * 1000),
                trace_id=trace_id,
                action_id=action_id,
            )

        config = self._configs.get(business_id)
        if config is None:
            log_call(call(ConfirmOutcome.NOT_FOUND), audited=False)
            raise ActionNotFound
        scope = ToolScope(business_id, request.customer_ref, config)
        try:
            decision = await self._transaction(scope, action_id, request, trace_id, attempt, call)
        except asyncio.CancelledError:
            log_call(call(CANCELLED), audited=False)
            raise
        except (TimeoutError, SQLAlchemyError, OSError) as error:
            unknown = unknown_outcome(error, committing=attempt.committing)
            record = call(ConfirmOutcome.OUTCOME_UNKNOWN if unknown else ConfirmOutcome.ROLLED_BACK)
            log_call(record, audited=await self._audit_after(record))
            failure: Exception = OutcomeUnknown() if unknown else ConfirmFailed()
        except Exception:
            # A bug. Raised before the COMMIT, it rolled the transaction back: audited as such.
            record = call(ConfirmOutcome.ROLLED_BACK)
            audited = False if attempt.committing else await self._audit_after(record)
            log_call(call(ERROR), audited=audited)
            raise
        else:
            log_call(call(decision.outcome), audited=decision.audited)
            if decision.response is None:
                raise ActionNotFound
            return decision.response
        raise failure  # outside the `except`: no database message is chained to it

    async def _audit_after(self, record: CallRecord) -> bool:
        entry = record.audit_entry()
        return entry is not None and await audit_after(self._database, entry)

    async def _transaction(
        self,
        scope: ToolScope,
        action_id: UUID,
        request: ConfirmRequest,
        trace_id: str,
        attempt: _Attempt,
        call: Callable[[str], CallRecord],
    ) -> _Decision:
        async with (
            asyncio.timeout(WRITE_TIMEOUT_S),
            tool_transaction(
                self._database, scope.business_id, WRITE_TIMEOUT_S, read_only=False
            ) as connection,
        ):
            decision = await self._decide(
                connection, scope, action_id, request.confirm_token, trace_id, attempt, call
            )
            if scope.seen - {scope.business_id}:  # pragma: no cover (RLS and the clauses)
                raise RuntimeError("a confirm read a row of another business")
            await check_keys_now(connection)
            attempt.committing = True  # the body is done; what fails from here is the COMMIT
        return decision

    async def _decide(
        self,
        connection: AsyncConnection,
        scope: ToolScope,
        action_id: UUID,
        token: ConfirmToken,
        trace_id: str,
        attempt: _Attempt,
        call: Callable[[str], CallRecord],
    ) -> _Decision:
        rows = await scope.fetch(connection, LOCK_ACTION, {"action_id": action_id})
        if len(rows) != 1:
            return _Decision(ConfirmOutcome.NOT_FOUND, audited=False)  # no tool: logged only
        row = rows[0]
        attempt.tool = ToolName(row.tool)

        async def audited(outcome: str, response: ConfirmResponse | None = None) -> _Decision:
            await insert_audit(connection, entry_of(call(outcome)))
            return _Decision(outcome, audited=True, response=response)

        if not token_matches(token, row.confirm_token_sha256):
            return await audited(ConfirmOutcome.WRONG_TOKEN)
        if row.status != ActionStatus.PENDING:
            return await audited(ConfirmOutcome.REPLAYED, stored_response(row))
        if row.expired:
            return await audited(ConfirmOutcome.EXPIRED)
        params = {"action_id": action_id}
        marked = await scope.fetch(connection, MARK_CONFIRMED, params | {"trace_id": trace_id})
        if len(marked) != 1:  # pragma: no cover (the row is locked and pending)
            raise RuntimeError("the locked pending action could not be marked confirmed")
        action = ConfirmedAction(
            id=row.id,
            tool=attempt.tool,
            arguments=row.arguments,
            customer_ref=row.customer_ref,
            confirmed_at=marked[0].confirmed_at,
            confirm_trace_id=marked[0].confirm_trace_id,
        )
        outcome = await execute(connection, action, scope)
        response = self._response(action, outcome)
        finished = await connection.execute(
            FINISH,
            {
                "business_id": scope.business_id,
                "customer_ref": scope.customer_ref,
                "action_id": action_id,
                "status": response.status.value,
                "result": json.dumps(stored_result(response), sort_keys=True),
                "failure_reason": None if isinstance(outcome, Executed) else outcome.reason.value,
            },
        )
        if finished.rowcount != 1:  # pragma: no cover (the row is locked, pending, confirmed)
            raise RuntimeError("the confirmed action could not be marked finished")
        return await audited(audit_outcome(response), response)

    @staticmethod
    def _response(action: ConfirmedAction, outcome: WriteOutcome) -> ConfirmResponse:
        if isinstance(outcome, Executed):
            return executed_response(action.id, action.tool, outcome.output)
        return failed_response(action.id, action.tool, outcome.reason)


def entry_of(record: CallRecord) -> AuditEntry:
    """The audit row of a call whose action (and so its tool) was read."""
    entry = record.audit_entry()
    if entry is None:  # pragma: no cover (called only once the tool is known)
        raise RuntimeError("a known action's audit row needs its tool")
    return entry


async def check_keys_now(connection: AsyncConnection) -> None:
    """`CHECK_KEYS_NOW`; a violation is `BrokenWrite` (no database message chained to it)."""
    try:
        await connection.execute(CHECK_KEYS_NOW)
    except IntegrityError:
        broken = True
    else:
        broken = False
    if broken:
        raise BrokenWrite("a write row's action key does not hold")


def audit_outcome(response: ConfirmResponse) -> str:
    """`executed`, or the failure's reason (`slot_unavailable`, `booking_changed`, ...)."""
    if response.failure_reason is None:
        return ConfirmOutcome.EXECUTED.value
    return response.failure_reason.value


def stored_result(response: ConfirmResponse) -> dict[str, object]:
    """`pending_actions.result`: the tool's output, or the failure's reason."""
    if response.result is not None:
        return response.result.model_dump(mode="json")
    return {"failure_reason": None if response.failure_reason is None else response.failure_reason}
