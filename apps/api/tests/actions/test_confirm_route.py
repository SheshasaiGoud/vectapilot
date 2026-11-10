"""M5 PR 3: `POST /v1/agent/actions/{action_id}/confirm`, without a database.

The registry lookup is replaced (as `tests/test_business_header.py` does) and the confirm service
is a stand-in, so each rule of the route is pinned on its own; the real service is in
`test_confirm_db.py` and `test_confirm_api.py`.

- **Refused outside `HEADER_ENVIRONMENTS`** (ADR 0009; `customer_ref` is client-asserted until
  M8): production gets the header's 400 and the service is never called.
- **The body is closed** (W2): tool arguments are a 422, a missing token is a 422, and no 422
  echoes the token or the customer.
- **Responses:** `executed` is 200, `failed` is 409 with the same body shape, every refusal is the
  one 404 body, a rollback and an unknown outcome are 503s with their own codes, each with
  `X-Trace-Id`; a bug is a 500, never a typed error.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from tests.test_business_header import registry_says
from vectapilot_api.actions.router import ACTION_NOT_FOUND
from vectapilot_api.actions.types import (
    ActionNotFound,
    ActionStatus,
    ConfirmFailed,
    ConfirmRequest,
    ConfirmResponse,
    OutcomeUnknown,
)
from vectapilot_api.business import REFUSED_ENVIRONMENT
from vectapilot_api.config import Settings
from vectapilot_api.main import create_app
from vectapilot_api.tools.book import BookOutput
from vectapilot_api.tools.config import ToolName
from vectapilot_api.tools.types import WriteFailure

ACTION: Final = UUID("5f0c0b1e-1111-4222-8333-444455556666")
TOKEN: Final = "Q7zx9TOKENtokenTOKEN_-"  # noqa: S105 (a test token, not a secret)
PATH: Final = f"/v1/agent/actions/{ACTION}/confirm"
HEADERS: Final = {"X-Business-Id": "paws-and-co"}
BODY: Final = {"customer_ref": "cust-001", "confirm_token": TOKEN}
TRACE_PATTERN: Final = r"^[0-9a-f]{32}$"

EXECUTED: Final = ConfirmResponse(
    action_id=ACTION,
    tool=ToolName.BOOK,
    status=ActionStatus.EXECUTED,
    result=BookOutput(booking_id="B-1", slot_id="S-09"),
    message="Done: the confirmed action was carried out.",
)
FAILED: Final = ConfirmResponse(
    action_id=ACTION,
    tool=ToolName.BOOK,
    status=ActionStatus.FAILED,
    failure_reason=WriteFailure.SLOT_UNAVAILABLE,
    message="That slot is no longer free, so nothing was booked.",
)


@dataclass
class StandIn:
    """The confirm service: answers with `answer` (a response or an exception to raise)."""

    answer: ConfirmResponse | BaseException
    calls: list[tuple[str, UUID, ConfirmRequest, str]] = field(default_factory=list)

    async def confirm(
        self, business_id: str, action_id: UUID, request: ConfirmRequest, *, trace_id: str
    ) -> ConfirmResponse:
        self.calls.append((business_id, action_id, request, trace_id))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@contextmanager
def served(
    settings: Settings, answer: ConfirmResponse | BaseException
) -> Iterator[tuple[TestClient, StandIn]]:
    """The real app with the stand-in as its confirm service (set after startup)."""
    app = create_app(settings)
    stand_in = StandIn(answer)
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.confirm_service = stand_in
        yield client, stand_in


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    registry_says(monkeypatch, exists=True)


def post(
    answer: ConfirmResponse | BaseException,
    settings: Settings,
    body: dict[str, Any] = BODY,
    path: str = PATH,
) -> tuple[Any, StandIn]:
    with served(settings, answer) as (client, stand_in):
        return client.post(path, json=body, headers=HEADERS), stand_in


@pytest.mark.usefixtures("registered")
def test_production_refuses_the_route_before_the_service() -> None:
    settings = make_settings(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="not-a-real-key",
        api_allow_paid_llm=True,
        embeddings_provider="voyage",
        voyage_api_key="not-a-real-key",
    )

    response, stand_in = post(EXECUTED, settings)

    assert (response.status_code, response.json()) == (400, {"detail": REFUSED_ENVIRONMENT})
    assert stand_in.calls == []


@pytest.mark.usefixtures("registered")
def test_an_executed_action_is_200_with_its_result_and_a_trace_id(settings: Settings) -> None:
    response, stand_in = post(EXECUTED, settings)

    assert response.status_code == 200
    assert response.json() == json.loads(EXECUTED.model_dump_json())
    [(business, action, request, trace)] = stand_in.calls
    assert (business, action, request.customer_ref) == ("paws-and-co", ACTION, "cust-001")
    assert request.confirm_token.get_secret_value() == TOKEN
    assert response.headers["X-Trace-Id"] == trace


@pytest.mark.usefixtures("registered")
def test_a_failed_action_is_409_with_the_same_body_shape(settings: Settings) -> None:
    response, stand_in = post(FAILED, settings)

    assert response.status_code == 409
    assert response.json() == json.loads(FAILED.model_dump_json())
    assert response.headers["X-Trace-Id"] == stand_in.calls[0][3]


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ActionNotFound(), 404, "action_not_found"),
        (ConfirmFailed(), 503, "confirm_failed"),
        (OutcomeUnknown(), 503, "outcome_unknown"),
    ],
)
def test_typed_failures_have_fixed_bodies_and_a_trace_id(
    settings: Settings, error: Exception, status: int, code: str
) -> None:
    response, stand_in = post(error, settings)

    assert response.status_code == status and response.json()["error"] == code
    assert set(response.json()) == {"detail", "error"}
    assert response.headers["X-Trace-Id"] == stand_in.calls[0][3]
    if status == 404:
        assert response.json()["detail"] == ACTION_NOT_FOUND


@pytest.mark.usefixtures("registered")
def test_a_bug_is_a_500_never_a_typed_error(settings: Settings) -> None:
    response, _ = post(KeyError("a bug"), settings)

    assert response.status_code == 500 and "X-Trace-Id" not in response.headers


@pytest.mark.usefixtures("registered")
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(BODY | {"slot_id": "S-17"}, id="tool arguments"),
        pytest.param(BODY | {"arguments": {"slot_id": "S-17"}}, id="nested arguments"),
        pytest.param({"customer_ref": "cust-001"}, id="no token"),
        pytest.param({"confirm_token": TOKEN}, id="no customer"),
        pytest.param(BODY | {"confirm_token": TOKEN + "x"}, id="long token"),
        pytest.param(BODY | {"customer_ref": "Cust 001"}, id="bad customer"),
    ],
)
def test_the_body_is_closed_and_a_422_echoes_nothing(
    settings: Settings, body: dict[str, Any]
) -> None:
    response, stand_in = post(EXECUTED, settings, body=body)

    assert response.status_code == 422 and stand_in.calls == []
    assert TOKEN not in response.text and "Cust 001" not in response.text
    assert "S-17" not in response.text


@pytest.mark.usefixtures("registered")
def test_a_malformed_action_id_is_a_422(settings: Settings) -> None:
    response, stand_in = post(EXECUTED, settings, path="/v1/agent/actions/S-17/confirm")

    assert response.status_code == 422 and stand_in.calls == [] and "S-17" not in response.text


def test_without_a_business_the_route_is_400(settings: Settings) -> None:
    with served(settings, EXECUTED) as (client, stand_in):
        response = client.post(PATH, json=BODY)

    assert response.status_code == 400 and stand_in.calls == []


def test_the_token_travels_in_the_body_never_the_path(settings: Settings) -> None:
    operation = create_app(settings).openapi()["paths"]["/v1/agent/actions/{action_id}/confirm"][
        "post"
    ]

    assert [p["name"] for p in operation["parameters"]] == ["action_id", "X-Business-Id"]
    assert "requestBody" in operation
