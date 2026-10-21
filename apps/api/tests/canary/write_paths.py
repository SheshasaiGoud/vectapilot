"""EXTENSION POINT: every API route that writes business data, for the canary suite (ADR 0009).

A write path sends one request through the real app (real Postgres, as the app role) for one
business, or with no `X-Business-Id` when the business is None, and returns the response. The
canary tests in `test_leakage.py` run every entry of `WRITE_PATHS`:

- `test_no_write_path_changes_another_businesss_rows`: acting as one business, the other's rows
  are byte-for-byte the same afterwards, and the path did change the actor's own rows (the
  positive control: a path that writes nothing would pass for the wrong reason). Each path must
  therefore write something for a freshly planted business (`test_leakage.plant`).
- `test_a_write_path_without_a_business_is_refused`: no header ⇒ 400 and no row changes.

Add every new write route here, with no other change (PR 6+, M5 tools, M8 admin).
"""

from collections.abc import Callable
from typing import Final

import httpx2
from fastapi.testclient import TestClient

from vectapilot_api.business import BUSINESS_HEADER

WritePath = Callable[[TestClient, str | None], httpx2.Response]
"""(the API client, the business to send in X-Business-Id or None for none) -> the response."""


def _headers(business_id: str | None, **extra: str) -> dict[str, str]:
    return extra | ({} if business_id is None else {BUSINESS_HEADER: business_id})


def ingest_a_document(client: TestClient, business_id: str | None) -> httpx2.Response:
    """Writes one new document and its chunks."""
    return client.post(
        "/v1/kb/documents",
        content=b"# Written through the API\n\nA canary write path.\n",
        params={
            "source_id": "canary-write-path",
            "version": "1",
            "language": "en",
            "content_kind": "faq",
            "status": "draft",
            "audience": "staff_only",
        },
        headers=_headers(business_id, **{"Content-Type": "text/markdown"}),
    )


def reindex(client: TestClient, business_id: str | None) -> httpx2.Response:
    """Re-embeds the planted chunk (planted with model id `fake`, not the configured one)."""
    return client.post("/v1/kb/reindex", headers=_headers(business_id))


WRITE_PATHS: Final[dict[str, WritePath]] = {
    "POST /v1/kb/documents": ingest_a_document,
    "POST /v1/kb/reindex": reindex,
}
