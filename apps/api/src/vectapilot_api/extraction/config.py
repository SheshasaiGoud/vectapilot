"""Extraction templates and business files (ADR 0017 decision 2, decided D2 (a); M6 PR 1).

Two kinds of file, read from `VECTAPILOT_EXTRACTION_CONFIG_DIR` (`files.load_extraction_dir`):
- **A template per business type and document kind**, `templates/<business_type>/<kind>.json`:
  its fields (each with a type, a category, a description, its printed labels and its rules), the
  fields always sent to review, cross-field rules and the date formats a business may choose from.
  Templates are reviewed platform configuration: their text is the only instruction text the
  extraction prompt holds (E7).
- **A file per business**, `businesses/<business_id>.json`: `synthetic`, its business type
  (checked against the corpus manifest), the kinds it enables, and per kind the date formats it
  uses, a narrower enum list and extra always-review fields. It names no template: the template
  is selected by the business's type and the kind, never by a name or a path (rule 9), so one
  business's file can never select another type's template.

**Code holds closed vocabularies only** (rule 9): field types, rules, categories and date formats
are enums here; business types are `intents.policy.BusinessType`. Anything else in a file is
refused, never ignored. There is no clinical category (business-context §4.1, E6), and a field
naming a clinical term (`CLINICAL_TERMS`) is refused as a backstop to the template's review, so a
template cannot ask for a diagnosis, a reason for referral or a requested service (D6 (a), Q7).
Template text is prompt text (E7): one line, no markup characters (`PROMPT_MARKUP`).

**Tighten, never loosen (E5).** A business file cannot express a removal: it has no key for
fields, labels, rules or the template's always-review list (`extra="forbid"`). What it can say is
checked against its template (`resolve_kind`): its formats and enum values must be the template's
own, and its extra always-review fields must exist. Labels stay in the reviewed template, because
more labels would loosen PR 2's label check (plan "Review notes"). A set of formats that mixes
day-first and month-first numeric dates is refused, because `03/04/2026` would then read two ways.

**The output model checks shape only** (`compile_output_model`). The model's answer for one
document is an object with one key per field, each `{value, quote, unsure}`, closed
(`extra="forbid"`) and with maximum lengths at twice the bounds below. Types, patterns, enum
values, lengths and rules are PR 2's per-field signals, so one bad field costs that field (it goes
to review), never the whole document. No output model has a `business_id`: context is injected,
never answered by the model (E8).

**Errors name the file and the location, never a value.** Every model hides its input
(`hide_input_in_errors`); our validators' messages are fixed sentences with indexes only; the
business file uses lists rather than mappings, so no key a file wrote can reach a location.

The bounds are proposals (plan PR 1, "Bounds"), configuration rather than measurements.
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_validator,
)

from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.intents.policy import BusinessType
from vectapilot_api.tools.config import SingleLine, describe_validation_error

# ── Bounds [proposal] ──────────────────────────────────────────────────────────────────────────
MAX_FIELDS: Final = 8
"""Fields per template: the answer must fit the `small` alias's 1024 output tokens (D3 (a))."""
LABELS_PER_FIELD: Final = (1, 5)
LABEL_MAX: Final = 40
TEXT_MAX: Final = 200
"""A text field's `max_length` is at most this."""
QUOTE_MAX: Final = 120
"""A quote longer than this is PR 2's `quote_too_long`."""
IDENTIFIER_MAX: Final = 40
"""An identifier field's `max_length` is at most this."""
IDENTIFIER_PATTERN_MAX: Final = 64
DATE_VALUE_MAX: Final = 30
"""A date as printed (`14 September 2026` is 17 characters)."""
TEMPLATE_DATE_FORMATS_MAX: Final = 9
"""The closed list's size: a template may offer every format."""
BUSINESS_DATE_FORMATS_MAX: Final = 3
ENUM_VALUES_MAX: Final = 40
SYNONYMS_PER_VALUE: Final = (1, 5)
DESCRIPTION_MAX: Final = 300
TITLE_MAX: Final = 100
CROSS_RULES_MAX: Final = 8
OUTPUT_SLACK: Final = 2
"""The output model's maximum lengths are this many times the bounds: shape only, so a value just
over its bound fails its own field in PR 2 (`too_long`), not the whole answer."""
KINDS_MAX: Final = 20
"""Kinds one business may enable."""
RETENTION_DAYS: Final = (1, 90)
DEFAULT_RETENTION_DAYS: Final = 30
"""Days before a stored extraction is hidden and purgeable (D6 (a), E10; PR 3 stores it)."""

