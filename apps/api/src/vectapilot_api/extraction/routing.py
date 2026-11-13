"""Review routing per field (ADR 0017 decision 5, decided D5 (a); E1, E3, E4; M6 PR 2).

Guide: docs/learning/m6-routing.md · Tests: tests/extraction/test_routing.py (strict xfails until
the body is written) and tests/extraction/test_routing_types.py (the types below, passing now).
The plan's "H1" section (docs/plans/m6-pr-checks.md).

**Team code, built and tested:** the types `route_fields` reads and returns. Signals are facts
that code computed about one field (PR 2's `signals.py`: is the quote on a page, is the value in
it, ...); a decision is what happens to the field (`accepted`, `review` or `absent`) with its
level and its reasons. Each type checks itself when it is built, so an impossible decision (an
accepted `low` value, a review without a reason) can never be constructed, by H1 or by a test.

**H1, `[HAND-WRITE]` by shesha (decided D7 (b)), time-box 1.5 h:** the body of `route_fields`,
at the end of this file. After it, team code runs `invariants.py` (E3), which forces any
`accepted` or `absent` field its signals do not justify to `review` and counts it: a guardrail
does not trust the check it guards. A change to this file bumps `EXTRACTION_RULES_VERSION`
(plan, "Scoring rules").
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vectapilot_api.extraction.config import KindConfig


class Level(StrEnum):
    """An ordinal level from checks in code (D4 (a)), never a probability: no percentage is shown,
    because none is calibrated (rule 3). Only a value has a level."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FieldStatus(StrEnum):
    ACCEPTED = "accepted"
    """A `high` value outside the always-review list: no human needs to look."""
    REVIEW = "review"
    """A human must look: the value is doubtful, or a missing value may really be there."""
    ABSENT = "absent"
    """No value, and nothing suggests the document has one."""


class DocumentStatus(StrEnum):
    COMPLETE = "complete"
    """No field is in review."""
    NEEDS_REVIEW = "needs_review"


class Reason(StrEnum):
    """Why a field is where it is. One closed vocabulary for signals, decisions and storage."""

    # A check failed: the value is `low`.
    QUOTE_NOT_FOUND = "quote_not_found"
    """The quote is not in any one page's text (E2)."""
    VALUE_NOT_IN_QUOTE = "value_not_in_quote"
    """The value is not the quote's value (E2: `AB1` in `Policy no. AB123` fails)."""
    VALUE_CUT = "value_cut"
    """The quote stops before its line's text does (E2: `Patient name: Asha` on the line
    `Patient name: Asha Rao`)."""
    BAD_DATE = "bad_date"
    BAD_PATTERN = "bad_pattern"
    BAD_ENUM = "bad_enum"
    TOO_LONG = "too_long"
    NAME_CHARSET = "name_charset"
    QUOTE_TOO_LONG = "quote_too_long"
    RULE_FAILED = "rule_failed"
    # A signal lowers the value to `medium`.
    UNSURE = "unsure"
    """The model said it is not sure (`unsure` can only lower, E1)."""
    FROM_OCR = "from_ocr"
    LABEL_NOT_IN_QUOTE = "label_not_in_quote"
    SHARED_VALUE = "shared_value"
    CONFLICTING_VALUES = "conflicting_values"
    DATE_AMBIGUOUS = "date_ambiguous"
    IMAGE_ON_PAGE = "image_on_page"
    # A value's review.
    ALWAYS_REVIEW = "always_review"
    # A missing value's review (E4).
    NOT_FOUND = "not_found"
    """The field is required."""
    LABEL_PRESENT = "label_present"
    """One of the field's labels is in the document's text."""
    IMAGE_PAGE = "image_page"
    """A page has an image: text on it may be unseen."""
    # The whole extraction.
    MISSING_SIGNAL = "missing_signal"
    """Signals did not cover this field."""
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_OUTPUT = "invalid_output"
    TRUNCATED = "truncated"
    TIMEOUT = "timeout"
    INTERNAL_ERROR = "internal_error"
    # After H1 (team code, `invariants.py`).
    ROUTING_INVARIANT = "routing_invariant"


