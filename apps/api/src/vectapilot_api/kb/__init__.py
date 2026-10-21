"""Knowledge-base endpoints (M2 PR 5): ingest a business's documents and reindex its chunks.

- `types.py` — request metadata (`IngestParams`, `ContentKind`), results and typed errors.
- `store.py` — the `KbStore` protocol and its Postgres implementation (one business transaction
  per method, under row-level security).
- `service.py` — load → chunk → embed → store, idempotent per (source id, version); reindex.
- `router.py` — the two routes: the file is the raw request body.
- `errors.py` — the handlers that turn typed errors (and 422s) into bodies that echo no input.
"""
