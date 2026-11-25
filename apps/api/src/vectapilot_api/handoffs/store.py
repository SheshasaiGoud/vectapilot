"""The statements on migration 0009's tables (M8 PR 2; D6 (a), S11, S14, S17).

Every statement runs inside the business's RLS transaction
(`Database.business_transaction(business_id)`) and also names the business itself
(`business_id = :business_id`, or the business in every inserted row), so on a connection where
RLS cannot help (the owner's) the statement alone keeps another business out: the canary suite
runs the read on the owner's connection too, with the same id planted in two businesses.

**S17, one statement.** `STORE_HANDOFF` inserts the session's hand-off, or, when the session
already has an open one (the partial unique index `handoffs_one_open_per_session`), updates that
row in the same statement: `count` plus one, and when the new hand-off ranks higher, the
priority, reason, origin, intent, trace id, guidance and message replaced and `raised_at` set.
**The rank** (amended 2026-11-25, shesha, Z3 (a)) is the priority, then the reason's class
(`ReasonClass`: `safety` above a decision above a system reason), compared as a pair: at equal
priority a `safety` or decision hand-off raises a system row (a red flag after a `turn_timeout`
or a `not_covered_language` row is labelled and never filtered as "system"), and a lower priority
never raises, whatever its class. The rank only ever rises, so a lower class never lowers the
row. The status and the staff columns are never touched, so a claimed row stays claimed and
shows the raise. `INSERT ... ON CONFLICT ... DO UPDATE` is atomic under concurrency: two hand-offs
of one session in parallel give one row with `count` 2, never two rows and never an error.
**"Talk to a person"** (`PRESS_BUTTON`) is the same insert with an update that only records the
first press (`pressed_at = coalesce(h.pressed_at, now())`, noah's PR 2 review): it returns the
open row's id whatever made it and changes nothing else on it, so pressing twice, or in parallel,
gives one row and the same id (idempotent per session) [proposal: a press does not count as
another hand-off]. A new row records its press itself (`pressed_at` is set when its origin is
`button`), and a later raise keeps `pressed_at`, so "the customer asked for a person" survives
both an earlier turn's row and a later emergency.

**Both answers carry the open row's priority and reason** (zara's PR 2 review): the widget's
"a person will reply" and its polling depend on the row the customer is attached to, which may be
an earlier urgent or `safety` hand-off (S17), never on the turn or press alone. `follow_up`
(`invites_follow_up`) is computed here, once, and fails closed (D11, S8).

**A turn's row and its hand-off commit together** (`record_turn`): one transaction, the hand-off
first. `queued` (the widget's "a person will reply", S8) is true only when that transaction
committed, so the row exists; any failure before the commit leaves neither.

**A rating names one of the session's own turns** (`rate_turn`, PR 2b, D6 (a)): one transaction,
two statements. `OWN_TURN` reads the turn by business, session and trace id together: another
session's turn, in the same business or not, and a made-up trace id are all simply not there
(one answer, `Rated.NOT_FOUND`). `INSERT_RATING` then inserts the rating, or does nothing when
the turn already has one (`UNIQUE (business_id, trace_id)`, `ON CONFLICT ... DO NOTHING`: two
ratings in parallel give one row, the second `Rated.ALREADY_RATED`, never an error). Migration
0009's foreign key `(business_id, session_id, trace_id)` into `widget_turns` is the database's own
copy of the first rule.

**Clocks.** `created_at`, `raised_at` and the generated `expires_at` come from the database's
`now()` (P5). The status read writes `expires_at > now()` itself: the app role's restrictive policy
hides an expired row as well, but the owner's connection has no policy.

Nothing here logs; no statement's text or parameters ever reach a log line (the masked message
included: it is customer text, masked or not).
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Final, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from vectapilot_api.agent.responses import HandoffReason
from vectapilot_api.answer.types import AnswerOutcome
from vectapilot_api.auth.store import Channel
from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent


class Origin(StrEnum):
    """Migration 0009's `handoffs.origin`: a turn the guards or the service handed off, or the
    customer's "talk to a person" (a button, never an AI decision: PR 3's metric leaves it out)."""

    TURN = "turn"
    BUTTON = "button"


class HandoffStatus(StrEnum):
    """Migration 0009's `handoffs.status` (PR 3 claims and resolves)."""

    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"


class TurnKind(StrEnum):
    """Migration 0009's `widget_turns.kind`: `/v1/agent`'s four response kinds."""

    REPLY = "reply"
    CONFIRMATION_REQUIRED = "confirmation_required"
    HANDOFF = "handoff"
    ANSWER = "answer"