CHECK_FAILURES: Final = frozenset(
    {
        Reason.BAD_DATE,
        Reason.BAD_PATTERN,
        Reason.BAD_ENUM,
        Reason.TOO_LONG,
        Reason.NAME_CHARSET,
        Reason.QUOTE_TOO_LONG,
        Reason.RULE_FAILED,
        Reason.VALUE_CUT,
    }
)
"""The reason codes signals may set in `FieldSignals.codes` (types, patterns, lengths, rules)."""
FAILURES: Final = frozenset(
    {
        Reason.MODEL_UNAVAILABLE,
        Reason.INVALID_OUTPUT,
        Reason.TRUNCATED,
        Reason.TIMEOUT,
        Reason.INTERNAL_ERROR,
    }
)
"""E3's failures: what `DocumentSignals.failure` may be."""


@dataclass(frozen=True)
class FieldSignals:
    """What code found about one field's proposal (PR 2's `signals.py`). Facts, not decisions.

    A value's signals (`quote_found` to `image_on_page`) mean something only if `has_value`; for a
    missing value only `label_present` does."""

    name: str
    has_value: bool
    """The model proposed a value (not null)."""
    quote_found: bool = False
    """The quote is in one page's text (E2)."""
    value_in_quote: bool = False
    """The value is the quote's value (E2)."""
    label_in_quote: bool = False
    """One of the field's template labels is in the quote."""
    codes: frozenset[Reason] = frozenset()
    """Failed checks of the value's type, pattern, length and rules: a subset of
    `CHECK_FAILURES`."""
    unsure: bool = False
    from_ocr: bool = False
    """The quote's page was read by an OCR engine."""
    shared_value: bool = False
    """Another field has the same value or the same quote."""
    conflicting_values: bool = False
    """Two places where a label of this field reads as a label give different text after it."""
    date_ambiguous: bool = False
    """The quote's date still parses two ways under the business's formats."""
    image_on_page: bool = False
    """The quote's page has an image (E4)."""
    label_present: bool = False
    """One of the field's labels is in the document's text (anywhere)."""

    def __post_init__(self) -> None:
        if not self.codes <= CHECK_FAILURES:
            raise ValueError("codes holds only check failures (CHECK_FAILURES)")


