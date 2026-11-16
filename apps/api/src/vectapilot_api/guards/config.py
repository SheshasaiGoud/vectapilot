"""The guard configuration folder (`VECTAPILOT_GUARD_CONFIG_DIR`, G4; M7 PR 1).

    <dir>/README.md                        optional, ignored
    <dir>/platform.json                    every business: terms, script ranges, fixed negatives
    <dir>/types/<business_type>.json       one business type's terms (every health type has one)
    <dir>/businesses/<business_id>.json    one business: added terms, guidance, `not_covered`

**Configuration names the terms and the guidance, code does not** (rule 9). Code holds closed
vocabularies only: term kinds (`types.TermKind`), categories (`types.TermCategory`), M4's intents
and business types, source sections and uncovered categories. Each file has three term lists:
- `red_flags`: each term maps to one of M4's two red-flag intents (`emergency` urgent,
  `professional_judgement` high: the priority comes from M4's route policy, never from here);
- `injection`: injection phrases;
- `output`: phrases H3 matches on model text (`success_claim`, `medical_advice`, `diagnosis`,
  `abuse`).

A term is a `phrase` (literal words) or `near` (word sets within a window of words, any order). It
cites its source: a line of business-context §3.5, §4.1, §4.2 or §4.3, a stated synonym rule
naming the term it is a synonym of, or (injection and output terms only) a stated authoring rule.
Every term is folded exactly as the text is (`normalize.fold`), so both sides of a match agree.

**Type resolution.** A business's type comes from its corpus manifest
(`<VECTAPILOT_CORPUS_DIR>/<business_id>/manifest.json`). Where a guarded route also knows a type
for it (`tools.json` or the extraction business file, passed as `served`), the business is health
if either says so, and it gets both types' terms (W11's stricter source). A business without a
file here is *unresolved*: it gets the platform's and every type's terms (fail towards recall),
counts as health for priorities, and has no guidance, so a hit is recorded `no_guidance`.

**Tighten, never loosen.** A business file can only add: it has no key that removes or edits a
term (`extra="forbid"`), and a term id it repeats from the platform or its type is refused, so it
cannot redefine one. A business's `not_covered` lists supported languages without a reviewed term
list (D3 (i)): each needs a script range in `platform.json`.

**Startup refuses** (G4): an unknown type, category or intent; a loosening file; a supported
language (the manifest's `languages`) that is neither covered nor `not_covered`; a type file silent
on `not_covered_categories`; a health type without a type file; a health business without guidance
(and a health business a guarded route serves without a file); a business file whose id differs
from its filename, that has no corpus manifest, or without `synthetic: true` (while Q4c is open);
and, through `selftest`, a term that fails G14. A language is covered when the business's terms
hold at least one injection and one `emergency` term in it, and for a health business one
`professional_judgement` term too [proposal].

**Errors name the file and the location, never a term.** Every model hides its input; our
validators raise fixed sentences; the files use lists, never mappings, so no text a file wrote
becomes a location; and an unknown key is reported as `(unknown key)`, because the key itself could
be a term. Term ids stay out of messages too: an id such as `chest_pain` says what it screens.
Locations are list positions (`types/veterinary.json: red_flags.3`).

The bounds below are proposals, configuration rather than measurements.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.db import InvalidBusinessId, parse_business_id
from vectapilot_api.evalset.corpus import (
    CORPUS_MANIFEST_FILE,
    CorpusError,
    CorpusManifest,
    LanguageTag,
    load_corpus_manifest,
)
from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.guards.normalize import ScriptRanges, fold
from vectapilot_api.guards.types import (
    OUTPUT_CATEGORIES,
    RED_FLAG_CATEGORIES,
    NearTerm,
    PhraseTerm,
    Term,
    TermCategory,
    TermKind,
    TermSet,
)
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType
from vectapilot_api.intents.taxonomy import Intent
from vectapilot_api.tools.config import SingleLine

README: Final = "README.md"
PLATFORM_FILE: Final = "platform.json"
TYPES: Final = "types"
BUSINESSES: Final = "businesses"
JSON_SUFFIX: Final = ".json"

# ── Bounds [proposal] ──────────────────────────────────────────────────────────────────────────
MAX_FILE_BYTES: Final = 256 * 1024
"""A bound, not a measurement: a term file is a few kilobytes."""
MAX_MANIFEST_BYTES: Final = 256 * 1024
"""As `agent.profiles`' bound for the same manifests."""
TERMS_MAX: Final = 1000
"""Terms per list in one file."""
TERM_ID_PATTERN: Final = r"^[a-z][a-z0-9_]{1,63}$"
PHRASE_MAX: Final = 100
PHRASE_WORDS_MAX: Final = 8
WORD_MAX: Final = 40
NEAR_SETS: Final = (2, 4)
SET_WORDS_MAX: Final = 12
WINDOW: Final = (2, 12)
"""A `near` term's window, in words."""
SOURCE_TEXT_MAX: Final = 200
GUIDANCE_LINE_MAX: Final = 300
GUIDANCE_LINES_MAX: Final = 10
"""As `ToolsConfig.emergency_guidance`, whose lines a test will require these to equal (PR 4)."""
NEGATIVES_MAX: Final = 200
SCRIPTS_MAX: Final = 50
RANGES_MAX: Final = 16
LAST_CODE_POINT: Final = 0x10FFFF

