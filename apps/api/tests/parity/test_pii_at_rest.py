"""S11: customer text at rest through the widget route holds no generated personal data (M8 PR 2a;
D6 (a); "How we will know": "18 of 18" inside the parity gate). Unit, on the parity harness's app.

- `test_pii_at_rest[<id>]`: each frozen category E case, on its own session, prefixed with a
  red-flag stand-in so its turn hands off and its text is stored (`CategoryLookup`, NOT H1),
  through `POST /v1/widget/messages`: the stored hand-off's masked message is S11's helper's, and
  no form of any value the case was filled with (`redteam.pii.leak_forms`) is in any stored row
  or in the response.
- `test_echoed_pii_at_rest[<run>]`: F's echoed-PII case (`f-006`, a scripted reply holding a phone
  number the message never held), in both parity runs: the hand-off is stored, and neither the
  rows nor the response hold the echoed value.
- The widget routes join F's log path (`redteam/logs.py`'s vehicle, read, never edited): a guard
  that raises with the case's filled input (f-001, e-017, through the message route), a confirm
  service that raises with it (f-005, through widget confirm: zara's and noah's PR 2 review, so
  each excluded log case is replayed by id), and a store that fails with it, leave no form of any
  generated value in a line the app's own formatter writes.
- The counts are of the rows the route hands to the store (`RecordingHandoffStore`, in memory),
  never a Postgres row: the same rows against Postgres are `tests/canary/test_widget_routes.py`'s.

`scripts/parity_gate.py` requires every E case's test and both runs' echoed test, passed once.
"""

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.parity_gate import ECHOED_PII, RUNS
from tests.parity.harness import (
    BEARER,
    WIDGET,
    WIDGET_CONFIRM,
    parity_app,
    seams_for,
    session_of,
    through_widget,
)
from tests.redteam.standins import CategoryLookup
from tests.redteam.suite import CASES, GENERATOR
from tests.widget.guarded import RecordingHandoffStore
from vectapilot_api.actions.router import get_confirm_service
from vectapilot_api.agent.router import get_agent_guard
from vectapilot_api.auth.dependencies import get_customer_session
from vectapilot_api.guards.types import TermCategory
from vectapilot_api.handoffs.at_rest import mask_at_rest
from vectapilot_api.redteam.agent import REAL_SEAMS
from vectapilot_api.redteam.cases import Category
from vectapilot_api.redteam.harness import FilledCase, fill
from vectapilot_api.redteam.logs import ACTION_ID, CONFIRM_TOKEN, app_formatter
from vectapilot_api.redteam.pii import PLACEHOLDER, PiiType, Placeholder, leak_forms
from vectapilot_api.widget.messages import STORE_FAILED, get_handoff_store

E_CASES: Final = tuple(sorted(c.id for c in CASES.values() if c.category is Category.E))
RED_FLAG: Final = "TEST-ONLY red-flag stand-in. "
"""What `CategoryLookup` hits (with the case's input after it): NOT H1, never a real term."""
ASGI_ERROR: Final = "Exception in ASGI application"
CLIENT_LOGGERS: Final = ("httpx2",)


@pytest.fixture(scope="module")
def served() -> Iterator[tuple[FastAPI, TestClient]]:
    with parity_app() as app:
        yield app


def generated(filled: FilledCase, templates: Sequence[str]) -> list[str]:
    """Every value the case's placeholders in `templates` were filled with (same case id, type
    and n as the case's own filling)."""
    return [
        filled.generator.value(
            filled.case.id, Placeholder(PiiType(match.group(1)), int(match.group(2) or 1))
        )
        for template in templates
        for match in PLACEHOLDER.finditer(template)
    ]


def leaked(values: Sequence[str], *texts: str) -> list[str]:
    """The forms of `values` found in `texts` (never printed: a failure names the count only)."""
    forms = {form for value in values for form in leak_forms(value)}
    return [form for form in forms if any(form in text for text in texts)]


def rows_of(store: RecordingHandoffStore) -> str:
    rows: list[dict[str, Any]] = [asdict(row) for row in (*store.written, *store.presses)]
    rows += [asdict(turn) for turn in store.turns]
    return json.dumps(rows, default=str, ensure_ascii=False)


@pytest.mark.parametrize("case_id", E_CASES)
def test_pii_at_rest(case_id: str, served: tuple[FastAPI, TestClient]) -> None:
    app, client = served
    filled = fill(CASES[case_id], GENERATOR)
    message = RED_FLAG + filled.input
    seams = replace(REAL_SEAMS, matcher=CategoryLookup({message: TermCategory.EMERGENCY}))

    seen = through_widget(app, client, filled, seams, message=message)

    assert seen.stored is not None and seen.body is not None
    [handoff] = seen.stored.written
    assert seen.body["turn"]["reason"] == "safety"  # the red flag decided: the text is stored
    assert seen.body["handoff"]["queued"] is True
    assert handoff.message_masked == mask_at_rest(message) is not None  # S11's helper
    values = generated(filled, [filled.case.input])
    assert values  # the positive control: the case holds personal data
    found = leaked(values, rows_of(seen.stored), json.dumps(seen.body, ensure_ascii=False))
    assert not found, f"{len(found)} forms of a generated value at rest"


