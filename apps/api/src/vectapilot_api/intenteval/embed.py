"""Embedding the rows of an `intents` version (M4 PR 4, the `embed` command's core).

Through the product's `EmbeddingRunner` (batches, retries, one metadata-only `embedding_call`
record per batch) around a `CappedEmbedder` (each request's worst case reserved against the run's
`--max-usd` before it is sent; ADR 0013 decision 4) around the configured embedder: the fake one
in a dry run, Voyage in shesha's live run.

- **Training rows in batches,** one runner call per business (the runner splits by the model's
  request limits): cheap, and their latency is never reported.
- **Eval rows one per request,** each timed on its own: classifier latency is reported as this
  embedding plus the scoring (plan, "Cost and latency"), and each row's reported tokens give its
  embedding cost.
- Every text is embedded as a `query` (D2), normalised (`cache.normalise_text`), and hashed; the
  text itself is never stored or logged.

**Stops.** `CapReached` (a request the cap refuses) and a cap stop rule (`CostCap.stop`, e.g. a
bound miss) end the embedding: the entries made so far are returned with the reason, and the
command marks the folder `partial`; a partial cache is refused by `train` (`cache_problems`).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from vectapilot_api.embeddings.runner import EmbeddingRunner
from vectapilot_api.evalrun.budget import CapReached, CostCap
from vectapilot_api.intenteval.cache import CacheEntry, encode_vector, normalise_text, text_sha256
from vectapilot_api.intenteval.rows import EvalRow, TrainRow

EMBED_OPERATION: Final = "intent-embed"
"""The operation of every `embedding_call` record this writes."""


@dataclass
class Embedded:
    entries: list[CacheEntry]
    stop: str | None
    """Why the embedding ended early, or None when every row has its vector."""


def _entry(
    row: TrainRow | EvalRow,
    file: str,
    model: str,
    dimension: int,
    vector: Sequence[float],
    latency_ms: float | None = None,
    tokens: int | None = None,
) -> CacheEntry:
    return CacheEntry.model_validate(
        {
            "id": row.id,
            "file": file,
            "text_sha256": text_sha256(row.text),
            "model": model,
            "dimension": dimension,
            "input_type": "query",
            "vector": encode_vector(vector),
            "latency_ms": latency_ms,
            "reported_tokens": tokens,
        }
    )


async def embed_rows(
    runner: EmbeddingRunner,
    cap: CostCap,
    train: Sequence[TrainRow],
    eval_rows: Sequence[EvalRow],
    *,
    clock: Callable[[], float],
    progress: Callable[[str], None] = lambda line: None,
) -> Embedded:
    """Embed every training row (batched per business), then every eval row (one per request).
    `CapReached` and a cap stop end it early (module docstring); anything else propagates."""
    embedded = Embedded(entries=[], stop=None)
    businesses = list(dict.fromkeys(r.business_id for r in train))
    try:
        for business in businesses:
            rows = [r for r in train if r.business_id == business]
            result = await runner.embed(
                [normalise_text(r.text) for r in rows],
                input_type="query",
                business_id=business,
                operation=EMBED_OPERATION,
            )
            for train_row, vector in zip(rows, result.vectors, strict=True):
                entry = _entry(train_row, "train", result.model, result.dimension, vector)
                embedded.entries.append(entry)
            progress(f"train {business}: {len(rows)} rows in {result.calls} request(s)")
            if cap.stop is not None:
                embedded.stop = cap.stop
                return embedded
        for number, row in enumerate(eval_rows, start=1):
            started = clock()
            result = await runner.embed(
                [normalise_text(row.text)],
                input_type="query",
                business_id=row.business_id,
                operation=EMBED_OPERATION,
            )
            latency = max(0.0, (clock() - started) * 1000)
            embedded.entries.append(
                _entry(
                    row,
                    "eval",
                    result.model,
                    result.dimension,
                    result.vectors[0],
                    latency_ms=latency,
                    tokens=result.total_tokens,
                )
            )
            if number % 20 == 0 or number == len(eval_rows):
                progress(f"eval: {number} of {len(eval_rows)} rows")
            if cap.stop is not None:
                embedded.stop = cap.stop
                return embedded
    except CapReached as reached:
        embedded.stop = str(reached)
    return embedded
