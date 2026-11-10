"""Audit rows and log records for pending actions and confirms (W12, ADR 0007: metadata only).

The same `tool_audit` row and `tool_call` log record as the executor's (`tools.executor`): tool,
kind, phase, outcome, duration, trace id and action id. Never the arguments, the result, the
token or its hash.

- `insert_audit` runs on a connection the caller holds: the store's insert and the confirm's
  `executed` or `failed` mark commit together with their audit row, so neither exists without it.
- `audit_after` is the short transaction of its own for what has no transaction left to join
  (W12): a rollback and an unknown outcome. It never raises: if the insert fails, the warning is
  logged with the error's type only and the caller's answer stands (the outcome is already
  decided). A refusal and a repeated confirm commit their row with the confirm's own transaction.
- `unknown_outcome` decides between the two 503s (W9): a timeout, or any failure while
  committing, leaves the outcome unknown; only another failure before the COMMIT was sent is a
  known rollback of this attempt.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.executor import (
    AUDIT_INSERT,
    AUDIT_TIMEOUT_S,
    TOOL_CALL,
    AuditEntry,
    AuditPhase,
    TransactionSource,
    failure_of,
    tool_transaction,
)
from vectapilot_api.tools.types import ToolErrorCode, ToolKind

logger = logging.getLogger("vectapilot_api.tools")
# The executor's logger, pinned at INFO there too: a quieter log level never drops a record.
logger.setLevel(logging.INFO)

AUDIT_FAILED: Final = "tool_audit_failed"
ROLLED_BACK: Final = "rolled_back"
"""This attempt's transaction rolled back before its COMMIT was sent: it changed nothing."""
OUTCOME_UNKNOWN: Final = "outcome_unknown"
"""A timeout or a failure while committing: the caller cannot know, so it retries (W9)."""


def unknown_outcome(error: BaseException, *, committing: bool) -> bool:
    """Whether `error` leaves the outcome unknown (W9): it happened while committing, or it is a
    timeout (the asyncio backstop, `statement_timeout` or `lock_timeout`). A timed-out attempt
    may have been waiting behind another confirm of the same action that is about to commit, so
    "nothing was written" would be true of the attempt only, never of the action."""
    return (
        committing or isinstance(error, TimeoutError) or failure_of(error) is ToolErrorCode.TIMEOUT
    )


async def insert_audit(connection: AsyncConnection, entry: AuditEntry) -> None:
    """Insert `entry` on `connection`, inside the caller's transaction."""
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


async def audit_after(source: TransactionSource, entry: AuditEntry) -> bool:
    """Insert `entry` in its own short transaction; False if that failed (never raised)."""
    try:
        async with (
            asyncio.timeout(AUDIT_TIMEOUT_S),
            tool_transaction(
                source, entry.business_id, AUDIT_TIMEOUT_S, read_only=False
            ) as connection,
        ):
            await insert_audit(connection, entry)
    except (TimeoutError, SQLAlchemyError, OSError) as error:
        logger.warning(
            AUDIT_FAILED, extra={"business_id": entry.business_id, "reason": type(error).__name__}
        )
        return False
    return True


@dataclass(frozen=True)
class CallRecord:
    """One call's metadata: an audit row's fields, with `tool` unknown (None) when a confirm names
    no action this business and customer have. Such a call is logged, never audited:
    `tool_audit.tool` is required, and an unknown id has no tool (see `audit_entry`)."""

    business_id: str
    tool: ToolName | None
    phase: AuditPhase
    outcome: str
    duration_ms: float
    trace_id: str | None
    action_id: UUID | None

    def audit_entry(self) -> AuditEntry | None:
        """The audit row for this call, or None when the tool is unknown."""
        if self.tool is None:
            return None
        return AuditEntry(
            business_id=self.business_id,
            tool=self.tool,
            kind=ToolKind.WRITE,
            phase=self.phase,
            outcome=self.outcome,
            duration_ms=self.duration_ms,
            trace_id=self.trace_id,
            action_id=self.action_id,
        )


def log_call(call: CallRecord, *, audited: bool) -> None:
    """The one `tool_call` record of a call (W12); every pending-action call is a write's."""
    record = {
        "business_id": call.business_id,
        "tool": None if call.tool is None else call.tool.value,
        "kind": ToolKind.WRITE.value,
        "phase": call.phase.value,
        "outcome": call.outcome,
        "duration_ms": call.duration_ms,
        "trace_id": call.trace_id,
        "action_id": None if call.action_id is None else str(call.action_id),
        "audited": audited,
    }
    logger.info(TOOL_CALL, extra={TOOL_CALL: record})
