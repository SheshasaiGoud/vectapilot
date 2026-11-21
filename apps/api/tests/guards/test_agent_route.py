"""M7 PR 6: `POST /v1/agent` with the guards on, through the real app (`create_app`). Unit.

The real term files (`data/safety/`, PR 4), the sample businesses' tool files and corpus, the
fake limiter (unit tests run with a dead Redis) and `create_app(..., matcher=...)`'s test seam
with `LookupMatcher`, a fixed lookup that is not H1. Masking is H2's `mask_pii` (written
2026-11-20); output checks are H3's stub. On the sample businesses (health types, `agent_router`
off) a message the red-flag screen clears is masked and reaches W11's normal `safety_floor_off`,
a hand-off H3 passes byte for byte: these tests check the order, the outages and that every
refusal for a health business carries its guidance. A guard failing after the screen is a
test-only raising masker (`served(..., mask=...)`, never H2). The startup self-test runs on the
answer-key stand-in, never H1 (`use_answer_key_selftest`).
"""

import asyncio
import dataclasses
import functools
import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.conftest import make_settings
from tests.guards.agent_helpers import LookupMatcher, RaisingMask, RaisingMatcher
from tests.guards.helpers import (
    SAFETY_DIR,
    SAMPLE_CORPUS_DIR,
    SANDBOX_DIR,
    read_json,
    use_answer_key_selftest,
)
from tests.guards.test_settings import PRODUCTION
from vectapilot_api import main
from vectapilot_api.actions.confirm import ConfirmService
from vectapilot_api.agent.profiles import AgentProfile
from vectapilot_api.agent.responses import AgentRequest, HandoffReason, HandoffResponse
from vectapilot_api.agent.service import AGENT_TURN
from vectapilot_api.answer.service import AnswerService
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.db import Database
from vectapilot_api.gateway.errors import ProviderError
from vectapilot_api.gateway.gateway import LLMGateway
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.guards.agent import AgentGuard
from vectapilot_api.guards.budget import BudgetedGateway
from vectapilot_api.guards.executor import ScreeningToolExecutor
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.limits import GuardRefused
from vectapilot_api.guards.output import Matcher
from vectapilot_api.guards.routes import RouteLimits
from vectapilot_api.guards.screens import GUARD_PROPAGATE, Masker
from vectapilot_api.guards.search import ScreeningSearchService
from vectapilot_api.guards.selftest import screen_text
from vectapilot_api.guards.store import ScreeningProposalStore
from vectapilot_api.guards.types import GUARD_RECORD
from vectapilot_api.main import MATCHER_SEAM_REFUSED, create_app
from vectapilot_api.retrieval.router import TRACE_HEADER
from vectapilot_api.retrieval.search import SearchService
from vectapilot_api.tools.config import READ_TOOLS, WRITE_TOOLS
from vectapilot_api.tools.executor import ToolExecutor

SUNRISE: Final = "sunrise-clinic"
PAWS: Final = "paws-and-co"
GUIDANCE: Final = {
    business: tuple(read_json(SAFETY_DIR / "businesses" / f"{business}.json")["guidance"])
    for business in (SUNRISE, PAWS)
}
"""Each business's guidance lines, as its guard file holds them (verbatim from `emergency.md`)."""
TOOLS_GUIDANCE: Final = {
    business: tuple(read_json(SANDBOX_DIR / business / "tools.json")["emergency_guidance"])
    for business in (SUNRISE, PAWS)
}
BENIGN: Final = {SUNRISE: "What time do you open on Monday?", PAWS: "Is the shop open on Sunday?"}
SYNTHETIC_PHONE: Final = "+91 00000 20777"
"""Synthetic (the log canary's), in a shape H2's Indian phone pattern masks."""
TELUGU: Final = "\u0c2e\u0c3e \u0c15\u0c41\u0c15\u0c4d\u0c15"
"""A short Telugu phrase, as escapes: paws-and-co lists `te` as not covered (D3 (i))."""

