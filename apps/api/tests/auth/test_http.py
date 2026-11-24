"""`auth.http` (M8 PR 1; S1, S9): a session route's 422 names our own locations and replaces an
unknown key's name, the one location a client writes; and every answer a `SessionRoute` makes
carries `no-store`, a typed error raised elsewhere and FastAPI's own refusal included. The routes'
own 422s are in `tests/widget/test_routes.py` and `tests/staff/test_routes.py`."""

from collections.abc import Iterator

import pytest
from fastapi import APIRouter, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from tests.conftest import make_settings
from vectapilot_api.auth.http import UNKNOWN_KEY, UNREADABLE_REQUEST, SessionRoute, closed_problems
from vectapilot_api.kb.types import KbError
from vectapilot_api.main import create_app

MARKER = "zqxmarker"


def test_only_an_unknown_keys_name_is_replaced() -> None:
    error = RequestValidationError(
        [
            {"loc": ("body", "zqxmarker"), "type": "extra_forbidden", "input": "x"},
            {"loc": ("body", "ui_language"), "type": "string_pattern_mismatch", "input": "x"},
            {"loc": (), "type": "extra_forbidden", "input": "x"},
            {"type": "missing"},
        ]
    )

    assert closed_problems(error) == [
        {"loc": ["body", UNKNOWN_KEY], "type": "extra_forbidden"},
        {"loc": ["body", "ui_language"], "type": "string_pattern_mismatch"},
        {"loc": [], "type": "extra_forbidden"},
        {"loc": [], "type": "missing"},
    ]


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The real app plus two probe routes of the session route class."""
    router = APIRouter(route_class=SessionRoute)

    @router.get("/probe/typed-error")
    async def typed_error() -> None:
        # A typed error built elsewhere (PR 2's messages, PR 3's queue) with no headers of its own.
        raise KbError(409, "probe_conflict", "a fixed message")

    @router.get("/probe/http-exception")
    async def http_exception() -> None:
        raise HTTPException(418, detail=MARKER, headers={"x-probe": MARKER})

    app = create_app(make_settings())
    app.include_router(router)
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def test_a_typed_error_raised_without_headers_still_gets_no_store(client: TestClient) -> None:
    response = client.get("/probe/typed-error")

    assert response.status_code == 409
    assert response.json() == {"detail": "a fixed message", "error": "probe_conflict"}
    assert response.headers["cache-control"] == "no-store"


def test_fastapis_own_refusal_is_typed_fixed_text_and_no_store(client: TestClient) -> None:
    # Zara's review: FastAPI raises its HTTPException (400) for a body that is not UTF-8, before any
    # dependency runs; a session route answers it typed, with fixed text, and no-store.
    response = client.get("/probe/http-exception")

    assert response.status_code == 418  # the original status
    assert response.json() == {"detail": UNREADABLE_REQUEST, "error": "invalid_request"}
    assert response.headers["cache-control"] == "no-store"
    assert MARKER not in response.text and "x-probe" not in response.headers  # nothing echoed