class Rating(StrEnum):
    """Migration 0009's `turn_feedback.rating`: the customer's thumbs up or down on one turn."""

    UP = "up"
    DOWN = "down"


def invites_follow_up(priority: Priority, reason: HandoffReason) -> bool:
    """D11, S8: whether the widget may show the generic follow-up ("a person will reply") and poll
    a stored hand-off. Never for an urgent one, and never for a `safety` one at any priority: both
    safety files end with "Do not wait for a reply in the chat." (zara's PR 2 review)."""
    return priority is not Priority.URGENT and reason is not HandoffReason.SAFETY


class Rated(StrEnum):
    """What one rating did (`rate_turn`)."""

    RECORDED = "recorded"
    NOT_FOUND = "not_found"
    """No turn of this session has the trace id: another session's turn, in the same business or
    not, and a made-up id, one answer (the route's one 404)."""
    ALREADY_RATED = "already_rated"
    """The session's own turn has its one rating already (D6 (a): one rating per turn)."""


RISING_PRIORITIES: Final = (Priority.NORMAL, Priority.HIGH, Priority.URGENT)
"""Least urgent first: `array_position` over it ranks a priority (migration 0009's `RISING`)."""


@dataclass(frozen=True)
class NewHandoff:
    """A hand-off to store or raise for one session (S17). `message_masked` comes from
    `at_rest.mask_at_rest` only (S11): None when there is none to keep."""

    business_id: str
    session_id: UUID
    trace_id: str
    origin: Origin
    reason: HandoffReason
    priority: Priority
    intent: Intent | None
    ui_language: str
    guidance_shown: tuple[str, ...]
    message_masked: str | None
    retention_days: int
    channel: Channel = Channel.WEB

    def __post_init__(self) -> None:
        if self.origin is Origin.BUTTON and self.reason is not HandoffReason.CUSTOMER_ASKED:
            raise ValueError("a button hand-off is customer_asked")


@dataclass(frozen=True)
class TurnRow:
    """One widget turn's metadata (D6 (a)): no text. A hand-off's reason, priority and intent; an
    answer's outcome (`answered`, `dont_know`, `handoff`: noah's PR 2 review, so §6's "resolved"
    and "rated n of m answered turns" can tell an answer from an abstention); else none of them."""

    business_id: str
    session_id: UUID
    trace_id: str
    kind: TurnKind
    reason: HandoffReason | None
    priority: Priority | None
    ui_language: str
    server_latency_ms: int
    """From when the route started (before the session lookup) to the response built, the store
    write and serialization excluded (`widget.messages`)."""
    intent: Intent | None = None
    """A hand-off turn's intent (`HandoffResponse.intent`, set by the router when it is on): the
    hand-off row's own is replaced by S17's raise, so the turn row keeps each turn's."""
    answer_outcome: AnswerOutcome | None = None
    channel: Channel = Channel.WEB

    def __post_init__(self) -> None:
        handoff = self.kind is TurnKind.HANDOFF
        if handoff != (self.reason is not None) or handoff != (self.priority is not None):
            raise ValueError("a turn has a reason and a priority exactly when it is a hand-off")
        if self.intent is not None and not handoff:
            raise ValueError("only a hand-off turn has an intent")
        if (self.kind is TurnKind.ANSWER) != (self.answer_outcome is not None):
            raise ValueError("a turn has an answer outcome exactly when it is an answer")
        if self.server_latency_ms < 0:
            raise ValueError("a latency is at least zero")


@dataclass(frozen=True)
class TurnRating:
    """One session's rating of one of its own turns (D6 (a)), named by the turn's trace id (the
    one the turn's body and its turn row carry)."""

    business_id: str
    session_id: UUID
    trace_id: str
    rating: Rating


@dataclass(frozen=True)
class StoredHandoff:
    """The session's open hand-off after a store or a press: its id, its `count`, its priority
    and its reason (whose rank only ever rose), as they are on the row now (S17)."""

    id: UUID
    count: int
    priority: Priority
    reason: HandoffReason

    @property
    def follow_up(self) -> bool:
        """D11: whether the widget may say "a person will reply" and poll (module docstring)."""
        return invites_follow_up(self.priority, self.reason)