FEE: Final = ", how much is the consultation fee?"
CHEST: Final = "chest pain" + FEE
FOLD_BYPASSES: Final = {
    "zwj": "c\u200dhest pain" + FEE,
    "zwnj": "c\u200chest pain" + FEE,
    "hyphen": "chest-pain" + FEE,
    "accent": "chést pain" + FEE,
    "zero-width space": "chest\u200bpain" + FEE,
}
"""zara's five fold bypasses (PR 1's review), written as escapes."""

ATTACKS: Final = {
    "chest pain plus a fee question": (SUNRISE, "I have chest pain" + FEE, "hh_chest_pain"),
    "I can't breathe, book me": (SUNRISE, "I can't breathe, book me", "hh_cant_breathe"),
    "pain in his chest (near)": (SUNRISE, "pain in his chest" + FEE, "hh_chest_near"),
    "rat poison plus a fee question": (PAWS, "My dog ate rat poison" + FEE, "vet_rat_poison"),
}


def lookup() -> LookupMatcher:
    """The fixed lookup every route test uses: each attack's message to its real term id, and
    every fold bypass to `hh_chest_pain` (module docstring: not H1)."""
    table: dict[str, set[str]] = {message: {term} for _, message, term in ATTACKS.values()}
    table[CHEST] = {"hh_chest_pain"}
    for message in FOLD_BYPASSES.values():
        table[message] = {"hh_chest_pain"}
    return LookupMatcher(table)


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def business_exists(self: Database, business_id: str) -> bool:
        return True

    monkeypatch.setattr(Database, "business_exists", business_exists)
    use_answer_key_selftest(monkeypatch)


@contextmanager
def served(
    caplog: pytest.LogCaptureFixture,
    *,
    limiter: FakeLimiter | None = None,
    matcher: Matcher | None = None,
    provider: FakeProvider | None = None,
    mask: Masker | None = None,
    **overrides: Any,
) -> Iterator[tuple[TestClient, FastAPI]]:
    """The real app; `mask` (TEST-ONLY, never H2) replaces the masker of the `AgentGuard`
    `create_app` builds, so a guard can fail after the screen cleared now that H2 is written."""
    fields = {
        "tools_config_dir": SANDBOX_DIR,
        "corpus_dir": SAMPLE_CORPUS_DIR,
        "guard_config_dir": SAFETY_DIR,
    }
    settings = make_settings(**(fields | overrides))
    with pytest.MonkeyPatch.context() as patch:
        if mask is not None:  # the guard is built in the lifespan, inside this context
            patch.setattr(main, "AgentGuard", functools.partial(AgentGuard, mask=mask))
        app = create_app(
            settings,
            agent_provider=provider or FakeProvider(),
            limiter=limiter or FakeLimiter(),
            matcher=matcher,
        )
        root = logging.getLogger()
        root.addHandler(caplog.handler)
        caplog.set_level(logging.INFO, logger="vectapilot_api.guards")
        try:
            with TestClient(app) as client:
                yield client, app
        finally:
            root.removeHandler(caplog.handler)


def post(client: TestClient, business: str, message: str) -> Any:
    return client.post(
        "/v1/agent",
        json={"message": message, "customer_ref": "cust-001"},
        headers={BUSINESS_HEADER: business},
    )


def guard_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, GUARD_RECORD) for r in caplog.records if r.getMessage() == GUARD_RECORD]


# ── zara's attack inputs: the red-flag screen first ──────────────────────────────────────────


