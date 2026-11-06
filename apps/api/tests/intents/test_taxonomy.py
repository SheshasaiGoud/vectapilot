"""M4 PR 1: the intent set, its definitions and the decision order (ADR 0015 decision 1)."""

import pytest

from vectapilot_api.intents.taxonomy import DECISION_ORDER, DEFINITIONS, Intent


def test_the_intents_are_the_decided_eight_in_the_decided_order() -> None:
    # The first that fits wins; the order is ADR 0015's and the plan's. Changing it is a decision.
    assert [i.value for i in DECISION_ORDER] == [
        "emergency",
        "professional_judgement",
        "complaint",
        "refund_or_payment",
        "human_request",
        "action_request",
        "question",
        "out_of_scope",
    ]
    assert tuple(Intent) == DECISION_ORDER


@pytest.mark.parametrize("intent", list(Intent))
def test_every_intent_has_one_line_that_fits_a_table_and_a_prompt(intent: Intent) -> None:
    definition = DEFINITIONS[intent]
    assert definition == definition.strip()
    assert definition
    assert "\n" not in definition
    assert "|" not in definition  # the README's and the brief's tables
    assert len(definition) <= 200


def test_every_definition_belongs_to_an_intent() -> None:
    assert set(DEFINITIONS) == set(Intent)


def test_emergency_names_safety_risk_distress_and_self_harm() -> None:
    # §3.5 and §4.1, in every D5 option, although M4's data has no self-harm rows (D5 (a)).
    assert (
        "safety risk or distress, including thoughts of self-harm" in DEFINITIONS[Intent.EMERGENCY]
    )


def test_professional_judgement_covers_every_section_4_item_that_is_not_an_emergency() -> None:
    # zara: §4.1's "prognosis or second opinion" and "recommend, change or dose any medication or
    # treatment" (stopping or halving a prescribed tablet included), §4.2's diets. A narrower
    # line lets a writer, the training draft or the fallback prompt file these as `question`.
    definition = DEFINITIONS[Intent.PROFESSIONAL_JUDGEMENT]
    assert "prognosis or second opinion" in definition
    assert "whether to start, stop or change a medicine, diet or dose" in definition
    assert "what a symptom or test result means, how serious it is" in definition


def test_asking_for_a_refund_and_asking_about_the_policy_are_told_apart() -> None:
    assert "asking about the refund policy is `question`" in DEFINITIONS[Intent.REFUND_OR_PAYMENT]


def test_a_service_the_business_may_not_offer_is_a_question_not_out_of_scope() -> None:
    assert "even about a service this business may not offer" in DEFINITIONS[Intent.QUESTION]
    assert "not a support request" in DEFINITIONS[Intent.OUT_OF_SCOPE]


def test_the_definitions_are_read_only() -> None:
    with pytest.raises(TypeError):
        DEFINITIONS[Intent.QUESTION] = "anything"  # type: ignore[index]
