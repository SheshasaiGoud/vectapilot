"""The guards' vocabulary (ADR 0018, G2, G10): text kinds, terms, hits, closed codes, a guard's
decision and the one metadata-only `guard` record per request.

**Codes, never text.** A finding, a `decided_by` and an action are closed `StrEnum`s, so the
`guard` record can only say *which rule* acted, never *what the customer wrote*. A term id is not
a code: `red_flag:chest_pain` would say what the customer wrote, so `TermHit` (which carries the
id) stays inside the request and the record counts findings by category only (G10).

**A record cannot hold text.** Every field is a closed code, a count, an amount, a duration or an
id checked against its fixed format when the record is built (`GuardRecord.__post_init__`), so a
caller cannot put a message, a term or an exception's text into it by mistake. The exception's
class name is kept (`error_type`), as `agent_turn` does.
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, NewType
from uuid import UUID

from vectapilot_api.intents.policy import Priority
from vectapilot_api.intents.taxonomy import Intent

Clean = NewType("Clean", str)
"""`normalize.clean`'s output: what the model, the embedder and masking see (G5)."""
Folded = NewType("Folded", str)
"""`normalize.fold`'s output: what the screens and H1 see. Terms go through the same fold."""


# ── Terms and hits (H1's input and output) ───────────────────────────────────────────────────


class TermKind(StrEnum):
    PHRASE = "phrase"
    """Literal words, matched as whole words on both ends with `\\s*` between them (H1)."""
    NEAR = "near"
    """Word sets that must all appear within a window of words, in any order (H1)."""


class TermCategory(StrEnum):
    """What a term screens for. The two red-flag categories are M4's intents by value, so the
    hand-off's priority comes from M4's route policy (`intents/policy.py`), never from here."""

    EMERGENCY = "emergency"
    PROFESSIONAL_JUDGEMENT = "professional_judgement"
    INJECTION = "injection"
    SUCCESS_CLAIM = "success_claim"
    MEDICAL_ADVICE = "medical_advice"
    DIAGNOSIS = "diagnosis"
    ABUSE = "abuse"


RED_FLAG_CATEGORIES: Final[frozenset[TermCategory]] = frozenset(
    {TermCategory.EMERGENCY, TermCategory.PROFESSIONAL_JUDGEMENT}
)
"""The red-flag screen's categories (G1): an urgent or a high hand-off."""
OUTPUT_CATEGORIES: Final[frozenset[TermCategory]] = frozenset(
    {
        TermCategory.SUCCESS_CLAIM,
        TermCategory.MEDICAL_ADVICE,
        TermCategory.DIAGNOSIS,
        TermCategory.ABUSE,
    }
)
"""The output-phrase categories H3 matches on model text (G8; `abuse` under D2 (i))."""


def red_flag_intent(category: TermCategory) -> Intent:
    """M4's intent for a red-flag category; a ValueError for any other category."""
    if category not in RED_FLAG_CATEGORIES:
        raise ValueError("only a red-flag category maps to an intent")
    return Intent(category.value)


def _check_words(words: tuple[str, ...]) -> None:
    if not words or any(not word or word != word.strip() or " " in word for word in words):
        raise ValueError("a term's words are non-empty single words")


@dataclass(frozen=True)
class PhraseTerm:
    """A literal phrase, already folded (`config` folds every term as the text is folded)."""

    id: str
    category: TermCategory
    words: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_words(self.words)

    @property
    def kind(self) -> TermKind:
        return TermKind.PHRASE

    @property
    def text(self) -> Folded:
        """The folded phrase: its words joined by single spaces."""
        return Folded(" ".join(self.words))


@dataclass(frozen=True)
class NearTerm:
    """At least two word sets; every set must be matched by a word within `window` consecutive
    words of the text, in any order. Already folded; no word belongs to two sets."""

    id: str
    category: TermCategory
    sets: tuple[tuple[str, ...], ...]
    window: int

    def __post_init__(self) -> None:
        if len(self.sets) < 2:
            raise ValueError("a near term has at least two word sets")
        for words in self.sets:
            _check_words(words)
        if self.window < len(self.sets):
            raise ValueError("a near term's window holds at least one word per set")

    @property
    def kind(self) -> TermKind:
        return TermKind.NEAR


Term = PhraseTerm | NearTerm


@dataclass(frozen=True)
class TermSet:
    """The terms one screen runs. Ids are not checked here: the loader refuses a duplicate id
    with the file and location (G4), and H1 refuses one again (`ValueError`)."""

    terms: tuple[Term, ...]

    def categories(self) -> frozenset[TermCategory]:
        return frozenset(term.category for term in self.terms)


@dataclass(frozen=True)
class TermHit:
    """One match: the term's id and category and where the match starts in the folded text.
    Never the text itself."""

    term_id: str
    category: TermCategory
    start: int


# ── Codes ────────────────────────────────────────────────────────────────────────────────────


