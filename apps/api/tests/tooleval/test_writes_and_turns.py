"""The harness's per-scenario pieces without a database (`tooleval/writes.py`, `turns.py`): the
counts are SELECTs scoped to a business, the leak check stops a foreign row before the model sees
it, only a matching proposal is confirmed, and the sandbox check is exact. Unit; fakes only."""

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.agent.fakes import ScriptedStepModel, ScriptedTools, observed, proposed
from tests.tooleval.helpers import parsed, scenario
from tests.tooleval.results import row
from tests.tooleval.test_harness_units import bench_with
from tests.tools.core import PAWS
from vectapilot_api.actions.tokens import new_confirm_token
from vectapilot_api.actions.types import (
    ActionNotFound,
    ActionStatus,
    ConfirmFailed,
    ConfirmRequest,
    ConfirmResponse,
    OutcomeUnknown,
    PendingAction,
)
from vectapilot_api.agent.responses import (
    AgentRequest,
    ConfirmationResponse,
    HandoffReason,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.service import AgentRun
from vectapilot_api.agent.types import LoopLimits, StepModel, ToolRunner, TurnOutcome
from vectapilot_api.intents.policy import Priority
from vectapilot_api.tooleval import turns
from vectapilot_api.tooleval.scenarios import Scenario
from vectapilot_api.tooleval.turns import (
    CONFIRM_ATTEMPTS,
    Bench,
    Confirmed,
    CrossBusinessLeak,
    LeakCheckedTools,
    LeakLog,
    confirm_twice,
    expected_active,
    key_arguments,
    leak_checked,
    matches,
    run_scenario,
    sandbox_exact,
    trace_for,
)
from vectapilot_api.tooleval.writes import (
    ACTIVE_BOOKINGS,
    AUDIT_CALLS,
    STATEMENTS,
    TURN_PROPOSALS,
    WRITE_ROWS,
    StoredProposal,
    write_rows,
)
from vectapilot_api.tools.config import ToolName

TRACE: Final = "4bf92f3577b34da6a3ce929d0e0e4736"


def test_every_count_is_a_select_scoped_to_the_business() -> None:
    for statement in STATEMENTS:
        sql = " ".join(str(statement).split()).lower()
        assert sql.startswith("select"), sql
        assert re.search(r"\bbusiness_id\s*=\s*:b\b", sql), sql
        assert not re.search(r"\b(insert|update|delete|merge|truncate|alter|grant)\b", sql), sql


def test_trace_ids_are_w3c_and_derived_from_run_and_scenario() -> None:
    turn = trace_for("run-1", "s-1", "turn")
    assert re.fullmatch(r"[0-9a-f]{32}", turn)
    assert turn == trace_for("run-1", "s-1", "turn")
    assert turn != trace_for("run-2", "s-1", "turn") != trace_for("run-1", "s-1", "confirm-1-1")


# ── The leak check ───────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_foreign_row_raises_before_the_observation_is_returned() -> None:
    log = LeakLog()
    own = observed(ToolName.CHECK_SLOTS, business_ids=frozenset({"paws-and-co"}))
    foreign = observed(
        ToolName.ORDER_STATUS, business_ids=frozenset({"paws-and-co", "sbx-foreign"})
    )
    tools = LeakCheckedTools(ScriptedTools([own, foreign]), "paws-and-co", log)

    assert await tools.run(ToolName.CHECK_SLOTS, {}) is own
    with pytest.raises(CrossBusinessLeak) as raised:
        await tools.run(ToolName.ORDER_STATUS, {"order_id": "1042"})
    assert log.leaks == ["order_status returned rows of 1 other business(es)"]
    assert "sbx-foreign" not in str(raised.value)


@pytest.mark.anyio
async def test_a_foreign_proposal_is_stopped_too_and_the_wrapper_is_not_a_loop() -> None:
    log = LeakLog()
    inner = ScriptedTools([proposed()])  # its envelope names paws-and-co

    async def stand_in(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        await tools.propose(ToolName.BOOK, {"slot_id": "S-17"})
        raise AssertionError("unreachable: the proposal's envelope names another business")

    with pytest.raises(CrossBusinessLeak):
        await leak_checked(stand_in, "sunrise-clinic", log)(
            "TEST-ONLY", step_model=ScriptedStepModel([]), tools=inner, limits=LoopLimits(1)
        )
    assert len(log.leaks) == 1


# ── The proposal ─────────────────────────────────────────────────────────────────────────────

BOOKING: Final = parsed(
    scenario(
        "s-1",
        kind="confirmation_required",
        tool="book",
        arguments={"slot_id": "S-09"},
        confirm=True,
    )
)


def pending(action_id: UUID) -> PendingAction:
    return PendingAction(
        id=action_id,
        tool=ToolName.BOOK,
        summary="TEST-ONLY",
        expires_at=datetime(2026, 11, 16, tzinfo=UTC),
        confirm_token=new_confirm_token(),
    )


def offered(action_id: UUID, *, own: UUID | None = None) -> AgentRun:
    """The service's run of a stored proposal: the response offers `action_id`; the in-process
    action is `own` (default: the same action)."""
    response = ConfirmationResponse(
        trace_id=TRACE,
        action_id=action_id,
        tool=ToolName.BOOK,
        summary="TEST-ONLY summary",
        expires_at=datetime(2026, 11, 16, tzinfo=UTC),
        confirm_token=new_confirm_token(),
    )
    return AgentRun(response, action=pending(own or action_id))


RUNNABLE: Final = BOOKING.model_copy(update={"split": "dev"})
"""`BOOKING` as a frozen dataset holds it: `run_scenario` runs only scenarios with a split."""


def stored(action_id: UUID, tool: ToolName = ToolName.BOOK, **arguments: str) -> StoredProposal:
    return StoredProposal(str(action_id), tool, dict(arguments) or {"slot_id": "S-09"})


def test_only_one_stored_proposal_with_expect_s_tool_and_key_arguments_matches() -> None:
    action = uuid4()
    run = offered(action)
    assert matches(BOOKING, (stored(action),), run) is True
    assert matches(BOOKING, (stored(action, slot_id="S-11"),), run) is False
    assert matches(BOOKING, (stored(action, ToolName.CREATE_TICKET, category="other"),), run) is (
        False
    )
    assert matches(BOOKING, (stored(uuid4()),), run) is False  # not the action offered
    # The response offers the stored action, but the service's own action is another one.
    assert matches(BOOKING, (stored(action),), offered(action, own=uuid4())) is False
    no_action = AgentRun(offered(action).response)
    assert matches(BOOKING, (stored(action),), no_action) is False
    assert matches(BOOKING, (), run) is None
    assert matches(BOOKING, (stored(action), stored(uuid4())), run) is None
    reply = AgentRun(ReplyResponse(trace_id=TRACE, text="TEST-ONLY"))
    assert matches(BOOKING, (stored(action),), reply) is False


def test_key_arguments_never_carry_a_tickets_text() -> None:
    ticket = StoredProposal("a", ToolName.CREATE_TICKET, {"category": "other", "summary": "x y"})
    assert key_arguments(ticket) == {"category": "other"}


class FakeConfirm:
    """Records each confirm; plays `script` (statuses or exceptions) one per call. With a
    `barrier`, the first `barrier.parties` calls each wait for the others before answering: only
    calls that are in flight together can get past it."""

    def __init__(
        self, script: list[str | Exception], barrier: asyncio.Barrier | None = None
    ) -> None:
        self.script = script
        self.barrier = barrier
        self.calls: list[tuple[str, UUID, str, str]] = []
        self.tokens: list[str] = []

    async def confirm(
        self, business_id: str, action_id: UUID, request: ConfirmRequest, *, trace_id: str
    ) -> ConfirmResponse:
        self.calls.append((business_id, action_id, request.customer_ref, trace_id))
        self.tokens.append(request.confirm_token.get_secret_value())
        if self.barrier is not None and len(self.calls) <= self.barrier.parties:
            await self.barrier.wait()
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ConfirmResponse(
            action_id=action_id,
            tool=ToolName.BOOK,
            status=ActionStatus(item),
            message="TEST-ONLY",
        )


TOKEN: Final = new_confirm_token().get_secret_value()


def quiet_bench(confirm: object, sleeps: list[float] | None = None) -> Bench:
    """`bench_with`, whose sleep records the wait instead of waiting."""

    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    return replace(bench_with(confirm), sleep=sleep)


@pytest.mark.anyio
async def test_the_pair_of_confirms_is_in_flight_together() -> None:
    # Each of the first two calls waits at the barrier until the other arrives: two awaits one
    # after the other would never get past it, and the timeout turns that into a failure.
    confirm = FakeConfirm(["executed"] * 3, barrier=asyncio.Barrier(2))
    action_id = uuid4()
    async with asyncio.timeout(1):
        done = await confirm_twice(quiet_bench(confirm), BOOKING, action_id, TOKEN, set())
    assert done == Confirmed(("executed", "executed", "executed"), unresolved=False)


@pytest.mark.anyio
async def test_two_confirms_at_once_then_a_repeat_with_the_responses_token() -> None:
    action_id = uuid4()
    confirm = FakeConfirm(["executed", OutcomeUnknown(), "executed", "executed"])
    traces: set[str] = set()
    sleeps: list[float] = []
    done = await confirm_twice(quiet_bench(confirm, sleeps), BOOKING, action_id, TOKEN, traces)

    assert done == Confirmed(("executed", "executed", "executed"), unresolved=False)
    assert len(confirm.calls) == 4 and len(traces) == 4  # W9's one retry, no wait
    assert sleeps == []
    assert {c[:3] for c in confirm.calls} == {("paws-and-co", action_id, "cust-001")}
    assert set(confirm.tokens) == {TOKEN}


@pytest.mark.anyio
async def test_an_unknown_outcome_confirms_again_until_it_is_definite() -> None:
    # Call 1: not found (definite). Call 2: ConfirmFailed on every attempt. The repeat: unknown
    # twice, then executed on its third attempt, after one wait.
    script: list[str | Exception] = [ActionNotFound()]
    script += [ConfirmFailed()] * CONFIRM_ATTEMPTS
    script += [OutcomeUnknown(), OutcomeUnknown(), "executed"]
    confirm = FakeConfirm(script)
    sleeps: list[float] = []
    done = await confirm_twice(quiet_bench(confirm, sleeps), BOOKING, uuid4(), TOKEN, set())

    assert done == Confirmed(("ActionNotFound", "ConfirmFailed", "executed"), unresolved=False)
    assert len(confirm.calls) == 1 + CONFIRM_ATTEMPTS + 3
    assert len(sleeps) == (CONFIRM_ATTEMPTS - 2) + 1 and all(s > 0 for s in sleeps)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "repeat",
    [[OutcomeUnknown()] * CONFIRM_ATTEMPTS, [ConfirmFailed()] * CONFIRM_ATTEMPTS, [RuntimeError()]],
    ids=["unknown", "failed-to-confirm", "a-bug"],
)
async def test_a_repeat_without_a_definite_answer_is_unresolved(repeat: list[Exception]) -> None:
    confirm = FakeConfirm(["executed", "executed", *repeat])
    done = await confirm_twice(quiet_bench(confirm), BOOKING, uuid4(), TOKEN, set())
    assert done.unresolved is True
    assert done.results[:2] == ("executed", "executed")
    assert done.results[2] == type(repeat[-1]).__name__
    assert confirm.script == []  # every attempt was used, and no more


# ── The sandbox ──────────────────────────────────────────────────────────────────────────────


def test_the_sandbox_is_exact_only_with_exactly_the_expected_rows_of_the_confirmed_action() -> None:
    good = (row("booking", "B-1", "A1", slot="S-09"),)
    assert sandbox_exact(BOOKING, good, "A1") is True
    assert sandbox_exact(BOOKING, good, None) is False
    assert sandbox_exact(BOOKING, good, "A2") is False
    assert sandbox_exact(BOOKING, (row("booking", "B-1", "A1", slot="S-10"),), "A1") is False
    assert sandbox_exact(BOOKING, (*good, row("booking", "B-2", "A1", slot="S-09")), "A1") is False
    move = parsed(
        scenario(
            "s-2",
            kind="confirmation_required",
            tool="reschedule",
            arguments={"booking_id": "B-103", "new_slot_id": "S-10"},
            confirm=True,
        )
    )
    moved = (row("retired", "B-103", "A1"), row("booking", "B-9", "A1", slot="S-10"))
    assert sandbox_exact(move, moved, "A1") is True
    assert sandbox_exact(move, (row("retired", "B-101", "A1"), moved[1]), "A1") is False
    unconfirmed = parsed(
        scenario("s-3", kind="confirmation_required", tool="book", arguments={"slot_id": "S-09"})
    )
    assert sandbox_exact(unconfirmed, (), None) is None
    ticket = parsed(
        scenario(
            "s-4",
            kind="confirmation_required",
            tool="create_ticket",
            arguments={"category": "order"},
            confirm=True,
        )
    )
    assert sandbox_exact(ticket, (row("ticket", "T-1", "A1", category="order"),), "A1") is True
    assert sandbox_exact(ticket, (row("ticket", "T-1", "A1", category="other"),), "A1") is False


def test_expected_active_bookings_add_one_for_a_confirmed_booking() -> None:
    seed = PAWS.seed
    base: Mapping[str, int] = expected_active(parsed(scenario("s-0")), seed)
    assert base == {"cust-001": 2, "cust-002": 1}  # the committed seed: B-101, B-103; B-102
    assert expected_active(BOOKING, seed)["cust-001"] == 3
    proposed_only = parsed(
        scenario("s-5", kind="confirmation_required", tool="book", arguments={"slot_id": "S-09"})
    )
    assert expected_active(proposed_only, seed)["cust-001"] == 2  # never confirmed: no new one


# ── Reading the database, and one scenario, without a database ───────────────────────────────


class FakeConnection:
    """Answers each statement of `writes.py` with planted rows; records the parameters."""

    def __init__(self, answers: Mapping[object, Sequence[Mapping[str, object]]]) -> None:
        self.answers = answers
        self.params: list[tuple[object, dict[str, object]]] = []

    async def execute(self, statement: object, params: dict[str, object]) -> Any:
        self.params.append((statement, params))
        rows = [SimpleNamespace(**row) for row in self.answers.get(statement, [])]
        return SimpleNamespace(all=lambda: rows)


class FakeOwner:
    def __init__(self, connection: FakeConnection | None) -> None:
        self.connection = connection

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FakeConnection]:
        if self.connection is None:
            raise AssertionError("the database must not be read")
        yield self.connection