SNAKE_PATTERN: Final = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
"""A kind, a field name or an enum id: lower-case words joined by single underscores, so two
distinct names never give the same output class name."""
SNAKE_MAX: Final = 40

NAME_PUNCTUATION: Final = frozenset(" .'-&")
"""The `name` rule's characters besides letters (plan PR 1): spaces and `. ' - &`. PR 2 checks
values against it (`name_charset`)."""

CLINICAL_TERMS: Final[tuple[tuple[str, ...], ...]] = (
    ("diagnos",),
    ("symptom",),
    ("medicat",),
    ("medicine",),
    ("prescri",),
    ("dose",),
    ("dosage",),
    ("treatment",),
    ("therap",),
    ("allerg",),
    ("result",),
    ("test",),
    ("positive",),
    ("negative",),
    ("reason",),
    ("condition",),
    ("history",),
    ("illness",),
    ("request", "service"),
)
"""E6's code backstop to zara's review of a template: a field whose name, labels, description, enum
ids or synonyms hold one of these is refused. Each term is a run of whole-token prefixes after
`fold` (`test` matches `tests`, never `latest`). A tripwire, not a proof; the template's own
description is not checked (it may say what is *not* extracted), and vaccine names stay allowed."""

PROMPT_MARKUP: Final = frozenset("<>{}[]`#\\")
"""Characters prompt text may not hold (E7): it must never imitate PR 2's document boundary."""

RESERVED_FIELD_NAMES: Final = frozenset({"business_id", "business_type", "tenant_id"})
"""Context is injected, never answered (E8): no output model may hold these. Pydantic's own names
(`model_*`, `json`, `copy`, ...) are refused too, so a field never shadows a model attribute."""


class FieldType(StrEnum):
    """What a field holds; PR 2's checks depend on it (E2)."""

    TEXT = "text"
    """Free text, such as a name: equals the quote minus its label and punctuation."""
    DATE = "date"
    """A date token of the quote, parsed under the business's formats."""
    ENUM = "enum"
    """One of a closed list of ids, found in the quote as one of its synonyms."""
    IDENTIFIER = "identifier"
    """A number or code as written, a whole token, matching the field's pattern."""


class Rule(StrEnum):
    """The closed set of rules. `required` and `name` are per field; `after` is a cross rule."""

    REQUIRED = "required"
    """A missing value goes to review (H1), never `absent`."""
    NAME = "name"
    """Letters, spaces and `. ' - &` only (`NAME_PUNCTUATION`); text fields only."""
    AFTER = "after"
    """A date field is later than another date field (a cross rule)."""


FIELD_RULES: Final = frozenset({Rule.REQUIRED, Rule.NAME})
CROSS_RULES: Final = frozenset({Rule.AFTER})


class Category(StrEnum):
    """What kind of personal data a field is (E6). Closed, and with no clinical category."""

    IDENTITY = "identity"
    CONTACT = "contact"
    ADMINISTRATIVE = "administrative"


class DateOrder(StrEnum):
    DAY_FIRST = "day_first"
    MONTH_FIRST = "month_first"
    YEAR_FIRST = "year_first"
    NAMED_MONTH = "named_month"
    """The month is a word, so day and month cannot be swapped."""


@dataclass(frozen=True)
class DateFormatSpec:
    """How a format reads. `strptime` is for parsing only (E2: a date is never rendered); `%B`
    and `%b` are the C locale's English names (the process never calls `setlocale`)."""

    strptime: str
    order: DateOrder


class DateFormat(StrEnum):
    """The closed list of date formats a template may offer and a business may pick."""

    DD_MM_YYYY_SLASH = "DD/MM/YYYY"
    DD_MM_YYYY_DASH = "DD-MM-YYYY"
    DD_MM_YYYY_DOT = "DD.MM.YYYY"
    MM_DD_YYYY_SLASH = "MM/DD/YYYY"
    MM_DD_YYYY_DASH = "MM-DD-YYYY"
    YYYY_MM_DD = "YYYY-MM-DD"
    D_MONTH_YYYY = "D MMMM YYYY"
    D_MON_YYYY = "D MMM YYYY"
    MONTH_D_YYYY = "MMMM D, YYYY"