RED_FLAG_INTENTS: Final[frozenset[Intent]] = frozenset(
    Intent(category.value) for category in RED_FLAG_CATEGORIES
)
"""M4's intents a red-flag term may map to: `emergency` and `professional_judgement`."""
SECTIONS: Final = ("3.5", "4.1", "4.2", "4.3")
"""The business-context sections a term may cite."""

UNKNOWN_KEY: Final = "(unknown key)"


class GuardConfigError(ValueError):
    """The guard configuration cannot be used. The message names the file, the location and the
    rule, never a term or a term id."""


class NotCoveredCategory(StrEnum):
    """A red-flag category a type file says it does not cover (D6: no crisis terms by agents).
    Recorded so the red-team summary can say "not measured"; closed, so a typo is refused."""

    SELF_HARM = "self_harm"


# ── File models ────────────────────────────────────────────────────────────────────────────────


class GuardModel(BaseModel):
    """Base of the guard files' models: immutable, closed, strict and values kept out of errors.
    Strict, so `"synthetic": "yes"` or `"window": "4"` is refused rather than coerced."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


TermId = Annotated[str, Field(pattern=TERM_ID_PATTERN)]
Word = Annotated[SingleLine, Field(max_length=WORD_MAX)]
SourceText = Annotated[SingleLine, Field(max_length=SOURCE_TEXT_MAX)]
CodePoint = Annotated[str, Field(pattern=r"^U\+[0-9A-F]{4,6}$")]
"""`U+0C00`: four to six upper-case hex digits."""


def _folded_words(text: str) -> tuple[str, ...]:
    return tuple(fold(text).split())


class TermSource(GuardModel):
    """Where a term comes from: exactly one of a section line (`section` and `item`), a synonym
    rule (`synonym_of` and `rule`) or a stated authoring rule (`rule` alone)."""

    section: Literal["3.5", "4.1", "4.2", "4.3"] | None = None
    item: SourceText | None = None
    """The section's line or list item the term comes from (PR 4's test checks it is there)."""
    synonym_of: TermId | None = None
    rule: SourceText | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        cited = (self.section is not None, self.item is not None)
        synonym = self.synonym_of is not None
        if cited[0] != cited[1]:
            raise ValueError("a section source has both section and item")
        if synonym and self.rule is None:
            raise ValueError("a synonym source states its rule")
        if sum((cited[0], synonym, self.rule is not None and not synonym)) != 1:
            raise ValueError(
                "a source is exactly one of: section and item, synonym_of and rule, or rule"
            )
        return self

    @property
    def cites_a_line(self) -> bool:
        """A section line or a synonym rule: what a red-flag term must cite (G4)."""
        return self.section is not None or self.synonym_of is not None


class TermSpec(GuardModel):
    """One term as a file writes it. An injection term as is; `RedFlagSpec` and `OutputSpec` add
    the intent or the category."""

    id: TermId
    language: LanguageTag
    kind: TermKind
    text: Annotated[SingleLine, Field(max_length=PHRASE_MAX)] | None = None
    """A `phrase`'s words."""
    sets: tuple[tuple[Word, ...], ...] | None = None
    """A `near` term's word sets."""
    window: int | None = Field(default=None, ge=WINDOW[0], le=WINDOW[1])
    source: TermSource

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind is TermKind.PHRASE:
            _check_phrase(self.text, self.sets, self.window)
        else:
            _check_near(self.text, self.sets, self.window)
        return self

    def term_category(self) -> TermCategory:
        return TermCategory.INJECTION

    def build(self) -> Term:
        """The folded term H1 matches (the shape was checked when the file was read)."""
        if self.text is not None:
            return PhraseTerm(self.id, self.term_category(), _folded_words(self.text))
        # A near term: `_shape` checked that both are there (NearTerm refuses the empty fallbacks).
        return NearTerm(
            self.id,
            self.term_category(),
            tuple(tuple(fold(word) for word in words) for words in self.sets or ()),
            self.window or 0,
        )


