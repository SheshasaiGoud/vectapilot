"""The X-Business-Id dependency (ADR 0009), without a database.

`/probe` exists only in these tests. The registry lookup is replaced where a test needs an answer;
the real lookup (a registered and an unregistered id) is in test_rls.py.
"""

from collections.abc import Iterator
from typing import Annotated, Literal

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from vectapilot_api.business import (
    DATABASE_UNAVAILABLE,
    HEADER_ENVIRONMENTS,
    REFUSED_ENVIRONMENT,
    UNKNOWN_BUSINESS,
    get_business_id,
)
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.main import create_app

UNKNOWN = {"detail": UNKNOWN_BUSINESS}


def probe_app(settings: Settings) -> FastAPI:
    """The real app plus one route that only echoes the business it was given."""
    app = create_app(settings)

    @app.get("/probe")
    async def probe(
        business_id: Annotated[BusinessId, Depends(get_business_id)],
    ) -> dict[str, str]:
        return {"business_id": business_id}

    return app


@pytest.fixture
def probe(settings: Settings) -> Iterator[TestClient]:
    with TestClient(probe_app(settings)) as client:
        yield client


def registry_says(monkeypatch: pytest.MonkeyPatch, exists: bool) -> None:
    async def business_exists(self: Database, business_id: str) -> bool:
        return exists

    monkeypatch.setattr(Database, "business_exists", business_exists)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"X-Business-Id": ""}, id="empty"),
        pytest.param({"X-Business-Id": "ab"}, id="too short"),
        pytest.param({"X-Business-Id": "a" * 41}, id="too long"),
        pytest.param({"X-Business-Id": "Sunrise-Clinic"}, id="uppercase"),
        pytest.param({"X-Business-Id": "sunrise_clinic"}, id="underscore"),
        pytest.param({"X-Business-Id": "sunrise--clinic"}, id="double hyphen"),
        pytest.param({"X-Business-Id": "../etc"}, id="path"),
        pytest.param([("X-Business-Id", "sunrise-clinic")] * 2, id="repeated"),
    ],
)
def test_malformed_ids_get_the_unknown_business_error(
    probe: TestClient, headers: dict[str, str] | list[tuple[str, str]]
) -> None:
    # The database is unreachable here: a 400 (not a 503) shows no query was made.
    response = probe.get("/probe", headers=headers)

    assert (response.status_code, response.json()) == (400, UNKNOWN)


def test_an_unknown_id_gets_the_same_error_as_a_malformed_one(
    probe: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_says(monkeypatch, exists=False)

    unknown = probe.get("/probe", headers={"X-Business-Id": "no-such-business"})
    malformed = probe.get("/probe", headers={"X-Business-Id": "No Such Business"})

    assert (unknown.status_code, unknown.json()) == (malformed.status_code, malformed.json())
    assert unknown.json() == UNKNOWN


def test_a_registered_id_reaches_the_route(
    probe: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_says(monkeypatch, exists=True)

    response = probe.get("/probe", headers={"x-business-id": "sunrise-clinic"})  # any letter case

    assert (response.status_code, response.json()) == (200, {"business_id": "sunrise-clinic"})


def test_a_database_outage_is_a_503(probe: TestClient) -> None:
    response = probe.get("/probe", headers={"X-Business-Id": "sunrise-clinic"})

    assert (response.status_code, response.json()) == (503, {"detail": DATABASE_UNAVAILABLE})


REFUSED = {"detail": REFUSED_ENVIRONMENT}


@pytest.mark.parametrize("header", ["sunrise-clinic", "Not Valid"])
def test_production_refuses_the_header(header: str, monkeypatch: pytest.MonkeyPatch) -> None:
    registry_says(monkeypatch, exists=True)  # even a registered business is refused
    settings = make_settings(
        environment="production",
        llm_provider="anthropic",
        anthropic_api_key="not-a-real-key",
        api_allow_paid_llm=True,  # production is never fake, so the API needs the opt-in
        embeddings_provider="voyage",
        voyage_api_key="not-a-real-key",
    )

    with TestClient(probe_app(settings)) as client:
        response = client.get("/probe", headers={"X-Business-Id": header})

    assert (response.status_code, response.json()) == (400, REFUSED)


@pytest.mark.parametrize("environment", ["staging", "demo", "Local", ""])
def test_an_unlisted_environment_refuses_the_header(
    environment: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_says(monkeypatch, exists=True)
    app = probe_app(settings)

    with TestClient(app) as client:
        # Settings admits only its four environments today; `model_copy` skips validation, so this
        # stands in for one added later. The allowlist must refuse it until it is listed.
        app.state.settings = settings.model_copy(update={"environment": environment})
        response = client.get("/probe", headers={"X-Business-Id": "sunrise-clinic"})

    assert (response.status_code, response.json()) == (400, REFUSED)


@pytest.mark.parametrize("environment", ["local", "test", "ci"])
def test_only_local_test_and_ci_accept_the_header(
    environment: Literal["local", "test", "ci"], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert frozenset({"local", "test", "ci"}) == HEADER_ENVIRONMENTS  # widening it needs a review
    registry_says(monkeypatch, exists=True)

    with TestClient(probe_app(make_settings(environment=environment))) as client:
        response = client.get("/probe", headers={"X-Business-Id": "sunrise-clinic"})

    assert (response.status_code, response.json()) == (200, {"business_id": "sunrise-clinic"})
