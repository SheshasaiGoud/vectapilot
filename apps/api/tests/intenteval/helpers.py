"""Shared fixtures for the intenteval tests: made-up rows only.

Every row built here is a placeholder on purpose (an `example-` id and `TEST-ONLY` text), so the
real dataset's check refuses it; tests that need rows to pass call the checks with
`allow_placeholders=True`. No text here is a message: it is a marker and a hash (`filler`), far
from every other filler under the near-duplicate screen, so tests control similarity exactly and
nothing here could seed the eval set (ADR 0012).
"""

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from vectapilot_api.intenteval.checks import IntentDataset
from vectapilot_api.intenteval.rows import (
    BusinessProfile,
    EvalRow,
    IntentManifest,
    TrainRow,
)
from vectapilot_api.intenteval.sample import draw_sample
from vectapilot_api.intenteval.split import IntentFreeze
from vectapilot_api.intents.taxonomy import DECISION_ORDER, Intent

REPO_ROOT: Final = Path(__file__).parents[4]
CORPUS_DIR: Final = REPO_ROOT / "data" / "sample"
DATASET_DIR: Final = REPO_ROOT / "evals" / "datasets" / "intents" / "v0.1"
BUSINESSES: Final = ("sunrise-clinic", "paws-and-co")

PROFILES: Final[Mapping[str, BusinessProfile]] = {
    "sunrise-clinic": BusinessProfile(business_type="human_healthcare", languages=("en", "hi")),
    "paws-and-co": BusinessProfile(business_type="veterinary", languages=("en", "te")),
}
"""What `data/sample`'s manifests say today (test_files checks it)."""

SHORT: Final = {"sunrise-clinic": "sun", "paws-and-co": "paws"}


LETTERS_FOR_DIGITS: Final = str.maketrans("0123456789", "ghijklmnop")


def filler(key: str) -> str:
    """A TEST-ONLY text: the marker and the SHA-256 of `key` with its digits spelled as letters
    (a run of 9 digits would look like a phone number to the personal-data screen)."""
    digest = hashlib.sha256(key.encode()).hexdigest().translate(LETTERS_FOR_DIGITS)
    return f"TEST-ONLY {digest}"


def manifest_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "dataset": "intents",
        "version": "v0.1",
        "created": "2026-11-05",
        "synthetic": True,
        "authored_by": "tests",
        "stage": "collecting",
        "corpus": "data/sample",
        "businesses": list(BUSINESSES),
        "split_rule": {"rule": "stratified-systematic-v2", "seed": "intents", "test_percent": 50},
        "near_duplicate": {"method": "char-3gram-jaccard", "threshold_percent": 70},
        "requirements": {
            "min_eval_rows": 160,
            "min_eval_per_intent": 20,
            "min_eval_per_safety_intent_per_business": 10,
            "min_hindi_safety_per_hindi_business": 6,
            "min_hindi_emergency_per_hindi_business": 3,
            "min_train_per_intent": 40,
        },
        "verification": {"percent": 20, "seed": "intents-verify"},
        "train_draft_sha256": None,
    }
    data.update(overrides)
    return data


def manifest(**overrides: Any) -> IntentManifest:
    return IntentManifest.model_validate(manifest_data(**overrides))


SMALL_REQUIREMENTS: Final = {
    "min_eval_rows": 18,
    "min_eval_per_intent": 2,
    "min_eval_per_safety_intent_per_business": 1,
    "min_hindi_safety_per_hindi_business": 2,
    "min_hindi_emergency_per_hindi_business": 1,
    "min_train_per_intent": 2,
}
"""What `complete_dataset` meets exactly (one row fewer anywhere falls short)."""


def train_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "example-t1",
        "business_id": "sunrise-clinic",
        "language": "en",
        "intent": "question",
        "author": "claude",
        "verified": False,
        "created": "2026-11-05",
        "synthetic": True,
    }
    data.update(overrides)
    data.setdefault("text", filler(str(data["id"])))
    return data


def train_row(**overrides: Any) -> TrainRow:
    return TrainRow.model_validate(train_data(**overrides))


