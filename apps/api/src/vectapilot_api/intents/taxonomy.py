"""The intent set (ADR 0015 decision 1): one platform set of eight intents, a one-line definition
for each, and a decision order.

**One platform set, not one per business type or per business.** A set per type or per business
would need several times the data, or the config loader that does not exist yet; a business type
that needs an intent this set cannot express is a revisit trigger (business-context §8).

**The decision order: when two intents fit, the first one wins.** The order is monotone in route
urgency (`policy.py` checks every template against it), so a message that fits two intents is
never routed less urgently than its most urgent reading. Why `complaint` before `human_request`:
an angry message that also asks for a manager is a complaint (high), not only a request for a
person (normal). Why `out_of_scope` last and decided from the text alone: a platform classifier
cannot see a business's documents, so a question about a service the business may not offer is
`question`, and RAG abstains on it by itself (unlike `support_qa`'s document-based
`out_of_scope`).

**One source of truth.** The dataset README, the writers' brief and (PR 3) the LLM fallback's
prompt quote `DEFINITIONS` word for word; tests keep them equal. A definition changes only by a
reviewed change here, never in a copy.

`emergency` names thoughts of self-harm (§3.5, §4.1) in every D5 option. Under D5 (a) M4's data
has no self-harm messages, so the router's safety recall excludes crisis messages (M7).
`professional_judgement` covers every §4 "must not, always escalate" item that is not an
emergency, a prognosis, a second opinion and stopping or changing a prescribed treatment
included: a narrower line would let a writer, the training draft or the fallback prompt file
those as `question`, which goes to RAG, and safety recall would never count the miss.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class Intent(StrEnum):
    """The eight intents, declared in decision order (`DECISION_ORDER`)."""

    EMERGENCY = "emergency"
    PROFESSIONAL_JUDGEMENT = "professional_judgement"
    COMPLAINT = "complaint"
    REFUND_OR_PAYMENT = "refund_or_payment"
    HUMAN_REQUEST = "human_request"
    ACTION_REQUEST = "action_request"
    QUESTION = "question"
    OUT_OF_SCOPE = "out_of_scope"


DECISION_ORDER: Final[tuple[Intent, ...]] = tuple(Intent)
"""When two intents fit a message, the earlier one here wins."""

DEFINITIONS: Final[Mapping[Intent, str]] = MappingProxyType(
    {
        Intent.EMERGENCY: (
            "safety risk or distress, including thoughts of self-harm: a person or an animal "
            "may need urgent help now"
        ),
        Intent.PROFESSIONAL_JUDGEMENT: (
            "asks for a clinician's or vet's judgement: diagnosis, prognosis or second opinion, "
            "what a symptom or test result means, how serious it is, or whether to start, stop "
            "or change a medicine, diet or dose"
        ),
        Intent.COMPLAINT: (
            "unhappy with the service, the staff, a product or an earlier reply, or threatens "
            "legal action"
        ),
        Intent.REFUND_OR_PAYMENT: (
            "asks for money back or reports a payment problem: a refund, a wrong or double "
            "charge, a failed payment, a disputed price (asking about the refund policy is "
            "`question`)"
        ),
        Intent.HUMAN_REQUEST: "asks to talk to a person instead of the assistant",
        Intent.ACTION_REQUEST: (
            "asks the business to do something: book, change or cancel an appointment, place "
            "or change an order, arrange a delivery"
        ),
        Intent.QUESTION: (
            "asks for information a business of this kind could give (hours, prices, services, "
            "products, policies, preparation), even about a service this business may not offer"
        ),
        Intent.OUT_OF_SCOPE: (
            "not a support request to a business of this kind: general knowledge, chit-chat, "
            "or anything unrelated to its services"
        ),
    }
)
"""One line per intent: what the README, the writers' brief and the fallback prompt quote.
Read-only through its type and at run time (a `MappingProxyType`)."""
