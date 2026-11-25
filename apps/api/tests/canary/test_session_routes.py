"""The session attack tests through PR 1's real routes (M8 PR 1; S1, S3, S5, ADR 0019 decision 8).
Integration only, under tests/canary/ so the canary gate's no-skip rule covers them.

`test_sessions.py` runs the auth dependencies on probe routes; this file runs the routes a browser
calls, against Postgres as the app role and the real Redis limiter (session creation's window;
staff login has none, step B3), on two planted businesses (`test_sessions.plant`, with migration
0008's bait):

- **Writes land in their own business only:** a new customer session and a staff login each add
  exactly one row to the actor's business, and the other business's rows are byte for byte the
  same afterwards (the positive control is the actor's new row).
- **Token-prefix tampering:** a new session's token with the other business's prefix is the one
  401, and revokes nothing.
- **Session X against session Y in one business:** ending X leaves Y live; logging out one staff
  session leaves the other.
- **Header and body smuggling (S1):** `X-Business-Id` is a 400 and a body naming
  `customer_ref`, `business_id` or anything else is a 422; neither writes or uses anything.
- **The wrong kind (S1):** a customer token as the staff cookie and a staff token as a bearer
  token are the one 401 on the real routes.
- **CSRF (S5):** a login from another origin is a 403 and leaves its link unused.
- **The login-link race (S5):** two parallel logins with one link, both waiting on the same row
  lock: exactly one 204 and one 401, and exactly one new staff session.
- **A disabled member's link** (B1's proposal, tightened by zara's review): it is used up, the
  login is the one 401 with no cookie, and no session row is added.
- **An expired link** (zara's review): the same 401 bytes, the link still unused, no session row.

Every planted business is removed afterwards (`sessions.SESSION_UPROOT`, which also removes the
sessions these routes add).
"""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.checks import Planted
from tests.canary.sessions import (
    BAIT_DISABLED_STAFF_ID,
    BAIT_STAFF_ID,
    SESSION_UPROOT,
    Purpose,
    bait_token,
)
from tests.canary.test_sessions import app_url, owner_url, plant
from tests.conftest import integration
from tests.tools.helpers import write_corpus
from vectapilot_api.auth.cookies import STAFF_COOKIE
from vectapilot_api.auth.dependencies import UNAUTHORIZED
from vectapilot_api.auth.tokens import make_token, token_sha256
from vectapilot_api.config import Settings
from vectapilot_api.db import business_transaction
from vectapilot_api.main import create_app
from vectapilot_api.staff.owner import issue_link

pytestmark = [integration, pytest.mark.anyio]

ORIGIN: Final = "https://api.vectapilot.example"
SAME_ORIGIN: Final = {"origin": ORIGIN}
UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
WAIT_S: Final = 0.3
"""How long both racing logins are given to block on the held row lock (a floor, not a
measurement: a slower database only makes the wait longer, never the outcome different)."""
LOOKUP_TIMEOUT_S: Final = 2.5
"""Above `WAIT_S` plus the work, so the held lock never turns a racing login into S14's 503; with
the hand-off store's default 2 s, below the widget's 5 s client margin (M8 PR 2's timing rule,
`config.HANDOFF_TIMING_RULE`)."""
TABLES: Final = ("sessions", "staff_login_links", "staff_users")


