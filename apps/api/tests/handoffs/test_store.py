"""The hand-off store's statements and its Postgres store, without a database (M8 PR 2a and 2b;
D6 (a), S17). Unit: `ScriptedDatabase` (`tests/auth/fakes.py`) answers each statement. The same
statements against Postgres, S17's concurrency included: `tests/canary/test_widget_routes.py`;
their columns against migration 0009: `tests/test_migrations.py`.

S17's rank (amended 2026-11-25, shesha, Z3 (a)): every (priority, reason class) pair against
every other, in an order written out here by hand, through `rises` with every reason of each
class, and through the in-memory store (`RecordingHandoffStore`); the SQL's text is pinned
above, and the same pairs run against Postgres in `tests/canary/test_widget_routes.py`.
"""

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

import pytest
from sqlalchemy import TextClause
from sqlalchemy.dialects import postgresql

from tests.auth.fakes import ScriptedConnection, ScriptedDatabase, as_connection
from tests.widget.guarded import RecordingHandoffStore
from vectapilot_api.agent.responses import HandoffReason
from vectapilot_api.handoffs.store import (
    HANDOFF_STATE,
    INSERT_RATING,
    INSERT_TURN,
    OWN_TURN,
    PRESS_BUTTON,
    RAISED_COLUMNS,
    RISES,
    STORE_HANDOFF,
    HandoffState,
    HandoffStatus,
    NewHandoff,
    Origin,
    PostgresHandoffStore,
    Rated,
    Rating,
    ReasonClass,
    StoredHandoff,
    TurnKind,
    TurnRating,
    TurnRow,
    handoff_params,
    handoff_state,
    insert_turn,
    press_button,
    rate_turn,
    rating_params,
    reason_class,
    rises,
    store_handoff,
    turn_params,
)
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.tooleval.metrics import (
    DECISION_HANDOFF_REASONS,
    INFRASTRUCTURE_HANDOFF_REASONS,
)

BUSINESS: Final = "paws-and-co"
SESSION: Final = UUID("0f0e0d0c-0b0a-4908-8706-050403020100")
HANDOFF: Final = UUID("4a4d0ff0-0000-4000-8000-0000000000b1")
TRACE: Final = "a" * 32


def handoff(**changes: Any) -> NewHandoff:
    fields: dict[str, Any] = {
        "business_id": BUSINESS,
        "session_id": SESSION,
        "trace_id": TRACE,
        "origin": Origin.TURN,
        "reason": HandoffReason.SAFETY,
        "priority": Priority.URGENT,
        "intent": Intent.EMERGENCY,
        "ui_language": "en",
        "guidance_shown": ("Call the emergency line.", "Do not wait for a reply in the chat."),
        "message_masked": "My dog ate rat poison, call [PHONE]",
        "retention_days": 30,
    }
    return NewHandoff(**(fields | changes))


def turn(**changes: Any) -> TurnRow:
    fields: dict[str, Any] = {
        "business_id": BUSINESS,
        "session_id": SESSION,
        "trace_id": TRACE,
        "kind": TurnKind.HANDOFF,
        "reason": HandoffReason.SAFETY,
        "priority": Priority.URGENT,
        "ui_language": "en",
        "server_latency_ms": 12,
    }
    return TurnRow(**(fields | changes))


def returned(priority: str = "urgent", count: int = 1, reason: str = "safety") -> SimpleNamespace:
    return SimpleNamespace(id=HANDOFF, handoff_count=count, priority=priority, reason=reason)


def binds(statement: TextClause) -> set[str]:
    """The bind names of a statement, as asyncpg's dialect compiles it."""
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    return set(statement.compile(dialect=dialect).params)


# ── The statements ───────────────────────────────────────────────────────────────────────────


def test_every_statement_binds_exactly_the_parameters_the_store_passes() -> None:
    assert binds(STORE_HANDOFF) == binds(PRESS_BUTTON) == set(handoff_params(handoff()))
    assert binds(INSERT_TURN) == set(turn_params(turn()))
    assert binds(HANDOFF_STATE) == {"business_id", "session_id", "handoff_id"}