class Finding(StrEnum):
    """What a check found, counted in the `guard` record (G10)."""

    RED_FLAG_EMERGENCY = "red_flag:emergency"
    RED_FLAG_PROFESSIONAL_JUDGEMENT = "red_flag:professional_judgement"
    NO_GUIDANCE = "no_guidance"
    """A red-flag hit for a business without guidance lines (its type is unresolved, G4)."""
    NOT_COVERED_LANGUAGE = "not_covered_language"
    INJECTION_PHRASE = "injection:phrase"
    INJECTION_INVISIBLE = "injection:invisible"
    OUTPUT_SUCCESS_CLAIM = "output:success_claim"
    OUTPUT_MEDICAL_ADVICE = "output:medical_advice"
    OUTPUT_DIAGNOSIS = "output:diagnosis"
    OUTPUT_ABUSE = "output:abuse"
    OUTPUT_PII = "output:pii"
    OUTPUT_BOUNDARY = "output:boundary"
    OUTPUT_UNGROUNDED_DIGITS = "output:ungrounded_digits"
    DOCUMENT_HIDDEN_TEXT = "document:hidden_text"
    OVER_CAP = "over_cap"
    """A surface's text was longer than its input cap (`caps`, G3): it fails closed."""


class DecidedBy(StrEnum):
    """The one rule that decided what the customer got (G10). The red-team suite scores every
    case on its outcome *and* on this code, so neither a stub nor a guard that hands off
    everything can pass (PR 2's scoring rules)."""

    NONE = "none"
    """No guard changed the service's response."""
    RED_FLAG_EMERGENCY = "red_flag:emergency"
    RED_FLAG_PROFESSIONAL_JUDGEMENT = "red_flag:professional_judgement"
    NOT_COVERED_LANGUAGE = "not_covered_language"
    LIMIT = "limit"
    BUDGET = "budget"
    LIMITS_UNAVAILABLE = "limits_unavailable"
    INJECTION_MESSAGE = "injection:message"
    INJECTION_CHUNK = "injection:chunk"
    INJECTION_TOOL_RESULT = "injection:tool_result"
    INJECTION_TICKET_TEXT = "injection:ticket_text"
    INJECTION_DOCUMENT = "injection:document"
    OUTPUT_SUCCESS_CLAIM = "output:success_claim"
    OUTPUT_MEDICAL_ADVICE = "output:medical_advice"
    OUTPUT_DIAGNOSIS = "output:diagnosis"
    OUTPUT_ABUSE = "output:abuse"
    OUTPUT_PII = "output:pii"
    OUTPUT_BOUNDARY = "output:boundary"
    OUTPUT_UNGROUNDED_DIGITS = "output:ungrounded_digits"
    INPUT_CAP = "input_cap"
    GUARD_ERROR = "guard_error"
    GUARDS_NOT_CONFIGURED = "guards_not_configured"


class GuardAction(StrEnum):
    """What a guard did (G2: each one only tightens)."""

    PASS = "pass"  # noqa: S105 (an action code, not a password)
    """The service's response, byte for byte."""
    DROP = "drop"
    """A retrieved chunk left out of the prompt (G7)."""
    WITHHOLD = "withhold"
    """A tool-result string field replaced by `[withheld]` (G7)."""
    READ_ONLY = "read_only"
    """The turn ran on the read-only service (G7, G13)."""
    HANDOFF = "handoff"
    """A hand-off to staff, before the service or replacing its whole response."""
    LIMIT = "limit"
    """A 429 with `Retry-After` (G9)."""
    UNAVAILABLE = "unavailable"
    """A typed 503 on a route without a hand-off (search, confirm, extractions; G3, G15)."""


class RouteSlug(StrEnum):
    """A rate-limited route, as a fixed slug (G9's key grammar): never a path. The first five are
    M7's guarded routes; `session` is M8 PR 1's per-business window on creating a customer session
    (S14), which no guard record names. Staff login has no window (M8 PR 1, step B3)."""

    AGENT = "agent"
    CONFIRM = "confirm"
    EXTRACTION = "extraction"
    ANSWER = "answer"
    SEARCH = "search"
    SESSION = "session"


class Check(StrEnum):
    """A step of G1's pipeline, as the `guard` record lists the ones that ran."""

    CLEAN = "clean"
    RED_FLAG = "red_flag"
    NOT_COVERED_LANGUAGE = "not_covered_language"
    LIMITS = "limits"
    BUDGET = "budget"
    INJECTION = "injection"
    MASK = "mask"
    OUTPUT = "output"
    DOCUMENT = "document"


# ── A guard's decision ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GuardDecision:
    """One guard's decision. A hand-off has a priority and only a hand-off does; a pass is
    decided by no rule and every other action by exactly one; guidance goes only with a
    hand-off or a 429 (G3: every refusal for a health business carries it)."""

    action: GuardAction
    decided_by: DecidedBy
    priority: Priority | None = None
    guidance: bool = False

    def __post_init__(self) -> None:
        if (self.action is GuardAction.HANDOFF) != (self.priority is not None):
            raise ValueError("a hand-off has a priority, and nothing else has one")
        if (self.action is GuardAction.PASS) != (self.decided_by is DecidedBy.NONE):
            raise ValueError("a pass is decided by no rule, and every other action by one")
        if self.guidance and self.action not in (GuardAction.HANDOFF, GuardAction.LIMIT):
            raise ValueError("guidance goes only with a hand-off or a 429")


