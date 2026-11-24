"""The session attack tests (M8 PR 1; S1, S3, S5, ADR 0019 decision 8). Integration only, under
tests/canary/ so the canary gate's no-skip rule covers them.

Two fictional businesses get migration 0008's bait (`sessions.py`: the same ids and shared hashes
in both, and real tokens per business), and the real auth dependencies run against Postgres as
the app role, through probe routes on the real app (`tests/auth/probes.py`; the session routes
come with PR 1's routes step):

- a token whose business prefix is edited to the other business is the one 401, both ways;
- expired, revoked, the wrong kind (a staff token as a bearer token, a customer token as the
  staff cookie) and a disabled member are the same 401, byte for byte;
- `X-Business-Id` next to a session is a 400 before any lookup (S1);
- S5's link use is atomic: while one transaction holds the link, a second waits and then gets
  nothing; and a link's token with an edited prefix logs no one in.

Each refusal has its positive control on the same app: the business's own live token works.
The businesses are removed afterwards (`sessions.SESSION_UPROOT` first).
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import Final

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.auth.probes import CUSTOMER_PROBE, REVIEWER_PROBE, probe_app
from tests.canary.checks import Planted
from tests.canary.sessions import (
    BAIT_STAFF_ID,
    OWNER_SESSION_PLANT,
    SESSION_UPROOT,
    Purpose,
    bait_token,
    owner_session_params,
    plant_app_sessions,
)
from tests.conftest import integration
from vectapilot_api.auth.cookies import STAFF_COOKIE
from vectapilot_api.auth.dependencies import BUSINESS_HEADER_REFUSED, UNAUTHORIZED
from vectapilot_api.auth.store import UsedLink, use_login_link
from vectapilot_api.auth.tokens import parse_token
from vectapilot_api.config import MigrationSettings, Settings
from vectapilot_api.db import business_transaction

pytestmark = [integration, pytest.mark.anyio]

UNAUTHORIZED_BODY: Final = {"detail": UNAUTHORIZED, "error": "unauthorized"}
WAIT_S: Final = 0.3
"""How long the second link use is given to block on the first's row lock (a floor, not a
measurement: a slower database only makes the wait longer, never the outcome different)."""


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


def app_url() -> str:
    return str(Settings().database_url.get_secret_value())


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


async def plant(owner: AsyncEngine, app: AsyncEngine, letter: str) -> Planted:
    planted = Planted(f"canary-{letter}-{secrets.token_hex(4)}", f"canary-{secrets.token_hex(8)}")
    params = {
        "b": planted.business_id,
        "canary": planted.canary,
        **owner_session_params(planted.business_id),
    }
    async with business_transaction(owner, planted.business_id) as connection:
        await connection.execute(text("INSERT INTO businesses (business_id) VALUES (:b)"), params)
        for statement in OWNER_SESSION_PLANT:
            await connection.execute(statement, params)
    async with business_transaction(app, planted.business_id) as connection:
        await plant_app_sessions(connection, planted.business_id)
    return planted


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


@pytest.fixture
def api() -> Iterator[TestClient]:
    """The real app plus the probes, as the app role (Settings from the environment, as CI)."""
    with TestClient(probe_app(Settings()), base_url="https://testserver") as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def cookie(token: str) -> dict[str, str]:
    return {"cookie": f"{STAFF_COOKIE}={token}"}


def edited(token: str, business_id: str) -> str:
    """`token` with its business prefix replaced by `business_id` (the secret unchanged)."""
    return f"{business_id}.{token.rpartition('.')[2]}"


def same_401(response: httpx2.Response, reference: httpx2.Response) -> bool:
    return (
        response.status_code == 401
        and response.json() == UNAUTHORIZED_BODY
        and (response.content, response.headers.get("www-authenticate"))
        == (reference.content, reference.headers.get("www-authenticate"))
    )


@pytest.mark.parametrize("me", [0, 1], ids=["a's token", "b's token"])
async def test_a_token_with_the_other_businesss_prefix_is_the_one_401(
    planted: tuple[Planted, Planted], api: TestClient, me: int
) -> None:
    # S3, not isolation evidence (zara's review): the hash covers the prefix, so the edited token
    # hashes to a value stored nowhere and would be a 401 even without RLS or a business clause.
    # The isolation evidence is the shared-hash bait (`read_paths.AUTH_PATHS` and its red proofs).
    mine, other = planted[me], planted[1 - me]
    customer = bait_token(mine.business_id, Purpose.CUSTOMER)
    staff = bait_token(mine.business_id, Purpose.STAFF)
    no_bearer, no_cookie = api.get(CUSTOMER_PROBE), api.get(REVIEWER_PROBE)

    own = api.get(CUSTOMER_PROBE, headers=bearer(customer))
    own_staff = api.get(REVIEWER_PROBE, headers=cookie(staff))
    moved = api.get(CUSTOMER_PROBE, headers=bearer(edited(customer, other.business_id)))
    moved_staff = api.get(REVIEWER_PROBE, headers=cookie(edited(staff, other.business_id)))

    # The positive control: the business's own tokens work, and name only it.
    assert own.status_code == 200, own.text
    assert own.json()["business_id"] == mine.business_id
    assert own.json()["customer_ref"] == f"s-{own.json()['session_id'].replace('-', '')}"
    assert own_staff.status_code == 200, own_staff.text
    assert (own_staff.json()["business_id"], own_staff.json()["display_name"]) == (
        mine.business_id,
        mine.canary,
    )
    # The same secret under the other business's prefix is found nowhere.
    assert same_401(moved, no_bearer), moved.text
    assert same_401(moved_staff, no_cookie), moved_staff.text
    for response in (moved, moved_staff):
        assert other.canary not in response.text and other.business_id not in response.text


@pytest.mark.parametrize(
    ("purpose", "probe", "as_cookie"),
    [
        pytest.param(Purpose.EXPIRED, CUSTOMER_PROBE, False, id="expired"),
        pytest.param(Purpose.REVOKED, CUSTOMER_PROBE, False, id="revoked"),
        pytest.param(Purpose.STAFF, CUSTOMER_PROBE, False, id="a staff token as a bearer token"),
        pytest.param(Purpose.CUSTOMER, REVIEWER_PROBE, True, id="a customer token as the cookie"),
        pytest.param(Purpose.DISABLED_STAFF, REVIEWER_PROBE, True, id="a disabled member"),
        pytest.param(Purpose.LINK, REVIEWER_PROBE, True, id="a login link as the cookie"),
    ],
)
async def test_expired_revoked_disabled_and_the_wrong_kind_are_the_one_401(
    planted: tuple[Planted, Planted],
    api: TestClient,
    purpose: Purpose,
    probe: str,
    as_cookie: bool,
) -> None:
    a, _ = planted
    token = bait_token(a.business_id, purpose)
    reference = api.get(probe)

    response = api.get(probe, headers=cookie(token) if as_cookie else bearer(token))

    assert same_401(response, reference), response.text
    live = Purpose.STAFF if as_cookie else Purpose.CUSTOMER
    positive = bait_token(a.business_id, live)
    assert api.get(probe, headers=cookie(positive) if as_cookie else bearer(positive)).is_success


@pytest.mark.parametrize("probe", [CUSTOMER_PROBE, REVIEWER_PROBE])
async def test_the_business_header_next_to_a_session_is_a_400(
    planted: tuple[Planted, Planted], api: TestClient, probe: str
) -> None:
    a, b = planted
    headers = {
        **bearer(bait_token(a.business_id, Purpose.CUSTOMER)),
        **cookie(bait_token(a.business_id, Purpose.STAFF)),
        "X-Business-Id": b.business_id,
    }

    response = api.get(probe, headers=headers)

    assert (response.status_code, response.json()) == (
        400,
        {"detail": BUSINESS_HEADER_REFUSED, "error": "business_header_refused"},
    )


async def test_a_link_is_used_once_while_a_second_use_waits(
    planted: tuple[Planted, Planted], app_engine: AsyncEngine
) -> None:
    a, b = planted
    token = parse_token(bait_token(a.business_id, Purpose.LINK))

    async def second_use() -> UsedLink | None:
        async with business_transaction(app_engine, a.business_id) as connection:
            return await use_login_link(connection, token)

    async with business_transaction(app_engine, a.business_id) as connection:
        won = await use_login_link(connection, token)
        waiting = asyncio.create_task(second_use())
        await asyncio.sleep(WAIT_S)
        blocked = not waiting.done()  # on the first's row lock, until its commit
    lost = await waiting

    assert won == UsedLink(a.business_id, BAIT_STAFF_ID, enabled=True)
    assert lost is None
    assert blocked
    # A link's token with the other business's prefix logs no one in, in either business.
    moved = parse_token(edited(bait_token(b.business_id, Purpose.LINK), a.business_id))
    async with business_transaction(app_engine, a.business_id) as connection:
        assert await use_login_link(connection, moved) is None
