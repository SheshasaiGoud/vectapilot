"""Eval v0 questions (M2 PR 7): the row schema, the dataset manifest, loaders and checks.

The files live at the repository root, in `evals/datasets/support_qa/<version>/`: `manifest.json`
(`EvalManifest`), `questions.jsonl` (one `EvalQuestion` per line; empty until shesha and friends
write them) and, once frozen, `freeze.json` (`split.FreezeManifest`). The folder's README.md
documents every field. Questions are human-written (decided 2026-10-21): no code here writes,
completes or suggests one. The rules follow shesha's decisions of 2026-10-22 (items 1-8 under
PR 7 in docs/plans/m2-pr-checks.md).

Loaders take text and do no I/O (`files.py` reads). Problems name the question id and the rule,
never question, answer or document text.
"""

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Final, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from vectapilot_api.evalset.corpus import (
    ANCHOR_MAX_CHARS,
    ANCHOR_MIN_CHARS,
    Corpus,
    EvalModel,
    KeyProblem,
    describe_validation_error,
    normalize_anchor,
    resolve_anchor,
)
from vectapilot_api.evalset.personal_data import personal_data_findings
from vectapilot_api.gateway.types import BusinessId, Slug
from vectapilot_api.ingestion.chunking import DEFAULT_CHUNKING
from vectapilot_api.ingestion.types import ChunkingConfig

MANIFEST_FILE: Final = "manifest.json"
QUESTIONS_FILE: Final = "questions.jsonl"
FREEZE_FILE: Final = "freeze.json"

Language = Literal["en", "hi", "te"]
"""The question's language. Hinglish is `hi` with `code_mixed: true` (as in gateway_smoke)."""

Category = Literal[
    "faq",
    "policy",
    "multi_hop",
    "ambiguous",
    "out_of_scope",
    "adversarial",
    "safety",
    "handoff_request",
    "action",
    "document",
]

CATEGORIES: Final[Mapping[Category, str]] = {
    "faq": "one fact from one passage: hours, fees, services, products, doctors or vets",
    "policy": (
        "a rule or procedure: booking, cancelling, the refund or return policy, what to bring"
    ),
    "multi_hop": "needs two or more passages, from one document or several, combined",
    "ambiguous": "could mean two things: answered, with evidence for every reading",
    "out_of_scope": "not in this business's approved public documents: an honest no-answer",
    "adversarial": "tries to make the assistant break a rule: leak, invent, obey injected text",
    "safety": "an emergency or a request for professional judgement: diagnosis, dosing, urgency",
    "handoff_request": (
        "asks for a human, or hits a mandatory hand-off rule (complaint, refund request)"
    ),
    "action": "asks the assistant to do something (book, cancel, order): tools, M5",
    "document": "about a document the customer uploads: extraction, M6",
}
"""Every category with its one-line definition (README and the guide use the same lines)."""

RESERVED_CATEGORIES: Final[Mapping[Category, str]] = {
    "action": "M5 (tools)",
    "document": "M6 (document extraction)",
}
"""Categories v0.1 refuses: the system cannot handle them before that milestone."""

REQUIRED_CATEGORIES: Final = tuple(c for c in CATEGORIES if c not in RESERVED_CATEGORIES)

CATEGORY_PRECEDENCE: Final[tuple[Category, ...]] = (
    "safety",
    "handoff_request",
    "adversarial",
    "out_of_scope",
    "ambiguous",
    "multi_hop",
    "policy",
    "faq",
)
"""When two categories fit a question, the earlier one here wins (decided 2026-10-22, item 5):
what the system must get right first. One order, for every labeller."""

Subtype = Literal[
    "emergency", "professional_judgement", "off_domain", "unanswerable", "non_public_only"
]

SUBTYPES: Final[Mapping[Category, Mapping[Subtype, str]]] = {
    "safety": {
        "emergency": "someone, or a pet, may need urgent help now",
        "professional_judgement": "asks for a diagnosis, a dose, or whether something is serious",
    },
    "out_of_scope": {
        "off_domain": "nothing to do with this business",
        "unanswerable": "about this business, but no document says it",
        "non_public_only": "only a staff-only, retired or draft document says it",
    },
}
"""The optional `subtype` each category takes (decided 2026-10-22, item 5); the others take none."""

Outcome = Literal["answered", "dont_know", "handoff"]
"""What a correct reply does: ADR 0011's outcomes."""

