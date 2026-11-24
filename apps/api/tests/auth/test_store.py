"""The auth statements without a database: what each one checks in its own SQL, and what each
function makes of a scripted row. Against Postgres: tests/canary/ and tests/test_migrations.py."""

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Final
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.auth.fakes import (
    BUSINESS,
    SESSION_ID,
    STAFF_ID,
    TOKEN,
    ScriptedConnection,
    as_connection,
    customer_row,
    staff_row,
)
from vectapilot_api.auth import store
from vectapilot_api.auth.store import (
    CUSTOMER_REF_PREFIX,
    MAX_LINK_LIFETIME_S,
    MAX_SESSION_LIFETIME_S,
    Channel,
    CustomerSession,
    NewSession,
    SessionKind,
    StaffRole,
    StaffSession,
    UsedLink,
    check_lifetime,
    customer_ref_for,
    find_customer_session,
    find_staff_session,
    insert_session,
    revoke_session,
    use_login_link,
)
from vectapilot_api.auth.tokens import ParsedToken, parse_token, token_sha256
from vectapilot_api.tools.config import CUSTOMER_REF_LENGTH, CUSTOMER_REF_PATTERN

pytestmark = pytest.mark.anyio

PARSED: Final = parse_token(TOKEN)
LOOKUP_CLAUSES: Final = ("expires_at > now()", "revoked_at IS NULL")

# ── What each statement says itself (the owner's connection has no RLS and no policy) ────────


def test_the_customer_lookup_names_business_hash_kind_expiry_and_revocation() -> None:
    sql = store.CUSTOMER_SESSION.text

    assert "business_id = :business_id AND token_sha256 = :token_sha256" in sql
    assert "kind = 'customer'" in sql
    assert all(clause in sql for clause in LOOKUP_CLAUSES)


def test_the_staff_lookup_pairs_the_business_in_its_join_and_needs_an_enabled_member() -> None:
    sql = store.STAFF_SESSION.text

    assert "ON u.business_id = s.business_id AND u.id = s.staff_id" in sql
    assert "s.business_id = :business_id AND s.token_sha256 = :token_sha256" in sql
    assert "s.kind = 'staff'" in sql
    assert all(f"s.{clause}" in sql for clause in LOOKUP_CLAUSES)
    assert "u.disabled_at IS NULL" in sql


def test_the_writes_name_the_business_and_take_their_times_from_the_database() -> None:
    insert, revoke, use = (
        store.INSERT_SESSION.text,
        store.REVOKE_SESSION.text,
        store.USE_LOGIN_LINK.text,
    )

    assert "now() + make_interval(secs => CAST(:lifetime_s AS double precision))" in insert
    assert not re.search(r"\b(id|created_at|revoked_at)\b,", insert.split("VALUES")[0])
    assert "SET revoked_at = now()" in revoke
    assert "business_id = :business_id AND id = :session_id" in revoke
    assert "revoked_at IS NULL AND expires_at > now()" in revoke
    # S5: one statement, used once, never after expiry.
    assert use.startswith("UPDATE staff_login_links SET used_at = now()")
    assert "business_id = :business_id AND token_sha256 = :token_sha256" in use
    assert "used_at IS NULL AND expires_at > now()" in use
    assert "RETURNING" in use
    # Zara's review: the member's state in the same statement, its business paired (a scalar
    # subquery: without the pairing it raises on a connection without RLS, never picks one).
    assert (
        "(SELECT u.disabled_at IS NULL FROM staff_users u"
        " WHERE u.business_id = staff_login_links.business_id"
        " AND u.id = staff_login_links.staff_id) AS enabled"
    ) in use


# ── The lookups ─────────────────────────────────────────────────────────────────────────────


async def test_a_live_customer_session_gives_its_business_id_ref_and_language() -> None:
    connection = ScriptedConnection([[customer_row()]])

    found = await find_customer_session(as_connection(connection), PARSED)

    assert found == CustomerSession(BUSINESS, SESSION_ID, f"s-{SESSION_ID.hex}", "en")
    assert connection.executed == [
        (store.CUSTOMER_SESSION.text, {"business_id": BUSINESS, "token_sha256": PARSED.sha256})
    ]


