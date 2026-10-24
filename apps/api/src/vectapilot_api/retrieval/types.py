"""Typed values of retrieval (M2 H1, M3): the question, one search hit, and one fused hit.

Every model is a `GatewayModel` (`gateway/types.py`): frozen, closed, validated however it is built
(`model_construct` is refused), and a `ValidationError` never shows a value
(`hide_input_in_errors`). The question's text and vector and a chunk's text stay out of `repr`.

- `VectorQuery` (M2 H1) and `TextQuery` (M3 H1) hold every check that needs no database, so
  `vector_search` and `text_search` only ever receive a valid request: a known-good business id,
  exactly `STORED_DIMENSION` finite numbers of unit length (ADR 0008), an id-shaped model id, a
  bounded `k` and a reader; `TextQuery` also the question's text, checked like the API's
  `Question`. Both share one base, so the two legs of a hybrid search check exactly the same.
  What the database decides (which rows qualify, their order) is each leg's SQL.
- `SearchHit` is one retrieved chunk. Its `score` is a cosine similarity whichever leg found it.
- `FusedHit` (M3 H2) is one hit of a fused list: the unchanged `SearchHit`, its RRF score and its
  rank in each input list. It has no `score` of its own, on purpose (ADR 0014 decision 4).
"""

import math
import uuid
from collections.abc import Mapping
from fractions import Fraction
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field, StringConstraints, field_validator

from vectapilot_api.embeddings.base import STORED_DIMENSION, UNIT_NORM_TOLERANCE
from vectapilot_api.gateway.types import BusinessId, GatewayModel, OpaqueId
from vectapilot_api.kb.types import Audience, ContentKind

MAX_K: Final = 50
"""The most hits one search may ask for (decided by shesha, 2026-10-22): PR 6's prompt needs about
5, M3's reranker may want 20-50 candidates, and a cap stops one request from reading a whole
knowledge base. Exact search scans every candidate whatever `k` is."""

MAX_QUESTION_CHARS: Final = 2000
"""The longest question accepted, in characters, counted before stripping. A platform default, not
measured: generous for a customer's question, far below the embedding model's context (32,000
tokens, ADR 0008), and it bounds what one request can put into a prompt."""


def check_question(value: str) -> str:
    """A question with text in it, stripped; encodable as UTF-8 (no lone surrogates, which the
    embedding and model APIs would refuse). Messages never contain the value."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("must contain text, not only whitespace")
    try:
        stripped.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("must be valid Unicode text") from None
    return stripped


Question = Annotated[
    str,
    StringConstraints(min_length=1, max_length=MAX_QUESTION_CHARS),
    AfterValidator(check_question),
]
"""A customer's question as the API accepts it (`GET /v1/kb/search?q=`, `POST /v1/answer`), and as
`TextQuery` holds it. Defined here, not in `search.py` (which re-exports it), since M3 PR 3:
`TextQuery` needs it, and `search.py` imports this module."""

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


class _SearchQuery(GatewayModel):
    """What every search leg receives: one business, the question's vector, how many hits, for
    whom. Not used on its own: `VectorQuery` and `TextQuery` are the two legs' requests."""

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


class VectorQuery(_SearchQuery):
    """One vector search for one business (M2 H1): what to compare, how many hits, for whom."""


class TextQuery(_SearchQuery):
    """One full-text search for one business (M3 H1, ADR 0014 decision 3): `VectorQuery`'s fields
    and the question's text.

    The text is what the chunks are matched and ranked against. The vector is still needed: each
    hit's `score` is its cosine to the question, so ADR 0011's evidence gate keeps comparing a
    cosine, and only chunks of `embedding_model` are candidates, as in the vector leg. A hybrid
    search (PR 4) builds both queries from the same values.
    """

    question: Question = Field(repr=False)
    """The question as `Question` accepts it: stripped, 1 to `MAX_QUESTION_CHARS` characters,
    valid Unicode. Untrusted text: it may hold anything a customer types, tsquery operators
    (`& | ! ( ) :* <-> ' \\`) included. `text_search` binds it unchanged and parses it in SQL."""


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
    Uncalibrated: not a probability, and not comparable across embedding models. Always the
    cosine, whichever leg found the hit: `text_search` ranks by `ts_rank` but returns the cosine
    here (M3, ADR 0014 decision 3)."""
    embedding_model: OpaqueId


_Rank = Annotated[int, Field(ge=1, strict=True)]
"""A 1-based rank in one ranked list (`True` is refused)."""


class FusedHit(GatewayModel):
    """One hit of a fused list (M3 H2, `rrf_fuse`): the hit, its RRF score, its rank in each list.

    **Why a wrapper without a `score`** (ADR 0014 decision 4, D6). An RRF score is a sum of
    `1/(k + rank)` terms: at k = 60, at most 2/61 ≈ 0.033 for two lists, whatever the evidence. It
    is not a cosine and must never meet `rag_min_score` (0.3 in the tests): every answer would be
    "I don't know", or a threshold re-tuned on it would admit a keyword-stuffed chunk with a low
    cosine. So the two numbers live under different names on different objects. `fused.hit.score`
    is the cosine, the only number ADR 0011's gate compares (D7). `fused.rrf_score` orders the
    fused list, and nothing else. `fused.score` does not exist (an `AttributeError`), and a
    `FusedHit` is not a `SearchHit`, so mypy refuses one where a hit is expected.
    """

    hit: SearchHit
    """The hit as its leg returned it, unchanged (the same object when built from one)."""
    rrf_score: Fraction = Field(strict=True, gt=0)
    """Σ 1/(k + rank) over the lists that hold the hit: an exact `Fraction`, never a float (two
    sums of the same terms in another order must tie exactly). Orders the fused list only."""
    ranks: tuple[_Rank | None, ...] = Field(strict=True, min_length=1)
    """`ranks[i]` is the hit's 1-based rank in the i-th list given to `rrf_fuse`, or None when
    that list does not hold it. PR 4 passes (vector, text), so `ranks[0] is None` marks a hit only
    full-text search found (an FTS-only hit, D7)."""

    @field_validator("ranks")
    @classmethod
    def _in_some_list(cls, ranks: tuple[int | None, ...]) -> tuple[int | None, ...]:
        if all(rank is None for rank in ranks):
            raise ValueError("a fused hit has a rank in at least one list")
        return ranks
