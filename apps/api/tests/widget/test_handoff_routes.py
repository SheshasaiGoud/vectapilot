"""`POST /v1/widget/handoffs` and `GET /v1/widget/handoffs/{handoff_id}` without a database or Redis
(M8 PR 2a; S1, S14, S17, D7 (a)), on the stand-ins of `tests/widget/guarded.py`. Against
Postgres: `tests/canary/test_widget_routes.py`.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from tests.auth.fakes import BUSINESS, SESSION_ID, customer_row
from tests.guards.agent_helpers import LookupMatcher
from tests.widget.guarded import (
    BEARER,
    BENIGN,
    HANDOFF_ID,
    HANDOFFS,
    RAT_POISON,
    GuardedServed,
    RecordingHandoffStore,
    serve_guarded,
    stored_row,
)
from tests.widget.helpers import guidance_of
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import RATE_LIMITED
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.handoffs.store import HANDOFF_STATE, PRESS_BUTTON
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.messages import (
    HANDOFF_NOT_FOUND,
    LIMITS_UNAVAILABLE,
    STORE_UNAVAILABLE,
)
from vectapilot_api.widget.models import HandoffQueued, HandoffStateResponse

GUIDANCE: Final = guidance_of(BUSINESS)
REPLIED: Final = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
DOSE: Final = "How many mg of paracetamol can I give my dog?"
"""zara's input (a): a paws-and-co professional-judgement message (`vet_dose_by_weight`, the real
term file), a `safety` hand-off at high whose guidance ends "Do not wait for a reply"."""


def dose_lookup() -> LookupMatcher:
    """NOT H1: the emergency and the dose question hit their real terms; nothing else hits."""
    return LookupMatcher({RAT_POISON: {"vet_rat_poison"}, DOSE: {"vet_dose_by_weight"}})


@pytest.fixture
def served() -> Iterator[GuardedServed]:
    with serve_guarded() as app:
        yield app


def status_path(handoff_id: UUID = HANDOFF_ID) -> str:
    return f"{HANDOFFS}/{handoff_id}"


def executed(served: GuardedServed) -> list[tuple[str, dict[str, Any]]]:
    return list(served.app.state.database.connection.executed)


# ── "Talk to a person" ───────────────────────────────────────────────────────────────────────


def test_talk_to_a_person_stores_a_button_hand_off_and_returns_its_id(
    served: GuardedServed,
) -> None:
    served.script([stored_row("normal")])

    response = served.client.post(HANDOFFS, headers=BEARER)

    assert response.status_code == 200, response.text
    # A new button row: queued, and neither urgent nor `safety`, so a follow-up (D11).
    assert response.json() == {"id": str(HANDOFF_ID), "queued": True, "follow_up": True}
    assert response.headers["cache-control"] == "no-store"
    [_, (text, params)] = executed(served)
    assert text == PRESS_BUTTON.text
    assert {key: params[key] for key in ("origin", "reason", "priority", "intent")} == {
        "origin": "button",
        "reason": "customer_asked",
        "priority": "normal",
        "intent": None,
    }
    assert (params["message_masked"], params["guidance_shown"]) == (None, [])
    assert (params["business_id"], params["session_id"]) == (BUSINESS, SESSION_ID)
    # No guard, no limiter, no model (S17: the button is never refused by a window).
    assert served.limiter.calls == [] and served.provider.calls == []


def test_talk_to_a_person_is_idempotent_per_session_and_never_lowers_an_open_row(
    served: GuardedServed,
) -> None:
    store = RecordingHandoffStore()
    served.use_store(store)
    served.script([customer_row()], [customer_row()])  # three requests, three session lookups

    urgent = served.send(RAT_POISON).json()["handoff"]
    pressed = [served.client.post(HANDOFFS, headers=BEARER).json() for _ in range(2)]

    assert pressed == [urgent, urgent]  # the open row's id, unchanged
    # zara's (b): a press after an emergency (say after a reload) invites no waiting (D11).
    assert urgent["queued"] is True and urgent["follow_up"] is False
    row = store.row(BUSINESS, SESSION_ID)
    assert (row.origin.value, row.reason.value, row.priority.value) == ("turn", "safety", "urgent")
    [only] = store.open.values()
    assert only.stored.count == 1  # a press counts nothing [proposal]


def test_a_failed_press_is_queued_false_never_a_5xx(served: GuardedServed) -> None:
    served.script(OperationalError("INSERT ...", {}, Exception()))

    response = served.client.post(HANDOFFS, headers=BEARER)

    assert (response.status_code, response.json()) == (
        200,
        {"id": None, "queued": False, "follow_up": False},
    )


def test_a_hung_press_ends_at_the_stores_timeout() -> None:
    with serve_guarded(handoff_store_timeout_seconds=0.05) as served:
        served.use_store(RecordingHandoffStore(hang=True))
        served.script()
        response = served.client.post(HANDOFFS, headers=BEARER)

    assert response.json() == {"id": None, "queued": False, "follow_up": False}


def test_talk_to_a_person_needs_a_session_and_an_enabled_widget(served: GuardedServed) -> None:
    served.script()
    anonymous = served.client.post(HANDOFFS)
    served.app.state.widgets = WidgetRegistry.empty()
    disabled = served.client.post(HANDOFFS, headers=BEARER)

    assert (anonymous.status_code, anonymous.json()["error"]) == (401, "unauthorized")
    assert (disabled.status_code, disabled.json()["error"]) == (404, "widget_not_found")


# ── The status of the session's own hand-off ─────────────────────────────────────────────────


def test_a_session_reads_its_own_hand_offs_status_and_reply(served: GuardedServed) -> None:
    state = SimpleNamespace(
        id=HANDOFF_ID,
        status="claimed",
        reply="We will call you.",
        replied_at=REPLIED,
        priority="normal",
        reason="customer_asked",
    )
    served.script([state])

    response = served.client.get(status_path(), headers=BEARER)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(HANDOFF_ID),
        "status": "claimed",
        "reply": "We will call you.",
        "replied_at": "2026-11-24T12:00:00Z",
        "follow_up": True,
    }
    assert response.headers["cache-control"] == "no-store"
    [_, (text, params)] = executed(served)
    assert text == HANDOFF_STATE.text
    # S1: this session's only, read by the session's own ids.
    assert params == {"business_id": BUSINESS, "session_id": SESSION_ID, "handoff_id": HANDOFF_ID}
    # S17: the per-session window, the session's reference as the customer.
    assert served.limiter.calls == [
        LimiterCall("admit", BUSINESS, RouteSlug.HANDOFF_STATUS, customered=True)
    ]


def test_another_sessions_or_an_unknown_hand_off_is_one_404(served: GuardedServed) -> None:
    served.script([])

    response = served.client.get(status_path(UUID(int=9)), headers=BEARER)

    assert (response.status_code, response.json()) == (
        404,
        {"detail": HANDOFF_NOT_FOUND, "error": "handoff_not_found"},
    )


def test_a_full_status_window_is_a_429_with_guidance_and_reads_nothing() -> None:
    with serve_guarded(limiter=FakeLimiter(limit="refuse")) as served:
        served.script()
        response = served.client.get(status_path(), headers=BEARER)

    assert response.status_code == 429
    assert response.json()["error"] == RATE_LIMITED
    assert response.json()["guidance"] == GUIDANCE  # a health business (G3)
    assert response.headers["retry-after"] == "30"
    assert response.headers["cache-control"] == "no-store"
    assert len(executed(served)) == 1  # the session lookup only


def test_with_redis_down_the_status_fails_closed_as_a_typed_503() -> None:
    with serve_guarded(limiter=FakeLimiter(limit="raise")) as served:
        served.script()
        response = served.client.get(status_path(), headers=BEARER)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": LIMITS_UNAVAILABLE, "error": "limits_unavailable"},
    )
    assert response.headers["cache-control"] == "no-store"
    assert len(executed(served)) == 1


@pytest.mark.parametrize("hang", [False, True], ids=["fails", "hangs"])
def test_a_status_read_that_cannot_run_is_a_typed_503(hang: bool) -> None:
    store = RecordingHandoffStore(hang=hang, fail=None if hang else OSError("down"))
    with serve_guarded(handoff_store_timeout_seconds=0.05) as served:
        served.use_store(store)
        served.script()
        response = served.client.get(status_path(), headers=BEARER)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": STORE_UNAVAILABLE, "error": "handoff_store_unavailable"},
    )


def test_a_status_id_that_is_not_a_uuid_is_a_closed_422_after_the_session(
    served: GuardedServed,
) -> None:
    served.script()

    response = served.client.get(f"{HANDOFFS}/zqxmarker", headers=BEARER)

    assert response.status_code == 422
    assert "zqxmarker" not in response.text
    assert served.limiter.calls == []


def test_the_status_route_needs_an_enabled_widget(served: GuardedServed) -> None:
    served.script()
    served.app.state.widgets = WidgetRegistry.empty()

    response = served.client.get(status_path(), headers=BEARER)

    assert (response.status_code, response.json()["error"]) == (404, "widget_not_found")
    assert served.limiter.calls == []


# ── D11 from the row as stored (zara's PR 2 review) ──────────────────────────────────────────


def test_a_press_after_a_professional_judgement_hand_off_invites_no_follow_up() -> None:
    # zara's (a): a `safety` hand-off at high, then "talk to a person": the press returns that
    # row, and the widget must not say "a person will reply" or poll it.
    store = RecordingHandoffStore()
    with serve_guarded(matcher=dose_lookup()) as served:
        served.use_store(store)
        served.script([customer_row()], [customer_row()])  # the press's and (c)'s lookups
        sent = served.send(DOSE).json()
        pressed = served.client.post(HANDOFFS, headers=BEARER).json()
        # zara's (c): a later, lower hand-off turn on the session answers the same `safety` row.
        later = served.send(BENIGN).json()

    assert (sent["turn"]["reason"], sent["turn"]["priority"]) == ("safety", "high")
    assert sent["handoff"] == {"id": pressed["id"], "queued": True, "follow_up": False}
    assert pressed == {"id": sent["handoff"]["id"], "queued": True, "follow_up": False}
    assert later["turn"]["kind"] == "handoff" and later["turn"]["reason"] != "safety"
    assert later["handoff"] == {"id": pressed["id"], "queued": True, "follow_up": False}


def test_the_status_read_stops_inviting_a_follow_up_once_the_row_is_raised(
    served: GuardedServed,
) -> None:
    store = RecordingHandoffStore()
    served.use_store(store)
    served.script(*([customer_row()] for _ in range(3)))  # four requests, four lookups

    pressed = served.client.post(HANDOFFS, headers=BEARER).json()
    before = served.client.get(status_path(UUID(pressed["id"])), headers=BEARER).json()
    served.send(RAT_POISON)  # S17: the open row rises to urgent `safety`
    after = served.client.get(status_path(UUID(pressed["id"])), headers=BEARER).json()

    assert pressed == {"id": pressed["id"], "queued": True, "follow_up": True}  # the control
    assert (before["status"], before["follow_up"]) == ("open", True)
    assert (after["status"], after["follow_up"]) == ("open", False)


def test_queued_and_id_agree_and_only_a_queued_hand_off_invites_a_follow_up() -> None:
    # Mutation probe (models-queued-consistency-dropped): S8's "a person will reply" lie.
    with pytest.raises(ValidationError):
        HandoffQueued(id=None, queued=True, follow_up=False)
    with pytest.raises(ValidationError):
        HandoffQueued(id=UUID(int=1), queued=False, follow_up=False)
    with pytest.raises(ValidationError):
        HandoffQueued(id=None, queued=False, follow_up=True)
    assert HandoffQueued(id=UUID(int=1), queued=True, follow_up=False)


def test_a_reply_time_is_aware() -> None:
    # Mutation probe (models-state-replied-naive).
    with pytest.raises(ValidationError):
        HandoffStateResponse(
            id=UUID(int=1),
            status="open",
            reply="x",
            replied_at=datetime(2026, 11, 24),  # naive on purpose
            follow_up=True,
        )


def test_a_press_keeps_the_widgets_retention(served: GuardedServed) -> None:
    # Mutation probe (msg-press-retention-hard-coded): rule 9, P5.
    widget = served.app.state.widgets.enabled(BUSINESS)
    file = widget.file.model_copy(update={"handoff_retention_days": 7})
    served.app.state.widgets = WidgetRegistry({BUSINESS: replace(widget, file=file)})
    served.script([stored_row("normal")])

    assert served.client.post(HANDOFFS, headers=BEARER).json()["queued"] is True
    [_, (_, params)] = executed(served)
    assert params["retention_days"] == 7


def test_a_full_status_window_of_a_non_health_business_carries_no_guidance() -> None:
    # Mutation probe (msg-status-429-guidance-for-non-health): G3, guidance for health only.
    with serve_guarded(limiter=FakeLimiter(limit="refuse")) as served:
        widget = served.app.state.widgets.enabled(BUSINESS)
        assert widget.guidance  # the positive control: guidance there to leak
        served.app.state.widgets = WidgetRegistry({BUSINESS: replace(widget, health=False)})
        served.script()
        response = served.client.get(status_path(), headers=BEARER)

    assert response.status_code == 429
    assert (response.json()["error"], response.json()["guidance"]) == (RATE_LIMITED, [])
