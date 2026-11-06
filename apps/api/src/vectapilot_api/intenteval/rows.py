"""`intents` v0.1 (M4 PR 1): the row schemas, the dataset manifest and their loaders.

The files live at the repository root, in `evals/datasets/intents/<version>/`: `manifest.json`
(`IntentManifest`), `train.jsonl` (`TrainRow`s), `eval.jsonl` (`EvalRow`s), the verification
sample `verification-sample.txt` (`sample.py`) and, once frozen, `freeze.json` (`split.py`). The
folder's README documents every field.

**Who writes which file (D3 (a), the blind order).** Training rows are drafted by Claude
(`author: claude`, PR 2) while `eval.jsonl` is empty; eval rows are written and labelled by
people only (`shesha` or `friend`), never by an agent (ADR 0012). The schemas enforce the author
per file, so a misfiled row fails to load. Nothing here writes, completes or suggests a row.

**Two schemas, one per file,** rather than one row with a `file` field: a training row has
`verified` (shesha's check of its label, D3's 20 % sample) and nothing to do with splits; an eval
row has `split` (set by `freeze`), `split_group` and `steering`. A field from the other file is
refused (`extra="forbid"`).

Hinglish is `language: hi` with `code_mixed: true`, as in `support_qa` (never `hi-Latn`). A row's
language must be one its business lists (`checks.py`); M4 has no Telugu rows (carried from M3),
so `te` is not a row language.

Loaders take text and do no I/O (`files.py` reads). Problems name the file, line and fields, never
a row's text (Pydantic's `hide_input_in_errors`, `evalset.corpus.EvalModel`).
"""

import json
from collections import Counter
from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Annotated, Final, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError, DatasetVersion, Split, SplitRule
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.intents.taxonomy import Intent

MANIFEST_FILE: Final = "manifest.json"
TRAIN_FILE: Final = "train.jsonl"
EVAL_FILE: Final = "eval.jsonl"
FREEZE_FILE: Final = "freeze.json"
SAMPLE_FILE: Final = "verification-sample.txt"

TEXT_MAX_CHARS: Final = 500

RowLanguage = Literal["en", "hi"]
"""A row's language. Hinglish is `hi` with `code_mixed: true`."""

LanguageGroup = Literal["en", "hi", "hi-code-mixed"]
LANGUAGE_GROUPS: Final[tuple[LanguageGroup, ...]] = ("en", "hi", "hi-code-mixed")

GROUPS_OF_LANGUAGE: Final[Mapping[str, tuple[LanguageGroup, ...]]] = MappingProxyType(
    {"en": ("en",), "hi": ("hi", "hi-code-mixed")}
)
"""The row language groups a business language allows (`te` allows none in M4)."""

TrainAuthor = Literal["claude"]
EvalAuthor = Literal["shesha", "friend"]
"""`shesha`, or a `friend` who wrote from the writers' brief (D3 (a))."""


def _language_group(language: RowLanguage, code_mixed: bool) -> LanguageGroup:
    if language == "en":
        return "en"
    return "hi-code-mixed" if code_mixed else "hi"


class _RowFields(EvalModel):
    """The fields both files share. Not a file model on its own."""

    id: Slug
    """Unique across `train.jsonl` and `eval.jsonl` together; never reused for a changed row."""
    business_id: BusinessId
    """One of the manifest's `businesses` (each a `data/sample/<business_id>/` folder)."""
    language: RowLanguage
    code_mixed: bool = False
    text: str = Field(min_length=1, max_length=TEXT_MAX_CHARS, repr=False)
    """The message exactly as a customer would type it, typos included."""
    intent: Intent
    """The gold label: the first intent in the decision order that fits (`intents.taxonomy`)."""

    @field_validator("text")
    @classmethod
    def _not_blank(cls, text: str) -> str:
        if not text.strip():
            raise ValueError("text is not blank")
        return text

    @model_validator(mode="after")
    def _code_mixed_is_hindi(self) -> Self:
        if self.code_mixed and self.language == "en":
            raise ValueError("a code-mixed row is hi mixed with English (Hinglish), not en")
        return self

    @property
    def language_group(self) -> LanguageGroup:
        """`en`, `hi`, or `hi-code-mixed` for Hinglish (`support_qa`'s groups)."""
        return _language_group(self.language, self.code_mixed)


class TrainRow(_RowFields):
    """One line of `train.jsonl`: drafted by Claude (PR 2), labels checked by shesha."""

    author: TrainAuthor
    verified: bool
    """Whether shesha has checked this row's label (D3: at least 20 %, from `sample`'s list)."""
    created: date
    synthetic: Literal[True]


