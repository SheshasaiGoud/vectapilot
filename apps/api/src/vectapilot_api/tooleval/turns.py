"""One scenario through the agent, counted from the database (M5 PR 6; the plan's scoring rules).

`run_scenario` does, for one scenario:
1. **Reset** the business's sandbox from its seed, as the owner (`tools.sandbox.reset_sandbox`,
   the command line's reset: every statement scoped to the business, `tool_audit` untouched), so
   every write row afterwards is this scenario's.
2. **The turn**: `AgentService.respond_unrouted` (`router=none`: no router, no W11 step; the
   report says "safety floor not exercised"), with a fresh trace id derived from the run id and the
   scenario id. The loop's tools are wrapped by `LeakCheckedTools`: every envelope's business ids
   are checked **before** its observation can reach the model, and a row of another business
   raises instead of returning (scoring rules, "Leak check first"); the run then stops (exit 4).
3. **The proposal, from the database**: the pending actions stored under the turn's trace id. It
   *matches* when there is exactly one, its tool and key arguments (`KEY_ARGUMENTS`) equal the
   scenario's `expect`, it is the action the response offered, and the service's own action is
   that one too (a cross-check). **Only a matching proposal of a `confirm` scenario is
   confirmed**, the way a client does it: the action id and the token are read from the
   response's JSON (the one place the token is revealed), never from the in-process action. Two
   confirms at once, then one repeat; a call answered `OutcomeUnknown` or `ConfirmFailed` confirms
   again (W9's client rule), at most `CONFIRM_ATTEMPTS` times, waiting a moment from the third
   attempt on: the confirm locks the action row, so a commit still in flight lands before the rows
   are read. A repeat without a definite answer marks the scenario `confirm_unresolved`. A
   mismatch is recorded by id and nothing is confirmed, so a confirmed wrong write cannot happen
   by construction.
4. **The counts, from the database** (`writes.py`, the owner's connection, by business and trace
   id): every write row with its action's state and whether the action is this turn's, the active
   bookings per customer, and the turn's audited tool calls (the reads, the typed errors, and the
   proposals the executor audited, which a failed store cannot remove). Never what the loop or the
   service reported.

The result line holds ids and codes only (`runinfo.ResultLine`); a reply's text goes to the
replies file, apart. Nothing here logs.
"""

import asyncio
import hashlib
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.types import (
    ActionNotFound,
    ConfirmFailed,
    ConfirmRequest,
    OutcomeUnknown,
)
from vectapilot_api.agent.responses import (
    AgentRequest,
    ConfirmationResponse,
    HandoffResponse,
    ReplyResponse,
)
from vectapilot_api.agent.service import AgentRun
from vectapilot_api.agent.types import LoopLimits, StepModel, ToolRunner, TurnLoop, TurnOutcome
from vectapilot_api.answer.types import AnswerRun
from vectapilot_api.db import business_transaction
from vectapilot_api.tooleval.runinfo import ActiveBookings, ReplyLine, ResultLine, WriteRow
from vectapilot_api.tooleval.scenarios import KEY_ARGUMENTS, Scenario
from vectapilot_api.tooleval.split import unit_key
from vectapilot_api.tooleval.writes import (
    StoredProposal,
    active_bookings,
    audit_calls,
    turn_proposals,
    write_rows,
)
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.sandbox import SandboxSeed, reset_sandbox
from vectapilot_api.tools.types import PROPOSED, ToolEnvelope, ToolErrorCode, ToolKind

TOOL_ERROR_CODES: Final = frozenset(code.value for code in ToolErrorCode)
CONFIRM_ATTEMPTS: Final = 5
"""Per confirm call: the first try, W9's one retry, then up to three more after `SETTLE_DELAY_S`."""
SETTLE_DELAY_S: Final = 0.2
DEFINITE: Final = frozenset({"executed", "failed", "ActionNotFound"})
"""A confirm answer that settles the action: its stored status, or no such action."""


