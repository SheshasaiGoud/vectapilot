"""Cited answers (M2 PR 6): retrieve a business's approved chunks, answer only from them.

- `prompt.py` — the versioned prompt (`PROMPT_VERSION`): retrieved text in unforgeable blocks.
- `types.py` — the request, the model's schema (`ModelAnswer`), the response, the `rag_answer`
  record and the errors.
- `service.py` — retrieve → abstain without a model call, or prompt → gateway → verify every
  citation (no source, no claim).
- `router.py` — `POST /v1/answer`.

Search itself (`GET /v1/kb/search`) lives in `retrieval/search.py` and `retrieval/router.py`.
"""
