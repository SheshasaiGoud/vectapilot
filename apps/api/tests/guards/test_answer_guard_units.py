"""M7 PR 8: `AnswerGuard` on its own, for the paths the routes cannot reach. Unit.

The fixture guard folder (tests/guards/fixtures: a clinic, a vet with Telugu not covered, a shop),
`LookupMatcher` (a fixed lookup, not H1), `IdentityMask` (not H2) and `FixedCheck` (not H3), with
the answer service over the retrieval stand-ins (no SQL). Request validation bounds a question
before any route reaches the guard, so its input caps are reached here with `model_construct`.
"""

import asyncio
import logging
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Final

import pytest
from fastapi.responses import JSONResponse

from tests.answer.providers import reply
from tests.guards.agent_helpers import (
    CLINIC,
    SHOP,
    FixedCheck,
    IdentityMask,
    LookupMatcher,
    RaisingMask,
    RaisingMatcher,
    budgeted,
    with_not_covered,
)
from tests.guards.helpers import load_fixtures
from tests.retrieval.standins import build_search, hit
from vectapilot_api.answer.service import AnswerService, RagConfig
from vectapilot_api.answer.types import AnswerRequest
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards import answer as answer_module
from vectapilot_api.guards.answer import AnswerGuard, AnswerSafetyHandoff, GuardedAnswer
from vectapilot_api.guards.config import GuardConfig
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.screens import GuardFailed
from vectapilot_api.guards.search import ScreeningSearchService
from vectapilot_api.guards.types import GUARD_RECORD, Finding, TermCategory
from vectapilot_api.intents.policy import BusinessType

TRACE: Final = "0123456789abcdef0123456789abcdef"
UNRESOLVED: Final = "clinic-without-a-file"
"""A business the guard folder has no file for: every term, health, no guidance (G4)."""
TELUGU: Final = "మా కుక్క"
CHEST: Final = "chest pain, what is the fee?"
KEPT: Final = hit("We open at 9.", 0.9)
CONFIG: Final = RagConfig(min_score=0.3, context_k=5, weak_evidence_outcome="dont_know")


@pytest.fixture(autouse=True)
def capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vectapilot_api.guards")


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, GUARD_RECORD) for r in caplog.records if r.getMessage() == GUARD_RECORD]


def guard(
    *,
    config: GuardConfig | None = None,
    limiter: FakeLimiter | None = None,
    matcher: Any = None,
    mask: Any = None,
    check: FixedCheck | None = None,
) -> AnswerGuard:
    return AnswerGuard(
        config=config or load_fixtures(),
        limiter=limiter or FakeLimiter(),
        matcher=matcher or LookupMatcher({CHEST: {"fx_chest_pain"}}),
        types={UNRESOLVED: BusinessType.HUMAN_HEALTHCARE},
        guidance={},
        check=check or FixedCheck(),
        mask=mask or IdentityMask(),
    )


def service(
    business: str, *, chunk_matcher: Any = None, provider: FakeProvider | None = None
) -> Any:
    search = build_search({business: [KEPT]})
    screening = ScreeningSearchService(
        search.database,
        search.service._runner,
        matcher=chunk_matcher or LookupMatcher(),
        search=search.standin,
        stored_models=search.stored,
    )
    answer = FakeReply(text=reply("answered", "We open at 9.", [str(KEPT.chunk_id)]))
    return AnswerService(
        screening, budgeted(provider or FakeProvider([answer]), FakeLimiter()), CONFIG
    )


def ask(built: AnswerGuard, business: str, question: str, svc: Any = "default") -> GuardedAnswer:
    request = AnswerRequest.model_construct(question=question)  # validation bounds it first (G1)
    chosen = service(business) if svc == "default" else svc
    return asyncio.run(built.answer(business, request, trace_id=TRACE, service=chosen))


def handoff(answered: GuardedAnswer) -> AnswerSafetyHandoff:
    assert isinstance(answered.response, AnswerSafetyHandoff), answered
    return answered.response


# ── Input caps (G3): behind validation, so only here ─────────────────────────────────────────


def test_an_over_cap_question_fails_closed_as_the_input_cap_on_both_routes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built = guard(matcher=RaisingMatcher())  # never reached: the cap comes first

    response = handoff(ask(built, CLINIC, "x" * 2_001))
    searched = asyncio.run(built.search(CLINIC, "x" * 2_001, trace_id=TRACE))

    assert (response.reason, response.priority) == ("guard_error", "urgent")
    assert isinstance(searched, JSONResponse) and searched.status_code == 503
    for record in records(caplog):
        assert (record["decided_by"], record["findings"]) == ("input_cap", {"over_cap": 1})
        assert record["error_type"] == "OverCap"


# ── Businesses without guidance (G4) ─────────────────────────────────────────────────────────


def test_an_unresolved_business_is_health_and_its_refusals_record_no_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    red_flag = handoff(ask(guard(), UNRESOLVED, CHEST))
    limited = guard(limiter=FakeLimiter(limit="refuse"))
    answered = ask(limited, UNRESOLVED, "Open?")
    searched = asyncio.run(limited.search(UNRESOLVED, "Open?", trace_id=TRACE))

    assert (red_flag.reason, red_flag.priority, red_flag.guidance) == ("safety", "urgent", ())
    for response in (answered.response, searched):
        assert isinstance(response, JSONResponse) and response.status_code == 429
    assert [record["findings"].get("no_guidance") for record in records(caplog)] == [1, 1, 1]


# ── Priorities and guidance for a business that is not health (G1, G3) ───────────────────────


