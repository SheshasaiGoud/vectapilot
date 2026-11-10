"""M5 PR 3: the confirm service's decisions and `execute`'s guards, without a database.

The real transactions against Postgres are in `test_confirm_db.py`; here each rule is pinned on
its own with a scripted connection (`fakes.ScriptedSource`):

- **W8, W9:** the confirm transaction is READ COMMITTED, asked for explicitly (not the read-only
  snapshot, not the session's default), and starts with `statement_timeout` and `lock_timeout`
  below the write budget; its body ends with `SET CONSTRAINTS ALL IMMEDIATE`.
- **W6's order:** an unknown action, a wrong token and an expired action are the same refusal and
  cost the same one transaction; a wrong token never reveals the stored result; a finished action
  returns it whatever its expiry, every failure reason with its own sentence, and nothing is
  marked or executed for it. The lock binds the request's customer and the business.
- **The success path:** the action is marked with its result or reason, and its one audit row
  (`kind = write`) commits in the same transaction; its `tool_call` record says so.
- **W12:** a refusal of a known action and a repeat are audited inside the confirm's transaction,
  a rollback and an unknown outcome after it; an unknown action, which has no tool, is logged
  only; every call leaves one `tool_call` record, a cancelled one too, and no record holds the
  token. A failed audit-after is logged with the error's type only.
- **Failures (W9):** any timeout (the backstop, `statement_timeout`, `lock_timeout`) and any error
  at the COMMIT are `OutcomeUnknown`; only another error before the COMMIT is `ConfirmFailed`; a
  deferred key that does not hold is `BrokenWrite`, a bug, never an unknown outcome.
- **`execute` (W2, W5):** refuses an action without `confirmed_at` and `confirm_trace_id`, or for
  another customer, before any statement; a disabled tool or arguments that no longer fit are
  typed failures; a booking id is derived from its action id; only the one-active-booking index
  inside the SAVEPOINT is `slot_unavailable`, any other integrity error propagates.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final, cast

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.actions.fakes import (
    ACTION,
    OTHER_TOKEN,
    TOKEN,
    ScriptedResult,
    ScriptedSource,
    action_row,
)
from tests.tools.core import PAWS, TRACE, RowsConnection, config_for
from vectapilot_api.actions import audit as audit_module
from vectapilot_api.actions.audit import AUDIT_FAILED
from vectapilot_api.actions.confirm import (
    BrokenWrite,
    ConfirmOutcome,
    ConfirmService,
    failed_response,
)
from vectapilot_api.actions.tokens import ConfirmToken
from vectapilot_api.actions.types import (
    EXECUTED_MESSAGE,
    FAILURE_MESSAGES,
    ActionNotFound,
    ActionStatus,
    ConfirmFailed,
    ConfirmRequest,
    OutcomeUnknown,
)
from vectapilot_api.log import JsonFormatter
from vectapilot_api.tools.book import BookOutput
from vectapilot_api.tools.config import RECORD_ID_MAX, RECORD_ID_PATTERN, ToolName
from vectapilot_api.tools.execute import (
    ONE_ACTIVE_PER_SLOT,
    ConfirmedAction,
    Executed,
    Failed,
    UnconfirmedAction,
    WriteOutcome,
    booking_id_for,
    execute,
    slot_taken,
)
from vectapilot_api.tools.executor import TOOL_CALL, server_timeouts
from vectapilot_api.tools.spec import WRITE_TIMEOUT_S, ToolScope
from vectapilot_api.tools.types import WriteFailure

pytestmark = pytest.mark.anyio

BUSINESS: Final = PAWS.config.business_id
CONFIG: Final = PAWS.config


def request(token: str = TOKEN, customer: str = "cust-001") -> ConfirmRequest:
    return ConfirmRequest(customer_ref=customer, confirm_token=ConfirmToken(token))


def service(source: ScriptedSource) -> ConfirmService:
    return ConfirmService(source, {BUSINESS: CONFIG})


async def confirm(source: ScriptedSource, **kwargs: Any) -> Any:
    return await service(source).confirm(BUSINESS, ACTION, request(**kwargs), trace_id=TRACE)


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, TOOL_CALL) for r in caplog.records if r.getMessage() == TOOL_CALL]


def rendered(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(JsonFormatter().format(record) for record in caplog.records)


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)


# ── The transaction ──────────────────────────────────────────────────────────────────────────


async def test_the_confirm_runs_read_committed_with_server_timeouts_below_its_budget() -> None:
    source = ScriptedSource(script=[[]])

    with pytest.raises(ActionNotFound):
        await confirm(source)

    # Not the read-only snapshot, and READ COMMITTED asked for, never the session's default.
    assert source.transactions[0] == (BUSINESS, False, True)
    sql, params = source.statements[0]
    assert "statement_timeout" in sql
    assert (params["statement"], params["lock"]) == server_timeouts(WRITE_TIMEOUT_S)
    assert source.statements[source.main_statements - 1][0] == "SET CONSTRAINTS ALL IMMEDIATE"


async def test_the_lock_binds_the_requests_customer_and_the_business() -> None:
    source = ScriptedSource(script=[[]])

    with pytest.raises(ActionNotFound):
        await confirm(source, customer="cust-007")

    [params] = source.params_of("FROM pending_actions")
    assert (params["business_id"], params["customer_ref"], params["action_id"]) == (
        BUSINESS,
        "cust-007",
        ACTION,
    )


async def test_a_business_without_tools_has_nothing_to_confirm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = ScriptedSource()

    with pytest.raises(ActionNotFound):
        await ConfirmService(source, {}).confirm(BUSINESS, ACTION, request(), trace_id=TRACE)

    assert source.transactions == []
    assert [(r["outcome"], r["audited"], r["tool"]) for r in records(caplog)] == [
        ("not_found", False, None)
    ]


# ── W6's order ───────────────────────────────────────────────────────────────────────────────


async def test_an_unknown_action_is_refused_and_logged_never_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = ScriptedSource(script=[[]])

    with pytest.raises(ActionNotFound):
        await confirm(source)

    assert source.audits() == [] and len(source.transactions) == 1
    [record] = records(caplog)
    assert (record["outcome"], record["audited"], record["phase"]) == (
        "not_found",
        False,
        "confirm",
    )
    assert record["action_id"] == str(ACTION) and record["trace_id"] == TRACE


@pytest.mark.parametrize(
    ("row", "outcome"),
    [
        pytest.param({"confirm_token_sha256": "0" * 64}, "wrong_token", id="wrong token"),
        pytest.param(
            {"status": "executed", "result": {"booking_id": "B-1", "slot_id": "S-09"}},
            "wrong_token",
            id="wrong token, executed",
        ),
        pytest.param({"expired": True}, "expired", id="expired"),
    ],
)
async def test_a_wrong_token_or_expiry_is_refused_and_audited_in_the_confirms_transaction(
    row: dict[str, object], outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    token = OTHER_TOKEN if outcome == "wrong_token" and "result" in row else TOKEN
    source = ScriptedSource(script=[[action_row(**row)], []])  # the lock, the audit insert

    with pytest.raises(ActionNotFound):
        await confirm(source, token=token)

    assert not source.sent("SET confirmed_at") and not source.sent("INSERT INTO sandbox_")
    [audit] = source.main_audits()
    assert source.audits() == [audit] and len(source.transactions) == 1
    assert (audit["outcome"], audit["phase"], audit["kind"], audit["tool"]) == (
        outcome,
        "confirm",
        "write",
        "book",
    )
    assert audit["action_id"] == ACTION
    assert [(r["outcome"], r["audited"], r["kind"]) for r in records(caplog)] == [
        (outcome, True, "write")
    ]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param([[]], id="unknown"),
        pytest.param([[action_row(confirm_token_sha256="0" * 64)], []], id="wrong token"),
        pytest.param([[action_row(expired=True)], []], id="expired"),
    ],
)
async def test_every_refusal_costs_the_same_one_transaction(
    script: list[list[Any] | BaseException],
) -> None:
    """No second transaction tells an owned id from an unknown one (the one INSERT a known
    action's refusal adds inside it is a known gap, M7's red-team results)."""
    source = ScriptedSource(script=script)

    with pytest.raises(ActionNotFound):
        await confirm(source)

    assert len(source.transactions) == 1 and source.script == []


EXECUTED_ROW: Final = {"status": "executed", "result": {"booking_id": "B-1", "slot_id": "S-09"}}


def failed_row(reason: WriteFailure) -> dict[str, object]:
    return {
        "status": "failed",
        "result": {"failure_reason": reason.value},
        "failure_reason": reason,
    }


@pytest.mark.parametrize(
    ("row", "expired"),
    [
        pytest.param(EXECUTED_ROW, False, id="executed"),
        pytest.param(EXECUTED_ROW, True, id="executed, expired"),
        *(pytest.param(failed_row(r), True, id=f"failed {r.value}") for r in WriteFailure),
    ],
)
async def test_a_finished_action_returns_its_stored_result_whatever_its_expiry(
    row: dict[str, Any], expired: bool, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row(**row, expired=expired)], []])

    response = await confirm(source)

    assert response.status == row["status"] and response.action_id == ACTION
    if row["status"] == "executed":
        assert response.result is not None and response.result.model_dump() == row["result"]
        assert response.message == EXECUTED_MESSAGE
    else:
        reason = WriteFailure(row["failure_reason"])
        assert response.failure_reason is reason
        assert response.message == FAILURE_MESSAGES[reason]
    assert not source.sent("SET confirmed_at")
    assert [a["outcome"] for a in source.main_audits()] == ["replayed"]
    assert len(source.transactions) == 1
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("replayed", True)]


@pytest.mark.parametrize("reason", list(WriteFailure))
def test_every_failure_has_its_own_sentence(reason: WriteFailure) -> None:
    response = failed_response(ACTION, ToolName.BOOK, reason)

    assert (response.failure_reason, response.message) == (reason, FAILURE_MESSAGES[reason])


# ── The success path ─────────────────────────────────────────────────────────────────────────


def marked() -> list[Any]:
    """`MARK_CONFIRMED`'s returned row."""
    return [
        SimpleNamespace(
            business_id=BUSINESS, confirmed_at=datetime.now(UTC), confirm_trace_id=TRACE
        )
    ]


