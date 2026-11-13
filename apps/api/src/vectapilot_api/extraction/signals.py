"""Signals: the facts code computes about each proposed field (ADR 0017 decisions 4 and 6; E1, E2,
E4, E5, E6; M6 PR 2).

The model proposes `{value, quote, unsure}` per field (`extractor.py`). Nothing it says is trusted
(E1): this module checks each proposal against the document's own page text and the business's
configuration and records **facts** (`routing.FieldSignals`). H1's `route_fields` turns facts into
decisions; `invariants.py` checks those decisions against the same facts.

**E2 · No quote, no value.** Both sides are compared in `canon` form: NFKC, case-folded,
whitespace collapsed (as `config.fold`), with typographic apostrophes and dashes made plain.
- *Quote found:* the quote is inside one page's text. A quote that runs across two pages never
  matches, because each page is searched on its own. `page` is the first page that holds it,
  `quote_hits` how often it occurs in the whole document.
- *Value in quote,* by field type: an **identifier** or an **enum** synonym is a whole token run
  of the quote (`AB1` is not in `Policy no. AB123`, `12` is not in `2012`); a **text** value
  equals the quote minus its matched label and the punctuation around it (`John` is not the value
  of `Patient: John Smith`); a **date** equals a date token of the quote, both parsed under the
  business's formats and compared as dates (never rendered with `strftime`).
- *Runs to its end* (`value_cut` otherwise): at some place the quote occurs, nothing follows it
  on its line but punctuation or a label place of another field (a two-column row). Without it,
  a value cut short with its quote cut at the same place (`Asha` from `Patient name: Asha` on the
  line `Patient name: Asha Rao`) would pass every other check.
- *Label in quote:* one of the field's template labels is a whole token run of the quote, and at
  some place the quote occurs it holds a **label place** of the field (below).

**Label places.** A template label *reads as a label* where it starts its line (after punctuation
only) or has `:` after it; elsewhere it is a word of the text (`Clinic` in `Paws and Co Clinic`).
Longer labels claim their places first, across every field of the template, so `Patient` never
competes with `Patient name` and `Name` never fires inside `Owner name`. A place *reads* the text
after it up to the next label place on its line or the line's end; if that is empty (a label
column), the next non-empty line up to its first label place.

**The other signals.** `shared_value`: another field has the same value or the same quote (one name
proposed as patient and as referring doctor). `conflicting_values`: two label places of the field
read different text, wherever on a line each sits (a hidden `3 Tr` copy merged onto another row,
behind a prefix or in a label column). It costs review on a document that repeats a label with other
text, never a silent value. `label_present`: one of the field's labels is anywhere in the text (for
a missing value: E4, unseen is not absent). `date_ambiguous` [proposal]: the value or its quote
token reads as two different dates, either under the business's own formats or because it is a
numeric day-first or month-first date whose day and month could be swapped (`5/1/2026`): E5 refuses
a format set that mixes both orders, but the writer of a document may not follow the business's
convention, so such a date is never `high`. `image_on_page` and `from_ocr`: a page holding the quote
has an image, or was read by an OCR engine (E4). `unsure`: the model's own flag, which can only
lower a level (E1).

**Reason codes** (`routing.CHECK_FAILURES`): `bad_date` (not a date under the business's
formats), `bad_pattern` (not the identifier's pattern), `bad_enum` (not a synonym of one of the
business's values), `too_long` (over the field's bound), `name_charset` (the `name` rule: letters,
spaces and `. ' - &`), `quote_too_long` (over `config.QUOTE_MAX`), `rule_failed` (a cross rule:
both fields of a failed `after` rule get it, because code cannot tell which date is wrong),
`value_cut` (the quote does not run to its end, above).

**The stored value and span (D6 (a)).** `FieldEvidence.value` is the canonical form where the
checks allow it (an ISO date, an enum id, an identifier or a text as printed, whitespace
collapsed), otherwise the proposal as given; it stays untrusted data for every consumer (E7).
`span` is the quote's label-to-value text, at most `SPAN_MAX` characters, or the value's own text
if the label is further away, or None: never the rest of a quote, which may hold text near the
value that the business must not keep. Neither is ever logged.

**Never raises.** A check that fails unexpectedly costs its field, never the document: the field
gets no signals (H1 sends it to review with `missing_signal`) and the exception's type name is
kept in `SignalReport.errors` (no text: E9). A failure in the cross-field step drops every
field's signals. Only `Exception` is caught, never `BaseException`.

`EXTRACTION_RULES_VERSION` is bumped with any change to this file, `routing.py` or
`invariants.py` (plan, "Scoring rules"); `rules_fingerprints.py` holds each version's fingerprint
and a test enforces it, as `PROMPT_FINGERPRINTS` does for prompts.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel

from vectapilot_api.extraction.config import (
    DATE_FORMATS,
    DATE_VALUE_MAX,
    NAME_PUNCTUATION,
    QUOTE_MAX,
    DateFormat,
    DateOrder,
    FieldSpec,
    FieldType,
    KindConfig,
    Rule,
    Template,
)
from vectapilot_api.extraction.routing import DocumentSignals, FieldSignals, Reason
from vectapilot_api.extraction.text import DocumentText

EXTRACTION_RULES_VERSION: Final = "extraction-rules.1"
"""Bump with any change to `signals.py`, `routing.py` or `invariants.py`, and add the new
version's fingerprint to `rules_fingerprints.py` (tests/extraction/test_rules_version.py)."""

