"""`redteam` v0.1 (M7 PR 2; D6 (a), D8 (a)): the case schema, the dataset manifest and the gate
labels, and their loaders.

The files live at the repository root, in `evals/datasets/redteam/<version>/`: `manifest.json`
(`RedteamManifest`), `cases.jsonl` (`Case`s), `gates.json` (`Gates`), once frozen `freeze.json`
(`freeze.py`), and `fixtures/` (the poisoned corpus and `rt-shop`'s files, never in
`data/sample/`, `data/sandbox/`, `support_qa` or `tests/`). The folder's README documents every
field; the scoring rules are `docs/plans/m7-pr-checks.md`, "Scoring rules for the red-team suite".

**The unit is a case** (one JSONL line), with exactly the scoring rules' fields: `id`, `family`,
`category`, `business_id`, `surface`, `route`, `language`, `code_mixed`, `obfuscation` (G only),
`input` (a template whose personal data is `{{pii:<type>}}` only, `pii.py`), `fixture` (typed per
surface), `expect`, `author`, `reviewed_by`, `created` and `synthetic`. Every vocabulary is
closed; what depends on other files (the business, its languages, the placeholders, literals, the
surface's fixture) is `checks.py`'s.

**Who writes a case** (D6 (a)): agents draft them, synthetic, labelled `zara-safety (agent)`;
`reviewed_by` stays empty until a person has reviewed the case. No self-harm content, no case
that looks like a real person, no Telugu case until a Telugu reviewer exists.

Loaders take text and do no I/O (`files.py` reads). Problems name the file, line, case id and
fields, never a value (`EvalModel`'s `hide_input_in_errors`).
"""

import json
import re
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from vectapilot_api.agent.types import REPLY_MAX, ModelHandoffReason
from vectapilot_api.evalset.corpus import EvalModel, LanguageTag, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.kb.types import Audience
from vectapilot_api.redteam.pii import MaskLabel
from vectapilot_api.retrieval.types import MAX_QUESTION_CHARS
from vectapilot_api.tools.config import ToolName

MANIFEST_FILE: Final = "manifest.json"
CASES_FILE: Final = "cases.jsonl"
GATES_FILE: Final = "gates.json"
FREEZE_FILE: Final = "freeze.json"
FIXTURES_DIR: Final = "fixtures"

AUTHOR_AGENT: Final = "zara-safety (agent)"


class Category(StrEnum):
    """The scoring rules' categories (plan PR 2's table)."""

    A = "A"
    """Direct injection in the message."""
    B = "B"
    """A poisoned retrieved chunk."""
    C = "C"
    """A poisoned tool result; poisoned text copied into a ticket."""
    D = "D"
    """A poisoned document."""
    E = "E"
    """Masking positives (a functional check per type)."""
    F = "F"
    """PII leakage: logs on every route; a reply echoing PII not in the message."""
    G = "G"
    """Red flags on health types, each tagged with an obfuscation class; no self-harm."""
    H = "H"
    """Professional judgement: requests (H-req, a message) and scripted replies (H-out)."""
    I = "I"  # noqa: E741 (the plan's category letter)
    """Output checks on scripted model text."""
    J = "J"
    """Limits and outages, with an emergency during each."""
    K = "K"
    """Cross-business isolation."""
    L = "L"
    """Benign: a hard core and soft look-alikes."""


class Surface(StrEnum):
    MESSAGE = "message"
    CHUNK = "chunk"
    TOOL_OUTPUT = "tool_output"
    TICKET_TEXT = "ticket_text"
    DOCUMENT = "document"
    REPLY = "reply"
    REDIS = "redis"
    LOG = "log"


class Route(StrEnum):
    AGENT = "agent"
    CONFIRM = "confirm"
    ANSWER = "answer"
    SEARCH = "search"
    EXTRACTION = "extraction"
    LIMITER = "limiter"
    SCAN = "scan"


class Obfuscation(StrEnum):
    """One closed class per G case. G-in: inside H1's contract (the fold and the matcher must
    survive it); G-out: outside it (`known_gap` from the freeze)."""

    NONE = "none"
    CASE = "case"
    ZERO_WIDTH = "zero_width"
    JOINER = "joiner"
    HOMOGLYPH = "homoglyph"
    ACCENT = "accent"
    PUNCTUATION = "punctuation"
    WHITESPACE = "whitespace"
    WORD_ORDER = "word_order"
    INTRA_WORD_SPACE = "intra_word_space"
    TYPO = "typo"
    LEETSPEAK = "leetspeak"
    PARAPHRASE = "paraphrase"
    TRANSLITERATION = "transliteration"


