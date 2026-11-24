"""The owner's statements without a database (M8 PR 1; S3, S5, D4 (a)): what each one says in its
own SQL and sends as parameters. Against Postgres: `tests/staff/test_cli_db.py`."""

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Final

import pytest

from tests.auth.fakes import BUSINESS, STAFF_ID, ScriptedConnection, as_connection
from vectapilot_api.auth.store import MAX_LINK_LIFETIME_S
from vectapilot_api.auth.tokens import SHA256_PATTERN, parse_token, token_sha256
from vectapilot_api.staff import owner
from vectapilot_api.staff.owner import disable_member, issue_link

pytestmark = pytest.mark.anyio

EXPIRES: Final = datetime(2026, 11, 24, 12, 15, tzinfo=UTC)

# ── issue_link ───────────────────────────────────────────────────────────────────────────────


def test_a_link_is_issued_only_for_an_enabled_member_of_the_business() -> None:
    sql = owner.ISSUE_LINK.text

    assert "WHERE business_id = :business_id AND id = :staff_id AND disabled_at IS NULL" in sql
    assert "now() + make_interval(secs => CAST(:lifetime_s AS double precision))" in sql


async def test_a_link_stores_only_its_tokens_hash() -> None:
    connection = ScriptedConnection([[SimpleNamespace(expires_at=EXPIRES)]])

    issued = await issue_link(as_connection(connection), BUSINESS, STAFF_ID, lifetime_s=900)

    assert issued is not None and issued.expires_at == EXPIRES
    assert parse_token(issued.token).business_id == BUSINESS
    [(statement, params)] = connection.executed
    assert statement == owner.ISSUE_LINK.text
    assert params == {
        "business_id": BUSINESS,
        "staff_id": STAFF_ID,
        "token_sha256": token_sha256(issued.token),
        "lifetime_s": 900,
    }
    assert re.fullmatch(SHA256_PATTERN, params["token_sha256"])
    assert issued.token not in str(params)  # S3: the token never reaches the database


async def test_no_enabled_member_gets_no_link() -> None:
    connection = ScriptedConnection([[]])

    assert await issue_link(as_connection(connection), BUSINESS, STAFF_ID, lifetime_s=900) is None


@pytest.mark.parametrize("lifetime_s", [0, MAX_LINK_LIFETIME_S + 1])
async def test_a_link_lifetime_outside_the_check_is_refused_before_any_sql(lifetime_s: int) -> None:
    connection = ScriptedConnection([[SimpleNamespace(expires_at=EXPIRES)]])

    with pytest.raises(ValueError, match="lifetime"):
        await issue_link(as_connection(connection), BUSINESS, STAFF_ID, lifetime_s=lifetime_s)

    assert connection.executed == []


# ── disable_member (zara's review: disabling also revokes) ─────────────────────────────────────


def test_disabling_names_the_business_and_revokes_only_that_members_live_sessions() -> None:
    disable, revoke = owner.DISABLE_MEMBER.text, owner.REVOKE_MEMBER_SESSIONS.text

    assert "business_id = :business_id AND id = :staff_id AND disabled_at IS NULL" in disable
    assert revoke.startswith("UPDATE sessions SET revoked_at = now()")
    assert "business_id = :business_id AND staff_id = :staff_id AND revoked_at IS NULL" in revoke


async def test_disabling_a_member_revokes_their_sessions_in_the_same_transaction() -> None:
    connection = ScriptedConnection([[SimpleNamespace(id=STAFF_ID)], []])

    assert await disable_member(as_connection(connection), BUSINESS, STAFF_ID) is True
    params = {"business_id": BUSINESS, "staff_id": STAFF_ID}
    assert connection.executed == [
        (owner.DISABLE_MEMBER.text, params),
        (owner.REVOKE_MEMBER_SESSIONS.text, params),
    ]


async def test_no_enabled_member_changes_nothing() -> None:
    connection = ScriptedConnection([[]])

    assert await disable_member(as_connection(connection), BUSINESS, STAFF_ID) is False
    assert [statement for statement, _ in connection.executed] == [owner.DISABLE_MEMBER.text]