def test_s17s_statement_is_one_insert_on_the_open_hand_off_index_raising_only_upwards() -> None:
    sql = STORE_HANDOFF.text

    assert sql.startswith("INSERT INTO handoffs AS h ")
    assert " ON CONFLICT (business_id, session_id) WHERE status <> 'resolved' DO UPDATE SET " in sql
    assert "count = h.count + 1" in sql
    assert sql.endswith(" RETURNING id, count AS handoff_count, priority, reason")
    # Z3 (a), amended 2026-11-25: a row comparison, the priority first, then the reason's class.
    rises = (
        "(array_position(ARRAY['normal', 'high', 'urgent'], EXCLUDED.priority),"
        " CASE WHEN EXCLUDED.reason IN ('safety') THEN 2"
        " WHEN EXCLUDED.reason IN ('customer_asked', 'other', 'routed') THEN 1 ELSE 0 END)"
        " > (array_position(ARRAY['normal', 'high', 'urgent'], h.priority),"
        " CASE WHEN h.reason IN ('safety') THEN 2"
        " WHEN h.reason IN ('customer_asked', 'other', 'routed') THEN 1 ELSE 0 END)"
    )
    assert rises == RISES
    for column in RAISED_COLUMNS:
        assert f"{column} = CASE WHEN {rises} THEN EXCLUDED.{column} ELSE h.{column} END" in sql
    assert f"raised_at = CASE WHEN {rises} THEN now() ELSE h.raised_at END" in sql
    # A raise never touches the session, the staff side or the retention (a claimed row stays
    # claimed, S17).
    updated = sql.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    for column in ("status", "claimed", "resolved", "staff_label", "reply", "retention"):
        assert not re.search(rf"\b{column}\w* =", updated), column


def test_a_press_changes_nothing_on_an_open_row() -> None:
    updated = PRESS_BUTTON.text.split("DO UPDATE SET", 1)[1]

    # Only the first press's time (noah's PR 2 review): `count`, the reason and the message stay.
    assert updated == (
        " count = h.count, pressed_at = coalesce(h.pressed_at, now())"
        " RETURNING id, count AS handoff_count, priority, reason"
    )


def test_the_status_read_names_the_business_the_session_and_expiry_itself() -> None:
    sql = HANDOFF_STATE.text

    for clause in (
        "business_id = :business_id",
        "id = :handoff_id",
        "session_id = :session_id",
        "expires_at > now()",
    ):
        assert clause in sql
    assert "message_masked" not in sql and "guidance_shown" not in sql  # never to the customer


def test_each_read_joins_its_clauses_with_and_only() -> None:
    # S1, S17 (mutation probe, PR 2): an OR keeps each clause's text, so the `in` checks above
    # still pass, but the read then returns another session's hand-off or turn.
    assert HANDOFF_STATE.text.split(" WHERE ", 1)[1] == (
        "business_id = :business_id AND id = :handoff_id AND session_id = :session_id"
        " AND expires_at > now()"
    )
    assert OWN_TURN.text.split(" WHERE ", 1)[1] == (
        "business_id = :business_id AND session_id = :session_id AND trace_id = :trace_id"
    )


# ── S17's rank (amended 2026-11-25, shesha, Z3 (a)) ───────────────────────────────────────────

LOWEST_FIRST: Final = (
    (Priority.NORMAL, ReasonClass.SYSTEM),
    (Priority.NORMAL, ReasonClass.DECISION),
    (Priority.NORMAL, ReasonClass.SAFETY),
    (Priority.HIGH, ReasonClass.SYSTEM),
    (Priority.HIGH, ReasonClass.DECISION),
    (Priority.HIGH, ReasonClass.SAFETY),
    (Priority.URGENT, ReasonClass.SYSTEM),
    (Priority.URGENT, ReasonClass.DECISION),
    (Priority.URGENT, ReasonClass.SAFETY),
)
"""Every (priority, reason class) pair, least serious first, written out by hand: the priority
decides, the class only breaks a tie (safety > decision > system)."""
CLASS_REASONS: Final = {
    ReasonClass.SYSTEM: tuple(sorted(INFRASTRUCTURE_HANDOFF_REASONS)),
    ReasonClass.DECISION: tuple(sorted(DECISION_HANDOFF_REASONS - {"safety"})),
    ReasonClass.SAFETY: ("safety",),
}
"""Each class's reasons, from `tooleval.metrics` (the one classification), not from the store."""
REPRESENTATIVE: Final = {
    ReasonClass.SYSTEM: HandoffReason.NOT_COVERED_LANGUAGE,
    ReasonClass.DECISION: HandoffReason.ROUTED,
    ReasonClass.SAFETY: HandoffReason.SAFETY,
}
PAIRS: Final = [(old, new) for old in LOWEST_FIRST for new in LOWEST_FIRST]
PAIR_IDS: Final = [f"{o[0].value}-{o[1].name}->{n[0].value}-{n[1].name}" for o, n in PAIRS]

