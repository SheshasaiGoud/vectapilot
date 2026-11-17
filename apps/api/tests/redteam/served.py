"""The red-team suite's database environment (M7 PR 2): the real app, as the app role, serving the
sample businesses and the fixture business `rt-shop`, once per module (CI time).

`rt-shop` is created as the canary creates its RETAIL businesses (`tests/canary/write_paths.py`):
its tool files and corpus folder come from the dataset's `fixtures/` (`redteam.businesses`).
As the owner role, its `businesses` row is inserted when missing and its sandbox is reseeded from
its `seed.json` through the real reset (`tests/tools/sandbox_db.seed_business`), so its tool
calls see the seed's orders and slots (leo's PR 2 review). Afterwards, only when this fixture
inserted the row, everything of it is removed in the sandbox tests' order (`uproot`: `tool_audit`
and the sandbox tables before `businesses`, which no foreign key cascades to). Nothing is ever
written under `data/`.

Each case scripts its own model: `replay_service` builds an `AgentService` as `create_app` does
but for the loop (the replay stand-in for M5's H1, `redteam.replay`) and the fake provider, and
`serving` overrides the app's `get_agent_service` with it for one case, then removes the override.
A case runs as `customer_for(case)` (`redteam.businesses.customer_ref_for`).

[proposal for PR 6, leo's PR 2 review] This hand-built copy has no router and cannot reach the
read-only service that G13 has `create_app` build. PR 6 adds a test-only `create_app(...,
agent_loop=...)` seam, as `agent_provider` is, that both services receive, and a `guard_dir` in
`AppFolders` (`data/safety/` plus `fixtures/rt-shop/safety.json`), so this module stops
hand-building `AgentService`.
"""

import asyncio
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.answer.apps import gateway_for
from tests.redteam.suite import DATASET, DATASET_DIR
from tests.tools.helpers import CORPUS_DIR, SANDBOX_DIR
from tests.tools.sandbox_db import fresh_engine, owner_url, seed_business, uproot
from vectapilot_api.agent.profiles import build_profiles, business_types
from vectapilot_api.agent.router import get_agent_service
from vectapilot_api.agent.service import AgentService
from vectapilot_api.agent.types import LoopLimits
from vectapilot_api.config import Settings
from vectapilot_api.gateway.providers.fake import FakeProvider, FakeReply
from vectapilot_api.main import create_app
from vectapilot_api.redteam.businesses import (
    AppFolders,
    customer_ref_for,
    fixture_business_tools,
    write_app_folders,
)
from vectapilot_api.redteam.cases import Case
from vectapilot_api.redteam.harness import FilledCase
from vectapilot_api.redteam.replay import agent_steps, replay_loop, step_replies
from vectapilot_api.tools.files import load_tools_dir
from vectapilot_api.tools.sandbox import SandboxSeed

_INSERT: Final = text(
    "INSERT INTO businesses (business_id) VALUES (:b) ON CONFLICT DO NOTHING RETURNING business_id"
)


async def _arrange(seed: SandboxSeed) -> bool:
    """As the owner: insert the business's row when missing, then reseed its sandbox. True when
    this call inserted the row."""
    owner = fresh_engine(owner_url())
    try:
        async with owner.begin() as connection:
            inserted = (await connection.execute(_INSERT, {"b": seed.business_id})).first()
        await seed_business(owner, seed)
        return inserted is not None
    finally:
        await owner.dispose()


async def _remove(business_id: str) -> None:
    owner = fresh_engine(owner_url())
    try:
        await uproot(owner, business_id)
    finally:
        await owner.dispose()


@dataclass(frozen=True)
class ServedSuite:
    client: TestClient
    app: FastAPI
    folders: AppFolders
    seeds: Mapping[str, SandboxSeed]
    """Every served business's sandbox seed, by business id."""

    def customer_for(self, case: Case) -> str:
        return customer_ref_for(case, self.seeds[case.business_id])


def served_suite() -> Iterator[ServedSuite]:
    """The module's served app (module docstring); a generator for a module-scoped fixture."""
    created: list[str] = []
    fixtures = {
        business_id: fixture_business_tools(DATASET_DIR, business_id).seed
        for business_id in DATASET.manifest.fixture_businesses
    }
    with tempfile.TemporaryDirectory() as directory:
        folders = write_app_folders(
            Path(directory),
            dataset_dir=DATASET_DIR,
            sandbox_dir=SANDBOX_DIR,
            corpus_dir=CORPUS_DIR,
            fixture_businesses=DATASET.manifest.fixture_businesses,
        )
        seeds = {
            business_id: t.seed for business_id, t in load_tools_dir(folders.tools_dir).items()
        }
        try:
            for seed in fixtures.values():
                if asyncio.run(_arrange(seed)):
                    created.append(seed.business_id)
            # From the environment, as the canary's served apps: the integration database.
            settings = Settings(tools_config_dir=folders.tools_dir, corpus_dir=folders.corpus_dir)
            app = create_app(settings)
            with TestClient(app) as client:
                yield ServedSuite(client=client, app=app, folders=folders, seeds=seeds)
        finally:
            for business_id in created:
                asyncio.run(_remove(business_id))


def replay_service(
    served: ServedSuite, filled: FilledCase, *, read_only: bool = False
) -> tuple[AgentService, FakeProvider]:
    """An `AgentService` built as `create_app` builds it, but on the replay of `filled`'s steps
    and a fake provider scripted with the same steps (module docstring)."""
    steps = agent_steps(filled.case)
    replies = step_replies(steps, filled.text, read_only=read_only)
    provider = FakeProvider([FakeReply(text=reply) for reply in replies])
    tools = load_tools_dir(served.folders.tools_dir)
    state = served.app.state
    settings = state.settings
    service = AgentService(
        build_profiles(state.database, tools, business_types(tools, served.folders.corpus_dir)),
        gateway=gateway_for(provider),
        store=state.pending_actions,
        answers=state.answer_service,
        limits=LoopLimits(max_steps=settings.agent_max_steps),
        turn_timeout_s=settings.agent_turn_timeout_seconds,
        loop=replay_loop(steps, filled.text, read_only=read_only),
    )
    return service, provider


@contextmanager
def serving(served: ServedSuite, service: AgentService) -> Iterator[None]:
    """`service` answers the app's agent route for one case; the override is always removed."""
    served.app.dependency_overrides[get_agent_service] = lambda: service
    try:
        yield
    finally:
        served.app.dependency_overrides.pop(get_agent_service, None)