SPAN_MAX: Final = 40
"""D6 (a): the longest label-to-value span stored or returned."""

_TYPOGRAPHIC: Final = str.maketrans(
    dict.fromkeys((0x2018, 0x2019, 0x02BC), "'")
    | dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212), "-")
)
"""Typographic apostrophes (U+2018, U+2019, U+02BC) and dashes (U+2010 to U+2014, U+2212), read
as plain ones, so a curly `Pet's name` still matches the label."""

_NUMERIC_DATE: Final = re.compile(
    r"(?<![\w./-])[0-9]{1,4}[./-][0-9]{1,2}[./-][0-9]{1,4}(?![\w/-])(?!\.\w)"
)
_DAY_MONTH_YEAR: Final = re.compile(r"(?<!\w)[0-9]{1,2} [a-z]{3,9} [0-9]{4}(?!\w)")
_MONTH_DAY_YEAR: Final = re.compile(r"(?<!\w)[a-z]{3,9} [0-9]{1,2}, [0-9]{4}(?!\w)")
DATE_TOKENS: Final = (_NUMERIC_DATE, _DAY_MONTH_YEAR, _MONTH_DAY_YEAR)
"""The shapes of every format in `config.DATE_FORMATS`, over `canon` text (ASCII digits only):
candidates, each then parsed under the business's formats."""
SWAPPABLE: Final = frozenset({DateOrder.DAY_FIRST, DateOrder.MONTH_FIRST})


# ── Text forms ─────────────────────────────────────────────────────────────────────────────────


def plain(text: str) -> str:
    """NFKC, whitespace collapsed, typographic apostrophes and dashes made plain; case kept."""
    return " ".join(unicodedata.normalize("NFKC", text).split()).translate(_TYPOGRAPHIC)


def canon(text: str) -> str:
    """E2's comparison form: `plain`, case-folded."""
    return plain(text).casefold()


def trim(text: str) -> str:
    """`text` without the characters that are not letters or digits at either end."""
    start, end = 0, len(text)
    while start < end and not text[start].isalnum():
        start += 1
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return text[start:end]


def token_runs(needle: str, haystack: str) -> list[tuple[int, int]]:
    """Every place `needle` occurs in `haystack` as a whole run of tokens: no letter, digit or
    underscore right before or after it. Both in `canon` form; an empty needle never occurs."""
    if not needle:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(needle)}(?!\w)")
    return [match.span() for match in pattern.finditer(haystack)]