def write_row(**changes: object) -> dict[str, object]:
    planted: dict[str, object] = {
        "kind": "booking",
        "row_id": "B-200",
        "action_id": "A1",
        "slot_id": "S-09",
        "category": None,
        "action_status": "executed",
        "confirmed_at_set": True,
        "action_trace_ours": True,
        "confirm_trace_id": "1" * 32,
        "executed_audit": True,
    }
    return planted | changes


@pytest.mark.anyio
async def test_write_rows_say_whose_turn_and_whose_confirm_each_row_is() -> None:
    connection = FakeConnection(
        {
            WRITE_ROWS: [
                write_row(),
                write_row(row_id="B-201", action_trace_ours=None, confirm_trace_id="2" * 32),
            ]
        }
    )
    rows = await write_rows(cast(Any, connection), "paws-and-co", TRACE, frozenset({"1" * 32}))
    assert [(r.action_trace_ours, r.confirm_trace_ours) for r in rows] == [
        (True, True),
        (False, False),  # another turn's action (or none), confirmed by another call
    ]
    assert connection.params == [(WRITE_ROWS, {"b": "paws-and-co", "turn": TRACE})]


def one_scenario_bench(
    respond: Any, owner: FakeOwner, confirm: object = None, sleeps: list[float] | None = None
) -> Bench:
    return replace(
        quiet_bench(confirm, sleeps),
        owner=cast(AsyncEngine, owner),
        respond=respond,
        seeds={PAWS.seed.business_id: PAWS.seed},
    )