def test_a_high_red_flag_on_a_non_health_business_still_shows_the_guidance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # G1: a red flag always carries the guidance, even when it is neither urgent nor on a health
    # business. TEST-ONLY: the shop gets the clinic's red-flag terms (a professional-judgement
    # term) and a guidance line, which the schema allows and today's files never do.
    config = load_fixtures()
    businesses = dict(config.businesses)
    clinic = businesses[BusinessId(CLINIC)]
    shop = replace(
        businesses[BusinessId(SHOP)],
        red_flags=clinic.red_flags,
        guidance=("TEST-ONLY: call the shop's emergency line.",),
    )
    businesses[BusinessId(SHOP)] = shop
    config = replace(config, businesses=MappingProxyType(businesses))
    judgement = next(
        t.id for t in clinic.red_flags.terms if t.category is TermCategory.PROFESSIONAL_JUDGEMENT
    )
    built = guard(config=config, matcher=LookupMatcher({"may i change my dose?": {judgement}}))

    response = handoff(ask(built, SHOP, "may i change my dose?"))

    assert (response.reason, response.priority) == ("safety", "high")
    assert response.guidance == shop.guidance


def test_an_output_finding_for_a_non_health_business_is_a_normal_handoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built = guard(check=FixedCheck((Finding.OUTPUT_UNGROUNDED_DIGITS,)))

    response = handoff(ask(built, SHOP, "Open?"))

    assert (response.reason, response.priority, response.guidance) == ("guard_output", "normal", ())
    [record] = records(caplog)
    assert record["decided_by"] == "output:ungrounded_digits"


def test_a_failing_guard_after_the_screen_cleared_is_normal_for_a_non_health_business(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = handoff(ask(guard(mask=RaisingMask()), SHOP, "Open?"))

    assert (response.reason, response.priority, response.guidance) == ("guard_error", "normal", ())
    assert records(caplog)[0]["error_type"] == "RuntimeError"


# ── Fail closed inside the service (G3) ──────────────────────────────────────────────────────


def test_a_missing_guarded_service_fails_closed_never_m3s(caplog: pytest.LogCaptureFixture) -> None:
    response = handoff(ask(guard(), CLINIC, "Open?", svc=None))

    assert (response.reason, response.priority) == ("guard_error", "high")
    assert records(caplog)[0]["error_type"] == "RuntimeError"


def test_a_chunk_screen_that_raises_fails_closed(caplog: pytest.LogCaptureFixture) -> None:
    provider = FakeProvider()
    svc = service(CLINIC, chunk_matcher=RaisingMatcher(RuntimeError("broken")), provider=provider)

    response = handoff(ask(guard(), CLINIC, "Open?", svc))

    assert (response.reason, response.priority) == ("guard_error", "high")
    assert provider.calls == []  # the chunk never reached a prompt
    assert records(caplog)[0]["error_type"] == "RuntimeError"


def test_a_guard_failure_a_seam_did_not_record_still_fails_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Raising:
        async def answer(self, business_id: str, question: str, *, trace_id: str) -> Any:
            raise GuardFailed("NoGuardScope")

    response = handoff(ask(guard(), CLINIC, "Open?", Raising()))

    assert response.reason == "guard_error"
    assert records(caplog)[0]["error_type"] == "NoGuardScope"


@pytest.mark.parametrize(
    ("message", "priority"), [(TELUGU, "high"), ("When do you open?", "normal")]
)
def test_redis_failing_keeps_the_before_the_screen_priority_for_an_uncovered_script(
    caplog: pytest.LogCaptureFixture, message: str, priority: str
) -> None:
    # G3: a non-health business's Telugu question was never cleared by the screen (it cannot read
    # it), so Redis failing is high, as before the screen; a cleared question drops to normal.
    config = with_not_covered(load_fixtures(), SHOP, frozenset({"te"}))
    built = guard(config=config, limiter=FakeLimiter(limit="raise"))

    response = handoff(ask(built, SHOP, message))

    assert (response.reason, response.priority) == ("guard_error", priority)
    [record] = records(caplog)
    assert (record["error_type"], record["decided_by"]) == ("ConnectionError", "guard_error")


def test_an_uncovered_script_on_search_is_recorded_only(caplog: pytest.LogCaptureFixture) -> None:
    config = with_not_covered(load_fixtures(), SHOP, frozenset({"te"}))

    searched = asyncio.run(guard(config=config).search(SHOP, TELUGU, trace_id=TRACE))

    assert searched == TELUGU
    [record] = records(caplog)
    assert (record["decided_by"], record["findings"]) == ("none", {"not_covered_language": 1})


def test_a_failing_search_guard_is_a_typed_503(caplog: pytest.LogCaptureFixture) -> None:
    searched = asyncio.run(guard(mask=RaisingMask()).search(CLINIC, "Open?", trace_id=TRACE))

    assert isinstance(searched, JSONResponse) and searched.status_code == 503
    [record] = records(caplog)
    assert (record["action"], record["error_type"]) == ("unavailable", "RuntimeError")


# ── The record (G10) ─────────────────────────────────────────────────────────────────────────


def test_a_cancel_propagates_and_writes_no_record(caplog: pytest.LogCaptureFixture) -> None:
    class Cancelled:
        async def answer(self, business_id: str, question: str, *, trace_id: str) -> Any:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        ask(guard(), CLINIC, "Open?", Cancelled())

    class Hanging(FakeLimiter):
        async def admit(self, *args: Any, **kwargs: Any) -> None:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard(limiter=Hanging()).search(CLINIC, "Open?", trace_id=TRACE))
    assert records(caplog) == []


def test_a_record_that_cannot_be_written_never_replaces_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(record: object) -> None:
        raise RuntimeError("log store down")

    monkeypatch.setattr(answer_module, "emit_guard_record", broken)

    assert handoff(ask(guard(), CLINIC, CHEST)).reason == "safety"
    assert asyncio.run(guard().search(CLINIC, "Open?", trace_id=TRACE)) == "Open?"
