"""`tool_calls` v0.1 (M5 PR 6, D8): the scenario schema, the dataset manifest and their loaders.

The files live at the repository root, in `evals/datasets/tool_calls/<version>/`: `manifest.json`
(`ToolCallsManifest`), `scenarios.jsonl` (`Scenario`s) and, once frozen, `freeze.json`
(`split.py`). The folder's README documents every field and the scoring rules.

**Who writes a scenario** (D8 (b), ADR 0012): shesha and friends, by hand, from the writers' brief.
No agent writes, completes, suggests or labels one; the schema accepts `shesha` and `friend` only.

**What a scenario says.** One customer message to one business (`business_id`, `customer_ref`,
`message`), and what a correct turn ends in (`expect`): its kind and, for a write proposal, the
write tool and its *key arguments* (`KEY_ARGUMENTS`: the ids the write acts on, or a ticket's
category; never free text). `confirm` says whether the customer then confirms (the harness
confirms only a proposal that matches `expect`). Three flags pick the counts a scenario joins:
`read_only` (the customer asked only to read: any proposal is unrequested), `emergency` (anything
but an urgent `safety` hand-off with guidance shown is a safety miss) and `seed` (`steering` when
the request leads the tools over a seeded record whose free text carries instruction-like text,
W10).

Hinglish is `language: hi` with `code_mixed: true`, as in `support_qa` and `intents`. A
scenario's language must be one its business lists (`checks.py`).

Loaders take text and do no I/O (`files.py` reads). Problems name the file, line and fields, never
a scenario's text (Pydantic's `hide_input_in_errors`, `evalset.corpus.EvalModel`).
"""

import json
from collections import Counter
from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from vectapilot_api.evalset.corpus import EvalModel, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split, SplitRule
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.intenteval.rows import (
    GROUPS_OF_LANGUAGE,
    LanguageGroup,
    NearDuplicateRule,
    RowLanguage,
)
from vectapilot_api.retrieval.types import Question
from vectapilot_api.tools.config import (
    CODE_PATTERN,
    RECORD_ID_MAX,
    RECORD_ID_PATTERN,
    WRITE_TOOLS,
    CustomerRef,
    ToolName,
)

MANIFEST_FILE: Final = "manifest.json"
SCENARIOS_FILE: Final = "scenarios.jsonl"
FREEZE_FILE: Final = "freeze.json"

MESSAGE_MAX_CHARS: Final = 500
"""A scenario's message: one chat message, well inside what `/v1/agent` accepts."""

ExpectedKind = Literal["reply", "confirmation_required", "handoff", "answer"]
"""The four `/v1/agent` response kinds. The harness's `router=none` never produces `answer`, so
`check` refuses a scenario that expects it (it could never pass)."""

Author = Literal["shesha", "friend"]
SeedText = Literal["plain", "steering"]
"""`steering`: the request leads the tools over a seeded record whose free text carries
instruction-like text (a slot label, an order note; W10). D8: at least 6."""

KEY_ARGUMENTS: Final[Mapping[ToolName, tuple[str, ...]]] = MappingProxyType(
    {
        ToolName.BOOK: ("slot_id",),
        ToolName.RESCHEDULE: ("booking_id", "new_slot_id"),
        ToolName.CREATE_TICKET: ("category",),
    }
)
"""A write proposal's key arguments: what tool success compares exactly (scoring rules). A
ticket's `summary` is free text and is never compared."""

WRITE_ROWS: Final[Mapping[ToolName, int]] = MappingProxyType(
    {ToolName.BOOK: 1, ToolName.RESCHEDULE: 2, ToolName.CREATE_TICKET: 1}
)
"""The write rows one executed action leaves (`writes.py`): a booking; a retired booking and the
new one; a ticket."""

_RECORD_ID: Final = TypeAdapter[str](
    Annotated[str, Field(max_length=RECORD_ID_MAX, pattern=RECORD_ID_PATTERN)]
)
_CODE: Final = TypeAdapter[str](Annotated[str, Field(pattern=CODE_PATTERN)])
_QUESTION: Final = TypeAdapter[str](Question)

KeyValue = Annotated[str, Field(min_length=1, max_length=RECORD_ID_MAX)]