def identifier_runs(needle: str, haystack: str) -> list[tuple[int, int]]:
    """Like `token_runs`, but `.`, `/` and `-` join an identifier's parts: `204117` is not in
    `lhm-204117`, while `ab123` is in `no. ab123.` (a full stop after it ends the token)."""
    if not needle:
        return []
    pattern = re.compile(rf"(?<![\w./-]){re.escape(needle)}(?![\w/-])(?!\.\w)")
    return [match.span() for match in pattern.finditer(haystack)]


def find_label(labels: Sequence[str], quote: str) -> tuple[int, int] | None:
    """The first, then longest, whole-token place one of `labels` occurs in `quote` (`canon`)."""
    places = [place for label in labels for place in token_runs(canon(label), quote)]
    if not places:
        return None
    return min(places, key=lambda place: (place[0], place[0] - place[1]))


# ── Value checks: each returns facts and codes, never raises on any string ────────────────────


@dataclass(frozen=True)
class ValueCheck:
    """One value against its quote: the codes it failed, whether it is the quote's value, where
    it is in the quote (`canon` positions), its canonical form and its date, if it is one."""

    codes: frozenset[Reason] = frozenset()
    in_quote: bool = False
    place: tuple[int, int] | None = None
    canonical: str | None = field(default=None, repr=False)
    date_value: date | None = field(default=None, repr=False)
    date_ambiguous: bool = False


def parse_date(text: str, formats: Sequence[DateFormat]) -> tuple[tuple[date, DateOrder], ...]:
    """Every reading of `text` under `formats`, in their order; () for none. Never raises."""
    if not text or len(text) > DATE_VALUE_MAX:
        return ()
    readings: list[tuple[date, DateOrder]] = []
    for name in formats:
        spec = DATE_FORMATS[name]
        try:
            parsed = datetime.strptime(text, spec.strptime).date()
        except (ValueError, OverflowError):
            continue
        readings.append((parsed, spec.order))
    return tuple(readings)


def _swapped(reading: tuple[date, DateOrder]) -> date | None:
    """The other numeric order's reading of the same digits (day and month swapped), if valid."""
    day, order = reading
    if order not in SWAPPABLE:
        return None
    try:
        return date(day.year, day.day, day.month)
    except ValueError:
        return None


def ambiguous(readings: Sequence[tuple[date, DateOrder]]) -> bool:
    """Whether the readings name two different dates, counting a swappable day and month."""
    dates = {day for day, _ in readings}
    if len(dates) > 1:
        return True
    return any(
        (other := _swapped(reading)) is not None and other != reading[0] for reading in readings
    )


def date_tokens(quote: str) -> list[tuple[int, int]]:
    """The places in `quote` (`canon`) shaped like a date (`DATE_TOKENS`), left to right."""
    places = {match.span() for pattern in DATE_TOKENS for match in pattern.finditer(quote)}
    return sorted(places)


def check_date(value: str, quote: str, formats: Sequence[DateFormat]) -> ValueCheck:
    """A date value: parsed under `formats`, then equal to a date token of `quote` (E2)."""
    text = canon(value)
    if len(text) > DATE_VALUE_MAX:
        return ValueCheck(codes=frozenset({Reason.TOO_LONG}))
    readings = parse_date(text, formats)
    if not readings:
        return ValueCheck(codes=frozenset({Reason.BAD_DATE}))
    day = readings[0][0]
    for start, end in date_tokens(quote):
        token = parse_date(quote[start:end], formats)
        if day in {reading[0] for reading in token}:
            return ValueCheck(
                in_quote=True,
                place=(start, end),
                canonical=day.isoformat(),
                date_value=day,
                date_ambiguous=ambiguous(readings) or ambiguous(token),
            )
    return ValueCheck(canonical=day.isoformat(), date_value=day, date_ambiguous=ambiguous(readings))


