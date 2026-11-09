"""Classifier mode (M4 PR 4): every eval row scored from its cached query vector by the artifact,
as the API would score it (`ClassifierArtifact.score`, standard library only).

The decided intent is the top-1 (ties in decision order): classifier mode is the classifier
alone, with no safety floor, no threshold and no LLM; the floor and tau are the hybrid's
(`llmrun.hybrid_rows`, H1). Each line keeps the calibrated probabilities (the hybrid replays
them), the top probability at the artifact's T and at T = 1 (the raw-against-calibrated
reliability table), the embedding latency of the row's own request (from the cache) and the
scoring latency measured here. No text is read: ids and vectors only.
"""

from collections.abc import Callable, Mapping, Sequence

from vectapilot_api.intenteval.cache import CacheEntry
from vectapilot_api.intenteval.llmrun import base_fields
from vectapilot_api.intenteval.rows import EvalRow
from vectapilot_api.intenteval.runinfo import ResultLine
from vectapilot_api.intenteval.thresholds import DevRow, top_intent
from vectapilot_api.intents.classifier import ClassifierArtifact, IntentScores, softmax
from vectapilot_api.intents.policy import RoutePolicy
from vectapilot_api.intents.taxonomy import DECISION_ORDER


def score_rows(
    rows: Sequence[EvalRow],
    artifact: ClassifierArtifact,
    cache: Mapping[str, CacheEntry],
    policies: Mapping[str, RoutePolicy],
    clock: Callable[[], float],
) -> list[ResultLine]:
    """One classifier-mode line per row (module docstring). Raises `ClassifierError` for a
    vector from another model or dimension."""
    lines: list[ResultLine] = []
    for row in rows:
        entry = cache[row.id]
        vector = entry.values()
        started = clock()
        scores = artifact.score(vector, model=entry.model, input_type=entry.input_type)
        scoring_ms = max(0.0, (clock() - started) * 1000)
        raw = softmax(artifact.logits(vector, model=entry.model, input_type=entry.input_type))
        top, probability = top_intent(scores)
        policy = policies[row.business_id]
        route = policy.route(top)
        embedding_ms = entry.latency_ms
        lines.append(
            ResultLine.model_validate(
                base_fields(row, policy)
                | {
                    "decided_by": "classifier",
                    "final_intent": top,
                    "route": route.kind,
                    "priority": route.priority,
                    "classifier_intent": top,
                    "top_probability": probability,
                    "top_probability_raw": max(raw),
                    "probabilities": tuple(scores.probabilities[i] for i in DECISION_ORDER),
                    "scoring_ms": scoring_ms,
                    "embedding_ms": embedding_ms,
                    "latency_ms": None if embedding_ms is None else embedding_ms + scoring_ms,
                }
            )
        )
    return lines


def dev_logits(
    rows: Sequence[EvalRow], artifact: ClassifierArtifact, cache: Mapping[str, CacheEntry]
) -> tuple[list[tuple[float, ...]], list[int]]:
    """The raw logits and gold class indices of the dev rows: what `calibrate` fits T on."""
    logits: list[tuple[float, ...]] = []
    labels: list[int] = []
    for row in rows:
        entry = cache[row.id]
        logits.append(
            artifact.logits(entry.values(), model=entry.model, input_type=entry.input_type)
        )
        labels.append(DECISION_ORDER.index(row.intent))
    return logits, labels


def dev_rows(
    rows: Sequence[EvalRow],
    artifact: ClassifierArtifact,
    cache: Mapping[str, CacheEntry],
    policies: Mapping[str, RoutePolicy],
) -> list[DevRow]:
    """The dev rows as D8's rule sees them, scored at the artifact's (calibrated) T."""
    found: list[DevRow] = []
    for row in rows:
        entry = cache[row.id]
        scores: IntentScores = artifact.score(
            entry.values(), model=entry.model, input_type=entry.input_type
        )
        found.append(
            DevRow(
                id=row.id,
                gold=row.intent,
                scores=scores,
                safety_intents=policies[row.business_id].safety_intents,
            )
        )
    return found