DATE_FORMATS: Final[Mapping[DateFormat, DateFormatSpec]] = MappingProxyType(
    {
        DateFormat.DD_MM_YYYY_SLASH: DateFormatSpec("%d/%m/%Y", DateOrder.DAY_FIRST),
        DateFormat.DD_MM_YYYY_DASH: DateFormatSpec("%d-%m-%Y", DateOrder.DAY_FIRST),
        DateFormat.DD_MM_YYYY_DOT: DateFormatSpec("%d.%m.%Y", DateOrder.DAY_FIRST),
        DateFormat.MM_DD_YYYY_SLASH: DateFormatSpec("%m/%d/%Y", DateOrder.MONTH_FIRST),
        DateFormat.MM_DD_YYYY_DASH: DateFormatSpec("%m-%d-%Y", DateOrder.MONTH_FIRST),
        DateFormat.YYYY_MM_DD: DateFormatSpec("%Y-%m-%d", DateOrder.YEAR_FIRST),
        DateFormat.D_MONTH_YYYY: DateFormatSpec("%d %B %Y", DateOrder.NAMED_MONTH),
        DateFormat.D_MON_YYYY: DateFormatSpec("%d %b %Y", DateOrder.NAMED_MONTH),
        DateFormat.MONTH_D_YYYY: DateFormatSpec("%B %d, %Y", DateOrder.NAMED_MONTH),
    }
)
"""Every `DateFormat`'s reading (a test checks none is missing)."""


def mixes_day_and_month_first(formats: Sequence[DateFormat]) -> bool:
    """Whether `formats` holds both a day-first and a month-first numeric format (E5): then
    `03/04/2026` is both 3 April and 4 March, so such a set is refused at startup."""
    orders = {DATE_FORMATS[name].order for name in formats}
    return {DateOrder.DAY_FIRST, DateOrder.MONTH_FIRST} <= orders


def fold(text: str) -> str:
    """E2's normalisation: NFKC, case-folded, whitespace runs collapsed to one space, trimmed.
    Used here to compare labels and synonyms; PR 2 compares quotes and values with it."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


# An identifier's pattern is configuration, so it is a small, safe subset of regular expressions:
# anchored, a sequence of literal characters or character classes, each with an optional bounded
# `{n}` or `{m,n}`; no groups, alternation, `*`, `+`, `?`, `.` wildcard or backreference. Bounded
# counts alone are not enough: several variable-width counts in a row that can match the same
# characters (`^A{0,40}A{0,40}A{0,40}...$`) still backtrack exponentially. So at most one count
# may be variable-width ({m,n} with m < n), and the longest match is at most `IDENTIFIER_MAX`:
# then a failing match tries at most one split per start, linear in the value's length. PR 2 also
# checks a value's length before running its pattern.
_CLASS_ITEM: Final = r"(?:[A-Za-z0-9](?:-[A-Za-z0-9])?|\\[-./]|[ /_])"
_ATOM: Final = rf"(?:\[{_CLASS_ITEM}+\]|[A-Za-z0-9 /_-]|\\[.-])"
_QUANTIFIER: Final = r"(?:\{(?P<low>[0-9]{1,2})(?:,(?P<high>[0-9]{1,2}))?\})?"
_PIECE: Final = re.compile(rf"{_ATOM}{_QUANTIFIER}")


def safe_identifier_pattern(pattern: str) -> bool:
    """Whether `pattern` is in the safe subset above: anchored, pieces of the subset with ordered
    counts, at most one variable-width count, a longest match of at most `IDENTIFIER_MAX`, and it
    compiles."""
    if len(pattern) < 3 or not pattern.startswith("^") or not pattern.endswith("$"):
        return False
    body = pattern[1:-1]
    position = longest = variable = 0
    while position < len(body):
        piece = _PIECE.match(body, position)
        if piece is None or piece.end() == position:
            return False
        low, high = piece.group("low"), piece.group("high")
        if low is None:
            longest += 1
        elif high is None:
            longest += int(low)
        elif int(low) > int(high):
            return False
        else:
            variable += int(low) < int(high)
            longest += int(high)
        position = piece.end()
    if variable > 1 or longest > IDENTIFIER_MAX:
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


class ExtractionConfigError(ValueError):
    """An extraction template or business file cannot be used. The message names the file, the
    location and the rule, never a value."""


class ConfigModel(BaseModel):
    """Base of the files' models: immutable, closed, strict, values kept out of errors. Strict so
    `"synthetic": "yes"` or `"version": "1"` is refused rather than coerced: the synthetic gate
    must be a JSON `true`."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