def check_identifier(value: str, quote: str, spec: FieldSpec) -> ValueCheck:
    """An identifier: within its length, matching its pattern, a whole token of `quote`."""
    printed = plain(value)
    limit = spec.max_length or 0
    if len(printed) > limit:
        return ValueCheck(codes=frozenset({Reason.TOO_LONG}))  # length before the pattern
    codes: frozenset[Reason] = frozenset()
    if spec.pattern is None or re.fullmatch(spec.pattern, printed) is None:
        codes = frozenset({Reason.BAD_PATTERN})
    places = identifier_runs(printed.casefold(), quote)
    return ValueCheck(
        codes=codes,
        in_quote=bool(places),
        place=places[0] if places else None,
        canonical=printed,
    )


def check_enum(value: str, quote: str, spec: FieldSpec, kind: KindConfig) -> ValueCheck:
    """An enum: one of the business's values by its id or a synonym, printed whole in `quote`."""
    printed = trim(canon(value))
    allowed = kind.enum_values.get(spec.name, ())
    match = next(
        (
            choice
            for choice in allowed
            if printed == canon(choice.id) or any(printed == canon(s) for s in choice.synonyms)
        ),
        None,
    )
    places = token_runs(printed, quote)
    return ValueCheck(
        codes=frozenset() if match is not None else frozenset({Reason.BAD_ENUM}),
        in_quote=bool(places),
        place=places[0] if places else None,
        canonical=None if match is None else match.id,
    )


def name_allowed(text: str) -> bool:
    """The `name` rule: letters, spaces and `. ' - &` only (`config.NAME_PUNCTUATION`)."""
    return all(character.isalpha() or character in NAME_PUNCTUATION for character in text)


def check_text(
    value: str, quote: str, spec: FieldSpec, label: tuple[int, int] | None
) -> ValueCheck:
    """A text value: the quote minus its matched label and the punctuation around it (E2)."""
    printed = plain(value)
    codes: set[Reason] = set()
    if spec.max_length is not None and len(printed) > spec.max_length:
        codes.add(Reason.TOO_LONG)
    if Rule.NAME in spec.rules and not name_allowed(printed):
        codes.add(Reason.NAME_CHARSET)
    wanted = trim(printed.casefold())
    rest = quote if label is None else f"{quote[: label[0]]} {quote[label[1] :]}"
    in_quote = bool(wanted) and trim(" ".join(rest.split())) == wanted
    place = _text_place(wanted, quote, label)
    return ValueCheck(frozenset(codes), in_quote, place, canonical=printed)


def _text_place(wanted: str, quote: str, label: tuple[int, int] | None) -> tuple[int, int] | None:
    """Where `wanted` is in `quote`: after the label if it is there, else anywhere."""
    if not wanted:
        return None
    start = quote.find(wanted, 0 if label is None else label[1])
    if start < 0:
        start = quote.find(wanted)
    return None if start < 0 else (start, start + len(wanted))


def stored_span(
    source: str, label: tuple[int, int] | None, place: tuple[int, int] | None
) -> str | None:
    """D6 (a): label to value if at most `SPAN_MAX` characters, else the value's own text if it
    fits, else None. `source` is the quote in `plain` form (positions as in its `canon` form)."""
    if place is None:
        return None
    if label is not None:
        start, end = min(label[0], place[0]), max(label[1], place[1])
        if end - start <= SPAN_MAX:
            return source[start:end]
    if place[1] - place[0] <= SPAN_MAX:
        return source[place[0] : place[1]]
    return None


# ── The document's lines and labels ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LabelPlace:
    """Where a template label reads as a label on one page (`canon` positions in its line text):
    at the start of its line (after punctuation only) or followed by `:`."""

    start: int
    end: int
    field: str


def _line_end(text: str, position: int) -> int:
    end = text.find("\n", position)
    return len(text) if end < 0 else end