@pytest.mark.anyio
async def test_a_leak_returns_at_once_and_the_database_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    message = "order_status returned rows of 1 other business(es)"

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        log.leaks.append(message)  # what LeakCheckedTools records before it raises
        handoff = HandoffResponse(
            trace_id=trace, reason=HandoffReason.AGENT_ERROR, priority=Priority.NORMAL
        )
        return AgentRun(handoff, error="CrossBusinessLeak")

    done = await run_scenario(one_scenario_bench(respond, FakeOwner(None)), RUNNABLE)
    assert done.leaks == (message,)
    assert done.line.outcome == "not_run" and done.reply is None
    assert done.error == "CrossBusinessLeak"


def answers_for(action_id: UUID) -> dict[object, list[dict[str, object]]]:
    return {
        TURN_PROPOSALS: [
            {"action_id": str(action_id), "tool": "book", "arguments": '{"slot_id": "S-09"}'}
        ],
        WRITE_ROWS: [write_row(action_id=str(action_id))],
        ACTIVE_BOOKINGS: [
            {"customer_ref": "cust-001", "n": 3},
            {"customer_ref": "cust-002", "n": 1},
        ],
        AUDIT_CALLS: [
            {"tool": "check_slots", "kind": "read", "phase": "run", "outcome": "ok"},
            {"tool": "book", "kind": "write", "phase": "propose", "outcome": "proposed"},
        ],
    }


