"""`POST /v1/agent` through the real app (M5 PR 4): W11 under the default settings, the closed
body, the trace id, the token in the JSON only, and no path to the harness's `router=none`.

No database: only the registry lookup of `X-Business-Id` is replaced (as tests/answer/apps.py
does). The agent's model is a fake provider that must never be called on these paths.
"""

import ast
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from tests.agent.fakes import PAWS, SUNRISE, canned_loop, proposed
from tests.agent.test_service import (
    ALL,
    EMERGENCIES,
    CannedAnswers,
    RecordingStore,
)
from tests.answer.apps import gateway_for
from tests.conftest import make_settings
from tests.tools.helpers import CORPUS_DIR, REPO_ROOT, SANDBOX_DIR
from vectapilot_api.agent.router import get_agent_service
from vectapilot_api.agent.service import AgentService
from vectapilot_api.agent.types import ConfirmationRequired, LoopLimits
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.db import Database
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import TRACE_HEADER

SRC: Final = REPO_ROOT / "apps" / "api" / "src" / "vectapilot_api"
SETTINGS: Final = make_settings(tools_config_dir=SANDBOX_DIR, corpus_dir=CORPUS_DIR)


@contextmanager
def served(
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeProvider,
    service: AgentService | None = None,
    caplog: pytest.LogCaptureFixture | None = None,
) -> Iterator[TestClient]:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", business_exists)
    app = create_app(SETTINGS, agent_provider=provider)
    if service is not None:
        app.dependency_overrides[get_agent_service] = lambda: service
    root = logging.getLogger()
    if caplog is not None:
        root.addHandler(caplog.handler)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if caplog is not None:
            root.removeHandler(caplog.handler)


def post(client: TestClient, business: str | None, body: dict[str, Any]) -> Any:
    headers = {} if business is None else {BUSINESS_HEADER: business}
    return client.post("/v1/agent", json=body, headers=headers)


@pytest.mark.parametrize(("business", "message"), EMERGENCIES)
def test_an_emergency_with_a_booking_request_gets_the_guidance_never_a_proposal(
    monkeypatch: pytest.MonkeyPatch, business: str, message: str
) -> None:
    provider = FakeProvider()  # unscripted: any model call would fail the turn
    config = {"paws-and-co": PAWS, "sunrise-clinic": SUNRISE}[business]

    with served(monkeypatch, provider) as client:
        response = post(client, business, {"message": message, "customer_ref": "cust-001"})

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "handoff"
    assert (body["reason"], body["priority"]) == ("safety_floor_off", "normal")
    assert body["guidance"] == list(config.emergency_guidance)  # one line per item, in order
    assert response.headers[TRACE_HEADER] == body["trace_id"]
    assert provider.calls == []
    assert "confirm_token" not in body and "action_id" not in body


@pytest.mark.parametrize(
    "extra",
    [
        {"history": []},
        {"business_id": "sunrise-clinic"},
        {"confirm_token": "x" * 22},
        {"arguments": {"slot_id": "S-17"}},
    ],
    ids=["history", "business_id", "confirm_token", "arguments"],
)
def test_the_body_is_closed_and_echoes_nothing(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    body = {"message": "my private message", "customer_ref": "cust-001", **extra}

    with served(monkeypatch, FakeProvider()) as client:
        response = post(client, "paws-and-co", body)

    assert response.status_code == 422
    assert "my private message" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"message": "", "customer_ref": "cust-001"},
        {"message": "hi", "customer_ref": "Not A Ref"},
        {"message": "hi"},
    ],
    ids=["empty message", "bad customer", "no customer"],
)
def test_a_bad_body_is_a_422(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    with served(monkeypatch, FakeProvider()) as client:
        assert post(client, "paws-and-co", body).status_code == 422


def test_without_a_business_the_route_is_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    with served(monkeypatch, FakeProvider()) as client:
        response = post(client, None, {"message": "hi", "customer_ref": "cust-001"})

    assert response.status_code == 400


def test_a_proposals_token_is_in_the_json_response_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    envelope = proposed()
    assert envelope.proposal is not None
    store = RecordingStore()
    service = AgentService(
        ALL,
        gateway=gateway_for(FakeProvider()),
        store=store,
        answers=CannedAnswers(),
        limits=LoopLimits(max_steps=4),
        turn_timeout_s=35.0,
        loop=canned_loop(ConfirmationRequired(envelope.proposal)),
    )

    with served(monkeypatch, FakeProvider(), service, caplog) as client:
        response = post(client, "corner-shop", {"message": "Book S-17", "customer_ref": "cust-001"})

    body = response.json()
    assert (response.status_code, body["kind"]) == (200, "confirmation_required")
    assert body["summary"] == envelope.proposal.summary
    assert body["summary_language"] == "en"
    token = body["confirm_token"]
    assert len(token) == 22 and "*" not in token  # revealed, not masked
    logged = " ".join(r.getMessage() + repr(vars(r)) for r in caplog.records)
    assert token not in logged


def test_the_route_runs_the_configured_routing_and_never_the_harnesss() -> None:
    # `respond_unrouted` (router=none) is the eval harness's alone: no module under src/ names
    # it but the service that defines it and PR 6's harness (`tooleval/`), so no route reaches it.
    namers = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute | ast.Name)
        }
        if "respond_unrouted" in names:
            namers.append(path.relative_to(SRC).as_posix())
    # The definition is a FunctionDef name, not a Name or an Attribute.
    assert [name for name in namers if not name.startswith("tooleval/")] == []
    assert "respond_unrouted" in (SRC / "agent" / "service.py").read_text("utf-8")
    assert ".respond(" in (SRC / "agent" / "router.py").read_text("utf-8")


def test_the_openapi_response_lists_the_four_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(SETTINGS)
    schema = app.openapi()
    response = schema["paths"]["/v1/agent"]["post"]["responses"]["200"]
    kinds = {
        ref["$ref"].rsplit("/", 1)[-1]
        for ref in response["content"]["application/json"]["schema"]["oneOf"]
    }
    assert kinds == {
        "ReplyResponse",
        "ConfirmationResponse",
        "HandoffResponse",
        "AnswerKindResponse",
    }