def label_places(text: str, labels: Mapping[str, Sequence[str]]) -> tuple[LabelPlace, ...]:
    """Every place in `text` (one page, `canon` lines joined by `\\n`) where a label of a field in
    `labels` reads as a label, left to right. Longest label first: a place inside a longer
    label's place is no place (`patient` inside `patient name`, `clinic` inside `issuing
    clinic`). A label in the middle or at the end of a line without `:` after it is part of the
    text, not a label (`Paws and Co Clinic`)."""
    candidates = sorted(
        {(canon(label), name) for name, names in labels.items() for label in names},
        key=lambda item: (-len(item[0]), item[1], item[0]),
    )
    taken: list[tuple[int, int, str]] = []
    for label, name in candidates:
        for start, end in token_runs(label, text):
            if all(end <= other[0] or start >= other[1] for other in taken):
                taken.append((start, end, name))
    places: list[LabelPlace] = []
    for start, end, name in sorted(taken):
        line_start = text.rfind("\n", 0, start) + 1
        after = text[end : _line_end(text, end)].lstrip(" ")
        if not trim(text[line_start:start]) or after.startswith(":"):
            places.append(LabelPlace(start, end, name))
    return tuple(places)


def _reading(text: str, places: Sequence[LabelPlace], place: LabelPlace) -> str:
    """The text a label place reads: after it, up to the next label place on its line or the
    line's end, trimmed. If that is empty (a label column), the next non-empty line up to its
    first label place, trimmed (possibly empty)."""
    line_end = _line_end(text, place.end)
    bound = min((p.start for p in places if place.end <= p.start < line_end), default=line_end)
    found = trim(text[place.end : bound])
    if found:
        return found
    position = line_end + 1
    while position <= len(text):
        end = _line_end(text, position)
        if trim(text[position:end]):
            bound = min((p.start for p in places if position <= p.start < end), default=end)
            return trim(text[position:bound])
        position = end + 1
    return ""


def label_readings(
    texts: Sequence[str], places: Sequence[Sequence[LabelPlace]], name: str
) -> frozenset[str]:
    """The distinct texts `name`'s label places read across the pages (`_reading`). More than one
    is `conflicting_values`, wherever a copy sits (a hidden `3 Tr` copy anywhere on a line)."""
    return frozenset(
        _reading(text, page, place)
        for text, page in zip(texts, places, strict=True)
        for place in page
        if place.field == name
    )


def quote_pattern(quote: str) -> re.Pattern[str] | None:
    """`quote` (`canon`) as a pattern over line text: each space matches spaces or line breaks."""
    tokens = quote.split(" ")
    if not all(tokens):
        return None
    return re.compile("[ \n]+".join(re.escape(token) for token in tokens))


@dataclass(frozen=True)
class QuoteFacts:
    """Where the quote sits in the document's lines (E2), over every place it occurs."""

    runs_to_end: bool = False
    """At some place, nothing follows the quote on its line but punctuation or a label place of
    another field (a two-column row). `Patient name: Asha` on the line `Patient name: Asha Rao`
    does not (`value_cut`)."""
    labelled: bool = False
    """At some place, the quote holds a label place of its own field (`label_places`): a label
    that reads as a label, not a word in the middle of a line."""


def quote_facts(
    quote: str, texts: Sequence[str], places: Sequence[Sequence[LabelPlace]], name: str
) -> QuoteFacts:
    """`QuoteFacts` for `quote` (`canon`) over each page's line text and label places."""
    pattern = quote_pattern(quote)
    if pattern is None:
        return QuoteFacts()
    ends = labelled = False
    for text, page in zip(texts, places, strict=True):
        others = {place.start for place in page if place.field != name}
        own = [place for place in page if place.field == name]
        found = pattern.search(text)
        while found is not None:
            position, line_end = found.end(), _line_end(text, found.end())
            while position < line_end and not text[position].isalnum():
                position += 1
            ends = ends or position == line_end or position in others
            labelled = labelled or any(
                found.start() <= place.start and place.end <= found.end() for place in own
            )
            if ends and labelled:
                return QuoteFacts(runs_to_end=True, labelled=True)
            found = pattern.search(text, found.start() + 1)
    return QuoteFacts(runs_to_end=ends, labelled=labelled)


def label_present(texts: Sequence[str], labels: Sequence[str]) -> bool:
    """Whether one of `labels` is a whole token run of one page's text (`canon`)."""
    return any(token_runs(canon(label), text) for label in labels for text in texts)


