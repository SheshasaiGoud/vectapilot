"""The gateway_smoke dataset: its case and manifest types, and pure loaders (PR 8).

The files live at the repository root, in `evals/datasets/gateway_smoke/<version>/`:
`manifest.json` (one JSON object) and `cases.jsonl` (one case per line). The schema is documented
in that folder's README.md. The loaders here take the files' *text* and do no I/O; the measurement
CLI reads the files. Error messages name the line and the rule, never a prompt.
"""

from collections.abc import Collection, Mapping
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from vectapilot_api.gateway.types import BusinessId, Slug

Language = Literal["en", "hi", "te"]
"""Languages of v0.1. A code-mixed case (Hinglish, Tenglish) has `hi` or `te` plus `code_mixed`."""

CaseKind = Literal["plain", "structured"]

DatasetVersion = Annotated[str, Field(pattern=r"^v[0-9]+\.[0-9]+$")]

CASES_FILE: Final = "cases.jsonl"
MANIFEST_FILE: Final = "manifest.json"


class DatasetError(ValueError):
    """The dataset files cannot be used: not JSON, not a valid case or manifest, or inconsistent."""


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class SmokeCase(_Closed):
    """One prompt of the gateway smoke run: what the CLI sends, and how the summary groups it."""

    id: Slug
    """Unique in the dataset; never reused for a different prompt in a later version."""
    business_id: BusinessId
    alias: Slug
    """A catalog alias (`small`, `large`)."""
    language: Language
    code_mixed: bool = False
    """Hindi or Telugu mixed with English, in Latin script (Hinglish, Tenglish)."""
    kind: CaseKind
    schema_name: str | None = None
    """Structured cases only: a key of `measure.schemas.SCHEMAS`."""
    forced_repair: bool = False
    """The one structured case whose first answer is expected to fail validation (see schemas)."""
    prompt: str = Field(min_length=1, max_length=2000, repr=False)
    """Sent as the only user message, with no system prompt (a structured call's system prompt is
    the gateway's schema instruction)."""
    fake_reply: str = Field(min_length=1, repr=False)
    """The fake provider's answer, for dry runs only. Valid JSON for the schema on a structured
    case; on the forced-repair case, an answer that fails the hidden rule."""
    fake_repair_reply: str | None = Field(default=None, min_length=1, repr=False)
    """The forced-repair case only: the fake's valid answer to the repair request."""

    @model_validator(mode="after")
    def _kind_rules(self) -> Self:
        if (self.kind == "structured") != (self.schema_name is not None):
            raise ValueError("schema_name is required for a structured case and only for one")
        if self.forced_repair and self.kind != "structured":
            raise ValueError("only a structured case can be a forced repair")
        if self.forced_repair != (self.fake_repair_reply is not None):
            raise ValueError("fake_repair_reply is required for a forced repair and only for one")
        if self.code_mixed and self.language == "en":
            raise ValueError("a code-mixed case is hi or te mixed with English, not en")
        return self

    @property
    def language_group(self) -> str:
        """The per-language group of the summary: `hi`, or `hi-code-mixed` for Hinglish."""
        return f"{self.language}-code-mixed" if self.code_mixed else self.language


class DatasetManifest(_Closed):
    """`manifest.json`: what the dataset is, who wrote it, and which businesses it covers."""

    dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    """Every case is invented (business-context §6: synthetic until a real pilot)."""
    authored_by: str = Field(min_length=1)
    review: Literal["pending", "done"]
    """The native-speaker review of the hi/te and code-mixed prompts (shesha, before a live run)."""
    businesses: dict[BusinessId, tuple[Language, ...]] = Field(min_length=1)
    """Each business's supported languages, from docs/business-context.md §7."""


def load_manifest(text: str) -> DatasetManifest:
    """Parse and validate `manifest.json`. Raises `DatasetError`."""
    try:
        return DatasetManifest.model_validate_json(text)
    except ValidationError:
        pass  # raised below, outside the `except`, so nothing is chained
    raise DatasetError(f"{MANIFEST_FILE} is not a valid manifest")


def load_cases(text: str) -> tuple[SmokeCase, ...]:
    """Parse and validate `cases.jsonl`: one JSON object per line, blank lines skipped, ids unique.

    Raises `DatasetError` naming the line number and the rule, never the line.
    """
    cases: list[SmokeCase] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            cases.append(_parse_case(line, number))
    if not cases:
        raise DatasetError(f"{CASES_FILE} has no cases")
    ids = [case.id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise DatasetError(f"case ids must be unique; repeated: {', '.join(duplicates)}")
    return tuple(cases)


def _parse_case(line: str, number: int) -> SmokeCase:
    try:
        return SmokeCase.model_validate_json(line)
    except ValidationError as error:
        fields = sorted({".".join(map(str, item["loc"])) or "(case)" for item in error.errors()})
    raise DatasetError(f"{CASES_FILE} line {number} is not a valid case: {', '.join(fields)}")


def check_dataset(
    manifest: DatasetManifest,
    cases: Collection[SmokeCase],
    *,
    aliases: Collection[str],
    schemas: Mapping[str, type[BaseModel]],
) -> None:
    """Check the cases against the manifest, the catalog's aliases and the schemas.

    Every case's business is in the manifest and its language is one that business supports; its
    alias is in the catalog; its schema exists; and its canned fake replies behave as the dry run
    needs (valid JSON for the schema, except the forced repair's first reply, which must fail).
    Raises `DatasetError` naming the case and the rule.
    """
    for case in cases:
        problem = _case_problem(case, manifest, aliases, schemas)
        if problem is not None:
            raise DatasetError(f"case {case.id}: {problem}")


def _case_problem(
    case: SmokeCase,
    manifest: DatasetManifest,
    aliases: Collection[str],
    schemas: Mapping[str, type[BaseModel]],
) -> str | None:
    languages = manifest.businesses.get(case.business_id)
    if languages is None:
        return "its business is not in the manifest"
    if case.language not in languages:
        return "its language is not one its business supports (manifest)"
    if case.alias not in aliases:
        return "its alias is not in the catalog"
    if case.schema_name is None:
        return None
    schema = schemas.get(case.schema_name)
    if schema is None:
        return "its schema_name is not a known schema"
    if _validates(schema, case.fake_reply) == case.forced_repair:
        return "its fake_reply must validate, unless it is the forced repair's (which must not)"
    if case.fake_repair_reply is not None and not _validates(schema, case.fake_repair_reply):
        return "its fake_repair_reply must validate"
    return None


def _validates(schema: type[BaseModel], text: str) -> bool:
    try:
        schema.model_validate_json(text)
    except ValidationError:
        return False
    return True
