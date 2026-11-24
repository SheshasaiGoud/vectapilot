"""Stand-ins and builders for the guarded `/v1/agent` tests (M7 PR 6).

**None of these is H1, H2 or H3.** `LookupMatcher` is a fixed lookup from a whole folded message
to the term ids it "hits": it never looks inside a text, so it cannot be copied into a matcher.
It exists so the route's order, fold and fail-closed paths are tested before shesha's H1
(`guards/match.py`). `IdentityMask` returns the text unmasked (not H2), `DigitRunMask` masks one
fixed shape (ten digits in a row, nothing else: not H2) and `FixedCheck` returns fixed findings
(not H3): they let a unit test reach the steps after masking and output checks. H2's `mask_pii` is
written (2026-11-20), so a builder's default masker is the real one; `RaisingMask` stands in for a
masker that breaks, to pin G3's fail-closed path after the screen cleared. H3's `check_reply` is
written too (2026-11-23), so a builder's default output check is the real one; `RaisingCheck`
stands in for a check that breaks, for the same reason.
`WithholdingRecorder` is the pending-action store's stand-in with `create_withheld` (G7).
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final
from uuid import UUID

from tests.agent.fakes import NO_DATABASE, PAWS, TOOLS
from tests.agent.test_service import RecordingStore
from tests.guards.helpers import load_fixtures
from vectapilot_api.actions.tokens import new_confirm_token
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.agent.profiles import AgentProfile, build_profile, build_profiles
from vectapilot_api.agent.service import AgentService, AnswerSource
from vectapilot_api.agent.types import LoopLimits, TurnLoop
from vectapilot_api.gateway.catalog import load_catalog
from vectapilot_api.gateway.providers.fake import FakeProvider
from vectapilot_api.gateway.types import BusinessId, RetryPolicy
from vectapilot_api.guards.agent import AgentGuard, read_only_tools
from vectapilot_api.guards.budget import BudgetedGateway
from vectapilot_api.guards.config import GuardConfig
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.limits import GuardRefused, Limiter
from vectapilot_api.guards.locales import Locale
from vectapilot_api.guards.output import OutputCheck, OutputContext, check_reply
from vectapilot_api.guards.pii import Masked, mask_pii
from vectapilot_api.guards.screens import GUARD_PROPAGATE, Masker
from vectapilot_api.guards.selftest import screen_text
from vectapilot_api.guards.store import ScreeningProposalStore, WithholdingStore
from vectapilot_api.guards.types import Clean, Finding, Folded, TermHit, TermSet
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.tools.files import BusinessTools
from vectapilot_api.tools.types import ToolContext, WriteProposal

CLINIC: Final = "fx-clinic"
VET: Final = "fx-vet"
SHOP: Final = "fx-shop"
"""The fixture guard folder's businesses (tests/guards/fixtures): a clinic, a vet, a shop."""
CUSTOMER: Final = "cust-001"


