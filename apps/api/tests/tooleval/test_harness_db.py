"""The tool-call harness against the real sandbox (M5 PR 6). Integration.

Two throwaway businesses, copies of the committed Paws & Co and Sunrise configurations renamed
`sbx-…`, so the fictional businesses' own rows are never written; a third, a bystander seeded the
same way, must come out byte for byte unchanged. The harness resets each business as the owner
before every scenario and once at the end, runs the agent service with `router=none` as the app
role, confirms only a matching proposal (two at once, then one repeat), and counts the writes from
the database on the owner's connection.

- **With a scripted stand-in for the loop** (`tests/tooleval/fakes.py`, not a loop): plain tests,
  passing now. They check the counts the harness writes, the confirm discipline (a mismatch and a
  `confirm: false` scenario are never confirmed; a double confirm leaves one write), the replies
  file, the run folder and the resets.
- **With the real `run_turn`** and the dry run's fixture plans: waits for H1 through the one
  switch (`pending_hand_write("run_turn")`); until then the harness stops after the first scenario
  ("H1 not written yet"), which the test turns into the NotImplementedError the strict xfail
  expects.
"""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Coroutine, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.catalog import business_tables
from tests.canary.read_paths import pending_hand_write
from tests.conftest import integration
from tests.intenteval.harness_helpers import FakeRepo, Now
from tests.tooleval.fakes import (
    Hand,
    Propose,
    ReadThenReply,
    ask_once_stand_in,
    canned_metrics,
    scripted_stand_in,
)
from tests.tooleval.helpers import PAWS, SUNRISE, frozen_on_split, scenario
from tests.tools.helpers import REPO_ROOT
from tests.tools.sandbox_db import (
    fresh_engine,
    new_business,
    owner_url,
    seed_business,
    seed_for,
    uproot,
)
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.actions.types import ConfirmRequest, ConfirmResponse, PendingAction
from vectapilot_api.agent.service import ProposalStore
from vectapilot_api.agent.types import ModelHandoffReason
from vectapilot_api.config import Settings
from vectapilot_api.main import build_action_services
from vectapilot_api.tooleval import runs, turns
from vectapilot_api.tooleval.harness import Io
from vectapilot_api.tooleval.plans import DEFAULT_PLANS
from vectapilot_api.tooleval.runinfo import (
    DRY_LABEL,
    read_replies,
    read_results,
    read_run,
)
from vectapilot_api.tools.config import ToolName, ToolsConfig
from vectapilot_api.tools.executor import TransactionSource
from vectapilot_api.tools.files import read_business_tools
from vectapilot_api.tools.types import ToolContext, WriteProposal

pytestmark = [integration]

PLANS: Final = REPO_ROOT / DEFAULT_PLANS
PAWS_SEED: Final = read_business_tools(REPO_ROOT / "data" / "sandbox" / PAWS).seed


def db[T](work: Coroutine[Any, Any, T]) -> T:
    """Run one piece of database work (the harness runs its own event loop, so these tests are
    synchronous; the owner's engine has no pool, so each loop gets its own connections)."""
    return asyncio.run(work)


@pytest.fixture
def owner() -> Iterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    db(engine.dispose())


