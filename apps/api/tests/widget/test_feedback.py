"""`POST /v1/widget/feedback` without a database or Redis (M8 PR 2b; D6 (a), S1, S4, S9): the
real app with the guards on (`tests/widget/guarded.py`), the session lookup and the rating's two
statements on `ScriptedDatabase`, `FakeLimiter` for Redis. Against Postgres (the foreign key that
pairs the session, two sessions, two businesses): `tests/canary/test_widget_routes.py`.

- A rating of the session's own turn is stored once: 201 with the body as sent; the statements
  name the session's business and id and the body's trace id, nothing from anywhere else.
- Another session's turn and a made-up trace id are the one 404 (the turn read finds nothing, so
  nothing is inserted); a second rating is the 409.
- A closed body: naming `customer_ref`, `session_id`, `business_id` or anything else, or a trace id
  or rating outside their forms, is a 422 that echoes nothing, before the window and any statement.
- The per-session window (`RouteSlug.FEEDBACK`) fails closed: a 429 with a health business's
  guidance, or a typed 503 with Redis down, each before any statement.
- A rating that cannot be stored is a typed 503 whose warning names the error's class only; a
  session lookup that cannot run is S14's.
"""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Final

import pytest
from sqlalchemy.exc import OperationalError

from tests.auth.fakes import BUSINESS, SESSION_ID, ScriptedDatabase
from tests.handoffs.test_store import turn
from tests.widget.guarded import BEARER, GuardedServed, RecordingHandoffStore, serve_guarded
from tests.widget.helpers import guidance_of
from vectapilot_api.auth.dependencies import SESSION_STORE_UNAVAILABLE
from vectapilot_api.auth.http import UNKNOWN_KEY
from vectapilot_api.guards.fake_limiter import FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import RATE_LIMITED
from vectapilot_api.guards.types import RouteSlug
from vectapilot_api.handoffs.store import INSERT_RATING, OWN_TURN, Rated, Rating
from vectapilot_api.widget.config import WidgetRegistry
from vectapilot_api.widget.feedback import (
    ALREADY_RATED,
    LIMITS_UNAVAILABLE,
    STORE_UNAVAILABLE,
    TURN_NOT_FOUND,
)
from vectapilot_api.widget.messages import STORE_FAILED
from vectapilot_api.widget.router import WIDGET_NOT_FOUND

FEEDBACK: Final = "/v1/widget/feedback"
TRACE: Final = "7ace" * 8
BODY: Final = {"trace_id": TRACE, "rating": "up"}
GUIDANCE: Final = guidance_of(BUSINESS)
ROW: Final = SimpleNamespace(id="a row")


@pytest.fixture
def served() -> Iterator[GuardedServed]:
    with serve_guarded() as app:
        yield app


def rate(served: GuardedServed, body: dict[str, Any] = BODY) -> Any:
    return served.client.post(FEEDBACK, json=body, headers=BEARER)


def executed(database: ScriptedDatabase) -> list[tuple[str, dict[str, Any]]]:
    return [(sql, dict(params)) for sql, params in database.connection.executed]


# ── One rating of the session's own turn ─────────────────────────────────────────────────────


@pytest.mark.parametrize("rating", ["up", "down"])
def test_a_rating_of_the_sessions_own_turn_is_stored_once(
    served: GuardedServed, rating: str
) -> None:
    database = served.script([ROW], [ROW])

    response = rate(served, {"trace_id": TRACE, "rating": rating})

    assert response.status_code == 201, response.text
    assert response.json() == {"trace_id": TRACE, "rating": rating}
    assert response.headers["cache-control"] == "no-store"
    ids = {"business_id": BUSINESS, "session_id": SESSION_ID, "trace_id": TRACE}
    assert executed(database)[1:] == [
        (OWN_TURN.text, ids),
        (INSERT_RATING.text, ids | {"rating": rating}),
    ]
    assert database.opened == [BUSINESS, BUSINESS]  # the lookup, then the rating's transaction
    # Per session (S4), under its own slug, before the statements.
    assert served.limiter.calls == [LimiterCall("admit", BUSINESS, RouteSlug.FEEDBACK, True)]


def test_another_sessions_turn_or_a_made_up_one_is_the_one_404_and_inserts_nothing(
    served: GuardedServed,
) -> None:
    database = served.script([])

    response = rate(served)

    assert (response.status_code, response.json()) == (
        404,
        {"detail": TURN_NOT_FOUND, "error": "turn_not_found"},
    )
    assert [sql for sql, _ in executed(database)[1:]] == [OWN_TURN.text]


def test_a_second_rating_of_a_turn_is_refused_never_an_update(served: GuardedServed) -> None:
    served.script([ROW], [])

    response = rate(served, {"trace_id": TRACE, "rating": "down"})

    assert (response.status_code, response.json()) == (
        409,
        {"detail": ALREADY_RATED, "error": "already_rated"},
    )


def test_the_in_memory_store_follows_the_same_rule(served: GuardedServed) -> None:
    # The recorder the parity tests use: a turn it recorded is rated once; any other is not found.
    store = RecordingHandoffStore()
    served.use_store(store)
    served.script()
    store.turns.append(turn(trace_id=TRACE))
    first = rate(served)
    served.script()
    again = rate(served)
    served.script()
    other = rate(served, {"trace_id": "b" * 32, "rating": "up"})

    assert (first.status_code, again.status_code, other.status_code) == (201, 409, 404)
    assert store.ratings == {(BUSINESS, TRACE): Rating.UP}


