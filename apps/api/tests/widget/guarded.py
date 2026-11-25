"""The real app serving PR 2a's widget routes with the guards on, on stand-ins (M8 PR 2a): the
sample businesses' tool, guard, corpus and widget folders, `FakeLimiter` for Redis,
`ScriptedDatabase` for Postgres (the session lookup and the hand-off store's statements, in
order), the fake provider, and `LookupMatcher` for H1 (`create_app`'s test seam: NOT H1).

`RecordingHandoffStore` is the hand-off store in memory, with S17's rule (one open row per
session, raised only to a higher rank: the priority, then the reason's class, `store.rises`;
idempotent presses) and PR 2b's rating rule (one rating
of a session's own recorded turn), for the parity and PII tests and the S17 unit tests; the same
rules in SQL are checked against Postgres by `tests/canary/test_widget_routes.py`.
"""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.auth.fakes import TOKEN, ScriptedConnection, ScriptedDatabase, customer_row
from tests.guards.agent_helpers import LookupMatcher
from tests.guards.helpers import SANDBOX_DIR, use_answer_key_selftest
from tests.widget.helpers import widget_settings
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.output import Matcher
from vectapilot_api.handoffs.store import (
    HandoffState,
    HandoffStatus,
    NewHandoff,
    Rated,
    Rating,
    StoredHandoff,
    TurnRating,
    TurnRow,
    rises,
)
from vectapilot_api.main import create_app
from vectapilot_api.widget.messages import get_handoff_store

BEARER: Final = {"authorization": f"Bearer {TOKEN}"}
MESSAGES: Final = "/v1/widget/messages"
HANDOFFS: Final = "/v1/widget/handoffs"
HANDOFF_ID: Final = UUID("4a4d0ff0-0000-4000-8000-0000000000b1")
FEE: Final = ", how much is the consultation fee?"
RAT_POISON: Final = "My dog ate rat poison" + FEE
"""A paws-and-co emergency (`vet_rat_poison`, the real term file), as M7's route tests send it."""
BENIGN: Final = "Is the shop open on Sunday?"


def lookup() -> LookupMatcher:
    """NOT H1: the one emergency above hits its real term; nothing else hits."""
    return LookupMatcher({RAT_POISON: {"vet_rat_poison"}})


def stored_row(
    priority: str = "urgent", count: int = 1, reason: str | None = None
) -> SimpleNamespace:
    """What `STORE_HANDOFF ... RETURNING id, count AS handoff_count, priority, reason` returns. The
    reason defaults to `safety` for an urgent row and `customer_asked` for any other."""
    if reason is None:
        reason = "safety" if priority == "urgent" else "customer_asked"
    return SimpleNamespace(id=HANDOFF_ID, handoff_count=count, priority=priority, reason=reason)


@dataclass
class GuardedServed:
    client: TestClient
    app: FastAPI
    limiter: FakeLimiter
    provider: FakeProvider

    def script(self, *items: list[Any] | BaseException) -> ScriptedDatabase:
        """A database whose statements answer the session lookup, then `items`, in order."""
        database = ScriptedDatabase(ScriptedConnection([[customer_row()], *items]))
        self.app.state.database = database
        return database

    def use_store(self, store: object) -> None:
        self.app.dependency_overrides[get_handoff_store] = lambda: store

    def send(self, message: str, **headers: str) -> httpx2.Response:
        return self.client.post(MESSAGES, json={"message": message}, headers={**BEARER, **headers})


@contextmanager
def serve_guarded(
    *,
    matcher: Matcher | None = None,
    limiter: FakeLimiter | None = None,
    provider: FakeProvider | None = None,
    **overrides: Any,
) -> Iterator[GuardedServed]:
    """The real app with the sample businesses' folders and the guards on (module docstring); the
    database starts untouched (any statement raises) until a test scripts it."""
    fake = limiter or FakeLimiter()
    model = provider or FakeProvider()
    settings = widget_settings(**({"tools_config_dir": SANDBOX_DIR} | overrides))
    with pytest.MonkeyPatch.context() as patch:
        use_answer_key_selftest(patch)
        app = create_app(settings, agent_provider=model, limiter=fake, matcher=matcher or lookup())
        with TestClient(app, base_url="https://testserver") as client:
            app.state.database = ScriptedDatabase()
            yield GuardedServed(client, app, fake, model)


@dataclass
class _Open:
    stored: StoredHandoff
    handoff: NewHandoff


@dataclass
class RecordingHandoffStore:
    """The hand-off store in memory (module docstring). `fail` raises on every write and read;
    `hang` never answers. Records every turn row and every hand-off as written (after S17)."""

    fail: BaseException | None = None
    hang: bool = False
    turns: list[TurnRow] = field(default_factory=list)
    written: list[NewHandoff] = field(default_factory=list)
    presses: list[NewHandoff] = field(default_factory=list)
    open: dict[tuple[str, UUID], _Open] = field(default_factory=dict)
    replies: Mapping[UUID, str] = field(default_factory=dict)
    ratings: dict[tuple[str, str], Rating] = field(default_factory=dict)

    async def _io(self) -> None:
        if self.hang:
            await asyncio.Event().wait()
        if self.fail is not None:
            raise self.fail

    def _store(self, handoff: NewHandoff, *, press: bool) -> StoredHandoff:
        key = (handoff.business_id, handoff.session_id)
        found = self.open.get(key)
        if found is None:
            stored = StoredHandoff(uuid4(), 1, handoff.priority, handoff.reason)
            self.open[key] = _Open(stored, handoff)
            self.written.append(handoff)
            return stored
        if press:
            return found.stored
        open_row = (found.stored.priority, found.stored.reason)
        raised = rises(handoff.priority, handoff.reason, over=open_row)
        kept = handoff if raised else found.handoff
        stored = replace(
            found.stored, count=found.stored.count + 1, priority=kept.priority, reason=kept.reason
        )
        self.open[key] = _Open(stored, kept)
        if raised:
            self.written.append(handoff)
        return stored

    async def record_turn(self, turn: TurnRow, handoff: NewHandoff | None) -> StoredHandoff | None:
        await self._io()
        stored = None if handoff is None else self._store(handoff, press=False)
        self.turns.append(turn)
        return stored

    async def press_button(self, handoff: NewHandoff) -> StoredHandoff:
        await self._io()
        self.presses.append(handoff)
        return self._store(handoff, press=True)

    async def state(
        self, business_id: str, session_id: UUID, handoff_id: UUID
    ) -> HandoffState | None:
        await self._io()
        found = self.open.get((business_id, session_id))
        if found is None or found.stored.id != handoff_id:
            return None
        reply = self.replies.get(handoff_id)
        stored = found.stored
        return HandoffState(
            handoff_id, HandoffStatus.OPEN, reply, None, stored.priority, stored.reason
        )

    async def rate(self, rating: TurnRating) -> Rated:
        """PR 2b: one rating of one of the session's own recorded turns (`rate_turn`'s rule)."""
        await self._io()
        own = any(
            (turn.business_id, turn.session_id, turn.trace_id)
            == (rating.business_id, rating.session_id, rating.trace_id)
            for turn in self.turns
        )
        if not own:
            return Rated.NOT_FOUND
        key = (rating.business_id, rating.trace_id)
        if key in self.ratings:
            return Rated.ALREADY_RATED
        self.ratings[key] = rating.rating
        return Rated.RECORDED

    def row(self, business_id: str, session_id: UUID) -> NewHandoff:
        """What the session's open row holds now (its last raise)."""
        return self.open[(business_id, session_id)].handoff