@pytest.fixture
async def owner_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(app_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def planted(
    owner_engine: AsyncEngine, app_engine: AsyncEngine
) -> AsyncIterator[tuple[Planted, Planted]]:
    seeded: list[Planted] = []
    try:
        seeded.append(await plant(owner_engine, app_engine, "a"))
        seeded.append(await plant(owner_engine, app_engine, "b"))
        yield seeded[0], seeded[1]
    finally:
        for business in seeded:
            async with business_transaction(owner_engine, business.business_id) as connection:
                for statement in (
                    *SESSION_UPROOT,
                    text("DELETE FROM businesses WHERE business_id = :b"),
                ):
                    await connection.execute(statement, {"b": business.business_id})


def widget_file(business_id: str) -> dict[str, Any]:
    """A minimal synthetic widget for a planted (`retail`) business: no guidance needed (S8)."""
    return {
        "business_id": business_id,
        "synthetic": True,
        "review": "synthetic test widget (M8 PR 1 canary)",
        "enabled": True,
        "display_name": "A canary business",
        "allowed_origins": ["https://host.example"],
        "languages": ["en"],
        "texts": [{"language": "en", "greeting": "Hello.", "contact_fallback": ["Phone: none"]}],
    }


@contextmanager
def served(*business_ids: str) -> Iterator[TestClient]:
    """The real app as the app role (Settings from the environment, as CI), with a widget file and
    a `retail` corpus manifest per planted business, and the API's origin set. The folders this
    file does not need are switched off, so a developer's `.env` cannot change the app."""
    with tempfile.TemporaryDirectory() as directory:
        corpus = write_corpus(Path(directory) / "corpus", dict.fromkeys(business_ids, "retail"))
        folder = Path(directory) / "widget"
        (folder / "businesses").mkdir(parents=True)
        for business_id in business_ids:
            (folder / "businesses" / f"{business_id}.json").write_text(
                json.dumps(widget_file(business_id)), encoding="utf-8"
            )
        settings = Settings(
            widget_config_dir=folder,
            corpus_dir=corpus,
            tools_config_dir=None,
            extraction_config_dir=None,
            guard_config_dir=None,
            public_origin=ORIGIN,
            session_lookup_timeout_seconds=LOOKUP_TIMEOUT_S,
        )
        with TestClient(create_app(settings), base_url="https://testserver") as client:
            yield client


async def rows(engine: AsyncEngine, business_id: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of 0008's tables for `business_id`, as the owner (in a stable order)."""
    found: dict[str, list[tuple[Any, ...]]] = {}
    async with business_transaction(engine, business_id) as connection:
        for table in TABLES:
            result = await connection.execute(
                text(f"SELECT * FROM {table} WHERE business_id = :b ORDER BY id"),  # noqa: S608
                {"b": business_id},
            )
            found[table] = [tuple(row) for row in result]
    return found


def new_session(client: TestClient, business_id: str, **extra: Any) -> httpx2.Response:
    return client.post(
        "/v1/widget/sessions",
        json={"business_id": business_id, "ui_language": "en", **extra},
        headers=SAME_ORIGIN,
    )


def end(client: TestClient, token: str, **headers: str) -> httpx2.Response:
    return client.delete(
        "/v1/widget/session", headers={"authorization": f"Bearer {token}", **headers}
    )


def login(client: TestClient, token: str, **headers: str) -> httpx2.Response:
    client.cookies.clear()  # every request names its own credential
    return client.post("/v1/staff/login", json={"token": token}, headers={**SAME_ORIGIN, **headers})


def staff_cookie(response: httpx2.Response) -> str:
    """The new session's token from a login's `Set-Cookie`."""
    [header] = response.headers.get_list("set-cookie")
    name, _, rest = header.partition("=")
    assert name == STAFF_COOKIE
    return rest.partition(";")[0]


def me(client: TestClient, token: str) -> httpx2.Response:
    client.cookies.clear()
    return client.get("/v1/staff/me", headers={"cookie": f"{STAFF_COOKIE}={token}"})


def logout(client: TestClient, token: str) -> httpx2.Response:
    client.cookies.clear()
    return client.post(
        "/v1/staff/logout", headers={**SAME_ORIGIN, "cookie": f"{STAFF_COOKIE}={token}"}
    )


def edited(token: str, business_id: str) -> str:
    return f"{business_id}.{token.rpartition('.')[2]}"


def added(before: Sequence[tuple[Any, ...]], after: Sequence[tuple[Any, ...]]) -> list[Any]:
    return [row for row in after if row not in before]


async def link_used(engine: AsyncEngine, business_id: str, token: str) -> bool:
    async with business_transaction(engine, business_id) as connection:
        used = await connection.scalar(
            text(
                "SELECT used_at IS NOT NULL FROM staff_login_links"
                " WHERE business_id = :b AND token_sha256 = :h"
            ),
            {"b": business_id, "h": token_sha256(token)},
        )
    assert used is not None, "no such link"
    return bool(used)


async def fresh_link(engine: AsyncEngine, business_id: str, staff_id: Any) -> str:
    """A new link for an enabled member, issued by the owner CLI's own statement."""
    async with business_transaction(engine, business_id) as connection:
        issued = await issue_link(connection, business_id, staff_id, lifetime_s=900)
    assert issued is not None
    return issued.token


# ── Writes land in their own business only ───────────────────────────────────────────────────


async def test_a_new_session_lands_in_its_business_only_and_its_token_opens_only_it(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    before_a, before_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )

    with served(a.business_id, b.business_id) as client:
        created = new_session(client, a.business_id)
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        moved = end(client, edited(token, b.business_id))
        after_moved_b = await rows(owner_engine, b.business_id)
        ended = end(client, token)
        again = end(client, token)

    after_a, after_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )
    # The positive control: exactly one new customer session in A, holding the hash only.
    [row] = added(before_a["sessions"], after_a["sessions"])
    columns = dict(zip(("business_id", "id", "kind"), row[:3], strict=True))
    assert columns["business_id"] == a.business_id and columns["kind"] == "customer"
    assert token_sha256(token) in row and token not in str(row)
    assert after_b == before_b == after_moved_b  # B, byte for byte, throughout
    assert (moved.status_code, moved.json()) == (401, UNAUTHORIZED_BODY)
    assert ended.status_code == 204
    assert (again.status_code, again.json()) == (401, UNAUTHORIZED_BODY)


async def test_the_database_sets_the_lifetime(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    with served(a.business_id) as client:
        created = new_session(client, a.business_id)
        lifetime = created.json()["lifetime_seconds"]

    async with business_transaction(owner_engine, a.business_id) as connection:
        span = await connection.scalar(
            text("SELECT expires_at - created_at FROM sessions WHERE token_sha256 = :h"),
            {"h": token_sha256(created.json()["token"])},
        )
    assert span == timedelta(seconds=lifetime)


async def test_ending_session_x_leaves_session_y_of_the_same_business_live(
    planted: tuple[Planted, Planted],
) -> None:
    a, _ = planted
    with served(a.business_id) as client:
        x = new_session(client, a.business_id).json()["token"]
        y = new_session(client, a.business_id).json()["token"]
        ended_x = end(client, x)
        x_again = end(client, x)
        ended_y = end(client, y)

    assert (ended_x.status_code, x_again.status_code, ended_y.status_code) == (204, 401, 204)


# ── S1: header and body smuggling ────────────────────────────────────────────────────────────


async def test_the_business_header_and_closed_bodies_write_nothing(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    link = bait_token(a.business_id, Purpose.LINK)
    before = [await rows(owner_engine, p.business_id) for p in planted]

    with served(a.business_id, b.business_id) as client:
        header = client.post(
            "/v1/widget/sessions",
            json={"business_id": a.business_id, "ui_language": "en"},
            headers={**SAME_ORIGIN, "X-Business-Id": b.business_id},
        )
        smuggled = [
            new_session(client, a.business_id, customer_ref="cust-001"),
            new_session(client, a.business_id, session_id="0f0e0d0c-0b0a-4908-8706-050403020100"),
            client.post(
                "/v1/staff/login",
                json={"token": link, "business_id": b.business_id},
                headers=SAME_ORIGIN,
            ),
        ]
        live = bait_token(a.business_id, Purpose.CUSTOMER)
        header_on_end = end(client, live, **{"X-Business-Id": a.business_id})
        still_live = end(client, live)

    assert header.status_code == 400 and header_on_end.status_code == 400
    assert [response.status_code for response in smuggled] == [422, 422, 422]
    for response in smuggled:
        assert "cust-001" not in response.text and b.business_id not in response.text
    assert still_live.status_code == 204  # the 400 revoked nothing
    after = [await rows(owner_engine, p.business_id) for p in planted]
    # Nothing written by the refused requests: only the one revocation, in A's sessions.
    assert after[1] == before[1]
    assert after[0]["staff_login_links"] == before[0]["staff_login_links"]
    assert len(after[0]["sessions"]) == len(before[0]["sessions"])
    assert not await link_used(owner_engine, a.business_id, link)


# ── S1: the wrong kind on the real routes ────────────────────────────────────────────────────


async def test_each_surfaces_token_is_the_one_401_on_the_other_surface(
    planted: tuple[Planted, Planted],
) -> None:
    a, _ = planted
    customer = bait_token(a.business_id, Purpose.CUSTOMER)
    with served(a.business_id) as client:
        staff = staff_cookie(login(client, bait_token(a.business_id, Purpose.LINK)))
        customer_as_cookie = me(client, customer)
        staff_as_bearer = end(client, staff)
        staff_own = me(client, staff)
        customer_own = end(client, customer)

    assert customer_as_cookie.json() == UNAUTHORIZED_BODY
    assert staff_as_bearer.json() == UNAUTHORIZED_BODY
    assert staff_own.status_code == 200 and customer_own.status_code == 204  # positive controls
    assert staff_own.json()["display_name"] == a.canary


# ── S5: login, CSRF, logout ──────────────────────────────────────────────────────────────────


async def test_a_login_lands_in_the_links_business_and_its_cookie_opens_only_that(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, b = planted
    link = bait_token(a.business_id, Purpose.LINK)
    before_a, before_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )

    with served(a.business_id, b.business_id) as client:
        response = login(client, link)
        cookie = staff_cookie(response)
        own = me(client, cookie)
        moved = me(client, edited(cookie, b.business_id))
        second = login(client, link)

    assert response.status_code == 204, response.text
    assert cookie != link
    assert (own.status_code, own.json()["business_id"]) == (200, a.business_id)
    assert own.json()["display_name"] == a.canary
    assert (moved.status_code, moved.json()) == (401, UNAUTHORIZED_BODY)
    assert (second.status_code, second.json()) == (401, UNAUTHORIZED_BODY)  # single use
    after_a, after_b = (
        await rows(owner_engine, a.business_id),
        await rows(owner_engine, b.business_id),
    )
    assert after_b == before_b
    [new] = added(before_a["sessions"], after_a["sessions"])
    assert token_sha256(cookie) in new and cookie not in str(new)
    assert await link_used(owner_engine, a.business_id, link)


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"origin": "https://evil.example"}, id="another origin"),
        pytest.param({"origin": ""}, id="an empty origin"),
        pytest.param({"sec-fetch-site": "cross-site"}, id="a cross-site fetch"),
    ],
)
async def test_login_csrf_is_refused_and_leaves_the_link_unused(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine, headers: dict[str, str]
) -> None:
    a, _ = planted
    link = bait_token(a.business_id, Purpose.LINK)
    with served(a.business_id) as client:
        client.cookies.clear()
        refused = client.post(
            "/v1/staff/login", json={"token": link}, headers={**SAME_ORIGIN, **headers}
        )
        form = client.post(
            "/v1/staff/login",
            content=f"token={link}".encode(),
            headers={**SAME_ORIGIN, "content-type": "application/x-www-form-urlencoded"},
        )
        assert not await link_used(owner_engine, a.business_id, link)
        accepted = login(client, link)  # the positive control: the same link, same origin

    assert refused.status_code == 403 and form.status_code == 415
    assert "set-cookie" not in refused.headers and "set-cookie" not in form.headers
    assert accepted.status_code == 204


