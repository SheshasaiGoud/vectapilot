"""The deterministic, offline embedder for tests and CI (ADR 0008). No network, no cost.

Same text ⇒ same unit vector, in every process and on every machine (SHAKE-256 of the UTF-8 text,
never Python's randomised `hash`); different texts ⇒ different vectors. `input_type` does not
change the vector, so a question that repeats a chunk's text finds that chunk first. It plans
batches and checks its vectors exactly like the real adapter, so CI refuses what production would.
The vectors carry no meaning: no quality number is ever reported from them (ADR 0008, PR 8).
"""

import hashlib
import math
import struct
from collections.abc import Sequence

from vectapilot_api.embeddings.base import (
    Embeddings,
    EmbeddingSpec,
    InputType,
    check_business_id,
    check_vectors,
    plan_batches,
)
from vectapilot_api.gateway.types import BusinessId

_UINT32_HALF = 2**31


def fake_vector(text: str, dimension: int) -> tuple[float, ...]:
    """The fake's unit vector for `text`: `dimension` floats from a hash, scaled to length 1."""
    digest = hashlib.shake_256(text.encode("utf-8", "surrogatepass")).digest(4 * dimension)
    raw = [n / _UINT32_HALF - 1.0 for n in struct.unpack(f">{dimension}I", digest)]
    norm = math.sqrt(math.sumprod(raw, raw))
    return tuple(x / norm for x in raw)


class FakeEmbedder:
    """The `Embedder` for `test`/`ci`. `calls` records `(input_type, number of texts)` per call."""

    def __init__(self, spec: EmbeddingSpec) -> None:
        self._spec = spec
        self.calls: list[tuple[InputType, int]] = []

    @property
    def model(self) -> str:
        return self._spec.model

    @property
    def dimension(self) -> int:
        return self._spec.dimension

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: BusinessId
    ) -> Embeddings:
        business = check_business_id(business_id)
        batches = plan_batches(texts, self._spec)
        self.calls.append((input_type, len(texts)))
        vectors = [fake_vector(text, self._spec.dimension) for text in texts]
        return Embeddings(
            business_id=business,
            input_type=input_type,
            model=self._spec.model,
            dimension=self._spec.dimension,
            vectors=check_vectors(vectors, count=len(texts), dimension=self._spec.dimension),
            total_tokens=0,
            requests=len(batches),
        )

    async def aclose(self) -> None:
        """Nothing to release."""
