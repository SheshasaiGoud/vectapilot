"""The owner CLI's statements against Postgres (M8 PR 1; S5, D4 (a), migration 0008). Integration.

`run_with_owner` runs each command in one owner transaction of its business, as
`python -m vectapilot_api.staff` does: add a member, issue a link (only its hash stored, its expiry
from the database's clock), disable the member (their live sessions revoked, other members'
left; a link for them is then refused), and the
refusals the database decides (an unregistered business, a member of another business). A planted
business is removed afterwards, its rows first.
"""

import secrets
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from tests.canary.sessions import SESSION_UPROOT
from tests.conftest import integration
from vectapilot_api.auth.store import SessionKind, StaffRole, insert_session
from vectapilot_api.auth.tokens import make_token, parse_token, token_sha256
from vectapilot_api.config import MigrationSettings
from vectapilot_api.db import business_transaction
from vectapilot_api.staff.cli import Add, Disable, IssueLink, run_with_owner
from vectapilot_api.staff.owner import IssuedLink

pytestmark = [integration, pytest.mark.anyio]

ORIGIN: Final = "https://api.vectapilot.example"


def owner_url() -> str:
    return str(MigrationSettings().migration_database_url.get_secret_value())


@pytest.fixture
async def owner() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(owner_url(), poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def businesses(owner: AsyncEngine) -> AsyncIterator[tuple[str, str]]:
    """Two registered fictional businesses, removed afterwards with their 0008 rows."""
    ids = (f"staffcli-a-{secrets.token_hex(4)}", f"staffcli-b-{secrets.token_hex(4)}")
    try:
        for business_id in ids:
            async with business_transaction(owner, business_id) as connection:
                await connection.execute(
                    text("INSERT INTO businesses (business_id) VALUES (:b)"), {"b": business_id}
                )
        yield ids
    finally:
        for business_id in ids:
            async with business_transaction(owner, business_id) as connection:
                for statement in (
                    *SESSION_UPROOT,
                    text("DELETE FROM businesses WHERE business_id = :b"),
                ):
                    await connection.execute(statement, {"b": business_id})


async def staff_session(engine: AsyncEngine, business_id: str, staff_id: UUID) -> UUID:
    """A live staff session for `staff_id`, stored through `auth.store` (as a login would)."""
    async with business_transaction(engine, business_id) as connection:
        stored = await insert_session(
            connection,
            parse_token(make_token(business_id)),
            kind=SessionKind.STAFF,
            lifetime_s=28_800,
            staff_id=staff_id,
        )
    return stored.session_id


async def revoked(engine: AsyncEngine, business_id: str, session_id: UUID) -> bool:
    async with business_transaction(engine, business_id) as connection:
        value = await connection.scalar(
            text("SELECT revoked_at IS NOT NULL FROM sessions WHERE business_id = :b AND id = :id"),
            {"b": business_id, "id": session_id},
        )
    return bool(value)


async def test_add_issue_and_disable_as_the_owner(
    owner: AsyncEngine, businesses: tuple[str, str]
) -> None:
    a, _ = businesses
    url = owner_url()

    staff_id = await run_with_owner(url, Add(a, "Synthetic Reviewer", StaffRole.REVIEWER))
    assert isinstance(staff_id, UUID)
    issued = await run_with_owner(url, IssueLink(a, staff_id, 900, ORIGIN))

    assert isinstance(issued, IssuedLink)
    assert parse_token(issued.token).business_id == a
    async with business_transaction(owner, a) as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT staff_id, token_sha256, expires_at - created_at AS span, used_at"
                    " FROM staff_login_links WHERE business_id = :b"
                ),
                {"b": a},
            )
        ).one()
        member = (
            await connection.execute(
                text("SELECT display_name, role, disabled_at FROM staff_users WHERE id = :id"),
                {"id": staff_id},
            )
        ).one()
    assert (row.staff_id, row.token_sha256) == (staff_id, token_sha256(issued.token))
    assert (row.span, row.used_at) == (timedelta(seconds=900), None)  # the database's clock
    assert (member.display_name, member.role, member.disabled_at) == (
        "Synthetic Reviewer",
        "reviewer",
        None,
    )

    # Zara's review: disabling revokes the member's live sessions, and only theirs.
    other = await run_with_owner(url, Add(a, "Synthetic Admin", StaffRole.ADMIN))
    assert isinstance(other, UUID)
    sessions = {member: await staff_session(owner, a, member) for member in (staff_id, other)}

    assert await run_with_owner(url, Disable(a, staff_id)) is True
    assert await run_with_owner(url, Disable(a, staff_id)) is False
    assert await run_with_owner(url, IssueLink(a, staff_id, 900, ORIGIN)) is None
    assert {member: await revoked(owner, a, sessions[member]) for member in sessions} == {
        staff_id: True,
        other: False,
    }


async def test_a_member_of_another_business_gets_no_link(businesses: tuple[str, str]) -> None:
    a, b = businesses
    url = owner_url()
    staff_id = await run_with_owner(url, Add(a, "Synthetic Admin", StaffRole.ADMIN))
    assert isinstance(staff_id, UUID)

    assert await run_with_owner(url, IssueLink(b, staff_id, 900, ORIGIN)) is None
    assert await run_with_owner(url, Disable(b, staff_id)) is False
    assert await run_with_owner(url, IssueLink(a, uuid4(), 900, ORIGIN)) is None


async def test_an_unregistered_business_is_refused_by_the_database() -> None:
    with pytest.raises(IntegrityError) as refused:
        await run_with_owner(
            owner_url(), Add(f"staffcli-none-{secrets.token_hex(4)}", "X", StaffRole.REVIEWER)
        )

    assert getattr(refused.value.orig, "sqlstate", None) == "23503"  # the registry's foreign key
