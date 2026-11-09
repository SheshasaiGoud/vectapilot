"""The embedding cache of an `intents` version (M4 PR 4, ADR 0015 decision 4): one query vector per
training and eval row, committed in the `-live-embed` folder so the classifier and the hybrid rows
can be recomputed from the repository at $0, without a paid re-embed.

**One entry per row** (`CacheEntry`, one JSON line each in `embeddings.jsonl`): the row id, which
file it came from, the SHA-256 of the row's normalised text, the model id the provider *answered*,
the dimension and the input type (`query`, D2), the vector, and for eval rows (one row per request,
so each has its own latency) the request's latency and reported tokens. `train` and `run` refuse a
cache that does not match the frozen rows (`cache_problems`): a changed text, a missing or extra
row, another model or dimension.

**The text is never stored**, only its hash: the cache is a vector store of synthetic rows, and
the rows themselves live in the dataset. **Normalisation** is Unicode NFC only (`normalise_text`):
the text sent to the embedder is the normalised text, so the hash names exactly what was embedded,
and a file re-saved in another Unicode form does not look like a changed row.

**float32, base64** (`encode_vector`): about 3 MB for ~560 rows of 1024 values (an estimate), a
third of a JSON float list. Training and scoring both read the decoded float32 values, so the
classifier is trained and scored on the same numbers. Little-endian, fixed by `struct`'s `<`.
"""

import base64
import hashlib
import math
import struct
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Final, Literal

from pydantic import Field, ValidationError

from vectapilot_api.evalset.corpus import EvalModel, Sha256, describe_validation_error
from vectapilot_api.evalset.questions import DatasetError
from vectapilot_api.gateway.types import OpaqueId, Slug
from vectapilot_api.intenteval.rows import EvalRow, TrainRow

CACHE_FILE: Final = "embeddings.jsonl"

RowFile = Literal["train", "eval"]


def normalise_text(text: str) -> str:
    """What is embedded and hashed: the row's text in Unicode NFC (module docstring)."""
    return unicodedata.normalize("NFC", text)


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalise_text(text).encode("utf-8", "surrogatepass")).hexdigest()


def encode_vector(values: Sequence[float]) -> str:
    """The vector as little-endian float32, base64. Refuses a value that is not finite."""
    if not all(math.isfinite(v) for v in values):
        raise ValueError("a vector value is not finite")
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def decode_vector(text: str, dimension: int) -> tuple[float, ...]:
    """The float32 values back as floats. Raises ValueError for the wrong length."""
    raw = base64.b64decode(text.encode("ascii"), validate=True)
    if len(raw) != 4 * dimension:
        raise ValueError("a cached vector does not have its entry's dimension")
    return struct.unpack(f"<{dimension}f", raw)


class CacheEntry(EvalModel):
    """One row's query vector (module docstring)."""

    id: Slug
    file: RowFile
    text_sha256: Sha256
    model: OpaqueId
    """The model id the provider answered (ADR 0008), never only the one requested."""
    dimension: int = Field(gt=0, strict=True)
    input_type: Literal["query"]
    vector: Annotated[str, Field(min_length=1, repr=False)]
    """`encode_vector`'s base64."""
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    """Eval rows: the row's own request (one row per request); None for training rows."""
    reported_tokens: int | None = Field(default=None, ge=0, strict=True)
    """Eval rows: the tokens the provider reported for the row's request (0 for the fake)."""

    def values(self) -> tuple[float, ...]:
        return decode_vector(self.vector, self.dimension)


def render_cache(entries: Iterable[CacheEntry]) -> str:
    return "".join(entry.model_dump_json() + "\n" for entry in entries)


def parse_cache(text: str) -> tuple[CacheEntry, ...]:
    """Every entry of `embeddings.jsonl` (blank lines skipped). Raises `DatasetError` naming the
    bad lines and fields, never a value."""
    entries: list[CacheEntry] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(CacheEntry.model_validate_json(line))
        except ValidationError as error:
            detail = describe_validation_error(error, "(entry)")
            problems.append(f"{CACHE_FILE} line {number} is not a valid entry: {detail}")
    if problems:
        raise DatasetError("\n".join(problems))
    return tuple(entries)


def cache_problems(
    entries: Sequence[CacheEntry],
    train: Sequence[TrainRow],
    eval_rows: Sequence[EvalRow],
    *,
    model: str | None = None,
    dimension: int | None = None,
) -> list[str]:
    """Why this cache does not belong to these rows (empty: it does). Every row needs exactly one
    entry from its own file with its text's hash, no entry may name another row, and every entry
    has one model and one dimension (`model` and `dimension`, when given, must be those). Ids
    only, never a text."""
    problems: list[str] = []
    counts = Counter(e.id for e in entries)
    problems += [
        f"{CACHE_FILE}: row {i} has {n} entries" for i, n in sorted(counts.items()) if n > 1
    ]
    by_id = {e.id: e for e in entries}
    rows: list[tuple[RowFile, TrainRow | EvalRow]] = [("train", r) for r in train]
    rows += [("eval", r) for r in eval_rows]
    for file, row in rows:
        entry = by_id.get(row.id)
        if entry is None:
            problems.append(f"{CACHE_FILE}: {file} row {row.id} has no vector")
        elif entry.file != file:
            problems.append(f"{CACHE_FILE}: row {row.id} is cached as a {entry.file} row")
        elif entry.text_sha256 != text_sha256(row.text):
            problems.append(f"{CACHE_FILE}: row {row.id}'s text changed since it was embedded")
    known = {row.id for _, row in rows}
    problems += [
        f"{CACHE_FILE}: row {i} is not in the dataset" for i in sorted(by_id) if i not in known
    ]
    models = sorted({e.model for e in entries})
    dimensions = sorted({e.dimension for e in entries})
    if len(models) > 1 or len(dimensions) > 1:
        problems.append(f"{CACHE_FILE} mixes models or dimensions: never mixed vectors (ADR 0008)")
    if model is not None and models and models != [model]:
        problems.append(f"{CACHE_FILE} was embedded with another model than {model}")
    if dimension is not None and dimensions and dimensions != [dimension]:
        problems.append(f"{CACHE_FILE} has another dimension than {dimension}")
    return problems


def vectors_by_id(entries: Iterable[CacheEntry]) -> Mapping[str, tuple[float, ...]]:
    """Each entry's decoded vector, by row id."""
    return {entry.id: entry.values() for entry in entries}