def executing(monkeypatch: pytest.MonkeyPatch, outcome: WriteOutcome) -> list[ConfirmedAction]:
    """Replace the write with one that returns `outcome`; the actions it was handed."""
    handed: list[ConfirmedAction] = []

    async def fake(connection: object, action: ConfirmedAction, scope: ToolScope) -> WriteOutcome:
        handed.append(action)
        return outcome

    monkeypatch.setattr("vectapilot_api.actions.confirm.execute", fake)
    return handed


@pytest.mark.parametrize(
    ("outcome", "stored", "reason"),
    [
        pytest.param(
            Executed(BookOutput(booking_id="B-1", slot_id="S-09")),
            {"booking_id": "B-1", "slot_id": "S-09"},
            None,
            id="executed",
        ),
        pytest.param(
            Failed(WriteFailure.BOOKING_CHANGED),
            {"failure_reason": "booking_changed"},
            WriteFailure.BOOKING_CHANGED,
            id="failed",
        ),
    ],
)
async def test_a_confirmed_action_is_marked_and_audited_in_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: WriteOutcome,
    stored: dict[str, str],
    reason: WriteFailure | None,
) -> None:
    handed = executing(monkeypatch, outcome)
    # The lock, the mark, FINISH (one row), the audit insert; SET CONSTRAINTS passes through.
    source = ScriptedSource(script=[[action_row()], marked(), [SimpleNamespace()], []])

    response = await confirm(source)

    [action] = handed
    assert (action.id, action.confirm_trace_id, action.customer_ref) == (ACTION, TRACE, "cust-001")
    [finish] = source.params_of("SET status = :status")
    assert finish["status"] == ("executed" if reason is None else "failed")
    assert json.loads(str(finish["result"])) == stored
    assert finish["failure_reason"] == (None if reason is None else reason.value)
    expected = "executed" if reason is None else reason.value
    [audit] = source.main_audits()
    assert source.audits() == [audit] and len(source.transactions) == 1
    assert (audit["outcome"], audit["kind"], audit["phase"]) == (expected, "write", "confirm")
    assert [(r["outcome"], r["audited"], r["kind"]) for r in records(caplog)] == [
        (expected, True, "write")
    ]
    assert response.message == (EXECUTED_MESSAGE if reason is None else FAILURE_MESSAGES[reason])
    assert source.statements[source.main_statements - 1][0] == "SET CONSTRAINTS ALL IMMEDIATE"