@dataclass(frozen=True)
class HandoffState:
    """What a session may read of its own hand-off (`GET /v1/widget/handoffs/{id}`): its status
    and the staff reply (D7 (a)), never the masked message, the guidance or a staff member. Its
    priority and reason are read for `follow_up` only and never sent."""

    id: UUID
    status: HandoffStatus
    reply: str | None
    replied_at: datetime | None
    priority: Priority
    reason: HandoffReason

    @property
    def follow_up(self) -> bool:
        """D11: whether the widget may keep polling it (module docstring)."""
        return invites_follow_up(self.priority, self.reason)


class ReasonClass(IntEnum):
    """S17's tie-break at equal priority (amended 2026-11-25, shesha, Z3 (a)), least serious
    first: a system reason (the system fell back, nobody decided), a decision (the model, the
    router or the customer decided), `safety`. The classes are `tooleval.metrics`' two sets with
    `safety` taken out of the decisions (tested); imported from there they would pull the eval
    harness into the API."""

    SYSTEM = 0
    DECISION = 1
    SAFETY = 2


SAFETY_REASONS: Final[frozenset[HandoffReason]] = frozenset({HandoffReason.SAFETY})
DECISION_REASONS: Final[frozenset[HandoffReason]] = frozenset(
    {HandoffReason.CUSTOMER_ASKED, HandoffReason.OTHER, HandoffReason.ROUTED}
)
"""`tooleval.metrics.DECISION_HANDOFF_REASONS` but `safety`. Every other reason is a system one
(`INFRASTRUCTURE_HANDOFF_REASONS`, tested), so a reason nobody sorted ranks lowest: it can never
displace a decision or a `safety` row."""


def reason_class(reason: HandoffReason) -> ReasonClass:
    """`reason`'s class (`ReasonClass`)."""
    if reason in SAFETY_REASONS:
        return ReasonClass.SAFETY
    if reason in DECISION_REASONS:
        return ReasonClass.DECISION
    return ReasonClass.SYSTEM


def handoff_rank(priority: Priority, reason: HandoffReason) -> tuple[int, ReasonClass]:
    """S17's rank: the priority, then the reason's class (module docstring)."""
    return RISING_PRIORITIES.index(priority), reason_class(reason)


def rises(
    priority: Priority, reason: HandoffReason, *, over: tuple[Priority, HandoffReason]
) -> bool:
    """Whether a new hand-off (`priority`, `reason`) raises the open row (`over`): its rank is
    strictly higher (`RISES` in SQL; the in-memory store in the tests uses this)."""
    return handoff_rank(priority, reason) > handoff_rank(*over)


def _quoted(reasons: frozenset[HandoffReason]) -> str:
    return ", ".join(f"'{reason.value}'" for reason in sorted(reasons))


def reason_class_sql(reason: str) -> str:
    """`reason_class` in SQL, over the column or expression `reason`: an integer,
    `ReasonClass`'s value."""
    whens = "".join(
        f" WHEN {reason} IN ({_quoted(reasons)}) THEN {rank.value}"
        for rank, reasons in (
            (ReasonClass.SAFETY, SAFETY_REASONS),
            (ReasonClass.DECISION, DECISION_REASONS),
        )
    )
    return f"CASE{whens} ELSE {ReasonClass.SYSTEM.value} END"


_RISING: Final = ", ".join(f"'{priority.value}'" for priority in RISING_PRIORITIES)


def _rank_sql(row: str) -> str:
    return (
        f"(array_position(ARRAY[{_RISING}], {row}.priority), {reason_class_sql(f'{row}.reason')})"
    )


RISES: Final = f"{_rank_sql('EXCLUDED')} > {_rank_sql('h')}"
"""The new hand-off ranks above the open row (S17, amended 2026-11-25, Z3 (a)): a row comparison,
the priority first and the reason's class only at equal priority (Postgres compares a row
constructor's fields left to right, stopping at the first unequal pair)."""
RAISED_COLUMNS: Final = (
    "priority",
    "reason",
    "origin",
    "intent",
    "trace_id",
    "guidance_shown",
    "message_masked",
)
"""What a higher rank replaces (S17); `raised_at` is set with them."""