async def rows_of(owner: AsyncEngine, business_id: str) -> str:
    """A digest of every row of `business_id` in every business table (the catalog's)."""
    async with owner.connect() as connection:
        parts = []
        for table in await business_tables(connection):
            found = await connection.execute(
                text(
                    f"SELECT coalesce(string_agg(row_to_json(t)::text, chr(10)"  # noqa: S608
                    f" ORDER BY row_to_json(t)::text), '') FROM {table} AS t"
                    " WHERE business_id = :b"
                ),
                {"b": business_id},
            )
            parts.append(f"{table}:{found.scalar_one()}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


async def scalar(owner: AsyncEngine, sql: str, **params: Any) -> Any:
    async with owner.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar_one()


def io_for(repo: FakeRepo, loop: Any = None, *, metrics: Any = None) -> Io:
    base = Io(
        git=repo.git,
        head_file=repo.head_file,
        head_paths=repo.head_paths,
        now=Now(),
        load_settings=lambda live: Settings(),
        owner_url=owner_url,
    )
    changes: dict[str, Any] = {}
    if loop is not None:
        changes["loop"] = loop
    if metrics is not None:
        changes["metrics"] = metrics
    return replace(base, **changes)


def run_dev(io: Io, dataset_dir: Path) -> tuple[Path, int]:
    return runs.run_command(
        io,
        dataset=dataset_dir,
        corpus=None,
        sandbox=None,
        split="dev",
        live=False,
        max_usd=None,
        cap_text=None,
        reason=None,
        plans=PLANS,
    )


def proposal(
    scenario_id: str, business: str, tool: str, arguments: dict[str, str], **fields: Any
) -> dict[str, Any]:
    return scenario(
        scenario_id,
        business,
        kind="confirmation_required",
        tool=tool,
        arguments=arguments,
        **fields,
    )


class RecordingConfirm:
    """The real confirm service, noting when each call starts and ends (by action id)."""

    def __init__(self, inner: ConfirmService, events: list[tuple[str, UUID]]) -> None:
        self.inner = inner
        self.events = events

    async def confirm(
        self, business_id: str, action_id: UUID, request: ConfirmRequest, *, trace_id: str
    ) -> ConfirmResponse:
        self.events.append(("start", action_id))
        try:
            return await self.inner.confirm(business_id, action_id, request, trace_id=trace_id)
        finally:
            self.events.append(("end", action_id))


def test_the_harness_counts_from_the_database_and_confirms_only_a_match(
    tmp_path: Path, owner: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, UUID]] = []

    def recorded(
        database: TransactionSource, configs: Mapping[str, ToolsConfig], *, lifetime_s: int
    ) -> tuple[ProposalStore, Any]:
        store, confirm = build_action_services(database, configs, lifetime_s=lifetime_s)
        return store, RecordingConfirm(confirm, events)

    monkeypatch.setattr(runs, "build_action_services", recorded)
    paws, sunrise = new_business("t"), new_business("u")
    bystander = seed_for(new_business("z"), PAWS_SEED)
    data = [
        proposal("p-book", paws, "book", {"slot_id": "S-09"}, confirm=True),
        proposal(
            "s-move",
            sunrise,
            "reschedule",
            {"booking_id": "B-103", "new_slot_id": "S-10"},
            confirm=True,
        ),
        proposal(
            "p-ticket",
            paws,
            "create_ticket",
            {"category": "order"},
            confirm=True,
            customer_ref="cust-002",
        ),
        proposal("p-mismatch", paws, "book", {"slot_id": "S-09"}, confirm=True),
        proposal("p-no-confirm", paws, "book", {"slot_id": "S-10"}),
        scenario("p-read", paws, read_only=True),
        scenario("s-sos", sunrise, kind="handoff", emergency=True),
    ]
    messages = {d["id"]: d["message"] for d in data}
    loop = scripted_stand_in(
        {
            messages["p-book"]: Propose(ToolName.BOOK, {"slot_id": "S-09"}),
            messages["s-move"]: Propose(
                ToolName.RESCHEDULE, {"booking_id": "B-103", "new_slot_id": "S-10"}
            ),
            messages["p-ticket"]: Propose(
                ToolName.CREATE_TICKET, {"category": "order", "summary": "TEST-ONLY ticket"}
            ),
            messages["p-mismatch"]: Propose(ToolName.BOOK, {"slot_id": "S-11"}),
            messages["p-no-confirm"]: Propose(ToolName.BOOK, {"slot_id": "S-10"}),
            messages["p-read"]: ReadThenReply(ToolName.ORDER_STATUS, {"order_id": "1042"}),
            messages["s-sos"]: Hand(ModelHandoffReason.SAFETY),
        }
    )
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        db(seed_business(owner, bystander))
        before = db(rows_of(owner, bystander.business_id))

        folder, code = run_dev(io_for(repo, loop), dataset_dir)

        info = read_run(folder)
        lines = {line.id: line for line in read_results(folder)}
        assert (info.status, info.dry_run, info.routing, code) == ("complete", True, "none", 1)
        assert folder.name.endswith("-fake-agent-dev")
        assert folder.parent.parent.parent == repo.root / "evals" / "reports" / "tmp"

        book = lines["p-book"]
        assert (book.outcome, book.proposal_matched, book.proposals_stored) == (
            "confirmation_required",
            True,
            1,
        )
        assert book.confirm_results == ("executed", "executed", "executed")
        assert [(r.kind, r.slot_id, r.action_id) for r in book.write_rows] == [
            ("booking", "S-09", book.confirmed_action)
        ]
        written = book.write_rows[0]
        assert written.action_status == "executed" and written.confirmed_at_set
        assert written.action_trace_ours and written.confirm_trace_ours and written.executed_audit
        assert book.sandbox_exact is True
        assert (book.proposals_audited, book.confirm_unresolved) == (1, False)
        # The pair of confirms was in flight together: both started before either ended.
        mine = [kind for kind, action in events if str(action) == book.confirmed_action]
        assert mine[:2] == ["start", "start"] and len(mine) == 6
        assert {(a.customer_ref, a.actual, a.expected) for a in book.active_bookings} == {
            ("cust-001", 3, 3),
            ("cust-002", 1, 1),
        }

        move = lines["s-move"]
        assert sorted(r.kind for r in move.write_rows) == ["booking", "retired"]
        assert move.sandbox_exact is True and set(move.confirm_results) == {"executed"}

        ticket = lines["p-ticket"]
        assert [(r.kind, r.category) for r in ticket.write_rows] == [("ticket", "order")]
        assert ticket.proposal_arguments == {"category": "order"}  # never the ticket's text

        for scenario_id, arguments in (
            ("p-mismatch", {"slot_id": "S-11"}),
            ("p-no-confirm", {"slot_id": "S-10"}),
        ):
            line = lines[scenario_id]
            assert line.confirmed_action is None and line.confirm_results == ()
            assert line.write_rows == () and line.proposal_arguments == arguments
        assert lines["p-mismatch"].proposal_matched is False
        assert lines["p-no-confirm"].proposal_matched is True

        read = lines["p-read"]
        assert (read.outcome, read.reads, read.proposals_stored) == ("reply", ("order_status",), 0)
        assert read.proposals_audited == 0
        assert [r.id for r in read_replies(folder)] == ["p-read"]

        sos = lines["s-sos"]
        assert (sos.outcome, sos.handoff_reason, sos.priority) == ("handoff", "safety", "urgent")
        assert sos.guidance_lines > 0  # the business's configured guidance, as a count only

        # The double confirm left one executed audit row and two replays for that action.
        outcomes = Counter(
            json.loads(
                db(
                    scalar(
                        owner,
                        "SELECT coalesce(json_agg(outcome), '[]')::text FROM tool_audit"
                        " WHERE business_id = :b AND action_id = :a AND phase = 'confirm'",
                        b=paws,
                        a=book.confirmed_action,
                    )
                )
            )
        )
        assert outcomes == Counter({"executed": 1, "replayed": 2})
        # Reset at the end: no write row and no pending action is left; the audit stays.
        for business in (paws, sunrise):
            booked = (
                "SELECT count(*) FROM sandbox_bookings WHERE business_id = :b AND origin = 'action'"
            )
            assert db(scalar(owner, booked, b=business)) == 0
            pending = "SELECT count(*) FROM pending_actions WHERE business_id = :b"
            assert db(scalar(owner, pending, b=business)) == 0
        assert db(rows_of(owner, bystander.business_id)) == before
        text_out = (folder / "results.jsonl").read_text("utf-8")
        assert "TEST-ONLY" not in text_out
    finally:
        for business in (paws, sunrise, bystander.business_id):
            db(uproot(owner, business))