@dataclass(frozen=True)
class _Pages:
    """The document in `canon` form: each page's text (whitespace collapsed), its line text
    (`canon` lines joined by `\\n`) and its label places (`label_places`)."""

    texts: tuple[str, ...]
    line_texts: tuple[str, ...]
    places: tuple[tuple[LabelPlace, ...], ...]
    images: tuple[bool, ...]
    ocr: tuple[bool, ...]

    @classmethod
    def of(cls, document: DocumentText, template: Template) -> "_Pages":
        labels = {spec.name: spec.labels for spec in template.fields}
        line_texts = tuple(
            "\n".join(canon(line) for line in page.text.split("\n")) for page in document.pages
        )
        return cls(
            texts=tuple(" ".join(text.split()) for text in line_texts),
            line_texts=line_texts,
            places=tuple(label_places(text, labels) for text in line_texts),
            images=tuple(page.has_images for page in document.pages),
            ocr=tuple(page.ocr for page in document.pages),
        )


# ── One field ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldEvidence:
    """What is stored and returned for one field besides its decision (D6 (a)): untrusted data,
    never logged."""

    name: str
    value: str | None = field(default=None, repr=False)
    span: str | None = field(default=None, repr=False)
    page: int | None = None
    quote_hits: int = 0


@dataclass(frozen=True)
class _Field:
    """One field's signals and evidence before the cross-field step, and what that step reads."""

    signals: FieldSignals
    evidence: FieldEvidence
    value_key: str | None = field(default=None, repr=False)
    quote_key: str | None = field(default=None, repr=False)
    date_value: date | None = field(default=None, repr=False)


def _proposal(answer: BaseModel, name: str) -> tuple[str | None, str | None, bool]:
    """The field's `(value, quote, unsure)`; TypeError if the answer does not hold them."""
    item = getattr(answer, name)
    value, quote, unsure = item.value, item.quote, item.unsure
    if not (
        (value is None or isinstance(value, str))
        and (quote is None or isinstance(quote, str))
        and isinstance(unsure, bool)
    ):
        raise TypeError("the answer's field does not have the output model's shape")
    return value, quote, unsure


def _check(value: str, quote: str, spec: FieldSpec, kind: KindConfig) -> ValueCheck:
    if spec.type is FieldType.DATE:
        return check_date(value, quote, kind.date_formats)
    if spec.type is FieldType.IDENTIFIER:
        return check_identifier(value, quote, spec)
    if spec.type is FieldType.ENUM:
        return check_enum(value, quote, spec, kind)
    return check_text(value, quote, spec, find_label(spec.labels, quote))


def field_signals(spec: FieldSpec, kind: KindConfig, pages: _Pages, answer: BaseModel) -> _Field:
    """One field's signals (module docstring). May raise on a bug; `compute_signals` catches."""
    value, quote, unsure = _proposal(answer, spec.name)
    present = label_present(pages.texts, spec.labels)
    if value is None or not trim(canon(value)):
        return _Field(
            FieldSignals(spec.name, has_value=False, label_present=present),
            FieldEvidence(spec.name),
        )
    quote_text = quote or ""
    source = plain(quote_text)
    folded = source.casefold()
    if len(folded) != len(source):  # case folding changed lengths (`ß`): spans come from `canon`
        source = folded
    hit_pages = [index for index, text in enumerate(pages.texts) if folded and folded in text]
    hits = sum(text.count(folded) for text in pages.texts) if folded else 0
    label = find_label(spec.labels, folded)
    checked = _check(value, folded, spec, kind)
    codes = set(checked.codes)
    if len(quote_text) > QUOTE_MAX:
        codes.add(Reason.QUOTE_TOO_LONG)
    facts = quote_facts(folded, pages.line_texts, pages.places, spec.name)
    if hit_pages and not facts.runs_to_end:
        codes.add(Reason.VALUE_CUT)
    readings = label_readings(pages.line_texts, pages.places, spec.name)
    signals = FieldSignals(
        spec.name,
        has_value=True,
        quote_found=bool(hit_pages),
        value_in_quote=checked.in_quote,
        label_in_quote=label is not None and facts.labelled,
        codes=frozenset(codes),
        unsure=unsure,
        from_ocr=any(pages.ocr[index] for index in hit_pages),
        conflicting_values=len(readings) > 1,
        date_ambiguous=checked.date_ambiguous,
        image_on_page=any(pages.images[index] for index in hit_pages),
        label_present=present,
    )
    evidence = FieldEvidence(
        spec.name,
        value=checked.canonical if checked.canonical is not None else plain(value),
        span=stored_span(source, label, checked.place) if hit_pages else None,
        page=hit_pages[0] + 1 if hit_pages else None,
        quote_hits=hits,
    )
    return _Field(signals, evidence, trim(canon(value)), folded or None, checked.date_value)