@pytest.mark.anyio
async def test_a_matching_proposal_is_confirmed_with_the_id_and_token_the_response_reveals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    action_id = uuid4()
    run = offered(action_id)
    response = cast(ConfirmationResponse, run.response)
    revealed = response.confirm_token.get_secret_value()

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        return run

    confirm = FakeConfirm(["executed"] * 3)
    connection = FakeConnection(answers_for(action_id))
    bench = one_scenario_bench(respond, FakeOwner(connection), confirm)
    line = (await run_scenario(bench, RUNNABLE)).line

    assert {c[1] for c in confirm.calls} == {action_id}
    assert confirm.tokens == [revealed] * 3
    assert (line.proposal_matched, line.confirmed_action) == (True, str(action_id))
    assert (line.proposals_stored, line.proposals_audited, line.reads) == (1, 1, ("check_slots",))
    assert line.confirm_unresolved is False and line.sandbox_exact is True
    assert revealed not in line.model_dump_json()  # the token stays in memory only


@pytest.mark.anyio
async def test_an_offer_that_is_not_the_services_own_action_is_never_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    action_id = uuid4()
    run = offered(action_id, own=uuid4())

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        return run

    confirm = FakeConfirm([])
    bench = one_scenario_bench(respond, FakeOwner(FakeConnection(answers_for(action_id))), confirm)
    line = (await run_scenario(bench, RUNNABLE)).line
    assert confirm.calls == []
    assert (line.proposal_matched, line.confirmed_action, line.confirm_results) == (False, None, ())


