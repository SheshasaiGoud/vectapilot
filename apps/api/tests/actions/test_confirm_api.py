"""M5 PR 3: the confirm route through the real app, as the app role. Integration.

A throwaway business is seeded from the Paws & Co seed and given a tool configuration folder (a
copy of `data/sandbox/` with Paws & Co renamed to it), so the app loads its `tools.json` at
startup exactly as it loads the fictional businesses'. Actions are stored with the real store,
then confirmed over HTTP.

- 200 with the stored result, the same body on every repeat, one booking.
- 409 for a slot another action took first, the same body on a repeat, nothing written.
- **One 404 body (W6):** a wrong token, another customer's reference, an unknown id and an
  expired action get byte-identical responses, and none of them writes.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.actions.db import active_on, audits, written_by
from tests.conftest import integration
from tests.tools.core import PAWS, config_for, context_for
from tests.tools.helpers import copy_sandbox, corpus_matching
from tests.tools.sandbox_db import (
    app_url,
    fresh_engine,
    new_business,
    owner_url,
    seed_business,
    seed_for,
    uproot,
)
from vectapilot_api.actions.pending import PendingActionStore
from vectapilot_api.actions.router import ACTION_NOT_FOUND
from vectapilot_api.actions.types import PendingAction
from vectapilot_api.business import BUSINESS_HEADER
from vectapilot_api.config import Settings
from vectapilot_api.db import Database
from vectapilot_api.main import create_app
from vectapilot_api.tools.executor import ToolExecutor

pytestmark = [integration, pytest.mark.anyio]

SLOT: Final = "S-09"
NOT_FOUND_BODY: Final = {"detail": ACTION_NOT_FOUND, "error": "action_not_found"}


@dataclass
class Served:
    business_id: str
    client: TestClient
    executor: ToolExecutor
    store: PendingActionStore

    async def book(self, customer: str = "cust-001") -> PendingAction:
        context = context_for(self.business_id, customer)
        envelope = await self.executor.propose("book", {"slot_id": SLOT}, context)
        assert envelope.proposal is not None, envelope.observation
        return await self.store.create(envelope.proposal, context)

    def confirm(
        self,
        action_id: Any,
        token: str,
        customer: str = "cust-001",
        business_id: str | None = None,
    ) -> Any:
        return self.client.post(
            f"/v1/agent/actions/{action_id}/confirm",
            json={"customer_ref": customer, "confirm_token": token},
            headers={BUSINESS_HEADER: business_id or self.business_id},
        )


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = fresh_engine(owner_url())
    yield engine
    await engine.dispose()


@pytest.fixture
async def served(owner: AsyncEngine, tmp_path: Path) -> AsyncIterator[Served]:
    business_id = new_business("h")
    folder = copy_sandbox(tmp_path / "sandbox", {"paws-and-co": business_id})
    engine = fresh_engine(app_url())
    database = Database(engine)
    config = config_for(business_id, PAWS)
    try:
        await seed_business(owner, seed_for(business_id, PAWS.seed))
        corpus = corpus_matching(tmp_path / "corpus", folder)
        settings = Settings(tools_config_dir=folder, corpus_dir=corpus)
        with TestClient(create_app(settings)) as client:
            yield Served(
                business_id=business_id,
                client=client,
                executor=ToolExecutor(database, config),
                store=PendingActionStore(database, {business_id: config}, lifetime_s=900),
            )
    finally:
        await engine.dispose()
        await uproot(owner, business_id)


def secret(action: PendingAction) -> str:
    return action.confirm_token.get_secret_value()


async def test_a_confirm_executes_once_and_every_repeat_returns_its_body(
    owner: AsyncEngine, served: Served
) -> None:
    action = await served.book()

    first = served.confirm(action.id, secret(action))
    repeats = [served.confirm(action.id, secret(action)) for _ in range(2)]

    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["status"], body["tool"], body["action_id"]) == ("executed", "book", str(action.id))
    assert body["result"]["slot_id"] == SLOT
    assert [(r.status_code, r.json()) for r in repeats] == [(200, body), (200, body)]
    assert len({first.headers["X-Trace-Id"], *(r.headers["X-Trace-Id"] for r in repeats)}) == 3
    assert len(await active_on(owner, served.business_id, SLOT)) == 1
    assert secret(action) not in first.text


async def test_a_slot_taken_first_is_a_409_and_writes_nothing(
    owner: AsyncEngine, served: Served
) -> None:
    mine, theirs = await served.book("cust-001"), await served.book("cust-002")
    assert served.confirm(mine.id, secret(mine)).status_code == 200

    lost = served.confirm(theirs.id, secret(theirs), customer="cust-002")
    again = served.confirm(theirs.id, secret(theirs), customer="cust-002")

    assert lost.status_code == 409 and lost.json()["failure_reason"] == "slot_unavailable"
    assert (again.status_code, again.json()) == (409, lost.json())
    assert await written_by(owner, served.business_id, theirs) == 0


async def test_every_refusal_is_the_same_404_body_and_writes_nothing(
    owner: AsyncEngine, served: Served
) -> None:
    action, expired = await served.book(), await served.book("cust-002")
    async with owner.begin() as connection:
        await connection.execute(
            text(
                "UPDATE pending_actions SET created_at = now() - interval '2 hours',"
                " expires_at = now() - interval '1 hour' WHERE business_id = :b AND id = :id"
            ),
            {"b": served.business_id, "id": expired.id},
        )
    wrong = "Q7zx9TOKENtokenTOKEN_-"

    refusals = [
        served.confirm(action.id, wrong),
        served.confirm(action.id, secret(action), customer="cust-002"),
        served.confirm(uuid4(), secret(action)),
        served.confirm(expired.id, secret(expired), customer="cust-002"),
    ]

    assert {(r.status_code, r.content) for r in refusals} == {
        (404, json.dumps(NOT_FOUND_BODY, separators=(",", ":")).encode())
    }
    assert all(r.headers["X-Trace-Id"] for r in refusals)
    assert await written_by(owner, served.business_id, action) == 0
    assert await written_by(owner, served.business_id, expired) == 0
    outcomes = sorted(a[1] for a in await audits(owner, served.business_id) if a[0] == "confirm")
    assert outcomes == ["expired", "wrong_token"]  # the other two name no visible action