Snake = Annotated[str, Field(min_length=2, max_length=SNAKE_MAX, pattern=SNAKE_PATTERN)]


def _no_markup(text: str) -> str:
    if any(character in PROMPT_MARKUP for character in text):
        raise ValueError("prompt text holds no markup characters (E7)")
    return text


PromptText = Annotated[SingleLine, AfterValidator(_no_markup)]
"""Template text that reaches the prompt (E7): one line, no markup characters."""
Label = Annotated[PromptText, Field(max_length=LABEL_MAX)]
Description = Annotated[PromptText, Field(max_length=DESCRIPTION_MAX)]


def _reserved(name: str) -> bool:
    return name in RESERVED_FIELD_NAMES or name.startswith("model_") or hasattr(BaseModel, name)


def _names_clinical_term(text: str) -> bool:
    tokens = re.findall(r"[^\W_]+", fold(text))
    for term in CLINICAL_TERMS:
        for start in range(len(tokens) - len(term) + 1):
            window = tokens[start : start + len(term)]
            if all(token.startswith(prefix) for token, prefix in zip(window, term, strict=True)):
                return True
    return False


def _clinical_terms(index: int, spec: "FieldSpec") -> list[str]:
    """E6's tripwire for one field: locations only, never the term (`CLINICAL_TERMS`)."""
    texts = [("name", spec.name), ("description", spec.description)]
    texts += [(f"labels.{i}", label) for i, label in enumerate(spec.labels)]
    for value_index, value in enumerate(spec.values or ()):
        texts.append((f"values.{value_index}.id", value.id))
        texts += [
            (f"values.{value_index}.synonyms.{i}", synonym)
            for i, synonym in enumerate(value.synonyms)
        ]
    return [
        f"fields.{index}.{where} names a clinical term (E6)"
        for where, text in texts
        if _names_clinical_term(text)
    ]


def _unique_folded(texts: Sequence[str]) -> bool:
    folded = [fold(text) for text in texts]
    return len(set(folded)) == len(folded)


class EnumValue(ConfigModel):
    """One enum value: the id a value is stored as, and the words a document prints for it."""

    id: Snake
    synonyms: tuple[Label, ...] = Field(
        min_length=SYNONYMS_PER_VALUE[0], max_length=SYNONYMS_PER_VALUE[1]
    )