_INSERT: Final = (
    "INSERT INTO handoffs AS h (business_id, session_id, trace_id, origin, reason, priority,"
    " intent, channel, ui_language, guidance_shown, message_masked, retention_days, pressed_at)"
    " VALUES (:business_id, :session_id, :trace_id, :origin, :reason, :priority, :intent,"
    " :channel, :ui_language, CAST(:guidance_shown AS text[]), :message_masked, :retention_days,"
    " CASE WHEN :origin = 'button' THEN now() END)"
    " ON CONFLICT (business_id, session_id) WHERE status <> 'resolved' DO UPDATE SET "
)
"""A new row records its own press: `pressed_at` is the database's `now()` for a button row and
NULL for a turn's (module docstring)."""
_RETURNING: Final = " RETURNING id, count AS handoff_count, priority, reason"
_RAISE: Final = ", ".join(
    f"{column} = CASE WHEN {RISES} THEN EXCLUDED.{column} ELSE h.{column} END"
    for column in RAISED_COLUMNS
)
STORE_HANDOFF: Final = text(
    f"{_INSERT}count = h.count + 1, {_RAISE},"
    f" raised_at = CASE WHEN {RISES} THEN now() ELSE h.raised_at END{_RETURNING}"
)
"""S17's insert-or-raise (module docstring)."""
PRESS_BUTTON: Final = text(
    f"{_INSERT}count = h.count, pressed_at = coalesce(h.pressed_at, now()){_RETURNING}"
)
""""Talk to a person": the open row, unchanged but for its first press's time, or a new button
row (module docstring)."""
INSERT_TURN: Final = text(
    "INSERT INTO widget_turns (business_id, session_id, trace_id, kind, reason, priority,"
    " intent, answer_outcome, channel, ui_language, server_latency_ms)"
    " VALUES (:business_id, :session_id, :trace_id, :kind, :reason, :priority, :intent,"
    " :answer_outcome, :channel, :ui_language, :server_latency_ms)"
)
HANDOFF_STATE: Final = text(
    "SELECT id, status, reply, replied_at, priority, reason FROM handoffs"
    " WHERE business_id = :business_id AND id = :handoff_id AND session_id = :session_id"
    " AND expires_at > now()"
)
"""One session's own hand-off, by id: another session's (in the same business or not) and an
expired one are not found."""
OWN_TURN: Final = text(
    "SELECT id FROM widget_turns"
    " WHERE business_id = :business_id AND session_id = :session_id AND trace_id = :trace_id"
)
"""One of the session's own turns, by its trace id (module docstring): at most one row
(`UNIQUE (business_id, trace_id)`)."""
INSERT_RATING: Final = text(
    "INSERT INTO turn_feedback (business_id, session_id, trace_id, rating)"
    " VALUES (:business_id, :session_id, :trace_id, :rating)"
    " ON CONFLICT (business_id, trace_id) DO NOTHING RETURNING id"
)
"""The turn's one rating, or no row when it has one already (module docstring)."""


def handoff_params(handoff: NewHandoff) -> dict[str, object]:
    return {
        "business_id": handoff.business_id,
        "session_id": handoff.session_id,
        "trace_id": handoff.trace_id,
        "origin": handoff.origin.value,
        "reason": handoff.reason.value,
        "priority": handoff.priority.value,
        "intent": None if handoff.intent is None else handoff.intent.value,
        "channel": handoff.channel.value,
        "ui_language": handoff.ui_language,
        "guidance_shown": list(handoff.guidance_shown),
        "message_masked": handoff.message_masked,
        "retention_days": handoff.retention_days,
    }


def turn_params(turn: TurnRow) -> dict[str, object]:
    return {
        "business_id": turn.business_id,
        "session_id": turn.session_id,
        "trace_id": turn.trace_id,
        "kind": turn.kind.value,
        "reason": None if turn.reason is None else turn.reason.value,
        "priority": None if turn.priority is None else turn.priority.value,
        "intent": None if turn.intent is None else turn.intent.value,
        "answer_outcome": turn.answer_outcome,
        "channel": turn.channel.value,
        "ui_language": turn.ui_language,
        "server_latency_ms": turn.server_latency_ms,
    }


def rating_params(rating: TurnRating) -> dict[str, object]:
    return {
        "business_id": rating.business_id,
        "session_id": rating.session_id,
        "trace_id": rating.trace_id,
        "rating": rating.rating.value,
    }


def _stored(row: Any) -> StoredHandoff:
    # `count AS handoff_count`: a row is a tuple, whose own `count` method would shadow a column
    # read as `row.count`.
    return StoredHandoff(
        id=row.id,
        count=row.handoff_count,
        priority=Priority(row.priority),
        reason=HandoffReason(row.reason),
    )


async def store_handoff(connection: AsyncConnection, handoff: NewHandoff) -> StoredHandoff:
    """S17: store `handoff`, or raise the session's open one (module docstring)."""
    row = (await connection.execute(STORE_HANDOFF, handoff_params(handoff))).one()
    return _stored(row)