# ── A closed body; the windows; the failures ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(BODY | {"customer_ref": "zqxmarker"}, id="customer_ref"),
        pytest.param(BODY | {"session_id": "zqxmarker"}, id="session_id"),
        pytest.param(BODY | {"business_id": "zqxmarker"}, id="business_id"),
        pytest.param(BODY | {"comment": "zqxmarker"}, id="any text"),
        pytest.param({"trace_id": TRACE, "rating": "zqxmarker"}, id="rating"),
        pytest.param({"trace_id": "zqxmarker", "rating": "up"}, id="trace id"),
        pytest.param({"trace_id": TRACE.upper(), "rating": "up"}, id="upper case"),
        pytest.param({"trace_id": "0" * 32, "rating": "up"}, id="all zeros"),
        pytest.param({"rating": "up"}, id="no trace id"),
    ],
)
def test_a_body_naming_anyone_or_anything_else_is_a_closed_422_before_the_window(
    served: GuardedServed, body: dict[str, Any]
) -> None:
    database = served.script()

    response = rate(served, body)

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "zqxmarker" not in response.text and TRACE.upper() not in response.text
    problems = response.json()["problems"]
    assert all(UNKNOWN_KEY in p["loc"] or p["type"] != "extra_forbidden" for p in problems)
    assert served.limiter.calls == [] and len(executed(database)) == 1


def test_a_full_window_is_a_429_with_the_health_businesss_guidance_and_no_statement() -> None:
    with serve_guarded(limiter=FakeLimiter(limit="refuse")) as served:
        database = served.script()
        response = rate(served)

    assert response.status_code == 429
    body = response.json()
    assert (body["error"], body["guidance"]) == (RATE_LIMITED, GUIDANCE)
    assert response.headers["retry-after"] == "30"
    assert response.headers["cache-control"] == "no-store"
    assert len(executed(database)) == 1


def test_with_redis_down_feedback_fails_closed_with_a_typed_503_and_no_statement() -> None:
    with serve_guarded(limiter=FakeLimiter(limit="raise")) as served:
        database = served.script()
        response = rate(served)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": LIMITS_UNAVAILABLE, "error": "limits_unavailable"},
    )
    assert len(executed(database)) == 1


@pytest.mark.parametrize("hang", [False, True], ids=["down", "hung"])
def test_a_rating_that_cannot_be_stored_is_a_typed_503_logging_the_class_name_only(
    caplog: pytest.LogCaptureFixture, hang: bool
) -> None:
    leak = OperationalError("INSERT ...", {"trace_id": TRACE}, Exception("zqxmarker"))
    store = RecordingHandoffStore(fail=None if hang else leak, hang=hang)
    with serve_guarded(handoff_store_timeout_seconds=0.05) as served:
        served.use_store(store)
        served.script()
        root = logging.getLogger()
        root.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING):
                response = rate(served)
        finally:
            root.removeHandler(caplog.handler)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": STORE_UNAVAILABLE, "error": "feedback_store_unavailable"},
    )
    [record] = [r for r in caplog.records if r.getMessage() == STORE_FAILED]
    reason = "TimeoutError" if hang else "OperationalError"
    assert getattr(record, "reason") == reason  # noqa: B009
    assert "zqxmarker" not in json.dumps(vars(record), default=str)


def test_a_bug_in_the_store_propagates_never_a_typed_error(served: GuardedServed) -> None:
    served.use_store(RecordingHandoffStore(fail=KeyError("a bug")))
    served.script()

    with pytest.raises(KeyError):
        rate(served)


@pytest.mark.parametrize("hang", [False, True], ids=["down", "hung"])
def test_with_postgres_down_feedback_is_s14s_typed_503_and_runs_nothing(
    served: GuardedServed, hang: bool
) -> None:
    store = RecordingHandoffStore()
    served.use_store(store)
    served.app.state.database = (
        ScriptedDatabase(hang=True)
        if hang
        else ScriptedDatabase(fail=OperationalError("SELECT ...", {}, Exception()))
    )

    response = rate(served)

    assert (response.status_code, response.json()) == (
        503,
        {"detail": SESSION_STORE_UNAVAILABLE, "error": "session_store_unavailable"},
    )
    assert served.limiter.calls == [] and store.ratings == {}


def test_a_session_whose_widget_is_disabled_gets_the_one_404_and_rates_nothing(
    served: GuardedServed,
) -> None:
    served.app.state.widgets = WidgetRegistry.empty()
    database = served.script()

    response = rate(served)

    assert (response.status_code, response.json()) == (
        404,
        {"detail": WIDGET_NOT_FOUND, "error": "widget_not_found"},
    )
    assert served.limiter.calls == [] and len(executed(database)) == 1


def test_rated_is_the_three_answers_the_route_maps() -> None:
    assert {rated.value for rated in Rated} == {"recorded", "not_found", "already_rated"}
    assert {rating.value for rating in Rating} == {"up", "down"}


def test_a_full_window_of_a_non_health_business_carries_no_guidance() -> None:
    # Mutation probe (feedback-429-guidance-for-non-health): G3, guidance for health only.
    with serve_guarded(limiter=FakeLimiter(limit="refuse")) as served:
        widget = served.app.state.widgets.enabled(BUSINESS)
        assert widget.guidance  # the positive control: guidance there to leak
        served.app.state.widgets = WidgetRegistry({BUSINESS: replace(widget, health=False)})
        served.script()
        response = rate(served)

    assert response.status_code == 429
    assert (response.json()["error"], response.json()["guidance"]) == (RATE_LIMITED, [])
