"""Search refuses mixed model ids; a model change means a reindex (`embeddings/guard.py`)."""

import pytest

from vectapilot_api.embeddings.base import EmbeddingSpec
from vectapilot_api.embeddings.fake import FakeEmbedder
from vectapilot_api.embeddings.guard import ReindexRequired, needs_reindex, require_one_model

BUSINESS = "sunrise-clinic"


def embedder(model: str) -> FakeEmbedder:
    return FakeEmbedder(
        EmbeddingSpec(
            model=model,
            dimension=1024,
            max_texts_per_request=1_000,
            max_tokens_per_request=320_000,
            context_tokens=32_000,
        )
    )


def test_one_model_passes_and_an_empty_knowledge_base_passes() -> None:
    require_one_model(stored_models=["model-a", "model-a"], query_model="model-a")
    require_one_model(stored_models=[], query_model="model-a")


@pytest.mark.parametrize(
    ("stored", "query"),
    [(["model-a", "model-b"], "model-a"), (["model-a"], "model-b")],
    ids=["mixed-index", "other-model"],
)
def test_search_refuses_mixed_or_other_model_ids(stored: list[str], query: str) -> None:
    with pytest.raises(ReindexRequired) as caught:
        require_one_model(stored_models=stored, query_model=query)

    error = caught.value
    assert (error.stored_models, error.query_model) == (frozenset(stored), query)
    assert str(error) == (
        "1 stored embedding model id(s) differ from the query's: reindex the business with one"
        " model before searching"
    )


@pytest.mark.anyio
async def test_a_model_change_requires_a_reindex() -> None:
    # Chunks stored with model A (each row keeps `Embeddings.model`).
    chunks = ["Opening hours: 9 to 5.", "Vaccinations need an appointment."]
    old = await embedder("model-a").embed(chunks, input_type="document", business_id=BUSINESS)
    stored = {old.model}

    # The configured model changes to B: queries now come back as B, so search refuses.
    new = embedder("model-b")
    query = await new.embed(["When are you open?"], input_type="query", business_id=BUSINESS)
    assert needs_reindex(stored_models=stored, current_model=new.model)
    with pytest.raises(ReindexRequired):
        require_one_model(stored_models=stored, query_model=query.model)

    # Half a reindex is still mixed: refused.
    first = await new.embed(chunks[:1], input_type="document", business_id=BUSINESS)
    with pytest.raises(ReindexRequired):
        require_one_model(stored_models=[first.model, old.model], query_model=query.model)

    # The full reindex re-embeds every chunk with B: search runs again.
    redone = await new.embed(chunks, input_type="document", business_id=BUSINESS)
    stored = {redone.model}
    assert not needs_reindex(stored_models=stored, current_model=new.model)
    require_one_model(stored_models=stored, query_model=query.model)