@pytest.mark.parametrize("run", RUNS)
def test_echoed_pii_at_rest(run: str, served: tuple[FastAPI, TestClient]) -> None:
    app, client = served
    filled = fill(CASES[ECHOED_PII], GENERATOR)
    replies = [getattr(step, "text", "") for step in filled.case.fixture.scripted_model]

    seen = through_widget(app, client, filled, seams_for(run, filled))

    assert seen.stored is not None and seen.body is not None
    assert seen.body["turn"]["kind"] == "handoff"  # withheld (standins) or failed closed (seams)
    [handoff] = seen.stored.written
    assert handoff.message_masked == mask_at_rest(filled.input)
    values = generated(filled, replies)
    assert values  # the reply echoes a value the message never held
    found = leaked(values, rows_of(seen.stored), json.dumps(seen.body, ensure_ascii=False))
    assert not found, f"{len(found)} forms of an echoed value at rest"


# ── F's log path, through the widget route ───────────────────────────────────────────────────


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(CLIENT_LOGGERS):
            self.records.append(record)


@contextmanager
def lines_written() -> Iterator[list[str]]:
    """Every line the app's own formatter writes for the records of the block, any logger."""
    formatter = app_formatter()
    assert formatter is not None, "the app's redacting formatter is installed"
    handler = _Records()
    root = logging.getLogger()
    root.addHandler(handler)
    lines: list[str] = []
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        lines.extend(formatter.format(record) for record in handler.records)


class _RaisingGuard:
    """F's vehicle on the widget route: the guard raises with the case's filled input."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.raised = 0

    async def respond(self, *args: Any, **kwargs: Any) -> Any:
        self.raised += 1
        raise RuntimeError(self.message)


class _RaisingConfirm:
    """F's vehicle on widget confirm (f-005's, M7's `redteam/logs.py`): the confirm service
    raises with the case's filled input."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.raised = 0

    async def confirm(self, *args: Any, **kwargs: Any) -> Any:
        self.raised += 1
        raise RuntimeError(self.message)


def _posted(client: TestClient, path: str, body: dict[str, str]) -> None:
    """One request whose forced exception may escape the app: logged as the server would."""
    try:
        client.post(path, json=body, headers=BEARER)
    except RuntimeError as escaped:  # the client re-raises what escaped the app
        logging.getLogger("uvicorn.error").error(f"{ASGI_ERROR}\n", exc_info=escaped)


@pytest.mark.parametrize("case_id", ["f-001", "e-017"])
def test_a_guard_failing_on_the_widget_route_logs_no_generated_value(
    case_id: str, served: tuple[FastAPI, TestClient]
) -> None:
    app, client = served
    filled = fill(CASES[case_id], GENERATOR)
    guard = _RaisingGuard(f"forced failure: {filled.input}")
    session = session_of(filled.case)
    store = RecordingHandoffStore()
    overrides = {
        get_agent_guard: lambda: guard,
        get_customer_session: lambda: session,
        get_handoff_store: lambda: store,
    }
    app.dependency_overrides.update(overrides)
    try:
        with lines_written() as lines:
            _posted(client, WIDGET, {"message": filled.input})
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)

    assert guard.raised == 1  # the vehicle was reached
    assert lines  # the positive control: something was written
    found = leaked(generated(filled, [filled.case.input]), *lines)
    assert not found, f"{len(found)} forms of a generated value in a log line"


@pytest.mark.parametrize("case_id", ["f-005"])
def test_a_confirm_failing_on_widget_confirm_logs_no_generated_value(
    case_id: str, served: tuple[FastAPI, TestClient]
) -> None:
    # f-005 is the confirm route's log case: excluded from parity (`log_vehicle`), replayed here
    # by id through the widget's own confirm route with its filled values.
    app, client = served
    filled = fill(CASES[case_id], GENERATOR)
    service = _RaisingConfirm(f"forced failure: {filled.input}")
    session = session_of(filled.case)
    overrides = {
        get_confirm_service: lambda: service,
        get_customer_session: lambda: session,
    }
    app.dependency_overrides.update(overrides)
    try:
        with lines_written() as lines:
            _posted(
                client,
                WIDGET_CONFIRM.format(action_id=ACTION_ID),
                {"confirm_token": CONFIRM_TOKEN},
            )
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)

    assert service.raised == 1  # the vehicle was reached
    assert any(ASGI_ERROR in line for line in lines)  # the positive control: it escaped, logged
    values = generated(filled, [filled.case.input])
    assert values  # the case holds personal data
    found = leaked(values, *lines)
    assert not found, f"{len(found)} forms of a generated value in a log line"


def test_a_store_failing_with_the_message_logs_no_generated_value(
    served: tuple[FastAPI, TestClient],
) -> None:
    app, client = served
    filled = fill(CASES["e-017"], GENERATOR)
    message = RED_FLAG + filled.input
    seams = replace(REAL_SEAMS, matcher=CategoryLookup({message: TermCategory.EMERGENCY}))
    store = RecordingHandoffStore(fail=RuntimeError(f"forced failure: {message}"))

    with lines_written() as lines:
        seen = through_widget(app, client, filled, seams, message=message, store=store)

    assert seen.body is not None
    assert seen.body["handoff"] == {"id": None, "queued": False, "follow_up": False}
    assert any(STORE_FAILED in line for line in lines)  # the vehicle was reached
    found = leaked(generated(filled, [filled.case.input]), *lines)
    assert not found, f"{len(found)} forms of a generated value in a log line"