Pair = tuple[Priority, ReasonClass]


def test_the_reason_classes_are_toolevals_sets_with_safety_apart() -> None:
    for rank, reasons in CLASS_REASONS.items():
        assert {r for r in HandoffReason if reason_class(r) is rank} == {
            HandoffReason(value) for value in reasons
        }, rank.name
    assert all(reason_class(reason) is rank for rank, reason in REPRESENTATIVE.items())


@pytest.mark.parametrize(("open_pair", "new_pair"), PAIRS, ids=PAIR_IDS)
def test_a_hand_off_raises_the_open_row_exactly_when_its_pair_ranks_higher(
    open_pair: Pair, new_pair: Pair
) -> None:
    expected = LOWEST_FIRST.index(new_pair) > LOWEST_FIRST.index(open_pair)
    for open_reason in CLASS_REASONS[open_pair[1]]:
        for new_reason in CLASS_REASONS[new_pair[1]]:
            over = (open_pair[0], HandoffReason(open_reason))
            raised = rises(new_pair[0], HandoffReason(new_reason), over=over)
            assert raised is expected, (open_reason, new_reason)


def ranked(priority: Priority, rank: ReasonClass, trace: str) -> NewHandoff:
    return handoff(
        priority=priority,
        reason=REPRESENTATIVE[rank],
        trace_id=trace,
        intent=None,
        guidance_shown=(f"line {trace[0]}",),
        message_masked=f"message {trace[0]}",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(("open_pair", "new_pair"), PAIRS, ids=PAIR_IDS)
async def test_the_in_memory_store_keeps_the_higher_pair_and_never_lowers_the_row(
    open_pair: Pair, new_pair: Pair
) -> None:
    store = RecordingHandoffStore()
    first, second = ranked(*open_pair, "a" * 32), ranked(*new_pair, "b" * 32)

    opened = await store.record_turn(turn(), first)
    stored = await store.record_turn(turn(trace_id="b" * 32), second)

    raised = LOWEST_FIRST.index(new_pair) > LOWEST_FIRST.index(open_pair)
    kept = second if raised else first
    assert opened is not None and stored is not None
    assert (stored.id, stored.count) == (opened.id, 2)
    assert (stored.priority, stored.reason) == (kept.priority, kept.reason)
    # The whole row moves with the rank: its trace id, guidance and message are the kept one's.
    assert store.open[(BUSINESS, SESSION)].handoff == kept
    assert store.written == ([first, second] if raised else [first])


@pytest.mark.parametrize(
    ("open_row", "new", "raised"),
    [
        # Z3's examples: a red flag after a system row of the same priority raises it.
        ((Priority.HIGH, HandoffReason.NOT_COVERED_LANGUAGE), (Priority.HIGH, "safety"), True),
        ((Priority.URGENT, HandoffReason.GUARD_ERROR), (Priority.URGENT, "safety"), True),
        ((Priority.URGENT, HandoffReason.TURN_TIMEOUT), (Priority.URGENT, "safety"), True),
        ((Priority.NORMAL, HandoffReason.NOT_CONFIGURED), (Priority.NORMAL, "routed"), True),
        # Never lower: a lower class at the same priority, the same pair, a lower priority.
        ((Priority.HIGH, HandoffReason.SAFETY), (Priority.HIGH, "not_covered_language"), False),
        ((Priority.URGENT, HandoffReason.SAFETY), (Priority.URGENT, "other"), False),
        ((Priority.HIGH, HandoffReason.OTHER), (Priority.HIGH, "customer_asked"), False),
        ((Priority.URGENT, HandoffReason.TURN_TIMEOUT), (Priority.HIGH, "safety"), False),
        # The priority decides first: a higher one raises whatever its class.
        ((Priority.HIGH, HandoffReason.SAFETY), (Priority.URGENT, "turn_timeout"), True),
    ],
)
def test_z3s_sequences(
    open_row: tuple[Priority, HandoffReason], new: tuple[Priority, str], raised: bool
) -> None:
    assert rises(new[0], HandoffReason(new[1]), over=open_row) is raised


# ── The rows' rules ──────────────────────────────────────────────────────────────────────────


def test_a_button_hand_off_is_customer_asked() -> None:
    with pytest.raises(ValueError, match="customer_asked"):
        handoff(origin=Origin.BUTTON, reason=HandoffReason.SAFETY)
    assert handoff(origin=Origin.BUTTON, reason=HandoffReason.CUSTOMER_ASKED)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": TurnKind.REPLY},
        {"reason": None},
        {"priority": None},
        {"kind": TurnKind.REPLY, "reason": None},
        {"server_latency_ms": -1},
        {"kind": TurnKind.REPLY, "reason": None, "priority": None, "intent": Intent.QUESTION},
        {"kind": TurnKind.ANSWER, "reason": None, "priority": None},
        {"kind": TurnKind.REPLY, "reason": None, "priority": None, "answer_outcome": "answered"},
    ],
)
def test_a_turn_row_has_a_reason_and_priority_exactly_when_it_hands_off(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        turn(**changes)
    assert turn(kind=TurnKind.REPLY, reason=None, priority=None)


# ── One connection ───────────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_store_handoff_sends_the_rows_values_and_reads_the_open_row() -> None:
    connection = ScriptedConnection([[returned("urgent", 3)]])

    stored = await store_handoff(as_connection(connection), handoff(intent=None))

    assert stored == StoredHandoff(HANDOFF, 3, Priority.URGENT, HandoffReason.SAFETY)
    [(text, params)] = connection.executed
    assert text == STORE_HANDOFF.text
    assert params == {
        "business_id": BUSINESS,
        "session_id": SESSION,
        "trace_id": TRACE,
        "origin": "turn",
        "reason": "safety",
        "priority": "urgent",
        "intent": None,
        "channel": "web",
        "ui_language": "en",
        "guidance_shown": ["Call the emergency line.", "Do not wait for a reply in the chat."],
        "message_masked": "My dog ate rat poison, call [PHONE]",
        "retention_days": 30,
    }


@pytest.mark.anyio
async def test_press_button_takes_only_a_button_hand_off() -> None:
    connection = ScriptedConnection([[returned("high", 2, "other")]])
    pressed = handoff(
        origin=Origin.BUTTON,
        reason=HandoffReason.CUSTOMER_ASKED,
        priority=Priority.NORMAL,
        intent=None,
        guidance_shown=(),
        message_masked=None,
    )

    stored = await press_button(as_connection(connection), pressed)

    assert stored == StoredHandoff(HANDOFF, 2, Priority.HIGH, HandoffReason.OTHER)  # the open row
    assert connection.executed[0][0] == PRESS_BUTTON.text
    with pytest.raises(ValueError, match="button"):
        await press_button(as_connection(connection), handoff())


@pytest.mark.anyio
async def test_insert_turn_and_the_status_read() -> None:
    replied = datetime(2026, 11, 24, tzinfo=UTC)
    state = SimpleNamespace(
        id=HANDOFF,
        status="resolved",
        reply="Done.",
        replied_at=replied,
        priority="high",
        reason="other",
    )
    connection = ScriptedConnection([[], [state], []])

    answered = turn(kind=TurnKind.ANSWER, reason=None, priority=None, answer_outcome="dont_know")
    await insert_turn(as_connection(connection), answered)
    found = await handoff_state(as_connection(connection), BUSINESS, SESSION, HANDOFF)
    missing = await handoff_state(as_connection(connection), BUSINESS, SESSION, HANDOFF)

    sent = connection.executed[0][1]
    assert (sent["kind"], sent["answer_outcome"]) == ("answer", "dont_know")
    assert (sent["reason"], sent["priority"], sent["intent"]) == (None, None, None)
    assert found == HandoffState(
        HANDOFF, HandoffStatus.RESOLVED, "Done.", replied, Priority.HIGH, HandoffReason.OTHER
    )
    assert found.follow_up is True
    assert missing is None


# ── The Postgres store: one business transaction per call ────────────────────────────────────


@pytest.mark.anyio
async def test_a_turn_and_its_hand_off_are_written_in_one_transaction_hand_off_first() -> None:
    database = ScriptedDatabase(ScriptedConnection([[returned()], []]))

    stored = await PostgresHandoffStore(database).record_turn(turn(), handoff())

    assert stored == StoredHandoff(HANDOFF, 1, Priority.URGENT, HandoffReason.SAFETY)
    assert database.opened == [BUSINESS]
    assert [text for text, _ in database.connection.executed] == [
        STORE_HANDOFF.text,
        INSERT_TURN.text,
    ]


@pytest.mark.anyio
async def test_a_turn_without_a_hand_off_writes_its_row_only() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    row = turn(kind=TurnKind.REPLY, reason=None, priority=None)

    assert await PostgresHandoffStore(database).record_turn(row, None) is None
    assert [text for text, _ in database.connection.executed] == [INSERT_TURN.text]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [{"session_id": UUID(int=1)}, {"business_id": "sunrise-clinic"}],
    ids=["another session", "another business"],
)
async def test_a_turn_and_a_hand_off_of_two_sessions_are_refused_before_any_sql(
    changes: dict[str, Any],
) -> None:
    database = ScriptedDatabase()

    with pytest.raises(ValueError, match="one session"):
        await PostgresHandoffStore(database).record_turn(turn(), handoff(**changes))
    assert database.opened == []