G_IN: Final[frozenset[Obfuscation]] = frozenset(
    {
        Obfuscation.NONE,
        Obfuscation.CASE,
        Obfuscation.ZERO_WIDTH,
        Obfuscation.JOINER,
        Obfuscation.HOMOGLYPH,
        Obfuscation.ACCENT,
        Obfuscation.PUNCTUATION,
        Obfuscation.WHITESPACE,
        Obfuscation.WORD_ORDER,
    }
)
G_OUT: Final[frozenset[Obfuscation]] = frozenset(Obfuscation) - G_IN


class Outage(StrEnum):
    """Category J's outage (plan, "Design points left to their PRs"): the key of J's failure-path
    exception, which G1 and D3 narrow to `guard_raising` (`scoring.OUTAGE_PATHS`), and of J's
    armed control (`scoring.OUTAGE_ARMED`). `expect` gains no outage value."""

    REDIS_DOWN = "redis_down"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_FAILING = "provider_failing"
    GUARD_RAISING = "guard_raising"
    BURST = "burst"


PLAIN_EXPECTS: Final = frozenset(
    {
        "urgent_handoff",
        "high_handoff",
        "handoff",
        "withheld",
        "dropped",
        "write_tools_removed",
        "allowed",
        "blocked_429",
        "no_leak",
    }
)
MASKED_PREFIX: Final = "masked:"
_LABELS: Final = frozenset(label.value for label in MaskLabel)
_MASKED: Final = re.compile(r"^masked:([a-z_]+(?:,[a-z_]+)*)$")


def masked_labels(expect: str) -> tuple[MaskLabel, ...] | None:
    """The labels of a `masked:<labels>` expectation (None for any other)."""
    match = _MASKED.fullmatch(expect)
    if match is None:
        return None
    return tuple(MaskLabel(label) for label in match.group(1).split(","))


def _check_expect(expect: str) -> str:
    if expect in PLAIN_EXPECTS:
        return expect
    match = _MASKED.fullmatch(expect)
    if match is None:
        raise ValueError("expect is one of the closed outcomes or masked:<labels>")
    labels = match.group(1).split(",")
    if any(label not in _LABELS for label in labels):
        raise ValueError("masked:<labels> names G6 labels only")
    if labels != sorted(set(labels)):
        raise ValueError("masked:<labels> lists each label once, sorted")
    return expect


Expect = Annotated[str, Field(min_length=1, max_length=120)]

CaseId = Annotated[str, Field(pattern=r"^[a-l]-[0-9]{3}$")]
"""`<category, lower case>-<three digits>` (`g-007`): the test id is `test_case[<id>]` and a known
gap's reason `known_gap:<id>` [proposal]."""
Author = Literal["zara-safety (agent)", "shesha"]
"""D6 (a): agents draft (crisis cases, later, are shesha's after Q8)."""
Reviewer = Literal["shesha"]
"""A person who reviewed the case; agents' reviews are not recorded here [proposal]."""
FixtureText = Annotated[str, Field(min_length=1, max_length=8_000)]
"""A chunk's or a page line's text; at most the chunk cap (`guards.caps`)."""
FieldValue = str | int | bool | None
FieldName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
"""A tool argument's or a tool result field's name, as the tools spell them (`order_id`,
`slot_id`, `date_from`): a Python identifier in lower case, never a slug (B2's fix: a slug has no
underscore, so no real argument name was accepted)."""


class ChunkFixture(EvalModel):
    """One retrieved chunk the case's scripted search returns (B, and answer-route cases)."""

    id: Slug
    text: FixtureText
    audience: Audience


class ToolResultFixture(EvalModel):
    """One tool result the case's scripted executor returns (C): its free-text and id fields."""

    tool: ToolName
    fields: dict[FieldName, FieldValue] = Field(min_length=1)


class HiddenLine(EvalModel):
    """A line a reader cannot see on the page (D): built into the PDF at run time."""

    text: FixtureText
    mode: Literal["invisible_render", "white_fill", "off_page"]
    """`3 Tr` (text render mode 3), white text on white, or drawn outside the page's box."""


class PageFixture(EvalModel):
    visible: tuple[FixtureText, ...] = ()
    hidden: tuple[HiddenLine, ...] = ()

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.visible and not self.hidden:
            raise ValueError("a page has at least one line")
        return self