def trace_for(run_id: str, scenario_id: str, what: str) -> str:
    """A W3C trace id (32 hex) for one call of one scenario of one run: the turn (`turn`) or a
    confirm attempt (`confirm-<call>-<attempt>`). Derived, so the database's rows of a run can be
    found again from its run id."""
    return hashlib.sha256(f"tool-calls:{run_id}:{scenario_id}:{what}".encode()).hexdigest()[:32]


# ── The leak check ───────────────────────────────────────────────────────────────────────────


class CrossBusinessLeak(Exception):
    """A tool returned a row of another business. Names the tool and counts, never an id."""


@dataclass
class LeakLog:
    """Leaks seen in one turn. Kept apart from the exception: the loop turns any tool exception
    into a hand-off (H1 rule 7), so the harness reads this after the turn, whatever it ended in."""

    leaks: list[str] = field(default_factory=list)


class LeakCheckedTools:
    """A `ToolRunner` that checks every envelope's business ids before returning it."""

    def __init__(self, inner: ToolRunner, business_id: str, log: LeakLog) -> None:
        self._inner = inner
        self._business_id = business_id
        self._log = log

    def kind(self, tool: ToolName) -> ToolKind:
        return self._inner.kind(tool)

    def _checked(self, tool: ToolName, envelope: ToolEnvelope) -> ToolEnvelope:
        foreign = envelope.business_ids - {self._business_id}
        if foreign:
            message = f"{tool.value} returned rows of {len(foreign)} other business(es)"
            self._log.leaks.append(message)
            raise CrossBusinessLeak(message)
        return envelope

    async def run(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return self._checked(tool, await self._inner.run(tool, arguments))

    async def propose(self, tool: ToolName, arguments: Mapping[str, object]) -> ToolEnvelope:
        return self._checked(tool, await self._inner.propose(tool, arguments))


def leak_checked(loop: TurnLoop, business_id: str, log: LeakLog) -> TurnLoop:
    """`loop` with its tools wrapped by `LeakCheckedTools`. Not a loop: it only wraps."""

    async def checked(
        message: str, *, step_model: StepModel, tools: ToolRunner, limits: LoopLimits
    ) -> TurnOutcome:
        wrapped = LeakCheckedTools(tools, business_id, log)
        return await loop(message, step_model=step_model, tools=wrapped, limits=limits)

    return checked


class NoAnswers:
    """The service's answer source under `router=none`, which never routes to RAG."""

    async def answer(self, business_id: str, question: str, *, trace_id: str) -> AnswerRun:
        raise RuntimeError("router=none never routes to an answer")


# ── One scenario ─────────────────────────────────────────────────────────────────────────────


Respond = Callable[[Scenario, AgentRequest, str, LeakLog], Awaitable[AgentRun]]
"""`(scenario, request, trace id, leak log) -> AgentRun`: `AgentService.respond_unrouted`
through a service built with `leak_checked` (`runs.py`); tests pass their own."""


@dataclass(frozen=True)
class Bench:
    """What every scenario of a run shares."""

    run_id: str
    owner: AsyncEngine
    respond: Respond
    confirm: ConfirmService
    seeds: Mapping[str, SandboxSeed]
    cost_of: Callable[[str], tuple[Decimal | None, bool]]
    """A trace id → (the summed cost of its `llm_call` records, whether every one was complete)."""
    clock: Callable[[], float] = time.perf_counter
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass(frozen=True)
class ScenarioRun:
    line: ResultLine
    reply: ReplyLine | None
    leaks: tuple[str, ...]
    error: str | None
    """The class name of an exception the service turned into a hand-off."""


async def reset(owner: AsyncEngine, seed: SandboxSeed) -> None:
    """The command line's reset (`tools/cli.py`), committed: the seed's business only."""
    async with business_transaction(owner, seed.business_id) as connection:
        await reset_sandbox(connection, seed)


def key_arguments(proposal: StoredProposal) -> dict[str, str]:
    """A stored proposal's key arguments as text (never a ticket's summary)."""
    return {
        name: str(proposal.arguments[name])
        for name in KEY_ARGUMENTS[proposal.tool]
        if name in proposal.arguments
    }


def matches(scenario: Scenario, stored: tuple[StoredProposal, ...], run: AgentRun) -> bool | None:
    """None without exactly one stored proposal; else whether it is `expect`'s tool and key
    arguments, the action the response offered, and the service's own action (module docstring,
    3)."""
    if len(stored) != 1:
        return None
    proposal = stored[0]
    response = run.response
    offered = (
        isinstance(response, ConfirmationResponse)
        and str(response.action_id) == proposal.action_id
        and run.action is not None
        and run.action.id == response.action_id
    )
    return (
        offered
        and proposal.tool == scenario.expect.tool
        and key_arguments(proposal) == scenario.expect.arguments
    )


@dataclass(frozen=True)
class Confirmed:
    results: tuple[str, ...]
    """Each call's last answer, in order: the pair, then the repeat."""
    unresolved: bool
    """The repeat's last answer is not in `DEFINITE`."""


async def confirm_twice(
    bench: Bench, scenario: Scenario, action_id: UUID, token: str, traces: set[str]
) -> Confirmed:
    """Two confirms at once, then one repeat, with `token` as the response revealed it; each
    result is the stored status or the exception's class name (module docstring, 3). Every confirm
    trace id used is added to `traces`. The repeat runs after both of the pair have ended and
    waits on the action's row lock, so its definite answer settles the action."""

    async def once(call: int) -> str:
        last = "not_sent"
        for attempt in range(1, CONFIRM_ATTEMPTS + 1):
            if attempt > 2:
                await bench.sleep(SETTLE_DELAY_S)
            trace = trace_for(bench.run_id, scenario.id, f"confirm-{call}-{attempt}")
            traces.add(trace)
            request = ConfirmRequest.model_validate(
                {"customer_ref": scenario.customer_ref, "confirm_token": token}
            )
            try:
                response = await bench.confirm.confirm(
                    scenario.business_id, action_id, request, trace_id=trace
                )
            except (OutcomeUnknown, ConfirmFailed) as error:  # W9: the client confirms again
                last = type(error).__name__
                continue
            except ActionNotFound:
                return "ActionNotFound"
            except Exception as error:  # a bug: recorded by class name, the counts still run
                return type(error).__name__
            return response.status.value
        return last

    pair = await asyncio.gather(once(1), once(2))
    repeat = await once(3)
    return Confirmed((*pair, repeat), repeat not in DEFINITE)


def sandbox_exact(
    scenario: Scenario, rows: tuple[WriteRow, ...], confirmed: str | None
) -> bool | None:
    """For a `confirm` scenario: whether the sandbox holds exactly the expected write (one booking
    on the slot; the booking retired and one on the new slot; one ticket of the category), all of
    the confirmed action. None for any other scenario."""
    expect = scenario.expect
    if not scenario.confirm or expect.tool is None or expect.arguments is None:
        return None
    if confirmed is None or len(rows) != expect.write_rows:
        return False
    if any(row.action_id != confirmed for row in rows):
        return False
    arguments = expect.arguments
    created = [row for row in rows if row.kind == "booking"]
    retired = [row for row in rows if row.kind == "retired"]
    tickets = [row for row in rows if row.kind == "ticket"]
    if expect.tool is ToolName.BOOK:
        return len(created) == 1 and created[0].slot_id == arguments["slot_id"]
    if expect.tool is ToolName.RESCHEDULE:
        return (
            len(created) == 1
            and len(retired) == 1
            and retired[0].row_id == arguments["booking_id"]
            and created[0].slot_id == arguments["new_slot_id"]
        )
    return len(tickets) == 1 and tickets[0].category == arguments["category"]


def expected_active(scenario: Scenario, seed: SandboxSeed) -> Counter[str]:
    """Each customer's active bookings the sandbox should hold: the seed's, plus one for the
    scenario's customer when it confirms a `book`."""
    expected = Counter({customer: 0 for customer in seed.customers})
    expected.update(booking.customer_ref for booking in seed.bookings)
    if scenario.confirm and scenario.expect.tool is ToolName.BOOK:
        expected[scenario.customer_ref] += 1
    return expected


def base_fields(scenario: Scenario) -> dict[str, object]:
    if scenario.split is None:
        raise ValueError(f"scenario {scenario.id} has no split: run only a frozen dataset")
    return {
        "id": scenario.id,
        "business_id": scenario.business_id,
        "language_group": scenario.language_group,
        "split": scenario.split,
        "unit": ":".join(unit_key(scenario)),
        "read_only": scenario.read_only,
        "emergency": scenario.emergency,
        "confirm": scenario.confirm,
        "steering": scenario.seed == "steering",
        "expected_kind": scenario.expect.kind,
        "expected_tool": scenario.expect.tool,
        "expected_arguments": scenario.expect.arguments,
        "expected_write_rows": scenario.expected_write_rows,
    }


def not_run(scenario: Scenario) -> ResultLine:
    return ResultLine.model_validate(base_fields(scenario) | {"outcome": "not_run"})


async def run_scenario(bench: Bench, scenario: Scenario) -> ScenarioRun:
    """One scenario (module docstring). `CapReached` and cancellation propagate."""
    seed = bench.seeds[scenario.business_id]
    business = scenario.business_id
    await reset(bench.owner, seed)
    turn = trace_for(bench.run_id, scenario.id, "turn")
    log = LeakLog()
    request = AgentRequest(message=scenario.message, customer_ref=scenario.customer_ref)
    started = bench.clock()
    run = await bench.respond(scenario, request, turn, log)
    latency_ms = max(0.0, (bench.clock() - started) * 1000)
    if log.leaks:
        return ScenarioRun(not_run(scenario), None, tuple(log.leaks), run.error)

    async with bench.owner.connect() as connection:
        stored = await turn_proposals(connection, business, turn)
    matched = matches(scenario, stored, run)
    response = run.response
    confirm_traces: set[str] = set()
    confirmed: str | None = None
    settled = Confirmed((), unresolved=False)
    if matched and scenario.confirm and isinstance(response, ConfirmationResponse):
        # As a client: the id and the token from the response's JSON (module docstring, 3).
        payload = json.loads(response.model_dump_json())
        action_id = UUID(payload["action_id"])
        confirmed = str(action_id)
        settled = await confirm_twice(
            bench, scenario, action_id, payload["confirm_token"], confirm_traces
        )

    async with bench.owner.connect() as connection:
        rows = await write_rows(connection, business, turn, frozenset(confirm_traces))
        active = await active_bookings(connection, business)
        calls = await audit_calls(connection, business, turn)
    expected = expected_active(scenario, seed)
    cost, complete = bench.cost_of(turn)
    handoff = response if isinstance(response, HandoffResponse) else None
    fields = base_fields(scenario) | {
        "outcome": response.kind,
        "handoff_reason": None if handoff is None else handoff.reason.value,
        "priority": None if handoff is None else handoff.priority.value,
        "guidance_lines": 0 if handoff is None else len(handoff.guidance),
        "proposals_stored": len(stored),
        "proposals_audited": sum(c.phase == "propose" and c.outcome == PROPOSED for c in calls),
        "proposal_tool": stored[0].tool if len(stored) == 1 else None,
        "proposal_arguments": key_arguments(stored[0]) if len(stored) == 1 else None,
        "proposal_matched": matched,
        "confirmed_action": confirmed,
        "confirm_results": settled.results,
        "confirm_unresolved": settled.unresolved,
        "write_rows": rows,
        "sandbox_exact": sandbox_exact(scenario, rows, confirmed),
        "active_bookings": tuple(
            ActiveBookings(customer_ref=c, actual=active[c], expected=expected[c])
            for c in sorted(set(expected) | set(active))
        ),
        "reads": tuple(c.tool for c in calls if c.kind == ToolKind.READ.value and c.phase == "run"),
        "tool_errors": tuple(
            c.outcome
            for c in calls
            if c.phase in ("run", "propose") and c.outcome in TOOL_ERROR_CODES
        ),
        "steps": run.steps,
        "latency_ms": latency_ms,
        "cost_usd": cost,
        "cost_complete": complete,
        "error": run.error,
    }
    reply = (
        ReplyLine(id=scenario.id, text=response.text)
        if isinstance(response, ReplyResponse)
        else None
    )
    return ScenarioRun(ResultLine.model_validate(fields), reply, (), run.error)