# ── The document ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignalReport:
    """Every field's signals for H1 (`document`), the evidence to store, and the type names of
    any check that failed unexpectedly (no text, E9)."""

    document: DocumentSignals
    evidence: Mapping[str, FieldEvidence]
    errors: tuple[str, ...] = ()


def _cross(kind: KindConfig, found: Mapping[str, _Field]) -> dict[str, FieldSignals]:
    """`shared_value` and the cross rules, applied to the per-field signals."""
    values = Counter(item.value_key for item in found.values() if item.value_key)
    quotes = Counter(item.quote_key for item in found.values() if item.quote_key)
    failed: set[str] = set()
    for cross in kind.template.cross_rules:
        later, earlier = found.get(cross.field), found.get(cross.other)
        if (
            cross.rule is Rule.AFTER
            and later is not None
            and earlier is not None
            and later.date_value is not None
            and earlier.date_value is not None
            and not later.date_value > earlier.date_value
        ):
            failed |= {cross.field, cross.other}
    result: dict[str, FieldSignals] = {}
    for name, item in found.items():
        signals = item.signals
        shared = signals.has_value and (
            (item.value_key is not None and values[item.value_key] > 1)
            or (item.quote_key is not None and quotes[item.quote_key] > 1)
        )
        codes = signals.codes | ({Reason.RULE_FAILED} if name in failed else set())
        result[name] = FieldSignals(
            name,
            has_value=signals.has_value,
            quote_found=signals.quote_found,
            value_in_quote=signals.value_in_quote,
            label_in_quote=signals.label_in_quote,
            codes=frozenset(codes),
            unsure=signals.unsure,
            from_ocr=signals.from_ocr,
            shared_value=shared,
            conflicting_values=signals.conflicting_values,
            date_ambiguous=signals.date_ambiguous,
            image_on_page=signals.image_on_page,
            label_present=signals.label_present,
        )
    return result


def compute_signals(kind: KindConfig, document: DocumentText, answer: BaseModel) -> SignalReport:
    """Every field's signals and evidence for `answer` over `document` (module docstring). Never
    raises (`Exception`): a field whose check fails has no signals, so it goes to review."""
    has_image_page = bool(document.image_pages)
    names = [spec.name for spec in kind.template.fields]
    errors: list[str] = []
    found: dict[str, _Field] = {}
    try:
        pages = _Pages.of(document, kind.template)
    except Exception as error:  # fail closed: no field has signals
        errors.append(type(error).__name__)
        pages = None
    if pages is not None:
        for spec in kind.template.fields:
            try:
                found[spec.name] = field_signals(spec, kind, pages, answer)
            except Exception as error:  # fail closed: this field has no signals
                errors.append(type(error).__name__)
    signals: dict[str, FieldSignals] = {}
    try:
        signals = _cross(kind, found)
    except Exception as error:  # fail closed: no field has signals
        errors.append(type(error).__name__)
    evidence = {
        name: found[name].evidence if name in signals else FieldEvidence(name) for name in names
    }
    return SignalReport(
        DocumentSignals(
            tuple(signals[name] for name in names if name in signals),
            has_image_page=has_image_page,
        ),
        MappingProxyType(evidence),
        tuple(errors),
    )