AuthorType = Literal["team", "friend_blind"]
"""`team`: shesha and the team; `friend_blind`: a friend who wrote from the brief, without seeing
the documents (decided 2026-10-22, item 7)."""

Split = Literal["dev", "test"]

PLACEHOLDER_MARKER: Final = "EXAMPLE-ONLY"
"""What guides and templates write on their made-up rows (`import-friends` skips such rows)."""

PLACEHOLDER_PATTERN: Final = re.compile(r"example[\W_]*only|test[-_\u2010-\u2015\u2212]+only")
"""Every marker a real dataset refuses, matched on NFKC-casefolded text (mutation audit D5b), so
case and full-width letters never hide one. `EXAMPLE-ONLY` (guides) in any spacing or
punctuation: `example only`, `Example_Only`. `TEST-ONLY` (tests) only when a hyphen, underscore or
dash joins the words (`test_only`, `TEST-ONLY` with a non-breaking hyphen), so a real
question such as "the blood test only" passes (decided 2026-10-22)."""

_PLACEHOLDER_ID: Final = re.compile(r"^example(-|$)")
"""An id that is `example` or starts with `example-` is a placeholder too."""

DatasetVersion = Annotated[str, Field(pattern=r"^v[0-9]+\.[0-9]+$")]


def is_placeholder_text(text: str) -> bool:
    """Whether `text` carries a placeholder marker (`PLACEHOLDER_PATTERN`)."""
    return PLACEHOLDER_PATTERN.search(unicodedata.normalize("NFKC", text).casefold()) is not None


class DatasetError(ValueError):
    """A dataset file cannot be parsed. The message names the file, line and fields."""


class SourceKey(EvalModel):
    """One place a question's evidence is: a document of the question's business and a quote
    from it (`corpus.py` says why not a chunk id)."""

    source_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    anchor: str = Field(min_length=ANCHOR_MIN_CHARS, max_length=ANCHOR_MAX_CHARS, repr=False)
    """A verbatim quote from the document's loaded text (copy it from `lookup`); whitespace
    differences are ignored, case is not. Stored NFC with whitespace runs collapsed."""

    @field_validator("anchor")
    @classmethod
    def _normalized(cls, anchor: str) -> str:
        normalized = normalize_anchor(anchor)
        if len(normalized) < ANCHOR_MIN_CHARS:
            raise ValueError(f"an anchor has at least {ANCHOR_MIN_CHARS} characters")
        return normalized


EvidenceGroup = Annotated[tuple[SourceKey, ...], Field(min_length=1)]
"""Interchangeable keys for one piece of evidence: the same fact stated in several places (`faq`
and `faq-hi`, the FAQ and the fees PDF). A hit that matches any one key finds the group
(`relevance.py`)."""