def violation(constraint: str) -> IntegrityError:
    class Cause(Exception):
        def __init__(self, name: str) -> None:
            self.constraint_name = name

    orig = Exception("DETAIL: Key (business_id, action_id, customer_ref)=(x, y, cust-009)")
    orig.__cause__ = Cause(constraint)
    return IntegrityError("INSERT", {}, orig)


async def test_a_deferred_key_that_does_not_hold_is_a_bug_never_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executing(monkeypatch, Executed(BookOutput(booking_id="B-1", slot_id="S-09")))
    source = ScriptedSource(
        script=[[action_row()], marked(), [SimpleNamespace()], []],
        constraints_error=violation("sandbox_bookings_action_fkey"),
    )

    with pytest.raises(BrokenWrite) as caught:
        await confirm(source)

    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is False
    assert caught.value.__context__ is None  # raised outside the `except`
    assert [a["outcome"] for a in source.audits()[1:]] == ["rolled_back"]  # after the rollback
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("error", True)]
    assert "cust-009" not in rendered(caplog)


# ── Failures ─────────────────────────────────────────────────────────────────────────────────


def operational() -> OperationalError:
    return OperationalError("SELECT 1", {}, OSError("connection lost"))


class ServerError(Exception):
    """asyncpg's error as SQLAlchemy wraps it: `sqlstate` on `orig`."""

    def __init__(self, sqlstate: str) -> None:
        self.sqlstate = sqlstate