async def test_a_staff_session_gives_its_member_and_role() -> None:
    connection = ScriptedConnection([[staff_row(role="admin")]])

    found = await find_staff_session(as_connection(connection), PARSED)

    assert found == StaffSession(
        BUSINESS, SESSION_ID, STAFF_ID, StaffRole.ADMIN, "Synthetic Reviewer"
    )


Lookup = Callable[[AsyncConnection, ParsedToken], Awaitable[object]]
LOOKUPS: Final = [
    pytest.param(find_customer_session, customer_row, id="customer"),
    pytest.param(find_staff_session, staff_row, id="staff"),
]


@pytest.mark.parametrize(("lookup", "row"), LOOKUPS)
async def test_no_row_or_another_hash_is_none(
    lookup: Lookup, row: Callable[..., SimpleNamespace]
) -> None:
    other = row(token_sha256=token_sha256(TOKEN + "x"))

    assert await lookup(as_connection(ScriptedConnection([[]])), PARSED) is None
    assert await lookup(as_connection(ScriptedConnection([[other]])), PARSED) is None


@pytest.mark.parametrize(("lookup", "row"), LOOKUPS)
async def test_two_rows_raise_instead_of_choosing(
    lookup: Lookup, row: Callable[..., SimpleNamespace]
) -> None:
    with pytest.raises(MultipleResultsFound):
        await lookup(as_connection(ScriptedConnection([[row(), row()]])), PARSED)


def test_roles_grant_as_p2_says() -> None:
    reviewer = StaffSession(BUSINESS, SESSION_ID, STAFF_ID, StaffRole.REVIEWER, "R")
    admin = StaffSession(BUSINESS, SESSION_ID, STAFF_ID, StaffRole.ADMIN, "A")

    assert reviewer.may(StaffRole.REVIEWER) and not reviewer.may(StaffRole.ADMIN)
    assert admin.may(StaffRole.REVIEWER) and admin.may(StaffRole.ADMIN)


# ── S4: customer_ref from the session ──────────────────────────────────────────────────────


def test_the_customer_ref_is_s_and_the_uuid_hex_and_fits_the_tool_core() -> None:
    session_id = uuid4()
    ref = customer_ref_for(session_id)

    assert ref == CUSTOMER_REF_PREFIX + session_id.hex
    assert len(ref) == 34  # 2 + 32, arithmetic
    low, high = CUSTOMER_REF_LENGTH
    assert re.fullmatch(CUSTOMER_REF_PATTERN, ref) and low <= len(ref) <= high
    assert customer_ref_for(UUID(int=0)) == "s-" + "0" * 32


# ── Writes ─────────────────────────────────────────────────────────────────────────────────

EXPIRES: Final = datetime(2026, 11, 25, tzinfo=UTC)


async def test_a_customer_session_is_stored_with_its_language_and_no_member() -> None:
    connection = ScriptedConnection([[SimpleNamespace(id=SESSION_ID, expires_at=EXPIRES)]])

    stored = await insert_session(
        as_connection(connection),
        PARSED,
        kind=SessionKind.CUSTOMER,
        lifetime_s=43_200,
        ui_language="hi",
    )

    assert stored == NewSession(SESSION_ID, EXPIRES)
    assert connection.executed == [
        (
            store.INSERT_SESSION.text,
            {
                "business_id": BUSINESS,
                "kind": "customer",
                "staff_id": None,
                "token_sha256": PARSED.sha256,
                "channel": Channel.WEB.value,
                "ui_language": "hi",
                "lifetime_s": 43_200,
            },
        )
    ]


async def test_a_staff_session_is_stored_with_its_member_and_no_language() -> None:
    connection = ScriptedConnection([[SimpleNamespace(id=SESSION_ID, expires_at=EXPIRES)]])

    await insert_session(
        as_connection(connection),
        PARSED,
        kind=SessionKind.STAFF,
        lifetime_s=28_800,
        staff_id=STAFF_ID,
    )

    params = connection.executed[0][1]
    assert (params["kind"], params["staff_id"], params["ui_language"]) == ("staff", STAFF_ID, None)