UNCHANGED: Final = GuardDecision(GuardAction.PASS, DecidedBy.NONE)
"""The decision of a guard that let the service's response through byte for byte."""


# ── The `guard` record (G10) ─────────────────────────────────────────────────────────────────

GUARD_RECORD: Final = "guard"
"""The log message of the one record per request, and the key that holds it."""

logger = logging.getLogger("vectapilot_api.guards")
# Pinned at INFO like the gateway's: a quieter `log_level` must never drop a `guard` record.
logger.setLevel(logging.INFO)

_BUSINESS_ID: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TRACE_ID: Final = re.compile(r"^[0-9a-f]{32}$")
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_ERROR_TYPE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
"""A Python class name: one identifier, so an exception's text can never ride in it."""


def _all_of(kind: type[StrEnum], values: Iterable[object]) -> bool:
    return all(isinstance(value, kind) for value in values)


@dataclass(frozen=True)
class GuardRecord:
    """One request's metadata (G10): which checks ran, what decided, counts, amounts and
    durations. Never matched text, a term or a term id."""

    business_id: str
    trace_id: str
    route: RouteSlug
    rules_version: str | None
    """The sha256 of the guard folder (`config.GuardConfig.rules_version`); None when the guards
    are not configured."""
    decision: GuardDecision
    checks: tuple[Check, ...] = ()
    findings: Mapping[Finding, int] = field(default_factory=lambda: MappingProxyType({}))
    invisible: int = 0
    """Characters `clean` or `fold` dropped from the message (`normalize.InvisibleCounts`)."""
    error_type: str | None = None
    reserved_micro_usd: int | None = None
    settled_micro_usd: int | None = None
    budget_refused: int = 0
    withheld_action_id: UUID | None = None
    """The pending action of a proposal whose response a guard replaced (G7)."""
    durations_ms: Mapping[Check, float] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", MappingProxyType(dict(self.findings)))
        object.__setattr__(self, "durations_ms", MappingProxyType(dict(self.durations_ms)))
        problems = self._problems()
        if problems:
            raise ValueError(f"invalid guard record: {', '.join(problems)}")

    def _problems(self) -> list[str]:
        problems: list[str] = []
        if not (3 <= len(self.business_id) <= 40 and _BUSINESS_ID.fullmatch(self.business_id)):
            problems.append("business_id")
        if not _TRACE_ID.fullmatch(self.trace_id) or set(self.trace_id) == {"0"}:
            problems.append("trace_id")
        if self.rules_version is not None and not _SHA256.fullmatch(self.rules_version):
            problems.append("rules_version")
        # A `StrEnum` is a `str`, so a plain string would pass a type check that is skipped at
        # runtime: only the closed codes themselves are accepted, never free text.
        if len(set(self.checks)) != len(self.checks) or not _all_of(Check, self.checks):
            problems.append("checks")
        if not _all_of(Finding, self.findings) or any(n < 1 for n in self.findings.values()):
            problems.append("findings")
        if not _all_of(Check, self.durations_ms):
            problems.append("durations_ms")
        if self.error_type is not None and not _ERROR_TYPE.fullmatch(self.error_type):
            problems.append("error_type")
        amounts = (self.reserved_micro_usd, self.settled_micro_usd)
        if any(amount is not None and amount < 0 for amount in amounts):
            problems.append("micro_usd")
        if min(self.invisible, self.budget_refused) < 0:
            problems.append("counts")
        if any(not math.isfinite(ms) or ms < 0 for ms in self.durations_ms.values()):
            problems.append("durations_ms")
        return list(dict.fromkeys(problems))

    def as_extra(self) -> dict[str, object]:
        """The record as JSON-ready values, in a fixed key order."""
        decision = self.decision
        return {
            "business_id": self.business_id,
            "trace_id": self.trace_id,
            "route": self.route.value,
            "rules_version": self.rules_version,
            "action": decision.action.value,
            "decided_by": decision.decided_by.value,
            "priority": None if decision.priority is None else decision.priority.value,
            "guidance": decision.guidance,
            "checks": [check.value for check in self.checks],
            "findings": {code.value: n for code, n in sorted(self.findings.items())},
            "invisible": self.invisible,
            "error_type": self.error_type,
            "reserved_micro_usd": self.reserved_micro_usd,
            "settled_micro_usd": self.settled_micro_usd,
            "budget_refused": self.budget_refused,
            "withheld_action_id": (
                None if self.withheld_action_id is None else str(self.withheld_action_id)
            ),
            "durations_ms": {check.value: ms for check, ms in sorted(self.durations_ms.items())},
        }


def emit_guard_record(record: GuardRecord) -> None:
    """Write the one `guard` record of a request, at INFO."""
    logger.info(GUARD_RECORD, extra={GUARD_RECORD: record.as_extra()})