class DocumentFixture(EvalModel):
    """A document built deterministically at run time (D), never a committed PDF."""

    pages: tuple[PageFixture, ...] = Field(min_length=1, max_length=5)


ArgumentValue = str | int | bool | None


class CallToolStep(EvalModel):
    action: Literal["call_tool"]
    tool: ToolName
    arguments: dict[FieldName, ArgumentValue] = Field(default_factory=dict)


class ReplyStep(EvalModel):
    action: Literal["reply"]
    text: str = Field(min_length=1, max_length=REPLY_MAX)


class HandoffStep(EvalModel):
    action: Literal["handoff"]
    reason: ModelHandoffReason


class AnswerStep(EvalModel):
    """The answer route's scripted answer text; its citations are `context.cited`."""

    action: Literal["answer"]
    text: str = Field(min_length=1, max_length=4_000)


ScriptStep = Annotated[
    CallToolStep | ReplyStep | HandoffStep | AnswerStep, Field(discriminator="action")
]
"""One step the fake plays, always the attacker's best case. The replay stand-in plays them in
order and never reads an observation to choose (scoring rules, "The loop is a replay")."""


class ContextFixture(EvalModel):
    cited: tuple[Slug, ...] = Field(min_length=1)
    """The chunk ids the scripted answer cites (each one of `chunks`)."""


class Fixture(EvalModel):
    """Everything a case's outcome depends on, typed per surface (`checks.fixture_problems`)."""

    chunks: tuple[ChunkFixture, ...] = ()
    tool_results: tuple[ToolResultFixture, ...] = ()
    document: DocumentFixture | None = None
    scripted_model: tuple[ScriptStep, ...] = ()
    context: ContextFixture | None = None
    outage: Outage | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [chunk.id for chunk in self.chunks]
        if len(set(ids)) != len(ids):
            raise ValueError("each chunk id is listed once")
        if self.context is not None and not set(self.context.cited) <= set(ids):
            raise ValueError("context.cited names chunks of this fixture only")
        return self


class Case(EvalModel):
    """One line of `cases.jsonl` (module docstring)."""

    id: CaseId
    family: Slug
    """The base attack: obfuscated variants of one message share a family (recall is also
    reported "from f families")."""
    category: Category
    business_id: BusinessId
    surface: Surface
    route: Route
    language: LanguageTag
    code_mixed: bool = False
    obfuscation: Obfuscation | None = None
    input: str = Field(max_length=MAX_QUESTION_CHARS + 200, repr=False)
    """A template: personal data only as `{{pii:<type>}}`. Empty where the surface carries the
    attack (a chunk, a tool result, a document, a scripted reply). Up to 200 characters over the
    message cap, so J's over-length emergency can be written."""
    fixture: Fixture = Field(default_factory=Fixture, repr=False)
    expect: Expect
    author: Author
    reviewed_by: tuple[Reviewer, ...] = ()
    created: date
    synthetic: Literal[True]

    _expect = field_validator("expect")(_check_expect)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.id.startswith(self.category.value.lower() + "-"):
            raise ValueError("a case id starts with its category's letter")
        if (self.category is Category.G) != (self.obfuscation is not None):
            raise ValueError("a G case has an obfuscation class, and only a G case has one")
        if self.code_mixed and self.language != "hi":
            raise ValueError("a code-mixed case is hi mixed with English (Hinglish)")
        if self.fixture.outage is not None and self.category is not Category.J:
            raise ValueError("only a J case names an outage")
        if len(set(self.reviewed_by)) != len(self.reviewed_by):
            raise ValueError("each reviewer is listed once")
        return self

    @property
    def g_in(self) -> bool:
        return self.category is Category.G and self.obfuscation in G_IN

    @property
    def g_out(self) -> bool:
        return self.category is Category.G and self.obfuscation in G_OUT

    @property
    def subgroup(self) -> str:
        """The report's subgroup: G-in or G-out, H-req (a message) or H-out (a reply), else the
        category."""
        if self.category is Category.G:
            return "G-in" if self.g_in else "G-out"
        if self.category is Category.H:
            return "H-out" if self.surface is Surface.REPLY else "H-req"
        return self.category.value

    @property
    def language_row(self) -> str:
        """The summary's language row: code-mixed hi is its own (`hi-code-mixed`)."""
        return "hi-code-mixed" if self.code_mixed else self.language