class LookupMatcher:
    """NOT H1 (module docstring): `table` maps a raw message to the term ids it hits; the key is
    folded as a guarded route folds the message, and the text is compared whole."""

    def __init__(self, table: Mapping[str, Iterable[str]] | None = None) -> None:
        self.table = {screen_text(text): frozenset(ids) for text, ids in (table or {}).items()}
        self.calls: list[tuple[Folded, frozenset[str]]] = []

    def __call__(self, text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        self.calls.append((text, frozenset(term.id for term in terms.terms)))
        wanted = self.table.get(text, frozenset())
        return tuple(TermHit(t.id, t.category, 0) for t in terms.terms if t.id in wanted)


@dataclass
class RaisingMatcher:
    """A matcher that raises, as H1's stub does (`NotImplementedError`) or a broken one would."""

    error: Exception = field(default_factory=lambda: NotImplementedError("H1 stub"))
    calls: int = 0

    def __call__(self, text: Folded, terms: TermSet) -> tuple[TermHit, ...]:
        self.calls += 1
        raise self.error


@dataclass
class RaisingMask:
    """NOT H2: a masker that raises, as a broken one would (H2 is written, so its own stub can no
    longer show the fail-closed path)."""

    error: Exception = field(default_factory=lambda: RuntimeError("masker broke"))
    calls: int = 0

    def __call__(self, text: Clean, locales: Sequence[Locale]) -> Masked:
        self.calls += 1
        raise self.error


@dataclass
class IdentityMask:
    """NOT H2: returns the text as it came, and records it."""

    seen: list[str] = field(default_factory=list)

    def __call__(self, text: Clean, locales: Sequence[Locale]) -> Masked:
        self.seen.append(text)
        return Masked(text)


@dataclass
class DigitRunMask:
    """NOT H2: replaces each run of exactly ten ASCII digits with `[PHONE]`, nothing else (a fixed
    shape, so a test can see what was masked and what was left alone)."""

    seen: list[str] = field(default_factory=list)

    def __call__(self, text: Clean, locales: Sequence[Locale]) -> Masked:
        self.seen.append(text)
        return Masked(Clean(re.sub(r"(?<![0-9])[0-9]{10}(?![0-9])", "[PHONE]", text)))


@dataclass
class RaisingCheck:
    """NOT H3: an output check that raises, as a broken one would (H3 is written, so its own stub
    can no longer show the fail-closed path)."""

    error: Exception = field(default_factory=lambda: RuntimeError("check broke"))
    calls: int = 0

    def __call__(self, reply: str, context: OutputContext) -> tuple[Finding, ...]:
        self.calls += 1
        raise self.error


WITHHELD_ACTION: Final = UUID("22222222-3333-4444-8555-666666666666")


@dataclass
class WithholdingRecorder(RecordingStore):
    """`RecordingStore` with the pending-action store's `create_withheld` (G7): records the
    proposal and returns an action with a fresh token, kept here so a test can prove it never
    reaches a response."""

    withheld: list[tuple[WriteProposal, ToolContext]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    withheld_error: Exception | None = None

    async def create_withheld(self, proposal: WriteProposal, context: ToolContext) -> PendingAction:
        self.withheld.append((proposal, context))
        if self.withheld_error is not None:
            raise self.withheld_error
        token = new_confirm_token()
        self.tokens.append(token.get_secret_value())
        return PendingAction(
            id=WITHHELD_ACTION,
            tool=proposal.tool,
            summary=proposal.summary,
            expires_at=datetime(2026, 11, 16, 10, 0, tzinfo=UTC),
            confirm_token=token,
        )


@dataclass
class FixedCheck:
    """NOT H3: returns `findings` for every text, and records what it was asked."""

    findings: tuple[Finding, ...] = ()
    asked: list[tuple[str, OutputContext]] = field(default_factory=list)

    def __call__(self, reply: str, context: OutputContext) -> tuple[Finding, ...]:
        self.asked.append((reply, context))
        return self.findings


def fixture_tools() -> Mapping[BusinessId, BusinessTools]:
    """Tool files for the fixture businesses: Paws & Co's, under each fixture id and type."""
    kinds = {
        CLINIC: BusinessType.HUMAN_HEALTHCARE,
        VET: BusinessType.VETERINARY,
        SHOP: BusinessType.ECOMMERCE,
    }
    seed = TOOLS["paws-and-co"].seed
    return MappingProxyType(
        {
            BusinessId(business): BusinessTools(
                config=PAWS.model_copy(update={"business_id": business, "business_type": kind}),
                seed=seed,
            )
            for business, kind in kinds.items()
        }
    )


FX_TOOLS: Final = fixture_tools()
FX_TYPES: Final = MappingProxyType({b: t.config.business_type for b, t in FX_TOOLS.items()})


def profiles(read_only: bool = False) -> Mapping[BusinessId, AgentProfile]:
    tools = read_only_tools(FX_TOOLS) if read_only else FX_TOOLS
    return build_profiles(NO_DATABASE, tools, FX_TYPES)


def profile(business: str) -> AgentProfile:
    config = FX_TOOLS[BusinessId(business)].config
    return build_profile(NO_DATABASE, config, config.business_type)


def budgeted(provider: FakeProvider, limiter: Limiter) -> BudgetedGateway:
    return BudgetedGateway(
        provider,
        load_catalog(),
        attempt_timeout_s=5.0,
        deadline_s=10.0,
        retry_policy=RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0),
        max_repairs=1,
        limiter=limiter,
    )


ProfileWrap = Callable[[Mapping[BusinessId, AgentProfile]], Mapping[BusinessId, AgentProfile]]


@dataclass
class Guarded:
    """One guard over a full and a read-only service, and every stand-in they run on."""

    guard: AgentGuard
    service: AgentService
    read_only: AgentService
    limiter: FakeLimiter
    provider: FakeProvider
    matcher: LookupMatcher | RaisingMatcher


def guarded(
    *,
    loop: TurnLoop,
    read_only_loop: TurnLoop | None = None,
    matcher: LookupMatcher | RaisingMatcher | None = None,
    limiter: FakeLimiter | None = None,
    config: GuardConfig | bool | None = True,
    store: WithholdingStore,
    answers: AnswerSource,
    check: OutputCheck | None = None,
    mask: Masker | None = None,
    routing: str = "off",
    router: object = None,
    provider: FakeProvider | None = None,
    tools: ProfileWrap | None = None,
) -> Guarded:
    """`AgentGuard` as `create_app` wires it, on stand-ins (`config=True`: the fixture folder;
    `False` or None: no guard folder). `tools` replaces both services' profiles' executors (as
    `guards.executor.screening_profiles` does in `create_app`)."""
    limiter = limiter or FakeLimiter()
    matcher = matcher or LookupMatcher()
    provider = provider or FakeProvider()
    gateway = budgeted(provider, limiter)
    screened = ScreeningProposalStore(store, matcher=matcher, check=check or check_reply)
    propagate: tuple[type[Exception], ...] = (GuardRefused, *GUARD_PROPAGATE)
    loaded = load_fixtures() if config is True else (config or None)

    def service(served: Mapping[BusinessId, AgentProfile], turn: TurnLoop) -> AgentService:
        return AgentService(
            served,
            gateway=gateway,
            store=screened,
            answers=answers,
            limits=LoopLimits(max_steps=4),
            turn_timeout_s=35.0,
            routing="on" if routing == "on" else "off",
            router=router,  # type: ignore[arg-type]
            router_timeout_s=15.0,
            loop=turn,
            propagate=propagate,
        )

    wrap = tools or (lambda served: served)
    full = service(wrap(profiles()), loop)
    read_only = service(wrap(profiles(read_only=True)), read_only_loop or loop)
    guard = AgentGuard(
        config=loaded if isinstance(loaded, GuardConfig) else None,
        limiter=limiter,
        matcher=matcher,
        profiles=profiles(),
        gateway=gateway,
        max_steps=4,
        check=check or check_reply,
        mask=mask or mask_pii,
    )
    return Guarded(guard, full, read_only, limiter, provider, matcher)


def with_not_covered(config: GuardConfig, business: str, languages: frozenset[str]) -> GuardConfig:
    """`config` with `business`'s `not_covered` replaced (a test arrangement, never a file)."""
    businesses = dict(config.businesses)
    businesses[BusinessId(business)] = replace(
        businesses[BusinessId(business)], not_covered=languages
    )
    return replace(config, businesses=MappingProxyType(businesses))