@pytest.mark.anyio
async def test_the_press_and_the_status_read_each_open_the_businesss_transaction() -> None:
    database = ScriptedDatabase(ScriptedConnection([[returned("normal")], []]))
    store = PostgresHandoffStore(database)
    pressed = handoff(
        origin=Origin.BUTTON,
        reason=HandoffReason.CUSTOMER_ASKED,
        priority=Priority.NORMAL,
        intent=None,
        guidance_shown=(),
        message_masked=None,
    )

    assert (await store.press_button(pressed)).id == HANDOFF
    assert await store.state(BUSINESS, SESSION, HANDOFF) is None
    assert database.opened == [BUSINESS, BUSINESS]


@pytest.mark.anyio
async def test_a_failing_transaction_propagates_for_the_route_to_turn_into_queued_false() -> None:
    database = ScriptedDatabase(fail=OSError("down"))

    with pytest.raises(OSError):
        await PostgresHandoffStore(database).record_turn(turn(), handoff())


# ── PR 2b: a rating of one of the session's own turns ────────────────────────────────────────


def rating(value: Rating = Rating.UP) -> TurnRating:
    return TurnRating(business_id=BUSINESS, session_id=SESSION, trace_id=TRACE, rating=value)


def test_the_rating_statements_bind_the_session_the_turn_and_nothing_else() -> None:
    assert binds(OWN_TURN) == {"business_id", "session_id", "trace_id"}
    assert binds(INSERT_RATING) == set(rating_params(rating())) == binds(OWN_TURN) | {"rating"}
    for clause in (
        "business_id = :business_id",
        "session_id = :session_id",
        "trace_id = :trace_id",
    ):
        assert clause in OWN_TURN.text
    # One rating per turn, by the table's own key, and never an update (no UPDATE grant either).
    assert INSERT_RATING.text.endswith(
        " ON CONFLICT (business_id, trace_id) DO NOTHING RETURNING id"
    )
    assert "UPDATE" not in INSERT_RATING.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("script", "expected", "statements"),
    [
        ([[returned()], [returned()]], Rated.RECORDED, 2),
        ([[returned()], []], Rated.ALREADY_RATED, 2),
        ([[]], Rated.NOT_FOUND, 1),
    ],
    ids=["recorded", "already rated", "not found"],
)
async def test_rate_turn_reads_the_sessions_own_turn_before_any_insert(
    script: list[list[Any]], expected: Rated, statements: int
) -> None:
    connection = ScriptedConnection(list(script))

    assert await rate_turn(as_connection(connection), rating(Rating.DOWN)) is expected

    sent = connection.executed
    assert len(sent) == statements
    assert sent[0] == (
        OWN_TURN.text,
        {"business_id": BUSINESS, "session_id": SESSION, "trace_id": TRACE},
    )
    if statements == 2:
        assert sent[1] == (INSERT_RATING.text, rating_params(rating(Rating.DOWN)))
        assert sent[1][1]["rating"] == "down"


@pytest.mark.anyio
async def test_a_rating_opens_its_businesss_transaction_and_propagates_a_failure() -> None:
    database = ScriptedDatabase(ScriptedConnection([[]]))
    assert await PostgresHandoffStore(database).rate(rating()) is Rated.NOT_FOUND
    assert database.opened == [BUSINESS]

    with pytest.raises(OSError):
        await PostgresHandoffStore(ScriptedDatabase(fail=OSError("down"))).rate(rating())