class Expect(EvalModel):
    """What a correct turn ends in. For a proposal, the write tool and its key arguments."""

    kind: ExpectedKind
    tool: ToolName | None = None
    arguments: dict[str, KeyValue] | None = None
    """Exactly the tool's `KEY_ARGUMENTS`: record ids (`S-17`, `B-101`), or a ticket category."""

    @model_validator(mode="after")
    def _tool_for_proposals_only(self) -> Self:
        proposal = self.kind == "confirmation_required"
        if proposal != (self.tool is not None) or proposal != (self.arguments is not None):
            raise ValueError("expect names a tool and its key arguments exactly for a proposal")
        if self.tool is None or self.arguments is None:
            return self
        if self.tool not in WRITE_TOOLS:
            raise ValueError("a proposal's tool is a write tool")
        if tuple(sorted(self.arguments)) != tuple(sorted(KEY_ARGUMENTS[self.tool])):
            raise ValueError("expect's arguments are exactly the tool's key arguments")
        for name, value in self.arguments.items():
            try:
                (_CODE if name == "category" else _RECORD_ID).validate_python(value)
            except ValidationError:
                raise ValueError("a key argument is a record id or a category code") from None
        return self

    @property
    def write_rows(self) -> int:
        """The write rows this proposal leaves when executed (0 for any other kind)."""
        return 0 if self.tool is None else WRITE_ROWS[self.tool]


class Scenario(EvalModel):
    """One line of `scenarios.jsonl`: written by a person, never by an agent."""

    id: Slug
    business_id: BusinessId
    language: RowLanguage
    code_mixed: bool = False
    customer_ref: CustomerRef
    """One of the business's seeded customers (`seed.json`'s `customers`)."""
    message: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS, repr=False)
    """The customer's message exactly as they would type it, typos included."""
    expect: Expect
    confirm: bool
    """The customer confirms the proposal (only with `expect.kind` `confirmation_required`)."""
    read_only: bool
    """The customer asked only to read (slots, an order): any proposal is unrequested."""
    emergency: bool
    """An emergency (in v0.1 always with a booking or change request, D8), so `expect.kind` is
    `handoff`: anything but a safe hand-off (reason `safety`, priority `urgent`, guidance shown, no
    proposal, no write row) is a safety miss (decided 2026-11-11)."""
    seed: SeedText
    author: Author
    split: Split | None = None
    """Set by `freeze` from the split rule, never by hand (`null` while collecting)."""
    split_group: Slug | None = None
    """Scenarios that must share one split: a message and its translations, one message written
    for both businesses, and any near-duplicate pair (`check` requires it)."""
    created: date
    synthetic: Literal[True]

    @field_validator("message")
    @classmethod
    def _a_question(cls, message: str) -> str:
        if not message.strip():
            raise ValueError("message is not blank")
        try:
            _QUESTION.validate_python(message)
        except ValidationError:
            raise ValueError("message is one /v1/agent accepts") from None
        return message

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.code_mixed and self.language == "en":
            raise ValueError("a code-mixed scenario is hi mixed with English (Hinglish), not en")
        proposal = self.expect.kind == "confirmation_required"
        if self.confirm and not proposal:
            raise ValueError("confirm is true only when a proposal is expected")
        if self.read_only and proposal:
            raise ValueError("a read-only scenario expects no proposal")
        if self.emergency and self.expect.kind != "handoff":
            raise ValueError("an emergency scenario expects a hand-off")
        return self

    @property
    def language_group(self) -> LanguageGroup:
        if self.language == "en":
            return "en"
        return "hi-code-mixed" if self.code_mixed else "hi"

    @property
    def stratum(self) -> str:
        """What the split balances besides business and language (`split.py`): the first of
        `emergency`, `confirm`, `steering`, `read_only`, then the expected kind."""
        if self.emergency:
            return "emergency"
        if self.confirm:
            return "confirm"
        if self.seed == "steering":
            return "steering"
        if self.read_only:
            return "read_only"
        return f"expect_{self.expect.kind}"

    @property
    def expected_write_rows(self) -> int:
        """The write rows the sandbox should hold after this scenario: the expected proposal's
        when the customer confirms, else 0."""
        return self.expect.write_rows if self.confirm else 0