def _check_phrase(text: str | None, sets: object, window: int | None) -> None:
    if text is None or sets is not None or window is not None:
        raise ValueError("a phrase term has text, and no sets or window")
    words = _folded_words(text)
    if not words:
        raise ValueError("a phrase term folds to at least one word")
    if len(words) > PHRASE_WORDS_MAX:
        raise ValueError(f"a phrase term folds to at most {PHRASE_WORDS_MAX} words")


def _check_near(text: str | None, sets: Sequence[Sequence[str]] | None, window: int | None) -> None:
    if text is not None or sets is None or window is None:
        raise ValueError("a near term has sets and a window, and no text")
    if not NEAR_SETS[0] <= len(sets) <= NEAR_SETS[1]:
        raise ValueError(f"a near term has {NEAR_SETS[0]} to {NEAR_SETS[1]} word sets")
    folded: list[str] = []
    for words in sets:
        if not 1 <= len(words) <= SET_WORDS_MAX:
            raise ValueError(f"a near term's word set has 1 to {SET_WORDS_MAX} words")
        for word in words:
            parts = _folded_words(word)
            if len(parts) != 1:
                raise ValueError("each word of a near term folds to exactly one word")
            folded.append(parts[0])
    if len(set(folded)) != len(folded):
        raise ValueError("a word belongs to one set of a near term, once")
    if window < len(sets):
        raise ValueError("a near term's window holds at least one word per set")


class RedFlagSpec(TermSpec):
    intent: Intent

    @model_validator(mode="after")
    def _red_flag(self) -> Self:
        if self.intent not in RED_FLAG_INTENTS:
            raise ValueError("a red-flag term's intent is emergency or professional_judgement")
        if not self.source.cites_a_line:
            raise ValueError("a red-flag term cites a section line or a synonym rule (G4)")
        return self

    def term_category(self) -> TermCategory:
        return TermCategory(self.intent.value)


class OutputSpec(TermSpec):
    category: TermCategory

    @model_validator(mode="after")
    def _output(self) -> Self:
        if self.category not in OUTPUT_CATEGORIES:
            raise ValueError(
                "an output term's category is success_claim, medical_advice, diagnosis or abuse"
            )
        return self

    def term_category(self) -> TermCategory:
        return self.category


class TermLists(GuardModel):
    """The three lists every file may hold."""

    red_flags: tuple[RedFlagSpec, ...] = Field(default=(), max_length=TERMS_MAX)
    injection: tuple[TermSpec, ...] = Field(default=(), max_length=TERMS_MAX)
    output: tuple[OutputSpec, ...] = Field(default=(), max_length=TERMS_MAX)

    def located(self, where: str) -> list[tuple[TermSpec, str]]:
        """Every term with its location (`<file>: <list>.<index>`), in file order."""
        lists: tuple[tuple[str, Sequence[TermSpec]], ...] = (
            ("red_flags", self.red_flags),
            ("injection", self.injection),
            ("output", self.output),
        )
        return [
            (spec, f"{where}: {name}.{index}")
            for name, specs in lists
            for index, spec in enumerate(specs)
        ]


class CodeRange(GuardModel):
    first: CodePoint
    last: CodePoint

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not int(self.first[2:], 16) <= int(self.last[2:], 16) <= LAST_CODE_POINT:
            raise ValueError("a range's first code point is at most its last, at most U+10FFFF")
        return self

    def bounds(self) -> tuple[int, int]:
        return int(self.first[2:], 16), int(self.last[2:], 16)


class ScriptSpec(GuardModel):
    """A language's script, for D3 (i)'s uncovered-language check."""

    language: LanguageTag
    ranges: tuple[CodeRange, ...] = Field(min_length=1, max_length=RANGES_MAX)