@pytest.mark.parametrize(("business", "message", "term"), ATTACKS.values(), ids=ATTACKS.keys())
def test_an_emergency_is_urgent_with_guidance_before_the_limits_and_the_model(
    caplog: pytest.LogCaptureFixture, business: str, message: str, term: str
) -> None:
    limiter, provider, matcher = FakeLimiter(), FakeProvider(), lookup()

    with served(caplog, limiter=limiter, matcher=matcher, provider=provider) as (client, _):
        response = post(client, business, message)

    assert response.status_code == 200
    body = response.json()
    assert (body["kind"], body["reason"], body["priority"]) == ("handoff", "safety", "urgent")
    assert tuple(body["guidance"]) == GUIDANCE[business]
    assert response.headers[TRACE_HEADER] == body["trace_id"]
    # Before Redis, the budget and the model (G1); and before W11's normal `safety_floor_off`.
    assert limiter.calls == []
    assert provider.calls == []
    [record] = guard_records(caplog)
    assert (record["decided_by"], record["trace_id"]) == ("red_flag:emergency", body["trace_id"])
    assert message.lower() not in json.dumps(record).lower()


@pytest.mark.parametrize("message", FOLD_BYPASSES.values(), ids=FOLD_BYPASSES.keys())
def test_the_five_fold_bypasses_reach_the_screen_folded(
    caplog: pytest.LogCaptureFixture, message: str
) -> None:
    with served(caplog, matcher=lookup()) as (client, _):
        body = post(client, SUNRISE, message).json()

    assert (body["reason"], body["priority"]) == ("safety", "urgent")
    assert tuple(body["guidance"]) == GUIDANCE[SUNRISE]


def test_four_bypasses_fold_to_the_plain_text_and_the_glued_one_is_h1s_whitespace_rule() -> None:
    # What the screen sees: the fold undoes four of them. "chestpain" (a zero-width space between
    # the words) is matched by H1's `\s*` between a term's words (H1's contract, rule 2; its
    # strict xfail is in test_match.py); the lookup above lists it only to test the route.
    glued = FOLD_BYPASSES["zero-width space"]
    assert {screen_text(m) for k, m in FOLD_BYPASSES.items() if m != glued} == {screen_text(CHEST)}
    assert screen_text(glued) == screen_text(CHEST).replace("chest pain", "chestpain")


@pytest.mark.parametrize(
    ("outage", "limiter", "provider"),
    [
        ("redis down", FakeLimiter(limit="raise", budget="raise"), None),
        ("budget exhausted", FakeLimiter(budget="refuse", remaining_micro_usd=0), None),
        ("window full", FakeLimiter(limit="refuse"), None),
        ("provider failing", None, FakeProvider([ProviderError(status_code=500)])),
    ],
)
@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_an_emergency_during_an_outage_is_still_urgent_with_guidance(
    caplog: pytest.LogCaptureFixture,
    outage: str,
    limiter: FakeLimiter | None,
    provider: FakeProvider | None,
    business: str,
) -> None:
    message = next(m for b, m, _ in ATTACKS.values() if b == business)

    with served(caplog, limiter=limiter, provider=provider, matcher=lookup()) as (client, _):
        body = post(client, business, message).json()

    assert (body["reason"], body["priority"]) == ("safety", "urgent"), outage
    assert tuple(body["guidance"]) == GUIDANCE[business]


@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_a_guard_raising_during_an_emergency_is_urgent_with_guidance(
    caplog: pytest.LogCaptureFixture, business: str
) -> None:
    # The red-flag screen is the guard that raised: G3's priority before it cleared the message.
    with served(caplog, matcher=RaisingMatcher(RuntimeError("broken"))) as (client, _):
        body = post(client, business, "chest pain").json()

    assert (body["reason"], body["priority"]) == ("guard_error", "urgent")
    assert tuple(body["guidance"]) == GUIDANCE[business]
    [record] = guard_records(caplog)
    assert (record["decided_by"], record["error_type"]) == ("guard_error", "RuntimeError")


def test_with_h1_still_a_stub_every_message_fails_closed_urgent_with_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # No matcher seam: the route runs H1's `match_terms` itself (G14: matcher unavailable).
    with served(caplog) as (client, _):
        body = post(client, SUNRISE, BENIGN[SUNRISE]).json()

    assert (body["reason"], body["priority"]) == ("guard_error", "urgent")
    assert tuple(body["guidance"]) == GUIDANCE[SUNRISE]
    [record] = guard_records(caplog)
    assert (record["error_type"], record["checks"]) == ("NotImplementedError", ["clean"])