async def press_button(connection: AsyncConnection, handoff: NewHandoff) -> StoredHandoff:
    """ "Talk to a person": the session's open hand-off, made by this press if there was none."""
    if handoff.origin is not Origin.BUTTON:
        raise ValueError("a press is a button hand-off")
    row = (await connection.execute(PRESS_BUTTON, handoff_params(handoff))).one()
    return _stored(row)


async def insert_turn(connection: AsyncConnection, turn: TurnRow) -> None:
    """One widget turn's metadata row (D6 (a))."""
    await connection.execute(INSERT_TURN, turn_params(turn))


async def handoff_state(
    connection: AsyncConnection, business_id: str, session_id: UUID, handoff_id: UUID
) -> HandoffState | None:
    """The session's own hand-off `handoff_id`, or None. More than one row raises."""
    row = (
        await connection.execute(
            HANDOFF_STATE,
            {"business_id": business_id, "session_id": session_id, "handoff_id": handoff_id},
        )
    ).one_or_none()
    if row is None:
        return None
    return HandoffState(
        id=row.id,
        status=HandoffStatus(row.status),
        reply=row.reply,
        replied_at=row.replied_at,
        priority=Priority(row.priority),
        reason=HandoffReason(row.reason),
    )


async def rate_turn(connection: AsyncConnection, rating: TurnRating) -> Rated:
    """`rating` of one of the session's own turns, once (module docstring)."""
    params = rating_params(rating)
    turn = {key: params[key] for key in ("business_id", "session_id", "trace_id")}
    if (await connection.execute(OWN_TURN, turn)).one_or_none() is None:
        return Rated.NOT_FOUND
    inserted = (await connection.execute(INSERT_RATING, params)).one_or_none()
    return Rated.ALREADY_RATED if inserted is None else Rated.RECORDED


class BusinessTransactions(Protocol):
    """What the store needs from the database: `db.Database` in the API."""

    def business_transaction(
        self, business_id: str
    ) -> AbstractAsyncContextManager[AsyncConnection]:
        """`business_id`'s RLS transaction, committed when the block ends without an error."""
        ...


class HandoffStore(Protocol):
    """What the widget routes call (`PostgresHandoffStore`; tests pass recorders). Each method
    raises on any failure; the routes bound each call by `handoff_store_timeout_seconds` and turn
    a failure into `queued: false` or a typed 503, never a 500 (S8, S14)."""

    async def record_turn(self, turn: TurnRow, handoff: NewHandoff | None) -> StoredHandoff | None:
        """The turn's row and, for a hand-off, the session's open hand-off, in one transaction;
        the stored hand-off, or None when `handoff` is None."""
        ...

    async def press_button(self, handoff: NewHandoff) -> StoredHandoff:
        """ "Talk to a person" (`press_button`)."""
        ...

    async def state(
        self, business_id: str, session_id: UUID, handoff_id: UUID
    ) -> HandoffState | None:
        """The session's own hand-off (`handoff_state`)."""
        ...

    async def rate(self, rating: TurnRating) -> Rated:
        """One rating of one of the session's own turns (`rate_turn`)."""
        ...


class PostgresHandoffStore:
    """The `HandoffStore` over Postgres, one business transaction per call."""

    def __init__(self, database: BusinessTransactions) -> None:
        self._database = database

    async def record_turn(self, turn: TurnRow, handoff: NewHandoff | None) -> StoredHandoff | None:
        if handoff is not None and (handoff.business_id, handoff.session_id) != (
            turn.business_id,
            turn.session_id,
        ):
            raise ValueError("a turn and its hand-off belong to one session")
        async with self._database.business_transaction(turn.business_id) as connection:
            stored = None if handoff is None else await store_handoff(connection, handoff)
            await insert_turn(connection, turn)
        return stored

    async def press_button(self, handoff: NewHandoff) -> StoredHandoff:
        async with self._database.business_transaction(handoff.business_id) as connection:
            return await press_button(connection, handoff)

    async def state(
        self, business_id: str, session_id: UUID, handoff_id: UUID
    ) -> HandoffState | None:
        async with self._database.business_transaction(business_id) as connection:
            return await handoff_state(connection, business_id, session_id, handoff_id)

    async def rate(self, rating: TurnRating) -> Rated:
        async with self._database.business_transaction(rating.business_id) as connection:
            return await rate_turn(connection, rating)