class FieldSpec(ConfigModel):
    """One field of a template."""

    name: Snake
    type: FieldType
    category: Category
    description: Description
    """Prompt text (E7): what the field is, for the model. Reviewed with the template (zara)."""
    labels: tuple[Label, ...] = Field(
        min_length=LABELS_PER_FIELD[0], max_length=LABELS_PER_FIELD[1]
    )
    """The field's printed names ("Date of birth", "DOB"): PR 2's `label_in_quote`."""
    rules: tuple[Rule, ...] = ()
    max_length: int | None = Field(default=None, ge=1)
    """Text and identifier fields only: at most `TEXT_MAX` or `IDENTIFIER_MAX`."""
    pattern: str | None = Field(default=None, max_length=IDENTIFIER_PATTERN_MAX)
    """Identifier fields only: an anchored pattern in the safe subset
    (`safe_identifier_pattern`)."""
    values: tuple[EnumValue, ...] | None = Field(
        default=None, min_length=1, max_length=ENUM_VALUES_MAX
    )
    """Enum fields only: the type-level list; a business may narrow it, never extend it."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        if _reserved(self.name):
            problems.append("name is reserved (context is injected, never extracted)")
        if not _unique_folded(self.labels):
            problems.append("each label is listed once")
        if len(set(self.rules)) != len(self.rules):
            problems.append("each rule is listed once")
        if not set(self.rules) <= FIELD_RULES:
            problems.append("rules holds only per-field rules (after is a cross rule)")
        if Rule.NAME in self.rules and self.type is not FieldType.TEXT:
            problems.append("the name rule applies to text fields only")
        problems += self._type_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _type_problems(self) -> list[str]:
        problems: list[str] = []
        limit = {FieldType.TEXT: TEXT_MAX, FieldType.IDENTIFIER: IDENTIFIER_MAX}.get(self.type)
        if limit is None and self.max_length is not None:
            problems.append("max_length is for text and identifier fields only")
        if limit is not None and (self.max_length is None or self.max_length > limit):
            problems.append(f"a {self.type.value} field has a max_length of at most {limit}")
        if self.type is FieldType.IDENTIFIER:
            if self.pattern is None or not safe_identifier_pattern(self.pattern):
                problems.append(
                    "an identifier field has an anchored pattern of literals and character"
                    " classes with bounded counts only"
                )
        elif self.pattern is not None:
            problems.append("pattern is for identifier fields only")
        if self.type is FieldType.ENUM:
            if self.values is None:
                problems.append("an enum field lists its values")
            else:
                if len({value.id for value in self.values}) != len(self.values):
                    problems.append("each enum id is listed once")
                if not _unique_folded([s for value in self.values for s in value.synonyms]):
                    problems.append("each synonym belongs to one enum value, once")
        elif self.values is not None:
            problems.append("values is for enum fields only")
        return problems

    @property
    def required(self) -> bool:
        return Rule.REQUIRED in self.rules

    @property
    def value_bound(self) -> int:
        """The longest value this field can validly hold, before `OUTPUT_SLACK`."""
        if self.type is FieldType.DATE:
            return DATE_VALUE_MAX
        if self.type is FieldType.ENUM:
            return max(len(text) for v in self.values or () for text in (v.id, *v.synonyms))
        return self.max_length or TEXT_MAX

    def value_ids(self) -> tuple[str, ...]:
        return tuple(value.id for value in self.values or ())


class CrossRule(ConfigModel):
    """A rule between two fields: `after` means `field`'s date is later than `other`'s."""

    rule: Rule
    field: Snake
    other: Snake


class Template(ConfigModel):
    """`templates/<business_type>/<kind>.json`: one document kind for one business type."""

    business_type: BusinessType
    kind: Snake
    version: int = Field(ge=1)
    title: Annotated[PromptText, Field(max_length=TITLE_MAX)]
    description: Description
    date_formats: tuple[DateFormat, ...] = Field(default=(), max_length=TEMPLATE_DATE_FORMATS_MAX)
    """The formats a business may choose from (both orders allowed here: a business picks)."""
    fields: tuple[FieldSpec, ...] = Field(min_length=1, max_length=MAX_FIELDS)
    always_review: tuple[Snake, ...] = ()
    cross_rules: tuple[CrossRule, ...] = Field(default=(), max_length=CROSS_RULES_MAX)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        names = [spec.name for spec in self.fields]
        if len(set(names)) != len(names):
            problems.append("each field name is listed once")
        problems += _overlapping_labels(self.fields)
        for index, spec in enumerate(self.fields):
            problems += _clinical_terms(index, spec)
        if len(set(self.always_review)) != len(self.always_review) or not set(
            self.always_review
        ) <= set(names):
            problems.append("always_review lists fields of this template, each once")
        # Decided by shesha, 2026-11-12 (M6 PR 1 review, zara): an identifier (a policy number, an
        # ID) is always reviewed, enforced here rather than left to each template's data. A
        # mistyped identifier is a silent error that is hard to spot later.
        for index, spec in enumerate(self.fields):
            if spec.type is FieldType.IDENTIFIER and spec.name not in self.always_review:
                problems.append(
                    f"fields.{index} is an identifier field and is listed in always_review"
                )
        if len(set(self.date_formats)) != len(self.date_formats):
            problems.append("each date format is listed once")
        has_dates = any(spec.type is FieldType.DATE for spec in self.fields)
        if has_dates != bool(self.date_formats):
            problems.append("date_formats is given exactly when the template has a date field")
        types = {spec.name: spec.type for spec in self.fields}
        for index, cross in enumerate(self.cross_rules):
            if (
                cross.rule not in CROSS_RULES
                or cross.field == cross.other
                or types.get(cross.field) is not FieldType.DATE
                or types.get(cross.other) is not FieldType.DATE
            ):
                problems.append(f"cross_rules.{index} is an after rule between two date fields")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def field(self, name: str) -> FieldSpec | None:
        return next((spec for spec in self.fields if spec.name == name), None)


def _overlapping_labels(fields: Sequence[FieldSpec]) -> list[str]:
    """A label inside another field's label (`Date` inside `Date of birth`) would make PR 2's
    label check name the wrong field, so templates refuse it. Indexes only, never the text."""
    problems: list[str] = []
    for index, spec in enumerate(fields):
        for other_index, other in enumerate(fields):
            if other_index == index:
                continue
            for label_index, label in enumerate(spec.labels):
                if any(fold(label) in fold(text) for text in other.labels):
                    problems.append(
                        f"fields.{index}.labels.{label_index} is inside a label of"
                        f" fields.{other_index}"
                    )
    return problems


class EnumChoice(ConfigModel):
    """A business's narrower list for one enum field: ids of the template's values."""

    field: Snake
    values: tuple[Snake, ...] = Field(min_length=1, max_length=ENUM_VALUES_MAX)


class KindSettings(ConfigModel):
    """One enabled kind in a business file. Present means enabled."""

    kind: Snake
    date_formats: tuple[DateFormat, ...] = Field(default=(), max_length=BUSINESS_DATE_FORMATS_MAX)
    enum_values: tuple[EnumChoice, ...] = Field(default=(), max_length=MAX_FIELDS)
    extra_always_review: tuple[Snake, ...] = Field(default=(), max_length=MAX_FIELDS)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        if len(set(self.date_formats)) != len(self.date_formats):
            problems.append("each date format is listed once")
        if mixes_day_and_month_first(self.date_formats):
            problems.append(
                "date_formats mixes day-first and month-first numeric dates (E5: a date such as"
                " 03/04/2026 would read two ways)"
            )
        if len({choice.field for choice in self.enum_values}) != len(self.enum_values):
            problems.append("each enum field is listed once")
        if any(len(set(choice.values)) != len(choice.values) for choice in self.enum_values):
            problems.append("each enum value is listed once")
        if len(set(self.extra_always_review)) != len(self.extra_always_review):
            problems.append("each extra always-review field is listed once")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class BusinessFile(ConfigModel):
    """`businesses/<business_id>.json`: one business's extraction settings."""

    business_id: BusinessId
    synthetic: bool
    business_type: BusinessType
    retention_days: int = Field(
        default=DEFAULT_RETENTION_DAYS, ge=RETENTION_DAYS[0], le=RETENTION_DAYS[1]
    )
    kinds: tuple[KindSettings, ...] = Field(default=(), max_length=KINDS_MAX)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len({settings.kind for settings in self.kinds}) != len(self.kinds):
            raise ValueError("each kind is enabled once")
        if self.kinds and not self.synthetic:
            raise ValueError(
                "a kind is enabled only with synthetic: true (ADR 0017 decision 6: synthetic"
                " documents only until Q4c and M7)"
            )
        return self


