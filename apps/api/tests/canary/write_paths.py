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
`VECTAPILOT_TOOLS_CONFIG_DIR` would, with a corpus folder holding a minimal manifest of the same
business type (M5 PR 4: `VECTAPILOT_CORPUS_DIR`, W11's type check). The action it confirms is the
one `test_leakage.plant` creates for the actor (`read_paths.bait_action_id`, with the shared
`BAIT_CONFIRM_TOKEN`).

**M5 PR 4's `POST /v1/agent`** runs the agent loop, `run_turn`, shesha's M5 H1. Its service turns
every exception into a hand-off, the stub's `NotImplementedError` included, so the path re-raises
it while `run_turn` is in `PENDING_HAND_WRITE` (`surface_pending_loop`): the canary test is then
the strict xfail `pending_hand_writes` makes (`write_path_params`), and it runs for real the moment
the stub's name is removed. The planted business is served as a `retail` business: W11 hands a
health business off before any tool, so a clinic could never reach the write.
"""

import json
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.canary.read_paths import (
    BAIT_CONFIRM_TOKEN,
    BAIT_CUSTOMER,
    BAIT_ORDER_ID,
    PENDING_HAND_WRITE,
    bait_action_id,
    bait_tools,
    pending_hand_writes,
)
from tests.tools.helpers import write_corpus
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.gateway.providers.base import Provider
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.intents.policy import BusinessType
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
def served_with_tools(
    *business_ids: str,
    business_type: BusinessType = BusinessType.VETERINARY,
    agent_provider: Provider | None = None,
) -> Iterator[TestClient]:
    """The real app, as the app role, with a tool configuration and a corpus manifest of
    `business_type` for each planted business; `agent_provider` scripts the agent's model."""
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory) / "sandbox"
        for business_id in business_ids:
            (folder / business_id).mkdir(parents=True)
            config = bait_tools(business_id).model_copy(update={"business_type": business_type})
            (folder / business_id / TOOLS_FILE).write_text(
                config.model_dump_json(), encoding="utf-8"
            )
            (folder / business_id / SEED_FILE).write_text(
                bait_seed(business_id).model_dump_json(), encoding="utf-8"
            )
        corpus = write_corpus(
            Path(directory) / "corpus", dict.fromkeys(business_ids, business_type.value)
        )
        settings = Settings(tools_config_dir=folder, corpus_dir=corpus)
        with TestClient(create_app(settings, agent_provider=agent_provider)) as client:
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


AGENT_TICKET: Final = "A canary message to staff."
"""The ticket text the agent path's scripted model proposes (synthetic, no canary: a proposal's
row holds the arguments, and the bait canaries must stay the only ones)."""


def post_agent(client: TestClient, business_id: str | None) -> httpx2.Response:
    return client.post(
        "/v1/agent",
        json={"message": "Please pass a message to your staff.", "customer_ref": BAIT_CUSTOMER},
        headers=_headers(business_id),
    )


def surface_pending_loop(response: httpx2.Response) -> None:
    """Re-raise the pending stub's `NotImplementedError` that the agent service turned into a
    hand-off (module docstring); nothing once `run_turn` is written."""
    if "run_turn" not in PENDING_HAND_WRITE or not response.is_success:
        return
    body = response.json()
    if body.get("kind") == "handoff" and body.get("reason") == "agent_error":
        raise NotImplementedError("run_turn (M5 H1) is pending: the agent service handed off")


def propose_through_the_agent(client: TestClient, business_id: str | None) -> httpx2.Response:
    """`POST /v1/agent` (M5 PR 4) with the agent's model scripted to propose `create_ticket`: the
    service stores a pending action with its `stored` audit row, after the proposal's own
    `propose` audit row. Writes nothing else (the ticket waits for a confirm that never comes)."""
    if business_id is None:
        return post_agent(client, None)
    step = {
        "step": {
            "action": "call_tool",
            "tool": "create_ticket",
            "arguments": {"category": "other", "summary": AGENT_TICKET},
        }
    }
    provider = FakeProvider([FakeReply(text=json.dumps(step))])
    with served_with_tools(
        business_id, business_type=BusinessType.RETAIL, agent_provider=provider
    ) as served:
        response = post_agent(served, business_id)
    surface_pending_loop(response)
    return response


WRITE_PATHS: Final[dict[str, WritePath]] = {
    "POST /v1/agent": propose_through_the_agent,
    "POST /v1/agent/actions/{id}/confirm": confirm_the_planted_action,
    "POST /v1/kb/documents": ingest_a_document,
    "POST /v1/kb/documents (approve a new version)": approve_a_new_version,
    "POST /v1/kb/reindex": reindex,
}


WRITE_NEEDS_HAND_WRITE: Final[Mapping[str, frozenset[str]]] = {
    "POST /v1/agent": frozenset({"run_turn"}),
}
"""The [HAND-WRITE] stubs each write path runs (as `read_paths.NEEDS_HAND_WRITE`)."""


def write_path_params(names: Iterable[str] = WRITE_PATHS) -> list[object]:
    """`names` (all of `WRITE_PATHS` by default) for `pytest.mark.parametrize`, sorted; a path on
    a pending stub is marked strict xfail (`read_paths.pending_hand_writes`)."""
    return [
        pytest.param(name, marks=pending_hand_writes(*sorted(WRITE_NEEDS_HAND_WRITE.get(name, ()))))
        for name in sorted(names)
    ]
