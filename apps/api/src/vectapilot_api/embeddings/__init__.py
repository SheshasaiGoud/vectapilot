"""Embeddings (ADR 0008): chunk and question texts in, unit vectors out.

- `base.py` — the `Embedder` protocol, the result, request limits (batching) and the response
  checks every embedder runs.
- `catalog.py` + `models.toml` — model ids, dimensions and request limits (no model id elsewhere).
- `fake.py` — the deterministic, offline embedder for tests and CI (`test`/`ci` allow only it).
- `voyage.py` — the Voyage AI adapter over plain REST (`httpx2`): the only code that knows the URL.
- `guard.py` — search refuses vectors of mixed model ids; a model change means a reindex.
- `factory.py` — `build_embedder(settings)`.
"""