# ── Parsing ────────────────────────────────────────────────────────────────────────────────────


def parse_template(text: str, *, where: str) -> Template:
    """Parse and validate one template; `ExtractionConfigError` names `where`, fields and rule."""
    try:
        return Template.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise ExtractionConfigError(f"{where} is not a valid extraction template: {detail}")


def parse_business_file(text: str, *, where: str) -> BusinessFile:
    """Parse and validate one business file; the error names `where`, fields and rule."""
    try:
        return BusinessFile.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise ExtractionConfigError(f"{where} is not a valid extraction business file: {detail}")


# ── The output model (shape only) ──────────────────────────────────────────────────────────────

OUTPUT_CONFIG: Final = ConfigDict(
    frozen=True, extra="forbid", strict=True, hide_input_in_errors=True
)
VALUE_DESCRIPTION: Final = (
    "The field's value exactly as the document prints it, or null if the document does not give it."
)
QUOTE_DESCRIPTION: Final = (
    "The shortest verbatim text from one page of the document that holds the field's label and"
    " its value, or null if the value is null."
)
UNSURE_DESCRIPTION: Final = (
    "true if you are not sure this value belongs to this field; otherwise false."
)


def _pascal(name: str) -> str:
    """`x1_y` is `X1Y`, `x1y` is `X1y`, `a_1b` is `A_1b` and `a1b` is `A1b`: a capital or an
    underscore before a digit marks each part, so two distinct snake-case names never give the
    same class name (`str.title` would capitalise after a digit)."""
    parts = name.split("_")
    return (
        parts[0][:1].upper()
        + parts[0][1:]
        + "".join(
            f"_{part}" if part[:1].isdigit() else part[:1].upper() + part[1:] for part in parts[1:]
        )
    )