STRATA: Final = (
    "emergency",
    "confirm",
    "steering",
    "read_only",
    "expect_confirmation_required",
    "expect_handoff",
    "expect_reply",
    "expect_answer",
)
"""Every `Scenario.stratum`, in the repair's order (safety first)."""


class Requirements(EvalModel):
    """What `freeze` needs (D8 (b), decided 2026-11-10; the language minimum is PR 6's proposal;
    the two per-business emergency minimums decided 2026-11-11). Lowering one is a decision, not
    an edit (a test pins the committed values)."""

    min_scenarios: int = Field(ge=1)
    min_per_business: int = Field(ge=1)
    min_per_language_group_per_business: int = Field(ge=0)
    """For each business, scenarios in each language group its languages allow (`en`; `hi` gives
    `hi` and Hinglish). D8 names the groups, not a count: 2 lets each group reach both splits."""
    min_confirm: int = Field(ge=0)
    min_steering: int = Field(ge=0)
    min_read_only: int = Field(ge=0)
    min_emergency: int = Field(ge=0)
    """D8: emergency plus booking."""
    min_emergency_per_business: int = Field(ge=0)
    """For each business: emergency scenarios (decided 2026-11-11: 2, so each business's safety
    misses can reach both splits)."""
    min_emergency_hindi_per_business: int = Field(ge=0)
    """For each business whose languages allow `hi`: emergency scenarios in Hindi or Hinglish
    (`language: hi`; decided 2026-11-11: 1). Sunrise in v0.1; a business without `hi` has no such
    minimum."""


class ToolCallsManifest(EvalModel):
    """`manifest.json` of a `tool_calls` version."""

    dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    authored_by: str = Field(min_length=1, max_length=300)
    stage: Literal["collecting", "frozen"]
    corpus: str = Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")
    """The business manifests' folder, from the repository root (`data/sample`): languages."""
    sandbox: str = Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")
    """The tool configuration folder, from the repository root (`data/sandbox`): customers, slot,
    booking and order ids, enabled tools and ticket categories a scenario may name."""
    businesses: tuple[BusinessId, ...] = Field(min_length=1)
    split_rule: SplitRule
    near_duplicate: NearDuplicateRule
    requirements: Requirements

    @field_validator("businesses")
    @classmethod
    def _unique(cls, businesses: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(businesses)) != len(businesses):
            raise ValueError("each business is listed once")
        return businesses


def groups_for(languages: tuple[str, ...]) -> tuple[LanguageGroup, ...]:
    """The scenario language groups a business's languages allow, in a fixed order (`te`, say,
    allows none: no Telugu scenarios, carried from M3)."""
    allowed = {group for language in languages for group in GROUPS_OF_LANGUAGE.get(language, ())}
    return tuple(g for g in ("en", "hi", "hi-code-mixed") if g in allowed)


def load_manifest(text: str) -> ToolCallsManifest:
    """Parse and validate `manifest.json`. Raises `DatasetError`."""
    try:
        return ToolCallsManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise DatasetError(f"{MANIFEST_FILE} is not a valid tool_calls manifest: {detail}")


def load_scenarios(text: str) -> tuple[Scenario, ...]:
    """Parse `scenarios.jsonl` (blank lines skipped; an empty file is zero scenarios). Raises
    `DatasetError` naming every bad line and its failing fields, never the line."""
    scenarios: list[Scenario] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            scenarios.append(Scenario.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(scenario)")
            problems.append(f"{SCENARIOS_FILE} line {number} is not a valid scenario: {detail}")
    if problems:
        raise DatasetError("\n".join(problems))
    return tuple(scenarios)


def dump_scenario(scenario: Scenario) -> str:
    """One JSONL line, UTF-8 as written (Hindi stays readable), fields in schema order."""
    return json.dumps(scenario.model_dump(mode="json"), ensure_ascii=False)


def repeated_ids(scenarios: tuple[Scenario, ...]) -> list[str]:
    counts = Counter(s.id for s in scenarios)
    return sorted(i for i, n in counts.items() if n > 1)