@pytest.mark.anyio
async def test_a_match_the_customer_does_not_confirm_is_never_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    action_id = uuid4()
    run = offered(action_id)

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        return run

    unconfirmed = RUNNABLE.model_copy(update={"confirm": False})
    confirm = FakeConfirm([])
    bench = one_scenario_bench(respond, FakeOwner(FakeConnection(answers_for(action_id))), confirm)
    line = (await run_scenario(bench, unconfirmed)).line
    assert confirm.calls == []
    assert (line.proposal_matched, line.confirmed_action, line.confirm_results) == (True, None, ())


@pytest.mark.anyio
async def test_an_unresolved_repeat_marks_the_scenario(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    action_id = uuid4()
    run = offered(action_id)

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        return run

    confirm = FakeConfirm(["executed", "executed", *[OutcomeUnknown()] * CONFIRM_ATTEMPTS])
    bench = one_scenario_bench(respond, FakeOwner(FakeConnection(answers_for(action_id))), confirm)
    line = (await run_scenario(bench, RUNNABLE)).line
    assert line.confirm_results == ("executed", "executed", "OutcomeUnknown")
    assert line.confirm_unresolved is True


@pytest.mark.anyio
async def test_a_hand_off_records_its_guidance_as_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_reset(*args: object) -> None:
        return None

    monkeypatch.setattr(turns, "reset", no_reset)
    sos = parsed(scenario("s-sos", kind="handoff", emergency=True, split="dev"))

    async def respond(item: Scenario, request: AgentRequest, trace: str, log: LeakLog) -> AgentRun:
        handoff = HandoffResponse(
            trace_id=trace,
            reason=HandoffReason.SAFETY,
            priority=Priority.URGENT,
            guidance=("TEST-ONLY line one", "TEST-ONLY line two"),
        )
        return AgentRun(handoff)

    connection = FakeConnection({})
    line = (await run_scenario(one_scenario_bench(respond, FakeOwner(connection)), sos)).line
    assert (line.handoff_reason, line.priority, line.guidance_lines) == ("safety", "urgent", 2)
    assert "TEST-ONLY" not in line.model_dump_json()