@dataclass(frozen=True)
class DocumentSignals:
    """Everything H1 reads about one document: each field's signals, whether any page has an
    image (E4), and the failure that stopped the extraction, if any (E3)."""

    fields: tuple[FieldSignals, ...]
    has_image_page: bool = False
    failure: Reason | None = None

    def __post_init__(self) -> None:
        names = [signals.name for signals in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("each field has signals at most once")
        if self.failure is not None and self.failure not in FAILURES:
            raise ValueError("failure is one of E3's failures (FAILURES)")


@dataclass(frozen=True)
class ReviewPolicy:
    """The business's kind, as routing sees it: the fields in schema order, the required ones and
    the always-review ones (the template's plus the business's, E5)."""

    fields: tuple[str, ...]
    required: frozenset[str] = frozenset()
    always_review: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("a policy lists at least one field, each once")
        if not (self.required | self.always_review) <= set(self.fields):
            raise ValueError("required and always_review name fields of the policy")

    @classmethod
    def for_kind(cls, kind: KindConfig) -> "ReviewPolicy":
        """The policy of one business's kind, from its configuration."""
        template = kind.template
        return cls(
            fields=tuple(spec.name for spec in template.fields),
            required=frozenset(spec.name for spec in template.fields if spec.required),
            always_review=kind.always_review,
        )


@dataclass(frozen=True)
class FieldDecision:
    """What happens to one field, checked when built:
    - `accepted`: level `high`, no reasons;
    - `absent`: no level, no reasons;
    - `review`: at least one reason (and a level if the field has a value)."""

    field: str
    status: FieldStatus
    level: Level | None = None
    reasons: frozenset[Reason] = frozenset()

    def __post_init__(self) -> None:
        if self.status is FieldStatus.ACCEPTED and (self.level is not Level.HIGH or self.reasons):
            raise ValueError("an accepted field is high, with no reasons")
        if self.status is FieldStatus.ABSENT and (self.level is not None or self.reasons):
            raise ValueError("an absent field has no level and no reasons")
        if self.status is FieldStatus.REVIEW and not self.reasons:
            raise ValueError("a field in review has at least one reason")


@dataclass(frozen=True)
class ExtractionDecision:
    """Every field's decision, in schema order, and the document's status: `complete` exactly
    when no field is in review (checked when built)."""

    status: DocumentStatus
    fields: tuple[FieldDecision, ...]
    by_field: Mapping[str, FieldDecision] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        names = [decision.field for decision in self.fields]
        if not names or len(set(names)) != len(names):
            raise ValueError("a decision has at least one field, each once")
        in_review = any(decision.status is FieldStatus.REVIEW for decision in self.fields)
        if (self.status is DocumentStatus.COMPLETE) == in_review:
            raise ValueError("a document is complete exactly when no field is in review")
        object.__setattr__(self, "by_field", MappingProxyType({d.field: d for d in self.fields}))

    @classmethod
    def of(cls, decisions: Sequence[FieldDecision]) -> "ExtractionDecision":
        """The decision with its status computed from `decisions` (rule 6)."""
        in_review = any(decision.status is FieldStatus.REVIEW for decision in decisions)
        status = DocumentStatus.NEEDS_REVIEW if in_review else DocumentStatus.COMPLETE
        return cls(status, tuple(decisions))


# ── H1 (M6) — `[HAND-WRITE]`, written by shesha (decided D7 (b)); time-box 1.5 h ───────────────


def route_fields(doc: DocumentSignals, policy: ReviewPolicy) -> ExtractionDecision:
    """Decide every field of `policy`: accepted, review or absent, with its level and reasons.

    Contract (ADR 0017 decision 5; the plan's "H1"). Pure: no I/O, no logging, no clock, and the
    inputs are never changed.

    1. **Every field, once, in schema order.** Return one `FieldDecision` for each name in
       `policy.fields`, in that order. A field without signals in `doc.fields` is `review` with
       `MISSING_SIGNAL` and no level. Signals for a field `policy` does not have raise
       `ValueError` (a caller bug), always, before anything else.
    2. **The level of a value** (`has_value`): `LOW` if the quote is not found
       (`QUOTE_NOT_FOUND`), the value is not in its quote (`VALUE_NOT_IN_QUOTE`) or any reason
       code is set (each code in `codes`); else `MEDIUM` if any of `unsure` (`UNSURE`),
       `from_ocr` (`FROM_OCR`), no label in the quote (`LABEL_NOT_IN_QUOTE`), `shared_value`,
       `conflicting_values`, `date_ambiguous` or `image_on_page` (the reason of the same name);
       else `HIGH`. No value: no level.
    3. **A value is `accepted` only at `HIGH` and outside `policy.always_review`**; otherwise it
       is `review`, with every reason that applies: all of rule 2's that are set (a `LOW` value's
       `MEDIUM` signals too), plus `ALWAYS_REVIEW` if the field is in the list.
    4. **No value:** `review` with every reason that applies: `NOT_FOUND` if the field is in
       `policy.required`, `LABEL_PRESENT` if its `label_present`, `IMAGE_PAGE` if
       `doc.has_image_page` (E4: unseen is not absent). None applies: `absent`, no reasons.
       `ALWAYS_REVIEW` is about values only: a missing always-review field follows this rule.
    5. **A failure** (`doc.failure` is set, E3): every field is `review` with that one reason and
       no level, whatever its signals say.
    6. **The document** is `COMPLETE` only if no field is `review`; otherwise `NEEDS_REVIEW`
       (`ExtractionDecision.of` computes it).

    TODO (shesha, H1): write the body. The guide's §2 explains each rule with a hand-checkable
    example; §4 maps the tests to the rules.
    """
    raise NotImplementedError("H1 (M6): write route_fields (guide: docs/learning/m6-routing.md)")