async def test_logging_out_one_staff_session_leaves_the_other(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    second_link = await fresh_link(owner_engine, a.business_id, BAIT_STAFF_ID)
    with served(a.business_id) as client:
        first = staff_cookie(login(client, bait_token(a.business_id, Purpose.LINK)))
        second = staff_cookie(login(client, second_link))
        out = logout(client, first)
        first_after = me(client, first)
        second_after = me(client, second)

    assert out.status_code == 204
    assert (first_after.status_code, first_after.json()) == (401, UNAUTHORIZED_BODY)
    assert second_after.status_code == 200


PLANT_LINK: Final = text(
    "INSERT INTO staff_login_links (business_id, staff_id, token_sha256, created_at, expires_at)"
    " VALUES (:b, :staff_id, :h, now() - make_interval(mins => :age),"
    " now() - make_interval(mins => :age) + interval '15 minutes')"
)
"""A link planted by the owner, `age` minutes old (15 minutes of life, P1)."""


async def plant_link(engine: AsyncEngine, business_id: str, staff_id: Any, *, age: int) -> str:
    link = make_token(business_id)
    async with business_transaction(engine, business_id) as connection:
        await connection.execute(
            PLANT_LINK,
            {"b": business_id, "staff_id": staff_id, "h": token_sha256(link), "age": age},
        )
    return link


@pytest.mark.parametrize(
    ("staff_id", "age", "used"),
    [
        pytest.param(BAIT_DISABLED_STAFF_ID, 0, True, id="a disabled member's link"),
        pytest.param(BAIT_STAFF_ID, 20, False, id="an expired link"),  # expired 5 minutes ago
    ],
)
async def test_a_disabled_members_or_an_expired_link_is_the_one_401_and_opens_no_session(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine, staff_id: Any, age: int, used: bool
) -> None:
    # Zara's review: a disabled member's link is used up (one-time) but stores no session and
    # sets no cookie; an expired link is not used at all. Both are the one 401, byte for byte.
    a, _ = planted
    link = await plant_link(owner_engine, a.business_id, staff_id, age=age)
    before = await rows(owner_engine, a.business_id)
    with served(a.business_id) as client:
        response = login(client, link)
        unknown = login(client, make_token(a.business_id))  # a link that does not exist

    assert (response.status_code, response.json()) == (401, UNAUTHORIZED_BODY)
    assert (response.content, response.headers.get("cache-control")) == (
        unknown.content,
        unknown.headers.get("cache-control"),
    )
    assert "set-cookie" not in response.headers
    after = await rows(owner_engine, a.business_id)
    assert after["sessions"] == before["sessions"]  # no session row, for anyone
    assert await link_used(owner_engine, a.business_id, link) is used


# ── S5: two parallel logins with one link ────────────────────────────────────────────────────


async def test_two_parallel_logins_with_one_link_open_exactly_one_session(
    planted: tuple[Planted, Planted], owner_engine: AsyncEngine
) -> None:
    a, _ = planted
    link = bait_token(a.business_id, Purpose.LINK)
    before = await rows(owner_engine, a.business_id)

    with served(a.business_id) as client:

        def attempt() -> httpx2.Response:
            # No cookie jar: each attempt is its own browser.
            return client.post(
                "/v1/staff/login",
                json={"token": link},
                headers={**SAME_ORIGIN, "cookie": "unrelated=1"},
            )

        async with business_transaction(owner_engine, a.business_id) as holder:
            # Hold the link's row lock, so both logins are in flight on the same row at once.
            await holder.execute(
                text(
                    "SELECT 1 FROM staff_login_links WHERE business_id = :b AND token_sha256 = :h"
                    " FOR UPDATE"
                ),
                {"b": a.business_id, "h": token_sha256(link)},
            )
            racing = asyncio.gather(asyncio.to_thread(attempt), asyncio.to_thread(attempt))
            await asyncio.sleep(WAIT_S)
            blocked = not racing.done()
        responses = await racing  # the lock is released: one wins, the other finds it used

    assert blocked
    assert sorted(response.status_code for response in responses) == [204, 401]
    [loser] = [response for response in responses if response.status_code == 401]
    assert loser.json() == UNAUTHORIZED_BODY and "set-cookie" not in loser.headers
    after = await rows(owner_engine, a.business_id)
    new = added(before["sessions"], after["sessions"])
    assert len(new) == 1  # exactly one staff session
    [winner] = [response for response in responses if response.status_code == 204]
    assert token_sha256(staff_cookie(winner)) in new[0]
    assert await link_used(owner_engine, a.business_id, link)
