"""The invariant check after routing (ADR 0017 decision 5; E1, E3, E4; M6 PR 2). Team code.

H1's `route_fields` decides; this module checks the decision against the same signals and
**forces back to review** every field the signals do not justify, counting each correction
(`ROUTING_INVARIANT`). A guardrail does not trust the check it guards: a slip in routing (or a
stand-in that accepts everything) can cost staff time, never a silent error.

It states only *when* a status is allowed, not how levels and reasons are chosen (that is H1):
- **`accepted`** needs a value whose quote was found and holds a label of the field, whose value
  is the quote's value, with no reason code and no lowering signal (`unsure`, `from_ocr`,
  `shared_value`, `conflicting_values`, `date_ambiguous`, `image_on_page`), outside the
  always-review list, in a document without a failure.
- **`absent`** needs no value, a field that is not required, none of its labels in the text, no
  page with an image (E4: unseen is not absent), and no failure.
- **Every field once, in schema order**: a field the decision left out is put in review; a
  decision for a field the policy does not have is dropped and counted.
- **A level only with a value**: a value in review without a level, or a missing value with one,
  is corrected.

A forced field keeps its reasons and gains `ROUTING_INVARIANT` (and the document's failure, if
any). Its level is `LOW` if it has a value, because the routing it came from was wrong, and None
otherwise. A field without signals counts as having no value. Pure: no I/O, no logging.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from vectapilot_api.extraction.routing import (
    DocumentSignals,
    ExtractionDecision,
    FieldDecision,
    FieldSignals,
    FieldStatus,
    Level,
    Reason,
    ReviewPolicy,
)


@dataclass(frozen=True)
class CheckedDecision:
    """The decision after the check, the fields it moved (schema order) and the decisions it
    dropped because their field is not in the policy."""

    decision: ExtractionDecision
    corrected: tuple[str, ...] = ()
    dropped: int = 0


def may_accept(signals: FieldSignals | None, policy: ReviewPolicy, doc: DocumentSignals) -> bool:
    """Whether the signals justify `accepted` (module docstring)."""
    return (
        signals is not None
        and doc.failure is None
        and signals.name not in policy.always_review
        and signals.has_value
        and signals.quote_found
        and signals.value_in_quote
        and signals.label_in_quote
        and not signals.codes
        and not (
            signals.unsure
            or signals.from_ocr
            or signals.shared_value
            or signals.conflicting_values
            or signals.date_ambiguous
            or signals.image_on_page
        )
    )


def may_be_absent(signals: FieldSignals | None, policy: ReviewPolicy, doc: DocumentSignals) -> bool:
    """Whether the signals justify `absent` (module docstring)."""
    return (
        signals is not None
        and doc.failure is None
        and not signals.has_value
        and signals.name not in policy.required
        and not signals.label_present
        and not doc.has_image_page
    )


def _forced(
    name: str, has_value: bool, doc: DocumentSignals, reasons: frozenset[Reason] = frozenset()
) -> FieldDecision:
    extra = {Reason.ROUTING_INVARIANT} | ({doc.failure} if doc.failure is not None else set())
    return FieldDecision(
        name, FieldStatus.REVIEW, Level.LOW if has_value else None, reasons | extra
    )


def _justified(
    decision: FieldDecision,
    signals: FieldSignals | None,
    policy: ReviewPolicy,
    doc: DocumentSignals,
) -> bool:
    has_value = signals is not None and signals.has_value
    if decision.status is FieldStatus.ACCEPTED:
        return may_accept(signals, policy, doc)
    if decision.status is FieldStatus.ABSENT:
        return may_be_absent(signals, policy, doc)
    return (decision.level is not None) == has_value


def enforce(
    decision: ExtractionDecision, doc: DocumentSignals, policy: ReviewPolicy
) -> CheckedDecision:
    """`decision` with every field its signals do not justify moved to review (module
    docstring), in `policy`'s order."""
    by_name: Mapping[str, FieldSignals] = {signals.name: signals for signals in doc.fields}
    fields: list[FieldDecision] = []
    corrected: list[str] = []
    for name in policy.fields:
        signals = by_name.get(name)
        has_value = signals is not None and signals.has_value
        given = decision.by_field.get(name)
        if given is not None and _justified(given, signals, policy, doc):
            fields.append(given)
            continue
        reasons = given.reasons if given is not None else frozenset()
        fields.append(_forced(name, has_value, doc, reasons))
        corrected.append(name)
    dropped = sum(1 for name in decision.by_field if name not in policy.fields)
    return CheckedDecision(ExtractionDecision.of(fields), tuple(corrected), dropped)