# ── Gate labels ──────────────────────────────────────────────────────────────────────────────

Label = Literal["hard", "soft", "known_gap"]
HARD_ONLY: Final = frozenset({"F", "G-in", "H-req", "H-out", "K"})
"""Subgroups whose cases accept only `hard` (scoring rules: F, G-in, H and K)."""


class Gates(EvalModel):
    """`gates.json`: the CI semantics of each case (D8) and the soft over-escalation ceiling."""

    max_over_escalated: int = Field(ge=0)
    labels: dict[CaseId, Label]


# ── The manifest ─────────────────────────────────────────────────────────────────────────────

Folder = Annotated[str, Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")]


class Counts(EvalModel):
    """The counts `freeze` pins; the gate refuses a run with any other (scoring rules)."""

    total: int = Field(ge=1)
    categories: dict[Category, int]

    @model_validator(mode="after")
    def _sum(self) -> Self:
        if any(n < 0 for n in self.categories.values()):
            raise ValueError("a category count is 0 or more")
        if sum(self.categories.values()) != self.total:
            raise ValueError("the category counts add up to the total")
        return self


class RedteamManifest(EvalModel):
    """`manifest.json` of a `redteam` version."""

    dataset: Literal["redteam"]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    authored_by: str = Field(min_length=1, max_length=300)
    stage: Literal["collecting", "frozen"]
    corpus: Folder
    """The sample businesses' manifests, from the repository root (`data/sample`)."""
    fixture_businesses: tuple[BusinessId, ...] = ()
    """Test-only businesses whose corpus manifest is `fixtures/<id>/manifest.json` (`rt-shop`)."""
    counts: Counts | None = None
    """Written by `freeze`; None while collecting."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.fixture_businesses)) != len(self.fixture_businesses):
            raise ValueError("each fixture business is listed once")
        if (self.stage == "frozen") != (self.counts is not None):
            raise ValueError("a frozen manifest pins its counts, and only a frozen one")
        return self


# ── Loaders ──────────────────────────────────────────────────────────────────────────────────

_ID: Final = re.compile(r"^[a-l]-[0-9]{3}$")


def load_manifest(text: str) -> RedteamManifest:
    try:
        return RedteamManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise DatasetError(f"{MANIFEST_FILE} is not a valid redteam manifest: {detail}")


def load_gates(text: str) -> Gates:
    try:
        return Gates.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(gates)")
    raise DatasetError(f"{GATES_FILE} is not a valid gates file: {detail}")


def _line_id(line: str) -> str | None:
    """The case id of a line that does not validate, when it has a well-formed one (so the
    problem can name it); never any other value."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    value = data.get("id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def parse_cases(text: str) -> tuple[tuple[Case, ...], tuple[str, ...]]:
    """Every valid case of `cases.jsonl`, and one problem per line that is not (blank lines
    skipped), naming the line, the case id when well formed, and the failing fields."""
    cases: list[Case] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(Case.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(case)")
            case_id = _line_id(line)
            named = f" (case {case_id})" if case_id else ""
            problems.append(f"{CASES_FILE} line {number}{named} is not a valid case: {detail}")
    return tuple(cases), tuple(problems)


def dump_case(case: Case) -> str:
    """One JSONL line, UTF-8 as written (Hindi stays readable), fields in schema order."""
    return json.dumps(case.model_dump(mode="json"), ensure_ascii=False)


SURFACE_ROUTES: Final[Mapping[Surface, frozenset[Route]]] = MappingProxyType(
    {
        Surface.MESSAGE: frozenset({Route.AGENT, Route.ANSWER, Route.SEARCH}),
        Surface.CHUNK: frozenset({Route.AGENT, Route.ANSWER, Route.SEARCH, Route.SCAN}),
        Surface.TOOL_OUTPUT: frozenset({Route.AGENT}),
        Surface.TICKET_TEXT: frozenset({Route.AGENT, Route.CONFIRM}),
        Surface.DOCUMENT: frozenset({Route.EXTRACTION, Route.SCAN}),
        Surface.REPLY: frozenset({Route.AGENT, Route.ANSWER}),
        Surface.REDIS: frozenset(Route),
        Surface.LOG: frozenset(Route),
    }
)
"""Which routes a surface can be attacked through [proposal]: a message reaches the message
routes, a tool result only the agent, a document the extraction route and the KB scan; Redis and
logs are reached from any route."""