class NegativeSpec(GuardModel):
    """A fixed negative (G14): text that must not hit any configured term."""

    language: LanguageTag
    text: Annotated[SingleLine, Field(max_length=SOURCE_TEXT_MAX)]


class PlatformFile(TermLists):
    """`platform.json`: every business's terms, the script ranges and the fixed negatives."""

    scripts: tuple[ScriptSpec, ...] = Field(default=(), max_length=SCRIPTS_MAX)
    negatives: tuple[NegativeSpec, ...] = Field(default=(), max_length=NEGATIVES_MAX)

    @model_validator(mode="after")
    def _unique_scripts(self) -> Self:
        languages = [script.language for script in self.scripts]
        if len(set(languages)) != len(languages):
            raise ValueError("each language's script is listed once")
        return self

    @property
    def scripts_languages(self) -> frozenset[str]:
        return frozenset(script.language for script in self.scripts)


class TypeFile(TermLists):
    """`types/<business_type>.json`: one business type's terms."""

    business_type: BusinessType
    not_covered_categories: tuple[NotCoveredCategory, ...]
    """Required, never defaulted: a type file must say what it does not cover (G4)."""

    @model_validator(mode="after")
    def _unique_categories(self) -> Self:
        if len(set(self.not_covered_categories)) != len(self.not_covered_categories):
            raise ValueError("each not_covered category is listed once")
        return self


