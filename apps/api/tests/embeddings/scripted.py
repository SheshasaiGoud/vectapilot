"""Test doubles for embedding calls: an embedder that fails or answers as scripted, and a clock."""

import dataclasses
from collections.abc import Iterable, Sequence

from vectapilot_api.embeddings.base import Embeddings, EmbeddingSpec, InputType
from vectapilot_api.embeddings.catalog import load_embedding_catalog
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.gateway.types import BusinessId, RetryPolicy

FAKE_SPEC = load_embedding_catalog().resolve("fake", None, 1024)
POLICY = RetryPolicy(max_retries=2, base_delay_s=0.5, max_delay_s=8.0)


def small_spec(**overrides: int) -> EmbeddingSpec:
    """The fake model with small request limits, to force several batches."""
    return FAKE_SPEC.model_copy(update=overrides)


class ScriptedEmbedder:
    """The fake embedder, except that call k raises `script[k]` (None = answer normally) and
    answers with `answer_models[k]` as its model id when given. `batches` records every call."""

    def __init__(
        self,
        spec: EmbeddingSpec = FAKE_SPEC,
        script: Iterable[BaseException | None] = (),
        *,
        answer_models: Iterable[str] = (),
        reported_tokens: int = 0,
    ) -> None:
        self._fake = FakeEmbedder(spec)
        self._script = list(script)
        self._answer_models = list(answer_models)
        self._reported_tokens = reported_tokens
        self.batches: list[list[str]] = []
        self.closed = False

    @property
    def model(self) -> str:
        return self._fake.model

    @property
    def dimension(self) -> int:
        return self._fake.dimension

    async def embed(
        self, texts: Sequence[str], *, input_type: InputType, business_id: BusinessId
    ) -> Embeddings:
        self.batches.append(list(texts))
        step = self._script.pop(0) if self._script else None
        if step is not None:
            raise step
        result = await self._fake.embed(texts, input_type=input_type, business_id=business_id)
        model = self._answer_models.pop(0) if self._answer_models else result.model
        return dataclasses.replace(result, model=model, total_tokens=self._reported_tokens)

    async def aclose(self) -> None:
        self.closed = True


class FakeClock:
    """`clock` and `sleep` for the runner: sleeping advances the clock, nothing waits."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
