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

**M5 PR 3's confirm route** needs the business's tool configuration, which a planted business
(`canary-<letter>-<hex>`) has no file for: `served_with_tools` writes one into a temporary folder
(`read_paths.bait_tools` plus a minimal synthetic seed) and serves the real app from it, as
`VECTAPILOT_TOOLS_CONFIG_DIR` would. The action it confirms is the one `test_leakage.plant`
creates for the actor (`read_paths.bait_action_id`, with the shared `BAIT_CONFIRM_TOKEN`).
"""

import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import httpx2
from fastapi.testclient import TestClient

from tests.canary.read_paths import (
    BAIT_CONFIRM_TOKEN,
    BAIT_CUSTOMER,
    BAIT_ORDER_ID,
    bait_action_id,
    bait_tools,
)
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.main import create_app
from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE
from vectapilot_api.tools.sandbox import SandboxSeed, SeedOrder

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


def approve_a_new_version(client: TestClient, business_id: str | None) -> httpx2.Response:
    """Approves version 2 of the planted source `canary` (planted approved as version 1): writes
    the new document and its chunks and retires version 1, in one transaction (ADR 0010). The
    other business's `canary` source is approved under the same source id. Under RLS (the app
    role) this proves approving retires none of its rows; the store's own `WHERE business_id` is
    proven on the owner's connection, under the canary gate, by
    `test_approving_retires_only_the_businesss_own_version_where_rls_cannot_help` (M3 PR 3).
    """
    return client.post(
        "/v1/kb/documents",
        content=b"# Approved through the API\n\nThe next version of the canary source.\n",
        params={
            "source_id": "canary",
            "version": "2",
            "language": "en",
            "content_kind": "faq",
            "status": "approved",
            "audience": "public",
        },
        headers=_headers(business_id, **{"Content-Type": "text/markdown"}),
    )


def reindex(client: TestClient, business_id: str | None) -> httpx2.Response:
    """Re-embeds the planted chunk (planted with model id `fake`, not the configured one)."""
    return client.post("/v1/kb/reindex", headers=_headers(business_id))


SEED_CANARY: Final = "canary-confirm-write-path"
"""The minimal seed's canary: a seed needs one (in a label or note); this one is never planted."""


def bait_seed(business_id: str) -> SandboxSeed:
    """A valid, minimal seed for a planted business's tool folder (the reset never reads it)."""
    return SandboxSeed(
        business_id=business_id,
        synthetic=True,
        canary=SEED_CANARY,
        customers=(BAIT_CUSTOMER, "cust-other"),
        slots=(),
        bookings=(),
        orders=(
            SeedOrder(
                id=BAIT_ORDER_ID,
                customer_ref=BAIT_CUSTOMER,
                status="placed",
                items="Canary kibble",
                note=SEED_CANARY,
                placed_at=datetime(2026, 11, 1, 9, tzinfo=UTC),
            ),
        ),
    )


@contextmanager
def served_with_tools(*business_ids: str) -> Iterator[TestClient]:
    """The real app, as the app role, with a tool configuration for each planted business."""
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        for business_id in business_ids:
            (folder / business_id).mkdir()
            (folder / business_id / TOOLS_FILE).write_text(
                bait_tools(business_id).model_dump_json(), encoding="utf-8"
            )
            (folder / business_id / SEED_FILE).write_text(
                bait_seed(business_id).model_dump_json(), encoding="utf-8"
            )
        with TestClient(create_app(Settings(tools_config_dir=folder))) as client:
            yield client


def confirm(
    client: TestClient, action_id: UUID, business_id: str | None, token: str = BAIT_CONFIRM_TOKEN
) -> httpx2.Response:
    return client.post(
        f"/v1/agent/actions/{action_id}/confirm",
        json={"customer_ref": BAIT_CUSTOMER, "confirm_token": token},
        headers=_headers(business_id),
    )


def confirm_the_planted_action(client: TestClient, business_id: str | None) -> httpx2.Response:
    """Confirms the actor's planted `book` action (M5 PR 3): writes a booking of its free bait
    slot, marks the action executed and audits it, in one transaction. The other business holds
    an action under its own id for the same slot id, customer and token."""
    if business_id is None:
        return confirm(client, uuid4(), None)
    with served_with_tools(business_id) as served:
        return confirm(served, bait_action_id(business_id), business_id)


WRITE_PATHS: Final[dict[str, WritePath]] = {
    "POST /v1/agent/actions/{id}/confirm": confirm_the_planted_action,
    "POST /v1/kb/documents": ingest_a_document,
    "POST /v1/kb/documents (approve a new version)": approve_a_new_version,
    "POST /v1/kb/reindex": reindex,
}
