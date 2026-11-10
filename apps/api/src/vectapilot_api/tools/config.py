"""A business's tool configuration: `<VECTAPILOT_TOOLS_CONFIG_DIR>/<business_id>/tools.json`.

ADR 0016 decision 1 (rule 9): which tools a business enables, its closed service list, its
business type, its emergency guidance, its timezone and its sandbox date all come from this file,
never from code. It is not the corpus manifest (`data/sample/<business_id>/manifest.json`), which
is closed and hashed into the eval's freeze pins; a test checks that the two agree on the
business type.

- **The tool names are a platform set** (`ToolName`): five tools, two reads and three writes. A
  business enables a subset; an unknown name is refused, never ignored.
- **Services are a closed enum per business.** `check_slots`' `service` argument (PR 2) is one of
  these ids, so the model cannot invent a service, and a summary shows the configured name.
- **Ticket categories are a closed enum per business** (M5 PR 2, [proposal]): `create_ticket`'s
  `category` is one of these ids, and its summary shows the configured name. A business that
  enables `create_ticket` lists at least one.
- **Emergency guidance is copied, never written here** (W11): each line is a verbatim sentence of
  the business's own synthetic `emergency.md` (a test checks it), so no safety text is invented.
  For a health business the agent route shows it whenever it fails closed or a loop hands off for
  safety (PR 4); the model never writes it.
- **The sandbox date** is the sandbox's "today": it decides which seeded slots are in the future
  (W5), and the agent's prompt states it (PR 4). It is not the database clock, which sets
  `pending_actions.expires_at` (W6).

The patterns below are the database's CHECKs (migration 0006 writes them out; a test compares).
Every error names the file, the fields and the rule, never a value (`hide_input_in_errors`).
"""

import re
import unicodedata
import zoneinfo
from datetime import date
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from vectapilot_api.gateway.types import BusinessId
from vectapilot_api.intents.policy import HEALTH_TYPES, BusinessType

TOOLS_FILE: Final = "tools.json"
SEED_FILE: Final = "seed.json"

RECORD_ID_PATTERN: Final = r"^[A-Z0-9]+(-[A-Z0-9]+)*$"
RECORD_ID_MAX: Final = 32
"""A slot, booking or order id (`S-17`, `B-101`, `1043`): upper-case letters, digits and single
hyphens. Chosen by the seed, unique per business (migration 0006)."""
CUSTOMER_REF_PATTERN: Final = r"^[a-z0-9]+(-[a-z0-9]+)*$"
CUSTOMER_REF_LENGTH: Final = (3, 40)
"""A customer reference (`cust-001`): a slug, like a business id. Client-asserted in M5 (ADR 0016
decision 9), so it is only ever an identifier, never a name."""
CODE_PATTERN: Final = r"^[a-z][a-z0-9_]{1,39}$"
"""A service id, a ticket category, an audit outcome or a failure reason."""
TIMEZONE_PATTERN: Final = r"^[A-Za-z]+(_[A-Za-z]+)*(/[A-Za-z0-9]+([_+-][A-Za-z0-9]+)*)*$"
"""An IANA zone name (`Asia/Kolkata`); checked again by `zoneinfo`. No dots, so no path."""

RecordId = Annotated[str, Field(max_length=RECORD_ID_MAX, pattern=RECORD_ID_PATTERN)]
CustomerRef = Annotated[
    str,
    Field(
        min_length=CUSTOMER_REF_LENGTH[0],
        max_length=CUSTOMER_REF_LENGTH[1],
        pattern=CUSTOMER_REF_PATTERN,
    ),
]
Code = Annotated[str, Field(pattern=CODE_PATTERN)]
TICKET_SUMMARY_MAX: Final = 500
"""A ticket's summary (`sandbox_tickets.summary`); `create_ticket`'s input (PR 2) uses it."""

_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
"""Control characters, line and paragraph separators: a configured line stays one line."""
_INVISIBLE_CATEGORIES: Final = frozenset({"Cf", "Co", "Cn", "Cs"})
"""Format characters (zero-width spaces, bidi overrides, tag characters), private-use,
unassigned and surrogate code points: text a person cannot see on a confirmation screen but a
model reading the stored text receives as tokens ("ASCII smuggling"). Refused, so what the customer
sees is what is stored (W3)."""
_JOINERS: Final = frozenset({"\u200c", "\u200d"})
"""ZWNJ and ZWJ (both `Cf`): Devanagari and Telugu need them to choose a conjunct's shape, and the
pilot businesses write Hindi and Telugu, so they stay allowed."""
_VARIATION_SUPPLEMENT: Final = range(0xE0100, 0xE01F0)
"""Variation selectors 17-256 (`Mn`, so not caught by category): no script we serve needs them,
and a run of them carries hidden bytes as tag characters do."""


def _invisible(character: str) -> bool:
    if character in _JOINERS:
        return False
    return (
        unicodedata.category(character) in _INVISIBLE_CATEGORIES
        or ord(character) in _VARIATION_SUPPLEMENT
    )


