"""Storing a proposal as a pending action (decided D3 (a), M5 PR 3; W2, W6, W12).

`PendingActionStore.create(proposal, context)` turns `propose`'s `WriteProposal` into a row of
`pending_actions` and returns a `PendingAction` with the one copy of its confirm token. PR 4's
`/v1/agent` calls it and puts the token in its HTTP response; nothing else ever sees the token.

- **The arguments are validated again** against the business's input model (the executor's
  `business_input_model`) and stored as that model's JSON, so the row holds exactly what the
  summary showed and what confirm will run. If validating changed them (a normalizing validator
  added later), the proposal is refused: its summary was rendered from other values. The
  customer and the business come from the injected `ToolContext`, never from the proposal (W7).
- **Only `main.py` imports this store** in `src/` (PR 4 adds its agent service by name), and the
  MCP server imports nothing of `vectapilot_api.actions` (both tested,
  `tests/actions/test_write_boundary.py`), so a token can never reach an MCP result.
- **Only the token's hash is stored** (`tokens.token_sha256`).
- **The database sets the expiry** (W6): `now()` plus `lifetime_s` in the INSERT itself, so no
  application clock decides how long a proposal lives. Settings bounds the lifetime to more than
  zero and at most one hour (`Settings.action_lifetime_seconds`), migration 0006's CHECK, so a
  typo stops startup instead of failing every proposal with SQLSTATE 23514; `__init__` checks it
  again for a caller that builds a store by hand.
- **One transaction** inserts the action and its audit row (`propose`, `stored`, with the new
  action id), so no pending action exists without its audit row (W12).
- **One `tool_call` record per call** (W12), failures included, so a proposal that failed to be
  stored is told apart from one that never happened: `stored` (audited); `rolled_back` or
  `outcome_unknown` for a database or network error, by `audit.unknown_outcome`; `cancelled`;
  `error` for a bug. A failure is logged only, never audited: no action id exists to name.

A caller's bug (a context for a business without tools, a disabled or read tool, arguments that
do not validate) raises `ValueError` before anything is sent. Database errors propagate after
their record: PR 4's service turns any exception into a hand-off.
"""

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from typing import Final
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from vectapilot_api.actions.audit import (
    OUTCOME_UNKNOWN,
    ROLLED_BACK,
    CallRecord,
    insert_audit,
    log_call,
    unknown_outcome,
)
from vectapilot_api.actions.tokens import new_confirm_token, token_sha256
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.tools.config import ToolsConfig
from vectapilot_api.tools.executor import (
    CANCELLED,
    ERROR,
    AuditPhase,
    TransactionSource,
    tool_transaction,
)
from vectapilot_api.tools.registry import REGISTRY
from vectapilot_api.tools.spec import WRITE_TIMEOUT_S, WriteTool, business_input_model
from vectapilot_api.tools.types import ToolContext, WriteProposal

MAX_LIFETIME_S: Final = 3600
"""Migration 0006's `expires_at <= created_at + interval '1 hour'`."""
STORED: Final = "stored"
"""The audit outcome of a stored pending action."""

INSERT_ACTION: Final = text(
    "INSERT INTO pending_actions (business_id, tool, arguments, customer_ref,"
    " confirm_token_sha256, status, trace_id, expires_at)"
    " VALUES (:business_id, :tool, CAST(:arguments AS jsonb), :customer_ref, :token_sha256,"
    " 'pending', :trace_id, now() + make_interval(secs => CAST(:lifetime_s AS double precision)))"
    " RETURNING business_id, id, expires_at"
)
"""`expires_at` from the database's clock; the id from its default (the app role cannot choose
one: migration 0006's column grants)."""


def check_lifetime(lifetime_s: int) -> int:
    """`lifetime_s` if it is more than zero and at most an hour, else `ValueError`."""
    if not 0 < lifetime_s <= MAX_LIFETIME_S:
        raise ValueError("a pending action lives more than 0 and at most 3600 seconds")
    return lifetime_s


class PendingActionStore:
    """Stores proposals for the businesses in `configs` (each business's `tools.json`)."""

    def __init__(
        self,
        database: TransactionSource,
        configs: Mapping[str, ToolsConfig],
        *,
        lifetime_s: int,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._database = database
        self._configs = configs
        self._lifetime_s = check_lifetime(lifetime_s)
        self._clock = clock

    def _arguments(self, proposal: WriteProposal, context: ToolContext) -> str:
        """The proposal's arguments, validated for the context's business, as JSON."""
        config = self._configs.get(context.business_id)
        if config is None:
            raise ValueError("the context's business has no tools")
        if proposal.tool not in config.enabled_tools:
            raise ValueError("the proposal's tool is not enabled for the context's business")
        spec = REGISTRY[proposal.tool]
        if not isinstance(spec, WriteTool):  # pragma: no cover (WriteProposal refuses a read)
            raise ValueError("only a write tool's proposal is stored")
        try:
            validated = business_input_model(spec, config).model_validate(dict(proposal.arguments))
        except ValidationError:
            raise ValueError("the proposal's arguments do not fit its tool") from None
        stored = validated.model_dump(mode="json")
        if stored != dict(proposal.arguments):
            raise ValueError("the proposal's arguments changed when validated again")
        return json.dumps(stored, ensure_ascii=False, sort_keys=True)

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction:
        """Store `proposal` for `context`'s business and customer; the action and its token."""
        arguments = self._arguments(proposal, context)
        token = new_confirm_token()
        started = self._clock()
        committing = False

        def call(outcome: str, action_id: UUID | None = None) -> CallRecord:
            return CallRecord(
                business_id=context.business_id,
                tool=proposal.tool,
                phase=AuditPhase.PROPOSE,
                outcome=outcome,
                duration_ms=max(0.0, (self._clock() - started) * 1000),
                trace_id=context.trace_id,
                action_id=action_id,
            )

        try:
            async with (
                asyncio.timeout(WRITE_TIMEOUT_S),
                tool_transaction(
                    self._database, context.business_id, WRITE_TIMEOUT_S, read_only=False
                ) as connection,
            ):
                row = (
                    await connection.execute(
                        INSERT_ACTION,
                        {
                            "business_id": context.business_id,
                            "tool": proposal.tool.value,
                            "arguments": arguments,
                            "customer_ref": context.customer_ref,
                            "token_sha256": token_sha256(token),
                            "trace_id": context.trace_id,
                            "lifetime_s": float(self._lifetime_s),
                        },
                    )
                ).one()
                if row.business_id != context.business_id:  # pragma: no cover (RLS, the clause)
                    raise RuntimeError("a pending action was stored for another business")
                stored = call(STORED, row.id)
                entry = stored.audit_entry()
                if entry is None:  # pragma: no cover (the tool is known here)
                    raise RuntimeError("a stored action's audit row needs its tool")
                await insert_audit(connection, entry)
                committing = True
        except asyncio.CancelledError:
            log_call(call(CANCELLED), audited=False)
            raise
        except (TimeoutError, SQLAlchemyError, OSError) as error:
            unknown = unknown_outcome(error, committing=committing)
            log_call(call(OUTCOME_UNKNOWN if unknown else ROLLED_BACK), audited=False)
            raise
        except Exception:
            log_call(call(ERROR), audited=False)
            raise
        log_call(stored, audited=True)
        return PendingAction(
            id=row.id,
            tool=proposal.tool,
            summary=proposal.summary,
            expires_at=row.expires_at,
            confirm_token=token,
        )
