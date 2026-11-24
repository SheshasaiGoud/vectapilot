"""The real app serving the widget and staff routes on stand-ins (M8 PR 1): the committed widget
folder and the real guard folder, `FakeLimiter` for Redis and `ScriptedDatabase` for Postgres
(`tests/auth/fakes.py`), so a route test needs neither. The database is replaced after startup,
as the auth dependency tests do."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Final
from uuid import UUID

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.auth.fakes import ScriptedConnection, ScriptedDatabase
from tests.widget.helpers import ORIGIN, widget_settings
from vectapilot_api.config import Settings
from vectapilot_api.guards.fake_limiter import FakeLimiter
from vectapilot_api.main import create_app

SAME_ORIGIN: Final = {"origin": ORIGIN}
"""What the widget page and the dashboard, served by the API (D2 (a)), send on a POST."""
NEW_SESSION_ID: Final = UUID("5e551011-0000-4000-8000-0000000000a1")
EXPIRES_AT: Final = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)
NO_STORE: Final = "no-store"


def inserted(session_id: UUID = NEW_SESSION_ID) -> SimpleNamespace:
    """The row `INSERT_SESSION ... RETURNING id, expires_at` returns."""
    return SimpleNamespace(id=session_id, expires_at=EXPIRES_AT)


@dataclass
class Served:
    client: TestClient
    app: FastAPI
    limiter: FakeLimiter

    def use(self, database: ScriptedDatabase) -> ScriptedDatabase:
        self.app.state.database = database
        return database

    def script(self, *items: list[Any] | BaseException) -> ScriptedDatabase:
        """A database whose statements answer `items`, one each, in order."""
        return self.use(ScriptedDatabase(ScriptedConnection(list(items))))


@contextmanager
def serve(settings: Settings | None = None, limiter: FakeLimiter | None = None) -> Iterator[Served]:
    """The app on https (a `Secure` cookie needs it), started, with an untouched database: any
    statement raises (`ScriptedConnection` with an empty script), so a test that expects none
    proves it by passing."""
    fake = limiter or FakeLimiter()
    app = create_app(settings or widget_settings(), limiter=fake)
    with TestClient(app, base_url="https://testserver") as client:
        served = Served(client, app, fake)
        served.use(ScriptedDatabase())
        yield served


def same_bytes(*responses: httpx2.Response) -> bool:
    """Whether every response has the same status, body and `WWW-Authenticate`."""
    keys = {
        (
            r.status_code,
            r.content,
            r.headers.get("www-authenticate"),
            r.headers.get("cache-control"),
        )
        for r in responses
    }
    return len(keys) == 1
