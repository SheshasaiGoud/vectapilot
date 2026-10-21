"""Never compare vectors of different models (ADR 0008, "Model id per chunk").

Vectors from two models live in different spaces: their dot product means nothing, and a search
over mixed vectors returns confident garbage. So each chunk stores the model id it was embedded with
(`Embeddings.model`, the id the provider returned), and:

- search (PR 6) calls `require_one_model` with the business's stored model ids and the query's
  `Embeddings.model` before it compares anything; H1's SQL also filters on `embedding_model`;
- reindexing (PR 5, `kb/service.py`) re-embeds every chunk whose stored model id is not the
  configured one (its SQL selects them; `needs_reindex` answers the same question for a set).

A model change (a new `VECTAPILOT_EMBEDDINGS_MODEL`) therefore means a full reindex of the business,
never mixed vectors. The dimension needs no check of its own: the column is `vector(1024)` and the
catalog allows no other (`catalog.resolve`).
"""

from collections.abc import Iterable


class ReindexRequired(Exception):
    """The business's chunks carry other model ids than the query: search refuses to run.

    Metadata only: the message gives a count; the ids are attributes for the caller to act on.
    """

    def __init__(self, *, stored_models: frozenset[str], query_model: str) -> None:
        self.stored_models = stored_models
        self.query_model = query_model
        others = len(stored_models - {query_model})
        super().__init__(
            f"{others} stored embedding model id(s) differ from the query's:"
            " reindex the business with one model before searching"
        )


def needs_reindex(*, stored_models: Iterable[str], current_model: str) -> bool:
    """True if any stored chunk was embedded with a model other than `current_model`."""
    return bool(frozenset(stored_models) - {current_model})


def require_one_model(*, stored_models: Iterable[str], query_model: str) -> None:
    """Raise `ReindexRequired` unless every stored chunk carries `query_model`.

    `stored_models` is the business's distinct `chunks.embedding_model` values (under RLS); none
    (an empty knowledge base) passes.
    """
    stored = frozenset(stored_models)
    if needs_reindex(stored_models=stored, current_model=query_model):
        raise ReindexRequired(stored_models=stored, query_model=query_model)