class FailingStore:
    """A store whose every `create` fails, as a database outage at that moment would."""

    async def create(self, proposal: WriteProposal, context: ToolContext) -> PendingAction:
        raise RuntimeError("TEST-ONLY: the store is down")


def test_a_proposal_whose_store_failed_is_still_counted_from_the_audit(
    tmp_path: Path, owner: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(
        database: TransactionSource, configs: Mapping[str, ToolsConfig], *, lifetime_s: int
    ) -> tuple[ProposalStore, ConfirmService]:
        _, confirm = build_action_services(database, configs, lifetime_s=lifetime_s)
        return FailingStore(), confirm

    monkeypatch.setattr(runs, "build_action_services", failing)
    paws, sunrise = new_business("t"), new_business("u")
    data = [
        scenario("p-sos", paws, kind="handoff", emergency=True),
        scenario("s-look", sunrise, read_only=True),
    ]
    loop = scripted_stand_in(
        {
            data[0]["message"]: Propose(ToolName.BOOK, {"slot_id": "S-09"}),
            data[1]["message"]: Propose(ToolName.BOOK, {"slot_id": "S-10"}),
        }
    )
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        folder, _ = run_dev(io_for(repo, loop), dataset_dir)
        for line in read_results(folder):
            assert (line.outcome, line.handoff_reason, line.error) == (
                "handoff",
                "proposal_not_stored",
                "RuntimeError",
            )
            assert (line.proposals_stored, line.proposals_audited) == (0, 1), line.id
            assert line.write_rows == () and line.confirmed_action is None
    finally:
        for business in (paws, sunrise):
            db(uproot(owner, business))


def test_a_row_another_turn_left_behind_is_listed_and_not_this_turns(
    tmp_path: Path, owner: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Each business is reset only the first time, so p-look counts the booking p-book left, as it
    # would if anything else wrote on the business between a reset and the count.
    resets: list[str] = []
    real_reset = turns.reset

    async def first_only(engine: AsyncEngine, seed: Any) -> None:
        if seed.business_id not in resets:
            await real_reset(engine, seed)
        resets.append(seed.business_id)

    monkeypatch.setattr(turns, "reset", first_only)
    paws, sunrise = new_business("t"), new_business("u")
    data = [
        proposal("p-book", paws, "book", {"slot_id": "S-09"}, confirm=True),
        scenario("p-look", paws, read_only=True),
        scenario("s-ask", sunrise, kind="handoff"),
    ]
    loop = scripted_stand_in(
        {
            data[0]["message"]: Propose(ToolName.BOOK, {"slot_id": "S-09"}),
            data[1]["message"]: ReadThenReply(ToolName.ORDER_STATUS, {"order_id": "1042"}),
            data[2]["message"]: Hand(ModelHandoffReason.CUSTOMER_ASKED),
        }
    )
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        folder, _ = run_dev(io_for(repo, loop), dataset_dir)
        lines = {line.id: line for line in read_results(folder)}
        assert resets == [paws, paws, sunrise]
        assert lines["s-ask"].write_rows == ()
        [mine] = lines["p-book"].write_rows
        assert mine.action_trace_ours and mine.confirm_trace_ours
        [left] = lines["p-look"].write_rows
        assert (left.row_id, left.action_id) == (mine.row_id, mine.action_id)
        assert (left.action_trace_ours, left.confirm_trace_ours) == (False, False)
    finally:
        for business in (paws, sunrise):
            db(uproot(owner, business))


def test_with_h2_stand_ins_the_run_writes_its_summary(tmp_path: Path, owner: AsyncEngine) -> None:
    paws, sunrise = new_business("t"), new_business("u")
    data = [scenario("p-read", paws, read_only=True), scenario("s-ask", sunrise, kind="handoff")]
    loop = scripted_stand_in(
        {
            data[0]["message"]: ReadThenReply(
                ToolName.CHECK_SLOTS, {"date_from": "2026-11-16", "date_to": "2026-11-17"}
            ),
            data[1]["message"]: Hand(ModelHandoffReason.CUSTOMER_ASKED),
        }
    )
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        folder, code = run_dev(io_for(repo, loop, metrics=canned_metrics()), dataset_dir)
        assert code == 0
        summary = (folder / "summary.md").read_text("utf-8")
        assert DRY_LABEL in summary.splitlines()[2]
        lines = read_results(folder)
        assert [(x.id, x.reads) for x in lines] == [("p-read", ("check_slots",)), ("s-ask", ())]
        assert lines[1].handoff_reason == "customer_asked"
    finally:
        for business in (paws, sunrise):
            db(uproot(owner, business))


def test_the_model_path_runs_through_the_capped_gateway_and_the_plans(
    tmp_path: Path, owner: AsyncEngine
) -> None:
    paws, sunrise = new_business("t"), new_business("u")
    data = [scenario("p-1", paws), scenario("s-1", sunrise)]
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        folder, _ = run_dev(io_for(repo, ask_once_stand_in()), dataset_dir)
        info = read_run(folder)
        lines = read_results(folder)
        assert info.status == "complete" and info.cost.llm_calls == 2
        assert info.fingerprint.plans_sha256 is not None
        assert [(x.steps, x.handoff_reason, x.cost_usd is not None) for x in lines] == [
            (1, "other", True),
            (1, "other", True),
        ]
        records = (folder / "llm_call_log.jsonl").read_text("utf-8").splitlines()
        assert len(records) == 2
        assert all(json.loads(r)["llm_call"]["operation"] == "agent-step" for r in records)
        assert "TEST-ONLY" not in "".join(records)  # metadata only, never the message
    finally:
        for business in (paws, sunrise):
            db(uproot(owner, business))


@pytest.mark.parametrize("stub", [pytest.param("run_turn", marks=pending_hand_write("run_turn"))])
def test_the_real_loop_runs_the_dry_plans_end_to_end(
    tmp_path: Path, owner: AsyncEngine, stub: str
) -> None:
    paws, sunrise = new_business("t"), new_business("u")
    data = [
        scenario("p-1", paws),
        scenario("p-2", paws, read_only=True),
        scenario("s-1", sunrise, kind="handoff", emergency=True),
    ]
    dataset_dir = frozen_on_split(tmp_path, data, renames={PAWS: paws, SUNRISE: sunrise})
    repo = FakeRepo(root=dataset_dir.parents[3])
    try:
        folder, _ = run_dev(io_for(repo, metrics=canned_metrics()), dataset_dir)
        info = read_run(folder)
        if info.stop == runs.H1_MISSING:
            raise NotImplementedError(runs.H1_MISSING)
        assert info.status == "complete"
        assert all(line.outcome != "not_run" for line in read_results(folder))
        assert (folder / "llm_call_log.jsonl").read_text("utf-8").strip()
    finally:
        for business in (paws, sunrise):
            db(uproot(owner, business))
