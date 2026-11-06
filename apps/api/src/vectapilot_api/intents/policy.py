"""The route policy (ADR 0015 decision 2, M4 PR 1): a business type -> its route template, and the
mandatory hand-off sets every template is checked against.

A platform file, like `ingestion/language_map.py`: it names no business, channel or document set
(rule 9). A business's type comes from its configuration (today `business_type` in
`data/sample/<business_id>/manifest.json`); only the *types* are a closed set here.

**A closed set of business types.** `human_healthcare` and `veterinary` have their own templates
(business-context §4.1, §4.2); `default` covers only the listed non-health types (`ecommerce`,
`retail`, `service`). An unknown or missing type raises `UnknownBusinessType`, never falls back
to `default`: a typo in a manifest must not silently drop a safety intent. `intenteval` refuses
a dataset with such a business; wiring (D6, M5) will hand off on the error.

**Mandatory sets are platform constants, not template fields.** A template lists its routes and
its safety intents; `template_problems` checks both against the constants when the template is
built (so a module with a bad template fails at import) and the tests check the templates again,
so a short safety list or a loosened route cannot pass:
- `MANDATORY_HANDOFF` (§3.5's platform defaults): every template hands these off.
- `PLATFORM_SAFETY` (§3.5: emergency, safety risk or distress): every template's safety intents
  include it, routed to an urgent hand-off.
- `HEALTH_SAFETY` (§4): both health types' safety intents include it. The safety intents are the
  ones the router's safety floor sums (H1) and the ones safety-escalation recall scores
  (`docs/plans/m4-pr-checks.md`, "Scoring rules").

**Routes.** `emergency` is an urgent hand-off; `professional_judgement`, `complaint` and
`refund_or_payment` high; `human_request` and `action_request` normal (until M5's tools);
`question` and `out_of_scope` go to `rag`, which abstains by itself when nothing answers. A
template may tighten a route (§2: tighten, never loosen), and no intent may rank above one with a
more urgent route in the decision order (`taxonomy.DECISION_ORDER`), so "the first that fits
wins" never loosens a message. The business layer (a business's own extra rules) comes with the
config loader (ADR 0011 decision 6).

Why a Python module and not a config file, as for `language_map.py`: a handful of typed entries
that mypy reads, nothing to parse at startup, and a change to a safety route is a reviewed code
change with its tests, never a file edit that skips them.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import Final

from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent


class BusinessType(StrEnum):
    """Every business type the platform knows (business-context §1, §7). Closed: a type not here
    is refused (`policy_for`).

    A pet store is `veterinary` (business-context §1 and §4.2 group "veterinary clinics and pet
    stores"): configured as `retail` it would get the default template, whose safety intents are
    `emergency` only, so `professional_judgement` would leave the safety floor and safety recall.
    Whether any other business that gives health or medicine advice (a pharmacy, say) must be a
    health type is an open question for shesha (business-context §9), not decided here."""

    HUMAN_HEALTHCARE = "human_healthcare"
    VETERINARY = "veterinary"
    ECOMMERCE = "ecommerce"
    RETAIL = "retail"
    SERVICE = "service"


HEALTH_TYPES: Final[frozenset[BusinessType]] = frozenset(
    {BusinessType.HUMAN_HEALTHCARE, BusinessType.VETERINARY}
)
"""The types business-context §4 sets safety boundaries for."""


class RouteKind(StrEnum):
    RAG = "rag"
    """Answer from the business's approved documents (ADR 0011); RAG abstains by itself."""
    HANDOFF = "handoff"
    """To staff, at a priority (§3.5)."""


class Priority(StrEnum):
    """A hand-off's priority in the staff review queue (§3.5), declared most urgent first."""

    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"


PRIORITY_ORDER: Final[tuple[Priority, ...]] = tuple(Priority)
"""Most urgent first."""


@dataclass(frozen=True)
class Route:
    """Where a message goes: `rag`, or a hand-off at a priority."""

    kind: RouteKind
    priority: Priority | None = None

    def __post_init__(self) -> None:
        if (self.kind is RouteKind.HANDOFF) != (self.priority is not None):
            raise ValueError("a hand-off has a priority, and rag has none")

    @property
    def urgency(self) -> int:
        """`rag` 0, then `normal` 1, `high` 2, `urgent` 3: a larger number is more urgent."""
        if self.priority is None:
            return 0
        return len(PRIORITY_ORDER) - PRIORITY_ORDER.index(self.priority)


RAG: Final = Route(RouteKind.RAG)


def handoff(priority: Priority) -> Route:
    return Route(RouteKind.HANDOFF, priority)


MANDATORY_HANDOFF: Final[frozenset[Intent]] = frozenset(
    {
        Intent.EMERGENCY,  # "Emergency, safety risk or distress"
        Intent.COMPLAINT,  # "Legal threats, complaints about staff"
        Intent.REFUND_OR_PAYMENT,  # "Money: refunds above a limit, price disputes" (no amount yet)
        Intent.HUMAN_REQUEST,  # "Customer asks for a human"
    }
)
"""§3.5's platform defaults: every template hands these off, at any priority. The router sees no
amount, so every refund request is handed off (tighter than "above a limit")."""

PLATFORM_SAFETY: Final[frozenset[Intent]] = frozenset({Intent.EMERGENCY})
"""§3.5: an emergency, safety risk or distress, for every business: an urgent hand-off."""

HEALTH_SAFETY: Final[frozenset[Intent]] = frozenset(
    {Intent.EMERGENCY, Intent.PROFESSIONAL_JUDGEMENT}
)
"""§4: what a healthcare or veterinary business must always escalate."""


@dataclass(frozen=True)
class RoutePolicy:
    """A business-type template: a route for every intent and the type's safety intents. Built
    only if `template_problems` finds nothing."""

    name: str
    business_types: frozenset[BusinessType]
    routes: Mapping[Intent, Route] = field(repr=False)
    safety_intents: frozenset[Intent]

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", MappingProxyType(dict(self.routes)))
        problems = template_problems(self)
        if problems:
            raise ValueError(f"route template {self.name}: {'; '.join(problems)}")

    def route(self, intent: Intent) -> Route:
        return self.routes[intent]

    @property
    def mandatory(self) -> frozenset[Intent]:
        """Everything this template must hand off: `MANDATORY_HANDOFF` and its safety intents."""
        return MANDATORY_HANDOFF | self.safety_intents


def template_problems(policy: RoutePolicy) -> list[str]:
    """Why `policy` breaks a platform rule (the module docstring), in a fixed order."""
    problems: list[str] = []
    if not policy.business_types:
        problems.append("serves no business type")
    missing = [i.value for i in DECISION_ORDER if i not in policy.routes]
    if missing:
        problems.append(f"maps no route for {missing}")
    required = set(PLATFORM_SAFETY)
    if policy.business_types & HEALTH_TYPES:
        required |= HEALTH_SAFETY
    short = [i.value for i in DECISION_ORDER if i in required and i not in policy.safety_intents]
    if short:
        problems.append(f"its safety intents lack {short}")
    loosened = [
        i.value
        for i in DECISION_ORDER
        if i in policy.mandatory and i in policy.routes and policy.routes[i].kind is RouteKind.RAG
    ]
    if loosened:
        problems.append(f"routes mandatory hand-offs to rag: {loosened}")
    urgent = handoff(Priority.URGENT)
    not_urgent = [
        i.value for i in DECISION_ORDER if i in PLATFORM_SAFETY and policy.routes.get(i) != urgent
    ]
    if not_urgent:
        problems.append(f"does not hand off {not_urgent} as urgent")
    ranked = [(i, policy.routes[i]) for i in DECISION_ORDER if i in policy.routes]
    for (earlier, first), (later, second) in pairwise(ranked):
        if first.urgency < second.urgency:
            problems.append(
                f"{earlier.value} comes before {later.value} in the decision order but has a "
                "less urgent route"
            )
    return problems


_ROUTES: Final[Mapping[Intent, Route]] = MappingProxyType(
    {
        Intent.EMERGENCY: handoff(Priority.URGENT),
        Intent.PROFESSIONAL_JUDGEMENT: handoff(Priority.HIGH),
        Intent.COMPLAINT: handoff(Priority.HIGH),
        Intent.REFUND_OR_PAYMENT: handoff(Priority.HIGH),
        Intent.HUMAN_REQUEST: handoff(Priority.NORMAL),
        Intent.ACTION_REQUEST: handoff(Priority.NORMAL),  # until M5's tools
        Intent.QUESTION: RAG,
        Intent.OUT_OF_SCOPE: RAG,
    }
)
"""M4's routes. Every template starts from the same table today; §4.1 and §4.2 diverge by a
reviewed change to one template, never by editing this shared table for one type."""

TEMPLATES: Final[tuple[RoutePolicy, ...]] = (
    RoutePolicy(
        name="human_healthcare",
        business_types=frozenset({BusinessType.HUMAN_HEALTHCARE}),
        routes=_ROUTES,
        safety_intents=HEALTH_SAFETY,
    ),
    RoutePolicy(
        name="veterinary",
        business_types=frozenset({BusinessType.VETERINARY}),
        routes=_ROUTES,
        safety_intents=HEALTH_SAFETY,
    ),
    RoutePolicy(
        name="default",
        business_types=frozenset(
            {BusinessType.ECOMMERCE, BusinessType.RETAIL, BusinessType.SERVICE}
        ),
        routes=_ROUTES,
        safety_intents=PLATFORM_SAFETY,
    ),
)

DEFAULT_TEMPLATE: Final = "default"
"""The template of the listed non-health types; never a fallback for an unknown type."""


def _by_type(templates: Iterable[RoutePolicy]) -> Mapping[BusinessType, RoutePolicy]:
    """Each business type's template. Raises unless every type has exactly one."""
    by_type: dict[BusinessType, RoutePolicy] = {}
    for template in templates:
        for business_type in sorted(template.business_types):
            if business_type in by_type:
                raise ValueError(f"business type {business_type.value} has two templates")
            by_type[business_type] = template
    uncovered = [t.value for t in BusinessType if t not in by_type]
    if uncovered:
        raise ValueError(f"business types without a template: {uncovered}")
    return MappingProxyType(by_type)


TEMPLATE_BY_TYPE: Final[Mapping[BusinessType, RoutePolicy]] = _by_type(TEMPLATES)


class UnknownBusinessType(ValueError):
    """A business type that is missing or not one of `BusinessType`. Raised, never defaulted."""


def policy_for(business_type: str | None) -> RoutePolicy:
    """The route template of `business_type` (a business configuration's `business_type`).
    Raises `UnknownBusinessType` for a missing or unknown type (case-sensitive)."""
    known = [t.value for t in BusinessType]
    if business_type not in known:
        raise UnknownBusinessType(
            f"unknown business type {business_type!r}; the platform knows {known}"
        )
    return TEMPLATE_BY_TYPE[BusinessType(business_type)]
