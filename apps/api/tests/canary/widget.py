"""The real app serving the widget routes for planted businesses, as the app role (M8 PR 2a): for
the canary's read path through `GET /v1/widget/handoffs/{id}` (`read_paths.py`) and the widget
route tests (`test_widget_routes.py`). Integration only (the canary checks run the read path
without Postgres or Redis, `tests/test_canary_checks.py`).

Each planted business is served as a `retail` business (no guidance needed, S8) with a minimal
synthetic widget file, a corpus manifest and a guard file (`write_paths.guard_folder`: the real
platform and type term files), the API's origin set, and Settings otherwise from the environment
(as CI). The guards are on: the matcher is `LookupMatcher` (NOT H1; `create_app`'s test seam),
which hits the one platform red flag below for the messages a test names, and the limits are the
test suite's (`read_paths.GUARDED_LIMITS`), on the real Redis.

**PR 2b: businesses with tools** (`served_tool_widgets`), for widget confirm against Postgres
(`test_widget_confirm.py`, `test_tool_customers.py`): each a business seeded from the Paws & Co
seed (`tests.tools.sandbox_db.seeded`) and served with a copy of Paws & Co's `tools.json` under its
own id (`tool_folder`, a `veterinary` business), its guidance in its guard file (a health
business's enabled widget needs it, S8), the same minimal widget file, and the limiter in memory
(Redis is not what these tests are about).
"""

import json
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.canary.write_paths import guard_folder
from tests.guards.agent_helpers import LookupMatcher
from tests.tools.helpers import SANDBOX_DIR, corpus_matching, read_json, write_corpus, write_json
from vectapilot_api.auth.tokens import token_sha256
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.guards.limits import Limiter
from vectapilot_api.main import create_app
from vectapilot_api.tools.config import SEED_FILE, TOOLS_FILE

ORIGIN: Final = "https://api.vectapilot.example"
SAME_ORIGIN: Final = {"origin": ORIGIN}
RED_FLAG_TERM: Final = "pf_this_is_an_emergency"
"""A platform red flag (`data/safety/platform.json`), so every planted business has it."""
REAL_REDIS_BOUNDS: Final[dict[str, Any]] = {
    "guard_redis_timeout_seconds": 1.0,
    "readiness_timeout_seconds": 1.0,
    "agent_turn_timeout_seconds": 40.0,
}
"""Real Redis on a loaded CI runner (under coverage) can take longer than the 0.25 s default, and
the limiter then fails closed (a typed 503): correct in production, a flake here (PR 4's CI run,
2026-11-25). Test-only; Settings' timing rules still hold (arithmetic): readiness >= Redis; the
turn 8 x 4 + 2 x 4 x 1.0 = 40 <= 40; router + turn 15 + 40 = 55 <= 60 and <= the widget's 60 s
bound; confirm 2.5 + 1.0 + 5 + 2 = 10.5 < 15."""

LIMITS: Final[dict[str, Any]] = {
    "guard_limit_business_per_minute": 100_000,
    "guard_limit_customer_per_minute": 100_000,
    "guard_budget_usd_per_business_per_day": "100",
    "session_limit_business_per_minute": 100_000,
    "handoff_poll_limit_business_per_minute": 100_000,
    "handoff_poll_limit_session_per_minute": 100_000,
    "session_lookup_timeout_seconds": 2.5,
    "handoff_store_timeout_seconds": 2.0,
    **REAL_REDIS_BOUNDS,
}
"""Far above any test's volume: these tests plant fresh businesses, never fill a window, with
`REAL_REDIS_BOUNDS`."""


def widget_file(business_id: str) -> dict[str, Any]:
    """A minimal synthetic widget for a planted (`retail`) business."""
    return {
        "business_id": business_id,
        "synthetic": True,
        "review": "synthetic test widget (M8 PR 2a canary)",
        "enabled": True,
        "display_name": "A canary business",
        "allowed_origins": ["https://host.example"],
        "languages": ["en"],
        "texts": [{"language": "en", "greeting": "Hello.", "contact_fallback": ["Phone: none"]}],
    }