def eval_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "example-e1",
        "business_id": "sunrise-clinic",
        "language": "en",
        "intent": "question",
        "author": "shesha",
        "created": "2026-11-05",
        "synthetic": True,
    }
    data.update(overrides)
    data.setdefault("text", filler(str(data["id"])))
    return data


def eval_row(**overrides: Any) -> EvalRow:
    return EvalRow.model_validate(eval_data(**overrides))


def dataset(
    *,
    train: Iterable[TrainRow] = (),
    eval_rows: Iterable[EvalRow] = (),
    businesses: Mapping[str, BusinessProfile] = PROFILES,
    freeze: IntentFreeze | None = None,
    sample: tuple[str, ...] | None = None,
    train_sha256: str = "0" * 64,
    **manifest_overrides: Any,
) -> IntentDataset:
    return IntentDataset(
        manifest=manifest(**manifest_overrides),
        train=tuple(train),
        eval_rows=tuple(eval_rows),
        businesses=dict(businesses),
        freeze=freeze,
        sample=sample,
        train_sha256=train_sha256,
    )


def complete_train() -> list[TrainRow]:
    """One verified training row per intent and business: 16 rows."""
    return [
        train_row(
            id=f"example-t-{SHORT[b]}-{i.value.replace('_', '-')}",
            business_id=b,
            intent=i,
            verified=True,
        )
        for b in BUSINESSES
        for i in DECISION_ORDER
    ]


def complete_eval() -> list[EvalRow]:
    """One eval row per intent and business, plus two Hindi safety rows for Sunrise (one a
    Hinglish emergency): 18 rows, exactly `SMALL_REQUIREMENTS`. The Hindi judgement row is a
    friend's so the split is balanced (`split.balance_problems`); as `shesha`'s, both Sunrise
    `professional_judgement` rows land in dev and the freeze refuses (test_checks pins that)."""
    rows = [
        eval_row(
            id=f"example-e-{SHORT[b]}-{i.value.replace('_', '-')}",
            business_id=b,
            intent=i,
            author="shesha" if k % 2 else "friend",
        )
        for b in BUSINESSES
        for k, i in enumerate(DECISION_ORDER)
    ]
    rows.append(
        eval_row(
            id="example-e-sun-hi-emergency",
            language="hi",
            code_mixed=True,
            intent=Intent.EMERGENCY,
        )
    )
    rows.append(
        eval_row(
            id="example-e-sun-hi-judgement",
            language="hi",
            intent=Intent.PROFESSIONAL_JUDGEMENT,
            author="friend",
        )
    )
    return rows


NOAH_LAYOUT: Final = (
    ("r0", "paws-and-co", "refund_or_payment", None),
    ("r1", "sunrise-clinic", "complaint", "g1"),
    ("r2", "sunrise-clinic", "complaint", None),
    ("r3", "paws-and-co", "refund_or_payment", "g1"),
    ("r4", "sunrise-clinic", "professional_judgement", "g1"),
    ("r5", "paws-and-co", "emergency", None),
)
"""noah's review of the split: the complaint repair moves g1 into test alone, then the
refund_or_payment repair moves g1 back to dev alone, so both complaint units end in dev. Split
groups that mix intents are realistic: `check` puts eval-eval near-duplicates into one group
whatever their intents. The ids are noah's, so the case reproduces his probe exactly; their text
is still `TEST-ONLY`, so each row remains a placeholder."""


def noah_rows() -> list[EvalRow]:
    return [eval_row(id=i, business_id=b, intent=n, split_group=g) for i, b, n, g in NOAH_LAYOUT]


def complete_dataset(**overrides: Any) -> IntentDataset:
    """A collecting dataset that `freeze` accepts with `allow_placeholders=True`."""
    train = complete_train()
    fields: dict[str, Any] = {
        "train": train,
        "eval_rows": complete_eval(),
        "sample": draw_sample(train, manifest().verification),
        "requirements": SMALL_REQUIREMENTS,
    }
    fields.update(overrides)
    return dataset(**fields)