class BusinessFile(TermLists):
    """`businesses/<business_id>.json`: one business's added terms and guidance."""

    business_id: BusinessId
    synthetic: bool
    not_covered: tuple[LanguageTag, ...] = ()
    guidance: tuple[Annotated[SingleLine, Field(max_length=GUIDANCE_LINE_MAX)], ...] = Field(
        default=(), max_length=GUIDANCE_LINES_MAX
    )
    """The business's emergency guidance, shown line by line; each line verbatim from its own
    `emergency.md` (PR 4's test, as for `tools.json`). Required for a health business."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.synthetic:
            raise ValueError("a business file carries synthetic: true while Q4c is open")
        if len(set(self.not_covered)) != len(self.not_covered):
            raise ValueError("each not_covered language is listed once")
        return self


def describe_validation_error(error: ValidationError) -> str:
    """The failing locations, then the rule text of our own validators (fixed sentences). An
    unknown key is shown as `(unknown key)`: the key is text the file wrote, possibly a term."""
    locations: set[str] = set()
    for item in error.errors():
        parts = [str(part) for part in item["loc"]]
        if item["type"] == "extra_forbidden" and parts:
            parts[-1] = UNKNOWN_KEY
        locations.add(".".join(parts) or "(file)")
    rules = sorted(
        {
            item["msg"].removeprefix("Value error, ")
            for item in error.errors()
            if item["type"] == "value_error"
        }
    )
    return ", ".join(sorted(locations)) + (f" ({'; '.join(rules)})" if rules else "")


def _parse[M: GuardModel](model: type[M], text: str, where: str) -> M:
    try:
        return model.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise GuardConfigError(f"{where} is not a valid guard file: {detail}")


# ── What the guards run with ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfiguredTerm:
    """One term: as written (for the self-test's variants), folded, and where it is written."""

    spec: TermSpec
    term: Term
    where: str
    """`<file>: <list>.<index>`: what an error names instead of the term."""


@dataclass(frozen=True)
class BusinessGuards:
    """What one business's screens run with."""

    business_id: BusinessId | None
    """None for the unresolved set (a business without a file)."""
    business_type: BusinessType | None
    """From the corpus manifest; None when unresolved."""
    health: bool
    """Either source names a health type, or the type is unresolved (fail towards safety)."""
    red_flags: TermSet
    injection: TermSet
    output: TermSet
    guidance: tuple[str, ...]
    not_covered: frozenset[str]
    """Supported languages without a reviewed term list (D3 (i)); each has a script range."""

    @property
    def resolved(self) -> bool:
        return self.business_type is not None

    def all_terms(self) -> TermSet:
        return TermSet(self.red_flags.terms + self.injection.terms + self.output.terms)


@dataclass(frozen=True)
class GuardConfig:
    """The whole folder, validated and resolved."""

    rules_version: str
    """SHA-256 over every file's path and bytes: the `guard` record's `rules_version`."""
    scripts: ScriptRanges
    negatives: tuple[tuple[NegativeSpec, str], ...]
    """Each fixed negative with its location."""
    terms: tuple[ConfiguredTerm, ...]
    """Every term of every file, in file order (the self-test runs each one alone)."""
    not_covered_categories: Mapping[BusinessType, frozenset[NotCoveredCategory]]
    businesses: Mapping[BusinessId, BusinessGuards]
    unresolved: BusinessGuards

    def for_business(self, business_id: str) -> BusinessGuards:
        """The business's guards, or the unresolved set when it has no file (G4)."""
        return self.businesses.get(business_id, self.unresolved)


# ── Reading the folder ─────────────────────────────────────────────────────────────────────────


def _read(path: Path, where: str, limit: int = MAX_FILE_BYTES) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        raise GuardConfigError(f"{where} cannot be read") from None
    if len(data) > limit:
        raise GuardConfigError(f"{where} is larger than {limit} bytes")
    return data


def _text(data: bytes, where: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise GuardConfigError(f"{where} is not UTF-8") from None


def _entries(folder: Path) -> list[Path]:
    return sorted(entry for entry in folder.iterdir() if not entry.name.startswith("."))


def _json_stem(entry: Path, where: str) -> str:
    if not entry.is_file() or entry.suffix != JSON_SUFFIX:
        raise GuardConfigError(f"unexpected entry {entry.name!r} in {where}")
    return entry.stem


def _require_folder(folder: Path, name: str) -> None:
    if not folder.is_dir():
        raise GuardConfigError(f"the guard configuration folder has no {name} folder")


def _manifest(corpus_dir: Path, business_id: str, where: str) -> CorpusManifest:
    """The business's corpus manifest, validated (as `agent.profiles.corpus_business_type`)."""
    manifest_where = f"{business_id}/{CORPUS_MANIFEST_FILE}"
    path = corpus_dir / business_id / CORPUS_MANIFEST_FILE
    if not path.is_file():
        raise GuardConfigError(
            f"{where}: {manifest_where}: no corpus manifest in the corpus folder"
            " (VECTAPILOT_CORPUS_DIR)"
        )
    text = _text(_read(path, f"{where}: {manifest_where}", MAX_MANIFEST_BYTES), manifest_where)
    problem = None
    try:
        manifest = load_corpus_manifest(text, where=manifest_where)
    except CorpusError as error:
        problem = str(error)  # names the manifest, its fields and the rule, never a value
    if problem is not None:
        raise GuardConfigError(f"{where}: {problem}")
    if manifest.business_id != business_id:
        raise GuardConfigError(f"{where}: {manifest_where} names another business")
    return manifest


def _known_type(name: str) -> BusinessType | None:
    try:
        return BusinessType(name)
    except ValueError:
        return None


@dataclass(frozen=True)
class _Files:
    platform: PlatformFile
    types: Mapping[BusinessType, TypeFile]
    businesses: Mapping[BusinessId, BusinessFile]
    hashes: Mapping[str, str]
    """Relative path -> SHA-256 of the file's bytes."""


def _read_files(directory: Path) -> _Files:
    for entry in _entries(directory):
        if entry.name == README and entry.is_file():
            continue
        if entry.name == PLATFORM_FILE and entry.is_file():
            continue
        if entry.name not in (TYPES, BUSINESSES) or not entry.is_dir():
            raise GuardConfigError(
                f"unexpected entry {entry.name!r} in the guard configuration folder"
            )
    hashes: dict[str, str] = {}

    def parse[M: GuardModel](path: Path, where: str, model: type[M]) -> M:
        data = _read(path, where)
        hashes[where] = hashlib.sha256(data).hexdigest()
        return _parse(model, _text(data, where), where)

    if not (directory / PLATFORM_FILE).is_file():
        raise GuardConfigError(f"the guard configuration folder has no {PLATFORM_FILE}")
    platform = parse(directory / PLATFORM_FILE, PLATFORM_FILE, PlatformFile)

    _require_folder(directory / TYPES, TYPES)
    types: dict[BusinessType, TypeFile] = {}
    for entry in _entries(directory / TYPES):
        stem = _json_stem(entry, TYPES)
        business_type = _known_type(stem)
        if business_type is None:
            raise GuardConfigError(f"{TYPES}: file {entry.name!r} is not a known business type")
        where = f"{TYPES}/{entry.name}"
        type_file = parse(entry, where, TypeFile)
        if type_file.business_type is not business_type:
            raise GuardConfigError(f"{where} names a different business_type than its filename")
        types[business_type] = type_file
    if missing := sorted(HEALTH_TYPES - types.keys()):
        names = ", ".join(f"{TYPES}/{business_type}{JSON_SUFFIX}" for business_type in missing)
        raise GuardConfigError(f"every health type has a type file: missing {names}")

    _require_folder(directory / BUSINESSES, BUSINESSES)
    businesses: dict[BusinessId, BusinessFile] = {}
    for entry in _entries(directory / BUSINESSES):
        stem = _json_stem(entry, BUSINESSES)
        try:
            business_id = parse_business_id(stem)
        except InvalidBusinessId:
            raise GuardConfigError(
                f"{BUSINESSES}: file {entry.name!r} is not named after a business id"
            ) from None
        where = f"{BUSINESSES}/{entry.name}"
        business = parse(entry, where, BusinessFile)
        if business.business_id != business_id:
            raise GuardConfigError(f"{where} names a different business_id than its filename")
        businesses[business_id] = business
    if not businesses:
        raise GuardConfigError("the guard configuration folder holds no business")
    return _Files(platform, types, businesses, hashes)


def _rules_version(hashes: Mapping[str, str]) -> str:
    lines = "".join(f"{where}\0{sha256}\n" for where, sha256 in sorted(hashes.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _configured(located: Iterable[tuple[TermSpec, str]]) -> list[ConfiguredTerm]:
    return [ConfiguredTerm(spec, spec.build(), where) for spec, where in located]


def _check_ids(terms: Sequence[ConfiguredTerm], scope: str) -> None:
    """Ids are unique in one business's resolved set, and each synonym names a term in it."""
    first: dict[str, str] = {}
    for configured in terms:
        earlier = first.setdefault(configured.term.id, configured.where)
        if earlier != configured.where:
            raise GuardConfigError(
                f"{configured.where} repeats the term id of {earlier} ({scope}): a file cannot"
                " redefine a term"
            )
    for configured in terms:
        base = configured.spec.source.synonym_of
        if base is not None and (base == configured.term.id or base not in first):
            raise GuardConfigError(
                f"{configured.where}: its synonym_of names no other term of {scope}"
            )


def _split(terms: Sequence[ConfiguredTerm]) -> tuple[TermSet, TermSet, TermSet]:
    def of(categories: frozenset[TermCategory]) -> TermSet:
        return TermSet(tuple(t.term for t in terms if t.term.category in categories))

    return (
        of(RED_FLAG_CATEGORIES),
        of(frozenset({TermCategory.INJECTION})),
        of(OUTPUT_CATEGORIES),
    )


def _uncovered(terms: Sequence[ConfiguredTerm], language: str, health: bool) -> list[str]:
    """The categories a covered language needs and `terms` lack in it (module docstring)."""
    needed = [TermCategory.INJECTION, TermCategory.EMERGENCY]
    if health:
        needed.append(TermCategory.PROFESSIONAL_JUDGEMENT)
    present = {t.term.category for t in terms if t.spec.language == language}
    return [category.value for category in needed if category not in present]


def _scripts(platform: PlatformFile) -> ScriptRanges:
    return MappingProxyType(
        {script.language: tuple(r.bounds() for r in script.ranges) for script in platform.scripts}
    )


def _resolve_business(
    business_id: BusinessId,
    business: BusinessFile,
    files: _Files,
    base: Sequence[ConfiguredTerm],
    corpus_dir: Path,
    served_type: BusinessType | None,
) -> BusinessGuards:
    where = f"{BUSINESSES}/{business_id}{JSON_SUFFIX}"
    manifest = _manifest(corpus_dir, business_id, where)
    business_type = _known_type(manifest.business_type)
    if business_type is None:
        raise GuardConfigError(
            f"{where}: {business_id}/{CORPUS_MANIFEST_FILE}: its business_type is not a known type"
        )
    kinds = [business_type] if served_type is None else [business_type, served_type]
    health = any(kind in HEALTH_TYPES for kind in kinds)
    terms = list(base)
    for kind in dict.fromkeys(kinds):
        if kind in files.types:
            terms += _configured(files.types[kind].located(f"{TYPES}/{kind}{JSON_SUFFIX}"))
    terms += _configured(business.located(where))
    _check_ids(terms, f"{business_id}'s terms")
    if health and not business.guidance:
        raise GuardConfigError(f"{where}: a health business has guidance lines (G3, G4)")
    for language in business.not_covered:
        if language not in manifest.languages:
            raise GuardConfigError(
                f"{where}: not_covered lists {language!r}, which its corpus manifest does not"
                " support"
            )
        if language not in files.platform.scripts_languages:
            raise GuardConfigError(
                f"{where}: not_covered lists {language!r}, which has no script range in"
                f" {PLATFORM_FILE} (D3 (i) cannot detect it)"
            )
    for language in manifest.languages:
        missing = _uncovered(terms, language, health)
        if missing and language not in business.not_covered:
            raise GuardConfigError(
                f"{where}: supported language {language!r} has no {', '.join(missing)} term;"
                " add terms or list it under not_covered"
            )
    red_flags, injection, output = _split(terms)
    return BusinessGuards(
        business_id,
        business_type,
        health,
        red_flags,
        injection,
        output,
        business.guidance,
        frozenset(business.not_covered),
    )


def _resolve_unresolved(files: _Files, base: Sequence[ConfiguredTerm]) -> BusinessGuards:
    """Every type's terms and the platform's (fail towards recall); health for priorities; no
    guidance. Its `not_covered` is every scripted language the union does not cover [proposal]."""
    terms = list(base)
    for business_type, type_file in sorted(files.types.items()):
        terms += _configured(type_file.located(f"{TYPES}/{business_type}{JSON_SUFFIX}"))
    _check_ids(terms, "the platform's and every type's terms")
    not_covered = frozenset(
        language
        for language in files.platform.scripts_languages
        if _uncovered(terms, language, health=True)
    )
    red_flags, injection, output = _split(terms)
    return BusinessGuards(None, None, True, red_flags, injection, output, (), not_covered)


def load_guard_dir(
    directory: Path,
    corpus_dir: Path,
    *,
    served: Mapping[BusinessId, BusinessType] | None = None,
) -> GuardConfig:
    """The whole folder, read and validated (module docstring). `served` maps each business a
    guarded route serves (`tools.json`, extraction business files) to the type that route reads:
    a health one needs a file here, and either source's health type makes the business health.
    Raises `GuardConfigError`. The self-test (G14) runs on the result (`selftest`)."""
    if not directory.is_dir():
        raise GuardConfigError(
            "the guard configuration folder (VECTAPILOT_GUARD_CONFIG_DIR) is missing"
        )
    if not corpus_dir.is_dir():
        raise GuardConfigError("the corpus folder (VECTAPILOT_CORPUS_DIR) is missing")
    files = _read_files(directory)
    served = served or {}
    for business_id, served_type in sorted(served.items()):
        if business_id not in files.businesses and served_type in HEALTH_TYPES:
            raise GuardConfigError(
                f"{business_id}: a guarded health business has no"
                f" {BUSINESSES}/{business_id}{JSON_SUFFIX} (its guidance, G4)"
            )
    base = _configured(files.platform.located(PLATFORM_FILE))
    businesses = {
        business_id: _resolve_business(
            business_id, business, files, base, corpus_dir, served.get(business_id)
        )
        for business_id, business in files.businesses.items()
    }
    all_terms = list(base)
    for business_type, type_file in sorted(files.types.items()):
        all_terms += _configured(type_file.located(f"{TYPES}/{business_type}{JSON_SUFFIX}"))
    for business_id, business in files.businesses.items():
        all_terms += _configured(business.located(f"{BUSINESSES}/{business_id}{JSON_SUFFIX}"))
    negatives = tuple(
        (negative, f"{PLATFORM_FILE}: negatives.{index}")
        for index, negative in enumerate(files.platform.negatives)
    )
    return GuardConfig(
        rules_version=_rules_version(files.hashes),
        scripts=_scripts(files.platform),
        negatives=negatives,
        terms=tuple(all_terms),
        not_covered_categories=MappingProxyType(
            {
                business_type: frozenset(type_file.not_covered_categories)
                for business_type, type_file in sorted(files.types.items())
            }
        ),
        businesses=MappingProxyType(businesses),
        unresolved=_resolve_unresolved(files, base),
    )