@contextmanager
def served_widgets(
    business_ids: Iterable[str],
    *,
    red_flags: Iterable[str] = (),
    limiter: Limiter | None = None,
    **overrides: Any,
) -> Iterator[TestClient]:
    """The real app for `business_ids` (module docstring); each message in `red_flags` hits
    `RED_FLAG_TERM`. `limiter` replaces the real Redis one (S14's "Redis down"); `overrides`
    change `LIMITS`' settings (a store timeout)."""
    ids = tuple(business_ids)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = write_corpus(root / "corpus", dict.fromkeys(ids, "retail"))
        folder = root / "widget"
        (folder / "businesses").mkdir(parents=True)
        for business_id in ids:
            (folder / "businesses" / f"{business_id}.json").write_text(
                json.dumps(widget_file(business_id)), encoding="utf-8"
            )
        settings = Settings(
            widget_config_dir=folder,
            corpus_dir=corpus,
            guard_config_dir=guard_folder(root, ids),
            tools_config_dir=None,
            extraction_config_dir=None,
            guard_routes="on",
            public_origin=ORIGIN,
            **(LIMITS | overrides),
        )
        matcher = LookupMatcher({message: {RED_FLAG_TERM} for message in red_flags})
        # Only when given: the canary checks' unit proof wraps `create_app` with its own limiter.
        extra: dict[str, Any] = {} if limiter is None else {"limiter": limiter}
        app = create_app(settings, matcher=matcher, **extra)
        with TestClient(app, base_url="https://testserver") as client:
            yield client


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def new_session(client: TestClient, business_id: str) -> str:
    """A new customer session's token, through the real route."""
    response = client.post(
        "/v1/widget/sessions",
        json={"business_id": business_id, "ui_language": "en"},
        headers=SAME_ORIGIN,
    )
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    assert isinstance(token, str)
    return token


# ── PR 2b: businesses with tools, for widget confirm ─────────────────────────────────────────


def tool_folder(destination: Path, business_ids: Iterable[str]) -> Path:
    """A tool folder with Paws & Co's `tools.json` and seed under each of `business_ids`."""
    for business_id in business_ids:
        folder = destination / business_id
        shutil.copytree(SANDBOX_DIR / "paws-and-co", folder)
        for name in (TOOLS_FILE, SEED_FILE):
            data = read_json(folder / name)
            data["business_id"] = business_id
            write_json(folder / name, data)
    return destination


@contextmanager
def served_tool_widgets(business_ids: Iterable[str]) -> Iterator[TestClient]:
    """The real app for seeded tool businesses (module docstring)."""
    ids = tuple(business_ids)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tools = tool_folder(root / "sandbox", ids)
        guidance = {
            business_id: list(read_json(tools / business_id / TOOLS_FILE)["emergency_guidance"])
            for business_id in ids
        }
        folder = root / "widget"
        (folder / "businesses").mkdir(parents=True)
        for business_id in ids:
            (folder / "businesses" / f"{business_id}.json").write_text(
                json.dumps(widget_file(business_id)), encoding="utf-8"
            )
        settings = Settings(
            widget_config_dir=folder,
            corpus_dir=corpus_matching(root / "corpus", tools),
            guard_config_dir=guard_folder(root, ids, guidance),
            tools_config_dir=tools,
            extraction_config_dir=None,
            guard_routes="on",
            public_origin=ORIGIN,
            **LIMITS,
        )
        app = create_app(settings, matcher=LookupMatcher(), limiter=FakeLimiter())
        with TestClient(app, base_url="https://testserver") as client:
            yield client


async def session_id_of(engine: AsyncEngine, business_id: str, token: str) -> UUID:
    """The session row `token` opens, read by its hash as the owner."""
    async with business_transaction(engine, business_id) as connection:
        found = await connection.scalar(
            text("SELECT id FROM sessions WHERE business_id = :b AND token_sha256 = :h"),
            {"b": business_id, "h": token_sha256(token)},
        )
    assert isinstance(found, UUID), "no such session"
    return found