class EvalQuestion(EvalModel):
    """One question (one line of `questions.jsonl`)."""

    id: Slug
    """Unique; never reused for a changed question (a changed question is a new id)."""
    replaces: Slug | None = None
    """In a later version: the id of the earlier version's question this one corrects. It takes
    that question's frozen split (`split.py`), so a corrected dev question stays dev."""
    split_group: Slug | None = None
    """Questions that must share one split (decided 2026-10-22, item 4): a question and its
    translations, or the same text asked of both businesses. The same text in two businesses is
    refused unless the rows share a `split_group`. Not an evidence group."""
    business_id: BusinessId
    question: str = Field(min_length=1, max_length=2000, repr=False)
    """Exactly as the customer would type it (`POST /v1/answer` takes up to 2,000 characters)."""
    language: Language
    code_mixed: bool = False
    category: Category
    subtype: Subtype | None = None
    """Optional, for `safety` and `out_of_scope` only (`SUBTYPES`)."""
    author_type: AuthorType
    saw_documents: bool
    """Whether the author had read the business's documents before writing it."""
    expected_outcome: Outcome
    expected_evidence: tuple[EvidenceGroup, ...] = ()
    """What a correct answer rests on: every group is needed, any key of a group finds it.
    `answered`: one group or more (`multi_hop`: two or more; `ambiguous`: one per reading);
    `dont_know` and `out_of_scope`: none; `handoff`: optional (what the reply points to)."""
    reference_answer: str | None = Field(default=None, min_length=1, max_length=1000, repr=False)
    """Optional, human-written: what a correct reply must say (helps the correctness labeller)."""
    split: Split | None = None
    """Set by `freeze` from the split rule, never by hand (`null` while collecting)."""
    notes: str | None = Field(default=None, min_length=1, max_length=1000, repr=False)
    created: date
    synthetic: Literal[True]
    """Every question is synthetic until a real pilot (business-context §1, Q1)."""

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if self.replaces == self.id:
            raise ValueError("a question cannot replace itself")
        reserved = RESERVED_CATEGORIES.get(self.category)
        if reserved is not None:
            raise ValueError(f"category {self.category} is reserved until {reserved}")
        subtypes = SUBTYPES.get(self.category, {})
        if self.subtype is not None and self.subtype not in subtypes:
            allowed = f"one of {sorted(subtypes)}" if subtypes else "none"
            raise ValueError(f"subtype: {self.category} takes {allowed}")
        if self.code_mixed and self.language == "en":
            raise ValueError("a code-mixed question is hi or te mixed with English, not en")
        if self.author_type == "friend_blind" and self.saw_documents:
            raise ValueError("a friend_blind question is written without seeing the documents")
        keys = [(k.source_id, k.version, k.anchor) for g in self.expected_evidence for k in g]
        if len(set(keys)) != len(keys):
            raise ValueError("each evidence key is listed once, in one group")
        groups = len(self.expected_evidence)
        if self.category == "out_of_scope" and (groups or self.expected_outcome == "answered"):
            raise ValueError("out_of_scope: no evidence, and dont_know or handoff")
        if self.expected_outcome == "answered" and not groups:
            raise ValueError("an answered question has at least one evidence group")
        if self.expected_outcome == "dont_know" and groups:
            raise ValueError(
                "a dont_know question has no evidence (a document that answers it "
                "makes it answered)"
            )
        if self.category in ("safety", "handoff_request") and self.expected_outcome != "handoff":
            raise ValueError(f"{self.category}: the expected outcome is handoff (§3.5)")
        if self.category == "ambiguous" and self.expected_outcome != "answered":
            raise ValueError(
                "ambiguous: the expected outcome is answered, with evidence for every reading"
            )
        if self.category == "multi_hop" and groups < 2:
            raise ValueError("multi_hop: at least two evidence groups")
        return self

    @property
    def language_group(self) -> str:
        """`en`, `hi`, or `hi-code-mixed` for Hinglish (gateway_smoke's groups)."""
        return f"{self.language}-code-mixed" if self.code_mixed else self.language

    def evidence_keys(self) -> Iterator[tuple[str, SourceKey]]:
        """Every evidence key with where it is, `evidence group G key K` (both from 1)."""
        for g, group in enumerate(self.expected_evidence, start=1):
            for k, key in enumerate(group, start=1):
                yield f"evidence group {g} key {k}", key


class SplitRule(EvalModel):
    """The test-split rule, recorded before any question exists (see `split.py`)."""

    rule: Literal["stratified-systematic-v2"]
    seed: str = Field(min_length=1, max_length=100)
    test_percent: int = Field(ge=1, le=99)


class Requirements(EvalModel):
    """What `ready` (and a frozen dataset) needs: decided 2026-10-21 (items 1, 5) and 2026-10-22
    (item 6)."""

    min_questions: int = Field(ge=1)
    min_hindi: int = Field(ge=0)
    """Questions with `language: hi` (Hinglish included) to a business whose corpus manifest
    lists `hi`. A Hindi question to a business that does not list it tests the hand-off path
    instead, and does not count."""
    min_friend_blind: int = Field(ge=0)
    min_per_category: int = Field(ge=1)
    """For every category except the reserved ones."""
    min_per_business: int = Field(ge=1)
    min_safety_per_business: int = Field(ge=0)
    """`safety` questions of each business: §6's hard gate is per business."""
    min_no_answer: int = Field(ge=0)
    """`dont_know` questions with no evidence (a `dont_know` row never has any), some about facts
    that only a staff-only, retired or draft document holds."""


class EvalManifest(EvalModel):
    """`manifest.json` of a dataset version."""

    dataset: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
    version: DatasetVersion
    created: date
    synthetic: Literal[True]
    authored_by: str = Field(min_length=1, max_length=300)
    stage: Literal["collecting", "frozen"]
    """`collecting`: rows are checked one by one, sizes are not. `frozen`: `freeze` assigned the
    splits and wrote freeze.json; the size and coverage requirements and the pins are enforced."""
    corpus: str = Field(pattern=r"^[a-z0-9_-]+(/[a-z0-9_-]+)*$")
    """The corpus folder, relative to the repository root (`data/sample`)."""
    businesses: tuple[BusinessId, ...] = Field(min_length=1)
    split_rule: SplitRule
    requirements: Requirements
    previous_version: DatasetVersion | None = None
    """The version whose frozen splits this one keeps (`split.py`); None for the first."""


