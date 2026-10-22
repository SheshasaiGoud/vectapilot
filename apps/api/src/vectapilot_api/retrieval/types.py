"""Typed values of retrieval (M2 H1): the question as a vector, and one search hit.

Both are `GatewayModel`s (`gateway/types.py`): frozen, closed, validated however they are built
(`model_construct` is refused), and a `ValidationError` never shows a value
(`hide_input_in_errors`). The question's vector and a chunk's text stay out of `repr`.

`VectorQuery` holds every check that needs no database, so `vector_search` (H1) only ever receives
a valid request: a known-good business id, exactly `STORED_DIMENSION` finite numbers of unit length
(ADR 0008), an id-shaped model id, a bounded `k` and a reader. What the database decides (which
rows qualify, their order) is H1's SQL.
"""

import math
import uuid
from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import Field, field_validator

from vectapilot_api.embeddings.base import STORED_DIMENSION, UNIT_NORM_TOLERANCE
from vectapilot_api.gateway.types import BusinessId, GatewayModel, OpaqueId
from vectapilot_api.kb.types import Audience, ContentKind

MAX_K: Final = 50
"""The most hits one search may ask for (decided by shesha, 2026-10-22): PR 6's prompt needs about
5, M3's reranker may want 20-50 candidates, and a cap stops one request from reading a whole
knowledge base. Exact search scans every candidate whatever `k` is."""

Reader = Literal["customer", "staff"]
"""Who will see the hits. `customer` is anyone talking to the business (the widget, PR 6's
`/v1/kb/search`); `staff` is the business's own people (M8, once auth can tell them apart)."""

VISIBLE_AUDIENCES: Final[Mapping[Reader, tuple[Audience, ...]]] = {
    "customer": ("public",),
    "staff": ("public", "staff_only"),
}
"""Which `documents.audience` values each reader may retrieve. Staff-only material is never quoted
to end customers (business-context §3.4)."""

_Component = Annotated[float, Field(strict=True, allow_inf_nan=False)]
"""One vector component: a finite int or float. `True`, `"0.5"`, NaN and infinities are refused."""


class VectorQuery(GatewayModel):
    """One vector search for one business: what to compare, how many hits, for whom."""

    business_id: BusinessId
    embedding: tuple[_Component, ...] = Field(
        strict=True, min_length=STORED_DIMENSION, max_length=STORED_DIMENSION, repr=False
    )
    """The question's vector (`Embeddings.vectors[0]` for `input_type="query"`). A tuple only: a
    list is refused, so a set or a generator can never reorder or exhaust it on the way in."""
    embedding_model: OpaqueId
    """The model id the question was embedded with (`Embeddings.model`); only chunks with the same
    id are compared (ADR 0008)."""
    k: int = Field(ge=1, le=MAX_K, strict=True)
    """At most this many hits."""
    reader: Reader

    @field_validator("embedding")
    @classmethod
    def _unit_length(cls, embedding: tuple[float, ...]) -> tuple[float, ...]:
        # Voyage normalises its vectors (ADR 0008) and `check_vectors` enforces it on every
        # embedder's output. Refusing anything else here also refuses the zero vector, whose cosine
        # distance is undefined (NaN).
        if abs(math.sqrt(math.sumprod(embedding, embedding)) - 1.0) > UNIT_NORM_TOLERANCE:
            raise ValueError("embedding must have length 1 (a unit vector, ADR 0008)")
        return embedding

    @property
    def audiences(self) -> tuple[Audience, ...]:
        """The `documents.audience` values this reader may retrieve (`VISIBLE_AUDIENCES`)."""
        return VISIBLE_AUDIENCES[self.reader]


class SearchHit(GatewayModel):
    """One retrieved chunk: the text, its score, and what a citation needs (PR 6)."""

    chunk_id: uuid.UUID
    """`chunks.id`: what an answer cites (PR 6 checks every cited id was retrieved)."""
    document_id: uuid.UUID
    source_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    language: str = Field(min_length=1)
    """`documents.language` (a BCP 47 tag such as `en` or `hi`, checked at upload), for PR 6's
    citations. Search never filters by it (Q6, cross-language questions, is open)."""
    content_kind: ContentKind
    """`documents.content_kind` (migration 0003's list). The column's text, e.g. `"faq"`, is
    accepted and becomes the `ContentKind` member."""
    page: Annotated[int, Field(ge=1, strict=True)] | None
    """1-based page for paged formats (PDF); None otherwise."""
    chunk_index: int = Field(ge=0, strict=True)
    content: str = Field(min_length=1, repr=False)
    score: float = Field(ge=-1.0, le=1.0, allow_inf_nan=False, strict=True)
    """Cosine similarity, 1 - cosine distance: 1 is the same direction, higher is closer.
    Uncalibrated: not a probability, and not comparable across embedding models."""
    embedding_model: OpaqueId