def server_timeout(sqlstate: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, ServerError(sqlstate))


@pytest.mark.parametrize("error", [OSError("refused"), operational(), server_timeout("40P01")])
async def test_an_error_before_the_commit_that_is_no_timeout_is_a_rollback(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row()], error])  # the MARK fails

    with pytest.raises(ConfirmFailed) as caught:
        await confirm(source)

    assert caught.value.__cause__ is None and str(caught.value) == ""
    [audit] = source.audits()
    assert (audit["outcome"], audit["tool"]) == ("rolled_back", "book")
    assert [r["outcome"] for r in records(caplog)] == ["rolled_back"]


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeoutError(), id="backstop"),
        pytest.param(server_timeout("57014"), id="statement_timeout"),
        pytest.param(server_timeout("55P03"), id="lock_timeout"),
    ],
)
async def test_a_timeout_before_the_commit_is_an_unknown_outcome_never_not_written(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    """W9: the attempt may have waited behind another confirm of the same action that is about
    to commit; "nothing was written" would be true of this attempt only."""
    source = ScriptedSource(script=[[action_row()], error])  # the MARK times out

    with pytest.raises(OutcomeUnknown) as caught:
        await confirm(source)

    assert caught.value.__cause__ is None and str(caught.value) == ""
    assert [a["outcome"] for a in source.audits()] == ["outcome_unknown"]
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("outcome_unknown", True)]


@pytest.mark.parametrize(
    ("error", "failure"),
    [(OSError("refused"), ConfirmFailed), (server_timeout("55P03"), OutcomeUnknown)],
)
async def test_an_error_before_the_action_was_read_is_logged_only(
    error: BaseException, failure: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    """A lock wait on the action itself times out before its tool is known: no audit row
    (`tool_audit.tool` is required), only the `tool_call` record."""
    source = ScriptedSource(script=[error])

    with pytest.raises(failure):
        await confirm(source)

    assert source.audits() == []
    [record] = records(caplog)
    assert (record["audited"], record["tool"]) == (False, None)


@pytest.mark.parametrize("error", [TimeoutError(), operational(), OSError("reset")])
async def test_an_error_at_the_commit_is_an_unknown_outcome(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row(**EXECUTED_ROW)], []], exit_error=error)

    with pytest.raises(OutcomeUnknown):
        await confirm(source)

    assert [a["outcome"] for a in source.audits()[1:]] == ["outcome_unknown"]
    assert [r["outcome"] for r in records(caplog)] == ["outcome_unknown"]


async def test_a_hung_confirm_is_ended_by_the_backstop_as_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("vectapilot_api.actions.confirm.WRITE_TIMEOUT_S", 0.01)

    source = ScriptedSource(script=[[action_row()], marked()])

    async def hang(*args: object, **kwargs: object) -> Any:
        await asyncio.sleep(10)

    monkeypatch.setattr("vectapilot_api.actions.confirm.execute", hang)

    with pytest.raises(OutcomeUnknown):
        await confirm(source)

    assert [a["outcome"] for a in source.audits()] == ["outcome_unknown"]