class ToolName(StrEnum):
    """The platform's tools (ADR 0016 decision 1). A business enables a subset."""

    BOOK = "book"
    CHECK_SLOTS = "check_slots"
    CREATE_TICKET = "create_ticket"
    ORDER_STATUS = "order_status"
    RESCHEDULE = "reschedule"


READ_TOOLS: Final[frozenset[ToolName]] = frozenset({ToolName.CHECK_SLOTS, ToolName.ORDER_STATUS})
WRITE_TOOLS: Final[frozenset[ToolName]] = frozenset(
    {ToolName.BOOK, ToolName.CREATE_TICKET, ToolName.RESCHEDULE}
)
CALENDAR_TOOLS: Final[frozenset[ToolName]] = frozenset(
    {ToolName.BOOK, ToolName.CHECK_SLOTS, ToolName.RESCHEDULE}
)
"""Tools that name a service or a slot: a business enabling one needs at least one service."""


class ToolsConfigError(ValueError):
    """A tool configuration or sandbox seed cannot be used. The message names the folder, file,
    fields and rule, never a value."""


def describe_validation_error(error: ValidationError) -> str:
    """The failing fields, then the rule text our own validators raise (fixed sentences, never a
    value), as `evalset.corpus` does."""
    errors = error.errors()
    fields = sorted({".".join(map(str, e["loc"])) or "(file)" for e in errors})
    rules = sorted(
        {e["msg"].removeprefix("Value error, ") for e in errors if e["type"] == "value_error"}
    )
    return ", ".join(fields) + (f" ({'; '.join(rules)})" if rules else "")


class ToolsModel(BaseModel):
    """Base of the tool files' models: immutable, closed, values kept out of errors."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def single_line(value: str) -> str:
    """`value` if it is one trimmed line without control or invisible characters; else a
    ValueError. The message stays the one fixed sentence, so no value is echoed."""
    if _CONTROL.search(value) or value != value.strip() or any(map(_invisible, value)):
        raise ValueError("text fields are one trimmed line without control or invisible characters")
    return value


SingleLine = Annotated[str, Field(min_length=1), AfterValidator(single_line)]


class Service(ToolsModel):
    """One bookable service: the id the tools use and the name a summary shows."""

    id: Code
    name: Annotated[SingleLine, Field(max_length=100)]


class TicketCategory(ToolsModel):
    """One ticket category: the id `create_ticket` takes and the name its summary shows."""

    id: Code
    name: Annotated[SingleLine, Field(max_length=100)]


class ToolsConfig(ToolsModel):
    """`tools.json`: one business's tools (ADR 0016 decision 1)."""

    business_id: BusinessId
    synthetic: bool
    business_type: BusinessType
    timezone: str = Field(max_length=64, pattern=TIMEZONE_PATTERN)
    sandbox_date: date
    enabled_tools: tuple[ToolName, ...]
    services: tuple[Service, ...]
    ticket_categories: tuple[TicketCategory, ...] = Field(default=(), max_length=20)
    emergency_guidance: tuple[Annotated[SingleLine, Field(max_length=300)], ...] = Field(
        min_length=1, max_length=10
    )
    """Lines shown in order, each verbatim from the business's own emergency guidance: whole
    sentences or a heading, never a fragment (a cut can drop a "Do not"). Shown as separate lines,
    never joined into one (a heading such as "Outside clinic hours" belongs on its own line)."""

    @field_validator("synthetic")
    @classmethod
    def _must_be_synthetic(cls, synthetic: bool) -> bool:
        if not synthetic:
            raise ValueError("the sandbox is synthetic only in M5 (ADR 0016 decision 6)")
        return synthetic

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, name: str) -> str:
        try:
            zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be an IANA zone this system knows") from None
        return name

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.enabled_tools)) != len(self.enabled_tools):
            raise ValueError("each tool is enabled once")
        if len({service.id for service in self.services}) != len(self.services):
            raise ValueError("each service id is listed once")
        if CALENDAR_TOOLS & set(self.enabled_tools) and not self.services:
            raise ValueError("a business that enables a calendar tool lists at least one service")
        if len({category.id for category in self.ticket_categories}) != len(self.ticket_categories):
            raise ValueError("each ticket category id is listed once")
        if ToolName.CREATE_TICKET in self.enabled_tools and not self.ticket_categories:
            raise ValueError("a business that enables create_ticket lists at least one category")
        return self

    @property
    def zone(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    @property
    def health_business(self) -> bool:
        """Whether W11's safety step applies (business-context §4)."""
        return self.business_type in HEALTH_TYPES

    @property
    def service_ids(self) -> frozenset[str]:
        return frozenset(service.id for service in self.services)

    def service_name(self, service_id: str) -> str | None:
        """The configured name of `service_id`, or None when the business has no such service."""
        return next((s.name for s in self.services if s.id == service_id), None)

    def category_name(self, category_id: str) -> str | None:
        """The configured name of ticket category `category_id`, or None."""
        return next((c.name for c in self.ticket_categories if c.id == category_id), None)


def parse_tools_config(text: str, *, where: str) -> ToolsConfig:
    """Parse and validate one `tools.json`; `ToolsConfigError` names `where`, fields and rule."""
    try:
        return ToolsConfig.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error)
    raise ToolsConfigError(f"{where} is not a valid tool configuration: {detail}")