def load_manifest(text: str) -> EvalManifest:
    """Parse and validate `manifest.json`. Raises `DatasetError`."""
    try:
        return EvalManifest.model_validate_json(text)
    except ValidationError as error:
        detail = describe_validation_error(error, "(manifest)")
    raise DatasetError(f"{MANIFEST_FILE} is not a valid manifest: {detail}")


def load_questions(text: str) -> tuple[EvalQuestion, ...]:
    """Parse `questions.jsonl`: one JSON object per line, blank lines skipped. An empty file is
    zero questions (the state v0.1 ships in). Raises `DatasetError` naming every bad line, its
    failing fields and broken rules, never the line."""
    questions: list[EvalQuestion] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            questions.append(EvalQuestion.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(row)")
            problems.append(f"{QUESTIONS_FILE} line {number} is not a valid question: {detail}")
    if problems:
        raise DatasetError("\n".join(problems))
    return tuple(questions)


# ── Checks on the rows (every stage) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedKey:
    """One evidence key and the chunks that hold its anchor, under one chunking config."""

    key: SourceKey
    chunk_indexes: tuple[int, ...]


def resolve_expected(
    corpus: Corpus, question: EvalQuestion, config: ChunkingConfig = DEFAULT_CHUNKING
) -> tuple[tuple[ResolvedKey, ...], ...]:
    """Each evidence group of the question, each key as the chunks that hold its anchor at
    `config` (what `check` shows a labeller). Scoring goes by content (`relevance.py`), not by
    these numbers. Raises `KeyProblem`."""
    return tuple(
        tuple(
            ResolvedKey(
                key=key, chunk_indexes=resolve_key(corpus, question.business_id, key, config)
            )
            for key in group
        )
        for group in question.expected_evidence
    )


def resolve_key(
    corpus: Corpus, business_id: str, key: SourceKey, config: ChunkingConfig = DEFAULT_CHUNKING
) -> tuple[int, ...]:
    """The chunk indexes of one key, looked up in `business_id`'s corpus only, by exact
    `source_id` and `version` (case included). Raises `KeyProblem`: an unknown document, one a
    customer's answer may never use, or an anchor that is missing, repeated or split across
    chunks."""
    document = corpus.document(business_id, key.source_id, key.version)
    if document is None:
        raise KeyProblem("its source_id and version are not in this business's corpus")
    if not document.source.answers_customers:
        raise KeyProblem(
            "it points at a document a customer's answer may never use (draft, retired or "
            "staff_only)"
        )
    return resolve_anchor(corpus, document, key.anchor, config)


def _screened_texts(question: EvalQuestion) -> list[tuple[str, str]]:
    """Every free text of a row, with the field it is in."""
    texts = [("question", question.question)]
    texts += [
        (name, text)
        for name, text in (
            ("notes", question.notes),
            ("reference_answer", question.reference_answer),
        )
        if text is not None
    ]
    texts += [(f"{where} anchor", key.anchor) for where, key in question.evidence_keys()]
    return texts


def _has_placeholder(question: EvalQuestion) -> bool:
    return bool(_PLACEHOLDER_ID.match(question.id)) or any(
        is_placeholder_text(text) for _, text in _screened_texts(question)
    )


def _repeated_texts(questions: Iterable[EvalQuestion]) -> int:
    """How many rows repeat an earlier row's text (case and whitespace ignored). Rows with the
    same text in different businesses that share one `split_group` are twins, not repeats."""
    by_text: defaultdict[str, list[EvalQuestion]] = defaultdict(list)
    for question in questions:
        by_text[" ".join(question.question.casefold().split())].append(question)
    repeated = 0
    for rows in by_text.values():
        first = rows[0]
        twins = first.split_group is not None and all(
            r.split_group == first.split_group for r in rows
        )
        twins = twins and len({r.business_id for r in rows}) == len(rows)
        if not twins:
            repeated += len(rows) - 1
    return repeated


def question_problems(
    manifest: EvalManifest,
    questions: Sequence[EvalQuestion],
    corpus: Corpus,
    *,
    config: ChunkingConfig = DEFAULT_CHUNKING,
    allow_placeholders: bool = False,
) -> list[str]:
    """Every problem with the rows, in file order: unique ids and texts, the business, the
    language, placeholders, personal data (`personal_data.py`), the evidence, and (while
    collecting) no split set by hand. `allow_placeholders` exists for tests only; the CLI and
    the dataset test never set it. A problem names the question id and the field, never text."""
    problems: list[str] = []
    counts = Counter(q.id for q in questions)
    problems += [f"question id {i} is used {n} times" for i, n in sorted(counts.items()) if n > 1]
    repeated = _repeated_texts(questions)
    if repeated:
        problems.append(
            f"{repeated} question(s) repeat another question's text (the same text in two "
            "businesses needs one shared `split_group`)"
        )
    for question in questions:
        where = f"question {question.id}"
        if question.business_id not in manifest.businesses:
            problems.append(f"{where}: its business is not in the dataset manifest")
            continue
        business = corpus.manifests.get(question.business_id)
        if business is None:
            problems.append(f"{where}: its business has no corpus under {manifest.corpus}")
            continue
        if question.language not in business.languages and question.expected_outcome != "handoff":
            problems.append(
                f"{where}: its business does not list {question.language}, so the expected "
                "outcome is handoff"
            )
        if not allow_placeholders and _has_placeholder(question):
            problems.append(f"{where}: is a placeholder (a marker such as {PLACEHOLDER_MARKER})")
        for field, text in _screened_texts(question):
            problems += [
                f"{where}: {field} holds what looks like personal data ({finding}); "
                "use the fake forms (README)"
                for finding in personal_data_findings(text)
            ]
        if manifest.stage == "collecting" and question.split is not None:
            problems.append(f"{where}: split is set by `freeze`, never by hand (leave it null)")
        for label, key in question.evidence_keys():
            try:
                resolve_key(corpus, question.business_id, key, config)
            except KeyProblem as problem:
                problems.append(f"{where}: {label} ({key.source_id}): {problem}")
    return problems


# ── Readiness (enforced once frozen) ──────────────────────────────────────────────────────────


def readiness_problems(
    manifest: EvalManifest, questions: Collection[EvalQuestion], corpus: Corpus
) -> list[str]:
    """What is missing before the dataset can be frozen: sizes and coverage (decided 2026-10-21,
    items 1 and 5; 2026-10-22, item 6)."""
    need = manifest.requirements
    problems: list[str] = []

    def at_least(what: str, have: int, minimum: int) -> None:
        if have < minimum:
            problems.append(f"{what}: {have} of at least {minimum}")

    def lists_hindi(business_id: str) -> bool:
        business = corpus.manifests.get(business_id)
        return business is not None and "hi" in business.languages

    at_least("questions", len(questions), need.min_questions)
    hindi = sum(q.language == "hi" and lists_hindi(q.business_id) for q in questions)
    at_least("Hindi or Hinglish questions to a business that lists hi", hindi, need.min_hindi)
    friends = sum(q.author_type == "friend_blind" for q in questions)
    at_least("friend_blind questions", friends, need.min_friend_blind)
    no_answer = sum(q.expected_outcome == "dont_know" for q in questions)
    at_least("dont_know questions with no evidence", no_answer, need.min_no_answer)
    categories = Counter(q.category for q in questions)
    for category in REQUIRED_CATEGORIES:
        at_least(f"category {category}", categories[category], need.min_per_category)
    businesses = Counter(q.business_id for q in questions)
    safety = Counter(q.business_id for q in questions if q.category == "safety")
    for business_id in manifest.businesses:
        at_least(f"business {business_id}", businesses[business_id], need.min_per_business)
        at_least(
            f"business {business_id} safety questions",
            safety[business_id],
            need.min_safety_per_business,
        )
    return problems


# ── Warnings: worth a look, never a failure ───────────────────────────────────────────────────

VERBATIM_WORDS: Final = 8
_WORD: Final = re.compile(r"[\w\u0900-\u0963\u0966-\u097F]+")
"""A word. The plain word class splits Hindi words at their vowel signs (combining marks), so the
Devanagari block counts too, except the danda and double danda (sentence ends)."""


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def verbatim_warnings(corpus: Corpus, questions: Iterable[EvalQuestion]) -> list[str]:
    """Questions that copy a run of `VERBATIM_WORDS` words from one of their business's
    documents. A customer rarely quotes the document, and a copied question makes retrieval look
    easier than it is: a reason to reword, not a rule."""
    warnings: list[str] = []
    for question in questions:
        grams = _ngrams(question.question, VERBATIM_WORDS)
        if not grams:
            continue
        for document in corpus.documents(question.business_id):
            if grams & _ngrams(document.loaded.text, VERBATIM_WORDS):
                warnings.append(
                    f"question {question.id}: copies {VERBATIM_WORDS}+ words in a row from "
                    f"{document.source.source_id} {document.source.version}"
                )
                break
    return warnings


def split_group_warnings(questions: Iterable[EvalQuestion]) -> list[str]:
    """Split groups with a single question: a split group shares a split between two or more,
    so one alone is most likely a typo in its name."""
    sizes = Counter(q.split_group for q in questions if q.split_group is not None)
    return [f"split_group {g} has only one question" for g, n in sizes.items() if n == 1]


# ── The coverage report: counts, not metrics ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Coverage:
    """How many questions there are of each kind. Counts describe the dataset; they measure
    nothing about the system."""

    total: int
    by_business: Counter[str]
    by_category: Counter[str]
    by_language: Counter[str]
    by_author: Counter[str]
    by_split: Counter[str]
    by_outcome: Counter[str]
    by_subtype: Counter[tuple[str, str]]
    """`(category, subtype)` for the rows that set one."""
    by_category_and_split: Counter[tuple[str, str]]
    by_author_and_split: Counter[tuple[str, str]]
    by_language_and_split: Counter[tuple[str, str]]
    by_business_and_split: Counter[tuple[str, str]]
    by_business_and_category: Counter[tuple[str, str]]
    unsupported_language: int
    """Questions in a language their business does not list (they expect `handoff`)."""
    with_reference_answer: int


def coverage(questions: Sequence[EvalQuestion], corpus: Corpus) -> Coverage:
    def split(q: EvalQuestion) -> str:
        return q.split or "unassigned"

    unsupported = 0
    for q in questions:
        manifest = corpus.manifests.get(q.business_id)
        if manifest is not None and q.language not in manifest.languages:
            unsupported += 1
    return Coverage(
        total=len(questions),
        by_business=Counter(q.business_id for q in questions),
        by_category=Counter(q.category for q in questions),
        by_language=Counter(q.language_group for q in questions),
        by_author=Counter(q.author_type for q in questions),
        by_split=Counter(split(q) for q in questions),
        by_outcome=Counter(q.expected_outcome for q in questions),
        by_subtype=Counter((q.category, q.subtype) for q in questions if q.subtype is not None),
        by_category_and_split=Counter((q.category, split(q)) for q in questions),
        by_author_and_split=Counter((q.author_type, split(q)) for q in questions),
        by_language_and_split=Counter((q.language_group, split(q)) for q in questions),
        by_business_and_split=Counter((q.business_id, split(q)) for q in questions),
        by_business_and_category=Counter((q.business_id, q.category) for q in questions),
        unsupported_language=unsupported,
        with_reference_answer=sum(q.reference_answer is not None for q in questions),
    )


def render_coverage(manifest: EvalManifest, report: Coverage) -> str:
    """Plain-text tables of the counts, every category and business listed (zeros included)."""
    lines = [
        f"{manifest.dataset} {manifest.version} ({manifest.stage}, synthetic): "
        f"{report.total} questions",
        "",
    ]

    def table(title: str, counts: Mapping[str, int], keys: Iterable[str]) -> None:
        lines.append(title)
        lines.extend(f"  {key:<31} {counts.get(key, 0):>4}" for key in keys)
        lines.append("")

    def by_split(
        title: str,
        totals: Mapping[str, int],
        pairs: Mapping[tuple[str, str], int],
        keys: Iterable[str],
        notes: Mapping[str, str] | None = None,
    ) -> None:
        lines.append(f"{title:<26} {'all':>4} {'dev':>4} {'test':>4} {'unassigned':>10}")
        for key in keys:
            cells = [pairs.get((key, s), 0) for s in ("dev", "test", "unassigned")]
            note = (notes or {}).get(key, "")
            lines.append(
                f"  {key:<24} {totals.get(key, 0):>4} "
                f"{cells[0]:>4} {cells[1]:>4} {cells[2]:>10}{note}"
            )
        lines.append("")

    languages = sorted({"en", "hi", "hi-code-mixed", *report.by_language})
    by_split("business", report.by_business, report.by_business_and_split, manifest.businesses)
    reserved: dict[str, str] = {c: " (reserved)" for c in RESERVED_CATEGORIES}
    by_split("category", report.by_category, report.by_category_and_split, CATEGORIES, reserved)
    by_split("language", report.by_language, report.by_language_and_split, languages)
    by_split("author_type", report.by_author, report.by_author_and_split, ("team", "friend_blind"))
    table("split", report.by_split, ("dev", "test", "unassigned"))
    table("expected_outcome", report.by_outcome, ("answered", "dont_know", "handoff"))
    subtypes = {
        f"{c}: {t}": report.by_subtype.get((c, t), 0) for c in SUBTYPES for t in SUBTYPES[c]
    }
    table("subtype (optional)", subtypes, subtypes)
    lines.append(
        f"questions in a language their business does not list: {report.unsupported_language}"
    )
    lines.append(f"questions with a reference_answer: {report.with_reference_answer}")
    return "\n".join(lines) + "\n"


# ── Correctness and faithfulness labels: the human slots, one file each per run ───────────────


class CorrectnessLabel(EvalModel):
    """One human judgement of one answer from one run (PR 9). Labels belong to a run, not to the
    question: the same question gets a new label whenever the system answers it again. They live
    in that run's report folder (`labels.jsonl`, PR 8), never in the dataset."""

    question_id: Slug
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    label: Literal["correct", "incorrect"]
    """Correct: the reply does what `expected_outcome` says, and every claim matches an approved
    source (an abstention is correct when the question expects one). A caveat such as "these
    prices are fictional" is ignored: label on the facts (decided 2026-10-22, item 7)."""
    labelled_by: Slug
    """Who labelled it (a person's slug, e.g. `shesha`; a pilot's staff later)."""
    labelled_on: date
    note: str | None = Field(default=None, min_length=1, max_length=500, repr=False)


class FaithfulnessLabel(EvalModel):
    """One human faithfulness judgement of one shown answer from one run (M3 PR 7; ADR 0013,
    amended 2026-10-28). It lives in that run's `faithfulness_labels.jsonl`, never in the dataset
    and never in `labels.jsonl`, whose format and summaries stay M2's.

    Only a shown answer (outcome `answered`) gets one, and the label is per answer, not per claim
    (M3 plan D11). No LLM judge writes it (D4 (b))."""

    question_id: Slug
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    label: Literal["faithful", "unfaithful"]
    """Faithful: every claim in the answer is supported by the text of the chunks it cites (the
    label sheet prints that text). Unfaithful: at least one claim is not. This is not
    correctness (`CorrectnessLabel`: the reply does what `expected_outcome` says, and every claim
    matches an approved source): an answer can be faithful to a cited chunk that does not answer
    the question, or correct with a claim no cited chunk states. Neither label stands in for the
    other."""
    labelled_by: Slug
    labelled_on: date
    note: str | None = Field(default=None, min_length=1, max_length=500, repr=False)


def load_labels(text: str, questions: Collection[EvalQuestion]) -> tuple[CorrectnessLabel, ...]:
    """Parse a run's `labels.jsonl`: one label per line, every question id known, one label per
    question and run. Raises `DatasetError`."""
    known = {q.id for q in questions}
    labels: list[CorrectnessLabel] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        label = _parse_label(line, number)
        if label.question_id not in known:
            raise DatasetError(f"labels line {number}: its question_id is not in the dataset")
        labels.append(label)
    pairs = Counter((label.question_id, label.run_id) for label in labels)
    if any(n > 1 for n in pairs.values()):
        raise DatasetError("labels: one label per question and run")
    return tuple(labels)


def _parse_label(line: str, number: int) -> CorrectnessLabel:
    try:
        return CorrectnessLabel.model_validate_json(line)
    except ValidationError as error:
        detail = describe_validation_error(error, "(row)")
    # Raised outside the `except`, so nothing is chained (as measure/dataset.py does).
    raise DatasetError(f"labels line {number} is not a valid label: {detail}")


def dump_question(question: EvalQuestion) -> str:
    """One JSONL line, UTF-8 as written (Hindi stays readable), fields in schema order."""
    return json.dumps(question.model_dump(mode="json"), ensure_ascii=False)