@pytest.mark.parametrize(
    "after_error",
    [
        pytest.param(
            OperationalError(
                "INSERT", {}, Exception("DETAIL: Key (customer_ref)=(cust-001) leaked")
            ),
            id="database",
        ),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_a_failed_audit_after_is_logged_by_type_and_the_answer_stands(
    after_error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row()], OSError("x")], after_error=after_error)

    with pytest.raises(ConfirmFailed):
        await confirm(source)

    assert len(source.transactions) == 2  # the main one, then the audit-after that failed
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("rolled_back", False)]
    [warning] = [r for r in caplog.records if r.getMessage() == AUDIT_FAILED]
    assert (warning.levelno, warning.__dict__["reason"]) == (
        logging.WARNING,
        type(after_error).__name__,
    )
    text = rendered(caplog)
    assert "cust-001)" not in text and "leaked" not in text


def test_the_audit_outcome_names_are_the_confirms() -> None:
    assert ConfirmOutcome.ROLLED_BACK.value == audit_module.ROLLED_BACK
    assert ConfirmOutcome.OUTCOME_UNKNOWN.value == audit_module.OUTCOME_UNKNOWN


async def test_a_cancelled_confirm_is_logged_and_re_raised_never_audited(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row()], marked()])

    async def cancelled(*args: object, **kwargs: object) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr("vectapilot_api.actions.confirm.execute", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await confirm(source)

    assert source.audits() == []
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("cancelled", False)]


async def test_a_bug_is_re_raised_and_audited_as_a_rollback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = ScriptedSource(script=[[action_row()], marked()])

    async def bug(*args: object, **kwargs: object) -> Any:
        raise KeyError("a bug")

    monkeypatch.setattr("vectapilot_api.actions.confirm.execute", bug)

    with pytest.raises(KeyError):
        await confirm(source)

    assert [a["outcome"] for a in source.audits()] == ["rolled_back"]
    assert [(r["outcome"], r["audited"]) for r in records(caplog)] == [("error", True)]


async def test_no_record_holds_the_token(caplog: pytest.LogCaptureFixture) -> None:
    scripts: list[list[list[Any] | BaseException]] = [
        [[]],
        [[action_row()], OSError("x")],
        [[action_row(expired=True)], []],
    ]
    for script in scripts:
        with pytest.raises((ActionNotFound, ConfirmFailed)):
            await confirm(ScriptedSource(script=script))

    text = rendered(caplog)
    assert TOKEN not in text and action_row().confirm_token_sha256 not in text
    assert records(caplog) and all(set(r) >= {"tool", "outcome"} for r in records(caplog))


def test_every_confirm_outcome_is_a_code() -> None:
    assert all(re.fullmatch(r"^[a-z][a-z0-9_]{1,39}$", o) for o in ConfirmOutcome)


# ── execute's guards ─────────────────────────────────────────────────────────────────────────


def confirmed(**changes: Any) -> ConfirmedAction:
    action: dict[str, Any] = {
        "id": ACTION,
        "tool": ToolName.BOOK,
        "arguments": {"slot_id": "S-09"},
        "customer_ref": "cust-001",
        "confirmed_at": datetime.now(UTC),
        "confirm_trace_id": TRACE,
    }
    return ConfirmedAction(**(action | changes))


def scope(**changes: Any) -> ToolScope:
    return ToolScope(BUSINESS, "cust-001", config_for(BUSINESS, **changes))


@pytest.mark.parametrize(
    "changes",
    [{"confirmed_at": None}, {"confirm_trace_id": None}, {"customer_ref": "cust-002"}],
)
async def test_execute_refuses_an_unconfirmed_or_foreign_action_before_any_statement(
    changes: dict[str, object],
) -> None:
    connection = RowsConnection()

    with pytest.raises(UnconfirmedAction):
        await execute(connection.connection(), confirmed(**changes), scope())

    assert connection.calls == []


async def test_a_tool_disabled_since_the_proposal_is_not_enabled() -> None:
    connection = RowsConnection()
    without_book = scope(enabled_tools=["check_slots", "order_status"])

    outcome = await execute(connection.connection(), confirmed(), without_book)

    assert outcome == Failed(WriteFailure.NOT_ENABLED) and connection.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ToolName.BOOK, {"slot_id": "not an id"}),
        (ToolName.BOOK, {"slot_id": "S-09", "customer_ref": "cust-002"}),
        (ToolName.CREATE_TICKET, {"category": "no_such_category", "summary": "Hello"}),
    ],
)
async def test_stored_arguments_that_no_longer_fit_are_invalid_arguments(
    tool: ToolName, arguments: dict[str, str]
) -> None:
    connection = RowsConnection()

    outcome = await execute(
        connection.connection(), confirmed(tool=tool, arguments=arguments), scope()
    )

    assert outcome == Failed(WriteFailure.INVALID_ARGUMENTS) and connection.calls == []