@pytest.mark.parametrize(
    ("kind", "staff_id", "ui_language", "lifetime_s"),
    [
        (SessionKind.CUSTOMER, STAFF_ID, "en", 60),
        (SessionKind.CUSTOMER, None, None, 60),
        (SessionKind.STAFF, None, None, 60),
        (SessionKind.STAFF, STAFF_ID, "en", 60),
        (SessionKind.CUSTOMER, None, "en", 0),
        (SessionKind.CUSTOMER, None, "en", MAX_SESSION_LIFETIME_S + 1),
    ],
)
async def test_a_wrong_shape_or_lifetime_is_refused_before_any_sql(
    kind: SessionKind, staff_id: UUID | None, ui_language: str | None, lifetime_s: int
) -> None:
    connection = ScriptedConnection()

    with pytest.raises(ValueError, match=r"lifetime|staff member|UI language"):
        await insert_session(
            as_connection(connection),
            PARSED,
            kind=kind,
            lifetime_s=lifetime_s,
            staff_id=staff_id,
            ui_language=ui_language,
        )
    assert connection.executed == []


def test_lifetimes_are_bounded_as_the_migrations_checks() -> None:
    assert (MAX_SESSION_LIFETIME_S, MAX_LINK_LIFETIME_S) == (86_400, 3_600)
    assert check_lifetime(1, 10) == 1 and check_lifetime(10, 10) == 10
    for bad in (0, -1, 11):
        with pytest.raises(ValueError, match="lifetime"):
            check_lifetime(bad, 10)


async def test_revoking_says_whether_a_live_session_was_revoked_now() -> None:
    connection = ScriptedConnection([[SimpleNamespace(id=SESSION_ID)], []])

    assert await revoke_session(as_connection(connection), BUSINESS, SESSION_ID) is True
    assert await revoke_session(as_connection(connection), BUSINESS, SESSION_ID) is False
    assert connection.executed[0] == (
        store.REVOKE_SESSION.text,
        {"business_id": BUSINESS, "session_id": SESSION_ID},
    )


def link_row(**changes: object) -> SimpleNamespace:
    """A row `USE_LOGIN_LINK ... RETURNING` gives: an enabled member's link of `BUSINESS`."""
    row = {
        "business_id": BUSINESS,
        "staff_id": STAFF_ID,
        "token_sha256": PARSED.sha256,
        "enabled": True,
    }
    return SimpleNamespace(**(row | changes))


async def test_using_a_link_gives_its_member_once() -> None:
    connection = ScriptedConnection([[link_row()], []])

    assert await use_login_link(as_connection(connection), PARSED) == UsedLink(
        BUSINESS, STAFF_ID, enabled=True
    )
    assert await use_login_link(as_connection(connection), PARSED) is None
    assert connection.executed[0] == (
        store.USE_LOGIN_LINK.text,
        {"business_id": BUSINESS, "token_sha256": PARSED.sha256},
    )


@pytest.mark.parametrize("enabled", [False, None], ids=["disabled", "member not found"])
async def test_a_disabled_or_missing_members_link_is_used_but_not_enabled(
    enabled: bool | None,
) -> None:
    connection = ScriptedConnection([[link_row(enabled=enabled)]])

    used = await use_login_link(as_connection(connection), PARSED)

    assert used == UsedLink(BUSINESS, STAFF_ID, enabled=False)  # NULL is never taken for True


async def test_a_link_row_with_another_hash_or_two_rows_never_logs_in() -> None:
    other = link_row(token_sha256=token_sha256(TOKEN + "x"))
    assert await use_login_link(as_connection(ScriptedConnection([[other]])), PARSED) is None
    with pytest.raises(MultipleResultsFound):
        await use_login_link(as_connection(ScriptedConnection([[other, other]])), PARSED)