def compile_output_model(template: Template) -> type[BaseModel]:
    """The closed, shape-only model of one template's answer (module docstring).

    Deterministic: the same template gives the same JSON Schema text byte for byte, because
    everything is built from the template's tuples in file order (no set is iterated). The class
    names are `<Kind>` and `<Kind>_<field>` (field names are unique and snake case, so these are
    too); the gateway hashes the qualified name and the schema into its cache key.
    """
    prefix = _pascal(template.kind)
    fields: dict[str, Any] = {}
    for spec in template.fields:
        answer = create_model(
            f"{prefix}_{spec.name}",
            __config__=OUTPUT_CONFIG,
            __doc__=spec.description,
            __module__=__name__,
            value=(
                Annotated[str, Field(max_length=OUTPUT_SLACK * spec.value_bound)] | None,
                Field(description=VALUE_DESCRIPTION),
            ),
            quote=(
                Annotated[str, Field(max_length=OUTPUT_SLACK * QUOTE_MAX)] | None,
                Field(description=QUOTE_DESCRIPTION),
            ),
            unsure=(bool, Field(description=UNSURE_DESCRIPTION)),
        )
        fields[spec.name] = (answer, Field(description=spec.description))
    model: type[BaseModel] = create_model(
        prefix,
        __config__=OUTPUT_CONFIG,
        __doc__=f"{template.title}: {template.description}",
        __module__=__name__,
        **fields,
    )
    return model


# ── A business's view of one kind (E5) ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompiledTemplate:
    """A validated template, its output model and the SHA-256 of its file (PR 4's fingerprint)."""

    template: Template
    output_model: type[BaseModel]
    sha256: str


@dataclass(frozen=True)
class KindConfig:
    """What one business's extraction of one kind runs with: the template, tightened (E5)."""

    business_id: BusinessId
    business_type: BusinessType
    kind: str
    compiled: CompiledTemplate
    date_formats: tuple[DateFormat, ...]
    enum_values: Mapping[str, tuple[EnumValue, ...]]
    """Each enum field's allowed values, in template order: the business's subset, or all."""
    always_review: frozenset[str]
    """The template's always-review fields plus the business's extra ones."""
    retention_days: int

    @property
    def template(self) -> Template:
        return self.compiled.template

    @property
    def output_model(self) -> type[BaseModel]:
        return self.compiled.output_model


def resolve_kind(
    business: BusinessFile, index: int, compiled: CompiledTemplate | None, *, where: str
) -> KindConfig:
    """`business.kinds[index]` against its template; `ExtractionConfigError` for anything the
    template does not allow (E5) or a kind without a template for the business's type. Locations
    are indexes, never a value."""
    settings = business.kinds[index]
    at = f"{where}: kinds.{index}"
    if compiled is None:
        raise ExtractionConfigError(f"{at}.kind has no template for the business's type")
    template = compiled.template
    problems: list[str] = []
    if not set(settings.date_formats) <= set(template.date_formats):
        problems.append("date_formats are chosen from the template's (E5: tighten, never loosen)")
    has_dates = bool(template.date_formats)
    if has_dates and not settings.date_formats:
        problems.append("date_formats picks at least one of the template's formats")
    enum_values: dict[str, tuple[EnumValue, ...]] = {
        spec.name: spec.values or () for spec in template.fields if spec.type is FieldType.ENUM
    }
    for choice_index, choice in enumerate(settings.enum_values):
        spec = template.field(choice.field)
        if spec is None or spec.type is not FieldType.ENUM:
            problems.append(f"enum_values.{choice_index}.field is an enum field of the template")
            continue
        if not set(choice.values) <= set(spec.value_ids()):
            problems.append(
                f"enum_values.{choice_index}.values are the template's ids (E5: tighten, never"
                " loosen)"
            )
            continue
        enum_values[spec.name] = tuple(v for v in spec.values or () if v.id in choice.values)
    names = {spec.name for spec in template.fields}
    if not set(settings.extra_always_review) <= names:
        problems.append("extra_always_review lists fields of the template")
    if problems:
        raise ExtractionConfigError(f"{at}: {'; '.join(problems)}")
    return KindConfig(
        business_id=business.business_id,
        business_type=business.business_type,
        kind=settings.kind,
        compiled=compiled,
        date_formats=settings.date_formats,
        enum_values=MappingProxyType(enum_values),
        always_review=frozenset(template.always_review) | frozenset(settings.extra_always_review),
        retention_days=business.retention_days,
    )