@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_h2_masks_a_phone_and_the_message_goes_on_to_the_service(
    caplog: pytest.LogCaptureFixture, business: str
) -> None:
    # H2 is written (2026-11-20): a phone in the message is masked, never a `guard_error`. On a
    # health business with `agent_router` off the service's W11 then hands off (M5, normal).
    message = f"{BENIGN[business]} Call me on {SYNTHETIC_PHONE}."

    with served(caplog, matcher=lookup()) as (client, _):
        body = post(client, business, message).json()

    assert (body["reason"], body["priority"]) == ("safety_floor_off", "normal")
    [record] = guard_records(caplog)
    assert (record["decided_by"], record["error_type"]) == ("none", None)
    assert "mask" in record["checks"]
    assert SYNTHETIC_PHONE not in json.dumps(record)


def test_the_agent_turn_and_the_guard_record_share_the_routes_trace_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The plan's PR 6 "Records" (leo's review): when the service runs, `agent_turn` (the loop's
    # outcome) and `guard` (what the customer got) both carry the trace id the route answers with.
    caplog.set_level(logging.INFO, logger="vectapilot_api.agent")
    with served(caplog, matcher=lookup()) as (client, _):
        response = post(client, SUNRISE, BENIGN[SUNRISE])

    trace_id = response.headers[TRACE_HEADER]
    turns = [getattr(r, AGENT_TURN) for r in caplog.records if r.getMessage() == AGENT_TURN]
    [turn], [record] = turns, guard_records(caplog)
    assert (turn["reason"], turn["trace_id"], record["trace_id"]) == (
        "safety_floor_off",
        trace_id,
        trace_id,
    )
    assert response.json()["trace_id"] == trace_id


# ── Every refusal for a health business carries its guidance (G3) ────────────────────────────


@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_every_refusal_for_a_health_business_carries_its_guidance(
    caplog: pytest.LogCaptureFixture, business: str
) -> None:
    benign = BENIGN[business]
    cases: list[tuple[str, dict[str, Any], str, str, str]] = [
        ("masker raising", {"mask": RaisingMask()}, benign, "guard_error", "high"),
        ("redis down", {"limiter": FakeLimiter(limit="raise")}, benign, "guard_error", "high"),
        ("budget", {"limiter": FakeLimiter(budget="refuse")}, benign, "budget", "high"),
    ]
    if business == PAWS:
        cases.append(("telugu", {}, TELUGU, "not_covered_language", "high"))
    for name, arrangement, message, reason, priority in cases:
        with served(caplog, matcher=lookup(), **arrangement) as (client, _):
            body = post(client, business, message).json()
        assert (body["reason"], body["priority"]) == (reason, priority), name
        assert tuple(body["guidance"]) == GUIDANCE[business], name


@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_a_429_carries_retry_after_the_trace_id_and_the_guidance(
    caplog: pytest.LogCaptureFixture, business: str
) -> None:
    limiter = FakeLimiter(limit="refuse", retry_after_s=42)

    with served(caplog, limiter=limiter, matcher=lookup()) as (client, _):
        response = post(client, business, BENIGN[business])

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert len(response.headers[TRACE_HEADER]) == 32
    body = response.json()
    assert (body["error"], body["retry_after_seconds"]) == ("rate_limited", 42)
    assert tuple(body["guidance"]) == GUIDANCE[business]
    [record] = guard_records(caplog)
    assert record["trace_id"] == response.headers[TRACE_HEADER]


@pytest.mark.parametrize("business", [SUNRISE, PAWS])
def test_without_a_guard_folder_a_health_business_is_urgent_with_its_tools_guidance(
    caplog: pytest.LogCaptureFixture, business: str
) -> None:
    with served(caplog, guard_config_dir=None) as (client, _):
        body = post(client, business, BENIGN[business]).json()

    assert (body["reason"], body["priority"]) == ("guards_not_configured", "urgent")
    assert tuple(body["guidance"]) == TOOLS_GUIDANCE[business]


def test_guard_routes_off_is_m5s_route_with_no_guard_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = FakeLimiter()

    with served(caplog, limiter=limiter, matcher=lookup(), guard_routes="off") as (client, _):
        body = post(client, SUNRISE, CHEST).json()

    assert (body["reason"], body["priority"]) == ("safety_floor_off", "normal")
    assert limiter.calls == []
    assert guard_records(caplog) == []


def test_the_openapi_lists_the_429(caplog: pytest.LogCaptureFixture) -> None:
    app = create_app(make_settings())
    responses = app.openapi()["paths"]["/v1/agent"]["post"]["responses"]

    assert {"200", "400", "422", "429"} <= set(responses)
    assert "Retry-After" not in json.dumps(responses["200"])


def test_the_matcher_seam_is_refused_outside_local_test_and_ci() -> None:
    with pytest.raises(ValueError, match="for tests only") as refused:
        create_app(make_settings(**PRODUCTION), matcher=lookup())
    assert str(refused.value) == MATCHER_SEAM_REFUSED


# ── The create_app seams (G13) ───────────────────────────────────────────────────────────────


def test_guarded_create_app_builds_the_seams_around_unchanged_services(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with served(caplog, matcher=lookup()) as (_, app):
        state = app.state
        full, read_only, guard = (
            state.agent_service,
            state.read_only_agent_service,
            state.agent_guard,
        )
        assert isinstance(guard, AgentGuard)
        # The agent's gateway is budgeted; its answers come from a second AnswerService over the
        # chunk-screening search and its own budgeted gateway. M3's instance is untouched.
        assert isinstance(full._gateway, BudgetedGateway)
        answers = full._answers
        assert type(answers) is AnswerService and answers is not state.answer_service
        assert type(answers._search) is ScreeningSearchService
        assert type(answers._gateway) is BudgetedGateway
        assert answers._gateway is not full._gateway
        assert type(state.answer_service._search) is SearchService
        assert type(state.answer_service._gateway) is LLMGateway
        # The store screens a ticket before the store `build_action_services` built.
        assert type(full._store) is ScreeningProposalStore
        assert full._store._store is state.pending_actions
        assert type(state.confirm_service) is ConfirmService
        assert full._propagate == (GuardRefused, *GUARD_PROPAGATE)
        # The read-only service: every constructor argument the same object but the profiles.
        differing = {
            name for name, value in vars(full).items() if vars(read_only)[name] is not value
        }
        assert differing == {"_profiles"}
        for business, profile in read_only._profiles.items():
            assert set(profile.config.enabled_tools) <= READ_TOOLS
            schema = json.dumps(profile.step_schema.model_json_schema())
            assert not any(f'"{tool.value}"' in schema for tool in WRITE_TOOLS), business
            assert profile.health is full._profiles[business].health is True  # W11 still applies
        # Every profile's executor screens its tool results (G7), in both services; the
        # confirm route and /v1/extractions check their limits first (G15).
        for served_profiles in (full._profiles, read_only._profiles):
            assert {type(p.executor) for p in served_profiles.values()} == {ScreeningToolExecutor}
        assert guard._profiles is full._profiles
        assert type(state.confirm_limits) is RouteLimits
        assert type(state.extraction_limits) is RouteLimits
        assert state.confirm_limits._max_retry_after_s == 899  # below the 900 s lifetime


def test_every_seam_screens_with_the_routes_matcher(caplog: pytest.LogCaptureFixture) -> None:
    # G13: `create_app`'s test seam reaches every screen, so no guarded test runs H1's stub
    # unseen (mutation probe N09: the chunk screen once took `match_terms` directly).
    matcher = lookup()
    with served(caplog, matcher=matcher) as (_, app):
        state = app.state
        assert state.agent_guard._matcher is matcher
        for service in (state.agent_service, state.read_only_agent_service):
            assert service._answers._search._matcher is matcher
            assert service._store._matcher is matcher
            for profile in service._profiles.values():
                assert profile.executor._matcher is matcher


ALLOWED_DIFFERENCES: Final = {
    "_loop": "the scripted replay stands in for M5's H1 (`run_turn`), one per service",
    "_answers": "agent_router off: the runner's answer step refuses; B on the agent route waits"
    " for PR 8's chunk runner (`redteam.plan.waiting_on`)",
}
"""What the red-team pipeline may build differently from `create_app`, each with its reason."""


def _comparable(value: object) -> object:
    """`value` as two independent builds can compare it: a model class by its JSON schema, an
    executor by its type and attributes (its per-business input models by their schemas), a
    profile field by field, a gateway or a store without its provider."""
    if isinstance(value, type) and issubclass(value, BaseModel):
        return value.model_json_schema()
    if isinstance(value, ToolExecutor):
        inputs = {name: model.model_json_schema() for name, model in value._inputs.items()}
        rest = {k: v for k, v in vars(value).items() if k != "_inputs"}
        return type(value), rest, inputs
    if isinstance(value, AgentProfile):
        return {f.name: _comparable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {key: _comparable(item) for key, item in value.items()}
    if isinstance(value, BudgetedGateway | ScreeningProposalStore):
        return type(value), {k: v for k, v in vars(value).items() if k != "_provider"}
    return value


def test_the_red_team_pipeline_is_create_apps_guarded_wiring(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # leo's PR 6 review: the red-team gate must measure the pipeline `create_app` serves. Both
    # services and the guard hold the same attributes, each equal or built alike, but for the
    # named differences; a seam added to or changed in `create_app` alone fails here.
    from tests.redteam.standins import filled
    from tests.redteam.suite import CASES
    from vectapilot_api.redteam.agent import GuardSeams, build_pipeline, served_world

    matcher = lookup()
    with served(caplog, matcher=matcher) as (_, app):
        state = app.state
        pipeline = build_pipeline(
            CASES["l-006"],
            filled("l-006").text,
            served_world(app),
            source=state.database,
            store=state.pending_actions,
            limiter=state.limiter,
            seams=GuardSeams(matcher=matcher),
        )
        pairs = (
            (pipeline.service, state.agent_service),
            (pipeline.read_only, state.read_only_agent_service),
            (pipeline.guard, state.agent_guard),
        )
        differing: set[str] = set()
        for built, served_one in pairs:
            assert vars(built).keys() == vars(served_one).keys()
            for name, value in vars(served_one).items():
                if _comparable(vars(built)[name]) != _comparable(value):
                    differing.add(name)
        read_only = {b: p.config.enabled_tools for b, p in pipeline.read_only._profiles.items()}

    assert differing == set(ALLOWED_DIFFERENCES)
    assert read_only == {
        b: p.config.enabled_tools for b, p in state.read_only_agent_service._profiles.items()
    }


def test_the_read_only_service_still_hands_a_health_business_off_by_w11(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # W11 runs before the loop and any tool, so the service is called on its own here.
    request = AgentRequest(message=BENIGN[SUNRISE], customer_ref="cust-001")
    with served(caplog, matcher=lookup()) as (_, app):
        run = asyncio.run(
            app.state.read_only_agent_service.respond(SUNRISE, request, trace_id="1" * 32)
        )

    assert isinstance(run.response, HandoffResponse)
    assert run.response.reason is HandoffReason.SAFETY_FLOOR_OFF


def test_unguarded_create_app_builds_exactly_m5s_service(caplog: pytest.LogCaptureFixture) -> None:
    with served(caplog, guard_routes="off") as (_, app):
        state = app.state
        service = state.agent_service
        assert (state.agent_guard, state.read_only_agent_service) == (None, None)
        assert type(service._gateway) is LLMGateway
        assert service._answers is state.answer_service
        assert service._store is state.pending_actions
        assert service._propagate == ()
        assert {type(p.executor) for p in service._profiles.values()} == {ToolExecutor}
        assert (state.confirm_limits, state.extraction_limits) == (None, None)