def test_a_booking_id_is_a_record_id_derived_from_its_action() -> None:
    booking = booking_id_for(ACTION)

    assert booking == "B-5F0C0B1E11114222" == booking_id_for(ACTION)
    assert re.fullmatch(RECORD_ID_PATTERN, booking) and len(booking) <= RECORD_ID_MAX


def test_only_the_one_active_booking_index_means_the_slot_was_taken() -> None:
    class Cause(Exception):
        def __init__(self, constraint: str) -> None:
            self.constraint_name = constraint

    def violation(constraint: str) -> IntegrityError:
        orig = Exception("unique")
        orig.__cause__ = Cause(constraint)
        return IntegrityError("INSERT", {}, orig)

    assert slot_taken(violation(ONE_ACTIVE_PER_SLOT))
    assert not slot_taken(violation("sandbox_bookings_pkey"))
    assert not slot_taken(IntegrityError("INSERT", {}, Exception("no cause")))


def test_action_status_values_are_the_responses() -> None:
    assert {ActionStatus.EXECUTED, ActionStatus.FAILED} < set(ActionStatus)


@dataclass
class SavepointConnection:
    """For `execute`'s write: `begin_nested` is a no-op, the booking insert raises `error`, the
    guarded retire matches one row, and any other statement returns the next of `rows`."""

    rows: list[list[Any]]
    error: BaseException
    sent: list[str] = field(default_factory=list)

    @asynccontextmanager
    async def begin_nested(self) -> AsyncIterator[None]:
        yield

    async def execute(self, statement: object, params: Mapping[str, object] | None = None) -> Any:
        sql = str(statement)
        self.sent.append(sql)
        if sql.startswith("INSERT INTO sandbox_bookings"):
            raise self.error
        if sql.startswith("UPDATE sandbox_bookings"):
            return ScriptedResult([SimpleNamespace()])
        return ScriptedResult(self.rows.pop(0))


async def no_refusal(*args: object, **kwargs: object) -> None:
    """A tool's `validate` that finds nothing wrong (the slot is free, the booking movable)."""


def booking_row() -> SimpleNamespace:
    """`LOCK_OWN_ACTIVE_BOOKING`'s row: the customer's booking, starting after the sandbox day."""
    starts = datetime.combine(CONFIG.sandbox_date + timedelta(days=1), datetime.min.time(), UTC)
    return SimpleNamespace(business_id=BUSINESS, booking_id="B-101", id="S-02", starts_at=starts)


WRITES: Final = [
    pytest.param(ToolName.BOOK, {"slot_id": "S-09"}, [], id="book"),
    pytest.param(
        ToolName.RESCHEDULE,
        {"booking_id": "B-101", "new_slot_id": "S-10"},
        [[booking_row()]],
        id="reschedule",
    ),
]


@pytest.mark.parametrize(("tool", "arguments", "rows"), WRITES)
async def test_only_the_one_active_booking_index_in_the_savepoint_is_slot_unavailable(
    monkeypatch: pytest.MonkeyPatch, tool: ToolName, arguments: dict[str, str], rows: list[Any]
) -> None:
    monkeypatch.setattr("vectapilot_api.tools.book.validate", no_refusal)
    monkeypatch.setattr("vectapilot_api.tools.reschedule.validate", no_refusal)
    action = confirmed(tool=tool, arguments=arguments)

    taken = SavepointConnection(list(rows), violation(ONE_ACTIVE_PER_SLOT))
    outcome = await execute(cast(AsyncConnection, taken), action, scope())
    assert outcome == Failed(WriteFailure.SLOT_UNAVAILABLE)

    # A second execution of one action collides on the primary key (or its action key): a bug,
    # never told to the customer as a lost slot.
    collided = SavepointConnection(list(rows), violation("sandbox_bookings_pkey"))
    with pytest.raises(IntegrityError):
        await execute(cast(AsyncConnection, collided), action, scope())
    assert any(sql.startswith("INSERT INTO sandbox_bookings") for sql in collided.sent)
