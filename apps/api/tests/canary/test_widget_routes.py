"""The widget's message and hand-off routes against Postgres and Redis (M8 PR 2a; S1, S8, S11,
S14, S17, D6 (a), ADR 0019 decision 8). Integration only, under tests/canary/ so the canary
gate's no-skip rule covers them.

The real app as the app role (`tests/canary/widget.py`: each planted business served as `retail`
with a guard folder, the guards on, `LookupMatcher` standing in for H1 on the red-flag messages a
test names, NOT H1), on two planted businesses (`test_sessions.plant`, migration 0008's and
0009's bait included: both hold one hand-off on one widget session with the same ids):

- **Writes land in their own business only:** a message that hands off adds exactly one hand-off
  row and one turn row to the session's business, under the session's id, and the other
  business's rows of migration 0009's tables are byte for byte the same afterwards (the positive
  control is the actor's new rows). The masked message is S11's helper's, never the raw text.
- **Honest hand-offs (S8):** `queued` is true and its id is the stored row's; a hung store (its
  session row locked past `handoff_store_timeout_seconds`) still answers the same guarded hand-off
  with `queued: false`, and leaves neither row.
- **One open hand-off per session (S17):** a flood of red flags is one row with its `count`; a
  normal hand-off then an urgent one is one urgent row (raised, its message replaced); two
  concurrent "talk to a person" presses, both waiting on the same row lock, are one row with one
  id. **The rank** (amended 2026-11-25, shesha, Z3 (a)), through the store's own statement as the
  app role: at equal priority a `safety` hand-off raises a system row (Z3's two sequences, every
  raised column replaced) and a lesser class never lowers it; every (priority, class) pair
  against every other ranks as `handoffs.store.rises` and the hand-written order of
  `tests/handoffs/test_store.py` say (savepoints, rolled back), and `reason_class_sql` classes
  every reason as `reason_class` does.
- **Session X against session Y (S1, S4):** Y cannot read X's hand-off in the same business, nor
  can a session of the other business; X reads its own.
- **Smuggling (S1):** a body naming `customer_ref` or `business_id`, and `X-Business-Id`, write
  nothing.
- **S14:** with Redis down, an existing session's red-flag message is still urgent and queued,
  and creating a session is the typed 503.
- **Turns and feedback (PR 2b, D6 (a)):** every turn writes one turn row holding no text, under
  the trace id its body carried; the session rates that turn once (201, then 409), and the rating
  lands in its own business only. Session Y of the same business, a session of the other business,
  a made-up trace id and the planted bait's trace id are each the one 404 and write nothing (S1,
  S4), and the database's own key refuses a rating that names another session's turn (the red
  proof: SQLSTATE 23503 as the app role). A body naming anyone is a 422 and the header a 400,
  writing nothing.
- **The wrong kind (S1, PR 2b):** a staff session's token as `Authorization: Bearer` is the one
  401 on widget confirm and on feedback.

Every planted business is removed afterwards (`sessions.SESSION_UPROOT`, 0009's tables first).
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final
from uuid import UUID

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.actions.db import until_waiting
from tests.canary.checks import Planted
from tests.canary.sessions import BAIT_TRACE_ID, SESSION_UPROOT, Purpose, bait_token
from tests.canary.test_sessions import app_url, owner_url, plant
from tests.canary.widget import SAME_ORIGIN, bearer, new_session, served_widgets
from tests.conftest import integration
from tests.handoffs.test_store import LOWEST_FIRST, REPRESENTATIVE
from vectapilot_api.agent.responses import HandoffReason
from vectapilot_api.auth.dependencies import UNAUTHORIZED
from vectapilot_api.auth.tokens import token_sha256
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.db import business_transaction
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.handoffs.at_rest import mask_at_rest
from vectapilot_api.handoffs.store import (
    INSERT_RATING,
    NewHandoff,
    Origin,
    StoredHandoff,
    reason_class,
    reason_class_sql,
    store_handoff,
)
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.retrieval.router import TRACE_HEADER

pytestmark = [integration, pytest.mark.anyio]

TABLES: Final = ("handoffs", "widget_turns", "turn_feedback")
"""Migration 0009's tables."""
SYNTHETIC_PHONE: Final = "+91 00000 20777"
"""Synthetic (the log canary's): never dialable, and in a shape H2 masks."""
EMERGENCY: Final = f"This is an emergency, please call me back on {SYNTHETIC_PHONE}"
"""Hits the platform red flag through `LookupMatcher` (NOT H1)."""
BENIGN: Final = "Do you deliver on Sundays?"
"""Hits nothing: a planted `retail` business has no tool profile, so a `not_configured`
hand-off at normal priority."""
FLOOD: Final = 5
HUNG_STORE_S: Final = 0.2
"""The store's timeout in the hung-store test: far below the time the lock is held."""


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def planted(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> AsyncIterator[tuple[Planted, Planted]]:
    seeded: list[Planted] = []
    try:
        seeded.append(await plant(owner_engine, app_engine, "a"))
        seeded.append(await plant(owner_engine, app_engine, "b"))
        yield seeded[0], seeded[1]
    finally:
        for business in seeded:
            async with business_transaction(owner_engine, business.business_id) as connection:
                for statement in (
                    *SESSION_UPROOT,
                    text("DELETE FROM businesses WHERE business_id = :b"),
                ):
                    await connection.execute(statement, {"b": business.business_id})


Rows = dict[str, list[dict[str, Any]]]


async def rows(engine: AsyncEngine, business_id: str) -> Rows:
    """Every row of 0009's tables for `business_id`, as the owner (in a stable order)."""
    found: Rows = {}
    async with business_transaction(engine, business_id) as connection:
        for table in TABLES:
            result = await connection.execute(
                text(f"SELECT * FROM {table} WHERE business_id = :b ORDER BY id"),  # noqa: S608
                {"b": business_id},
            )
            found[table] = [dict(row._mapping) for row in result]
    return found


def added(before: Rows, after: Rows, table: str) -> list[dict[str, Any]]:
    known = {row["id"] for row in before[table]}
    return [row for row in after[table] if row["id"] not in known]


async def session_id(engine: AsyncEngine, business_id: str, token: str) -> UUID:
    """The session row `token` opens, read by its hash as the owner."""
    async with business_transaction(engine, business_id) as connection:
        found = await connection.scalar(
            text("SELECT id FROM sessions WHERE business_id = :b AND token_sha256 = :h"),
            {"b": business_id, "h": token_sha256(token)},
        )
    assert isinstance(found, UUID), "no such session"
    return found


def send(client: TestClient, token: str, message: str, **headers: str) -> httpx2.Response:
    return client.post(
        "/v1/widget/messages", json={"message": message}, headers={**bearer(token), **headers}
    )


def press(client: TestClient, token: str) -> httpx2.Response:
    return client.post("/v1/widget/handoffs", headers=bearer(token))


def status(client: TestClient, token: str, handoff_id: object) -> httpx2.Response:
    return client.get(f"/v1/widget/handoffs/{handoff_id}", headers=bearer(token))


def queued(response: httpx2.Response) -> Mapping[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    handoff = body["handoff"] if "turn" in body else body
    assert isinstance(handoff, dict)
    return handoff


# ── Writes land in their own business only; honest hand-offs ─────────────────────────────────


async def test_a_red_flag_message_stores_its_turn_and_hand_off_in_its_sessions_business_only(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    before_a, before_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )

    with served_widgets([a.business_id, b.business_id], red_flags=[EMERGENCY]) as client:
        token = new_session(client, a.business_id)
        response = send(client, token, EMERGENCY)

    assert response.status_code == 200, response.text
    turn, handoff = response.json()["turn"], response.json()["handoff"]
    assert (turn["kind"], turn["reason"], turn["priority"]) == ("handoff", "safety", "urgent")
    assert handoff["queued"] is True and response.headers[TRACE_HEADER] == turn["trace_id"]
    after_a, after_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )
    assert after_b == before_b  # B, byte for byte
    session = await session_id(owner_engine, a.business_id, token)
    [stored] = added(before_a, after_a, "handoffs")
    # `queued: true` names the row that exists (S8), under the session's own ids (S1, S4).
    assert str(stored["id"]) == handoff["id"]
    assert (stored["session_id"], stored["trace_id"]) == (session, turn["trace_id"])
    assert (stored["origin"], stored["reason"], stored["priority"]) == ("turn", "safety", "urgent")
    assert (stored["count"], stored["status"], stored["channel"]) == (1, "open", "web")
    assert stored["guidance_shown"] == turn["guidance"] == []  # a retail business has none
    # S11: masked by the helper, never the raw text.
    assert stored["message_masked"] == mask_at_rest(EMERGENCY) != EMERGENCY
    assert SYNTHETIC_PHONE not in str(after_a)
    [row] = added(before_a, after_a, "widget_turns")
    assert (row["session_id"], row["trace_id"], row["kind"]) == (
        session,
        turn["trace_id"],
        "handoff",
    )
    assert (row["reason"], row["priority"], row["answer_outcome"]) == ("safety", "urgent", None)
    assert row["server_latency_ms"] >= 0
    # D11 (zara's PR 2 review): an urgent `safety` row invites no follow-up; no press yet.
    assert handoff["follow_up"] is False and stored["pressed_at"] is None
    assert added(before_a, after_a, "turn_feedback") == []


async def test_a_hung_store_keeps_the_guarded_hand_off_says_not_queued_and_leaves_no_row(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    # S8, S14: the session row is locked, so the hand-off's insert waits on its foreign-key check
    # past the store's own timeout; the customer still gets the same urgent hand-off.
    a, _ = planted
    with served_widgets(
        [a.business_id], red_flags=[EMERGENCY], handoff_store_timeout_seconds=HUNG_STORE_S
    ) as client:
        token = new_session(client, a.business_id)
        session = await session_id(owner_engine, a.business_id, token)
        before = await rows(owner_engine, a.business_id)
        async with business_transaction(owner_engine, a.business_id) as holder:
            await holder.execute(
                text("SELECT 1 FROM sessions WHERE business_id = :b AND id = :s FOR UPDATE"),
                {"b": a.business_id, "s": session},
            )
            response = await asyncio.to_thread(send, client, token, EMERGENCY)
        after = await rows(owner_engine, a.business_id)

    turn = response.json()["turn"]
    assert (turn["reason"], turn["priority"]) == ("safety", "urgent")
    assert queued(response) == {"id": None, "queued": False, "follow_up": False}
    assert after == before  # neither the hand-off nor the turn row (one transaction)


# ── S17: one open hand-off per session ───────────────────────────────────────────────────────


async def test_a_flood_of_red_flags_on_one_session_is_one_row_with_its_count(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    before = await rows(owner_engine, a.business_id)

    with served_widgets([a.business_id], red_flags=[EMERGENCY]) as client:
        token = new_session(client, a.business_id)
        answers = [queued(send(client, token, EMERGENCY)) for _ in range(FLOOD)]

    after = await rows(owner_engine, a.business_id)
    [stored] = added(before, after, "handoffs")
    assert {answer["id"] for answer in answers} == {str(stored["id"])}
    assert all(answer["queued"] for answer in answers)
    assert (stored["count"], stored["priority"], stored["raised_at"]) == (FLOOD, "urgent", None)
    assert len(added(before, after, "widget_turns")) == FLOOD  # one turn row per message


async def test_a_normal_hand_off_then_an_urgent_one_is_one_raised_urgent_row(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    before = await rows(owner_engine, a.business_id)

    with served_widgets([a.business_id], red_flags=[EMERGENCY]) as client:
        token = new_session(client, a.business_id)
        first = send(client, token, BENIGN)
        second = send(client, token, EMERGENCY)

    assert (first.json()["turn"]["kind"], first.json()["turn"]["priority"]) == ("handoff", "normal")
    assert queued(first)["id"] == queued(second)["id"]  # the customer's answer is its own (S17)
    after = await rows(owner_engine, a.business_id)
    [stored] = added(before, after, "handoffs")
    assert (stored["priority"], stored["reason"], stored["count"]) == ("urgent", "safety", 2)
    assert stored["raised_at"] is not None and stored["status"] == "open"
    assert stored["trace_id"] == second.json()["turn"]["trace_id"]  # replaced by the raise
    assert stored["message_masked"] == mask_at_rest(EMERGENCY)


def ranked_handoff(
    business_id: str, session: UUID, priority: Priority, reason: HandoffReason, step: int
) -> NewHandoff:
    """A turn's hand-off whose every raised column names `step`, so a raise shows in each."""
    return NewHandoff(
        business_id=business_id,
        session_id=session,
        trace_id=secrets.token_hex(16),
        origin=Origin.TURN,
        reason=reason,
        priority=priority,
        intent=tuple(Intent)[step % len(Intent)],
        ui_language="en",
        guidance_shown=(f"line {step}",),
        message_masked=f"message {step}",
        retention_days=30,
    )


async def store_as_app(app_engine: AsyncEngine, handoff: NewHandoff) -> StoredHandoff:
    async with business_transaction(app_engine, handoff.business_id) as connection:
        return await store_handoff(connection, handoff)


RAISED_READ: Final = text(
    "SELECT priority, reason, origin, intent, trace_id, guidance_shown, message_masked, count,"
    " raised_at FROM handoffs WHERE business_id = :b AND id = :id"
)


async def test_at_equal_priority_a_more_serious_reason_raises_the_row_and_a_lesser_never_lowers(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # Z3 (a), amended 2026-11-25: Z3's two sequences (a high `not_covered_language` row then a
    # high red flag; an urgent `turn_timeout` row then an urgent red flag) on one session.
    a, _ = planted
    with served_widgets([a.business_id]) as client:
        token = new_session(client, a.business_id)
    session = await session_id(owner_engine, a.business_id, token)
    steps = [
        (Priority.HIGH, HandoffReason.NOT_COVERED_LANGUAGE, True),  # opens the row
        (Priority.HIGH, HandoffReason.SAFETY, True),  # the tie: safety above a system reason
        (Priority.HIGH, HandoffReason.NOT_COVERED_LANGUAGE, False),  # never lowered
        (Priority.HIGH, HandoffReason.OTHER, False),  # a decision is below safety
        (Priority.URGENT, HandoffReason.TURN_TIMEOUT, True),  # the priority decides first
        (Priority.URGENT, HandoffReason.SAFETY, True),  # the tie again, at urgent
        (Priority.URGENT, HandoffReason.GUARD_ERROR, False),
    ]
    kept: NewHandoff | None = None
    raised_at: object = None
    ids = set()
    for step, (priority, reason, raises_row) in enumerate(steps):
        handoff = ranked_handoff(a.business_id, session, priority, reason, step)
        stored = await store_as_app(app_engine, handoff)
        ids.add(stored.id)
        async with business_transaction(owner_engine, a.business_id) as connection:
            row = (
                await connection.execute(RAISED_READ, {"b": a.business_id, "id": stored.id})
            ).one()
        if raises_row:
            kept = handoff
        assert kept is not None
        assert (stored.priority, stored.reason, stored.count) == (
            kept.priority,
            kept.reason,
            step + 1,
        ), step
        assert tuple(row)[:8] == (
            kept.priority.value,
            kept.reason.value,
            "turn",
            None if kept.intent is None else kept.intent.value,
            kept.trace_id,
            list(kept.guidance_shown),
            kept.message_masked,
            step + 1,
        ), step
        if step == 0:
            assert row.raised_at is None
        elif raises_row:
            assert row.raised_at is not None and row.raised_at != raised_at, step
        else:
            assert row.raised_at == raised_at, step  # a hand-off that does not raise keeps it
        raised_at = row.raised_at
    assert len(ids) == 1  # one open row for the session throughout (S17)


async def test_every_priority_and_class_pair_ranks_in_postgres_as_the_store_says(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # Z3 (a): every ordered pair of LOWEST_FIRST, each in a savepoint rolled back, as the app role.
    a, _ = planted
    with served_widgets([a.business_id]) as client:
        token = new_session(client, a.business_id)
    session = await session_id(owner_engine, a.business_id, token)
    outcomes: dict[tuple[int, int], tuple[Priority, HandoffReason, int]] = {}
    async with business_transaction(app_engine, a.business_id) as connection:
        for reason in HandoffReason:
            classed = await connection.scalar(
                text(f"SELECT {reason_class_sql('CAST(:r AS text)')}"), {"r": reason.value}
            )
            assert classed == reason_class(reason).value, reason
        for low, (open_priority, open_class) in enumerate(LOWEST_FIRST):
            for high, (new_priority, new_class) in enumerate(LOWEST_FIRST):
                savepoint = await connection.begin_nested()
                try:
                    first = ranked_handoff(
                        a.business_id, session, open_priority, REPRESENTATIVE[open_class], 0
                    )
                    second = ranked_handoff(
                        a.business_id, session, new_priority, REPRESENTATIVE[new_class], 1
                    )
                    await store_handoff(connection, first)
                    stored = await store_handoff(connection, second)
                    outcomes[(low, high)] = (stored.priority, stored.reason, stored.count)
                finally:
                    await savepoint.rollback()
    after = await rows(owner_engine, a.business_id)

    for (low, high), outcome in outcomes.items():
        priority, rank = LOWEST_FIRST[max(low, high)]  # the higher rank, never lowered
        assert outcome == (priority, REPRESENTATIVE[rank], 2), (
            LOWEST_FIRST[low],
            LOWEST_FIRST[high],
        )
    assert len(outcomes) == len(LOWEST_FIRST) ** 2
    assert [row for row in after["handoffs"] if row["session_id"] == session] == []


async def test_two_concurrent_presses_are_one_row_with_one_id(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    with served_widgets([a.business_id]) as client:
        token = new_session(client, a.business_id)
        session = await session_id(owner_engine, a.business_id, token)
        before = await rows(owner_engine, a.business_id)
        async with business_transaction(owner_engine, a.business_id) as holder:
            # Hold the session's row lock: both presses' inserts wait on their foreign-key check
            # (the first) or on the first's index entry (the second), so both are in flight.
            await holder.execute(
                text("SELECT 1 FROM sessions WHERE business_id = :b AND id = :s FOR UPDATE"),
                {"b": a.business_id, "s": session},
            )
            racing = asyncio.gather(
                asyncio.to_thread(press, client, token), asyncio.to_thread(press, client, token)
            )
            # Both are at the lock (leo's PR 2 review, as M5's test), never a sleep.
            await until_waiting(owner_engine, 2)
        responses = await racing
        again = queued(press(client, token))

    answers = [queued(response) for response in responses]
    after = await rows(owner_engine, a.business_id)
    [stored] = added(before, after, "handoffs")
    assert {answer["id"] for answer in (*answers, again)} == {str(stored["id"])}
    assert (stored["origin"], stored["reason"], stored["priority"]) == (
        "button",
        "customer_asked",
        "normal",
    )
    assert (stored["count"], stored["message_masked"], stored["guidance_shown"]) == (1, None, [])
    assert stored["pressed_at"] is not None  # a button row records its press
    assert all(answer["follow_up"] is True for answer in (*answers, again))  # normal, asked
    assert added(before, after, "widget_turns") == []  # a press is not a turn


# ── Session X against session Y (S1, S4) ─────────────────────────────────────────────────────


async def test_a_session_reads_only_its_own_hand_off(planted: tuple[Planted, Planted]) -> None:
    a, b = planted
    with served_widgets([a.business_id, b.business_id], red_flags=[EMERGENCY]) as client:
        x, y = new_session(client, a.business_id), new_session(client, a.business_id)
        other = new_session(client, b.business_id)
        handoff_id = queued(send(client, x, EMERGENCY))["id"]
        own = status(client, x, handoff_id)
        same_business = status(client, y, handoff_id)
        other_business = status(client, other, handoff_id)

    assert own.status_code == 200, own.text
    assert own.json() == {
        "id": handoff_id,
        "status": "open",
        "reply": None,
        "replied_at": None,
        "follow_up": False,  # an urgent `safety` row: never polled (D11)
    }
    for refused in (same_business, other_business):
        assert (refused.status_code, refused.json()["error"]) == (404, "handoff_not_found")
        assert refused.json() == other_business.json()  # one answer, whoever asks


async def test_smuggled_identity_writes_nothing(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    before_a, before_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )

    with served_widgets([a.business_id, b.business_id], red_flags=[EMERGENCY]) as client:
        token = new_session(client, a.business_id)
        named = [
            client.post(
                "/v1/widget/messages",
                json={"message": EMERGENCY, field: value},
                headers=bearer(token),
            )
            for field, value in (("customer_ref", "cust-001"), ("business_id", b.business_id))
        ]
        header = send(client, token, EMERGENCY, **{BUSINESS_HEADER: b.business_id})

    assert [response.status_code for response in named] == [422, 422]
    assert all(b.business_id not in response.text for response in named)
    assert (header.status_code, header.json()["error"]) == (400, "business_header_refused")
    assert await rows(owner_engine, a.business_id) == before_a
    assert await rows(owner_engine, b.business_id) == before_b


# ── S14: emergencies need no Redis ───────────────────────────────────────────────────────────


async def test_with_redis_down_a_red_flag_on_an_existing_session_is_urgent_and_queued(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    with served_widgets([a.business_id]) as client:  # the session, while Redis is up
        token = new_session(client, a.business_id)
    before = await rows(owner_engine, a.business_id)

    down = FakeLimiter(limit="raise", budget="raise")
    with served_widgets([a.business_id], red_flags=[EMERGENCY], limiter=down) as client:
        response = send(client, token, EMERGENCY)
        asked = list(down.calls)
        refused = client.post(
            "/v1/widget/sessions",
            json={"business_id": a.business_id, "ui_language": "en"},
            headers=SAME_ORIGIN,
        )

    turn = response.json()["turn"]
    assert (turn["reason"], turn["priority"]) == ("safety", "urgent")
    assert asked == []  # the red-flag screen first (M7 G1): Redis was never asked
    handoff = queued(response)
    after = await rows(owner_engine, a.business_id)
    [stored] = added(before, after, "handoffs")
    assert handoff == {"id": str(stored["id"]), "queued": True, "follow_up": False}
    # Session creation fails closed: the typed 503 (the widget shows its cached guidance, S16).
    assert (refused.status_code, refused.json()["error"]) == (503, "limits_unavailable")
    assert [call.operation for call in down.calls] == ["admit"]


# ── PR 2b: turn rows and feedback (D6 (a)) ───────────────────────────────────────────────────

TURN_COLUMNS: Final = frozenset(
    {
        "business_id",
        "id",
        "session_id",
        "trace_id",
        "kind",
        "reason",
        "priority",
        "intent",
        "answer_outcome",
        "channel",
        "ui_language",
        "server_latency_ms",
        "created_at",
    }
)
"""A turn row's columns: metadata only, no text (D6 (a))."""


def rate(client: TestClient, token: str, trace_id: str, rating: str = "down") -> httpx2.Response:
    return client.post(
        "/v1/widget/feedback",
        json={"trace_id": trace_id, "rating": rating},
        headers=bearer(token),
    )


async def test_a_session_rates_its_own_turn_once_and_the_rating_lands_in_its_business_only(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    before_a, before_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )

    with served_widgets([a.business_id, b.business_id]) as client:
        token = new_session(client, a.business_id)
        turn = send(client, token, BENIGN).json()["turn"]
        rated = rate(client, token, turn["trace_id"], "down")
        again = rate(client, token, turn["trace_id"], "up")

    assert rated.status_code == 201, rated.text
    assert rated.json() == {"trace_id": turn["trace_id"], "rating": "down"}
    assert (again.status_code, again.json()["error"]) == (409, "already_rated")
    after_a, after_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )
    assert after_b == before_b
    session = await session_id(owner_engine, a.business_id, token)
    # One turn row, no text, under the trace id the body carried: the id the rating names.
    [row] = added(before_a, after_a, "widget_turns")
    assert set(row) == TURN_COLUMNS
    assert (row["session_id"], row["trace_id"]) == (session, turn["trace_id"])
    [stored] = added(before_a, after_a, "turn_feedback")
    assert (stored["session_id"], stored["trace_id"], stored["rating"]) == (
        session,
        turn["trace_id"],
        "down",  # the first rating stands; the second changed nothing
    )


async def test_no_session_rates_another_sessions_turn_or_one_that_does_not_exist(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    with served_widgets([a.business_id, b.business_id]) as client:
        x, y = new_session(client, a.business_id), new_session(client, a.business_id)
        other = new_session(client, b.business_id)
        trace_id = send(client, x, BENIGN).json()["turn"]["trace_id"]
        before_a, before_b = (
            await rows(owner_engine, a.business_id),
            await rows(owner_engine, b.business_id),
        )
        refusals = [
            rate(client, y, trace_id),  # session Y, same business (S4)
            rate(client, other, trace_id),  # another business (S1)
            rate(client, x, "5eed" * 8),  # a made-up trace id
            rate(client, y, BAIT_TRACE_ID),  # the planted bait session's turn, in both businesses
        ]
        after_a, after_b = (
            await rows(owner_engine, a.business_id),
            await rows(owner_engine, b.business_id),
        )
        own = rate(client, x, trace_id)

    assert {(r.status_code, r.content) for r in refusals} == {(404, refusals[0].content)}
    assert refusals[0].json()["error"] == "turn_not_found"
    assert (after_a, after_b) == (before_a, before_b)
    assert own.status_code == 201  # the positive control: X's own turn


async def test_red_proof_the_database_refuses_a_rating_of_another_sessions_turn(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    # Without the route's own check, the rating's composite key into `widget_turns` (business,
    # session and trace id) still refuses X's turn rated under Y's session (migration 0009).
    a, _ = planted
    with served_widgets([a.business_id]) as client:
        x, y = new_session(client, a.business_id), new_session(client, a.business_id)
        trace_id = send(client, x, BENIGN).json()["turn"]["trace_id"]
    y_session = await session_id(owner_engine, a.business_id, y)
    params = {
        "business_id": a.business_id,
        "session_id": y_session,
        "trace_id": trace_id,
        "rating": "up",
    }

    with pytest.raises(IntegrityError) as raised:
        async with business_transaction(app_engine, a.business_id) as connection:
            await connection.execute(INSERT_RATING, params)

    assert getattr(raised.value.orig, "sqlstate", None) == "23503"


async def test_a_body_naming_anyone_or_the_header_rates_nothing(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    with served_widgets([a.business_id, b.business_id]) as client:
        token = new_session(client, a.business_id)
        trace_id = send(client, token, BENIGN).json()["turn"]["trace_id"]
        before = await rows(owner_engine, a.business_id)
        named = [
            client.post(
                "/v1/widget/feedback",
                json={"trace_id": trace_id, "rating": "up", field: value},
                headers=bearer(token),
            )
            for field, value in (
                ("customer_ref", "cust-001"),
                ("session_id", "zqxmarker"),
                ("business_id", b.business_id),
            )
        ]
        header = client.post(
            "/v1/widget/feedback",
            json={"trace_id": trace_id, "rating": "up"},
            headers={**bearer(token), BUSINESS_HEADER: b.business_id},
        )

    assert [response.status_code for response in named] == [422, 422, 422]
    assert all(b.business_id not in r.text and "zqxmarker" not in r.text for r in named)
    assert (header.status_code, header.json()["error"]) == (400, "business_header_refused")
    assert await rows(owner_engine, a.business_id) == before


async def test_a_staff_sessions_token_as_bearer_is_the_one_401_on_confirm_and_feedback(
    planted: tuple[Planted, Planted],
) -> None:
    a, _ = planted
    staff = bait_token(a.business_id, Purpose.STAFF)
    with served_widgets([a.business_id]) as client:
        answers = [
            client.post(
                f"/v1/widget/actions/{BAIT_TRACE_ID[:8]}-0000-4000-8000-000000000001/confirm",
                json={"confirm_token": "Q7zx9TOKENtokenTOKEN_-"},
                headers=bearer(staff),
            ),
            rate(client, staff, BAIT_TRACE_ID),
        ]
        own = rate(client, bait_token(a.business_id, Purpose.WIDGET), BAIT_TRACE_ID)

    for response in answers:
        assert (response.status_code, response.json()) == (
            401,
            {"detail": UNAUTHORIZED, "error": "unauthorized"},
        )
    # The positive control: the bait widget session is live, and its turn has its rating already.
    assert (own.status_code, own.json()["error"]) == (409, "already_rated")
