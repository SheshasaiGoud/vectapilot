"""M7 PR 6: limits on the confirm route (G15) and `POST /v1/extractions` (`guards.routes`). Unit.

Through the real app (`create_app`, the real guard folder `data/safety/`, the sample businesses'
tool and extraction files) with the fake limiter (unit tests run with a dead Redis). The confirm
service and the extraction store are stand-ins set after startup, as tests/actions and
tests/extraction do.

- **Confirm (G15):** business → body → the limiter pre-check → the service. A 429 has
  `Retry-After` below the action's lifetime and, for a health business, its guidance; Redis down
  or a raising check is a typed 503 `limits_unavailable`, distinct from confirm's own 503s; in
  both the service never runs, so a 429 can never follow a write. Confirm's own responses pass
  byte for byte, and a bug inside it is still a 500 (nothing of M7 encloses the call).
- **Extractions:** the limits run before the body is read: a limited caller costs no body read,
  no parse and no stored row.
- **One `guard` record per request**, written before the service, sharing the trace id.
- **Off** (`guard_routes = "off"`): the limiter is never called.
"""

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.actions.test_confirm_route import BODY, EXECUTED, FAILED, PATH, StandIn
from tests.conftest import make_settings
from tests.extraction.helpers import EXTRACTION_DIR
from tests.extraction.served import PDF
from tests.extraction.served import RecordingStore as ExtractionStore
from tests.extraction.standins import REFERRAL_LINES, pdf
from tests.guards.helpers import (
    SAFETY_DIR,
    SAMPLE_CORPUS_DIR,
    SANDBOX_DIR,
    read_json,
    use_answer_key_selftest,
)
from vectapilot_api.actions.types import ActionNotFound, ConfirmFailed, OutcomeUnknown
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.guards.config import load_guard_dir
from vectapilot_api.guards.fake_limiter import REDIS_DOWN, FakeLimiter, LimiterCall
from vectapilot_api.guards.limits import GuardRefused, RefusalKind
from vectapilot_api.guards.routes import (
    LIMITS_UNAVAILABLE,
    LIMITS_UNAVAILABLE_DETAIL,
    RouteLimits,
)
from vectapilot_api.guards.types import GUARD_RECORD, RouteSlug
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.main import create_app
from vectapilot_api.retrieval.router import TRACE_HEADER

PAWS: Final = "paws-and-co"
SUNRISE: Final = "sunrise-clinic"
GUIDANCE: Final = {
    business: list(read_json(SAFETY_DIR / "businesses" / f"{business}.json")["guidance"])
    for business in (PAWS, SUNRISE)
}
UNAVAILABLE: Final = {"detail": LIMITS_UNAVAILABLE_DETAIL, "error": LIMITS_UNAVAILABLE}
EXTRACTIONS: Final = "/v1/extractions"
REFERRAL: Final = pdf(list(REFERRAL_LINES))


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", business_exists)
    use_answer_key_selftest(monkeypatch)


@contextmanager
def served(
    caplog: pytest.LogCaptureFixture,
    *,
    limiter: FakeLimiter | None = None,
    confirm: StandIn | None = None,
    store: ExtractionStore | None = None,
    **overrides: Any,
) -> Iterator[tuple[TestClient, FastAPI]]:
    fields = {
        "tools_config_dir": SANDBOX_DIR,
        "extraction_config_dir": EXTRACTION_DIR,
        "corpus_dir": SAMPLE_CORPUS_DIR,
        "guard_config_dir": SAFETY_DIR,
    }
    app = create_app_with(make_settings(**(fields | overrides)), limiter or FakeLimiter())
    root = logging.getLogger()
    root.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="vectapilot_api.guards")
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.confirm_service = confirm or StandIn(EXECUTED)
            app.state.extraction_store = store or ExtractionStore()
            yield client, app
    finally:
        root.removeHandler(caplog.handler)


def create_app_with(settings: Settings, limiter: FakeLimiter) -> FastAPI:
    return create_app(settings, limiter=limiter)


def post_confirm(client: TestClient, body: dict[str, Any] = BODY, **headers: str) -> Any:
    return client.post(PATH, json=body, headers={BUSINESS_HEADER: PAWS} | headers)


def post_extraction(client: TestClient, content: bytes = REFERRAL) -> Any:
    return client.post(
        EXTRACTIONS,
        content=content,
        params={"kind": "referral_letter"},
        headers={BUSINESS_HEADER: SUNRISE, "Content-Type": PDF},
    )


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    found = [r for r in caplog.records if r.getMessage() == GUARD_RECORD]
    return [getattr(r, GUARD_RECORD) for r in found]


# ── The confirm route (G15) ──────────────────────────────────────────────────────────────────