class EvalRow(_RowFields):
    """One line of `eval.jsonl`: written and labelled by a person, never by an agent."""

    author: EvalAuthor
    split: Split | None = None
    """Set by `freeze` from the split rule, never by hand (`null` while collecting)."""
    split_group: Slug | None = None
    """Rows that must share one split: a message and its translations, one message written for
    both businesses, and any near-duplicate pair (`check` requires it). Not needed otherwise."""
    steering: bool = False
    """The message tries to steer its own classification (for example by telling the assistant
    how to file it). Optional; such rows are reported apart (PR 5). Label what the message is,
    never what it asks to be filed as: anything a message says about its own classification is
    part of the message, and a steering row keeps the intent of its real request (README, "The
    intents"). Labelled as it asks, a successful injection would score as correct."""
    created: date
    synthetic: Literal[True]


class BusinessProfile(EvalModel):
    """What the dataset needs of a business's configuration (today
    `data/sample/<business_id>/manifest.json`): its type, which picks its route template
    (`intents.policy`), and its languages, which a row's language must be one of. Read from the
    configuration, never written in code (rule 9); `freeze.json` pins both."""

    business_type: str = Field(min_length=1, max_length=40)
    languages: tuple[str, ...] = Field(min_length=1)


class NearDuplicateRule(EvalModel):
    """The near-duplicate screen (`similarity.py`), fixed before any row exists."""

    method: Literal["char-3gram-jaccard"]
    threshold_percent: int = Field(ge=1, le=100)
    """A pair at or above this similarity is a near-duplicate (integer arithmetic, exact)."""


class Requirements(EvalModel):
    """What `freeze` needs (D3 (a), decided 2026-11-05; the train minimum is PR 2's proposal).
    Lowering one is a decision, not an edit (a test pins the committed values)."""

    min_eval_rows: int = Field(ge=1)
    min_eval_per_intent: int = Field(ge=1)
    min_eval_per_safety_intent_per_business: int = Field(ge=0)
    """For each business, eval rows of each of its business type's safety intents
    (`intents.policy`: `emergency` and `professional_judgement` for both health types)."""
    min_hindi_safety_per_hindi_business: int = Field(ge=0)
    """For each business that lists `hi`: eval safety rows in Hindi or Hinglish."""
    min_hindi_emergency_per_hindi_business: int = Field(ge=0)
    """... of which `emergency`."""
    min_train_per_intent: int = Field(ge=1)


class Verification(EvalModel):
    """D3's human check of the training labels: a seeded, stratified sample (`sample.py`)."""

    percent: int = Field(ge=1, le=100)
    seed: str = Field(min_length=1, max_length=100)


class IntentManifest(EvalModel):
    """`manifest.json` of an `intents` version."""

    dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    authored_by: str = Field(min_length=1, max_length=300)
    stage: Literal["collecting", "frozen"]
    """`collecting`: rows are checked one by one, the minimums are not. `frozen`: `freeze`
    assigned the splits and wrote freeze.json; the minimums and the pins are enforced."""
    corpus: str = Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")
    """The folder of the business manifests, relative to the repository root (`data/sample`)."""
    businesses: tuple[BusinessId, ...] = Field(min_length=1)
    split_rule: SplitRule
    """`stratified-systematic-v2` over eval rows (`split.py`)."""
    near_duplicate: NearDuplicateRule
    requirements: Requirements
    verification: Verification
    train_draft_sha256: Sha256 | None = None
    """The SHA-256 of `train.jsonl` as PR 2 merged it (the blind order, step 1). `null` until
    then. Provenance, not a pin: shesha's label corrections change the file later."""

    @field_validator("businesses")
    @classmethod
    def _unique(cls, businesses: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(businesses)) != len(businesses):
            raise ValueError("each business is listed once")
        return businesses


def load_manifest(text: str) -> IntentManifest:
    """Parse and validate `manifest.json`. Raises `DatasetError`."""
    try:
        return IntentManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise DatasetError(f"{MANIFEST_FILE} is not a valid intents manifest: {detail}")


def _load_rows[R: _RowFields](text: str, model: type[R], file_name: str) -> tuple[R, ...]:
    rows: list[R] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(model.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(row)")
            problems.append(f"{file_name} line {number} is not a valid row: {detail}")
    if problems:
        raise DatasetError("\n".join(problems))
    return tuple(rows)


def load_train(text: str) -> tuple[TrainRow, ...]:
    """Parse `train.jsonl` (blank lines skipped; an empty file is zero rows). Raises
    `DatasetError` naming every bad line and its failing fields, never the line."""
    return _load_rows(text, TrainRow, TRAIN_FILE)


def load_eval(text: str) -> tuple[EvalRow, ...]:
    """Parse `eval.jsonl`, as `load_train`."""
    return _load_rows(text, EvalRow, EVAL_FILE)


def dump_row(row: TrainRow | EvalRow) -> str:
    """One JSONL line, UTF-8 as written (Hindi stays readable), fields in schema order."""
    return json.dumps(row.model_dump(mode="json"), ensure_ascii=False)


def repeated_ids(train: tuple[TrainRow, ...], eval_rows: tuple[EvalRow, ...]) -> list[str]:
    """Ids used more than once across both files, sorted."""
    counts = Counter(r.id for r in (*train, *eval_rows))
    return sorted(i for i, n in counts.items() if n > 1)
