"""M4 PR 1: business types, route templates and the mandatory sets (ADR 0015 decision 2).

The templates are checked twice: when they are built (`template_problems`, so a bad template
fails at import) and here, against the platform constants, so a short safety list or a loosened
route cannot pass either way.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from vectapilot_api.intents.policy import (
    DEFAULT_TEMPLATE,
    HEALTH_SAFETY,
    HEALTH_TYPES,
    MANDATORY_HANDOFF,
    PLATFORM_SAFETY,
    PRIORITY_ORDER,
    RAG,
    TEMPLATE_BY_TYPE,
    TEMPLATES,
    BusinessType,
    Priority,
    Route,
    RouteKind,
    RoutePolicy,
    UnknownBusinessType,
    _by_type,
    handoff,
    policy_for,
)
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

SAMPLE_DIR: Final = Path(__file__).parents[4] / "data" / "sample"
URGENT: Final = handoff(Priority.URGENT)
HIGH: Final = handoff(Priority.HIGH)
NORMAL: Final = handoff(Priority.NORMAL)

DECIDED_ROUTES: Final[Mapping[Intent, Route]] = {
    Intent.EMERGENCY: URGENT,
    Intent.PROFESSIONAL_JUDGEMENT: HIGH,
    Intent.COMPLAINT: HIGH,
    Intent.REFUND_OR_PAYMENT: HIGH,
    Intent.HUMAN_REQUEST: NORMAL,
    Intent.ACTION_REQUEST: NORMAL,
    Intent.QUESTION: RAG,
    Intent.OUT_OF_SCOPE: RAG,
}


def by_name(name: str) -> RoutePolicy:
    return next(t for t in TEMPLATES if t.name == name)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_template_maps_every_intent(template: RoutePolicy) -> None:
    assert set(template.routes) == set(Intent)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_no_mandatory_intent_maps_to_rag(template: RoutePolicy) -> None:
    # Tighten, never loosen (§2): the constants, not the template's own list, decide.
    required = MANDATORY_HANDOFF | PLATFORM_SAFETY
    if template.business_types & HEALTH_TYPES:
        required |= HEALTH_SAFETY
    for intent in required:
        assert template.route(intent).kind is RouteKind.HANDOFF, intent


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_every_safety_list_holds_the_platform_constants(template: RoutePolicy) -> None:
    assert template.safety_intents >= PLATFORM_SAFETY
    if template.business_types & HEALTH_TYPES:
        assert template.safety_intents >= HEALTH_SAFETY


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_an_emergency_is_an_urgent_hand_off_everywhere(template: RoutePolicy) -> None:
    assert template.route(Intent.EMERGENCY) == URGENT


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_no_intent_ranks_above_one_with_a_more_urgent_route(template: RoutePolicy) -> None:
    for i, earlier in enumerate(DECISION_ORDER):
        for later in DECISION_ORDER[i + 1 :]:
            assert template.route(earlier).urgency >= template.route(later).urgency, (
                earlier,
                later,
            )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.name)
def test_the_routes_are_the_decided_ones(template: RoutePolicy) -> None:
    # emergency urgent; professional_judgement, complaint, refund_or_payment high; human_request
    # and action_request normal (until M5's tools); question and out_of_scope to rag.
    assert dict(template.routes) == DECIDED_ROUTES


def test_the_business_types_are_closed_and_default_covers_only_non_health_types() -> None:
    assert {t.value for t in BusinessType} == {
        "human_healthcare",
        "veterinary",
        "ecommerce",
        "retail",
        "service",
    }
    assert {t.value for t in HEALTH_TYPES} == {"human_healthcare", "veterinary"}
    assert set(TEMPLATE_BY_TYPE) == set(BusinessType)
    assert by_name("human_healthcare").business_types == {BusinessType.HUMAN_HEALTHCARE}
    assert by_name("veterinary").business_types == {BusinessType.VETERINARY}
    assert by_name(DEFAULT_TEMPLATE).business_types == {
        BusinessType.ECOMMERCE,
        BusinessType.RETAIL,
        BusinessType.SERVICE,
    }
    assert not by_name(DEFAULT_TEMPLATE).business_types & HEALTH_TYPES
    assert by_name("human_healthcare").safety_intents == HEALTH_SAFETY
    assert by_name("veterinary").safety_intents == HEALTH_SAFETY
    assert by_name(DEFAULT_TEMPLATE).safety_intents == PLATFORM_SAFETY


def test_the_mandatory_sets_are_the_decided_ones() -> None:
    # §3.5's platform defaults and §4's health safety intents.
    assert {i.value for i in MANDATORY_HANDOFF} == {
        "emergency",
        "complaint",
        "refund_or_payment",
        "human_request",
    }
    assert {i.value for i in PLATFORM_SAFETY} == {"emergency"}
    assert {i.value for i in HEALTH_SAFETY} == {"emergency", "professional_judgement"}


@pytest.mark.parametrize("business_type", list(BusinessType))
def test_each_known_type_resolves_to_its_template(business_type: BusinessType) -> None:
    template = policy_for(business_type.value)
    assert business_type in template.business_types


@pytest.mark.parametrize(
    "business_type", [None, "", "default", "Veterinary", "clinic", " veterinary", "veterinary "]
)
def test_an_unknown_or_missing_type_raises_and_never_falls_back(
    business_type: str | None,
) -> None:
    with pytest.raises(UnknownBusinessType, match="unknown business type"):
        policy_for(business_type)


def test_every_sample_business_resolves_to_a_named_non_default_template() -> None:
    manifests = sorted(SAMPLE_DIR.glob("*/manifest.json"))
    assert len(manifests) >= 2
    for path in manifests:
        business_type = json.loads(path.read_text("utf-8"))["business_type"]
        template = policy_for(business_type)
        assert template.name != DEFAULT_TEMPLATE, path.parent.name
        assert template.name == business_type, path.parent.name


# ── The validator: a bad template cannot be built ─────────────────────────────────────────────


def template(**overrides: object) -> RoutePolicy:
    fields: dict[str, object] = {
        "name": "test-only",
        "business_types": frozenset({BusinessType.VETERINARY}),
        "routes": DECIDED_ROUTES,
        "safety_intents": HEALTH_SAFETY,
    }
    fields.update(overrides)
    return RoutePolicy(**fields)  # type: ignore[arg-type]


def test_a_valid_template_builds_and_its_routes_are_read_only() -> None:
    built = template()
    with pytest.raises(TypeError):
        built.routes[Intent.QUESTION] = URGENT  # type: ignore[index]
    assert built.mandatory == MANDATORY_HANDOFF | HEALTH_SAFETY


def test_a_health_template_with_a_short_safety_list_is_refused() -> None:
    with pytest.raises(ValueError, match=r"safety intents lack \['professional_judgement'\]"):
        template(safety_intents=PLATFORM_SAFETY)


def test_a_template_without_emergency_in_its_safety_list_is_refused() -> None:
    with pytest.raises(ValueError, match=r"safety intents lack \['emergency'\]"):
        template(
            business_types=frozenset({BusinessType.RETAIL}),
            safety_intents=frozenset(),
        )


@pytest.mark.parametrize("intent", sorted(MANDATORY_HANDOFF | HEALTH_SAFETY))
def test_a_mandatory_intent_routed_to_rag_is_refused(intent: Intent) -> None:
    # Moving the intent to rag also breaks the order's monotonicity for most intents; the
    # loosening is named either way.
    routes = {**DECIDED_ROUTES, intent: RAG}
    with pytest.raises(ValueError, match=f"routes mandatory hand-offs to rag: .*{intent.value}"):
        template(routes=routes)


def test_a_non_urgent_emergency_is_refused() -> None:
    routes = {**DECIDED_ROUTES, Intent.EMERGENCY: HIGH}
    with pytest.raises(ValueError, match=r"does not hand off \['emergency'\] as urgent"):
        template(routes=routes)


def test_an_intent_ranked_above_a_more_urgent_one_is_refused() -> None:
    # action_request (normal) above question: make question high, which is tighter for question
    # but would let "the first that fits" route a message less urgently.
    routes = {**DECIDED_ROUTES, Intent.QUESTION: HIGH}
    with pytest.raises(
        ValueError, match="action_request comes before question in the decision order"
    ):
        template(routes=routes)


def test_tightening_a_route_in_order_is_allowed() -> None:
    routes = {**DECIDED_ROUTES, Intent.PROFESSIONAL_JUDGEMENT: URGENT}
    assert template(routes=routes).route(Intent.PROFESSIONAL_JUDGEMENT) == URGENT


def test_a_template_that_misses_an_intent_or_a_type_is_refused() -> None:
    routes = {i: r for i, r in DECIDED_ROUTES.items() if i is not Intent.OUT_OF_SCOPE}
    with pytest.raises(ValueError, match=r"maps no route for \['out_of_scope'\]"):
        template(routes=routes)
    with pytest.raises(ValueError, match="serves no business type"):
        template(business_types=frozenset())


def test_every_type_needs_exactly_one_template() -> None:
    with pytest.raises(ValueError, match="business type veterinary has two templates"):
        _by_type((*TEMPLATES, template()))
    with pytest.raises(ValueError, match=r"business types without a template: \['veterinary'\]"):
        _by_type(t for t in TEMPLATES if t.name != "veterinary")


def test_a_route_has_a_priority_exactly_when_it_hands_off() -> None:
    with pytest.raises(ValueError, match="a hand-off has a priority"):
        Route(RouteKind.HANDOFF)
    with pytest.raises(ValueError, match="a hand-off has a priority"):
        Route(RouteKind.RAG, Priority.HIGH)


def test_urgency_puts_rag_below_every_hand_off_in_priority_order() -> None:
    assert [p.value for p in PRIORITY_ORDER] == ["urgent", "high", "normal"]
    assert [RAG.urgency, NORMAL.urgency, HIGH.urgency, URGENT.urgency] == [0, 1, 2, 3]