def test_confirm_checks_its_limits_after_the_body_and_before_the_service(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = FakeLimiter()
    stand_in = StandIn(EXECUTED)
    with served(caplog, limiter=limiter, confirm=stand_in) as (client, _):
        response = post_confirm(client)
        no_business = client.post(PATH, json=BODY)
        bad_body = post_confirm(client, {"customer_ref": "cust-001"})

    assert response.status_code == 200
    assert response.json() == json.loads(EXECUTED.model_dump_json())  # byte for byte
    assert (no_business.status_code, bad_body.status_code) == (400, 422)
    # Counted once, for the business and its customer, and only for the request that got there.
    assert limiter.calls == [LimiterCall("admit", PAWS, RouteSlug.CONFIRM, customered=True)]
    assert len(stand_in.calls) == 1
    [record] = records(caplog)
    assert (record["route"], record["action"], record["decided_by"]) == ("confirm", "pass", "none")
    assert record["checks"] == ["limits"]
    assert record["trace_id"] == response.headers[TRACE_HEADER] == stand_in.calls[0][3]


def test_a_full_window_is_a_429_before_any_write_with_the_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stand_in = StandIn(EXECUTED)
    limiter = FakeLimiter(limit="refuse", retry_after_s=30)
    with served(caplog, limiter=limiter, confirm=stand_in) as (client, _):
        response = post_confirm(client)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    body = response.json()
    assert (body["error"], body["retry_after_seconds"]) == ("rate_limited", 30)
    assert body["guidance"] == GUIDANCE[PAWS]  # a veterinary business: a health type (G3)
    assert stand_in.calls == []  # never a write before a 429 (G15)
    [record] = records(caplog)
    assert (record["action"], record["decided_by"], record["guidance"]) == ("limit", "limit", True)
    assert record["trace_id"] == response.headers[TRACE_HEADER]


def test_retry_after_stays_below_the_actions_lifetime(caplog: pytest.LogCaptureFixture) -> None:
    limiter = FakeLimiter(limit="refuse", retry_after_s=60)
    with served(caplog, limiter=limiter, action_lifetime_seconds=20) as (client, _):
        response = post_confirm(client)

    assert response.headers["Retry-After"] == "19"
    assert response.json()["retry_after_seconds"] == 19


@pytest.mark.parametrize(
    ("limiter", "decided_by", "error_type"),
    [
        (FakeLimiter(limit="raise"), "limits_unavailable", REDIS_DOWN),
        (None, "guard_error", "RuntimeError"),
    ],
    ids=["redis down", "a raising check"],
)
def test_when_the_limits_cannot_be_checked_confirm_is_a_typed_503_and_never_runs(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    limiter: FakeLimiter | None,
    decided_by: str,
    error_type: str,
) -> None:
    if limiter is None:
        limiter = FakeLimiter()

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("a bug in the limiter")

        monkeypatch.setattr(limiter, "admit", broken)
    stand_in = StandIn(EXECUTED)
    with served(caplog, limiter=limiter, confirm=stand_in) as (client, _):
        response = post_confirm(client)

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert "a bug" not in response.text
    assert stand_in.calls == []
    [record] = records(caplog)
    assert (record["action"], record["decided_by"]) == ("unavailable", decided_by)
    assert record["error_type"] == error_type
    assert record["trace_id"] == response.headers[TRACE_HEADER]


@pytest.mark.parametrize(
    "answer",
    [FAILED, ActionNotFound(), ConfirmFailed(), OutcomeUnknown()],
    ids=["409", "404", "503 confirm_failed", "503 outcome_unknown"],
)
def test_confirms_own_responses_pass_byte_for_byte(
    caplog: pytest.LogCaptureFixture, answer: Any
) -> None:
    with served(caplog, confirm=StandIn(answer)) as (client, _):
        guarded = post_confirm(client)
    with served(caplog, confirm=StandIn(answer), guard_routes="off") as (client, _):
        unguarded = post_confirm(client)

    assert (guarded.status_code, guarded.content) == (unguarded.status_code, unguarded.content)


def test_a_bug_inside_confirm_is_still_a_500(caplog: pytest.LogCaptureFixture) -> None:
    # No guard scope or exception mapping encloses the confirm call (G15).
    with served(caplog, confirm=StandIn(RuntimeError("a bug"))) as (client, _):
        response = post_confirm(client)

    assert response.status_code == 500


def test_with_the_guards_off_confirm_never_calls_the_limiter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = FakeLimiter(limit="raise")
    with served(caplog, limiter=limiter, guard_routes="off") as (client, app):
        response = post_confirm(client)
        assert (app.state.confirm_limits, app.state.extraction_limits) == (None, None)

    assert response.status_code == 200
    assert limiter.calls == [] and records(caplog) == []


def test_the_openapi_lists_both_429s(caplog: pytest.LogCaptureFixture) -> None:
    with served(caplog) as (_, app):
        paths = app.openapi()["paths"]
    assert "429" in paths["/v1/agent/actions/{action_id}/confirm"]["post"]["responses"]
    assert "429" in paths[EXTRACTIONS]["post"]["responses"]


# ── /v1/extractions ──────────────────────────────────────────────────────────────────────────


def test_extraction_limits_run_before_the_body_is_read(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def never(*args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("the body must not be read")

    monkeypatch.setattr("vectapilot_api.extraction.router.read_body", never)
    store = ExtractionStore()
    limiter = FakeLimiter(limit="refuse", retry_after_s=12)
    with served(caplog, limiter=limiter, store=store) as (client, _):
        response = post_extraction(client)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "12"
    assert response.json()["guidance"] == GUIDANCE[SUNRISE]
    assert store.saved == []
    assert limiter.calls == [LimiterCall("admit", SUNRISE, RouteSlug.EXTRACTION, customered=False)]
    [record] = records(caplog)
    assert (record["route"], record["decided_by"]) == ("extraction", "limit")


def test_redis_down_is_a_typed_503_and_nothing_is_stored(caplog: pytest.LogCaptureFixture) -> None:
    store = ExtractionStore()
    with served(caplog, limiter=FakeLimiter(limit="raise"), store=store) as (client, _):
        response = post_extraction(client)

    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert TRACE_HEADER in response.headers
    assert store.saved == []


def test_an_admitted_extraction_is_stored_under_the_guard_records_trace_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = ExtractionStore()
    with served(caplog, store=store) as (client, _):
        response = post_extraction(client)

    assert response.status_code == 201, response.text
    [saved] = store.saved
    [record] = records(caplog)
    assert record["decided_by"] == "none"
    assert record["trace_id"] == response.headers[TRACE_HEADER] == saved.trace_id


def test_with_the_guards_off_extractions_never_call_the_limiter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = FakeLimiter(limit="raise")
    with served(caplog, limiter=limiter, guard_routes="off") as (client, _):
        response = post_extraction(client)

    assert response.status_code == 201
    assert limiter.calls == []


# ── RouteLimits on its own ───────────────────────────────────────────────────────────────────


def admit(limits: RouteLimits, business: str) -> Any:
    return asyncio.run(limits.admit(business, trace_id="0123456789abcdef0123456789abcdef"))


@pytest.mark.parametrize(
    "lines", [(), ("Call the shop (fictional).",)], ids=["no lines", "tools.json lines"]
)
def test_a_business_that_is_not_health_gets_no_guidance_and_no_finding(
    caplog: pytest.LogCaptureFixture, lines: tuple[str, ...]
) -> None:
    # `refusal_decision`: guidance only for a health business, even when tools.json has lines
    # (mutation probe R10).
    caplog.set_level(logging.INFO, logger="vectapilot_api.guards")
    limits = RouteLimits(
        route=RouteSlug.CONFIRM,
        config=None,
        limiter=FakeLimiter(limit="refuse"),
        types={"corner-shop": BusinessType.RETAIL},
        guidance={"corner-shop": lines},
    )

    response = admit(limits, "corner-shop")

    assert json.loads(response.body)["guidance"] == []
    [record] = records(caplog)
    assert (record["guidance"], record["findings"]) == (False, {})


def test_without_a_guard_folder_a_served_health_business_gets_its_tools_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # G3 and the module docstring: health by either source (here only the type the route serves
    # it as), and tools.json's lines where no guard file has any (here: no guard folder).
    # Mutation probes R03 and R04.
    caplog.set_level(logging.INFO, logger="vectapilot_api.guards")
    limits = RouteLimits(
        route=RouteSlug.CONFIRM,
        config=None,
        limiter=FakeLimiter(limit="refuse"),
        types={"fx-clinic": BusinessType.HUMAN_HEALTHCARE},
        guidance={"fx-clinic": ("Call the fixture line, 000 (fictional).",)},
    )

    response = admit(limits, "fx-clinic")

    assert json.loads(response.body)["guidance"] == ["Call the fixture line, 000 (fictional)."]
    [record] = records(caplog)
    assert (record["guidance"], record["findings"]) == (True, {})


def test_an_unresolved_business_counts_as_health_and_records_missing_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="vectapilot_api.guards")
    config = load_guard_dir(SAFETY_DIR, SAMPLE_CORPUS_DIR, served={})
    limits = RouteLimits(
        route=RouteSlug.CONFIRM,
        config=config,
        limiter=FakeLimiter(limit="refuse"),
        types={},
        guidance={},
    )

    response = admit(limits, "no-file-shop")

    assert json.loads(response.body)["guidance"] == []
    [record] = records(caplog)
    assert (record["guidance"], record["findings"]) == (True, {"no_guidance": 1})


def test_a_budget_refusal_from_the_check_is_a_503_and_the_cap_is_validated() -> None:
    class BudgetRefusing(FakeLimiter):
        async def admit(self, *args: Any, **kwargs: Any) -> None:
            raise GuardRefused(RefusalKind.BUDGET)

    limits = RouteLimits(
        route=RouteSlug.EXTRACTION,
        config=None,
        limiter=BudgetRefusing(),
        types={},
        guidance={},
    )
    assert admit(limits, "corner-shop").status_code == 503
    with pytest.raises(ValueError, match="at least one second"):
        RouteLimits(
            route=RouteSlug.CONFIRM,
            config=None,
            limiter=FakeLimiter(),
            types={},
            guidance={},
            max_retry_after_s=0,
        )
